"""Activate and retrieve learned records with live Lark source checks."""

from sqlalchemy import and_, func, select, tuple_

from .catalog import Catalog
from .lark_sources import SourceError
from .memory import Memory
from .policy import effective, store
from .sanitize import sanitize
from .tables import (
    gb_memory,
    gb_memory_revisions,
    gb_memory_sources,
    gb_messages,
    gb_source_refs,
)


def live_messages(runtime, scope, ids):
    try:
        return read_messages(runtime.source, scope, ids)
    except Exception:
        return {}


def read_messages(source, scope, ids):
    if source is None:
        raise SourceError("unavailable")
    messages = source.messages(scope.removeprefix("group:"), ids)
    return {
        message.message_id: message
        for message in messages
        if message.message_id in ids
        and message.chat_id == scope.removeprefix("group:")
        and message.sender_type == "user"
        and sanitize(message.text).text
    }


def learn(runtime, scope, owner, args, ids, generation):
    """Re-read the cited group messages and attach their persisted revisions."""
    if not ids or len(ids) > 8:
        return {"status": "source_unavailable"}
    live = live_messages(runtime, scope, ids)
    if set(live) != set(ids):
        return {"status": "source_unavailable"}
    # New interactive facts can use lexical retrieval without an embedding provider.
    # An existing optional index remains available to the legacy document/index worker.
    with runtime.connection() as conn:
        store.row(conn, scope, lock=True)
        state = effective(conn, runtime.config, scope)
        if not state.allows("memory") or state.generation != generation:
            return {"status": "disabled"}
        Catalog().upsert_messages(
            conn,
            scope,
            live.values(),
            retention_days=runtime.config.source_retention_days,
        )
        memory = Memory()
        result = memory.propose(
            conn, scope, owner, args["content"], args["category"], ids
        )
        if not result.get("memory_id") or result.get("duplicate"):
            return {"status": "ok", **result}
        digest = conn.scalar(
            select(gb_memory_revisions.c.content_hash).where(
                gb_memory_revisions.c.group_key == scope,
                gb_memory_revisions.c.memory_id == result["memory_id"],
                gb_memory_revisions.c.revision == result["revision"],
            )
        )
        activated = memory.approve(
            conn,
            scope,
            result["memory_id"],
            result["revision"],
            digest,
            "verified_group_source",
            True,
        )
        if activated.get("status") != "active":
            return {"status": "source_unavailable"}
        store.audit(conn, scope, owner, "memory_learned", target_id=result["memory_id"])
        return {"status": "ok", **result, "state": "active"}


def references(conn, scope, memory_id, revision):
    rows = (
        conn.execute(
            select(
                gb_source_refs.c.lark_message_id,
                gb_source_refs.c.source_url,
                gb_source_refs.c.visibility_status,
                gb_messages.c.content_hash,
                gb_messages.c.revision,
                gb_source_refs.c.source_revision,
            )
            .join(
                gb_memory_sources,
                and_(
                    gb_memory_sources.c.group_key == gb_source_refs.c.group_key,
                    gb_memory_sources.c.source_ref_id == gb_source_refs.c.id,
                ),
            )
            .outerjoin(
                gb_messages,
                and_(
                    gb_messages.c.group_key == gb_source_refs.c.group_key,
                    gb_messages.c.id == gb_source_refs.c.canonical_source_id,
                    gb_messages.c.status == "active",
                    gb_messages.c.expires_at > func.now(),
                    gb_messages.c.visibility_status == "visible",
                ),
            )
            .where(
                gb_source_refs.c.group_key == scope,
                gb_memory_sources.c.memory_id == memory_id,
                gb_memory_sources.c.memory_revision == revision,
            )
            .order_by(gb_source_refs.c.id)
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def verify(runtime, scope, reference_sets):
    return [
        entry["sources"]
        for entry in check_sources(runtime.source, scope, reference_sets)
    ]


def check_sources(source, scope, reference_sets):
    ids = list(
        dict.fromkeys(
            row["lark_message_id"]
            for refs in reference_sets
            for row in refs
            if row["lark_message_id"]
        )
    )
    live = {}
    unavailable = set()
    for start in range(0, len(ids), 50):
        batch = ids[start : start + 50]
        try:
            live.update(read_messages(source, scope, batch))
        except SourceError as exc:
            if exc.code not in {"not_found", "forbidden"}:
                unavailable.update(batch)
        except Exception:
            unavailable.update(batch)
    hashes = {
        key: Catalog._hash(sanitize(message.text).text) for key, message in live.items()
    }
    checked = []
    for refs in reference_sets:
        status = "verified"
        evidence = []
        for ref in refs:
            key = ref["lark_message_id"]
            if key in unavailable:
                status = "temporarily_unavailable"
                break
            if (
                ref["visibility_status"] != "visible"
                or ref["revision"] != ref["source_revision"]
                or not ref["content_hash"]
                or hashes.get(key) != ref["content_hash"]
            ):
                status = "source_changed_or_unavailable"
                break
            message = live[key]
            evidence.append(
                {
                    "source_id": key,
                    "url": message.link,
                    "verified": True,
                    "time": message.created_at.isoformat(),
                    "excerpt": sanitize(message.text).text[:240],
                }
            )
        if not refs:
            status = "source_changed_or_unavailable"
        checked.append(
            {"status": status, "sources": evidence if status == "verified" else []}
        )
    return checked


PAGE_SIZE = 10


def list_records(conn, scope, after):
    boundary = []
    if after:
        anchor = conn.scalar(
            select(gb_memory.c.created_at).where(
                gb_memory.c.group_key == scope, gb_memory.c.id == after
            )
        )
        if anchor is None:
            return [], None
        boundary.append(
            tuple_(gb_memory.c.created_at, gb_memory.c.id) < tuple_(anchor, after)
        )
    rows = (
        conn.execute(
            select(
                gb_memory.c.id,
                gb_memory.c.category,
                gb_memory.c.current_revision,
                gb_memory_revisions.c.sanitized_content,
                gb_memory_revisions.c.approved_by,
            )
            .join(
                gb_memory_revisions,
                and_(
                    gb_memory_revisions.c.group_key == gb_memory.c.group_key,
                    gb_memory_revisions.c.memory_id == gb_memory.c.id,
                    gb_memory_revisions.c.revision == gb_memory.c.current_revision,
                ),
            )
            .where(
                gb_memory.c.group_key == scope,
                *boundary,
                gb_memory.c.state == "active",
                gb_memory_revisions.c.expires_at > func.now(),
            )
            .order_by(gb_memory.c.created_at.desc(), gb_memory.c.id.desc())
            .limit(PAGE_SIZE + 1)
        )
        .mappings()
        .all()
    )
    return [
        {**row, "refs": references(conn, scope, row["id"], row["current_revision"])}
        for row in rows[:PAGE_SIZE]
    ], rows[PAGE_SIZE - 1]["id"] if len(rows) > PAGE_SIZE else None
