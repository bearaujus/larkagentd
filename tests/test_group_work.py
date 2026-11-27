import json
import xml.etree.ElementTree as ET
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
from larkagentd.knowledge.learned import (
    activate_candidate,
    list_records,
    verify_candidate,
)
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


def confirmation_value(payload):
    elements = json.loads(payload["preview_card"])["body"]["elements"]
    callback = next(e for e in elements if e["tag"] == "button")["behaviors"][0]
    return callback["value"]


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

    library = SourceLibrary(store, connectors, read=read)
    config = GroupBrainConfig(enabled=True)
    found = library.search(f"group:{CHAT}", "pilot", config)
    assert {item["source"] for item in found["results"]} == {
        "Brief",
        "Policy",
        "Policy MCP",
        "Calendar MCP",
    }
    assert "30 volunteers" in str(found) and "95 volunteers" not in str(found)
    shared = library.search(f"group:{CHAT}", "pilot", config, source_scope="workspace")
    assert {item["scope"] for item in shared["results"]} == {"workspace"}
    assert "volunteers" not in str(shared)
    own = library.search(f"group:{CHAT}", "pilot", config, source_scope="group")
    other = library.search("group:oc_other", "pilot", config, source_scope="group")
    assert [item["text"] for item in own["results"]] == [
        "The pilot uses 30 volunteers."
    ]
    assert [item["text"] for item in other["results"]] == [
        "The pilot uses 95 volunteers."
    ]


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


@pytest.mark.parametrize(
    "bot_view,external_file,external_chat,same_tenant,app_matches,write,expected",
    [
        (True, False, False, True, True, False, None),
        (False, False, False, True, True, False, "lark_bot_view_denied"),
        (True, True, False, True, True, False, "lark_permission_unverified"),
        (True, False, True, True, True, False, "lark_permission_unverified"),
        (True, False, False, False, True, False, "lark_permission_unverified"),
        (True, False, False, True, False, False, "lark_permission_unverified"),
        (True, False, False, True, True, True, "lark_edit_unverified"),
    ],
)
def test_org_link_permission_when_lark_withholds_collaborators(
    bot_view, external_file, external_chat, same_tenant, app_matches, write, expected
):
    class Reader:
        app_id = "test-app"

        def _invoke(self, command, _args):
            match command[-1].split("/")[-1]:
                case "+member-list":
                    raise LarkError("forbidden")
                case "auth":
                    return {"auth_result": bot_view}
                case "public":
                    return {
                        "permission_public": {
                            "link_share_entity": "tenant_readable",
                            "external_access": external_file,
                        }
                    }
                case _:
                    return {
                        "external": external_chat,
                        "tenant_key": "t" if same_tenant else "another-tenant",
                    }

    files = LarkFiles(Reader(), None, None, "test-app" if app_matches else "other-app")
    link = parse_link(DOC)
    ctx = SimpleNamespace(
        owner=OWNER, chat_type="group", chat_id=CHAT, lark_allowed={link.token}
    )
    if expected:
        with pytest.raises(LarkError, match=expected):
            files.require(ctx, link, write=write)
    else:
        files.require(ctx, link, write=write)


@pytest.mark.parametrize(
    "group_edit, org_visible, edit_refusal, after_confirm, fresh_link",
    [
        ("view", True, "lark_write_permission", None, False),
        ("hidden", False, "lark_edit_unverified", None, False),
        ("edit", False, None, None, False),
        ("edit", False, None, "remove_group_edit", False),
        ("hidden", True, None, None, False),
        ("hidden", True, None, None, True),
        ("hidden", True, None, "remove_org_view", False),
        ("hidden", True, None, "lark_refuses_write", False),
        ("edit", False, None, "no_change", False),
    ],
)
def test_thread_edit_checks_access_and_requires_confirm(
    store, caplog, group_edit, org_visible, edit_refusal, after_confirm, fresh_link
):
    svc, model, transport = room(store)
    registry = ToolRegistry(store)
    from larkagentd.lark.inbound import Fetched

    transport.fetch_message = lambda _: Fetched(DOC, chat_id=GROUP, sender_id="u")

    class Reader:
        app_id = "synthetic-app"
        permission = "view"
        organization_link = org_visible
        refuse_write = False
        no_change = False
        content = "The rollout starts Monday."

        def _invoke(self, command, _args):
            if command[0] == "drive":
                if self.permission == "hidden":
                    raise LarkError("forbidden", provider_code=1063004)
                return {
                    "items": [
                        {
                            "member_id": GROUP,
                            "member_type": "openchat",
                            "perm": self.permission,
                        }
                    ]
                }
            if command == ("docs", "+update"):
                updates.append(command)
                if self.refuse_write:
                    raise LarkError("forbidden", provider_code=1063002)
                if not self.no_change:
                    self.content += "\nchange"
            if command[-1].endswith("/raw_content"):
                return {"content": self.content}
            if command[-1].endswith("/public"):
                return {
                    "permission_public": {
                        "external_access": False,
                        "link_share_entity": "tenant_readable"
                        if self.organization_link
                        else "closed",
                    }
                }
            if "/im/v1/chats/" in command[-1]:
                return {"external": False, "tenant_key": "t"}
            return {"auth_result": True}

        def doc(self, url):
            assert url == DOC
            return {"content": "The rollout starts Monday."}

    reader = Reader()
    tools = ToolService(
        store,
        svc,
        KEY,
        registry=registry,
        vault=svc.vault,
        lark=reader,
        gateway_app=reader.app_id,
    )
    registry.sync(tools.tools)
    with store.engine.begin() as conn:
        store.set_scope_tools(conn, f"group:{GROUP}", DEFAULT_GROUP_TOOLS)

    def call(session_id, name, args):
        return tools.handle(
            f"Bearer {KEY}",
            name,
            json.dumps({"session_id": session_id, "arguments": args}).encode(),
        )[1]

    def check_tools(session_id):
        assert (
            call(session_id, "lark_read", {"url": DOC})["result"]["content"]
            == "The rollout starts Monday."
        )
        reader.permission = group_edit
        outcomes.append(
            call(
                session_id,
                "lark_file_edit",
                {"url": DOC, "action": "append", "content": "change"},
            )
        )

    outcomes, updates = [], []
    model.hook = check_tools
    text = f"Append this to {DOC}" if fresh_link else "Try that append again?"
    origin = ask(svc, "u", text, thread="original")
    drain(svc, transport)
    if not fresh_link:
        assert model.requests[0]["lark_thread_links"] == [DOC]
    assert len(outcomes) == 1
    assert not updates
    if edit_refusal:
        assert outcomes[0]["error"] == edit_refusal
        assert outcomes[0]["retryable"] is False
        assert outcomes[0]["note"]
        if group_edit == "hidden":
            records = [
                record
                for record in caplog.records
                if getattr(record, "event", None) == "tool.called"
                and record.fields.get("tool") == "lark_file_edit"
            ]
            (record,) = records
            assert {
                k: record.fields[k]
                for k in (
                    "stage",
                    "access",
                    "bot_access",
                    "provider_code",
                    "upstream_outcome",
                )
            } == {
                "stage": "collaborator_list",
                "access": "edit",
                "bot_access": "granted",
                "provider_code": 1063004,
                "upstream_outcome": "forbidden",
            }
            assert record.fields["job_id"]
            assert DOC not in record.getMessage()
            assert "change" not in record.getMessage()
        return
    assert outcomes[0]["result"]["confirmation_requested"]
    if group_edit == "hidden":
        assert outcomes[0]["result"]["access_basis"] == "bot_edit"
    card = next(p for p in transport.sent if p.get("preview_card"))
    assert card["reply_to"] == origin
    value = confirmation_value(card)
    assert "no longer available" in svc.action("t", "other", GROUP, "1", value)
    assert "Confirmed" in svc.action("t", "u", GROUP, "2", value)
    if after_confirm == "remove_group_edit":
        reader.permission = "view"
    elif after_confirm == "remove_org_view":
        reader.organization_link = False
    elif after_confirm == "lark_refuses_write":
        reader.refuse_write = True
    elif after_confirm == "no_change":
        reader.no_change = True
    model.hook = lambda sid: outcomes.extend(
        call(sid, "run_confirmed_action", {}) for _ in range(2)
    )
    drain(svc, transport)
    if after_confirm == "remove_group_edit":
        assert outcomes[1]["error"] == "lark_write_permission"
        assert not updates
    elif after_confirm == "remove_org_view":
        assert outcomes[1]["error"] == "lark_edit_unverified"
        assert not updates
    elif after_confirm == "lark_refuses_write":
        assert outcomes[1]["error"] == "forbidden"
        (record,) = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "tool.called"
            and record.fields.get("stage") == "write_request"
        ]
        assert record.fields["provider_code"] == 1063002
        assert updates == [("docs", "+update")]
    elif after_confirm == "no_change":
        assert outcomes[1]["error"] == "write_no_change"
        assert updates == [("docs", "+update")]
    else:
        assert outcomes[1]["result"]["verified"]
        if group_edit == "hidden":
            assert outcomes[1]["result"]["access_basis"] == "bot_edit"
        assert updates == [("docs", "+update")]
    assert outcomes[2]["error"] == "confirmation_expired"


def test_slide_outline_yields_native_editable_safe_xml():
    xml = _slide_xml({"title": "A & B < C", "bullets": ["Pilot outcomes"]})
    assert "&amp;" in xml and "&lt;" in xml and "<shape" in xml
    with pytest.raises(LarkError, match="bad_slide"):
        _slide_xml({"title": "A", "bullets": []})


CHART_SLIDE = {
    "title": "Share by region",
    "chart": {
        "categories": ["North & East", "South"],
        "series": [
            {"name": "Orders (%)", "values": [40.5, 59.5]},
            {"name": "Sales (%)", "values": [55, 45]},
        ],
    },
}


def test_slide_chart_has_editable_grouped_columns_and_validated_values():
    slide = ET.fromstring(_slide_xml(CHART_SLIDE))
    assert slide.find(".//{*}chartPlot").get("type") == "column"
    assert slide.find(".//{*}chartStack") is None
    assert slide.find(".//{*}dim1/{*}chartField").text == "North & East,South"
    assert [
        (field.get("name"), field.text)
        for field in slide.findall(".//{*}dim2/{*}chartField")
    ] == [("Orders (%)", "40.5,59.5"), ("Sales (%)", "55,45")]
    for values in ([10], [True, 10], [float("nan"), 10]):
        invalid = {
            **CHART_SLIDE,
            "chart": {
                "categories": ["North", "South"],
                "series": [{"name": "Orders", "values": values}],
            },
        }
        with pytest.raises(LarkError, match="bad_slide"):
            _slide_xml(invalid)


def test_slide_chart_has_editable_percent_stacked_bars():
    chart = {
        "type": "percent_stacked_bar",
        "categories": ["Direct", "Partner"],
        "series": [
            {"name": "Low usage", "values": [20, 60]},
            {"name": "Rejection", "values": [10, 24]},
            {"name": "Eligible", "values": [70, 116]},
        ],
    }
    slide = ET.fromstring(_slide_xml({"title": "Failure mix", "chart": chart}))
    assert slide.find(".//{*}chartPlot").get("type") == "bar"
    assert slide.find(".//{*}chartStack").get("percentage") == "true"
    assert [
        (field.get("name"), field.text)
        for field in slide.findall(".//{*}dim2/{*}chartField")
    ] == [("Low usage", "20,60"), ("Rejection", "10,24"), ("Eligible", "70,116")]
    for values in ([20, -1], [20, False]):
        bad = {
            **chart,
            "series": [*chart["series"], {"name": "Other", "values": values}],
        }
        with pytest.raises(LarkError, match="bad_slide"):
            _slide_xml({"title": "Invalid mix", "chart": bad})


@pytest.mark.parametrize(
    "oversized, outcome, stage",
    [
        (True, "bad_slide", "slide_validation"),
        (False, "lark_slides_create_scope", "create_request"),
    ],
)
def test_slide_create_distinguishes_validation_from_lark_refusal(
    store, caplog, oversized, outcome, stage
):
    svc, model, transport = room(store)
    registry = ToolRegistry(store)
    calls, results = [], []

    class Reader:
        app_id = "synthetic-app"

        def _invoke(self, command, _args):
            calls.append(command)
            raise LarkError("forbidden", provider_code=99991672)

    reader = Reader()
    tools = ToolService(
        store,
        svc,
        KEY,
        registry=registry,
        vault=svc.vault,
        lark=reader,
        gateway_app=reader.app_id,
    )
    registry.sync(tools.tools)
    with store.engine.begin() as conn:
        store.set_scope_tools(conn, f"group:{GROUP}", DEFAULT_GROUP_TOOLS)
    slide = {"title": "Pilot", "bullets": ["x" * 101]} if oversized else CHART_SLIDE
    args = {"kind": "slides", "title": "Pilot", "content": json.dumps([slide])}
    model.hook = lambda sid: results.append(
        tools.handle(
            f"Bearer {KEY}",
            "lark_file_create",
            json.dumps({"session_id": sid, "arguments": args}).encode(),
        )[1]
    )
    ask(svc, "u", "Build slides for the pilot")
    drain(svc, transport)
    (result,) = results
    assert result["error"] == outcome and result["retryable"] is oversized
    assert calls == ([] if oversized else [("slides", "+create")])
    (record,) = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "tool.called"
        and record.fields.get("tool") == "lark_file_create"
    ]
    assert record.fields["stage"] == stage
    if not oversized:
        assert "slides:presentation:create" in result["note"]
        assert record.fields["provider_code"] == 99991672


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
    value = confirmation_value(payload)
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


def save_learned_fact(runtime, scope, message, content):
    args = {
        "content": content,
        "category": "decision",
        "evidence": message.text,
        "current_only": True,
        "ids": [message.message_id],
    }
    generation = runtime.effective(scope).generation
    live = verify_candidate(runtime, scope, message.sender_id, args)
    with runtime.connection() as conn:
        assert (
            activate_candidate(
                conn, runtime, scope, message.sender_id, args, live, generation
            )
            == "saved"
        )
        records, _ = list_records(conn, scope, None)
    return records[0]


def test_learned_fact_stops_serving_when_its_original_message_changes(store):
    from dataclasses import replace
    from datetime import datetime, timezone

    from larkagentd.knowledge.lark_sources import SourceMessage
    from larkagentd.knowledge.learned import verify
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
    result = save_learned_fact(
        runtime, scope, message, "The pilot uses thirty volunteers."
    )
    source_rows = result["refs"]
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
    assert shown()["saved_at"] < shown()["expires_at"]

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


def test_dashboard_reviews_the_pending_revision_with_live_evidence(store):
    from dataclasses import replace
    from datetime import datetime, timezone

    from gb_fakes import message
    from test_dashboard import identify

    from larkagentd.gateway.access import Access
    from larkagentd.gateway.learned_page import correction_review
    from larkagentd.knowledge.runtime import GroupBrainRuntime
    from larkagentd.toolservice.groupbrain_memory import group_memory_correct

    cfg = GroupBrainConfig(enabled=True)
    scope = f"group:{CHAT}"
    original = replace(
        message(
            "om_blue", chat_id=CHAT, sender_id="u", text="The pilot uses blue tags."
        ),
        created_at=datetime.now(timezone.utc),
    )
    correction = replace(
        original, message_id="om_green", text="The pilot now uses green tags."
    )
    messages = {m.message_id: m for m in (original, correction)}
    runtime = GroupBrainRuntime(
        store.engine,
        cfg,
        source=SimpleNamespace(
            messages=lambda _group, ids: [messages[i] for i in ids if i in messages]
        ),
    )
    access = Access(store, ("admin@example.com",), group_brain=cfg, runtime=runtime)
    change(access, "POST", "groups", {"group_id": CHAT})
    saved = save_learned_fact(runtime, scope, original, original.text)
    proposed = group_memory_correct(
        SimpleNamespace(
            chat_type="group",
            chat_id=CHAT,
            owner=OWNER,
            group_brain=runtime,
            turn={"message_id": correction.message_id},
        ),
        {
            "memory_id": saved["id"],
            "replacement": correction.text,
            "reason": "New group decision",
            "evidence": correction.text,
        },
    )
    assert proposed["state"] == "pending"
    assert proposed["revision"] == 2
    identify(store, "t:admin", "admin@example.com")
    with store.engine.begin() as conn:
        token, _ = access.issue(conn, "t:admin")
        session, _ = access.exchange(conn, token)
        actor = access.authenticate(conn, session)
        assert access.knowledge_store.catalog_counts(conn, scope)["pending_review"] == 1
    path = ["groups", CHAT, "knowledge", "candidates"]
    member = SimpleNamespace(
        group_member=lambda group, owner: (group, owner) == (CHAT, "admin")
    )
    outsider = SimpleNamespace(group_member=lambda *_: False)
    _, hidden = correction_review(access, session, path, "GET", {}, actor, outsider)
    assert "content" not in hidden["candidates"][0]
    _, shown = correction_review(access, session, path, "GET", {}, actor, member)
    (pending,) = shown["candidates"]
    assert pending["state"] == "pending" and pending["status"] == "verified"
    assert pending["content"] == correction.text
    approve = [*path, saved["id"], "approve"]
    body = {
        "revision": 2,
        "content_hash": pending["content_hash"],
        "acknowledge_group_sharing": True,
    }
    messages[correction.message_id] = replace(
        correction, text="The plan is still open."
    )
    with pytest.raises(AccessError, match="source_unavailable"):
        correction_review(access, session, approve, "POST", body, actor, member)
    messages[correction.message_id] = correction
    _, approved = correction_review(
        access, session, approve, "POST", body, actor, member
    )
    assert approved["status"] == "active"
    with store.engine.begin() as conn:
        records, _ = list_records(conn, scope, None)
        assert records[0]["sanitized_content"] == correction.text
        assert access.knowledge_store.catalog_counts(conn, scope)["pending_review"] == 0
        assert (
            access.dispatch(
                conn,
                actor,
                "POST",
                ["groups", CHAT, "knowledge", "learned", saved["id"], "retire"],
                {},
            )[0]
            == 200
        )
        assert list_records(conn, scope, None) == ([], None)


def test_memory_recall_looks_past_stale_matches_and_never_cites_expired_facts(store):
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone

    from gb_fakes import message
    from sqlalchemy import update

    from larkagentd.knowledge.runtime import GroupBrainRuntime
    from larkagentd.knowledge.tables import gb_memory_revisions
    from larkagentd.toolservice.groupbrain_knowledge import (
        group_knowledge_search,
        group_knowledge_sources,
    )

    svc, _, _ = room(store)
    for chat in (CHAT, "oc_other"):
        change(svc.access, "POST", "groups", {"group_id": chat})
    now = datetime.now(timezone.utc)
    good = replace(
        message(
            "om_valid",
            chat_id=CHAT,
            sender_id="u",
            text="Prosedur untuk PLAN-42 memakai templat biru.",
        ),
        created_at=now,
    )
    stale = replace(
        good,
        message_id="om_stale",
        text="Keputusan PLAN-42: prosedur persetujuan masih tertunda.",
    )
    other = replace(
        good,
        message_id="om_other",
        chat_id="oc_other",
        text="Keputusan PLAN-42: prosedur persetujuan berbeda.",
    )
    messages = {m.message_id: m for m in (good, stale, other)}
    runtime = GroupBrainRuntime(
        store.engine,
        GroupBrainConfig(enabled=True),
        source=SimpleNamespace(
            messages=lambda _group, ids: [messages[i] for i in ids if i in messages]
        ),
    )
    scope = f"group:{CHAT}"
    saved = save_learned_fact(runtime, scope, good, good.text)
    save_learned_fact(runtime, scope, stale, stale.text)
    save_learned_fact(runtime, "group:oc_other", other, other.text)
    del messages[stale.message_id]
    ctx = SimpleNamespace(
        chat_type="group",
        chat_id=CHAT,
        owner=OWNER,
        group_brain=runtime,
        job_note=None,
        conversation={},
    )
    result = group_knowledge_search(
        ctx,
        {"query": "Apa prosedur persetujuan PLAN-42?", "kinds": ["memory"], "limit": 1},
    )
    (fact,) = result["results"]
    assert fact["text"] == good.text
    lookup = {"evidence_ids": [fact["evidence_id"]]}
    assert group_knowledge_sources(ctx, lookup)["evidence"][0]["status"] == "valid"
    with runtime.connection() as conn:
        conn.execute(
            update(gb_memory_revisions)
            .where(gb_memory_revisions.c.memory_id == saved["id"])
            .values(expires_at=now - timedelta(days=1))
        )
    assert (
        group_knowledge_sources(ctx, lookup)["evidence"][0]["status"] == "unavailable"
    )


@pytest.mark.parametrize(
    "kind,content",
    [
        ("docx", "# Pilot\nA quick update."),
        ("sheets", '[["Name","Status"],["Pilot","Ready"]]'),
        ("slides", json.dumps([CHART_SLIDE])),
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
            self.matrix = []
            self.page = None

        def _invoke(self, command, args):
            self.calls.append((command, args))
            if command[1] in {"+create", "+workbook-create"}:
                return {"document_id": "ArtifactToken1234"}
            if command[1] == "+workbook-info":
                return {"sheets": [{"sheet_id": "tab_1"}]}
            if command[1] == "+add-slide":
                self.page = ET.fromstring(args[args.index("--slide") + 1])
                self.page.set("id", "slide_1")
                return {"slide_id": "slide_1"}
            if command[1] == "+xml-get":
                deck = ET.Element("presentation")
                deck.append(self.page)
                return {
                    "xml_presentation": {
                        "content": ET.tostring(deck, encoding="unicode")
                    }
                }
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
                if endpoint.endswith("/raw_content"):
                    return {"content": "A quick update."}
                if endpoint.endswith("/values") and command[1] == "PUT":
                    request = json.loads(args[args.index("--data") + 1])["valueRange"]
                    assert request["range"] == "tab_1!A1:B2"
                    self.matrix = request["values"]
                if endpoint.endswith("/values/tab_1!A1:B2") and command[1] == "GET":
                    return {"valueRange": {"values": self.matrix}}
                if endpoint.endswith("/auth"):
                    assert (
                        json.loads(args[args.index("--params") + 1])["action"]
                        == "manage_public"
                    )
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


def test_sheet_write_verifies_the_requested_rectangle():
    from larkagentd.lark.links import parse_link

    class Reader:
        def _invoke(self, command, args):
            if command[1] == "PUT":
                request = json.loads(args[args.index("--data") + 1])["valueRange"]
                assert request == {"range": "tab_1!Z2:AA2", "values": [[9, 12]]}
                return {"updatedCells": 2}
            assert command == (
                "api",
                "GET",
                "/open-apis/sheets/v2/spreadsheets/ArtifactToken1234/values/tab_1!Z2:AA2",
            )
            return {"valueRange": {"values": self.values}}

    reader = Reader()
    link = parse_link("https://example.larksuite.com/sheets/ArtifactToken1234")
    files = LarkFiles(reader, None, None)
    files.set_cells(link, "tab_1", "Z2", "[[9,12]]")
    reader.values = [[7, 12]]
    with pytest.raises(LarkError, match="write_outcome_unknown"):
        files.verify_cells(link, "tab_1", "Z2", "[[9,12]]")
    reader.values = [[9, 12]]
    files.verify_cells(link, "tab_1", "Z2", "[[9,12]]")


def test_sheet_preview_honors_a_selected_tab_without_a_range(monkeypatch, tmp_path):
    from larkagentd.lark.links import parse_link
    from larkagentd.toolservice.larkcli import LarkReader

    url = "https://example.larksuite.com/sheets/ArtifactToken1234"
    reader = LarkReader(("lark-cli",), "app_example", "fixture", tmp_path)
    files = LarkFiles(reader, None, None)
    monkeypatch.setattr(files, "require", lambda *_: None)

    def invoke(command, _args):
        if command == ("sheets", "+workbook-info"):
            return {
                "sheets": [
                    {"sheet_id": "tab_1", "sheet_name": "Summary"},
                    {
                        "sheet_id": "tab_2",
                        "sheet_name": "Pilot results",
                        "row_count": 10,
                        "column_count": 2,
                    },
                ]
            }
        assert command == (
            "api",
            "GET",
            "/open-apis/sheets/v2/spreadsheets/ArtifactToken1234/values/tab_2!A1:B10",
        )
        return {"valueRange": {"values": [["Invited", 30]]}}

    monkeypatch.setattr(reader, "_invoke", invoke)
    result = files.read(None, parse_link(url), sheet_id="Pilot results")
    assert result["preview"]["sheet_name"] == "Pilot results"
    assert result["preview"]["rows"] == [["Invited", 30]]


def test_slide_verification_rejects_title_without_chart(monkeypatch):
    from larkagentd.lark.links import parse_link
    from larkagentd.toolservice.larkfiles import _slide_xml

    xml = _slide_xml(
        {
            "title": "Pilot results",
            "chart": {
                "categories": ["First week", "Second week"],
                "series": [{"name": "Sign-ups", "values": [24, 30]}],
            },
        }
    )
    deck = ET.Element("presentation")
    page = ET.fromstring(xml)
    page.set("id", "page_1")
    deck.append(page)
    files = LarkFiles(None, None, None)
    monkeypatch.setattr(
        files,
        "_cli",
        lambda *_: {"xml_presentation": ET.tostring(deck, encoding="unicode")},
    )
    link = parse_link("https://example.larksuite.com/slides/ArtifactToken1234")
    files.verify_slides(link, {"page_1": xml})
    data = list(page)[0]
    data.remove(list(data)[-1])
    with pytest.raises(LarkError, match="write_outcome_unknown"):
        files.verify_slides(link, {"page_1": xml})


def test_slide_replace_uses_full_page_content_and_verifies_the_target(store):
    from larkagentd.lark.links import parse_link

    title = "Review outcome"
    link = parse_link("https://example.larksuite.com/slides/ArtifactToken1234")

    class Reader:
        page = None

        def _invoke(self, command, args):
            if command == ("drive", "+member-list"):
                return {
                    "items": [
                        {"member_type": "openchat", "member_id": CHAT, "perm": "edit"}
                    ]
                }
            if command[0] == "api":
                assert command[2].endswith("/auth")
                return {"auth_result": True}
            if command == ("slides", "+update-slide"):
                assert "--slide" not in args
                assert args[args.index("--slide-id") + 1] == "page_1"
                self.page = ET.fromstring(args[args.index("--content") + 1])
                self.page.set("id", "page_1")
                return {}
            assert command == ("slides", "+xml-get")
            root = ET.Element("presentation")
            root.append(self.page)
            return {
                "xml_presentation": {"content": ET.tostring(root, encoding="unicode")}
            }

    svc, _, _ = room(store)
    ctx = SimpleNamespace(
        chat_type="group",
        chat_id=CHAT,
        lark_allowed={link.token},
        turn={"job_id": "edit-slide"},
    )
    result = LarkFiles(Reader(), store, svc.vault).edit(
        ctx,
        link,
        {
            "action": "replace",
            "slide_id": "page_1",
            "content": json.dumps({"title": title, "bullets": ["Review on Monday."]}),
        },
    )
    assert result["updated"] is True and result["verified"] is True


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
        return save_learned_fact(runtime, scope, message, message.text)["id"]

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
    (sent,) = delivered
    assert sent[:3] == (GROUP, msg, "interactive") and sent[4]
    elements = sent[3]["body"]["elements"]
    assert elements[0]["content"] == "<at id=ou_requester></at>"
    assert elements[1]["content"] == answer["text"]


def test_mention_is_gateway_owned_in_post_card_and_text_fallbacks():
    from larkagentd.lark.adapter import Lark

    delivered = {}

    def fail_until_text(chat, target, kind, content, key, thread=False):
        delivered.setdefault(kind, json.loads(content))
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


def test_group_history_can_open_live_file_but_grants_no_edit_or_later_turn_access(
    store, tmp_path, monkeypatch
):
    from larkagentd.toolservice import toolservice
    from larkagentd.toolservice.larkcli import LarkReader

    svc, model, transport = room(store)
    group = CHAT
    change(svc.access, "POST", "groups", {"group_id": group})
    src = {
        "chat_id": group,
        "message_id": "om_humanmessage",
        "create_time": "2026-11-27 02:00",
        "msg_type": "text",
        "sender": {"id": "ou_teammate", "sender_type": "user"},
        "content": {"text": f"The test page is {DOC}"},
    }
    app_doc = "https://example.larksuite.com/docx/AppPostedDocument"
    cross_doc = "https://example.larksuite.com/docx/CrossGroupDocument"

    class Reader(LarkReader):
        latest = src
        org_readable = True
        docs = []

        def _call(self, command, args, cwd=None):
            if command == ("im", "+chat-messages-list"):
                return {
                    "messages": [
                        src,
                        dict(
                            src,
                            message_id="om_appmessage",
                            content={"text": app_doc},
                            sender={"sender_type": "app"},
                        ),
                        dict(
                            src,
                            message_id="om_crossmessage",
                            content={"text": cross_doc},
                            chat_id="oc_anothergroup",
                        ),
                    ]
                }
            assert command == ("im", "+messages-mget")
            return {"messages": [self.latest]}

        def _invoke(self, command, _args):
            route = command[-1]
            if route.endswith("/members/auth"):
                return {"auth_result": True}
            if command == ("drive", "+member-list"):
                raise LarkError("forbidden", provider_code=1063004)
            if route.endswith("/public"):
                return {
                    "permission_public": {
                        "external_access": False,
                        "link_share_entity": "tenant_readable"
                        if self.org_readable
                        else "closed",
                    }
                }
            assert route == f"/open-apis/im/v1/chats/{group}"
            return {"external": False, "tenant_key": "t"}

        def doc(self, url):
            self.docs.append(url)
            return {"content": "SELECT item FROM test_table LIMIT 5"}

    reader = Reader(("lark-cli",), "synthetic", "dummy", tmp_path)
    registry = ToolRegistry(store)
    tools = ToolService(
        store,
        svc,
        KEY,
        registry=registry,
        vault=svc.vault,
        lark=reader,
        gateway_app=reader.app_id,
    )
    registry.sync(tools.tools)

    def call(sid, name, **arguments):
        return tools.handle(
            f"Bearer {KEY}",
            name,
            json.dumps({"session_id": sid, "arguments": arguments}).encode(),
        )[1]

    def during(sid):
        with monkeypatch.context() as mock:
            mock.setattr(toolservice, "MAX_RESULT", 100)
            assert call(sid, "lark_thread", scope="chat")["error"] == "result_too_large"
        assert call(sid, "lark_read", url=DOC)["error"] == "link_not_from_user"
        history = call(sid, "lark_thread", scope="chat")
        assert history["ok"]
        assert call(sid, "lark_read", url=app_doc)["error"] == "link_not_from_user"
        assert call(sid, "lark_read", url=cross_doc)["error"] == "link_not_from_user"
        assert (
            call(sid, "lark_file_edit", url=DOC, action="append", content="edit")[
                "error"
            ]
            == "link_not_shared"
        )
        assert call(sid, "lark_read", url=DOC)["result"]["content"].endswith("LIMIT 5")
        reader.org_readable = False
        assert call(sid, "lark_read", url=DOC)["error"] == "lark_permission_unverified"
        reader.latest = dict(src, content={"text": "The file link was removed"})
        assert (
            call(sid, "lark_read", url=DOC)["error"]
            == "lark_history_source_unavailable"
        )

    model.hook = during
    ask(svc, "u", "Can you show five samples from the query in this page?", chat=group)
    drain(svc, transport)
    assert reader.docs == [DOC]

    def later(sid):
        assert call(sid, "lark_read", url=DOC)["error"] == "link_not_from_user"

    model.hook = later
    ask(svc, "u", "Can you check something else?", chat=group)
    drain(svc, transport)
    assert reader.docs == [DOC]
