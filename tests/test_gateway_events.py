import json
from types import SimpleNamespace as N

import pytest
from sqlalchemy import text
from test_core import store as store_fixture

store = store_fixture


def message_adapter(allowed=("user",)):
    from unittest.mock import Mock

    from larkagentd.lark.adapter import Lark

    adapter = Lark.__new__(Lark)
    adapter.service = Mock()
    adapter.config = N(lark_allowed_senders=allowed, app_id="cli_receiver")
    adapter.bot_id = "ou_receiver"
    return adapter


def receive_with(svc, ids, event="e1", sender="ou_alice"):
    svc.receive("t", sender, "chat", event, "m-" + event, "hello", ids=ids)


def aliases_of(store, owner):
    with store.engine.connect() as conn:
        return conn.execute(
            text("select state->'aliases' from users where id = :o"), {"o": owner}
        ).scalar_one()


def test_the_gateway_saves_the_senders_other_lark_ids_from_their_message(store):
    from test_core import setup

    svc, _ = setup(store)
    receive_with(svc, ("emp12345", "on_union_alice", None, ""))
    assert aliases_of(store, "t:ou_alice") == ["emp12345", "on_union_alice"]
    receive_with(svc, ("emp12345", "on_other"), event="e2")
    assert aliases_of(store, "t:ou_alice") == ["emp12345", "on_other", "on_union_alice"]


def test_the_open_id_is_never_stored_as_its_own_alias_and_no_ids_adds_nothing(store):
    from test_core import setup

    svc, _ = setup(store)
    receive_with(svc, ("ou_alice", None))
    receive_with(svc, (), event="e2")
    assert aliases_of(store, "t:ou_alice") is None


def test_the_lark_message_event_hands_the_gateway_the_senders_ids():
    adapter = message_adapter()
    adapter.message(
        N(
            header=N(tenant_key="t", event_id="e"),
            event=N(
                sender=N(
                    sender_type="user",
                    sender_id=N(open_id="ou_a", user_id="emp1", union_id="on_a"),
                ),
                message=N(
                    chat_type="p2p",
                    message_type="text",
                    chat_id="c",
                    message_id="m",
                    content='{"text": "hi"}',
                ),
            ),
        )
    )
    call = adapter.service.receive.call_args
    assert call.args[1] == "ou_a" and call.kwargs["ids"] == ("emp1", "on_a")


def message_event(message_type, content):
    adapter = message_adapter()
    adapter.message(
        N(
            header=N(tenant_key="t", event_id="e"),
            event=N(
                sender=N(
                    sender_type="user",
                    sender_id=N(open_id="ou_a", user_id="emp1", union_id="on_a"),
                ),
                message=N(
                    chat_type="p2p",
                    message_type=message_type,
                    chat_id="c",
                    message_id="m",
                    content=content,
                ),
            ),
        )
    )
    return adapter.service.receive.call_args.args[5]


def delivered(message_type, content, **extra):
    adapter = message_adapter()
    adapter.message(
        N(
            header=N(tenant_key="t", event_id="e"),
            event=N(
                sender=N(
                    sender_type="user",
                    sender_id=N(open_id="ou_a", user_id="emp1", union_id="on_a"),
                ),
                message=N(
                    chat_type="p2p",
                    message_type=message_type,
                    chat_id="c",
                    message_id="m9",
                    content=content,
                    **extra,
                ),
            ),
        )
    )
    return adapter.service.receive.call_args


def test_the_adapter_hands_over_what_a_message_carries_and_what_it_replies_to():
    call = delivered("image", '{"image_key": "k1"}', parent_id="p1")
    assert call.kwargs["kind"] == "image"
    assert call.kwargs["attachments"] == [
        dict(kind="image", key="k1", name="", message_id="m9")
    ]
    assert call.kwargs["reply_to"] == "p1"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("", ("user",)),
        (" user, cli_allowed, user ", ("user", "cli_allowed")),
        ("cli_allowed", ("cli_allowed",)),
    ],
)
def test_lark_sender_allowlist_default_and_explicit_entries(
    monkeypatch, value, expected
):
    from larkagentd.config import _lark_senders

    monkeypatch.setenv("LARK_ALLOWED_SENDERS", value)
    assert _lark_senders("cli_receiver") == expected


@pytest.mark.parametrize("value", ["user,app", "*", "cli_receiver", "ou_user"])
def test_lark_sender_allowlist_rejects_wildcards_and_the_receiver(monkeypatch, value):
    from larkagentd.config import _lark_senders

    monkeypatch.setenv("LARK_ALLOWED_SENDERS", value)
    with pytest.raises(ValueError, match="LARK_ALLOWED_SENDERS"):
        _lark_senders("cli_receiver")


@pytest.mark.parametrize(
    "allowed, sender_type, chat_type, mention, source_id, source_chat, accept, read",
    [
        (("user",), "app", "group", True, "cli_allowed", "c", False, False),
        (("user", "cli_allowed"), "app", "group", True, "cli_allowed", "c", True, True),
        (("cli_allowed",), "bot", "topic_group", True, "cli_allowed", "c", True, True),
        (("cli_allowed",), "bot", "group", True, "cli_other", "c", False, True),
        (("cli_receiver",), "app", "group", True, "cli_receiver", "c", False, True),
        (("cli_allowed",), "app", "group", True, "cli_allowed", "other", False, True),
        (("cli_allowed",), "app", "group", False, "cli_allowed", "c", False, False),
        (("cli_allowed",), "app", "p2p", True, "cli_allowed", "c", False, False),
    ],
)
def test_app_event_requires_a_verified_allowed_sender_and_explicit_group_mention(
    allowed, sender_type, chat_type, mention, source_id, source_chat, accept, read
):
    from unittest.mock import Mock

    adapter = message_adapter(allowed)
    adapter.message_items = Mock(
        return_value=(
            N(
                chat_id=source_chat,
                sender=N(id=source_id, id_type="app_id", sender_type="app"),
            ),
            [],
        )
    )
    adapter.message(
        N(
            header=N(tenant_key="t", event_id="original-event"),
            event=N(
                sender=N(sender_type=sender_type, sender_id=N(open_id="ou_sender")),
                message=N(
                    chat_id="c",
                    chat_type=chat_type,
                    message_id="m",
                    message_type="text",
                    content='{"text": "@_user_1 hello"}',
                    mentions=[
                        N(id=N(open_id="ou_receiver"), name="Maxx", key="@_user_1")
                    ]
                    if mention
                    else [],
                ),
            ),
        )
    )
    assert adapter.message_items.called is read
    assert adapter.service.receive.called is accept
    if accept:
        call = adapter.service.receive.call_args
        assert call.args == ("t", "cli_allowed", "c", "app-message:m", "m", "hello")
        assert call.kwargs["ids"] == ()


def test_app_turn_is_limited_to_scoped_read_tools():
    from unittest.mock import Mock

    from larkagentd.toolservice.registry import Decision
    from larkagentd.toolservice.tools import TOOLS
    from larkagentd.toolservice.toolservice import ToolService

    service = ToolService.__new__(ToolService)
    service.tools = TOOLS
    service.registry = Mock()
    service.registry.decide.return_value = Decision(True, "ok")
    service.group_decision = Mock(return_value=Decision(False, "group_scope"))
    turn = {"owner": "t:cli_allowed"}
    for name in ("lark_file_create", "group_memory_propose", "group_memory_correct"):
        assert service.decide(turn, name) == Decision(False, "app_read_only")
    service.group_decision.assert_not_called()
    for name in ("lark_file_read", "group_knowledge_search", "lark_thread"):
        assert service.decide(turn, name) == Decision(False, "group_scope")
        service.group_decision.assert_called_with(turn, name)


def test_source_preview_forwards_the_verified_text_to_the_reply_thread():
    from unittest.mock import Mock

    adapter = message_adapter()
    target = N(chat_id="oc_current", thread_id="omt_current")
    source = N(
        chat_id="oc_current",
        msg_type="text",
        sender=N(sender_type="user"),
        body=N(content='{"text":"A decision from this group."}'),
    )
    adapter.message_items = Mock(
        side_effect=lambda origin: (
            (target, []) if origin == "om_request" else (source, [])
        )
    )
    adapter.client = Mock()
    forward = adapter.client.im.v1.message.merge_forward
    forward.return_value = N(
        success=lambda: True,
        data=N(message=N(message_id="om_forwarded")),
    )
    assert (
        adapter.forward_source("oc_current", "om_request", "om_source", "job-key")
        == "om_forwarded"
    )
    request = forward.call_args.args[0]
    assert request.receive_id_type == "thread_id"
    assert request.uuid == "job-key"
    assert request.body.receive_id == "omt_current"
    assert request.body.message_id_list == ["om_source"]
    forward.reset_mock()
    source.chat_id = "oc_elsewhere"
    assert (
        adapter.forward_source("oc_current", "om_request", "om_source", "job-key")
        is None
    )
    forward.assert_not_called()


def test_link_only_reply_includes_a_preview_and_readable_details():
    from larkagentd.lark.card import reply_card

    url = (
        "https://applink.larksuite.com/client/thread/open?"
        "open_chat_id=oc_current&open_thread_id=omt_current&thread_position=7"
    )
    markup = f"[Original Lark message]({url})"
    original = "The group is using a staged rollout. " * 20
    source = {
        "markup": markup,
        "label": "Original Lark message",
        "url": url,
        "excerpt": original,
        "author": "Ari",
        "time": "2026-11-27 04:00 UTC",
    }
    card = json.loads(reply_card(markup, markup, [], "", None, source=source))
    nodes = card["body"]["elements"]
    assert nodes[0]["content"] == "Here’s the original message."
    assert any(
        node.get("text", {}).get("content") == "Ari · 2026-11-27 04:00 UTC"
        for node in nodes
    )
    details = next(node for node in nodes if node["tag"] == "collapsible_panel")
    assert details["elements"][0]["text"]["content"] == original
    assert details["header"]["title"]["content"] == "Read the full message"
    assert url not in json.dumps(card)

    reply = f"They requested a staged rollout.\n\nOriginal message (04:00 UTC):\n>{original}\n\n{markup}"
    card_text = reply_card(reply, reply, [], "", None, source=source)
    nodes = json.loads(card_text)["body"]["elements"]
    assert nodes[0]["content"] == "They requested a staged rollout."


def test_native_copy_link_is_reused_only_from_the_current_evidence():
    from unittest.mock import Mock

    from larkagentd.lark.message_links import native_copied_reply

    url = "https://applink.larksuite.com/client/message/link/open?token=synthetic%3D"
    answer = f"Here is the note.\n\n[Original message]({url})"
    assert native_copied_reply(answer, "Share the last message") is None
    message = native_copied_reply(answer, f"Share this note: {url}")
    assert message == f"Here is the note.\n\n{url}"
    adapter = message_adapter()
    adapter.deliver = Mock(return_value="om_reply")
    payload = {
        "card": None,
        "chat": "oc_group",
        "reply_to": "om_origin",
        "thread": True,
        "text": answer,
        "single_reply": True,
        "native_reply": message,
    }
    assert adapter.send(payload, "job-key") == "om_reply"
    chat, anchor, kind, body, key = adapter.deliver.call_args.args
    assert (chat, anchor, kind, key) == ("oc_group", "om_origin", "text", "job-key")
    assert json.loads(body)["text"] == message


def test_a_reply_target_may_come_from_the_upper_message_and_is_otherwise_none():
    assert (
        delivered("text", '{"text": "x"}', upper_message_id="u1").kwargs["reply_to"]
        == "u1"
    )
    assert delivered("text", '{"text": "x"}').kwargs["reply_to"] is None
    assert delivered("text", '{"text": "x"}', parent_id="").kwargs["reply_to"] is None


def test_a_question_with_inline_code_is_read_as_a_question_not_rejected():
    """Any formatting (here a table name in a code span) makes Lark send a post, not text."""

    post = {
        "title": "",
        "content": [
            [
                {"tag": "text", "text": "when can I move the "},
                {
                    "tag": "text",
                    "text": "p_example.demo.sample_table",
                    "style": ["code"],
                },
                {"tag": "text", "text": " schedule earlier?"},
            ]
        ],
    }
    assert (
        message_event("post", json.dumps(post))
        == "when can I move the `p_example.demo.sample_table` schedule earlier?"
    )


def test_planned_restart_gates_new_turns_and_waits_for_reply_delivery(
    tmp_path, monkeypatch
):
    from larkagentd.gateway import drain
    from larkagentd.gateway.runtime_status import RuntimeStatus

    marker = tmp_path / "drain"
    monkeypatch.setattr(drain, "MARKER", marker)
    status = RuntimeStatus()
    with status.agent_work() as admitted:
        assert admitted
        marker.touch()
        assert status.snapshot()["active_turns"] == 1
        with status.agent_work() as next_turn:
            assert not next_turn
    assert status.snapshot()["active_turns"] == 0
    tick = [0]
    assert drain.wait_until_idle(
        status.snapshot,
        lambda: tick[0] < 2,
        clock=lambda: tick[0],
        sleep=lambda seconds: tick.__setitem__(0, tick[0] + seconds),
    )
    assert tick[0] == 3
    marker.unlink()
    with status.agent_work() as next_turn:
        assert next_turn
