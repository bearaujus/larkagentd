from __future__ import annotations

from ..core.scopes import group_chat_scope
from ..knowledge.learned import learn
from ..knowledge.lineage import resolve_sources
from ..knowledge.memory import Memory
from ..knowledge.policy import effective
from ..knowledge.store import KnowledgeError

PROPOSE_DESCRIPTION = (
    "Save a durable, reusable decision, procedure or agreed fact for this group after you have "
    "found it in this group's Lark discussion. Supply the exact group search source IDs. "
    "The service rechecks every source in Lark. Avoid transient statuses, opinions, secrets, "
    "personal data and tool output. Report a saved fact only when state=active."
)
CORRECT_DESCRIPTION = (
    "Propose a correction to an existing group memory. This creates a new pending revision and does not edit "
    "or publish the existing fact. Nothing is shared until a group administrator reviews and approves it. "
    "Do not include secrets, credentials, personal data, or private tool output. Never claim it was published."
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
        "source_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    },
    "required": ["content", "category"],
    "additionalProperties": False,
}
CORRECT_PARAMETERS = {
    "type": "object",
    "properties": {
        "memory_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "replacement": {"type": "string", "minLength": 1, "maxLength": 4000},
        "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
        "source_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    },
    "required": ["memory_id", "replacement", "reason"],
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


def _run(ctx, arguments, method):
    group_key = _scope(ctx)
    runtime = getattr(ctx, "group_brain", None)
    if group_key is None:
        return {"status": "not_authorized"}
    if runtime is None:
        return {"status": "disabled", "reason": "global_disabled"}
    required = (
        {"content", "category"}
        if method == "propose"
        else {"memory_id", "replacement", "reason"}
    )
    if not _valid_args(arguments, required, {"source_ids"}):
        return _invalid()
    if method == "propose":
        if (
            not isinstance(arguments["content"], str)
            or len(arguments["content"]) > 4000
            or arguments["category"]
            not in PROPOSE_PARAMETERS["properties"]["category"]["enum"]
        ):
            return _invalid()
    elif any(
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
    try:
        resolved = resolve_sources(runtime, group_key, source_ids) if source_ids else []
    except Exception:
        return {"status": "source_unavailable"}
    verified_source_ids = list(
        dict.fromkeys(
            row["lark_message_id"]
            for row in resolved
            if row.get("group_key") == group_key
            and isinstance(row.get("lark_message_id"), str)
        )
    )[:20]
    if method == "propose":
        if not source_ids or not verified_source_ids:
            return {"status": "source_unavailable"}
        try:
            return learn(
                runtime,
                group_key,
                ctx.owner.split(":", 1)[-1],
                arguments,
                verified_source_ids,
                state.generation,
            )
        except KnowledgeError:
            return _invalid()
    try:
        with runtime.connection() as conn:
            current = effective(conn, runtime.config, group_key)
            if not current.allows("memory"):
                return {
                    "status": "disabled",
                    "reason": current.reasons[0]
                    if current.reasons
                    else "memory_disabled",
                }
            result = Memory(runtime).correct(
                conn,
                group_key,
                ctx.owner.split(":", 1)[-1],
                arguments["memory_id"],
                arguments["replacement"],
                arguments["reason"],
                verified_source_ids,
                verified_source_ids=verified_source_ids,
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
    return _run(ctx, arguments, "propose")


def group_memory_correct(ctx, arguments):
    return _run(ctx, arguments, "correct")
