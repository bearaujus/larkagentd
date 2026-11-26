import json
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select
from test_card_lifecycle import drain
from test_core import store as store_fixture
from test_dashboard import change
from test_group_chat import GROUP, ask, direct, room, texts

from larkagentd.core import group_actions
from larkagentd.core.scopes import DEFAULT_GROUP_TOOLS
from larkagentd.core.store import gateway_policies, jobs
from larkagentd.gateway.access import AccessError
from larkagentd.knowledge.library import SourceLibrary, mcp_settings
from larkagentd.knowledge.models import GroupBrainConfig
from larkagentd.lark.links import parse_link
from larkagentd.toolservice.larkcli import LarkError
from larkagentd.toolservice.larkfiles import LarkFiles, _slide_xml
from larkagentd.toolservice.registry import ToolRegistry
from larkagentd.toolservice.toolservice import ToolService

store = store_fixture
KEY = "synthetic-key-123456"
CHAT = "oc_synthetic"
OWNER = "t:u"
DOC = "https://example.larksuite.com/docx/ExampleDocumentToken"


def test_first_mention_is_answered_in_group_and_dm_cannot_start_model(store):
    svc, model, transport = room(store, "Pilot with thirty volunteers.")
    message = ask(svc, "u", "What is the pilot size?")
    drain(svc, transport)
    assert len(model.calls) == 1
    answer = next(p for p in transport.sent if p.get("text") == model.reply)
    assert answer["chat"] == GROUP
    assert answer["reply_to"] == message
    assert answer["thread"]
    direct(svc, transport, "u", "dm", "Repeat that query.")
    direct(svc, transport, "u", "dm", "/skills")
    assert len(model.calls) == 1
    assert model.reply not in texts(transport, "dm")


def test_two_groups_cannot_merge_requests_from_same_sender(store):
    svc, model, transport = room(store)
    with store.engine.begin() as conn:
        conn.execute(
            insert(gateway_policies).values(scope="group:grp2", mode="all_users")
        )
    ask(svc, "u", "This belongs to group one", thread="same")
    ask(svc, "u", "Private group two content", thread="same", chat="grp2")
    drain(svc, transport)
    assert len(model.calls) == 1
    assert "Private group two" not in json.dumps(model.requests[0])


def test_scoped_dashboard_reads_and_updates_only_its_registered_group(store):
    svc, _, _ = room(store)
    for group in (CHAT, "oc_other"):
        change(svc.access, "POST", "groups", {"group_id": group})
    with store.engine.begin() as conn:
        token, _ = svc.access.issue(conn, OWNER, f"group:{CHAT}")
        session, result = svc.access.exchange(conn, token)
        assert result["role"] == "group_admin"
        actor = svc.access.authenticate(conn, session)
        assert actor["group_id"] == CHAT
        path = ["groups", CHAT, "knowledge", "sources"]
        assert (
            svc.access.dispatch(
                conn,
                actor,
                "POST",
                path,
                {"name": "Pilot", "markdown": "Pilot begins Monday."},
            )[0]
            == 200
        )
        assert (
            svc.access.dispatch(conn, actor, "GET", path, {})[1]["sources"][0]["name"]
            == "Pilot"
        )
        for illegal in [
            ["governance"],
            ["groups"],
            ["knowledge", "sources"],
            ["groups", "oc_other", "knowledge", "sources"],
            ["groups", CHAT, "tools"],
        ]:
            with pytest.raises(AccessError, match="group_dashboard_scope"):
                svc.access.dispatch(conn, actor, "GET", illegal, {})
        with pytest.raises(AccessError, match="invalid_source"):
            svc.access.dispatch(
                conn,
                actor,
                "POST",
                path,
                {"name": "MCP", "server": "remote", "uri": "data:x"},
            )


def test_library_combines_own_group_and_workspace_with_mcp_sources(store):
    svc, _, _ = room(store)
    for group in (CHAT, "oc_other"):
        change(svc.access, "POST", "groups", {"group_id": group})
    change(
        svc.access,
        "POST",
        "knowledge/sources",
        {"name": "Policy", "markdown": "Project pilot checklist."},
    )
    connectors = mcp_settings(
        '{"policy":{"url":"https://knowledge.example.com/mcp"},'
        '"calendar":{"argv":["/opt/calendar","--stdio"]}}'
    )
    svc.access.knowledge_mcp = connectors
    change(
        svc.access,
        "POST",
        "knowledge/sources",
        {"name": "Policy MCP", "server": "policy", "uri": "info://pilot"},
    )
    change(
        svc.access,
        "POST",
        "knowledge/sources",
        {"name": "Calendar MCP", "server": "calendar", "uri": "info://schedule"},
    )
    for group, count in ((CHAT, 30), ("oc_other", 95)):
        change(
            svc.access,
            "POST",
            f"groups/{group}/knowledge/sources",
            {"name": "Brief", "markdown": f"The pilot uses {count} volunteers."},
        )

    async def read(entry, _uri):
        return "Pilot communications policy." if "url" in entry else "Pilot calendar."

    found = SourceLibrary(store, connectors, read=read).search(
        f"group:{CHAT}", "pilot", GroupBrainConfig(enabled=True)
    )
    assert {item["source"] for item in found["results"]} == {
        "Brief",
        "Policy",
        "Policy MCP",
        "Calendar MCP",
    }
    assert "30 volunteers" in str(found) and "95 volunteers" not in str(found)


def test_group_file_check_requires_group_edit_even_with_bot_edit():
    class Reader:
        app_id = "lark-app"

        def _invoke(self, command, _args):
            if command[0] == "drive":
                return {
                    "items": [
                        {"member_id": CHAT, "member_type": "openchat", "perm": "view"}
                    ]
                }
            return {"auth_result": True}

    service = LarkFiles(Reader(), None, None)
    link = parse_link(DOC)
    ctx = SimpleNamespace(chat_type="group", chat_id=CHAT, lark_allowed={link.token})
    assert service.shared_link(ctx, DOC) == link
    service.require(ctx, link)
    with pytest.raises(LarkError, match="lark_write_permission"):
        service.require(ctx, link, write=True)
    ctx.chat_id = "oc_other"
    with pytest.raises(LarkError, match="lark_share_required"):
        service.require(ctx, link)


def test_slide_outline_yields_native_editable_safe_xml():
    xml = _slide_xml({"title": "A & B < C", "bullets": ["Pilot outcomes"]})
    assert "&amp;" in xml and "&lt;" in xml and "<shape" in xml
    with pytest.raises(LarkError, match="bad_slide"):
        _slide_xml({"title": "A", "bullets": []})


def test_cards_bind_original_owner_chat_and_action_across_jobs(store):
    svc, model, transport = room(store)
    registry = ToolRegistry(store)
    tools = ToolService(
        store,
        svc,
        KEY,
        registry=registry,
        vault=svc.vault,
        web=SimpleNamespace(search=lambda query: {"answer": query}),
    )
    registry.sync(tools.tools)
    with store.engine.begin() as conn:
        store.set_scope_tools(conn, f"group:{GROUP}", DEFAULT_GROUP_TOOLS)
    results = []

    def call(session_id, name, args):
        return tools.handle(
            f"Bearer {KEY}",
            name,
            json.dumps({"session_id": session_id, "arguments": args}).encode(),
        )[1]

    model.hook = lambda session_id: results.append(
        call(session_id, "request_web_search", {"query": "public search example"})
    )
    origin = ask(svc, "u", "Can you research the topic?")
    drain(svc, transport)
    assert results[0]["result"]["confirmation_requested"]
    payload = next(p for p in transport.sent if p.get("preview_card"))
    assert payload["chat"] == GROUP and payload["reply_to"] == origin
    value = json.loads(payload["preview_card"])["elements"][1]["actions"][0]["value"]
    with store.engine.begin() as conn:
        assert not conn.scalar(select(jobs.c.id).where(jobs.c.kind == "prepare"))
    assert "no longer available" in svc.action("t", "other", GROUP, "1", value)
    assert "no longer available" in svc.action("t", "u", "another-group", "2", value)
    assert "Confirmed" in svc.action("t", "u", GROUP, "3", value)
    model.hook = lambda session_id: results.extend(
        [
            call(session_id, "run_confirmed_action", {}),
            call(session_id, "run_confirmed_action", {}),
        ]
    )
    drain(svc, transport)
    assert results[1]["result"]["answer"] == "public search example"
    assert results[2]["error"] == "confirmation_expired"
    assert "no longer available" in svc.action("t", "u", GROUP, "4", value)
    with store.engine.begin() as conn:
        assert conn.scalar(select(group_actions.actions.c.status)) == "consumed"


def test_learned_fact_stops_serving_when_its_original_message_changes(store):
    from dataclasses import replace
    from datetime import datetime, timezone

    from larkagentd.knowledge.lark_sources import SourceMessage
    from larkagentd.knowledge.learned import learn, references, verify
    from larkagentd.knowledge.runtime import GroupBrainRuntime

    svc, _, _ = room(store)
    change(svc.access, "POST", "groups", {"group_id": CHAT})
    scope = f"group:{CHAT}"
    message = SourceMessage(
        "om_syntheticsource",
        CHAT,
        "thread",
        None,
        None,
        "ou_example",
        "user",
        datetime.now(timezone.utc),
        "We agreed the pilot will use thirty volunteers.",
        "text",
        False,
        None,
    )
    rows = [message]
    runtime = GroupBrainRuntime(
        store.engine,
        GroupBrainConfig(enabled=True),
        source=SimpleNamespace(
            messages=lambda _group, ids: [m for m in rows if m.message_id in ids]
        ),
    )
    result = learn(
        runtime,
        scope,
        "ou_example",
        {"content": "The pilot uses thirty volunteers.", "category": "decision"},
        [message.message_id],
        runtime.effective(scope).generation,
    )
    assert result["state"] == "active"
    with store.engine.begin() as conn:
        source_rows = references(conn, scope, result["memory_id"], result["revision"])
    assert (
        verify(runtime, scope, [source_rows])[0][0]["source_id"] == message.message_id
    )
    from larkagentd.gateway.learned_page import learned_page
    from larkagentd.knowledge.lark_sources import SourceError

    svc.access.knowledge_runtime = runtime
    svc.access.knowledge_config = runtime.config
    with store.engine.begin() as conn:
        token, _ = svc.access.issue(conn, OWNER, scope)
        session, _ = svc.access.exchange(conn, token)
        actor = svc.access.authenticate(conn, session)

    def shown():
        page = learned_page(
            svc.access,
            session,
            ["groups", CHAT, "knowledge", "learned"],
            "",
            actor,
            None,
            lambda _: None,
        )
        return page["records"][0]

    assert shown()["sources"][0]["excerpt"] == message.text
    assert shown()["activation"] == "source_checked"

    def unavailable(*_):
        raise SourceError("timeout")

    healthy = runtime.source.messages
    runtime.source.messages = unavailable
    assert shown()["status"] == "temporarily_unavailable"
    assert shown()["content"] is None
    runtime.source.messages = healthy
    rows[0] = replace(
        message, text="We changed the pilot to sixty volunteers.", edited=True
    )
    assert verify(runtime, scope, [source_rows]) == [[]]
    assert shown()["status"] == "source_changed_or_unavailable"
    assert shown()["content"] is None
    assert shown()["sources"] == []
    rows[0] = replace(message, chat_id="oc_other")
    assert verify(runtime, scope, [source_rows]) == [[]]
    rows.clear()
    assert verify(runtime, scope, [source_rows]) == [[]]


@pytest.mark.parametrize(
    "kind,content",
    [
        ("docx", "# Pilot\nA quick update."),
        ("sheets", '[["Name","Status"],["Pilot","Ready"]]'),
        ("slides", '[{"title":"Pilot","bullets":["Ready to start."]}]'),
    ],
)
def test_create_verifies_native_owner_and_group_manage_before_releasing_link(
    store, kind, content
):
    class Reader:
        app_id = "same-app"

        def __init__(self):
            self.calls = []
            self.owner = "ou_bot"
            self.group = False

        def _invoke(self, command, args):
            self.calls.append((command, args))
            if command[1] in {"+create", "+workbook-create"}:
                return {"document_id": "ArtifactToken1234"}
            if command[1] == "+workbook-info":
                return {"sheets": [{"sheet_id": "tab_1"}]}
            if command[1] == "+add-slide":
                return {"slide_id": "slide_1"}
            if command[:2] == ("drive", "+member-add"):
                self.group = True
                return {}
            if command[:2] == ("drive", "+member-list"):
                return (
                    {
                        "items": [
                            {
                                "member_type": "openchat",
                                "member_id": CHAT,
                                "perm": "full_access",
                            }
                        ]
                    }
                    if self.group
                    else {"items": []}
                )
            if command[0] == "api":
                endpoint = command[2]
                if endpoint.endswith("/auth"):
                    return {"auth_result": True}
                if endpoint.endswith("/transfer_owner"):
                    self.owner = json.loads(args[args.index("--data") + 1])["member_id"]
                if endpoint.endswith("/metas/batch_query"):
                    return {
                        "metas": [
                            {
                                "doc_token": "ArtifactToken1234",
                                "owner_id": self.owner,
                                "title": "Pilot",
                                "url": f"https://example.larksuite.com/{kind}/ArtifactToken1234",
                            }
                        ]
                    }
            return {}

    svc, _, _ = room(store)
    reader = Reader()
    ctx = SimpleNamespace(
        chat_type="group",
        chat_id=CHAT,
        owner="t:ou_requester",
        turn={"job_id": "same-job"},
    )
    service = LarkFiles(reader, store, svc.vault, gateway_app=reader.app_id)
    args = {"title": "Pilot", "content": content, "kind": kind}
    result = service.create(ctx, args)
    assert result["verified"] and result["url"].endswith("/ArtifactToken1234")
    assert reader.owner == "ou_requester" and reader.group
    assert service.create(ctx, args) == result
    assert (
        sum(call[0][1] in ("+create", "+workbook-create") for call in reader.calls) == 1
    )


def test_dm_setup_checks_live_membership_of_an_admitted_group(store):
    from larkagentd.lark.ui_text import SETUP_GROUP_REQUIRED

    svc, model, transport = room(store)
    checks = []
    member = True

    def group_member(group, user):
        checks.append((group, user))
        return member

    transport.group_member = group_member
    direct(svc, transport, "u", "dm", "/setup")
    assert checks == []
    assert SETUP_GROUP_REQUIRED in texts(transport, "dm")
    ask(svc, "u", "/setup")
    drain(svc, transport)
    member = False
    direct(svc, transport, "u", "dm", "/setup")
    assert checks == [(GROUP, "u")]
    assert not any(payload.get("card") for payload in transport.sent)

    member = True
    direct(svc, transport, "u", "dm", "/setup")
    assert model.calls == []
    assert any(
        payload.get("card") and payload.get("chat") == "dm"
        for payload in transport.sent
    )
    with store.user(OWNER) as (conn, state):
        assert state["setup_group"] == GROUP
        version = state["card_version"]
    member = False
    svc.action("t", "u", "dm", "button", {"action": "connect", "version": version})
    drain(svc, transport)
    with store.user(OWNER) as (conn, state):
        assert "attempt" not in state
        assert state["card_closed"]


def test_account_credentials_are_discarded_if_membership_changes_during_exchange(
    store, monkeypatch
):
    svc, model, transport = room(store)
    transport.group_member = lambda *_: True
    ask(svc, "u", "/setup")
    drain(svc, transport)
    direct(svc, transport, "u", "dm", "/setup")
    with store.user(OWNER) as (conn, state):
        state["login_version"] = "in-flight"
        svc.store.add_job(
            conn,
            OWNER,
            "auth",
            {"group": GROUP, "action": "role", "login_version": "in-flight"},
        )

    def provider_reply(*_):
        transport.group_member = lambda *_: False
        return {"credential": "sealed-synthetic", "credential_version": "version"}, None

    monkeypatch.setattr(svc, "auth", provider_reply)
    drain(svc, transport)
    with store.user(OWNER) as (_, state):
        assert "credential" not in state
        assert state["card_closed"]
    assert model.calls == []


def test_dm_policy_endpoint_is_gone_and_workspace_can_pause(store):
    svc, model, transport = room(store)
    with pytest.raises(AccessError, match="not_found"):
        change(svc.access, "GET", "dm")
    change(svc.access, "PATCH", "bot-access", {"paused": True})
    assert change(svc.access, "GET", "bot-access")[1] == {"paused": True}
    ask(svc, "u", "Should be paused.")
    drain(svc, transport)
    assert not model.calls
    change(svc.access, "PATCH", "bot-access", {"paused": False})
    ask(svc, "u", "Should be handled.")
    drain(svc, transport)
    assert len(model.calls) == 1


def test_verified_group_member_can_finish_device_login_in_dm_without_a_model(store):
    from test_card_lifecycle import ready_jobs

    svc, model, transport = room(store)
    transport.group_member = lambda group, user: (group, user) == (GROUP, "u")
    ask(svc, "u", "/setup")
    drain(svc, transport)
    direct(svc, transport, "u", "dm", "/setup")

    def click(action, choice=None):
        with store.user(OWNER) as (_, state):
            version = state["card_version"]
        svc.action(
            "t",
            "u",
            "dm",
            action,
            {"action": action, "version": version, "choice": choice},
        )
        drain(svc, transport)

    click("connect")
    ready_jobs(store, "auth")
    drain(svc, transport)
    click("account", "123")
    click("role", "role1")
    with store.user(OWNER) as (_, state):
        assert state["credential_version"]
        assert state["card_closed"]
    assert not model.calls
    ask(svc, "u", "A group question after sign-in.")
    drain(svc, transport)
    assert model.requests[0]["credential_version"]
    assert texts(transport, GROUP)[-1] == model.reply


def test_renaming_edits_the_same_scoped_source(store):
    svc, _, _ = room(store)
    access = svc.access
    for group in (CHAT, "oc_other"):
        change(access, "POST", "groups", {"group_id": group})
    route = f"groups/{CHAT}/knowledge/sources"
    _, added = change(
        access, "POST", route, {"name": "Before", "markdown": "Some notes"}
    )
    edit = {"id": added["id"], "name": "After", "markdown": "Updated"}
    change(access, "PATCH", route, edit)
    assert [
        (row["id"], row["name"]) for row in change(access, "GET", route)[1]["sources"]
    ] == [(added["id"], "After")]
    with pytest.raises(AccessError, match="source_name_in_use"):
        change(access, "POST", route, {"name": "After", "markdown": "Other"})
    with pytest.raises(AccessError, match="source_not_found"):
        change(access, "PATCH", "groups/oc_other/knowledge/sources", edit)
    change(access, "DELETE", route, {"id": added["id"]})
    assert not change(access, "GET", route)[1]["sources"]


def test_optional_mcp_and_required_encryption_key(monkeypatch):
    from larkagentd.config import _shared

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost/test")
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValueError, match="CREDENTIAL_ENCRYPTION_KEY"):
        _shared()
    assert mcp_settings("") == {}
    with pytest.raises(ValueError):
        mcp_settings("invalid")


def test_learned_page_cursor_stays_stable_when_a_new_fact_arrives(store, monkeypatch):
    from datetime import datetime, timezone

    from larkagentd.knowledge import learned
    from larkagentd.knowledge.lark_sources import SourceMessage
    from larkagentd.knowledge.runtime import GroupBrainRuntime

    svc, _, _ = room(store)
    change(svc.access, "POST", "groups", {"group_id": CHAT})
    scope = f"group:{CHAT}"
    messages = {}
    runtime = GroupBrainRuntime(
        store.engine,
        GroupBrainConfig(enabled=True),
        source=SimpleNamespace(messages=lambda _, ids: [messages[key] for key in ids]),
    )

    def save(index):
        key = f"om_message{index}"
        message = SourceMessage(
            key,
            CHAT,
            None,
            None,
            None,
            "ou_synthetic",
            "user",
            datetime.now(timezone.utc),
            f"Decision number {index} is final.",
            "text",
            False,
            None,
        )
        messages[key] = message
        return learned.learn(
            runtime,
            scope,
            "ou_synthetic",
            {"content": message.text, "category": "decision"},
            [key],
            runtime.effective(scope).generation,
        )["memory_id"]

    monkeypatch.setattr(learned, "PAGE_SIZE", 2)
    oldest, middle, newest = [save(i) for i in range(3)]
    with store.engine.begin() as conn:
        first, cursor = learned.list_records(conn, scope, "")
    assert [row["id"] for row in first] == [newest, middle]
    save(3)
    with store.engine.begin() as conn:
        second, end = learned.list_records(conn, scope, cursor)
    assert [row["id"] for row in second] == [oldest]
    assert end is None


def test_extension_query_reporting_uses_the_declared_operation_and_project(store):
    from larkagentd.gateway.reporting import Reporting
    from larkagentd.toolservice import activity

    turn = {
        "session_id": "example",
        "job_id": "job",
        "owner": "t:user",
        "chat_type": "group",
        "chat_id": CHAT,
    }
    activity.start(store, turn, "example_estimator", "example_project", "estimate")
    reporting = Reporting(store)
    page = reporting.get("queries", "tool=example_estimator&project=example_project")
    assert page["total"] == 1
    assert page["items"][0]["tool"] == "example_estimator"
    assert reporting.get("summary", "")["statistics"]["estimate_calls"] == 1


def test_group_reply_uses_directory_name_and_native_requester_mention(store):
    from larkagentd.gateway.hermes import current_message
    from larkagentd.lark.adapter import Lark

    svc, model, transport = room(store, "Happy to help!")
    transport.display_names = lambda ids: (
        {"ou_requester": "Rina"} if "ou_requester" in ids else {}
    )
    msg = ask(svc, "ou_requester", "Hello there!")
    drain(svc, transport)
    request = model.requests[0]
    assert request["requester_name"] == "Rina"
    assert current_message(request).startswith('From "Rina":')
    assert "Person 1" not in current_message(request)
    answer = next(p for p in transport.sent if p.get("answer_to"))
    delivered = []

    def deliver(chat, target, kind, content, key, thread=False):
        delivered.append((chat, target, kind, json.loads(content), thread))
        return "om_sent"

    adapter = SimpleNamespace(deliver=deliver, send_parts=lambda *_: None)
    assert Lark.send(adapter, answer, "unique") == "om_sent"
    assert delivered == [
        (
            GROUP,
            msg,
            "post",
            {
                "zh_cn": {
                    "content": [
                        [{"tag": "at", "user_id": "ou_requester"}],
                        [{"tag": "md", "text": answer["text"]}],
                    ]
                }
            },
            True,
        )
    ]


def test_mention_is_gateway_owned_in_post_card_and_text_fallbacks():
    from larkagentd.lark.adapter import Lark

    delivered = {}

    def fail_until_text(chat, target, kind, content, key, thread=False):
        delivered[kind] = json.loads(content)
        if kind != "text":
            raise RuntimeError("unavailable")
        return "sent"

    adapter = SimpleNamespace(deliver=fail_until_text, send_parts=lambda *_: None)
    words = "Hello <at id=all></at>"
    payload = {
        "chat": CHAT,
        "card": None,
        "reply_to": "om_origin",
        "thread": True,
        "text": words,
        "answer_to": "ou_requester",
        "preview_card": json.dumps(
            {"body": {"elements": [{"tag": "markdown", "content": words}]}}
        ),
    }
    assert Lark.send(adapter, payload, "unique") == "sent"
    elements = delivered["interactive"]["body"]["elements"]
    assert elements[0]["content"] == "<at id=ou_requester></at>"
    assert "<at" not in elements[1]["content"]
    assert delivered["post"]["zh_cn"]["content"][0] == [
        {"tag": "at", "user_id": "ou_requester"}
    ]
    assert delivered["text"]["text"].startswith('<at user_id="ou_requester"></at>')
    assert "<at id=all>" not in delivered["text"]["text"]


def test_unmentionable_sender_still_receives_answer_in_the_original_thread():
    from larkagentd.lark.adapter import Lark

    attempts = []

    def deliver(chat, target, kind, content, key, thread):
        attempts.append((target, thread))
        if "ou_requester" in content:
            raise RuntimeError("mention rejected")
        return "om_sent"

    adapter = SimpleNamespace(deliver=deliver, send_parts=lambda *_: None)
    payload = {
        "card": None,
        "answer_to": "ou_requester",
        "reply_to": "om_origin",
        "thread": True,
        "chat": CHAT,
        "text": "Hi!",
    }
    assert Lark.send(adapter, payload, "unique") == "om_sent"
    assert attempts == [("om_origin", True)] * 3


def test_reply_skill_is_discoverable_and_readable():
    from pathlib import Path

    from larkagentd.skills.skills import SkillStore

    root = Path(__file__).resolve().parents[1] / "src/larkagentd/skills/builtin"
    store = SkillStore([root])
    assert "lark-replies" in {item["name"] for item in store.list()["global"]}
    assert "```sh" in store.read("lark-replies")["content"]
