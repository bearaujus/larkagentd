from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import policy
from .providers.base import namespace_for
from .sanitize import sanitize
from .store import KnowledgeError
from .tables import (
    MEMORY_CATEGORIES,
    gb_index_entries,
    gb_memory,
    gb_memory_revisions,
    gb_memory_sources,
    gb_messages,
    gb_source_refs,
)

IDENTIFIER = re.compile(r"(?=.*[0-9_])[A-Za-z0-9_.-]{3,}|[A-Z]{3,}")
TOKEN = re.compile(r"[A-Za-z0-9_.-]{2,}")
STOPWORDS = frozenset(
    "a an and are as at be by did do for from how i in is it my of on or the this that to was we what when where who why with".split()
)


def _terms(query):
    words = TOKEN.findall(query)
    identifiers = list(
        dict.fromkeys(word.lower() for word in words if IDENTIFIER.fullmatch(word))
    )
    keywords = list(
        dict.fromkeys(word.lower() for word in words if word.lower() not in STOPWORDS)
    )
    return keywords, identifiers


def _score(text, keywords, identifiers):
    normalized = text.lower()
    hits = sum(keyword in normalized for keyword in keywords)
    exact = sum(identifier in normalized for identifier in identifiers)
    return exact * 3.0 + hits / max(1, len(keywords)) if hits or exact else 0.0


class Memory:
    """Immutable, group-scoped collective memory and its review lifecycle."""

    def __init__(self, runtime=None):
        self.runtime = runtime

    @staticmethod
    def _hash(content):
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _check_content(content):
        result = sanitize(content, max_chars=4000)
        if result.blocked:
            return None
        return result.text

    @staticmethod
    def _message(conn, group_key, message_id):
        return (
            conn.execute(
                select(gb_messages).where(
                    gb_messages.c.group_key == group_key,
                    gb_messages.c.lark_message_id == message_id,
                    gb_messages.c.status == "active",
                    gb_messages.c.visibility_status == "visible",
                    gb_messages.c.expires_at > func.now(),
                )
            )
            .mappings()
            .first()
        )

    @classmethod
    def _add_sources(
        cls, conn, group_key, memory_id, revision, source_ids, verified_source_ids=()
    ):
        refs = []
        verified = set(verified_source_ids)
        for source_id in dict.fromkeys(source_ids or ()):
            message = cls._message(conn, group_key, source_id)
            if message is not None:
                canonical_id = message["id"]
                source_revision = message["revision"]
                lark_message_id = message["lark_message_id"]
                link_type = "message" if message["message_link"] else "none"
                source_url = message["message_link"]
            elif source_id in verified:
                canonical_id = source_id
                source_revision = 1
                lark_message_id = source_id
                link_type = "none"
                source_url = None
            else:
                continue
            existing = (
                conn.execute(
                    select(gb_source_refs).where(
                        gb_source_refs.c.group_key == group_key,
                        gb_source_refs.c.source_kind == "message",
                        gb_source_refs.c.canonical_source_id == canonical_id,
                        gb_source_refs.c.source_revision == source_revision,
                    )
                )
                .mappings()
                .first()
            )
            if existing is None:
                source_ref_id = str(uuid.uuid4())
                conn.execute(
                    pg_insert(gb_source_refs)
                    .values(
                        id=source_ref_id,
                        group_key=group_key,
                        source_kind="message",
                        canonical_source_id=canonical_id,
                        source_revision=source_revision,
                        lark_message_id=lark_message_id,
                        verified_at=func.now(),
                        visibility_status="visible",
                        link_type=link_type,
                        source_url=source_url,
                    )
                    .on_conflict_do_nothing(constraint="gb_source_refs_unique")
                )
                existing = (
                    conn.execute(
                        select(gb_source_refs).where(
                            gb_source_refs.c.group_key == group_key,
                            gb_source_refs.c.source_kind == "message",
                            gb_source_refs.c.canonical_source_id == canonical_id,
                            gb_source_refs.c.source_revision == source_revision,
                        )
                    )
                    .mappings()
                    .first()
                )
            if existing is not None:
                conn.execute(
                    pg_insert(gb_memory_sources)
                    .values(
                        group_key=group_key,
                        memory_id=memory_id,
                        memory_revision=revision,
                        source_ref_id=existing["id"],
                    )
                    .on_conflict_do_nothing(constraint="gb_memory_sources_pk")
                )
                refs.append(dict(existing))
        return refs

    def propose(
        self,
        conn,
        group_key,
        proposer_id,
        content,
        category,
        source_message_ids=(),
        now=None,
        **kwargs,
    ):
        if not isinstance(category, str) or category not in MEMORY_CATEGORIES:
            raise KnowledgeError("invalid_category")
        text = self._check_content(content)
        if text is None:
            return {"state": "refused", "reason": "sensitive"}
        digest = self._hash(text)
        current = conn.execute(
            select(gb_memory.c.id, gb_memory.c.current_revision, gb_memory.c.state)
            .join(
                gb_memory_revisions,
                (gb_memory_revisions.c.group_key == gb_memory.c.group_key)
                & (gb_memory_revisions.c.memory_id == gb_memory.c.id),
            )
            .where(
                gb_memory.c.group_key == group_key,
                gb_memory_revisions.c.content_hash == digest,
                gb_memory_revisions.c.status.in_(("pending", "approved", "active")),
            )
            .order_by(gb_memory.c.id)
            .limit(1)
        ).first()
        if current:
            return {
                "memory_id": current.id,
                "revision": current.current_revision or 1,
                "state": current.state,
                "duplicate": True,
            }
        memory_id = str(uuid.uuid4())
        created = now or datetime.now(timezone.utc)
        conn.execute(
            gb_memory.insert().values(
                id=memory_id,
                group_key=group_key,
                category=category,
                current_revision=None,
                state="pending",
                created_by=proposer_id,
                created_at=created,
            )
        )
        revision = 1
        conn.execute(
            gb_memory_revisions.insert().values(
                group_key=group_key,
                memory_id=memory_id,
                revision=revision,
                sanitized_content=text,
                content_hash=digest,
                proposed_by=proposer_id,
                status="pending",
                evidence_status="explicit",
                identifiers=IDENTIFIER.findall(text),
                created_at=created,
            )
        )
        self._add_sources(
            conn,
            group_key,
            memory_id,
            revision,
            source_message_ids,
            kwargs.get("verified_source_ids", ()),
        )
        return {
            "memory_id": memory_id,
            "revision": revision,
            "state": "pending",
            "duplicate": False,
        }

    def correct(
        self,
        conn,
        group_key,
        proposer_id,
        memory_id,
        replacement,
        reason,
        source_ids=(),
        now=None,
        **kwargs,
    ):
        parent = (
            conn.execute(
                select(gb_memory).where(
                    gb_memory.c.group_key == group_key, gb_memory.c.id == memory_id
                )
            )
            .mappings()
            .first()
        )
        if parent is None:
            return {"status": "not_found"}
        text = self._check_content(replacement)
        if text is None:
            return {"status": "refused", "reason": "sensitive"}
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise KnowledgeError("invalid_reason")
        clean_reason = sanitize(reason, max_chars=1000)
        if clean_reason.blocked or "truncated" in clean_reason.reasons:
            return {"status": "refused", "reason": "sensitive"}
        reason_text = clean_reason.text
        last = conn.execute(
            select(func.max(gb_memory_revisions.c.revision)).where(
                gb_memory_revisions.c.group_key == group_key,
                gb_memory_revisions.c.memory_id == memory_id,
            )
        ).scalar_one()
        revision = int(last or 0) + 1
        created = now or datetime.now(timezone.utc)
        conn.execute(
            gb_memory_revisions.insert().values(
                group_key=group_key,
                memory_id=memory_id,
                revision=revision,
                sanitized_content=text,
                content_hash=self._hash(text),
                proposed_by=proposer_id,
                status="pending",
                evidence_status="explicit",
                reason=reason_text,
                identifiers=IDENTIFIER.findall(text),
                created_at=created,
            )
        )
        if parent["current_revision"] is None:
            conn.execute(
                update(gb_memory)
                .where(gb_memory.c.group_key == group_key, gb_memory.c.id == memory_id)
                .values(state="pending")
            )
        self._add_sources(
            conn,
            group_key,
            memory_id,
            revision,
            source_ids,
            kwargs.get("verified_source_ids", ()),
        )
        return {
            "memory_id": memory_id,
            "revision": revision,
            "state": "pending",
            "duplicate": False,
        }

    def approve(
        self,
        conn,
        group_key,
        memory_id,
        revision,
        expected_content_hash,
        reviewer_email,
        confirm_sharing,
        now=None,
    ):
        if confirm_sharing is not True:
            return {"status": "sharing_confirmation_required"}
        if not isinstance(revision, int) or isinstance(revision, bool):
            return {"status": "stale_revision"}
        memory = (
            conn.execute(
                select(gb_memory).where(
                    gb_memory.c.group_key == group_key, gb_memory.c.id == memory_id
                )
            )
            .mappings()
            .first()
        )
        row = (
            conn.execute(
                select(gb_memory_revisions).where(
                    gb_memory_revisions.c.group_key == group_key,
                    gb_memory_revisions.c.memory_id == memory_id,
                    gb_memory_revisions.c.revision == revision,
                )
            )
            .mappings()
            .first()
        )
        if memory is None or row is None:
            return {"status": "not_found"}
        if row["status"] != "pending":
            return {"status": "not_pending"}
        if row["content_hash"] != expected_content_hash:
            return {"status": "hash_mismatch"}
        settings = policy.store.settings(conn, group_key)
        if settings.get("review_required") is False:
            return {"status": "review_required_locked"}
        current_time = now or datetime.now(timezone.utc)
        has_index = bool(
            self.runtime
            and self.runtime.index
            and self.runtime.config.embedding_configured
        )
        conn.execute(
            update(gb_memory_revisions)
            .where(
                gb_memory_revisions.c.group_key == group_key,
                gb_memory_revisions.c.memory_id == memory_id,
                gb_memory_revisions.c.revision == revision,
                gb_memory_revisions.c.status == "pending",
            )
            .values(
                status="approved",
                approved_by=reviewer_email,
                approved_at=current_time,
                expires_at=current_time
                + timedelta(
                    days=self.runtime.config.memory_review_days if self.runtime else 90
                ),
            )
        )
        if has_index:
            if memory["current_revision"] is None or memory["state"] != "active":
                conn.execute(
                    update(gb_memory)
                    .where(
                        gb_memory.c.group_key == group_key,
                        gb_memory.c.id == memory_id,
                    )
                    .values(state="approved_pending_index")
                )
            return {"status": "approved_pending_index", "lexical_only": False}
        if memory["current_revision"] is not None:
            conn.execute(
                update(gb_memory_revisions)
                .where(
                    gb_memory_revisions.c.group_key == group_key,
                    gb_memory_revisions.c.memory_id == memory_id,
                    gb_memory_revisions.c.revision == memory["current_revision"],
                    gb_memory_revisions.c.status == "active",
                )
                .values(status="superseded")
            )
        conn.execute(
            update(gb_memory_revisions)
            .where(
                gb_memory_revisions.c.group_key == group_key,
                gb_memory_revisions.c.memory_id == memory_id,
                gb_memory_revisions.c.revision == revision,
            )
            .values(status="active")
        )
        conn.execute(
            update(gb_memory)
            .where(gb_memory.c.group_key == group_key, gb_memory.c.id == memory_id)
            .values(state="active", current_revision=revision)
        )
        return {"status": "active", "lexical_only": True}

    def reject(self, conn, group_key, memory_id, revision, reason_code):
        if not isinstance(reason_code, str) or not re.fullmatch(
            r"[a-z0-9_]{1,64}", reason_code
        ):
            raise KnowledgeError("invalid_reason")
        result = conn.execute(
            update(gb_memory_revisions)
            .where(
                gb_memory_revisions.c.group_key == group_key,
                gb_memory_revisions.c.memory_id == memory_id,
                gb_memory_revisions.c.revision == revision,
                gb_memory_revisions.c.status == "pending",
            )
            .values(status="rejected", reason=reason_code)
        )
        if result.rowcount != 1:
            return {"status": "not_found"}
        conn.execute(
            update(gb_memory)
            .where(
                gb_memory.c.group_key == group_key,
                gb_memory.c.id == memory_id,
                gb_memory.c.current_revision.is_(None),
                gb_memory.c.state == "pending",
            )
            .values(state="failed")
        )
        return {"status": "rejected"}

    def revoke(self, conn, group_key, memory_id, reviewer_email=None):
        row = (
            conn.execute(
                select(gb_memory).where(
                    gb_memory.c.group_key == group_key, gb_memory.c.id == memory_id
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return {"status": "not_found"}
        conn.execute(
            update(gb_memory)
            .where(gb_memory.c.group_key == group_key, gb_memory.c.id == memory_id)
            .values(state="revoked")
        )
        conn.execute(
            update(gb_memory_revisions)
            .where(
                gb_memory_revisions.c.group_key == group_key,
                gb_memory_revisions.c.memory_id == memory_id,
                gb_memory_revisions.c.status.in_(("active", "approved", "pending")),
            )
            .values(status="revoked")
        )
        conn.execute(
            update(gb_index_entries)
            .where(
                gb_index_entries.c.group_key == group_key,
                gb_index_entries.c.kind == "memory",
                gb_index_entries.c.canonical_id == memory_id,
                gb_index_entries.c.state == "active",
            )
            .values(state="delete_pending")
        )
        return {"status": "revoked", "dependants_suspended": 0}

    def suspend_for_source(self, conn, group_key, source_ref_id):
        supported = conn.execute(
            select(
                gb_memory_sources.c.memory_id, gb_memory_sources.c.memory_revision
            ).where(
                gb_memory_sources.c.group_key == group_key,
                gb_memory_sources.c.source_ref_id == source_ref_id,
            )
        ).all()
        affected = set()
        for memory_id, revision in supported:
            permitted = conn.execute(
                select(gb_memory_sources.c.source_ref_id)
                .join(
                    gb_source_refs,
                    gb_source_refs.c.id == gb_memory_sources.c.source_ref_id,
                )
                .where(
                    gb_memory_sources.c.group_key == group_key,
                    gb_memory_sources.c.memory_id == memory_id,
                    gb_memory_sources.c.memory_revision == revision,
                    gb_memory_sources.c.source_ref_id != source_ref_id,
                    gb_source_refs.c.visibility_status == "visible",
                )
            ).first()
            if permitted is None:
                conn.execute(
                    update(gb_memory_revisions)
                    .where(
                        gb_memory_revisions.c.group_key == group_key,
                        gb_memory_revisions.c.memory_id == memory_id,
                        gb_memory_revisions.c.revision == revision,
                        gb_memory_revisions.c.status.in_(
                            ("pending", "approved", "active")
                        ),
                    )
                    .values(status="suspended")
                )
                conn.execute(
                    update(gb_memory)
                    .where(
                        gb_memory.c.group_key == group_key,
                        gb_memory.c.id == memory_id,
                        or_(
                            gb_memory.c.current_revision == revision,
                            and_(
                                gb_memory.c.current_revision.is_(None),
                                gb_memory.c.state.in_(
                                    ("pending", "approved_pending_index")
                                ),
                            ),
                        ),
                    )
                    .values(state="suspended")
                )
                conn.execute(
                    update(gb_index_entries)
                    .where(
                        gb_index_entries.c.group_key == group_key,
                        gb_index_entries.c.kind == "memory",
                        gb_index_entries.c.canonical_id == memory_id,
                        gb_index_entries.c.source_revision == revision,
                        gb_index_entries.c.state == "active",
                    )
                    .values(state="delete_pending")
                )
                affected.add(memory_id)
        return sorted(affected)

    def activate_after_index(
        self, conn, group_key, memory_id, revision, entry_ids, fence, generation
    ):
        if self.runtime is None:
            return False
        # The worker passes a fence token; verify its exact owning job where available.
        from ..core.store import database_time
        from .tables import gb_jobs

        job_candidates = (
            conn.execute(
                select(gb_jobs).where(
                    gb_jobs.c.group_key == group_key,
                    gb_jobs.c.kind == "index_revision",
                    gb_jobs.c.fence == fence,
                    gb_jobs.c.generation == generation,
                    gb_jobs.c.state == "running",
                )
            )
            .mappings()
            .all()
        )
        job = next(
            (
                candidate
                for candidate in job_candidates
                if candidate["ref_payload"].get("kind") == "memory"
                and candidate["ref_payload"].get("memory_id") == memory_id
                and candidate["ref_payload"].get("revision") == revision
            ),
            None,
        )
        lease_now = float(conn.execute(select(database_time())).scalar_one())
        if (
            job is None
            or job["lease_until"] <= lease_now
            or not policy.still_valid(
                conn, self.runtime.config, group_key, generation, "memory"
            )
        ):
            return False
        latest_revision = conn.execute(
            select(func.max(gb_memory_revisions.c.revision)).where(
                gb_memory_revisions.c.group_key == group_key,
                gb_memory_revisions.c.memory_id == memory_id,
            )
        ).scalar_one()
        if latest_revision != revision:
            return False
        memory = (
            conn.execute(
                select(gb_memory).where(
                    gb_memory.c.group_key == group_key,
                    gb_memory.c.id == memory_id,
                    gb_memory.c.state.in_(("active", "approved_pending_index")),
                )
            )
            .mappings()
            .first()
        )
        rev = (
            conn.execute(
                select(gb_memory_revisions).where(
                    gb_memory_revisions.c.group_key == group_key,
                    gb_memory_revisions.c.memory_id == memory_id,
                    gb_memory_revisions.c.revision == revision,
                    gb_memory_revisions.c.status == "approved",
                )
            )
            .mappings()
            .first()
        )
        if memory is None or rev is None:
            return False
        rows = conn.execute(
            select(gb_index_entries.c.id).where(
                gb_index_entries.c.group_key == group_key,
                gb_index_entries.c.kind == "memory",
                gb_index_entries.c.canonical_id == memory_id,
                gb_index_entries.c.source_revision == revision,
                gb_index_entries.c.id.in_(entry_ids),
                gb_index_entries.c.state == "indexing",
                gb_index_entries.c.generation == generation,
                gb_index_entries.c.provider_id.is_not(None),
            )
        ).all()
        if len(rows) != len(entry_ids):
            return False
        conn.execute(
            update(gb_index_entries)
            .where(
                gb_index_entries.c.group_key == group_key,
                gb_index_entries.c.kind == "memory",
                gb_index_entries.c.canonical_id == memory_id,
                gb_index_entries.c.source_revision == revision,
                gb_index_entries.c.id.in_(entry_ids),
                gb_index_entries.c.state == "indexing",
                gb_index_entries.c.generation == generation,
                gb_index_entries.c.provider_id.is_not(None),
            )
            .values(state="active", indexed_at=func.now())
        )
        if memory["current_revision"] is not None:
            conn.execute(
                update(gb_memory_revisions)
                .where(
                    gb_memory_revisions.c.group_key == group_key,
                    gb_memory_revisions.c.memory_id == memory_id,
                    gb_memory_revisions.c.revision == memory["current_revision"],
                    gb_memory_revisions.c.status == "active",
                )
                .values(status="superseded")
            )
        conn.execute(
            update(gb_memory_revisions)
            .where(
                gb_memory_revisions.c.group_key == group_key,
                gb_memory_revisions.c.memory_id == memory_id,
                gb_memory_revisions.c.revision == revision,
                gb_memory_revisions.c.status == "approved",
            )
            .values(status="active")
        )
        conn.execute(
            update(gb_memory)
            .where(
                gb_memory.c.group_key == group_key,
                gb_memory.c.id == memory_id,
                gb_memory.c.state.in_(("active", "approved_pending_index")),
            )
            .values(state="active", current_revision=revision)
        )
        conn.execute(
            update(gb_index_entries)
            .where(
                gb_index_entries.c.group_key == group_key,
                gb_index_entries.c.kind == "memory",
                gb_index_entries.c.canonical_id == memory_id,
                gb_index_entries.c.source_revision != revision,
                gb_index_entries.c.state == "active",
            )
            .values(state="delete_pending")
        )
        return True

    def expire(self, conn, group_key, now=None):
        now = now or datetime.now(timezone.utc)
        rows = conn.execute(
            select(
                gb_memory_revisions.c.memory_id, gb_memory_revisions.c.revision
            ).where(
                gb_memory_revisions.c.group_key == group_key,
                gb_memory_revisions.c.status == "active",
                gb_memory_revisions.c.expires_at <= now,
            )
        ).all()
        for memory_id, revision in rows:
            conn.execute(
                update(gb_memory_revisions)
                .where(
                    gb_memory_revisions.c.group_key == group_key,
                    gb_memory_revisions.c.memory_id == memory_id,
                    gb_memory_revisions.c.revision == revision,
                )
                .values(status="suspended")
            )
            conn.execute(
                update(gb_memory)
                .where(
                    gb_memory.c.group_key == group_key,
                    gb_memory.c.id == memory_id,
                    gb_memory.c.current_revision == revision,
                )
                .values(state="suspended")
            )
            conn.execute(
                update(gb_index_entries)
                .where(
                    gb_index_entries.c.group_key == group_key,
                    gb_index_entries.c.kind == "memory",
                    gb_index_entries.c.canonical_id == memory_id,
                    gb_index_entries.c.source_revision == revision,
                    gb_index_entries.c.state == "active",
                )
                .values(state="delete_pending")
            )
        return len(rows)

    @staticmethod
    def reviewer_can_read(conn, group_key, email, memory_id=None):
        from ..core.store import gateway_identities

        if not isinstance(email, str) or not email:
            return False
        owners = (
            conn.execute(
                select(gateway_identities.c.owner).where(
                    func.lower(gateway_identities.c.email) == email.lower(),
                    gateway_identities.c.verified_at.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        open_ids = {owner.split(":", 1)[-1] for owner in owners}
        if not open_ids:
            return False
        if memory_id is not None:
            proposed = conn.execute(
                select(gb_memory.c.id).where(
                    gb_memory.c.group_key == group_key,
                    gb_memory.c.id == memory_id,
                    gb_memory.c.created_by.in_(open_ids),
                )
            ).first()
            if proposed is not None:
                return True
        return (
            conn.execute(
                select(gb_messages.c.id)
                .where(
                    gb_messages.c.group_key == group_key,
                    gb_messages.c.sender_id.in_(open_ids),
                    gb_messages.c.status == "active",
                    gb_messages.c.visibility_status == "visible",
                )
                .limit(1)
            ).first()
            is not None
        )

    def list_candidates(self, conn, group_key, can_read=False, reviewer_email=None):
        query = (
            select(gb_memory, gb_memory_revisions)
            .join(
                gb_memory_revisions,
                (gb_memory_revisions.c.group_key == gb_memory.c.group_key)
                & (gb_memory_revisions.c.memory_id == gb_memory.c.id),
            )
            .where(
                gb_memory.c.group_key == group_key,
                gb_memory_revisions.c.status == "pending",
            )
            .order_by(gb_memory_revisions.c.created_at)
        )
        results = []
        for row in conn.execute(query).mappings():
            item = {
                "memory_id": row["id"],
                "revision": row["revision"],
                "category": row["category"],
                "state": row["state"],
                "content_hash": row["content_hash"],
                "proposed_by": row["proposed_by"],
                "created_at": row["created_at"],
            }
            allowed = can_read or (
                reviewer_email
                and self.reviewer_can_read(conn, group_key, reviewer_email, row["id"])
            )
            if allowed:
                item["content"] = row["sanitized_content"]
                item["reason"] = row["reason"]
            results.append(item)
        return results

    def search(self, runtime, conn, group_key, query, limit=5):
        state = policy.effective(conn, runtime.config, group_key)
        if not state.allows("memory"):
            return {"status": "disabled", "results": []}
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > 1000
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 10
        ):
            return {"status": "invalid_request", "results": []}
        now = datetime.now(timezone.utc)
        base = (
            select(
                gb_memory.c.id,
                gb_memory.c.category,
                gb_memory.c.current_revision,
                gb_memory_revisions.c.sanitized_content,
                gb_memory_revisions.c.approved_at,
                gb_memory_revisions.c.content_hash,
            )
            .join(
                gb_memory_revisions,
                (gb_memory_revisions.c.group_key == gb_memory.c.group_key)
                & (gb_memory_revisions.c.memory_id == gb_memory.c.id)
                & (gb_memory_revisions.c.revision == gb_memory.c.current_revision),
            )
            .where(
                gb_memory.c.group_key == group_key,
                gb_memory.c.state == "active",
                gb_memory_revisions.c.status == "active",
                gb_memory_revisions.c.expires_at > now,
            )
        )
        all_rows = conn.execute(base).mappings().all()
        keywords, identifiers = _terms(query)
        lexical = [
            r
            for r in all_rows
            if _score(r["sanitized_content"], keywords, identifiers) > 0
        ]
        lexical.sort(
            key=lambda r: (
                -_score(r["sanitized_content"], keywords, identifiers),
                r["id"],
            )
        )
        rankings = {}
        for rank, row in enumerate(lexical[: limit * 3], 1):
            rankings[row["id"]] = rankings.get(row["id"], 0) + 1 / (60 + rank)
        if runtime.index is not None and runtime.config.embedding_configured:
            namespace = namespace_for(runtime.config.collection, group_key, "memory")
            hits = runtime.index.search(
                namespace=namespace, query=query, top_k=limit * 3
            )
            valid_entries = {
                r["provider_id"]: r
                for r in conn.execute(
                    select(gb_index_entries).where(
                        gb_index_entries.c.group_key == group_key,
                        gb_index_entries.c.kind == "memory",
                        gb_index_entries.c.state == "active",
                        gb_index_entries.c.canonical_id.in_(
                            [row["id"] for row in all_rows] or [""]
                        ),
                        gb_index_entries.c.generation == state.generation,
                    )
                ).mappings()
                if r["provider_id"]
            }
            by_id = {r["id"]: r for r in all_rows}
            for rank, hit in enumerate(hits, 1):
                entry = valid_entries.get(hit.provider_id)
                row = by_id.get(entry["canonical_id"]) if entry else None
                if (
                    row is None
                    or entry["source_revision"] != row["current_revision"]
                    or entry["namespace"] != namespace
                ):
                    continue
                rankings[row["id"]] = rankings.get(row["id"], 0) + 1 / (60 + rank)
        by_id = {r["id"]: r for r in all_rows}
        results = []
        for memory_id in sorted(rankings, key=lambda item: (-rankings[item], item))[
            :limit
        ]:
            row = by_id[memory_id]
            sources = (
                conn.execute(
                    select(
                        gb_source_refs.c.id,
                        gb_source_refs.c.lark_message_id,
                        gb_source_refs.c.source_url,
                        gb_source_refs.c.link_type,
                    )
                    .join(
                        gb_memory_sources,
                        gb_memory_sources.c.source_ref_id == gb_source_refs.c.id,
                    )
                    .where(
                        gb_memory_sources.c.group_key == group_key,
                        gb_memory_sources.c.memory_id == memory_id,
                        gb_memory_sources.c.memory_revision == row["current_revision"],
                        gb_source_refs.c.group_key == group_key,
                        gb_source_refs.c.visibility_status == "visible",
                    )
                )
                .mappings()
                .all()
            )
            results.append(
                {
                    "memory_id": memory_id,
                    "category": row["category"],
                    "content": row["sanitized_content"],
                    "revision": row["current_revision"],
                    "approved_at": row["approved_at"],
                    "sources": [
                        {
                            "source_id": s["lark_message_id"],
                            "url": s["source_url"]
                            if s["link_type"] in ("message", "group_fallback")
                            else None,
                            "verified": bool(
                                s["source_url"] or s["link_type"] == "none"
                            ),
                        }
                        for s in sources
                    ],
                    "state": "approved collective memory",
                }
            )
        return {"status": "ok" if results else "partial", "results": results}
