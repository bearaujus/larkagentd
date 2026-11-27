from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_core import store as store_fixture
from test_dashboard import change

from larkagentd.gateway.access import Access
from larkagentd.knowledge.attachment_sync import AttachmentSync
from larkagentd.knowledge.attachments import attachment_link, attachments, passages
from larkagentd.knowledge.library import SourceLibrary
from larkagentd.knowledge.models import GroupBrainConfig
from larkagentd.knowledge.store import KnowledgeError
from larkagentd.knowledge.wiki import WikiReader
from larkagentd.toolservice.larkcli import LarkError
from larkagentd.toolservice.larkfiles import LarkFiles

store = store_fixture
HOST = "https://example.larksuite.com"
SPACE = HOST + "/wiki/settings/123#more"
ROOT = HOST + "/wiki/parentNode"
DOC = HOST + "/docx/parentDoc"
CLASSIC = HOST + "/docs/childDoc"
ROUTE = "knowledge/sources"


def node(token, document, kind, parent="", children=False):
    return dict(
        space_id="123",
        parent_node_token=parent,
        node_token=token,
        obj_token=document,
        obj_type=kind,
        node_type="origin",
        has_child=children,
        title=token,
    )


class Cli:
    app_id = "app"

    def __init__(self):
        self.nodes = {
            "parentNode": node("parentNode", "parentDoc", "docx", children=True),
            "sheetNode": node("sheetNode", "sheetToken", "sheet", "parentNode", True),
            "childNode": node("childNode", "childDoc", "doc", "sheetNode"),
            "elsewhere": node("elsewhere", "otherDoc", "docx"),
        }
        self.body = "## Office schedule\n\nFour office days with flexible weekdays."
        self.classic = "Holiday weeks\nOffice closures count as office days."
        self.timeout = False
        self.grant = "grp"
        self.on_read = None

    def _call(self, command, args):
        if command == ("wiki", "+node-get"):
            return self.nodes[args[1]].copy()
        assert command == ("wiki", "+node-list")
        parent = (
            args[args.index("--parent-node-token") + 1]
            if "--parent-node-token" in args
            else ""
        )
        candidates = [
            n.copy() for n in self.nodes.values() if n["parent_node_token"] == parent
        ]
        if self.timeout and parent == "sheetNode":
            raise LarkError("timeout")
        offset = (
            int(args[args.index("--page-token") + 1]) if "--page-token" in args else 0
        )
        end = offset + 1
        return {
            "nodes": candidates[offset:end],
            "has_more": end < len(candidates),
            "page_token": str(end),
        }

    def doc_markdown(self, url):
        if self.on_read:
            self.on_read()
        return {
            "title": "Office guide",
            "markdown": self.body if url == DOC else "Separate page",
            "provider_revision": "v1",
        }

    def _invoke(self, command, args):
        if command[0] == "drive":
            return {
                "items": [
                    {"member_type": "openchat", "member_id": self.grant, "perm": "view"}
                ]
            }
        if command[-1].endswith("/members/auth"):
            return {"auth_result": True}
        if command[-1].endswith("/public"):
            return {
                "permission_public": {
                    "external_access": False,
                    "link_share_entity": "closed",
                }
            }
        if command[-1].endswith("/doc/v2/meta/childDoc"):
            return {
                "obj_type": "doc",
                "title": "Holiday guide",
                "delete_flag": 0,
                "edit_time": 123,
            }
        assert command[-1].endswith("/doc/v2/childDoc/raw_content")
        return {"content": self.classic}


@pytest.fixture
def system(store):
    cfg = GroupBrainConfig(enabled=True)
    access = Access(store, ("admin@example.com",), group_brain=cfg)
    change(access, "POST", "groups", {"group_id": "oc_team"})
    cli = Cli()
    reader = WikiReader(cli)
    lib = SourceLibrary(store, wiki=reader)
    sync = AttachmentSync(store, reader, cfg)

    def search(query="office", chat="grp"):
        ctx = SimpleNamespace(
            chat_type="group",
            chat_id=chat,
            owner="tenant:user",
            lark_allowed=frozenset(),
            files=LarkFiles(cli, store, None, gateway_app="app"),
        )
        return lib.search("group:" + chat, query, cfg, ctx=ctx)["results"]

    return access, cli, sync, search


def attach(access, url, name="Guide", route=ROUTE):
    kind = "lark_wiki" if "/wiki/" in url else "lark_doc"
    return change(access, "POST", route, {"name": name, "kind": kind, "url": url})[1][
        "id"
    ]


def refresh(access, identifier):
    return change(access, "PATCH", ROUTE, {"id": identifier, "refresh": True})


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/wiki/parentNode",
        HOST + "/sheets/sheetToken",
        HOST + "/wiki/settings",
        HOST + "/wiki/settings/not-space",
        HOST + "/docs/childDoc/extra",
        "https://user@example.larksuite.com/wiki/parentNode",
    ],
)
def test_invalid_attachment_urls(url):
    with pytest.raises(KnowledgeError):
        attachment_link(url)


def test_wiki_branch_search_revision_scope_and_live_grants(system):
    access, cli, sync, search = system
    identifier = attach(
        access, ROOT, name="Handbook: Office & Holidays (Tim São Paulo)"
    )
    assert sync.run_one()
    assert not sync.run_one()
    item = change(access, "GET", ROUTE)[1]["sources"][0]
    assert (item["status"], item["indexed"], item["skipped"]) == ("ready", 2, 1)
    assert item["last_success"] and item["url"] == ROOT
    found = search("holiday")
    assert found[0]["url"] == HOST + "/wiki/childNode"
    assert "closures" in found[0]["text"]
    assert found[0]["scope"] == "workspace"
    assert search("holiday", "oc_team") == []
    cli.grant = "oc_team"
    assert search("holiday") == []
    assert search("holiday", "oc_team")
    cli.grant = "grp"
    cli.classic = "Holiday policy has changed."
    assert not search("holiday")
    refresh(access, identifier)
    assert sync.run_one()
    assert "changed" in search("holiday")[0]["text"]
    cli.nodes["childNode"]["parent_node_token"] = "elsewhere"
    assert not search("holiday")
    refresh(access, identifier)
    assert sync.run_one()
    assert change(access, "GET", ROUTE)[1]["sources"][0]["indexed"] == 1


def test_space_pagination_overlap_direct_doc_and_detach(system, store):
    access, _, sync, search = system
    space = attach(access, SPACE)
    single = attach(access, DOC, "Single")
    assert sync.run_one() and sync.run_one()
    listing = change(access, "GET", ROUTE)[1]["sources"]
    whole = next(x for x in listing if x["id"] == space)
    assert whole["indexed"] == 3 and whole["url"] == SPACE.split("#")[0]
    office = [r for r in search("flexible") if "flexible" in r["text"]]
    assert len(office) == 1
    change(access, "DELETE", ROUTE, {"id": space})
    assert search("flexible")[0]["url"] == DOC
    assert not search("holiday")
    change(access, "DELETE", ROUTE, {"id": single})
    assert not search("flexible")
    with store.engine.begin() as conn:
        assert conn.scalar(select(func.count()).select_from(passages)) == 0


def test_incomplete_inventory_and_worker_racing_detach(system, store):
    access, cli, sync, search = system
    identifier = attach(access, ROOT)
    assert sync.run_one()
    cli.timeout = True
    refresh(access, identifier)
    assert sync.run_one()
    item = change(access, "GET", ROUTE)[1]["sources"][0]
    assert item["status"] == "partial" and item["indexed"] == 2
    assert search("holiday")
    cli.timeout = False
    refresh(access, identifier)
    cli.on_read = lambda: change(access, "DELETE", ROUTE, {"id": identifier})
    assert sync.run_one()
    with store.engine.begin() as conn:
        assert not conn.execute(select(passages)).first()
        assert not conn.execute(select(attachments)).first()


def test_group_attachment_and_replacement_with_classic_doc(system):
    access, cli, sync, search = system
    group_route = "groups/oc_team/knowledge/sources"
    cli.grant = "oc_team"
    source = attach(access, ROOT, route=group_route)
    assert sync.run_one()
    assert search("flexible", "oc_team")[0]["scope"] == "group"
    cli.grant = "grp"
    assert not search("flexible", "grp")
    cli.grant = "oc_team"
    change(
        access,
        "PATCH",
        group_route,
        {"id": source, "name": "Guide", "kind": "lark_doc", "url": CLASSIC},
    )
    assert not search("flexible", "oc_team")
    assert sync.run_one()
    assert search("holiday", "oc_team")[0]["url"] == CLASSIC
    cli.grant = "grp"
    assert not search("holiday")


def test_detached_during_live_revision_check(system):
    access, cli, sync, search = system
    identifier = attach(access, DOC)
    assert sync.run_one()
    cli.on_read = lambda: change(access, "DELETE", ROUTE, {"id": identifier})
    assert not search("flexible")


def test_automatic_refresh_and_expired_claim(store, system):
    access, cli, sync, search = system
    attach(access, ROOT)
    assert sync.run_one()
    with store.engine.begin() as conn:
        row = conn.execute(select(attachments)).mappings().one()
        assert 899 <= row["due"] - row["last_success"] <= 901
        conn.execute(
            attachments.update().values(due=0, lease="expired", status="syncing")
        )
    cli.classic = "Holiday rescheduled."
    assert sync.run_one()
    assert "rescheduled" in search("holiday")[0]["text"]


def test_tenant_visibility_for_attachment_checks_the_receiving_group(system):
    access, cli, sync, search = system
    attach(access, DOC)
    assert sync.run_one()
    cli.grant = "oc_other"
    original = cli._invoke

    def org_sharing(command, args):
        if command[-1].endswith("/public"):
            return {
                "permission_public": {
                    "external_access": False,
                    "link_share_entity": "tenant_readable",
                }
            }
        if "/im/v1/chats/" in command[-1]:
            return {
                "external": not command[-1].endswith("/grp"),
                "tenant_key": "tenant",
            }
        return original(command, args)

    cli._invoke = org_sharing
    assert search("flexible", "grp")
    assert not search("flexible", "oc_team")


def test_wiki_page_grant_can_authorize_the_current_group(system):
    access, cli, sync, search = system
    attach(access, ROOT)
    assert sync.run_one()
    original = cli._invoke

    def wiki_grant(command, args):
        if command[0] == "drive" and "--perm-type" not in args:
            return {"items": []}
        if command[0] == "drive":
            assert args[args.index("--type") + 1] == "wiki"
            assert args[args.index("--perm-type") + 1] == "single_page"
        return original(command, args)

    cli._invoke = wiki_grant
    assert search("flexible")[0]["url"] == ROOT
    assert not search("flexible", "oc_team")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (DOC, "Actual document title"),
        (CLASSIC, "Holiday guide"),
        (SPACE, "Demo space"),
        (ROOT, "parentNode"),
    ],
)
def test_unnamed_attachment_uses_root_metadata_and_can_revert_a_custom_name(
    system, url, expected
):
    access, cli, sync, _ = system
    current = expected
    invoke = cli._invoke

    def metadata(command, args):
        if command[-1].endswith("/docx/v1/documents/parentDoc"):
            return {"document": {"title": current}}
        if command[-1].endswith("/wiki/v2/spaces/123"):
            return {"space": {"name": current}}
        return invoke(command, args)

    cli._invoke = metadata
    kind = "lark_wiki" if "/wiki/" in url else "lark_doc"
    ident = change(access, "POST", ROUTE, {"kind": kind, "url": url})[1]["id"]

    def item():
        return change(access, "GET", ROUTE)[1]["sources"][0]

    assert item()["custom_name"] is None
    assert sync.run_one() and item()["name"] == expected
    update = {"kind": kind, "url": url, "id": ident}
    change(access, "PATCH", ROUTE, {**update, "name": "My team guide"})
    refresh(access, ident)
    assert sync.run_one() and item()["name"] == "My team guide"
    change(access, "PATCH", ROUTE, {**update, "name": "   "})
    assert sync.run_one() and item()["name"] == expected
    if url == DOC:
        current = "Renamed in Lark"
        refresh(access, ident)
        assert sync.run_one() and item()["name"] == current


def test_two_automatic_sources_with_same_title_keep_separate_ids(system):
    access, cli, sync, _ = system
    cli.nodes["parentNode"]["title"] = cli.nodes["childNode"]["title"] = "Shared title"
    ids = {
        attach(access, ROOT, name=""),
        attach(access, HOST + "/wiki/childNode", name=None),
    }
    assert sync.run_one() and sync.run_one()
    result = change(access, "GET", ROUTE)[1]["sources"]
    assert {r["name"] for r in result} == {"Shared title"}
    assert {r["id"] for r in result} == ids
