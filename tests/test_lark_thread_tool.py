import json
from datetime import datetime, timezone

import pytest

from larkagentd.toolservice.larkcli import LarkError, LarkReader
from larkagentd.toolservice.tools import TOOLS, ToolContext, ToolError, validate


class FakeReader:
    def __init__(self):
        self.calls = []

    def thread(self, *args, **kwargs):
        self.calls.append(("thread", args, kwargs))
        return {"source": "thread"}

    def chat(self, *args, **kwargs):
        self.calls.append(("chat", args, kwargs))
        return {"source": "chat"}


def test_source_is_server_owned_and_model_cannot_supply_conversation_ids():
    with pytest.raises(ToolError):
        validate(TOOLS["lark_thread"].parameters, {"chat_id": "oc_abcdefgh"})
    reader = FakeReader()
    ctx = ToolContext(
        "tenant:ou_abcdefgh",
        "v1",
        object(),
        lark=reader,
        conversation={
            "in_thread": True,
            "thread": "omt_abcdefgh",
            "chat": "oc_abcdefgh",
        },
    )
    assert TOOLS["lark_thread"].run(ctx, {}) == {"source": "thread"}
    assert reader.calls[0][0] == "thread"


def test_missing_reader_and_upstream_forbidden_use_fixed_tool_errors(tmp_path):
    with pytest.raises(ToolError, match="not_configured"):
        TOOLS["lark_thread"].run(ToolContext("t:u", "v", object()), {})

    class Failed:
        returncode = 1
        stdout = ""
        stderr = '{"error":{"code":"forbidden","message":"private detail"}}'

    reader = LarkReader(
        ("lark-cli",), "app", "secret", tmp_path, run=lambda *a, **k: Failed()
    )
    with pytest.raises(LarkError, match="forbidden"):
        TOOLS["lark_thread"].run(
            ToolContext(
                "t:u",
                "v",
                object(),
                lark=reader,
                conversation={"in_thread": False, "chat": "oc_abcdefgh"},
            ),
            {},
        )


def test_chat_source_and_missing_conversation():
    reader = FakeReader()
    ctx = ToolContext(
        "tenant:ou_abcdefgh",
        "v1",
        object(),
        lark=reader,
        conversation={"in_thread": False, "chat": "oc_abcdefgh"},
    )
    TOOLS["lark_thread"].run(ctx, {"page_token": "next_1234", "limit": 4})
    assert reader.calls[0][0] == "chat"
    assert reader.calls[0][1] == ("oc_abcdefgh", "next_1234", 4)
    with pytest.raises(ToolError, match="no_conversation"):
        TOOLS["lark_thread"].run(ToolContext("t:u", "v", object(), lark=reader), {})


def test_reader_builds_fixed_argv_and_returns_bounded_transcript(tmp_path):
    messages = [
        {
            "msg_type": "text",
            "create_time": "1794305520000",
            "sender": {"id": "ou_abcdefgh"},
            "content": json.dumps({"text": "hello"}),
        },
        {
            "msg_type": "image",
            "create_time": "1794305580000",
            "sender": {"id": "ou_ijklmnop"},
            "content": "{}",
        },
        {
            "msg_type": "text",
            "create_time": "1794305640000",
            "sender": {"id": "ou_abcdefgh"},
            "deleted": True,
            "content": "gone",
        },
        {
            "msg_type": "interactive",
            "create_time": "1794305700000",
            "sender": {"sender_type": "app"},
            "content": "{}",
        },
        {
            "msg_type": "text",
            "create_time": "1794305760000",
            "sender": {"id": "ou_abcdefgh"},
            "content": json.dumps({"text": "x" * 1600}),
        },
    ]
    seen = []

    class Completed:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def run(argv, **kwargs):
        seen.append(argv)
        return Completed(
            json.dumps(
                {
                    "data": {
                        "messages": messages,
                        "has_more": True,
                        "page_token": "page_1234",
                    }
                }
            )
        )

    reader = LarkReader(("lark-cli",), "app", "secret", tmp_path, run=run)
    result = reader.thread("omt_abcdefgh", "page_1234", 5, asker="ou_abcdefgh")
    assert seen[0][1:3] == ["im", "+threads-messages-list"]
    assert "--download-resources" not in seen[0] and "--page-all" not in seen[0]
    assert result["source"] == "thread" and result["has_more"] is True
    assert result["next_page_token"] == "page_1234"
    assert [m["person"] for m in result["messages"]] == [
        "Person 1 (the person asking you)",
        "Person 2",
        "Assistant",
        "Person 1 (the person asking you)",
    ]
    assert [m["text"] for m in result["messages"]][1:3] == ["[image]", "[card]"]
    assert len(result["messages"][-1]["text"]) == 1500 and result["truncated"]
    assert result["messages"][0]["time"] == datetime.fromtimestamp(
        1794305520, timezone.utc
    ).strftime("%Y-%m-%d %H:%M")
    assert (
        "untrusted conversation" in result["note"]
        and "transcript only" in result["note"]
    )


def test_reader_validates_server_ids_and_page_token(tmp_path):
    reader = LarkReader(
        ("lark-cli",), "app", "secret", tmp_path, run=lambda *a, **k: None
    )
    for bad_id in ("om_123", "omt_bad-id", "oc_123"):
        with pytest.raises(LarkError):
            reader.thread(bad_id)
    with pytest.raises(LarkError, match="bad_page_token"):
        reader.chat("oc_abcdefgh", "bad token")


def test_first_group_history_and_followup_cursor_keep_native_thread_link(tmp_path):
    cursor = "ab_" * 41 + "xyz=="
    chat_id = "oc_abcdefgh"
    thread_id = "omt_abcdefgh"
    url = (
        f"https://applink.larksuite.com/client/thread/open?open_chat_id={chat_id}"
        f"&open_thread_id={thread_id}&thread_position=2"
    )
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        first = len(commands) == 1
        rows = [
            {
                "chat_id": chat_id,
                "create_time": "2026-01-01 00:00",
                "msg_type": "text",
                "content": {
                    "text": "First visible message" if first else "Second message"
                },
                "sender": {"name": "Verified Sender", "sender_type": "user"},
                "message_app_link": url,
            }
        ]
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stderr": "",
                "stdout": json.dumps(
                    {
                        "data": {
                            "messages": rows,
                            "has_more": first,
                            "page_token": cursor if first else None,
                        }
                    }
                ),
            },
        )()

    ctx = ToolContext(
        "t:reader",
        None,
        object(),
        lark=LarkReader(("lark-cli",), "app", "secret", tmp_path, run=run),
        conversation={"chat": chat_id, "in_thread": True, "thread": thread_id},
    )
    options = {"scope": "chat", "order": "asc", "limit": 1, "show_names": True}
    run_tool = TOOLS["lark_thread"].run
    first = run_tool(ctx, options)
    assert first["messages"] == [
        {
            "person": "Verified Sender",
            "text": "First visible message",
            "time": "2026-01-01 00:00",
            "url": url,
        }
    ]
    assert first["next_page_token"] == cursor
    second = run_tool(ctx, {**options, "page_token": first["next_page_token"]})
    assert second["messages"][0]["text"] == "Second message"
    assert all("+chat-messages-list" in argv and "asc" in argv for argv in commands)
    assert commands[1][commands[1].index("--page-token") + 1] == cursor
