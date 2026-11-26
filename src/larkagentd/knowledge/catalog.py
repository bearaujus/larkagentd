from __future__ import annotations

import hashlib
import re
import uuid
from datetime import timedelta, timezone

from sqlalchemy import and_, delete, func, literal, or_, select, update

from .sanitize import sanitize
from .tables import (
    gb_discussion_messages,
    gb_discussion_participants,
    gb_discussions,
    gb_index_entries,
    gb_messages,
)

IDENTIFIER = re.compile(r"(?=[A-Za-z0-9_.\-]*[0-9_])[A-Za-z0-9_.\-]{3,}|[A-Z]{3,}")


class Catalog:
    """Canonical, group-scoped conversation catalog. All I/O uses the caller's transaction."""

    @staticmethod
    def _utc(moment):
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

    @staticmethod
    def _hash(text):
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    def upsert_messages(
        self, conn, group_key, messages, texts=None, retention_days=30, now=None
    ):

        texts = texts or {}
        results = {}
        for message in messages:
            raw = texts.get(message.message_id, message.text)
            clean = sanitize(raw)
            text = None if clean.blocked else clean.text
            digest = self._hash(text if text is not None else "blocked")
            existing = (
                conn.execute(
                    select(gb_messages).where(
                        gb_messages.c.group_key == group_key,
                        gb_messages.c.lark_message_id == message.message_id,
                    )
                )
                .mappings()
                .first()
            )
            expires = self._utc(message.created_at) + timedelta(days=retention_days)
            values = dict(
                thread_id=message.thread_id,
                root_id=message.root_id,
                parent_id=message.parent_id,
                sender_type=message.sender_type,
                sender_id=message.sender_id,
                created_at=message.created_at,
                edited_at=getattr(message, "edited_at", None),
                message_link=message.link,
                sanitized_text=text,
                content_hash=digest,
                visibility_status="blocked" if text is None else "visible",
                expires_at=expires,
                status="active",
            )
            if existing is None:
                message_id = str(uuid.uuid4())
                conn.execute(
                    gb_messages.insert().values(
                        id=message_id,
                        group_key=group_key,
                        lark_message_id=message.message_id,
                        revision=1,
                        **values,
                    )
                )
                results[message.message_id] = (message_id, True)
            else:
                changed = existing["content_hash"] != digest
                if changed:
                    conn.execute(
                        update(gb_messages)
                        .where(
                            gb_messages.c.id == existing["id"],
                            gb_messages.c.group_key == group_key,
                        )
                        .values(**values, revision=existing["revision"] + 1)
                    )
                    affected = select(gb_discussion_messages.c.discussion_id).where(
                        gb_discussion_messages.c.group_key == group_key,
                        gb_discussion_messages.c.message_id == existing["id"],
                    )
                    conn.execute(
                        update(gb_discussions)
                        .where(
                            gb_discussions.c.group_key == group_key,
                            gb_discussions.c.id.in_(affected),
                        )
                        .values(status="suspended")
                    )
                    conn.execute(
                        update(gb_index_entries)
                        .where(
                            gb_index_entries.c.group_key == group_key,
                            gb_index_entries.c.kind == "discussion",
                            gb_index_entries.c.canonical_id.in_(affected),
                            gb_index_entries.c.state == "active",
                        )
                        .values(state="delete_pending")
                    )
                results[message.message_id] = (existing["id"], changed)
        return results

    def save_discussion(
        self, conn, group_key, discussion, texts, retention_days=30, now=None
    ):
        now = self._utc(now) if now is not None else None
        message_ids = tuple(discussion.message_ids)
        rows = (
            conn.execute(
                select(
                    gb_messages.c.id,
                    gb_messages.c.lark_message_id,
                    gb_messages.c.sanitized_text,
                    gb_messages.c.content_hash,
                    gb_messages.c.sender_id,
                    gb_messages.c.created_at,
                ).where(
                    gb_messages.c.group_key == group_key,
                    gb_messages.c.lark_message_id.in_(message_ids),
                    gb_messages.c.status == "active",
                    gb_messages.c.visibility_status == "visible",
                    gb_messages.c.sanitized_text.is_not(None),
                )
            )
            .mappings()
            .all()
            if message_ids
            else []
        )
        by_lark = {r["lark_message_id"]: r for r in rows}
        ordered = [by_lark[mid] for mid in message_ids if mid in by_lark]
        if not ordered:
            return {"discussion_id": None, "changed": False, "revision": None}
        source_texts = [
            texts.get(r["lark_message_id"], r["sanitized_text"]) for r in ordered
        ]
        title = re.split(r"(?<=[.!?])\s+", source_texts[0], maxsplit=1)[0][:120]
        summary = " ".join(source_texts[:8])[:1000]
        identifiers = sorted(
            {
                token.lower()
                for text in source_texts
                for token in IDENTIFIER.findall(text)
            }
        )
        native_thread_id = (
            discussion.native_thread_id if discussion.kind == "native_thread" else None
        )
        stable_key = discussion.window_key if discussion.kind == "chat_window" else None
        if discussion.kind == "chat_window" and not stable_key:
            raise ValueError("chat_window_requires_stable_key")
        identity = gb_discussions.c.group_key == group_key
        identity = identity & (
            gb_discussions.c.native_thread_id == native_thread_id
            if native_thread_id
            else gb_discussions.c.stable_window_key == stable_key
        )
        existing = (
            conn.execute(select(gb_discussions).where(identity)).mappings().first()
        )
        if existing:
            old_hashes = (
                conn.execute(
                    select(gb_messages.c.content_hash)
                    .select_from(
                        gb_discussion_messages.join(
                            gb_messages,
                            and_(
                                gb_discussion_messages.c.group_key
                                == gb_messages.c.group_key,
                                gb_discussion_messages.c.message_id == gb_messages.c.id,
                            ),
                        )
                    )
                    .where(
                        gb_discussion_messages.c.group_key == group_key,
                        gb_discussion_messages.c.discussion_id == existing["id"],
                    )
                    .order_by(gb_discussion_messages.c.ordinal)
                )
                .scalars()
                .all()
            )
            old_ids = (
                conn.execute(
                    select(gb_discussion_messages.c.message_id).where(
                        gb_discussion_messages.c.group_key == group_key,
                        gb_discussion_messages.c.discussion_id == existing["id"],
                    )
                )
                .scalars()
                .all()
            )
            new_ids = [r["id"] for r in ordered]
            changed = (
                old_ids != new_ids
                or old_hashes != [r["content_hash"] for r in ordered]
                or existing["status"] != "active"
                or existing["summary"] != summary
                or existing["title"] != title
            )
            did = existing["id"]
            if not changed:
                return {
                    "discussion_id": did,
                    "changed": False,
                    "revision": existing["source_revision"],
                }
            revision = existing["source_revision"] + 1
            conn.execute(
                update(gb_discussions)
                .where(
                    gb_discussions.c.group_key == group_key, gb_discussions.c.id == did
                )
                .values(
                    root_message_id=discussion.root_message_id,
                    title=title,
                    summary=summary,
                    started_at=discussion.started_at,
                    ended_at=discussion.ended_at,
                    coverage="partial" if discussion.partial else "complete",
                    identifiers=identifiers,
                    source_revision=revision,
                    status="active",
                    expires_at=(now + timedelta(days=retention_days))
                    if now
                    else func.now() + timedelta(days=retention_days),
                )
            )
            conn.execute(
                delete(gb_discussion_messages).where(
                    gb_discussion_messages.c.group_key == group_key,
                    gb_discussion_messages.c.discussion_id == did,
                )
            )
            conn.execute(
                delete(gb_discussion_participants).where(
                    gb_discussion_participants.c.group_key == group_key,
                    gb_discussion_participants.c.discussion_id == did,
                )
            )
        else:
            did = str(uuid.uuid4())
            revision = 1
            conn.execute(
                gb_discussions.insert().values(
                    id=did,
                    group_key=group_key,
                    kind=discussion.kind,
                    native_thread_id=native_thread_id,
                    stable_window_key=stable_key,
                    root_message_id=discussion.root_message_id,
                    title=title,
                    summary=summary,
                    started_at=discussion.started_at,
                    ended_at=discussion.ended_at,
                    coverage="partial" if discussion.partial else "complete",
                    identifiers=identifiers,
                    source_revision=revision,
                    expires_at=(now + timedelta(days=retention_days))
                    if now
                    else func.now() + timedelta(days=retention_days),
                )
            )
        for ordinal, row in enumerate(ordered):
            conn.execute(
                gb_discussion_messages.insert().values(
                    group_key=group_key,
                    discussion_id=did,
                    message_id=row["id"],
                    ordinal=ordinal,
                )
            )
        # Participants are sourced only from persisted message metadata, never text or caller claims.
        grouped = {}
        for row in ordered:
            sender = row["sender_id"]
            if sender:
                item = grouped.setdefault(
                    sender, [row["created_at"], row["created_at"], 0]
                )
                item[0] = min(item[0], row["created_at"])
                item[1] = max(item[1], row["created_at"])
                item[2] += 1
        for sender, (first, last, count) in grouped.items():
            conn.execute(
                gb_discussion_participants.insert().values(
                    group_key=group_key,
                    discussion_id=did,
                    sender_id=sender,
                    first_at=first,
                    last_at=last,
                    authored_count=count,
                )
            )
        return {"discussion_id": did, "changed": True, "revision": revision}

    def lexical_search(
        self,
        conn,
        group_key,
        keywords,
        identifiers,
        start=None,
        end=None,
        self_only=False,
        requester_id=None,
        limit=50,
    ):
        if not group_key:
            return []
        query = select(gb_discussions).where(
            gb_discussions.c.group_key == group_key,
            gb_discussions.c.status == "active",
            gb_discussions.c.expires_at > func.now(),
        )
        if start is not None:
            query = query.where(gb_discussions.c.ended_at >= start)
        if end is not None:
            query = query.where(gb_discussions.c.started_at < end)
        terms = [str(word) for word in keywords if word]
        ids = [str(token).lower() for token in identifiers if token]
        conditions = []
        if terms:
            tsquery = func.plainto_tsquery("simple", " ".join(terms))
            conditions.append(gb_discussions.c.search_vector.op("@@")(tsquery))
        if ids:
            conditions.append(gb_discussions.c.identifiers.op("&&")(ids))
        if not conditions:
            return []
        query = query.where(or_(*conditions))
        if self_only:
            if not requester_id:
                return []
            query = query.where(
                select(gb_discussion_participants.c.discussion_id)
                .where(
                    gb_discussion_participants.c.group_key == group_key,
                    gb_discussion_participants.c.discussion_id == gb_discussions.c.id,
                    gb_discussion_participants.c.sender_id == requester_id,
                )
                .exists()
            )
        rank = (
            func.ts_rank_cd(
                gb_discussions.c.search_vector,
                func.plainto_tsquery("simple", " ".join(terms)),
            )
            if terms
            else literal(0.0)
        )
        found = conn.execute(
            query.order_by(
                rank.desc(), gb_discussions.c.ended_at.desc(), gb_discussions.c.id
            ).limit(max(1, min(limit, 200)))
        ).mappings()
        results = []
        for row in found:
            participant_match = bool(
                requester_id
                and conn.execute(
                    select(gb_discussion_participants.c.discussion_id).where(
                        gb_discussion_participants.c.group_key == group_key,
                        gb_discussion_participants.c.discussion_id == row["id"],
                        gb_discussion_participants.c.sender_id == requester_id,
                    )
                ).first()
            )
            results.append(dict(row, participant_match=participant_match))
        return results

    def invalidate_message(self, conn, group_key, message_id):
        found = conn.execute(
            select(gb_messages.c.id).where(
                gb_messages.c.group_key == group_key,
                gb_messages.c.lark_message_id == message_id,
            )
        ).scalar()
        if found is None:
            return []
        discussions = (
            conn.execute(
                select(gb_discussion_messages.c.discussion_id).where(
                    gb_discussion_messages.c.group_key == group_key,
                    gb_discussion_messages.c.message_id == found,
                )
            )
            .scalars()
            .all()
        )
        conn.execute(
            update(gb_messages)
            .where(gb_messages.c.group_key == group_key, gb_messages.c.id == found)
            .values(visibility_status="recalled", sanitized_text=None, status="revoked")
        )
        if discussions:
            conn.execute(
                update(gb_discussions)
                .where(
                    gb_discussions.c.group_key == group_key,
                    gb_discussions.c.id.in_(discussions),
                )
                .values(status="suspended")
            )
            conn.execute(
                update(gb_index_entries)
                .where(
                    gb_index_entries.c.group_key == group_key,
                    gb_index_entries.c.kind == "discussion",
                    gb_index_entries.c.canonical_id.in_(discussions),
                    gb_index_entries.c.state.in_(("active", "stale", "indexing")),
                )
                .values(state="delete_pending")
            )
        return discussions

    def expire(self, conn, group_key, now):
        """Suppress expired content before clearing it; return number of source rows scrubbed."""
        now = self._utc(now)
        expired_messages = (
            conn.execute(
                select(gb_messages.c.id).where(
                    gb_messages.c.group_key == group_key,
                    gb_messages.c.expires_at <= now,
                    gb_messages.c.sanitized_text.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        discussions = set(
            conn.execute(
                select(gb_discussions.c.id).where(
                    gb_discussions.c.group_key == group_key,
                    gb_discussions.c.expires_at <= now,
                    gb_discussions.c.status == "active",
                )
            )
            .scalars()
            .all()
        )
        if expired_messages:
            discussions.update(
                conn.execute(
                    select(gb_discussion_messages.c.discussion_id).where(
                        gb_discussion_messages.c.group_key == group_key,
                        gb_discussion_messages.c.message_id.in_(expired_messages),
                    )
                )
                .scalars()
                .all()
            )
        if discussions:
            conn.execute(
                update(gb_discussions)
                .where(
                    gb_discussions.c.group_key == group_key,
                    gb_discussions.c.id.in_(discussions),
                    gb_discussions.c.status == "active",
                )
                .values(status="expired")
            )
            conn.execute(
                update(gb_index_entries)
                .where(
                    gb_index_entries.c.group_key == group_key,
                    gb_index_entries.c.kind == "discussion",
                    gb_index_entries.c.canonical_id.in_(discussions),
                    gb_index_entries.c.state.in_(("active", "stale", "indexing")),
                )
                .values(state="delete_pending")
            )
        return conn.execute(
            update(gb_messages)
            .where(
                gb_messages.c.group_key == group_key,
                gb_messages.c.expires_at <= now,
                gb_messages.c.sanitized_text.is_not(None),
            )
            .values(sanitized_text=None, status="expired")
        ).rowcount

    def sources_of(self, conn, group_key, discussion_id):
        return [
            dict(r)
            for r in conn.execute(
                select(
                    gb_messages.c.lark_message_id,
                    gb_messages.c.message_link,
                    gb_messages.c.created_at,
                )
                .select_from(
                    gb_discussion_messages.join(
                        gb_messages,
                        and_(
                            gb_discussion_messages.c.group_key
                            == gb_messages.c.group_key,
                            gb_discussion_messages.c.message_id == gb_messages.c.id,
                        ),
                    )
                )
                .where(
                    gb_discussion_messages.c.group_key == group_key,
                    gb_discussion_messages.c.discussion_id == discussion_id,
                    gb_messages.c.visibility_status == "visible",
                    gb_messages.c.status == "active",
                    gb_messages.c.expires_at > func.now(),
                )
                .order_by(gb_discussion_messages.c.ordinal)
            ).mappings()
        ]
