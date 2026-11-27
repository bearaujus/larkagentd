"""Read explicit Lark attachment roots. Never follow links embedded in page text."""

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..lark.links import TOKEN, DocumentLink
from ..toolservice.larkcli import LarkError


def _token(value):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise LarkError("lark_unavailable")
    return value


@dataclass(frozen=True)
class Page:
    link: DocumentLink
    node: str | None
    title: str = ""

    @property
    def citation(self):
        if self.node:
            return f"https://{urlsplit(self.link.url).hostname}/wiki/{self.node}"
        return self.link.url


@dataclass(frozen=True)
class Content:
    title: str
    markdown: str
    revision: str

    def digest(self):
        return hashlib.sha256(
            json.dumps((self.title, self.markdown, self.revision)).encode()
        ).hexdigest()


@dataclass(frozen=True)
class UnlistedBranch:
    error: str


class WikiReader:
    def __init__(self, reader):
        self.reader = reader

    def node(self, token):
        node = self.reader._call(("wiki", "+node-get"), ["--node-token", _token(token)])
        if not isinstance(node, dict) or node.get("node_token") != token:
            raise LarkError("lark_unavailable")
        return node

    def root_title(self, root):
        """Read the attached resource's own metadata, independent of its content or children."""
        if root.kind == "wiki":
            return self.node(root.token).get("title")
        if root.kind == "space":
            path, key, field = f"wiki/v2/spaces/{root.token}", "space", "name"
        elif root.kind == "docx":
            path, key, field = f"docx/v1/documents/{root.token}", "document", "title"
        else:
            path, key, field = f"doc/v2/meta/{root.token}", None, "title"
        raw = self.reader._invoke(("api", "GET", "/open-apis/" + path), [])
        resource = raw.get(key) if isinstance(raw, dict) and key else raw
        if not isinstance(resource, dict) or (
            key is None and resource.get("delete_flag") != 0
        ):
            raise LarkError("not_found")
        return resource.get(field)

    def listing(self, space, parent=None, cursor=None, size=50):
        if not isinstance(space, str) or not space.isascii() or not space.isdigit():
            raise LarkError("lark_unavailable")
        args = ["--space-id", space, "--page-size", str(size)]
        if parent:
            args.extend(["--parent-node-token", _token(parent)])
        if cursor:
            args.extend(["--page-token", cursor])
        data = self.reader._call(("wiki", "+node-list"), args)
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("nodes"), list)
            or not isinstance(data.get("has_more"), bool)
        ):
            raise LarkError("lark_unavailable")
        cursor = data.get("page_token") if data["has_more"] else None
        if data["has_more"] and (not isinstance(cursor, str) or not cursor):
            raise LarkError("lark_unavailable")
        return data["nodes"], cursor

    def walk(self, root, heartbeat):
        """Yield None for an unsupported page. A failed listing leaves the previous inventory intact."""
        if root.kind in ("docs", "docx"):
            yield Page(root, None)
            return
        if root.kind == "wiki":
            first = self.node(root.token)
            space, pending = first.get("space_id"), deque([first])
            parents = deque()
        else:
            space, pending, parents = root.token, deque(), deque([None])
        seen = set()
        while pending or parents:
            if not pending:
                parent = parents.popleft()
                cursor, cursors = None, set()
                while True:
                    heartbeat()
                    try:
                        nodes, cursor = self.listing(space, parent, cursor)
                    except LarkError as exc:
                        if parent is None or exc.code in {"rate_limited", "timeout"}:
                            raise
                        yield UnlistedBranch(exc.code)
                        break
                    for node in nodes:
                        if (
                            not isinstance(node, dict)
                            or node.get("space_id") != space
                            or (node.get("parent_node_token") or None) != parent
                        ):
                            raise LarkError("lark_unavailable")
                    pending.extend(nodes)
                    if cursor is None:
                        break
                    if cursor in cursors:
                        raise LarkError("lark_unavailable")
                    cursors.add(cursor)
                continue
            node = pending.popleft()
            node_id = _token(node.get("node_token"))
            if node_id in seen:
                continue
            seen.add(node_id)
            if node.get("node_type") != "origin":
                yield None
                continue
            if node.get("has_child") is True:
                parents.append(node_id)
            elif node.get("has_child") is not False:
                raise LarkError("lark_unavailable")
            kind = {"doc": "docs", "docx": "docx"}.get(node.get("obj_type"))
            if not kind:
                yield None
                continue
            token = _token(node.get("obj_token"))
            host = urlsplit(root.url).hostname
            yield Page(
                DocumentLink(kind, token, f"https://{host}/{kind}/{token}"),
                node_id,
                node.get("title") or "",
            )

    def current_page(self, root, row):
        """Prove current membership all the way to the selected wiki root."""
        if not row["node"]:
            if root.kind != row["doc_kind"] or root.token != row["doc"]:
                raise LarkError("not_found")
            return Page(root, None)
        node = self.node(row["node"])
        if (
            node.get("obj_token") != row["doc"]
            or node.get("node_type") != "origin"
            or node.get("obj_type")
            != {"docx": "docx", "docs": "doc"}.get(row["doc_kind"])
        ):
            raise LarkError("not_found")
        space, title = node.get("space_id"), node.get("title") or ""
        if root.kind == "space" and space == root.token:
            self.listing(space, size=1)
        elif root.kind == "wiki":
            seen = set()
            while node["node_token"] != root.token:
                if node["node_token"] in seen or not node.get("parent_node_token"):
                    raise LarkError("not_found")
                seen.add(node["node_token"])
                node = self.node(node["parent_node_token"])
                if node.get("space_id") != space or node.get("node_type") != "origin":
                    raise LarkError("not_found")
        else:
            raise LarkError("not_found")
        kind, token = row["doc_kind"], row["doc"]
        host = urlsplit(root.url).hostname
        return Page(
            DocumentLink(kind, token, f"https://{host}/{kind}/{token}"),
            row["node"],
            title,
        )

    def read(self, page):
        if page.link.kind == "docx":
            doc = self.reader.doc_markdown(page.link.url)
            body = doc["markdown"]
            title = page.title or doc["title"]
            revision = doc.get("provider_revision")
        else:
            raw = self.reader._invoke(
                (
                    "api",
                    "GET",
                    f"/open-apis/doc/v2/{_token(page.link.token)}/raw_content",
                ),
                [],
            )
            if not isinstance(raw, dict) or not isinstance(raw.get("content"), str):
                raise LarkError("lark_unavailable")
            meta = self.reader._invoke(
                ("api", "GET", f"/open-apis/doc/v2/meta/{_token(page.link.token)}"), []
            )
            if not isinstance(meta, dict) or meta.get("obj_type") != "doc":
                raise LarkError("lark_unavailable")
            if meta.get("delete_flag") != 0:
                raise LarkError("not_found")
            body, title, revision = (
                raw["content"],
                page.title or meta.get("title"),
                meta.get("edit_time"),
            )
        heading = body.split("\n", 1)[0].strip()
        return Content(
            title or heading.removeprefix("# ").strip()[:160] or "Lark Doc",
            body,
            str(revision or ""),
        )
