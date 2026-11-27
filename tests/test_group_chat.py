import itertools

from test_agent import FakeAgent
from test_card_lifecycle import Transport, drain
from test_core import setup
from test_core import store as store_fixture

store = store_fixture

GROUP = "grp"
ids = itertools.count(1)


def room(store, reply="answer"):
    svc, _ = setup(store)
    agent = FakeAgent(reply)
    svc.agent = agent
    return svc, agent, Transport()


def direct(svc, t, user, chat, text="/help"):
    """A person's first direct message: it is where the bot learns their direct chat."""
    n = next(ids)
    svc.receive("t", user, chat, f"dev{n}", f"dm{n}", text, thread=f"dm{n}")
    drain(svc, t)


def ask(svc, user, text, message_id=None, thread=None, chat=GROUP):
    """A message in a group that mentions the bot (the adapter has already checked that)."""
    message_id = message_id or f"g{next(ids)}"
    svc.receive(
        "t",
        user,
        chat,
        f"gev-{message_id}",
        message_id,
        text,
        thread=thread or message_id,
        in_thread=thread is not None,
        group=True,
    )
    return message_id


def owner_state(store, owner):
    with store.user(owner) as (_, s):
        return dict(s)


def to(t, chat):
    return [s for s in t.sent if s.get("chat") == chat]


def texts(t, chat):
    return [s.get("text") for s in to(t, chat)]


def test_passive_learning_saves_grounded_fact_with_successful_turn(store):
    import json
    from dataclasses import replace
    from datetime import datetime, timezone

    from gb_fakes import message
    from sqlalchemy import func, select

    from larkagentd.knowledge.models import GroupBrainConfig
    from larkagentd.knowledge.runtime import GroupBrainRuntime
    from larkagentd.knowledge.store import KnowledgeStore
    from larkagentd.knowledge.tables import gb_memory
    from larkagentd.toolservice.registry import ToolRegistry
    from larkagentd.toolservice.tools import TOOLS
    from larkagentd.toolservice.toolservice import ToolService

    svc, agent, transport = room(store)
    config = GroupBrainConfig(enabled=True)
    scope = "group:grp"
    knowledge = KnowledgeStore()
    fact = "All release checklists require restoring a backup in staging."

    class LiveSource:
        text = fact

        def messages(self, chat_id, ids):
            assert chat_id == GROUP and len(ids) == 1
            return [
                replace(
                    message(ids[0], chat_id=GROUP, sender_id="u", text=self.text),
                    created_at=datetime.now(timezone.utc),
                )
            ]

    source = LiveSource()
    runtime = GroupBrainRuntime(store.engine, config, source=source)
    svc.access.knowledge_runtime = runtime
    svc.group_brain = config
    registry = ToolRegistry(store)
    registry.sync(TOOLS)
    tools = ToolService(store, svc, "test-key", registry=registry, group_brain=runtime)
    with store.engine.begin() as conn:
        knowledge.update_settings(
            conn,
            scope,
            0,
            {
                "enabled": True,
                "documents_enabled": False,
                "recall_enabled": False,
                "notice_ref": "notice",
                "policy_ref": "policy",
            },
            "admin",
        )
    assert runtime.effective(scope).state == "enabled"

    def count_facts():
        with store.engine.connect() as conn:
            return conn.scalar(select(func.count()).select_from(gb_memory))

    def propose(session_id):
        status, response = tools.handle(
            "Bearer test-key",
            "group_memory_propose",
            json.dumps(
                {
                    "session_id": session_id,
                    "arguments": {
                        "category": "procedure",
                        "content": fact,
                        "evidence": fact,
                    },
                }
            ),
        )
        assert status == 200 and response["result"]["status"] == "queued"
        assert count_facts() == 0

    # A stale excerpt still allows delivery of the normal answer.
    agent.hook = propose
    source.text = "This release follows a different checklist."
    ask(svc, "u", fact)
    drain(svc, transport)
    assert count_facts() == 0
    assert "answer" in texts(transport, GROUP)

    source.text = fact
    ask(svc, "u", fact)
    drain(svc, transport)
    assert agent.requests[-1]["passive_learning"] is True
    assert count_facts() == 1
    with store.engine.connect() as conn:
        assert conn.execute(select(gb_memory.c.group_key, gb_memory.c.state)).one() == (
            scope,
            "active",
        )

    def recall(session_id):
        status, response = tools.handle(
            "Bearer test-key",
            "group_knowledge_search",
            json.dumps(
                {
                    "session_id": session_id,
                    "arguments": {"query": "release checklists", "kinds": ["memory"]},
                }
            ),
        )
        assert status == 200
        items = response["result"]["results"]
        assert [item["text"] for item in items] == [fact]
        assert items[0]["sources"][0]["verified"] is True

    agent.hook = recall
    ask(svc, "u", "What goes on our release checklist?")
    drain(svc, transport)
    assert count_facts() == 1


def test_group_turn_uses_one_activity_message_and_one_code_reply_with_usage(store):
    import json
    from types import SimpleNamespace

    from larkagentd.lark.adapter import Lark

    svc, agent, transport = room(store, "Here is the query.\n\n```sql\nSELECT 1\n```")
    agent.events = [
        ("said", None, {"text": "private draft"}),
        ("started", "lark_read", {"call": "a", "preview": "private arguments"}),
        ("started", "run_query", {"call": "b"}),
        ("completed", "run_query", {"call": "b", "error": True}),
        ("completed", "lark_read", {"call": "a"}),
        ("completed", "run_query", {"call": "b", "error": True}),
        ("steered", None),
    ]
    agent.tokens = {
        "input_tokens": 100,
        "cache_read_tokens": 1900,
        "cache_write_tokens": 0,
        "output_tokens": 50,
    }
    origin = ask(svc, "u", "Show me the query")
    drain(svc, transport)
    opened, updated = transport.progress
    assert opened[:3] == ("open", GROUP, origin)
    assert updated[:3] == ("update", opened[3], "interactive")
    status, steps = updated[3]["body"]["elements"]
    assert (
        "Reply ready" in status["content"] and "2 steps · 1 issue" in status["content"]
    )
    assert steps["expanded"] is False
    lines = steps["elements"][0]["content"]
    assert "Request updated" in lines
    assert "Read Lark file · Done" in lines and "Run data query · Issue" in lines
    assert "private" not in lines

    (answer,) = transport.sent
    assert answer["reply_to"] == origin and answer["thread"]
    card = json.loads(answer["preview_card"])
    body = card["body"]["elements"]
    assert "SELECT 1" in body[0]["content"]
    usage = body[-1]
    assert usage["expanded"] is False
    assert usage["header"]["title"]["content"] == "Usage · ~2.1K tokens"
    assert usage["elements"][0]["content"] == "100 input · 1.9K cached · 50 output"

    delivered = []

    def send(chat, target, kind, content, key, thread):
        if kind == "interactive":
            raise RuntimeError("card unavailable")
        assert chat == GROUP and target == origin and thread
        delivered.append(json.loads(content))
        return "answer"

    adapter = SimpleNamespace(deliver=send, send_parts=lambda *_: None)
    assert Lark.send(adapter, answer, "test") == "answer"
    (post,) = delivered
    rows = post["zh_cn"]["content"]
    assert rows[-2][0]["text"].endswith("```")
    assert "2.1K tokens" in rows[-1][0]["text"]


def test_long_code_is_an_attachment_on_the_same_final_reply(store):
    import json
    from types import SimpleNamespace

    from larkagentd.lark.adapter import Lark

    code = "\n".join(f"SELECT {i} AS item_name" for i in range(800))
    svc, _, transport = room(store, "```sql\n" + code + "\n```")
    origin = ask(svc, "u", "Show the SQL")
    drain(svc, transport)
    assert [event[0] for event in transport.progress] == ["open", "update"]
    (answer,) = transport.sent
    assert answer["attachment_keys"] == ["file_code-1.sql"]

    delivered = []

    def deliver(chat, target, kind, content, key, thread):
        assert (chat, target, kind, thread) == (GROUP, origin, "post", True)
        delivered.append(json.loads(content))
        return "answer"

    adapter = SimpleNamespace(deliver=deliver, send_parts=lambda *_: None)
    assert Lark.send(adapter, answer, "test") == "answer"
    (post,) = delivered
    assert post["files"] == [{"key": "file_code-1.sql"}]
    assert "full code is attached" in json.dumps(post["zh_cn"])


def test_csv_attachment_count_is_added_from_the_turn_artifact(store):
    from larkagentd.core.artifacts import Artifacts

    svc, agent, transport = room(store, "Filtered to the requested channel.")
    svc.artifacts = Artifacts(store, svc.vault)
    agent.hook = lambda sid: svc.artifacts.put_file(
        "t:u",
        sid,
        "results.csv",
        "id\n1\n2\n",
        rows=2,
        columns=1,
        note="The export reached the requested row limit.",
    )
    origin = ask(svc, "u", "Export two rows as CSV")
    drain(svc, transport)
    (answer,) = transport.sent
    assert answer["reply_to"] == origin and answer["thread"]
    assert answer["attachment_keys"] == ["file_results.csv"]
    assert "**CSV attached** · 2 data rows · 1 column" in answer["text"]
    assert "The export reached the requested row limit." in answer["text"]


def test_single_query_estimate_denial_uses_the_recorded_outcome(store):
    import time

    from sqlalchemy import select

    from larkagentd.core.store import turns
    from larkagentd.toolservice import activity

    svc, agent, transport = room(store, "I ran the query, but it was blocked.")
    agent.events = [
        ("started", "run_query", {"call": "query"}),
        ("completed", "run_query", {"call": "query", "error": True}),
    ]

    def deny_estimate(session_id):
        with store.engine.connect() as conn:
            turn = (
                conn.execute(select(turns).where(turns.c.session_id == session_id))
                .mappings()
                .one()
            )
        activity_id = activity.start(store, turn, "run_query", "p_test", "query")
        activity.finish(
            store,
            activity_id,
            time.monotonic(),
            "failed",
            error="maxcompute_estimate_denied",
        )

    agent.hook = deny_estimate
    ask(svc, "u", "Run a test query")
    drain(svc, transport)
    (answer,) = transport.sent
    assert "before the query was submitted" in answer["text"]
    assert "I ran the query" not in answer["text"]


def test_allowlisted_app_gets_a_group_reply_under_its_own_principal(store):
    svc, agent, transport = room(store, "Hello from Maxx")
    svc.lark_app_senders = frozenset({"cli_fixture"})
    sender, message = "cli_fixture", "om_app"
    for _ in range(2):
        svc.receive(
            "t",
            sender,
            GROUP,
            "app-message:" + message,
            message,
            "hello",
            thread=message,
            group=True,
        )
    drain(svc, transport)
    (answer,) = transport.sent
    assert answer["reply_to"] == message
    assert len(agent.calls) == 1
    assert not owner_state(store, "t:cli_fixture").get("credential_version")


def test_revoking_an_app_discards_its_queued_turn(store):
    svc, agent, transport = room(store)
    svc.lark_app_senders = frozenset({"cli_fixture"})
    svc.receive(
        "t",
        "cli_fixture",
        GROUP,
        "event",
        "om_app",
        "hello",
        thread="om_app",
        group=True,
    )
    svc.lark_app_senders = frozenset()
    drain(svc, transport)
    assert not agent.calls and not transport.sent


def test_interrupted_recovery_gets_one_specific_reply_and_folded_usage(store):
    import json

    from larkagentd.gateway.hermes import AgentError
    from larkagentd.gateway.main import run_job

    svc, model, transport = room(store)
    model.script = [AgentError("unknown_execution"), AgentError("unknown_execution")]
    model.tokens = {
        "input_tokens": 10,
        "cache_read_tokens": 25,
        "cache_write_tokens": 10,
        "output_tokens": 5,
    }
    origin = ask(svc, "u", "Find the example")
    activated = svc.store.claim()
    run_job(svc, activated, transport)
    drain(svc, transport)
    started, finished = transport.progress
    assert finished[1] == started[3]
    assert "header" not in finished[3]
    details = finished[3]["body"]["elements"]
    assert "Session interrupted" in details[0]["content"]
    assert "Session restarted" in details[1]["elements"][0]["content"]
    (answer,) = transport.sent
    assert answer["reply_to"] == origin
    assert "interrupted" in answer["text"]
    elements = json.loads(answer["preview_card"])["body"]["elements"]
    assert elements[-1]["header"]["title"]["content"] == "Usage · 100 tokens"
    assert "2 attempts" in elements[-1]["elements"][0]["content"]
