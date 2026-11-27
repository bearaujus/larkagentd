"""Search attached pages, then verify live membership, audience and revision."""

from sqlalchemy import func, select

from ..core.scopes import group_chat_scope
from ..lark.links import DocumentLink
from ..toolservice.larkcli import LarkError
from .attachments import attachment_link, attachments, passages
from .library import GLOBAL, sources
from .policy import effective
from .policy import store as policies


def verified_hits(store, reader, ctx, scopes, terms, config):
    scope = group_chat_scope(ctx.chat_id)
    if ctx.chat_type != "group" or scope not in scopes and scopes != [GLOBAL]:
        return [], False
    terms = sorted(terms)[:32]
    query = func.websearch_to_tsquery(
        "simple", " OR ".join(f'"{word}"' for word in terms)
    )
    rank = func.ts_rank_cd(passages.c.search_vector, query)
    with store.engine.begin() as conn:
        candidates = (
            conn.execute(
                select(passages, attachments.c.url, sources.c.scope, sources.c.name)
                .join(attachments, attachments.c.source_id == passages.c.source_id)
                .join(sources, sources.c.id == attachments.c.source_id)
                .where(
                    sources.c.scope.in_(scopes),
                    passages.c.search_vector.op("@@")(query),
                )
                .order_by(
                    rank.desc(),
                    (sources.c.scope == scope).desc(),
                    passages.c.source_id,
                    passages.c.ordinal,
                )
                .limit(40)
            )
            .mappings()
            .all()
        )

    checks, accepted, seen = {}, [], set()
    partial = False
    for row in candidates:
        identity = (row["source_id"], row["doc"], row["digest"])
        passage = (row["doc"], row["ordinal"])
        if passage in seen:
            continue
        if identity not in checks:
            if len(checks) >= 10:
                partial = True
                break
            try:
                page = reader.current_page(attachment_link(row["url"]), row)
                try:
                    ctx.files.require(ctx, page.link, attachment=True)
                except LarkError as exc:
                    if not page.node or exc.code not in {
                        "lark_permission_unverified",
                        "lark_share_required",
                        "lark_bot_view_denied",
                        "forbidden",
                    }:
                        raise
                    # Wiki-owned pages can expose the chat collaborator on the wiki node.
                    ctx.files.require(
                        ctx,
                        DocumentLink("wiki", page.node, page.citation),
                        attachment=True,
                    )
                checks[identity] = (
                    page.citation
                    if reader.read(page).digest() == row["digest"]
                    else None
                )
            except LarkError as exc:
                checks[identity] = None
                partial |= exc.code in {"timeout", "rate_limited", "lark_unavailable"}
        if citation := checks[identity]:
            seen.add(passage)
            accepted.append((row, citation))
            if len(accepted) == 5:
                break
    hits = []
    # The attachment, page and group may have changed during the provider reads.
    with store.engine.begin() as conn:
        if not policies.registered(conn, scope):
            return [], True
        for row, citation in accepted:
            if row["scope"] != GLOBAL and (
                not config or not effective(conn, config, scope).serving
            ):
                continue
            current = conn.scalar(
                select(passages.c.digest)
                .join(attachments)
                .where(
                    passages.c.source_id == row["source_id"],
                    passages.c.doc == row["doc"],
                    passages.c.ordinal == row["ordinal"],
                    attachments.c.url == row["url"],
                )
            )
            if current == row["digest"]:
                searchable = " ".join(
                    [row["title"], row["section"], row["body"]]
                ).lower()
                hits.append(
                    (
                        sum(term in searchable for term in terms),
                        {
                            "source": row["title"],
                            "scope": "workspace" if row["scope"] == GLOBAL else "group",
                            "section": row["section"],
                            "url": citation,
                            "text": row["body"][:1200],
                        },
                    )
                )
    return hits, partial
