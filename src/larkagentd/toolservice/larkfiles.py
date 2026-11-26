"""Native Lark operations with an explicit group share and bot permissions."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET

from ..core import group_actions
from ..lark.links import TOKEN, parse_link
from .larkcli import LarkError

DRIVE_TYPES = {
    "docx": "docx",
    "sheets": "sheet",
    "slides": "slides",
    "wiki": "wiki",
    "base": "bitable",
}
EDIT_ROLES = frozenset(("edit", "full_access"))
VIEW_ROLES = EDIT_ROLES | {"view"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")
SHEET_RANGE = re.compile(
    r"(?P<col>[A-Z]{1,3})(?P<first>[1-9][0-9]{0,4}):(?P=col)(?P<last>[1-9][0-9]{0,4})"
)
SML = "https://www.larkoffice.com/sml/2.0"
ET.register_namespace("", SML)


def _text(value, limit):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or "\x00" in value
    ):
        raise LarkError("bad_arguments")
    return value.strip()


def _markdown(value):
    text = _text(value, 28_000)
    if (
        text.startswith(("@", "-\n"))
        or text == "-"
        or re.search(
            r"(?i)<(?:img|audio|video|iframe|embed|object|script|include)\b", text
        )
    ):
        raise LarkError("bad_arguments")
    return text


def _id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise LarkError("bad_arguments")
    return value


def _json(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError, RecursionError):
        raise LarkError("bad_arguments") from None


def _cells(raw):
    rows = _json(raw)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
        raise LarkError("bad_arguments")
    columns = len(rows[0]) if isinstance(rows[0], list) else 0
    if not 1 <= columns <= 26:
        raise LarkError("bad_arguments")
    if any(
        not isinstance(row, list)
        or len(row) != columns
        or any(
            not isinstance(cell, (str, int, float, bool, type(None)))
            or isinstance(cell, str)
            and (len(cell) > 2000 or cell.startswith("="))
            for cell in row
        )
        for row in rows
    ):
        raise LarkError("bad_arguments")
    try:
        return json.dumps(rows, allow_nan=False)
    except ValueError:
        raise LarkError("bad_arguments") from None


def _slide_xml(page):
    """Generate one editable 16:9 text slide with fixed, readable bounds."""
    if not isinstance(page, dict) or set(page) != {"title", "bullets"}:
        raise LarkError("bad_slide")
    title = _text(page["title"], 72)
    bullets = page["bullets"]
    if not isinstance(bullets, list) or not 1 <= len(bullets) <= 5:
        raise LarkError("bad_slide")
    ns = f"{{{SML}}}"
    root = ET.Element(ns + "slide")
    data = ET.SubElement(root, ns + "data")
    header = ET.SubElement(
        data,
        ns + "shape",
        {
            "type": "text",
            "topLeftX": "72",
            "topLeftY": "45",
            "width": "816",
            "height": "120",
        },
    )
    title_content = ET.SubElement(
        header,
        ns + "content",
        {
            "textType": "title",
            "fontSize": "38",
            "color": "rgb(25,45,78)",
        },
    )
    ET.SubElement(title_content, ns + "p").text = title
    body = ET.SubElement(
        data,
        ns + "shape",
        {
            "type": "text",
            "topLeftX": "72",
            "topLeftY": "185",
            "width": "816",
            "height": "300",
        },
    )
    content = ET.SubElement(
        body,
        ns + "content",
        {
            "textType": "body",
            "fontSize": "22",
            "color": "rgb(36,48,68)",
        },
    )
    points = ET.SubElement(content, ns + "ul")
    for bullet in bullets:
        item = ET.SubElement(points, ns + "li")
        ET.SubElement(item, ns + "p").text = _text(bullet, 100)
    return ET.tostring(root, encoding="unicode")


class LarkFiles:
    def __init__(self, reader, store, vault, gateway_app=None):
        self.reader = reader
        self.store = store
        self.vault = vault
        self.gateway_app = gateway_app

    def _cli(self, command, *arguments):
        # Command and flags are chosen below. Model input is always data or a validated ID.
        result = self.reader._invoke(command, arguments)
        if not isinstance(result, dict):
            raise LarkError("lark_unavailable")
        return result

    def _api(self, method, path, params, body=None):
        args = ["--params", json.dumps(params)]
        if body is not None:
            args += ["--data", json.dumps(body)]
        return self._cli(("api", method, "/open-apis/" + path), *args)

    def members(self, link):
        data = self._cli(
            ("drive", "+member-list"),
            "--token",
            link.token,
            "--type",
            DRIVE_TYPES[link.kind],
        )
        items = data.get("items")
        if not isinstance(items, list):
            raise LarkError("permission_unverified")
        return [item for item in items if isinstance(item, dict)]

    def bot_permission(self, link, action):
        result = self._api(
            "GET",
            f"drive/v1/permissions/{link.token}/members/auth",
            {
                "type": DRIVE_TYPES[link.kind],
                "action": action,
            },
        )
        return result.get("auth_result") is True

    def require(self, ctx, link, write=False):
        if ctx.chat_type != "group" or not ctx.chat_id:
            raise LarkError("not_authorized")
        roles = EDIT_ROLES if write else VIEW_ROLES
        try:
            allowed = any(
                item.get("member_type") == "openchat"
                and item.get("member_id") == ctx.chat_id
                and item.get("perm") in roles
                for item in self.members(link)
            )
            if allowed and self.bot_permission(link, "edit" if write else "view"):
                return
        except LarkError:
            pass
        raise LarkError("lark_write_permission" if write else "lark_share_required")

    @staticmethod
    def shared_link(ctx, value):
        link = parse_link(value)
        if not link or link.token not in ctx.lark_allowed:
            raise LarkError("link_not_shared")
        return link

    def read(self, ctx, link, sheet_id=None, range_=None):
        self.require(ctx, link)
        if link.kind == "slides":
            return self._cli(("slides", "+xml-get"), "--presentation", link.token)
        if link.kind == "sheets":
            if range_:
                return self.reader.sheet(link.url, range=f"{_id(sheet_id)}!{range_}")
            return self._cli(
                ("sheets", "+workbook-info"), "--spreadsheet-token", link.token
            )
        if link.kind == "docx":
            return self._cli(
                ("docs", "+fetch"),
                "--doc",
                link.token,
                "--scope",
                "outline",
                "--detail",
                "with-ids",
            )
        raise LarkError("unsupported_file")

    def metadata(self, token, kind):
        data = self._api(
            "POST",
            "drive/v1/metas/batch_query",
            {"user_id_type": "open_id"},
            {
                "request_docs": [{"doc_token": token, "doc_type": DRIVE_TYPES[kind]}],
                "with_url": True,
            },
        )
        for item in data.get("metas") or ():
            if isinstance(item, dict) and item.get("doc_token") == token:
                link = parse_link(item.get("url"))
                if link and link.token == token and link.kind == kind:
                    return link, item
        raise LarkError("artifact_unverified")

    def publish(self, ctx, link):
        owner = ctx.owner.split(":", 1)[-1]
        _id(owner)
        _id(ctx.chat_id)
        self._cli(
            ("drive", "+member-add"),
            "--token",
            link.token,
            "--type",
            DRIVE_TYPES[link.kind],
            "--member-type",
            "openchat",
            "--member-id",
            ctx.chat_id,
            "--perm",
            "full_access",
            "--yes",
        )
        self._api(
            "POST",
            f"drive/v1/permissions/{link.token}/members/transfer_owner",
            {
                "type": DRIVE_TYPES[link.kind],
                "remove_old_owner": False,
                "old_owner_perm": "full_access",
                "need_notification": False,
                "stay_put": True,
            },
            {"member_type": "openid", "member_id": owner},
        )
        _, meta = self.metadata(link.token, link.kind)
        group_manage = any(
            item.get("member_type") == "openchat"
            and item.get("member_id") == ctx.chat_id
            and item.get("perm") == "full_access"
            for item in self.members(link)
        )
        if (
            meta.get("owner_id") != owner
            or not group_manage
            or not self.bot_permission(link, "manage")
        ):
            raise LarkError("artifact_permissions_unverified")
        return {
            "url": link.url,
            "kind": link.kind,
            "title": meta.get("title"),
            "verified": True,
        }

    def create(self, ctx, args):
        kind, title, content = args["kind"], _text(args["title"], 180), args["content"]
        if not self.gateway_app or self.reader.app_id != self.gateway_app:
            raise LarkError("artifact_bot_mismatch")
        if ctx.chat_type != "group" or not ctx.chat_id:
            raise LarkError("not_authorized")
        if kind == "docx":
            text = _markdown(content)
            command = ("docs", "+create")
            flags = ("--doc-format", "markdown", "--content=\n" + text)
            slides = []
        elif kind == "sheets":
            command = ("sheets", "+workbook-create")
            flags = ()
            content = _cells(content)
            slides = []
        elif kind == "slides":
            command = ("slides", "+create")
            flags = ()
            entries = _json(content)
            if not isinstance(entries, list) or not 1 <= len(entries) <= 6:
                raise LarkError("bad_arguments")
            slides = [_slide_xml(entry) for entry in entries]
        else:
            raise LarkError("unsupported_file")

        receipt, previous = group_actions.reserve(
            self.store, self.vault, ctx.turn, "create", args
        )
        if not receipt:
            if previous and previous.get("verified"):
                return previous
            raise LarkError("write_outcome_unknown")

        response = self._cli(command, "--title=" + title, *flags)
        data = response.get("document") or response.get("spreadsheet") or response
        token = (
            data.get("document_id")
            or data.get("spreadsheet_token")
            or data.get("xml_presentation_id")
        )
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise LarkError("artifact_unverified")
        group_actions.checkpoint(
            self.store, self.vault, receipt, {"kind": kind, "token": token}
        )
        link, _ = self.metadata(token, kind)
        if kind == "sheets":
            info = self.read_workbook(link)
            tabs = info.get("sheets") or []
            if len(tabs) != 1:
                raise LarkError("artifact_unverified")
            tab = _id(tabs[0].get("sheet_id"))
            self.set_cells(link, tab, "A1", content)
        slide_ids = []
        for slide in slides:
            result = self._cli(
                ("slides", "+add-slide"), "--presentation", token, "--slide", slide
            )
            slide_ids.append(_id(result.get("slide_id")))
            group_actions.checkpoint(
                self.store,
                self.vault,
                receipt,
                {
                    "kind": kind,
                    "token": token,
                    "slides": slide_ids,
                },
            )
        if kind in ("docx", "slides"):
            # Read back the artifact before making it available to the group.
            read_command = (
                ("docs", "+fetch") if kind == "docx" else ("slides", "+xml-get")
            )
            read_flags = (
                ("--doc", token, "--scope", "outline")
                if kind == "docx"
                else ("--presentation", token)
            )
            self._cli(read_command, *read_flags)
        result = self.publish(ctx, link)
        group_actions.checkpoint(self.store, self.vault, receipt, result)
        return result

    def read_workbook(self, link):
        return self._cli(
            ("sheets", "+workbook-info"), "--spreadsheet-token", link.token
        )

    def set_cells(self, link, sheet_id, start, content):
        # PUT values on the native Sheets surface. The first cell and matrix are validated.
        target = f"{sheet_id}!{start}"
        return self._api(
            "PUT",
            f"sheets/v2/spreadsheets/{link.token}/values",
            {},
            {
                "valueRange": {"range": target, "values": _json(content)},
            },
        )

    def edit(self, ctx, link, args):
        self.require(ctx, link, write=True)
        action = args["action"]
        content = args.get("content", "")
        if link.kind == "docx" and action in ("append", "replace"):
            flags = [
                "--doc",
                link.token,
                "--doc-format",
                "markdown",
                "--content=\n" + _markdown(content),
            ]
            if action == "replace":
                flags += ["--pattern=" + _text(args.get("match"), 500)]
            command = ("docs", "+update")
            flags += ["--command", "append" if action == "append" else "str_replace"]
        elif link.kind == "sheets" and action == "set":
            tab = _id(args.get("sheet_id"))
            start = args.get("range") or ""
            if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,4}", start):
                raise LarkError("bad_arguments")
            content = _cells(content)
            command = None
        elif link.kind == "slides" and action in ("append", "replace"):
            command = (
                "slides",
                "+add-slide" if action == "append" else "+update-slide",
            )
            flags = [
                "--presentation",
                link.token,
                "--slide",
                _slide_xml(_json(content)),
            ]
            if action == "replace":
                flags += ["--slide-id", _id(args.get("slide_id"))]
        else:
            raise LarkError("unsupported_edit")
        receipt, previous = group_actions.reserve(
            self.store, self.vault, ctx.turn, "edit", args
        )
        if not receipt:
            if previous and previous.get("verified"):
                return previous
            raise LarkError("write_outcome_unknown")
        if command:
            response = self._cli(command, *flags)
            if response.get("warnings"):
                raise LarkError("write_outcome_unknown")
        else:
            self.set_cells(link, tab, start, content)
        self.require(ctx, link)
        if link.kind in ("docx", "slides"):
            self.read(ctx, link)
        else:
            self.read_workbook(link)
        result = {"url": link.url, "kind": link.kind, "updated": True, "verified": True}
        group_actions.checkpoint(self.store, self.vault, receipt, result)
        return result

    def _revision(self, link):
        response = self._cli(
            ("sheets", "+revision-get"), "--spreadsheet-token", link.token
        )
        revision = response.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, (int, str)):
            raise LarkError("sheet_revision_unverified")
        return revision

    def _matching_rows(self, link, args):
        span = SHEET_RANGE.fullmatch(args.get("range") or "")
        tab = _id(args.get("sheet_id"))
        predicate = _text(args.get("match"), 250)
        if not span:
            raise LarkError("bad_arguments")
        first, last = int(span["first"]), int(span["last"])
        if last < first or last - first >= 500 or first < 2:
            raise LarkError("bad_arguments")
        revision = self._revision(link)
        cells = self._api(
            "GET",
            f"sheets/v2/spreadsheets/{link.token}/values/{tab}!{args['range']}",
            {},
        )
        records = (cells.get("valueRange") or {}).get("values")
        if not isinstance(records, list) or len(records) > last - first + 1:
            raise LarkError("sheet_range_unverified")
        if self._revision(link) != revision:
            raise LarkError("sheet_changed")
        ranges = [
            f"{first + offset}:{first + offset}"
            for offset, row in enumerate(records)
            if isinstance(row, list) and row and str(row[0]) == predicate
        ]
        fingerprint = hashlib.sha256(
            json.dumps([revision, records], sort_keys=True).encode()
        ).hexdigest()
        return ranges, fingerprint

    def request_delete(self, ctx, link, args):
        if link.kind != "sheets":
            raise LarkError("unsupported_file")
        self.require(ctx, link, write=True)
        ranges, fingerprint = self._matching_rows(link, args)
        if not ranges:
            return {"matched_rows": 0}
        if len(ranges) > 100:
            raise LarkError("too_many_rows")
        label = f"Delete {len(ranges)} matching rows in {args['range']} from this sheet? Sheet references may shift."
        data = {**args, "url": link.url, "fingerprint": fingerprint, "ranges": ranges}
        if not group_actions.action_request(
            self.store, self.vault, ctx.turn, "delete_rows", data, label
        ):
            raise LarkError("confirmation_already_pending")
        return {"confirmation_requested": True, "matched_rows": len(ranges)}

    def delete_rows(self, ctx, args):
        link = parse_link(args["url"])
        self.require(ctx, link, write=True)
        ranges, fingerprint = self._matching_rows(link, args)
        if ranges != args["ranges"] or fingerprint != args["fingerprint"]:
            raise LarkError("sheet_changed")
        self._cli(
            ("sheets", "+dim-delete"),
            "--spreadsheet-token",
            link.token,
            "--sheet-id",
            _id(args["sheet_id"]),
            "--ranges",
            json.dumps(ranges),
            "--yes",
        )
        outcome = self.read_workbook(link)
        self.require(ctx, link)
        if not outcome.get("sheets"):
            raise LarkError("write_outcome_unknown")
        return {"url": link.url, "deleted_rows": len(ranges)}
