from ..core.scopes import group_chat_scope
from ..knowledge.documents import Documents
from ..knowledge.store import KnowledgeError

DOCUMENT_DESCRIPTION = (
    "Propose a specific Lark document for administrator review. A proposal does not grant sharing access "
    "and does not promise that the document will be indexed. Do not include a title as evidence."
)
DOCUMENT_PARAMETERS = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "maxLength": 2048},
        "title_hint": {"type": "string", "maxLength": 200},
    },
    "required": ["url"],
    "additionalProperties": False,
}


def group_document_propose(ctx, arguments):
    if ctx.chat_type != "group" or not ctx.chat_id:
        return {"status": "not_authorized"}
    runtime = ctx.group_brain
    if runtime is None:
        return {"status": "disabled"}
    if not isinstance(arguments, dict) or set(arguments) - {"url", "title_hint"}:
        return {"status": "invalid_request"}
    url = arguments.get("url")
    hint = arguments.get("title_hint")
    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= 2048
        or (hint is not None and (not isinstance(hint, str) or len(hint) > 200))
    ):
        return {"status": "invalid_request"}
    group_key = group_chat_scope(ctx.chat_id)
    try:
        with runtime.connection() as conn:
            document = Documents(runtime).propose(
                conn, group_key, ctx.owner.split(":", 1)[-1], url
            )
    except KnowledgeError as exc:
        return {"status": "disabled" if exc.code == "disabled" else "invalid_request"}
    except Exception:
        return {"status": "invalid_request"}
    return {
        "status": "ok",
        "document_id": document["id"],
        "state": "pending",
        "message": "This is a proposal awaiting administrator approval. Indexing is not promised.",
    }
