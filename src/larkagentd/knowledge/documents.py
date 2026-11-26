from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import case, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import policy
from .chunking import DocumentTooLarge, check_limits, chunk_markdown
from .doc_sources import DocumentError, validate_document_url
from .policy import effective
from .providers.base import ProviderError, namespace_for
from .sanitize import sanitize
from .store import KnowledgeError, KnowledgeStore
from .tables import (
    gb_document_chunks,
    gb_document_revisions,
    gb_documents,
    gb_index_entries,
    gb_memory,
    gb_memory_sources,
    gb_source_refs,
)

STORE = KnowledgeStore()


def _affected_memory_ids(conn, group_key, document_id):
    document_support = (gb_source_refs.c.source_kind == "document") & (
        gb_source_refs.c.document_id == document_id
    )
    query = (
        select(gb_memory.c.id)
        .join(
            gb_memory_sources,
            (gb_memory_sources.c.group_key == gb_memory.c.group_key)
            & (gb_memory_sources.c.memory_id == gb_memory.c.id)
            & (gb_memory_sources.c.memory_revision == gb_memory.c.current_revision),
        )
        .join(gb_source_refs, gb_source_refs.c.id == gb_memory_sources.c.source_ref_id)
        .where(
            gb_memory.c.group_key == group_key,
            gb_memory_sources.c.group_key == group_key,
            gb_source_refs.c.group_key == group_key,
        )
        .group_by(gb_memory.c.id)
        .having(func.count() == func.sum(case((document_support, 1), else_=0)))
        .having(func.sum(case((document_support, 1), else_=0)) > 0)
    )
    return list(conn.execute(query).scalars())


class Documents:
    def __init__(self, runtime=None, config=None):
        self.runtime = runtime
        self.config = config or getattr(runtime, "config", None)

    def _require_enabled(self, conn, group_key):
        if self.config is not None:
            state = effective(conn, self.config, group_key)
            if (
                state.state not in ("enabled", "degraded")
                or not state.capabilities["documents"]
            ):
                raise KnowledgeError("disabled", 403)
            return
        settings = STORE.settings(conn, group_key)
        if (
            not STORE.registered(conn, group_key)
            or not settings["enabled"]
            or settings["state"] != "enabled"
            or not settings["documents_enabled"]
        ):
            raise KnowledgeError("disabled", 403)

    def propose(self, conn, group_key, proposer_id, url, now=None):
        kind, token, _ = validate_document_url(url)
        self._require_enabled(conn, group_key)
        existing = (
            conn.execute(
                select(gb_documents).where(
                    gb_documents.c.group_key == group_key,
                    gb_documents.c.canonical_doc_token == token,
                )
            )
            .mappings()
            .first()
        )
        if existing:
            return dict(existing)
        count = conn.execute(
            select(gb_documents.c.id).where(gb_documents.c.group_key == group_key)
        ).all()
        maximum = getattr(
            getattr(self.runtime, "config", None), "max_docs_per_group", 100
        )
        if len(count) >= maximum:
            raise DocumentError("too_large")
        document_id = str(uuid.uuid4())
        conn.execute(
            insert(gb_documents).values(
                id=document_id,
                group_key=group_key,
                provider_kind="lark_docx" if kind == "docx" else "lark_wiki",
                canonical_doc_token=token,
                wiki_token=token if kind == "wiki" else None,
                source_url=url,
                title="",
                state="pending",
                created_by=proposer_id,
            )
        )
        return dict(
            conn.execute(select(gb_documents).where(gb_documents.c.id == document_id))
            .mappings()
            .one()
        )

    def approve(
        self,
        conn,
        group_key,
        document_id,
        reviewer_email,
        sharing_policy_ref,
        confirm_sharing,
    ):
        self._require_enabled(conn, group_key)
        if (
            not isinstance(sharing_policy_ref, str)
            or not sharing_policy_ref.strip()
            or confirm_sharing is not True
        ):
            raise KnowledgeError("sharing_authorization_required", 403)
        doc = self._get(conn, group_key, document_id)
        if doc["state"] not in ("pending", "failed", "suspended"):
            raise KnowledgeError("invalid_transition", 409)
        settings = STORE.settings(conn, group_key)
        conn.execute(
            update(gb_documents)
            .where(gb_documents.c.id == document_id)
            .values(
                state="approved_pending_index",
                sharing_policy_ref=sharing_policy_ref.strip()[:200],
            )
        )
        STORE.enqueue(
            conn,
            group_key,
            "refresh_document",
            f"refresh-document:{document_id}:{settings['generation']}",
            settings["generation"],
            ref_payload={"document_id": document_id},
        )
        STORE.audit(
            conn, group_key, reviewer_email, "document_approved", target_id=document_id
        )
        return True

    def reject(self, conn, group_key, document_id, actor, reason="rejected"):
        self._get(conn, group_key, document_id)
        conn.execute(
            update(gb_documents)
            .where(gb_documents.c.id == document_id)
            .values(state="failed")
        )
        STORE.audit(
            conn,
            group_key,
            actor,
            "document_rejected",
            target_id=document_id,
            reason=reason,
        )

    def revoke(self, conn, group_key, document_id, actor=None):
        self._get(conn, group_key, document_id)
        conn.execute(
            update(gb_documents)
            .where(
                gb_documents.c.group_key == group_key, gb_documents.c.id == document_id
            )
            .values(state="revoked")
        )
        conn.execute(
            update(gb_index_entries)
            .where(
                gb_index_entries.c.group_key == group_key,
                gb_index_entries.c.kind == "document",
                gb_index_entries.c.canonical_id == document_id,
                gb_index_entries.c.state.in_(
                    ("active", "pending", "indexing", "stale")
                ),
            )
            .values(state="delete_pending")
        )
        affected = _affected_memory_ids(conn, group_key, document_id)
        settings = STORE.settings(conn, group_key)
        STORE.enqueue(
            conn,
            group_key,
            "reconcile_index",
            f"revoke-document:{document_id}:{settings['generation']}",
            settings["generation"],
            ref_payload={"document_id": document_id},
        )
        if actor:
            STORE.audit(
                conn, group_key, actor, "document_revoked", target_id=document_id
            )
        return affected

    def request_refresh(self, conn, group_key, document_id, actor=None):
        self._require_enabled(conn, group_key)
        doc = self._get(conn, group_key, document_id)
        if doc["state"] == "revoked":
            raise KnowledgeError("invalid_transition", 409)
        affected = _affected_memory_ids(conn, group_key, document_id)
        settings = STORE.settings(conn, group_key)
        current = doc["current_revision"] or 0
        conn.execute(
            update(gb_documents)
            .where(
                gb_documents.c.group_key == group_key, gb_documents.c.id == document_id
            )
            .values(state="suspended")
        )
        conn.execute(
            update(gb_index_entries)
            .where(
                gb_index_entries.c.group_key == group_key,
                gb_index_entries.c.kind == "document",
                gb_index_entries.c.canonical_id == document_id,
                gb_index_entries.c.state == "active",
            )
            .values(state="delete_pending")
        )
        STORE.enqueue(
            conn,
            group_key,
            "refresh_document",
            f"manual-refresh:{document_id}:{current}:{settings['generation']}",
            settings["generation"],
            current or None,
            {"document_id": document_id},
        )
        if actor:
            STORE.audit(
                conn,
                group_key,
                actor,
                "document_refresh_requested",
                target_id=document_id,
            )
        return {"document": dict(doc), "affected_memory_ids": affected}

    def suspend_for_acl_change(self, conn, group_key, document_id):
        doc = self._get(conn, group_key, document_id)
        affected = _affected_memory_ids(conn, group_key, document_id)
        conn.execute(
            update(gb_documents)
            .where(
                gb_documents.c.group_key == group_key, gb_documents.c.id == document_id
            )
            .values(state="suspended")
        )
        conn.execute(
            update(gb_index_entries)
            .where(
                gb_index_entries.c.group_key == group_key,
                gb_index_entries.c.kind == "document",
                gb_index_entries.c.canonical_id == document_id,
                gb_index_entries.c.state.in_(
                    ("active", "stale", "indexing", "pending")
                ),
            )
            .values(state="delete_pending")
        )
        settings = STORE.settings(conn, group_key)
        current = doc["current_revision"] or 0
        STORE.enqueue(
            conn,
            group_key,
            "refresh_document",
            f"acl-refresh:{document_id}:{current}:{settings['generation']}",
            settings["generation"],
            current or None,
            {"document_id": document_id},
        )
        STORE.enqueue(
            conn,
            group_key,
            "reconcile_index",
            f"document-cleanup:{document_id}:{current}:{settings['generation']}",
            settings["generation"],
            current or None,
            {"document_id": document_id},
        )
        return affected

    def _get(self, conn, group_key, document_id):
        row = (
            conn.execute(
                select(gb_documents).where(
                    gb_documents.c.group_key == group_key,
                    gb_documents.c.id == document_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise KnowledgeError("not_found", 404)
        return row

    def list_candidates(self, conn, group_key, limit=50):
        rows = conn.execute(
            select(gb_documents)
            .where(gb_documents.c.group_key == group_key)
            .order_by(gb_documents.c.id)
            .limit(max(1, min(limit, 100)))
        ).mappings()
        return [
            {
                key: row[key]
                for key in (
                    "id",
                    "provider_kind",
                    "source_url",
                    "title",
                    "state",
                    "current_revision",
                    "last_checked_at",
                    "last_synced_at",
                )
            }
            for row in rows
        ]

    def search(self, runtime, conn, group_key, terms, limit=5):
        with runtime.connection() as read_conn:
            state = effective(read_conn, runtime.config, group_key)
        if (
            state.state not in ("enabled", "degraded")
            or not state.capabilities["documents"]
            or not isinstance(terms, str)
            or not terms.strip()
        ):
            return []
        limit = max(1, min(limit, 10))
        namespace = namespace_for(runtime.config.collection, group_key, "document")
        lexical = []
        with runtime.connection() as read_conn:
            lexical = [
                dict(row)
                for row in read_conn.execute(
                    select(gb_document_chunks.c.id)
                    .join(
                        gb_documents,
                        (gb_documents.c.id == gb_document_chunks.c.document_id)
                        & (gb_documents.c.group_key == gb_document_chunks.c.group_key),
                    )
                    .where(
                        gb_documents.c.group_key == group_key,
                        gb_documents.c.state == "active",
                        gb_document_chunks.c.revision
                        == gb_documents.c.current_revision,
                        gb_document_chunks.c.sanitized_content.ilike(f"%{terms}%"),
                    )
                    .order_by(
                        gb_document_chunks.c.document_id, gb_document_chunks.c.ordinal
                    )
                    .limit(100)
                ).mappings()
            ]
            lexical_ids = [row["id"] for row in lexical]
        semantic_ids = []
        if runtime.index is not None:
            hits = runtime.index.search(
                namespace=namespace,
                query=terms,
                top_k=100,
                metadata_filters={"kind": "document"},
            )
            provider_ids = [hit.provider_id for hit in hits]
            if provider_ids:
                with runtime.connection() as read_conn:
                    matches = {
                        row["provider_id"]: row["id"]
                        for row in read_conn.execute(
                            select(
                                gb_index_entries.c.provider_id, gb_document_chunks.c.id
                            )
                            .join(
                                gb_document_chunks,
                                (
                                    gb_index_entries.c.canonical_id
                                    == gb_document_chunks.c.document_id
                                )
                                & (
                                    gb_index_entries.c.group_key
                                    == gb_document_chunks.c.group_key
                                )
                                & (
                                    gb_index_entries.c.source_revision
                                    == gb_document_chunks.c.revision
                                )
                                & (
                                    gb_index_entries.c.chunk_ordinal
                                    == gb_document_chunks.c.ordinal
                                ),
                            )
                            .join(
                                gb_documents,
                                (gb_documents.c.id == gb_document_chunks.c.document_id)
                                & (
                                    gb_documents.c.group_key
                                    == gb_document_chunks.c.group_key
                                ),
                            )
                            .where(
                                gb_documents.c.group_key == group_key,
                                gb_documents.c.state == "active",
                                gb_document_chunks.c.revision
                                == gb_documents.c.current_revision,
                                gb_index_entries.c.kind == "document",
                                gb_index_entries.c.state == "active",
                                gb_index_entries.c.provider_id.in_(provider_ids),
                            )
                        ).mappings()
                    }
                semantic_ids = list(
                    dict.fromkeys(
                        matches[provider_id]
                        for provider_id in provider_ids
                        if provider_id in matches
                    )
                )
        ranks = {}
        for ranked in (lexical_ids, semantic_ids):
            for rank, chunk_id in enumerate(ranked, 1):
                ranks[chunk_id] = ranks.get(chunk_id, 0) + 1 / (60 + rank)
        ordered = sorted(ranks, key=lambda item: (-ranks[item], item))[:limit]
        if not ordered:
            return []
        with runtime.connection() as read_conn:
            rows = {
                row["id"]: dict(row)
                for row in read_conn.execute(
                    select(
                        gb_document_chunks,
                        gb_documents.c.title,
                        gb_documents.c.source_url,
                        gb_documents.c.current_revision,
                        gb_document_revisions.c.provider_revision,
                    )
                    .join(
                        gb_documents,
                        (gb_documents.c.id == gb_document_chunks.c.document_id)
                        & (gb_documents.c.group_key == gb_document_chunks.c.group_key),
                    )
                    .join(
                        gb_document_revisions,
                        (
                            gb_document_revisions.c.group_key
                            == gb_document_chunks.c.group_key
                        )
                        & (
                            gb_document_revisions.c.document_id
                            == gb_document_chunks.c.document_id
                        )
                        & (
                            gb_document_revisions.c.revision
                            == gb_document_chunks.c.revision
                        ),
                    )
                    .where(
                        gb_document_chunks.c.group_key == group_key,
                        gb_document_chunks.c.id.in_(ordered),
                        gb_documents.c.state == "active",
                        gb_document_chunks.c.revision
                        == gb_documents.c.current_revision,
                    )
                ).mappings()
            }
        result = []
        checked = {}
        for chunk_id in ordered:
            row = rows.get(chunk_id)
            if not row:
                continue
            document_id = row["document_id"]
            if document_id not in checked:
                try:
                    access = runtime.doc_source.check(row["source_url"])
                    checked[document_id] = bool(
                        access.get("accessible")
                        and access.get("provider_revision") == row["provider_revision"]
                    )
                except Exception:
                    checked[document_id] = False
                if not checked[document_id]:
                    with runtime.connection() as write_conn:
                        self.suspend_for_acl_change(write_conn, group_key, document_id)
            if checked[document_id]:
                result.append(
                    {
                        "document_id": document_id,
                        "title": row["title"],
                        "section_path": row["section_path"],
                        "snippet": row["sanitized_content"][:500],
                        "source_url": row["source_url"],
                        "revision": row["revision"],
                    }
                )
        return result


def _payload(ctx, key):
    value = ctx.ref_payload.get(key)
    if not isinstance(value, str) or not value:
        raise DocumentError("unavailable")
    return value


def refresh_document(ctx):
    document_id = _payload(ctx, "document_id")
    if not ctx.still_valid("documents"):
        return
    with ctx.conn() as conn:
        doc = (
            conn.execute(
                select(gb_documents).where(
                    gb_documents.c.group_key == ctx.group_key,
                    gb_documents.c.id == document_id,
                )
            )
            .mappings()
            .first()
        )
        if (
            not doc
            or doc["state"] not in ("approved_pending_index", "active", "suspended")
            or not doc["sharing_policy_ref"]
            or not ctx.still_valid("documents")
        ):
            return
        url = doc["source_url"]
        current_revision = doc["current_revision"]
        current = (
            conn.execute(
                select(gb_document_revisions).where(
                    gb_document_revisions.c.group_key == ctx.group_key,
                    gb_document_revisions.c.document_id == document_id,
                    gb_document_revisions.c.revision == current_revision,
                )
            )
            .mappings()
            .first()
            if current_revision
            else None
        )
        current_provider_revision = current["provider_revision"] if current else None
    checked = ctx.runtime.doc_source.check(url)
    if not checked.get("accessible"):
        with ctx.conn() as conn:
            Documents(ctx.runtime).suspend_for_acl_change(
                conn, ctx.group_key, document_id
            )
        return
    if current and checked.get("provider_revision") != current_provider_revision:
        with ctx.conn() as conn:
            Documents(ctx.runtime).suspend_for_acl_change(
                conn, ctx.group_key, document_id
            )
    if not ctx.still_valid("documents") or not ctx.owns():
        return
    fetched = ctx.runtime.doc_source.fetch(url)
    checked_after = ctx.runtime.doc_source.check(url)
    if (
        not checked_after.get("accessible")
        or checked_after.get("provider_revision") != fetched.provider_revision
    ):
        with ctx.conn() as conn:
            Documents(ctx.runtime).suspend_for_acl_change(
                conn, ctx.group_key, document_id
            )
        return
    chunks = chunk_markdown(
        fetched.markdown,
        chunk_tokens=ctx.runtime.config.chunk_tokens,
        overlap=ctx.runtime.config.chunk_overlap,
    )
    total_tokens = sum(chunk.tokens for chunk in chunks)
    try:
        check_limits(
            chunks,
            total_tokens,
            max_chunks=250,
            max_tokens=ctx.runtime.config.max_document_tokens,
        )
    except DocumentTooLarge:
        raise DocumentError("too_large") from None
    if not chunks:
        raise DocumentError("unavailable")
    cleaned = []
    for chunk in chunks:
        safe = sanitize(chunk.text, max_chars=max(1, len(chunk.text)))
        if safe.blocked or not safe.text or "[REDACTED]" in chunk.text.upper():
            with ctx.conn() as conn:
                conn.execute(
                    update(gb_documents)
                    .where(
                        gb_documents.c.group_key == ctx.group_key,
                        gb_documents.c.id == document_id,
                    )
                    .values(state="failed")
                )
                conn.execute(
                    update(gb_index_entries)
                    .where(
                        gb_index_entries.c.group_key == ctx.group_key,
                        gb_index_entries.c.kind == "document",
                        gb_index_entries.c.canonical_id == document_id,
                        gb_index_entries.c.state.in_(
                            ("active", "stale", "indexing", "pending")
                        ),
                    )
                    .values(state="delete_pending")
                )
                settings = STORE.settings(conn, ctx.group_key)
                STORE.enqueue(
                    conn,
                    ctx.group_key,
                    "reconcile_index",
                    f"sensitive-document-cleanup:{document_id}:{settings['generation']}",
                    settings["generation"],
                    ref_payload={"document_id": document_id},
                )
            raise DocumentError("sensitive_content")
        cleaned.append((chunk, safe.text))
    content_hash = hashlib.sha256(fetched.markdown.encode()).hexdigest()
    with ctx.conn() as conn:
        if not policy.still_valid(
            conn, ctx.runtime.config, ctx.group_key, ctx.generation, "documents"
        ) or not STORE.owns(conn, ctx.job["id"], ctx.fence):
            return
        doc = (
            conn.execute(
                select(gb_documents)
                .where(
                    gb_documents.c.group_key == ctx.group_key,
                    gb_documents.c.id == document_id,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if (
            not doc
            or doc["state"] not in ("approved_pending_index", "active", "suspended")
            or not doc["sharing_policy_ref"]
        ):
            return
        current = (
            conn.execute(
                select(gb_document_revisions).where(
                    gb_document_revisions.c.group_key == ctx.group_key,
                    gb_document_revisions.c.document_id == document_id,
                    gb_document_revisions.c.revision == doc["current_revision"],
                )
            )
            .mappings()
            .first()
            if doc["current_revision"]
            else None
        )
        if (
            current
            and current["content_hash"] == content_hash
            and current["provider_revision"] == fetched.provider_revision
        ):
            conn.execute(
                update(gb_documents)
                .where(
                    gb_documents.c.group_key == ctx.group_key,
                    gb_documents.c.id == document_id,
                )
                .values(state="active", last_checked_at=func.now())
            )
            conn.execute(
                update(gb_index_entries)
                .where(
                    gb_index_entries.c.group_key == ctx.group_key,
                    gb_index_entries.c.kind == "document",
                    gb_index_entries.c.canonical_id == document_id,
                    gb_index_entries.c.source_revision == doc["current_revision"],
                    gb_index_entries.c.state == "delete_pending",
                    gb_index_entries.c.provider_id.is_not(None),
                )
                .values(state="active")
            )
            return
        latest = conn.execute(
            select(func.max(gb_document_revisions.c.revision)).where(
                gb_document_revisions.c.group_key == ctx.group_key,
                gb_document_revisions.c.document_id == document_id,
            )
        ).scalar_one()
        revision = (latest or 0) + 1
        conn.execute(
            insert(gb_document_revisions).values(
                group_key=ctx.group_key,
                document_id=document_id,
                revision=revision,
                provider_revision=fetched.provider_revision,
                content_hash=content_hash,
                state="pending",
            )
        )
        for chunk, text_value in cleaned:
            digest = hashlib.sha256(text_value.encode()).hexdigest()
            conn.execute(
                insert(gb_document_chunks).values(
                    id=str(uuid.uuid4()),
                    group_key=ctx.group_key,
                    document_id=document_id,
                    revision=revision,
                    ordinal=chunk.ordinal,
                    section_path=chunk.section_path,
                    block_anchor=chunk.block_anchor,
                    sanitized_content=text_value,
                    content_hash=digest,
                    token_count=chunk.tokens,
                )
            )
        conn.execute(
            update(gb_documents)
            .where(
                gb_documents.c.group_key == ctx.group_key,
                gb_documents.c.id == document_id,
            )
            .values(title=fetched.title, state="approved_pending_index")
        )
        settings = STORE.settings(conn, ctx.group_key)
        if ctx.runtime.index is not None:
            STORE.enqueue(
                conn,
                ctx.group_key,
                "index_revision",
                f"index-document:{document_id}:{revision}",
                settings["generation"],
                revision,
                {"kind": "document", "document_id": document_id, "revision": revision},
            )
            return
    # Recheck ACL after fetch and outside the staging transaction before lexical activation.
    checked_final = ctx.runtime.doc_source.check(url)
    if (
        not checked_final.get("accessible")
        or checked_final.get("provider_revision") != fetched.provider_revision
    ):
        with ctx.conn() as conn:
            Documents(ctx.runtime).suspend_for_acl_change(
                conn, ctx.group_key, document_id
            )
        return
    with ctx.conn() as conn:
        if not policy.still_valid(
            conn, ctx.runtime.config, ctx.group_key, ctx.generation, "documents"
        ) or not STORE.owns(conn, ctx.job["id"], ctx.fence):
            return
        doc = (
            conn.execute(
                select(gb_documents)
                .where(
                    gb_documents.c.group_key == ctx.group_key,
                    gb_documents.c.id == document_id,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if not doc or not doc["sharing_policy_ref"]:
            return
        conn.execute(
            update(gb_document_revisions)
            .where(
                gb_document_revisions.c.group_key == ctx.group_key,
                gb_document_revisions.c.document_id == document_id,
                gb_document_revisions.c.revision == revision,
            )
            .values(state="active", checked_at=func.now())
        )
        conn.execute(
            update(gb_document_revisions)
            .where(
                gb_document_revisions.c.group_key == ctx.group_key,
                gb_document_revisions.c.document_id == document_id,
                gb_document_revisions.c.revision != revision,
                gb_document_revisions.c.state == "active",
            )
            .values(state="superseded")
        )
        conn.execute(
            update(gb_documents)
            .where(
                gb_documents.c.group_key == ctx.group_key,
                gb_documents.c.id == document_id,
            )
            .values(
                state="active",
                current_revision=revision,
                last_checked_at=func.now(),
                last_synced_at=func.now(),
            )
        )


def _namespace_records(index, namespace):
    records, cursor = [], None
    while True:
        page, cursor = index.list_scoped(namespace=namespace, cursor=cursor, limit=100)
        records.extend(page)
        if cursor is None:
            return records


def _delete_verified(index, namespace, provider_id):
    if any(
        hit.provider_id == provider_id for hit in _namespace_records(index, namespace)
    ):
        index.delete(namespace=namespace, provider_id=provider_id)


def index_document_revision(ctx):
    document_id = _payload(ctx, "document_id")
    revision = ctx.ref_payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise DocumentError("unavailable")
    index = ctx.runtime.index
    if index is None:
        return
    namespace = namespace_for(ctx.runtime.config.collection, ctx.group_key, "document")
    created = []
    with ctx.conn() as conn:
        if not policy.still_valid(
            conn, ctx.runtime.config, ctx.group_key, ctx.generation, "documents"
        ) or not STORE.owns(conn, ctx.job["id"], ctx.fence):
            return
        doc = (
            conn.execute(
                select(gb_documents)
                .where(
                    gb_documents.c.group_key == ctx.group_key,
                    gb_documents.c.id == document_id,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if (
            not doc
            or doc["state"] not in ("approved_pending_index", "active")
            or not doc["sharing_policy_ref"]
        ):
            return
        source_url = doc["source_url"]
        latest = conn.execute(
            select(func.max(gb_document_revisions.c.revision)).where(
                gb_document_revisions.c.group_key == ctx.group_key,
                gb_document_revisions.c.document_id == document_id,
            )
        ).scalar_one()
        if latest != revision:
            return
        rows = list(
            conn.execute(
                select(gb_document_chunks)
                .where(
                    gb_document_chunks.c.group_key == ctx.group_key,
                    gb_document_chunks.c.document_id == document_id,
                    gb_document_chunks.c.revision == revision,
                )
                .order_by(gb_document_chunks.c.ordinal)
            ).mappings()
        )
        revision_row = (
            conn.execute(
                select(gb_document_revisions).where(
                    gb_document_revisions.c.group_key == ctx.group_key,
                    gb_document_revisions.c.document_id == document_id,
                    gb_document_revisions.c.revision == revision,
                    gb_document_revisions.c.state == "pending",
                )
            )
            .mappings()
            .first()
        )
        if revision_row is None:
            return
        staged_provider_revision = revision_row["provider_revision"]
        prepared = []
        for chunk in rows:
            idem = hashlib.sha256(
                f"{ctx.group_key}:document:{document_id}:{revision}:{chunk['ordinal']}:{chunk['content_hash']}:v1".encode()
            ).hexdigest()
            entry_id = str(uuid.uuid4())
            conn.execute(
                pg_insert(gb_index_entries)
                .values(
                    id=entry_id,
                    group_key=ctx.group_key,
                    kind="document",
                    canonical_id=document_id,
                    source_revision=revision,
                    chunk_ordinal=chunk["ordinal"],
                    namespace=namespace,
                    provider_id=None,
                    idempotency_key=idem,
                    generation=ctx.generation,
                    state="indexing",
                    content_hash=chunk["content_hash"],
                    embedding_model=ctx.runtime.config.embedding_model,
                    dimensions=ctx.runtime.config.embedding_dimensions,
                )
                .on_conflict_do_nothing(constraint="gb_index_idempotency")
            )
            entry = (
                conn.execute(
                    select(gb_index_entries)
                    .where(
                        gb_index_entries.c.group_key == ctx.group_key,
                        gb_index_entries.c.idempotency_key == idem,
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if entry["state"] == "active" and entry["provider_id"]:
                provider_id = entry["provider_id"]
            else:
                conn.execute(
                    update(gb_index_entries)
                    .where(
                        gb_index_entries.c.group_key == ctx.group_key,
                        gb_index_entries.c.id == entry["id"],
                    )
                    .values(state="indexing", provider_id=None)
                )
                provider_id = None
            prepared.append(
                (
                    chunk,
                    entry["id"],
                    idem,
                    provider_id,
                    bool(entry["state"] == "active" and provider_id),
                )
            )
    try:
        for chunk, entry_id, idem, active_provider_id, already_active in prepared:
            if (
                not ctx.heartbeat()
                or not ctx.still_valid("documents")
                or not ctx.owns()
            ):
                raise DocumentError("unavailable")
            if already_active:
                created.append(
                    (chunk["ordinal"], entry_id, idem, active_provider_id, False)
                )
                continue
            matching = [
                hit
                for hit in _namespace_records(index, namespace)
                if hit.metadata.get("index_key") == idem
            ]
            if len(matching) == 1:
                provider_id = matching[0].provider_id
            else:
                for hit in matching:
                    _delete_verified(index, namespace, hit.provider_id)
                try:
                    provider_ids = index.add(
                        namespace=namespace,
                        text=chunk["sanitized_content"],
                        metadata={
                            "index_key": idem,
                            "kind": "document",
                            "canonical_id": document_id,
                            "revision": revision,
                            "ordinal": chunk["ordinal"],
                        },
                    )
                except ProviderError as exc:
                    if exc.code != "timeout":
                        raise
                    matching = [
                        hit
                        for hit in _namespace_records(index, namespace)
                        if hit.metadata.get("index_key") == idem
                    ]
                    if len(matching) != 1:
                        for hit in matching:
                            _delete_verified(index, namespace, hit.provider_id)
                        raise
                    provider_ids = [matching[0].provider_id]
                if not provider_ids:
                    raise ProviderError("bad_response")
                provider_id = provider_ids[0]
                for extra_id in provider_ids[1:]:
                    _delete_verified(index, namespace, extra_id)
            created.append((chunk["ordinal"], entry_id, idem, provider_id, True))
        access = ctx.runtime.doc_source.check(source_url)
        if (
            not access.get("accessible")
            or access.get("provider_revision") != staged_provider_revision
        ):
            with ctx.conn() as conn:
                Documents(ctx.runtime).suspend_for_acl_change(
                    conn, ctx.group_key, document_id
                )
            raise DocumentError("forbidden")
        with ctx.conn() as conn:
            valid = policy.still_valid(
                conn, ctx.runtime.config, ctx.group_key, ctx.generation, "documents"
            )
            owns = STORE.owns(conn, ctx.job["id"], ctx.fence)
            doc = (
                conn.execute(
                    select(gb_documents)
                    .where(
                        gb_documents.c.group_key == ctx.group_key,
                        gb_documents.c.id == document_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            latest = conn.execute(
                select(func.max(gb_document_revisions.c.revision)).where(
                    gb_document_revisions.c.group_key == ctx.group_key,
                    gb_document_revisions.c.document_id == document_id,
                )
            ).scalar_one()
            revision_row = conn.execute(
                select(gb_document_revisions).where(
                    gb_document_revisions.c.group_key == ctx.group_key,
                    gb_document_revisions.c.document_id == document_id,
                    gb_document_revisions.c.revision == revision,
                    gb_document_revisions.c.state == "pending",
                )
            ).first()
            expected_provider_revision = conn.execute(
                select(gb_document_revisions.c.provider_revision).where(
                    gb_document_revisions.c.group_key == ctx.group_key,
                    gb_document_revisions.c.document_id == document_id,
                    gb_document_revisions.c.revision == revision,
                )
            ).scalar_one_or_none()
            if (
                not valid
                or not owns
                or not doc
                or doc["state"] not in ("approved_pending_index", "active")
                or not doc["sharing_policy_ref"]
                or latest != revision
                or revision_row is None
                or access.get("provider_revision") != expected_provider_revision
            ):
                raise DocumentError("unavailable")
            activated = 0
            for _, entry_id, _, provider_id, was_written in created:
                if was_written:
                    result = conn.execute(
                        update(gb_index_entries)
                        .where(
                            gb_index_entries.c.group_key == ctx.group_key,
                            gb_index_entries.c.id == entry_id,
                            gb_index_entries.c.state == "indexing",
                        )
                        .values(
                            state="active",
                            provider_id=provider_id,
                            indexed_at=func.now(),
                        )
                    )
                    activated += result.rowcount
            expected = sum(1 for item in created if item[4])
            if activated != expected:
                raise DocumentError("unavailable")
            conn.execute(
                update(gb_document_revisions)
                .where(
                    gb_document_revisions.c.group_key == ctx.group_key,
                    gb_document_revisions.c.document_id == document_id,
                    gb_document_revisions.c.revision == revision,
                    gb_document_revisions.c.state == "pending",
                )
                .values(state="active", checked_at=func.now())
            )
            conn.execute(
                update(gb_document_revisions)
                .where(
                    gb_document_revisions.c.group_key == ctx.group_key,
                    gb_document_revisions.c.document_id == document_id,
                    gb_document_revisions.c.revision != revision,
                    gb_document_revisions.c.state == "active",
                )
                .values(state="superseded")
            )
            conn.execute(
                update(gb_documents)
                .where(
                    gb_documents.c.group_key == ctx.group_key,
                    gb_documents.c.id == document_id,
                )
                .values(
                    state="active",
                    current_revision=revision,
                    last_checked_at=func.now(),
                    last_synced_at=func.now(),
                )
            )
            conn.execute(
                update(gb_index_entries)
                .where(
                    gb_index_entries.c.group_key == ctx.group_key,
                    gb_index_entries.c.kind == "document",
                    gb_index_entries.c.canonical_id == document_id,
                    gb_index_entries.c.source_revision != revision,
                    gb_index_entries.c.state == "active",
                )
                .values(state="delete_pending")
            )
    except Exception:
        for _, _, _, provider_id, newly_written in created:
            if newly_written:
                try:
                    _delete_verified(index, namespace, provider_id)
                except Exception:
                    pass
        raise


def verify_document_source(ctx):
    return refresh_document(ctx)


def handlers():
    return {
        "refresh_document": refresh_document,
        "verify_source": verify_document_source,
    }
