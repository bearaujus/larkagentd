from __future__ import annotations

from ..core.scopes import group_chat_scope
from ..knowledge.catalog import Catalog
from ..knowledge.learned import candidate_evidence
from ..knowledge.lineage import resolve_sources
from ..knowledge.memory import Memory
from ..knowledge.policy import effective, store
from ..knowledge.recall import opaque
from ..knowledge.sanitize import sanitize
from ..knowledge.store import KnowledgeError

PROPOSE_DESCRIPTION = (
    "At the end of an enabled group turn, quietly nominate one durable decision, procedure, "
    "standard or fact that would help this group in future turns. Skip routine questions, "
    "greetings, temporary status, guesses, instructions directed at the assistant, personal "
    "data, credentials, private tool results and duplicates. Use evidence written by people in "
    "this group. With no source_ids, evidence must be a verbatim excerpt from the current "
    "message. For evidence from group search, use its exact source IDs and an excerpt from "
    "one of those messages. The Gateway checks the sources after the turn succeeds."
)
CORRECT_DESCRIPTION = (
    "When a person explicitly asks to correct a saved group fact, use its memory_id from "
    "group_knowledge_search and a verbatim excerpt from a human group message supporting the replacement. "
    "Omit source_ids for the current message. Otherwise use the exact source IDs returned by group search. "
    "This queues a correction for a workspace administrator; the existing fact stays active until approval. "
    "Skip secrets, personal data, and private tool output."
)
PROPOSE_PARAMETERS = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "minLength": 1, "maxLength": 4000},
        "category": {
            "type": "string",
            "enum": [
                "decision",
                "business_logic",
                "procedure",
                "technical_discovery",
                "lesson",
                "standard",
                "project_context",
            ],
        },
        "evidence": {"type": "string", "minLength": 1, "maxLength": 500},
        "source_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": ["content", "category", "evidence"],
    "additionalProperties": False,
}
CORRECT_PARAMETERS = {
    "type": "object",
    "properties": {
        "memory_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "replacement": {"type": "string", "minLength": 1, "maxLength": 4000},
        "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 500},
        "source_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    },
    "required": ["memory_id", "replacement", "reason", "evidence"],
    "additionalProperties": False,
}


def _scope(ctx):
    if getattr(ctx, "chat_type", None) != "group" or not getattr(ctx, "chat_id", None):
        return None
    return group_chat_scope(ctx.chat_id)


def _invalid():
    return {"status": "invalid_request"}


def _valid_args(arguments, required, optional):
    if (
        not isinstance(arguments, dict)
        or not required <= set(arguments)
        or set(arguments) - required - optional
    ):
        return False
    if "source_ids" in arguments and (
        not isinstance(arguments["source_ids"], list)
        or len(arguments["source_ids"]) > 20
        or any(
            not isinstance(item, str) or not item or len(item) > 256
            for item in arguments["source_ids"]
        )
    ):
        return False
    return True


def _source_ids(ctx, group_key, requested):
    if not requested:
        current = (ctx.turn or {}).get("message_id")
        return [current] if current else []
    supplied = list(dict.fromkeys(requested))
    aliases = {item: item for item in supplied if item.startswith("om_")}
    pending = [item for item in supplied if item not in aliases]
    if pending:
        for row in resolve_sources(ctx.group_brain, group_key, pending):
            if row.get("group_key") == group_key:
                message_id = row["lark_message_id"]
                aliases[opaque("src", group_key, message_id)] = message_id
    if any(item not in aliases for item in supplied):
        return []
    return list(dict.fromkeys(aliases[item] for item in supplied))


def group_memory_correct(ctx, arguments):
    group_key = _scope(ctx)
    runtime = getattr(ctx, "group_brain", None)
    if group_key is None:
        return {"status": "not_authorized"}
    if runtime is None:
        return {"status": "disabled", "reason": "global_disabled"}
    required = {"memory_id", "replacement", "reason", "evidence"}
    if not _valid_args(arguments, required, {"source_ids"}) or any(
        not isinstance(arguments[key], str) or not arguments[key] for key in required
    ):
        return _invalid()
    state = runtime.effective(group_key)
    if not state.allows("memory"):
        return {
            "status": "disabled",
            "reason": state.reasons[0] if state.reasons else "memory_disabled",
        }
    source_ids = arguments.get("source_ids", [])
    evidence = sanitize(arguments["evidence"], max_chars=500)
    if not evidence.text or evidence.reasons:
        return _invalid()
    owner = ctx.owner.split(":", 1)[-1]
    try:
        ids = _source_ids(ctx, group_key, source_ids)
        outcome, live = candidate_evidence(
            runtime,
            group_key,
            owner,
            {
                "ids": ids,
                "current_only": not source_ids,
                "evidence": evidence.text,
            },
        )
    except Exception:
        return {"status": "source_unavailable"}
    if live is None:
        return {"status": outcome}
    try:
        with runtime.connection() as conn:
            store.row(conn, group_key, lock=True)
            current = effective(conn, runtime.config, group_key)
            if not current.allows("memory") or current.generation != state.generation:
                return {
                    "status": "disabled",
                    "reason": current.reasons[0]
                    if current.reasons
                    else "memory_disabled",
                }
            Catalog().upsert_messages(
                conn,
                group_key,
                live.values(),
                retention_days=runtime.config.source_retention_days,
            )
            result = Memory(runtime).correct(
                conn,
                group_key,
                owner,
                arguments["memory_id"],
                arguments["replacement"],
                arguments["reason"],
                live,
                require_active=True,
            )
    except KnowledgeError:
        return _invalid()
    if result.get("status") == "not_found":
        return {"status": "source_unavailable"}
    if result.get("reason") == "sensitive":
        return {"status": "invalid_request", "reason": "sensitive"}
    if result.get("state") == "refused":
        return {"status": "invalid_request", "reason": "sensitive"}
    return {"status": "ok", **result}


def group_memory_propose(ctx, arguments):
    group_key = _scope(ctx)
    if group_key is None:
        return {"status": "not_authorized"}
    runtime = ctx.group_brain
    if runtime is None:
        return {"status": "disabled"}
    required = {"content", "category", "evidence"}
    if not _valid_args(arguments, required, {"source_ids"}):
        return _invalid()
    category = arguments["category"]
    clean = sanitize(arguments["content"])
    evidence = sanitize(arguments["evidence"], max_chars=500)
    if (
        category not in PROPOSE_PARAMETERS["properties"]["category"]["enum"]
        or not clean.text
        or clean.reasons
        or not evidence.text
        or evidence.reasons
        or len(arguments.get("source_ids", [])) > 8
    ):
        return _invalid()
    generation = (ctx.conversation or {}).get("passive_generation")
    if generation is None:
        return {"status": "disabled"}
    state = runtime.effective(group_key)
    if not state.allows("memory") or generation != state.generation:
        return {"status": "disabled"}
    sources = arguments.get("source_ids")
    try:
        ids = _source_ids(ctx, group_key, sources)
    except Exception:
        return {"status": "source_unavailable"}
    if not ids:
        return {"status": "source_unavailable"}
    staged = ctx.job_note and ctx.job_note(
        passive_candidate={
            "session_id": ctx.session_id,
            "content": clean.text,
            "category": category,
            "evidence": evidence.text,
            "ids": ids,
            "current_only": not sources,
        }
    )
    return {"status": "queued" if staged else "not_staged"}
