import json
from types import SimpleNamespace as N

from sqlalchemy import text
from test_core import store as store_fixture

store = store_fixture


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
    from unittest.mock import Mock

    from larkagentd.lark.adapter import Lark

    adapter = Lark.__new__(Lark)
    adapter.service = Mock()
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
    from unittest.mock import Mock

    from larkagentd.lark.adapter import Lark

    adapter = Lark.__new__(Lark)
    adapter.service = Mock()
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
    from unittest.mock import Mock

    from larkagentd.lark.adapter import Lark

    adapter = Lark.__new__(Lark)
    adapter.service = Mock()
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
