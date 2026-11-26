from __future__ import annotations

from ..core.scopes import group_chat_scope
from ..knowledge.policy import effective
from ..knowledge.store import KnowledgeStore
from ..lark.ui_text import (
    KNOWLEDGE_DIRECT,
    KNOWLEDGE_DISABLED,
    KNOWLEDGE_HELP,
    KNOWLEDGE_LEARN_ACK,
    KNOWLEDGE_LEARN_THREAD,
    KNOWLEDGE_STATUS_DEGRADED,
    KNOWLEDGE_STATUS_ENABLED,
    KNOWLEDGE_STATUS_PAUSED,
    KNOWLEDGE_STATUS_PURGING,
)


def on_group_message(conn, service, *, chat_id, message_id, thread_id=None):
    """Queue only an opted-in Group Brain interaction, using verified receive metadata."""
    scope = group_chat_scope(chat_id)
    state = effective(conn, service.group_brain, scope)
    if (
        not state.serving
        or state.learning_mode != "interaction"
        or not (state.allows("recall") or state.allows("memory"))
    ):
        return None
    return KnowledgeStore().enqueue(
        conn,
        scope,
        "ingest_message",
        f"ingest:{message_id}:{state.generation}",
        state.generation,
        ref_payload={
            "message_id": message_id,
            "thread_id": thread_id or None,
            "reason": "interaction",
        },
    )


def knowledge_command(
    conn,
    service,
    text,
    *,
    chat_id,
    group,
    message_id,
    thread_ref=None,
    thread=None,
):
    """Handle Gateway-owned /knowledge commands without passing their text to Hermes."""
    if not group:
        return KNOWLEDGE_DIRECT
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
    if subcommand == "learn":
        if not state.serving or not (state.allows("recall") or state.allows("memory")):
            return KNOWLEDGE_DISABLED
        if not thread_ref:
            return KNOWLEDGE_LEARN_THREAD
        KnowledgeStore().enqueue(
            conn,
            scope,
            "ingest_message",
            f"ingest:{thread_ref}:{state.generation}",
            state.generation,
            ref_payload={
                "message_id": thread or message_id,
                "thread_id": thread_ref,
                "reason": "learn",
            },
        )
        return KNOWLEDGE_LEARN_ACK
    return KNOWLEDGE_HELP


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
