"""Model-facing operations use server-owned turn identity and approved actions."""

import httpx

from ..core import group_actions
from ..knowledge.sanitize import sanitize
from ..lark.links import parse_link
from .larkcli import LarkError


def _files(ctx):
    if ctx.files is None:
        raise LarkError("not_configured")
    return ctx.files


def file_read(ctx, args):
    files = _files(ctx)
    link = files.shared_link(ctx, args["url"], allow_history=True)
    return files.read(ctx, link, args.get("sheet_id"), args.get("range"))


def file_create(ctx, args):
    return _files(ctx).create(ctx, args)


def file_edit(ctx, args):
    files = _files(ctx)
    link = files.shared_link(ctx, args["url"], allow_thread=True)
    if args["action"] == "delete_matching_rows":
        return files.request_delete(ctx, link, args)
    return files.edit(ctx, link, args)


def request_web_search(ctx, args):
    if ctx.web is None:
        raise LarkError("web_search_unavailable")
    query = args["query"].strip()
    cleaned = sanitize(query, max_chars=400)
    if not query or cleaned.text != query or cleaned.blocked:
        raise LarkError("bad_search_query")
    if not ctx.request_action(
        "web_search",
        {"query": query},
        f"Send this query to public web search?\n{query}",
    ):
        raise LarkError("confirmation_already_pending")
    return {"confirmation_requested": True}


def run_confirmed_action(ctx, _args):
    identifier = (ctx.conversation or {}).get("approved_action")
    if not identifier:
        raise LarkError("confirmation_required")
    with ctx.store.engine.begin() as conn:
        action = group_actions.consume(conn, ctx.vault, ctx.turn, identifier)
    if not action:
        raise LarkError("confirmation_expired")
    if action["kind"] == "delete_rows" and ctx.allowed("lark_file_edit"):
        return _files(ctx).delete_rows(ctx, action["data"])
    if action["kind"] == "file_edit" and ctx.allowed("lark_file_edit"):
        data = action["data"]
        link = parse_link(data["url"])
        if not link:
            raise LarkError("bad_link")
        ctx.lark_allowed = frozenset({link.token})
        return _files(ctx).edit(ctx, link, data, confirmed=True)
    if action["kind"] == "web_search" and ctx.web and ctx.allowed("request_web_search"):
        try:
            return ctx.web.search(action["data"]["query"])
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise LarkError("web_search_unavailable") from None
    raise LarkError("not_enabled_for_group")


def require_group_share(ctx, url):
    link = parse_link(url)
    if not link:
        raise LarkError("bad_link")
    _files(ctx).require(ctx, link)


def search_group_knowledge(ctx, args):
    if ctx.source_library is None:
        raise LarkError("not_configured")
    from ..core.scopes import group_chat_scope
    from ..knowledge.store import KnowledgeError
    from .groupbrain import note_use

    scope = group_chat_scope(ctx.chat_id)
    config = ctx.group_brain.config if ctx.group_brain else None
    try:
        options = {"source_scope": args["scope"]} if "scope" in args else {}
        result = ctx.source_library.search(
            scope, args["query"], config, ctx=ctx, **options
        )
    except KnowledgeError as exc:
        raise LarkError(exc.code) from None
    generation = result.pop("_generation", None)
    if ctx.group_brain and any(item["scope"] == "group" for item in result["results"]):
        state = ctx.group_brain.effective(scope)
        if not state.serving or generation != state.generation:
            return {"status": "partial", "results": []}
        note_use(ctx, scope, result, generation)
    return result
