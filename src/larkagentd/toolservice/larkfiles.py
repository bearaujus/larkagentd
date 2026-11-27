"""Native Lark operations with verified group visibility and bot permissions."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET

from ..core import group_actions
from ..lark.links import TOKEN, parse_link
from .historyfiles import verify_history_file
from .larkcli import LarkError, _column_label, _column_number

DRIVE_TYPES = {
    "docx": "docx",
    "docs": "doc",
    "sheets": "sheet",
    "slides": "slides",
    "wiki": "wiki",
    "base": "bitable",
}
EDIT_ROLES = frozenset(("edit", "full_access"))
VIEW_ROLES = EDIT_ROLES | {"view"}
# Lark refuses the collaborator list when the caller cannot share the file.
NO_SHARE_PERMISSION = 1063004
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")
SHEET_RANGE = re.compile(
    r"(?P<col>[A-Z]{1,3})(?P<first>[1-9][0-9]{0,4}):(?P=col)(?P<last>[1-9][0-9]{0,4})"
)
CELL = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,4})")
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


def _cell_rectangle(start, rows):
    """The value API requires the full rectangle even when writing one cell."""
    match = CELL.fullmatch(start)
    if not match:
        raise LarkError("bad_arguments")
    end = _column_label(_column_number(match[1]) + len(rows[0]) - 1)
    row = int(match[2]) + len(rows) - 1
    if len(end) > 3 or row > 99999:
        raise LarkError("bad_arguments")
    return f"{start}:{end}{row}"


def _matches_cells(data, expected):
    if not isinstance(data, list) or len(data) > len(expected):
        return False
    for index, row in enumerate(expected):
        actual = data[index] if index < len(data) else []
        if not isinstance(actual, list) or len(actual) > len(row):
            return False
        for col, value in enumerate(row):
            got = actual[col] if col < len(actual) else None
            if value in (None, "") and got in (None, ""):
                continue
            if value != got or isinstance(value, bool) != isinstance(got, bool):
                return False
    return True


def _paragraphs(root):
    return {
        " ".join("".join(node.itertext()).split())
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1] == "p"
    }


def _slide_chart(data, ns, spec):
    if not isinstance(spec, dict) or set(spec) not in (
        {"categories", "series"},
        {"type", "categories", "series"},
    ):
        raise LarkError("bad_slide")
    kind = spec.get("type", "grouped_column")
    if kind not in ("grouped_column", "percent_stacked_bar"):
        raise LarkError("bad_slide")
    stacked = kind == "percent_stacked_bar"
    categories, series = spec["categories"], spec["series"]
    if (
        not isinstance(categories, list)
        or not (1 if stacked else 2) <= len(categories) <= 8
        or not isinstance(series, list)
        or not (2 if stacked else 1) <= len(series) <= (12 if stacked else 2)
    ):
        raise LarkError("bad_slide")
    labels = [_text(category, 24) for category in categories]
    if any(re.search(r"[,\r\n]", label) for label in labels):
        raise LarkError("bad_slide")
    names = set()
    totals = [0] * len(labels)
    chart = ET.SubElement(
        data,
        ns + "chart",
        {"width": "816", "height": "340", "topLeftX": "72", "topLeftY": "165"},
    )
    area = ET.SubElement(chart, ns + "chartPlotArea")
    plot = ET.SubElement(
        area, ns + "chartPlot", {"type": "bar" if stacked else "column"}
    )
    if stacked:
        extra = ET.SubElement(plot, ns + "chartExtra")
        ET.SubElement(extra, ns + "chartStack", {"percentage": "true"})
    axes = ET.SubElement(area, ns + "chartAxes")
    x = ET.SubElement(axes, ns + "chartAxis", {"type": "x"})
    ET.SubElement(x, ns + "chartLabel", {"fontSize": "12"})
    y = ET.SubElement(axes, ns + "chartAxis", {"type": "y", "position": "left"})
    ET.SubElement(
        x if stacked else y, ns + "chartGridLine", {"color": "rgb(226, 232, 240)"}
    )
    ET.SubElement(y, ns + "chartLabel", {"fontSize": "12"})
    ET.SubElement(
        chart,
        ns + "chartLegend",
        {"position": "bottom", "fontSize": "12" if stacked else "14"},
    )
    chart_data = ET.SubElement(chart, ns + "chartData")
    dimensions = ET.SubElement(chart_data, ns + "dim1")
    ET.SubElement(dimensions, ns + "chartField", {"name": "Category"}).text = ",".join(
        labels
    )
    measures = ET.SubElement(chart_data, ns + "dim2")
    for item in series:
        if not isinstance(item, dict) or set(item) != {"name", "values"}:
            raise LarkError("bad_slide")
        name = _text(item["name"], 40)
        values = item["values"]
        if (
            not isinstance(values, list)
            or len(values) != len(labels)
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not -(10**15) <= value <= 10**15
                or stacked
                and value < 0
                for value in values
            )
            or stacked
            and (name in names or len(set(labels)) != len(labels))
        ):
            raise LarkError("bad_slide")
        names.add(name)
        totals = [total + value for total, value in zip(totals, values)]
        ET.SubElement(measures, ns + "chartField", {"name": name}).text = ",".join(
            map(str, values)
        )
    if stacked and any(total <= 0 for total in totals):
        raise LarkError("bad_slide")


def _slide_xml(page):
    """Generate an editable slide from a text outline or chart data."""
    if not isinstance(page, dict) or set(page) not in (
        {"title", "bullets"},
        {"title", "chart"},
    ):
        raise LarkError("bad_slide")
    title = _text(page["title"], 72)
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
    if "chart" in page:
        _slide_chart(data, ns, page["chart"])
        return ET.tostring(root, encoding="unicode")
    bullets = page["bullets"]
    if not isinstance(bullets, list) or not 1 <= len(bullets) <= 5:
        raise LarkError("bad_slide")
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

    def members(self, link, *, single_page=False):
        data = self._cli(
            ("drive", "+member-list"),
            "--token",
            link.token,
            "--type",
            DRIVE_TYPES[link.kind],
            *(("--perm-type", "single_page") if single_page else ()),
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
        allowed = result.get("auth_result")
        if not isinstance(allowed, bool):
            raise LarkError("lark_permission_unverified")
        return allowed

    def tenant_readable(self, ctx, link, *, allow_thread=False, attachment=False):
        """Verify an organization-only link can be read by everyone in this internal chat."""
        if (
            link.kind == "wiki"
            or not self.gateway_app
            or self.reader.app_id != self.gateway_app
            or (
                not attachment
                and link.token not in ctx.lark_allowed
                and not (allow_thread and link.token in ctx.lark_thread_allowed)
            )
        ):
            return False
        tenant, _, _ = ctx.owner.partition(":")
        if not tenant:
            return False
        settings = self._api(
            "GET",
            f"drive/v1/permissions/{link.token}/public",
            {"type": DRIVE_TYPES[link.kind]},
        ).get("permission_public")
        if not isinstance(settings, dict):
            raise LarkError("lark_permission_unverified")
        if settings.get("external_access") is not False or settings.get(
            "link_share_entity"
        ) not in ("tenant_readable", "tenant_editable"):
            return False
        chat = self._api("GET", f"im/v1/chats/{_id(ctx.chat_id)}", {})
        return chat.get("external") is False and chat.get("tenant_key") == tenant

    def require(
        self, ctx, link, write=False, *, allow_bot_edit=False, attachment=False
    ):
        """Check live access and return whether the edit must use the bot's grant."""
        access = "edit" if write else "view"
        if ctx.chat_type != "group" or not ctx.chat_id:
            raise LarkError("not_authorized", stage="group_context", access=access)
        roles = EDIT_ROLES if write else VIEW_ROLES
        try:
            bot_allowed = self.bot_permission(link, access)
        except LarkError as exc:
            raise exc.during("bot_access", access) from None
        if not bot_allowed:
            raise LarkError(
                "lark_bot_edit_denied" if write else "lark_bot_view_denied",
                stage="bot_access",
                access=access,
            )
        unverified = "lark_edit_unverified" if write else "lark_permission_unverified"
        group_failure = None
        try:
            if any(
                item.get("member_type") == "openchat"
                and item.get("member_id") == ctx.chat_id
                and item.get("perm") in roles
                for item in self.members(
                    link, single_page=attachment and link.kind == "wiki"
                )
            ):
                return False
        except LarkError as exc:
            if str(exc) not in {"forbidden", "permission_unverified"}:
                raise exc.during("collaborator_list", access) from None
            group_failure = exc.during("collaborator_list", access, code=unverified)
        bot_edit = (
            write
            and allow_bot_edit
            and link.kind == "docx"
            and group_failure is not None
            and group_failure.provider_code == NO_SHARE_PERMISSION
        )
        if not write or bot_edit:
            try:
                if self.tenant_readable(
                    ctx,
                    link,
                    allow_thread=bot_edit,
                    attachment=attachment and not write,
                ):
                    return bot_edit
            except LarkError as exc:
                if str(exc) != "forbidden":
                    raise exc.during("organization_visibility", access) from None
                if group_failure is None:
                    group_failure = exc.during(
                        "organization_visibility", access, code=unverified
                    )
        if group_failure is not None:
            raise group_failure from None
        raise LarkError(
            "lark_write_permission" if write else "lark_share_required",
            stage="group_grant",
            access=access,
        )

    @staticmethod
    def shared_link(ctx, value, *, allow_thread=False, allow_history=False):
        link = parse_link(value)
        if not link or (
            link.token not in ctx.lark_allowed
            and not (allow_thread and link.token in ctx.lark_thread_allowed)
            and not (allow_history and verify_history_file(ctx, link))
        ):
            raise LarkError("link_not_shared", stage="link_provenance")
        return link

    def read(self, ctx, link, sheet_id=None, range_=None):
        self.require(ctx, link)
        if link.kind == "slides":
            return self._cli(("slides", "+xml-get"), "--presentation", link.token)
        if link.kind == "sheets":
            if range_:
                return self.reader.sheet(link.url, range=range_, tab=sheet_id)
            info = self.read_workbook(link)
            if sheet_id is not None or len(info.get("sheets") or ()) == 1:
                return {
                    **info,
                    "preview": self.reader.sheet(link.url, workbook=info, tab=sheet_id),
                }
            return info
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
            or not self.bot_permission(link, "manage_public")
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
            try:
                entries = _json(content)
                if not isinstance(entries, list) or not 1 <= len(entries) <= 6:
                    raise LarkError("bad_slide")
                slides = [_slide_xml(entry) for entry in entries]
            except LarkError as exc:
                raise exc.during("slide_validation", code="bad_slide") from None
        else:
            raise LarkError("unsupported_file")

        receipt, previous = group_actions.reserve(
            self.store, self.vault, ctx.turn, "create", args
        )
        if not receipt:
            if previous and previous.get("verified"):
                return previous
            raise LarkError("write_outcome_unknown")

        try:
            response = self._cli(command, "--title=" + title, *flags)
        except LarkError as exc:
            code = (
                "lark_slides_create_scope"
                if kind == "slides" and exc.provider_code == 99991672
                else None
            )
            raise exc.during("create_request", code=code) from None
        data = response.get("document") or response.get("spreadsheet") or response
        token = (
            data.get("document_id")
            or data.get("spreadsheet_token")
            or data.get("xml_presentation_id")
        )
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise LarkError("artifact_unverified", stage="create_response")
        group_actions.checkpoint(
            self.store, self.vault, receipt, {"kind": kind, "token": token}
        )
        stage = "create_metadata"
        try:
            link, _ = self.metadata(token, kind)
            stage = "create_content"
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
                    {"kind": kind, "token": token, "slides": slide_ids},
                )
            stage = "create_readback"
            if kind == "docx":
                if not self.doc_text(link).strip():
                    raise LarkError("artifact_unverified")
            elif kind == "sheets":
                self.verify_cells(link, tab, "A1", content)
            elif kind == "slides":
                self.verify_slides(link, dict(zip(slide_ids, slides)))
            stage = "create_publish"
            result = self.publish(ctx, link)
        except LarkError as exc:
            raise exc.during(stage, code="artifact_incomplete") from None
        group_actions.checkpoint(self.store, self.vault, receipt, result)
        return result

    def read_workbook(self, link):
        return self._cli(
            ("sheets", "+workbook-info"), "--spreadsheet-token", link.token
        )

    def set_cells(self, link, sheet_id, start, content):
        # PUT values on the native Sheets surface. The first cell and matrix are validated.
        rows = _json(content)
        target = f"{sheet_id}!{_cell_rectangle(start, rows)}"
        return self._api(
            "PUT",
            f"sheets/v2/spreadsheets/{link.token}/values",
            {},
            {
                "valueRange": {"range": target, "values": rows},
            },
        )

    def verify_cells(self, link, sheet_id, start, content):
        rows = _json(content)
        target = f"{sheet_id}!{_cell_rectangle(start, rows)}"
        values = self._api(
            "GET",
            f"sheets/v2/spreadsheets/{link.token}/values/{target}",
            {},
        ).get("valueRange")
        if not isinstance(values, dict) or not _matches_cells(
            values.get("values", []), rows
        ):
            raise LarkError("write_outcome_unknown")

    def doc_text(self, link):
        value = self._api("GET", f"docx/v1/documents/{link.token}/raw_content", {}).get(
            "content"
        )
        if not isinstance(value, str):
            raise LarkError("lark_unavailable")
        return value

    def verify_slides(self, link, expected):
        response = self._cli(("slides", "+xml-get"), "--presentation", link.token)
        presentation = response.get("xml_presentation")
        content = (
            presentation.get("content")
            if isinstance(presentation, dict)
            else presentation
        )
        try:
            root = ET.fromstring(content)
        except (ET.ParseError, TypeError, ValueError):
            raise LarkError("write_outcome_unknown") from None
        pages = {
            node.get("id"): node
            for node in root.iter()
            if node.tag.rsplit("}", 1)[-1] == "slide"
        }
        for page_id, xml in expected.items():
            page = pages.get(page_id)
            requested = ET.fromstring(xml)
            if page is None or not _paragraphs(requested) <= _paragraphs(page):
                raise LarkError("write_outcome_unknown")
            if requested.find(f".//{{{SML}}}chart") is not None and not any(
                node.tag.rsplit("}", 1)[-1] == "chart" for node in page.iter()
            ):
                raise LarkError("write_outcome_unknown")

    def edit(self, ctx, link, args, *, confirmed=False):
        bot_edit = self.require(ctx, link, write=True, allow_bot_edit=True)
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
            if not CELL.fullmatch(start):
                raise LarkError("bad_arguments")
            content = _cells(content)
            command = None
        elif link.kind == "slides" and action in ("append", "replace"):
            command = (
                "slides",
                "+add-slide" if action == "append" else "+update-slide",
            )
            xml = _slide_xml(_json(content))
            flags = [
                "--presentation",
                link.token,
            ]
            if action == "replace":
                slide_id = _id(args.get("slide_id"))
                flags += ["--slide-id", slide_id, "--content", xml]
            else:
                flags += ["--slide", xml]
        else:
            raise LarkError("unsupported_edit")
        access = {"access_basis": "bot_edit"} if bot_edit else {}
        if not confirmed and (bot_edit or link.token not in ctx.lark_allowed):
            via = " using the bot's Edit access" if bot_edit else ""
            label = f"Apply the requested {action} to this Lark file{via}?\n{link.url}"
            if not ctx.request_action("file_edit", {**args, "url": link.url}, label):
                raise LarkError("confirmation_already_pending")
            return {"confirmation_requested": True, **access}
        before = None
        if link.kind == "docx":
            try:
                before = self.doc_text(link)
            except LarkError as exc:
                raise exc.during("edit_preflight", "edit") from None
        receipt, previous = group_actions.reserve(
            self.store, self.vault, ctx.turn, "edit", args
        )
        if not receipt:
            if previous and previous.get("verified"):
                return previous
            raise LarkError("write_outcome_unknown")
        try:
            if command:
                response = self._cli(command, *flags)
                if response.get("warnings"):
                    raise LarkError("write_outcome_unknown")
            else:
                self.set_cells(link, tab, start, content)
        except LarkError as exc:
            raise exc.during("write_request", "edit") from None
        try:
            self.require(ctx, link)
            if link.kind == "docx":
                if self.doc_text(link) == before:
                    raise LarkError("write_no_change")
            elif link.kind == "sheets":
                self.verify_cells(link, tab, start, content)
            else:
                if action == "append":
                    slide_id = _id(response.get("slide_id"))
                self.verify_slides(link, {slide_id: xml})
        except LarkError as exc:
            raise exc.during(
                "write_readback",
                "edit",
                code="write_no_change"
                if str(exc) == "write_no_change"
                else "write_outcome_unknown",
            ) from None
        result = {
            "url": link.url,
            "kind": link.kind,
            "updated": True,
            "verified": True,
            **access,
        }
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
