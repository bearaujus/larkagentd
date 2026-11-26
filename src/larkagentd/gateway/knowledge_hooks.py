from __future__ import annotations

import logging

from ..core.diagnostics import emit, job_fields
from ..core.scopes import group_chat_scope
from ..knowledge.learned import activate_candidate, verify_candidate
from ..knowledge.policy import effective
from ..lark.ui_text import (
    KNOWLEDGE_DISABLED,
    KNOWLEDGE_HELP,
    KNOWLEDGE_STATUS_DEGRADED,
    KNOWLEDGE_STATUS_ENABLED,
    KNOWLEDGE_STATUS_PAUSED,
    KNOWLEDGE_STATUS_PURGING,
)


def knowledge_command(conn, service, text, *, chat_id):
    """Handle Gateway-owned /knowledge commands without passing their text to Hermes."""
    scope = group_chat_scope(chat_id)
    state = effective(conn, service.group_brain, scope)
    parts = text.strip().split(maxsplit=1)
    subcommand = parts[1].strip().lower() if len(parts) > 1 else ""
    if subcommand == "status":
        if state.state in ("disabled", "setup_blocked"):
            return KNOWLEDGE_DISABLED
        if state.state == "paused":
            return KNOWLEDGE_STATUS_PAUSED
        if state.state == "purging":
            return KNOWLEDGE_STATUS_PURGING
        if state.state == "degraded":
            reason = (
                ", ".join(state.reasons).replace("_", " ") or "dependency unavailable"
            )
            return KNOWLEDGE_STATUS_DEGRADED.format(reason=reason)
        if state.state == "enabled":
            return KNOWLEDGE_STATUS_ENABLED
        return KNOWLEDGE_DISABLED
    return KNOWLEDGE_HELP


def prepare_passive_learning(service, job, session_id):
    """Fetch evidence only for the attempt whose reply actually finished."""
    runtime = service.access.knowledge_runtime
    if runtime is None or runtime.source is None:
        return None
    scope = group_chat_scope(job["payload"].get("chat"))
    if scope is None:
        return None
    with service.store.engine.connect() as conn:
        payload = service.store.job_payload(conn, job["id"]) or {}
        candidate = payload.get("passive_candidate")
        generation = payload.get("passive_generation")
        if not candidate or candidate["session_id"] != session_id:
            return None
        state = effective(conn, runtime.config, scope)
        if not state.allows("memory") or state.generation != generation:
            return None
    owner = job["owner"].partition(":")[2]
    live = verify_candidate(runtime, scope, owner, candidate)
    if live is None:
        emit("knowledge.passive", **job_fields(job), outcome="source_unavailable")
        return None
    return runtime, scope, owner, candidate, live, generation


def finish_passive_learning(conn, job, candidate):
    if candidate is None:
        return
    try:
        with conn.begin_nested():
            outcome = activate_candidate(conn, *candidate)
    except Exception as exc:
        emit(
            "knowledge.passive",
            logging.WARNING,
            **job_fields(job),
            outcome="save_failed",
            error_type=type(exc).__name__,
        )
        return
    emit("knowledge.passive", **job_fields(job), outcome=outcome)


def start_knowledge_workers(config, store, client, stop, index=None):
    """Create isolated knowledge worker threads only when the emergency switch is enabled."""
    if not config.group_brain.enabled:
        return []
    from ..knowledge.handlers import DOCUMENT_KINDS
    from ..knowledge.runtime import GroupBrainRuntime
    from ..knowledge.worker import start_worker_threads
    from ..lark.group_source import SdkGroupSource

    runtime = GroupBrainRuntime(
        store.engine,
        config.group_brain,
        source=SdkGroupSource(client),
        index=index,
        llm=None,
    )
    return start_worker_threads(
        runtime,
        stop,
        "gateway-knowledge",
        config.group_brain.worker_concurrency,
        exclude_kinds=DOCUMENT_KINDS,
    )
