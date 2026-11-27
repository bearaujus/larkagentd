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
    assert service.decide(turn, "lark_file_create") == Decision(False, "denied")
    service.group_decision.assert_not_called()
    assert service.decide(turn, "lark_file_read") == Decision(False, "group_scope")
    service.group_decision.assert_called_once_with(turn, "lark_file_read")


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
