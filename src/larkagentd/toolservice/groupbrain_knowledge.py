"""Search and revalidate learned facts and Lark discussions in the calling group."""

from sqlalchemy import select

from ..knowledge.catalog import Catalog
from ..knowledge.learned import references, verify
from ..knowledge.memory import Memory
from ..knowledge.recall import Recall, iso, opaque
from ..knowledge.tables import (
    gb_discussions,
    gb_memory,
)
from .groupbrain import UNTRUSTED, note_use, requester_of, scope_of

KINDS = ("memory", "discussion")
CAPABILITY = {"discussion": "recall", "memory": "memory"}
MAX_EVIDENCE_IDS = 10
SCAN_ROWS = 5000

SEARCH_DESCRIPTION = (
    "Search this group's learned facts and earlier Lark discussions for a decision, process, or "
    "previous conversation. Search only the originating group. Optionally select memory or discussion. "
    "Date and participant filters apply to discussions. Cite the verified source URL when supplied, "
    "and use the excerpt and time if Lark does not provide a link. For workspace and group Markdown "
    "use search_group_knowledge."
)
SEARCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What to look for."},
        "kinds": {
            "type": "array",
            "items": {"type": "string", "enum": list(KINDS)},
            "description": "Which kinds to search; all when omitted.",
        },
        "date_from": {
            "type": "string",
            "description": "Discussions only: a date or RFC 3339 time.",
        },
        "date_to": {
            "type": "string",
            "description": "Discussions only: a date or RFC 3339 time.",
        },
        "participant": {"type": "string", "enum": ["self", "any"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
    },
    "required": ["query"],
}
SOURCES_DESCRIPTION = (
    "Check again that evidence from group_knowledge_search can still be used, and get its current source "
    "links. Pass the evidence_id values you are about to cite (at most 10). An evidence_id that is no "
    "longer available is returned as unavailable: do not cite it."
)
SOURCES_PARAMETERS = {
    "type": "object",
    "properties": {
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": MAX_EVIDENCE_IDS,
        }
    },
    "required": ["evidence_ids"],
}


def _invalid():
    return {"status": "invalid_request"}


def memory_evidence_id(memory_id):
    return opaque("ev", "memory", memory_id)


def _memory_items(runtime, group_key, query, limit):
    with runtime.connection() as conn:
        found = Memory(runtime).search(runtime, conn, group_key, query, limit)
        items = found.get("results", [])
        refs = [
            references(conn, group_key, item["memory_id"], item["revision"])
            for item in items
        ]
    verified = verify(runtime, group_key, refs)
    return [
        {
            "evidence_id": memory_evidence_id(item["memory_id"]),
            "kind": "memory",
            "category": item["category"],
            "text": item["content"],
            "approved_at": iso(item["approved_at"])
            if item.get("approved_at")
            else None,
            "revision": item["revision"],
            "sources": sources,
        }
        for item, sources in zip(items, verified)
        if sources
    ]


def _discussion_items(runtime, group_key, requester, arguments, limit):
    request = {"query": arguments["query"], "limit": limit}
    for key in ("date_from", "date_to", "participant"):
        if key in arguments:
            request[key] = arguments[key]
    found = Recall(runtime).search_discussions(group_key, requester, request)
    if found.get("status") not in ("ok", "partial"):
        return [], found.get("status"), None
    items = [
        {
            "evidence_id": item["evidence_id"],
            "kind": "discussion",
            "title": item["title"],
            "text": item["snippet"],
            "started_at": item["started_at"],
            "ended_at": item["ended_at"],
            "participant_match": item["participant_match"],
            "sources": item["sources"],
            "discussion_id": item["discussion_id"],
        }
        for item in found.get("results", [])
    ]
    return items, found["status"], found.get("coverage")


def _fuse(per_kind, limit):
    """Rank learned facts ahead of discussion excerpts at equal relevance."""
    scored = []
    for order, kind in enumerate(KINDS):
        for rank, item in enumerate(per_kind.get(kind, []), 1):
            scored.append((-1 / (60 + rank), order, rank, item))
    scored.sort(key=lambda row: row[:3])
    return [row[3] for row in scored[:limit]]


def _valid_search(arguments):
    allowed = {"query", "kinds", "date_from", "date_to", "participant", "limit"}
    if not isinstance(arguments, dict) or not set(arguments) <= allowed:
        return None
    query = arguments.get("query")
    kinds = arguments.get("kinds", list(KINDS))
    limit = arguments.get("limit", 5)
    if (
        not isinstance(query, str)
        or not 1 <= len(query.strip()) <= 500
        or not isinstance(kinds, list)
        or not kinds
        or not all(isinstance(k, str) and k in KINDS for k in kinds)
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 10
    ):
        return None
    return query.strip(), list(dict.fromkeys(kinds)), limit


def group_knowledge_search(ctx, arguments):
    valid = _valid_search(arguments)
    group_key = scope_of(ctx)
    if group_key is None:
        return {"status": "not_authorized"}
    runtime = ctx.group_brain
    if runtime is None:
        return {"status": "disabled", "reason": "global_disabled"}
    if valid is None:
        return _invalid()
    query, kinds, limit = valid
    state = runtime.effective(group_key)
    generation = state.generation
    per_kind, unavailable, partial, coverage = {}, [], False, None
    serving = [kind for kind in kinds if state.allows(CAPABILITY[kind])]
    if not serving:
        return {
            "status": "disabled",
            **({"reason": state.reasons[0]} if state.reasons else {}),
        }
    for kind in serving:
        try:
            if kind == "memory":
                per_kind[kind] = _memory_items(runtime, group_key, query, limit)
            else:
                items, status, coverage = _discussion_items(
                    runtime, group_key, requester_of(ctx), arguments, limit
                )
                if status not in ("ok", "partial"):
                    unavailable.append({"kind": kind, "reason": status})
                    continue
                partial = partial or status == "partial"
                per_kind[kind] = items
        except Exception:
            # Never the exception text: it can carry a path, a query or a provider body.
            unavailable.append({"kind": kind, "reason": "dependency_unavailable"})
    for kind in kinds:
        if kind not in serving:
            unavailable.append({"kind": kind, "reason": "not_enabled"})
    result = {
        "status": "partial" if unavailable or partial else "ok",
        "results": _fuse(per_kind, limit),
        "unavailable": unavailable,
        "content_is_untrusted_data": True,
        "notice": UNTRUSTED,
    }
    if coverage is not None:
        result["discussion_coverage"] = coverage
    if ctx.job_note:
        cached = (ctx.conversation or {}).get("gb_discussion_evidence") or {}
        current = {
            item["evidence_id"]: item["discussion_id"]
            for item in result["results"]
            if item["kind"] == "discussion"
        }
        if current:
            ctx.job_note(
                gb_discussion_evidence=dict(list({**cached, **current}.items())[-30:])
            )
    note_use(ctx, group_key, result, generation)
    return result


# --- revalidation ---


def _memory_sources(runtime, group_key, wanted):
    with runtime.connection() as conn:
        rows = conn.execute(
            select(gb_memory.c.id, gb_memory.c.current_revision)
            .where(
                gb_memory.c.group_key == group_key,
                gb_memory.c.state == "active",
            )
            .limit(SCAN_ROWS)
        ).all()
        matching = [
            (
                memory_evidence_id(row.id),
                references(conn, group_key, row.id, row.current_revision),
            )
            for row in rows
            if memory_evidence_id(row.id) in wanted
        ]
    verified = verify(runtime, group_key, [refs for _, refs in matching])
    return {key: refs for (key, _), refs in zip(matching, verified) if refs}


def _discussion_sources(conn, group_key, wanted):
    catalog = Catalog()
    found = {}
    rows = conn.execute(
        select(
            gb_discussions.c.id,
            gb_discussions.c.kind,
            gb_discussions.c.native_thread_id,
            gb_discussions.c.stable_window_key,
            gb_discussions.c.root_message_id,
        ).where(
            gb_discussions.c.group_key == group_key,
            gb_discussions.c.status == "active",
        )
    ).mappings()
    for row in list(rows)[:SCAN_ROWS]:
        key = (
            row["native_thread_id"]
            or row["stable_window_key"]
            or row["root_message_id"]
        )
        evidence_id = opaque("ev", opaque("d", group_key, row["kind"], key))
        if evidence_id not in wanted:
            continue
        sources = catalog.sources_of(conn, group_key, row["id"])
        if sources:
            found[evidence_id] = [
                {
                    "url": source["message_link"],
                    "timestamp": iso(source["created_at"]),
                    "verified": True,
                }
                for source in sources[:5]
            ]
    return found


def group_knowledge_sources(ctx, arguments):
    group_key = scope_of(ctx)
    if group_key is None:
        return {"status": "not_authorized"}
    runtime = ctx.group_brain
    if runtime is None:
        return {"status": "disabled", "reason": "global_disabled"}
    ids = arguments.get("evidence_ids") if isinstance(arguments, dict) else None
    if (
        not isinstance(arguments, dict)
        or set(arguments) != {"evidence_ids"}
        or not isinstance(ids, list)
        or not 1 <= len(ids) <= MAX_EVIDENCE_IDS
        or not all(isinstance(i, str) and 0 < len(i) <= 64 for i in ids)
    ):
        return _invalid()
    state = runtime.effective(group_key)
    if not state.allows("recall") and not state.allows("memory"):
        return {
            "status": "disabled",
            **({"reason": state.reasons[0]} if state.reasons else {}),
        }
    wanted = set(ids)
    valid = {}
    if state.allows("memory"):
        valid.update(_memory_sources(runtime, group_key, wanted))
    if state.allows("recall"):
        cached = (ctx.conversation or {}).get("gb_discussion_evidence") or {}
        recall = Recall(runtime)
        for identifier in ids:
            if discussion := cached.get(identifier):
                found = recall.get_discussion(
                    group_key,
                    requester_of(ctx),
                    {"discussion_id": discussion, "limit": 10},
                )
                if found.get("status") in ("ok", "partial"):
                    valid[identifier] = [
                        item["source"]
                        for item in found.get("messages", [])
                        if item.get("source")
                    ]
        remaining = wanted - valid.keys() - cached.keys()
        if remaining:
            with runtime.connection() as conn:
                valid.update(_discussion_sources(conn, group_key, remaining))
    result = {
        "status": "ok",
        "evidence": [
            {"evidence_id": i, "status": "valid", "sources": valid[i]}
            if i in valid
            else {"evidence_id": i, "status": "unavailable"}
            for i in ids
        ],
        "content_is_untrusted_data": True,
        "notice": UNTRUSTED,
    }
    note_use(ctx, group_key, result, state.generation)
    return result
