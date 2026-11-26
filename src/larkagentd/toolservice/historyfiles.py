"""Read-only provenance for file links returned by a group history tool."""

from ..core.scopes import group_chat_scope
from ..knowledge.lark_sources import CliGroupSource, SourceError
from ..lark.links import links
from .larkcli import MESSAGE_ID, LarkError


def observe_file_links(found, message_id, text):
    if isinstance(message_id, str) and MESSAGE_ID.fullmatch(message_id):
        found.update({link.url: message_id for link in links(text)})


def verify_history_file(ctx, link):
    """Confirm a file link still appears in its human-authored source in this group."""
    ref = (ctx.lark_history_refs or {}).get(link.url) if link else None
    if not ref or ctx.chat_type != "group":
        return False
    tool, generation = ref["tool"], ref["generation"]
    if ctx.allowed is None or not ctx.allowed(tool):
        return False
    scope = group_chat_scope(ctx.chat_id)
    if scope is None or ctx.lark is None:
        return False
    if generation is not None:
        state = ctx.group_brain.effective(scope) if ctx.group_brain else None
        if (
            state is None
            or not state.allows("recall")
            or state.generation != generation
        ):
            return False
    try:
        messages = CliGroupSource(ctx.lark).messages(ctx.chat_id, [ref["message_id"]])
    except SourceError as exc:
        raise LarkError(
            "lark_history_source_unavailable",
            stage="history_source",
            access="view",
            upstream=exc.code,
        ) from None
    for message in messages:
        if message.sender_type == "user" and link in links(message.text):
            # Apply only to this read's context so the organization visibility check
            # can establish the audience. Write tools build their own context.
            ctx.lark_allowed = ctx.lark_allowed | {link.token}
            return True
    raise LarkError(
        "lark_history_source_unavailable", stage="history_source", access="view"
    )
