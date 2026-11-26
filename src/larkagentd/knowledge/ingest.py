from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..core.store import database_time
from . import policy
from .catalog import Catalog
from .discussions import reconstruct
from .lark_sources import SourceError
from .providers.base import ProviderError, namespace_for
from .store import KnowledgeStore
from .tables import gb_discussions, gb_index_entries, gb_jobs

CATALOG = Catalog()
STORE = KnowledgeStore()
PAGE_LIMIT = 50
MAX_PAGES = 6
MAX_THREAD_PAGES = 20


def _payload(ctx, key):
    value = ctx.ref_payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("invalid_payload")
    return value


def _still(ctx):
    if not ctx.still_valid():
        raise SourceError("unavailable")
    if not ctx.heartbeat():
        raise SourceError("unavailable")


def _thread_messages(ctx, chat_id, thread_id):
    messages, token = [], None
    for _ in range(MAX_THREAD_PAGES):
        _still(ctx)
        page = ctx.runtime.source.thread_messages(
            chat_id, thread_id, page_token=token, limit=PAGE_LIMIT
        )
        messages.extend(m for m in page.messages if m.chat_id == chat_id)
        token = page.next_page_token
        if not token or page.complete:
            return messages, page.complete
    return messages, False


def _window_messages(ctx, chat_id):
    messages, token = [], None
    for _ in range(MAX_PAGES):
        _still(ctx)
        page = ctx.runtime.source.list_messages(
            chat_id, page_token=token, limit=PAGE_LIMIT
        )
        messages.extend(m for m in page.messages if m.chat_id == chat_id)
        token = page.next_page_token
        if not token or page.complete:
            break
    return messages


def ingest_message(ctx):
    if not ctx.still_valid():
        return
    message_id = _payload(ctx, "message_id")
    reason = ctx.ref_payload.get("reason")
    if reason not in ("learn", "interaction"):
        raise ValueError("invalid_payload")
    chat_id = ctx.group_key.removeprefix("group:")
    thread_id = ctx.ref_payload.get("thread_id")
    if thread_id:
        messages, complete = _thread_messages(ctx, chat_id, thread_id)
    else:
        messages = _window_messages(ctx, chat_id)
    if not ctx.still_valid():
        return
    selected = [m for m in messages if m.message_id == message_id]
    if not selected:
        raise SourceError("not_found")
    valid_messages = [m for m in messages if m.sender_type in ("user", "app")]
    from .sanitize import sanitize

    retention = ctx.runtime.config.source_retention_days
    with ctx.conn() as conn:
        if not STORE.owns(conn, ctx.job["id"], ctx.fence) or not policy.still_valid(
            conn, ctx.runtime.config, ctx.group_key, ctx.generation
        ):
            return
        settings = STORE.settings(conn, ctx.group_key)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=settings["history_window_days"])
        if settings["enabled_at"] is not None:
            cutoff = max(cutoff, settings["enabled_at"])
        recent = []
        for msg in valid_messages:
            created_at = msg.created_at
            created_at = (
                created_at.replace(tzinfo=timezone.utc)
                if created_at.tzinfo is None
                else created_at.astimezone(timezone.utc)
            )
            if created_at >= cutoff:
                recent.append(msg)
        texts = {}
        clean_messages = []
        for msg in recent:
            cleaned = sanitize(msg.text)
            if cleaned.text:
                texts[msg.message_id] = cleaned.text
                clean_messages.append(msg)
        discussions = reconstruct(
            clean_messages,
            thread_complete={thread_id} if thread_id and complete else set(),
        )
        discussion = next(
            (
                item
                for item in discussions
                if message_id in item.message_ids
                or thread_id
                and item.native_thread_id == thread_id
            ),
            None,
        )
        if discussion is None:
            blocked_selected = [m for m in recent if m.message_id == message_id]
            if blocked_selected:
                CATALOG.upsert_messages(
                    conn, ctx.group_key, blocked_selected, {}, retention
                )
            return
        selected_ids = set(discussion.message_ids)
        selected_messages = [msg for msg in recent if msg.message_id in selected_ids]
        selected_texts = {
            msg.message_id: texts[msg.message_id]
            for msg in selected_messages
            if msg.message_id in texts
        }
        CATALOG.upsert_messages(
            conn, ctx.group_key, selected_messages, selected_texts, retention
        )
        saved = CATALOG.save_discussion(
            conn, ctx.group_key, discussion, selected_texts, retention, now=now
        )
        if ctx.runtime.index is not None:
            STORE.enqueue(
                conn,
                ctx.group_key,
                "index_revision",
                f"discussion:{saved['discussion_id']}:{saved['revision']}:g{ctx.generation}",
                ctx.generation,
                source_revision=saved["revision"],
                ref_payload={
                    "discussion_id": saved["discussion_id"],
                    "revision": saved["revision"],
                },
            )


def refresh_discussion(ctx):
    discussion_id = _payload(ctx, "discussion_id")
    with ctx.conn() as conn:
        if not STORE.owns(conn, ctx.job["id"], ctx.fence) or not policy.still_valid(
            conn, ctx.runtime.config, ctx.group_key, ctx.generation
        ):
            return
        row = (
            conn.execute(
                select(gb_discussions).where(
                    gb_discussions.c.group_key == ctx.group_key,
                    gb_discussions.c.id == discussion_id,
                    gb_discussions.c.status == "active",
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return
        old_sources = CATALOG.sources_of(conn, ctx.group_key, discussion_id)
        thread_id = row["native_thread_id"]
        stable_window_key = row["stable_window_key"]
        settings = STORE.settings(conn, ctx.group_key)
    if not ctx.still_valid():
        return
    chat_id = ctx.group_key.removeprefix("group:")
    if thread_id:
        messages, complete = _thread_messages(ctx, chat_id, thread_id)
    else:
        messages, complete = _window_messages(ctx, chat_id), False
    from .sanitize import sanitize

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=settings["history_window_days"])
    if settings["enabled_at"] is not None:
        cutoff = max(cutoff, settings["enabled_at"])
    recent = []
    for message in messages:
        created_at = message.created_at
        created_at = (
            created_at.replace(tzinfo=timezone.utc)
            if created_at.tzinfo is None
            else created_at.astimezone(timezone.utc)
        )
        if created_at >= cutoff and message.sender_type in ("user", "app"):
            recent.append(message)
    texts = {}
    clean = []
    for message in recent:
        text = sanitize(message.text).text
        if text:
            texts[message.message_id] = text
            clean.append(message)
    discussion = next(
        (
            item
            for item in reconstruct(
                clean, thread_complete={thread_id} if thread_id and complete else set()
            )
            if item.native_thread_id == thread_id
            if thread_id or item.window_key == stable_window_key
        ),
        None,
    )
    with ctx.conn() as conn:
        if not STORE.owns(conn, ctx.job["id"], ctx.fence) or not policy.still_valid(
            conn, ctx.runtime.config, ctx.group_key, ctx.generation
        ):
            return
        clean_ids = {message.message_id for message in clean}
        for source in old_sources:
            if source["lark_message_id"] not in clean_ids:
                CATALOG.invalidate_message(
                    conn, ctx.group_key, source["lark_message_id"]
                )
        if discussion is None:
            return
        selected_ids = set(discussion.message_ids)
        selected_messages = [
            message for message in recent if message.message_id in selected_ids
        ]
        selected_texts = {
            message.message_id: texts[message.message_id]
            for message in selected_messages
            if message.message_id in texts
        }
        CATALOG.upsert_messages(
            conn,
            ctx.group_key,
            selected_messages,
            selected_texts,
            ctx.runtime.config.source_retention_days,
        )
        saved = CATALOG.save_discussion(
            conn,
            ctx.group_key,
            discussion,
            selected_texts,
            ctx.runtime.config.source_retention_days,
            now=now,
        )
        if ctx.runtime.index is not None:
            STORE.enqueue(
                conn,
                ctx.group_key,
                "index_revision",
                f"discussion:{saved['discussion_id']}:{saved['revision']}:g{ctx.generation}",
                ctx.generation,
                source_revision=saved["revision"],
                ref_payload={
                    "discussion_id": saved["discussion_id"],
                    "revision": saved["revision"],
                },
            )


def _index_key(ctx, kind, canonical_id, revision, content_hash):
    config = ctx.runtime.config
    raw = "\x1f".join(
        (
            ctx.group_key,
            kind,
            canonical_id,
            str(revision),
            "0",
            content_hash,
            config.embedding_model,
            str(config.embedding_dimensions),
        )
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _list_namespace(index, namespace):
    records, cursor = [], None
    seen = set()
    while True:
        page, next_cursor = index.list_scoped(
            namespace=namespace, cursor=cursor, limit=100
        )
        records.extend(page)
        if next_cursor is None:
            return records
        if next_cursor == cursor or next_cursor in seen:
            raise ProviderError("bad_response")
        seen.add(next_cursor)
        cursor = next_cursor


def _delete_verified(index, namespace, provider_id):
    records = _list_namespace(index, namespace)
    if any(record.provider_id == provider_id for record in records):
        index.delete(namespace=namespace, provider_id=provider_id)


def index_revision(ctx):
    discussion_id = _payload(ctx, "discussion_id")
    revision = ctx.ref_payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise ValueError("invalid_payload")
    if ctx.runtime.index is None or not ctx.runtime.config.embedding_configured:
        return
    with ctx.conn() as conn:
        if not STORE.owns(conn, ctx.job["id"], ctx.fence) or not policy.still_valid(
            conn, ctx.runtime.config, ctx.group_key, ctx.generation, "recall"
        ):
            return
        discussion = (
            conn.execute(
                select(gb_discussions).where(
                    gb_discussions.c.group_key == ctx.group_key,
                    gb_discussions.c.id == discussion_id,
                    gb_discussions.c.status == "active",
                    gb_discussions.c.source_revision == revision,
                    gb_discussions.c.expires_at > datetime.now(timezone.utc),
                )
            )
            .mappings()
            .first()
        )
        if discussion is None:
            return
        text = "\n".join(
            (
                discussion["title"],
                discussion["summary"],
                " ".join(discussion["identifiers"]),
            )
        )[:8000]
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        index_key = _index_key(ctx, "discussion", discussion_id, revision, content_hash)
        namespace = namespace_for(
            ctx.runtime.config.collection, ctx.group_key, "discussion"
        )
        entry_id = str(uuid.uuid4())
        conn.execute(
            pg_insert(gb_index_entries)
            .values(
                id=entry_id,
                group_key=ctx.group_key,
                kind="discussion",
                canonical_id=discussion_id,
                source_revision=revision,
                chunk_ordinal=0,
                namespace=namespace,
                provider_id=None,
                idempotency_key=index_key,
                generation=ctx.generation,
                state="indexing",
                content_hash=content_hash,
                embedding_model=ctx.runtime.config.embedding_model,
                dimensions=ctx.runtime.config.embedding_dimensions,
            )
            .on_conflict_do_nothing(constraint="gb_index_idempotency")
        )
        entry = (
            conn.execute(
                select(gb_index_entries).where(
                    gb_index_entries.c.group_key == ctx.group_key,
                    gb_index_entries.c.idempotency_key == index_key,
                )
            )
            .mappings()
            .one()
        )
        old_provider_id = None
        if entry["state"] == "active" and entry["provider_id"]:
            if entry["generation"] == ctx.generation:
                return
            old_provider_id = entry["provider_id"]
            conn.execute(
                update(gb_index_entries)
                .where(
                    gb_index_entries.c.id == entry["id"],
                    gb_index_entries.c.state == "active",
                    gb_index_entries.c.generation == entry["generation"],
                )
                .values(state="indexing", generation=ctx.generation)
            )
        entry_id = entry["id"]
    if not ctx.heartbeat() or not ctx.still_valid("recall") or not ctx.owns():
        return
    metadata = {
        "index_key": index_key,
        "kind": "discussion",
        "canonical_id": discussion_id,
        "revision": revision,
    }
    try:
        provider_ids = ctx.runtime.index.add(
            namespace=namespace, text=text, metadata=metadata
        )
    except ProviderError as exc:
        if exc.code != "timeout":
            raise
        matches = [
            r
            for r in _list_namespace(ctx.runtime.index, namespace)
            if r.metadata.get("index_key") == index_key
        ]
        if len(matches) == 1:
            provider_ids = [matches[0].provider_id]
        else:
            for item in matches:
                _delete_verified(ctx.runtime.index, namespace, item.provider_id)
            raise
    if not provider_ids:
        raise ProviderError("bad_response")
    provider_id = provider_ids[0]
    for extra in provider_ids[1:]:
        _delete_verified(ctx.runtime.index, namespace, extra)
    activated = False
    with ctx.conn() as conn:
        owns = STORE.owns(conn, ctx.job["id"], ctx.fence)
        valid = policy.still_valid(
            conn, ctx.runtime.config, ctx.group_key, ctx.generation, "recall"
        )
        current = conn.execute(
            select(gb_discussions.c.id).where(
                gb_discussions.c.group_key == ctx.group_key,
                gb_discussions.c.id == discussion_id,
                gb_discussions.c.status == "active",
                gb_discussions.c.source_revision == revision,
            )
        ).first()
        if owns and valid and current:
            result = conn.execute(
                update(gb_index_entries)
                .where(
                    gb_index_entries.c.group_key == ctx.group_key,
                    gb_index_entries.c.id == entry_id,
                    gb_index_entries.c.state == "indexing",
                    gb_index_entries.c.idempotency_key == index_key,
                    gb_index_entries.c.generation == ctx.generation,
                )
                .values(
                    state="active",
                    provider_id=provider_id,
                    generation=ctx.generation,
                    indexed_at=func.now(),
                )
            )
            activated = result.rowcount == 1
            if activated:
                conn.execute(
                    update(gb_index_entries)
                    .where(
                        gb_index_entries.c.group_key == ctx.group_key,
                        gb_index_entries.c.kind == "discussion",
                        gb_index_entries.c.canonical_id == discussion_id,
                        gb_index_entries.c.id != entry_id,
                        gb_index_entries.c.state == "active",
                    )
                    .values(state="delete_pending")
                )
    if activated:
        if old_provider_id and old_provider_id != provider_id:
            _delete_verified(ctx.runtime.index, namespace, old_provider_id)
    else:
        _delete_verified(ctx.runtime.index, namespace, provider_id)


def invalidate_source(ctx):
    message_id = _payload(ctx, "message_id")
    with ctx.conn() as conn:
        ids = CATALOG.invalidate_message(conn, ctx.group_key, message_id)
    return ids


def reconcile_index(ctx):
    with ctx.conn() as conn:
        if not STORE.owns(conn, ctx.job["id"], ctx.fence):
            return
        CATALOG.expire(conn, ctx.group_key, datetime.now(timezone.utc))
        namespaces = {
            namespace_for(ctx.runtime.config.collection, ctx.group_key, kind)
            for kind in ("discussion", "memory", "document")
        }
        namespaces.update(
            conn.execute(
                select(gb_index_entries.c.namespace)
                .where(gb_index_entries.c.group_key == ctx.group_key)
                .distinct()
            ).scalars()
        )
        entries = (
            conn.execute(
                select(
                    gb_index_entries.c.id,
                    gb_index_entries.c.kind,
                    gb_index_entries.c.canonical_id,
                    gb_index_entries.c.source_revision,
                    gb_index_entries.c.namespace,
                    gb_index_entries.c.provider_id,
                    gb_index_entries.c.idempotency_key,
                    gb_index_entries.c.state,
                ).where(gb_index_entries.c.group_key == ctx.group_key)
            )
            .mappings()
            .all()
        )
        running_jobs = (
            conn.execute(
                select(gb_jobs.c.ref_payload, gb_jobs.c.source_revision).where(
                    gb_jobs.c.group_key == ctx.group_key,
                    gb_jobs.c.state == "running",
                    gb_jobs.c.lease_until > database_time(),
                )
            )
            .mappings()
            .all()
        )
    pending = [
        entry for entry in entries if entry["state"] in ("delete_pending", "stale")
    ]
    if ctx.runtime.index is None:
        if any(entry["provider_id"] for entry in pending):
            raise ProviderError("dependency_unavailable")
        with ctx.conn() as conn:
            for entry in pending:
                conn.execute(
                    update(gb_index_entries)
                    .where(
                        gb_index_entries.c.group_key == ctx.group_key,
                        gb_index_entries.c.id == entry["id"],
                        gb_index_entries.c.state.in_(("delete_pending", "stale")),
                    )
                    .values(state="deleted")
                )
        return

    protected_keys = set()
    for job in running_jobs:
        payload = job["ref_payload"] or {}
        references = set(payload.values()) if isinstance(payload, dict) else set()
        for entry in entries:
            if (
                entry["state"] in ("pending", "indexing")
                and entry["canonical_id"] in references
                and (
                    job["source_revision"] is None
                    or job["source_revision"] == entry["source_revision"]
                )
            ):
                protected_keys.add(entry["idempotency_key"])

    records_by_namespace = {}
    for namespace in namespaces:
        if not ctx.heartbeat() or not ctx.owns():
            raise SourceError("unavailable")
        records_by_namespace[namespace] = _list_namespace(ctx.runtime.index, namespace)
    for entry in pending:
        records = records_by_namespace.get(entry["namespace"], ())
        if entry["provider_id"] and any(
            record.provider_id == entry["provider_id"] for record in records
        ):
            if not ctx.heartbeat() or not ctx.owns():
                raise SourceError("unavailable")
            try:
                ctx.runtime.index.delete(
                    namespace=entry["namespace"], provider_id=entry["provider_id"]
                )
            except ProviderError as exc:
                if exc.code != "not_found":
                    raise
        with ctx.conn() as conn:
            conn.execute(
                update(gb_index_entries)
                .where(
                    gb_index_entries.c.group_key == ctx.group_key,
                    gb_index_entries.c.id == entry["id"],
                    gb_index_entries.c.state.in_(("delete_pending", "stale")),
                )
                .values(state="deleted")
            )
    with ctx.conn() as conn:
        named = set(
            conn.execute(
                select(
                    gb_index_entries.c.namespace, gb_index_entries.c.provider_id
                ).where(
                    gb_index_entries.c.group_key == ctx.group_key,
                    gb_index_entries.c.provider_id.is_not(None),
                    gb_index_entries.c.state.in_(("active", "pending", "indexing")),
                )
            ).all()
        )
    for namespace, records in records_by_namespace.items():
        for record in records:
            if (namespace, record.provider_id) not in named and record.metadata.get(
                "index_key"
            ) not in protected_keys:
                if not ctx.heartbeat() or not ctx.owns():
                    raise SourceError("unavailable")
                try:
                    ctx.runtime.index.delete(
                        namespace=namespace, provider_id=record.provider_id
                    )
                except ProviderError as exc:
                    if exc.code != "not_found":
                        raise


def purge_group(ctx):
    with ctx.conn() as conn:
        if not STORE.owns(conn, ctx.job["id"], ctx.fence):
            return
        provider_ids = (
            conn.execute(
                select(gb_index_entries.c.provider_id).where(
                    gb_index_entries.c.group_key == ctx.group_key,
                    gb_index_entries.c.provider_id.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        namespaces = {
            namespace_for(ctx.runtime.config.collection, ctx.group_key, kind)
            for kind in ("discussion", "memory", "document")
        }
        namespaces.update(
            conn.execute(
                select(gb_index_entries.c.namespace)
                .where(gb_index_entries.c.group_key == ctx.group_key)
                .distinct()
            ).scalars()
        )
    if ctx.runtime.index is None and provider_ids:
        raise ProviderError("dependency_unavailable")
    if ctx.runtime.index is not None:
        for namespace in namespaces:
            previous_ids = None
            while True:
                if not ctx.heartbeat() or not ctx.owns():
                    raise SourceError("unavailable")
                records = _list_namespace(ctx.runtime.index, namespace)
                current_ids = {record.provider_id for record in records}
                if not current_ids:
                    break
                if previous_ids == current_ids:
                    raise ProviderError("bad_response")
                previous_ids = current_ids
                for record in records:
                    if not ctx.heartbeat() or not ctx.owns():
                        raise SourceError("unavailable")
                    try:
                        ctx.runtime.index.delete(
                            namespace=namespace, provider_id=record.provider_id
                        )
                    except ProviderError as exc:
                        if exc.code != "not_found":
                            raise
            if _list_namespace(ctx.runtime.index, namespace):
                raise ProviderError("bad_response")
    with ctx.conn() as conn:
        settings = STORE.settings(conn, ctx.group_key)
        if (
            not STORE.owns(conn, ctx.job["id"], ctx.fence)
            or settings["state"] != "purging"
            or settings["generation"] != ctx.generation
        ):
            return
        STORE.purge_catalog(conn, ctx.group_key)
        STORE.finish_purge(conn, ctx.group_key)


def handlers():
    return {
        "ingest_message": ingest_message,
        "refresh_discussion": refresh_discussion,
        "index_revision": index_revision,
        "invalidate_source": invalidate_source,
        "reconcile_index": reconcile_index,
        "purge_group": purge_group,
    }
