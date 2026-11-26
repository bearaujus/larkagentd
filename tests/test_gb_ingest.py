from dataclasses import replace
from datetime import datetime, timedelta, timezone

from gb_fakes import FakeGroupSource, message
from sqlalchemy import insert, select
from test_core import store as store_fixture

from larkagentd.core.store import gateway_policies
from larkagentd.knowledge.lark_sources import Page
from larkagentd.knowledge.models import GroupBrainConfig
from larkagentd.knowledge.runtime import GroupBrainRuntime
from larkagentd.knowledge.store import KnowledgeStore
from larkagentd.knowledge.tables import (
    gb_discussions,
    gb_jobs,
    gb_messages,
    group_knowledge_settings,
)
from larkagentd.knowledge.worker import KnowledgeWorker

store = store_fixture
GROUP = "group:oc_example1234"
kb = KnowledgeStore()


def test_ingest_persists_blocked_source_without_plaintext(store):
    secret_text = "contact " + "alice" + "@example.com"
    source_message = replace(
        message("om_ingest0001", text=secret_text),
        created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
    )
    source = FakeGroupSource(
        {("chat", "oc_example1234", None): Page((source_message,), None, True)}
    )
    with store.engine.begin() as conn:
        conn.execute(insert(gateway_policies).values(scope=GROUP, mode="all_users"))
        settings = kb.update_settings(
            conn,
            GROUP,
            0,
            {
                "enabled": True,
                "notice_ref": "notice-example",
                "policy_ref": "policy-example",
            },
            "ou_admin0001",
        )
        kb.enqueue(
            conn,
            GROUP,
            "ingest_message",
            "synthetic-ingest",
            settings["generation"],
            ref_payload={
                "message_id": source_message.message_id,
                "thread_id": None,
                "reason": "learn",
            },
        )
    config = GroupBrainConfig(enabled=True)
    runtime = GroupBrainRuntime(store.engine, config, source=source)
    worker = KnowledgeWorker(runtime, "worker-example")
    assert worker.run_once()
    with store.engine.begin() as conn:
        stored = conn.execute(select(gb_messages)).mappings().one()
        job = conn.execute(select(gb_jobs)).mappings().one()
        discussions = conn.execute(select(gb_discussions)).all()
    assert stored["visibility_status"] == "blocked"
    assert stored["sanitized_text"] is None
    assert secret_text not in repr(stored)
    assert job["state"] == "succeeded"
    assert discussions == []


def test_ingest_persists_only_selected_discussion_from_window(store):
    now = datetime.now(timezone.utc)
    selected = replace(
        message("om_ingest0002", text="selected discussion"),
        created_at=now - timedelta(minutes=2),
    )
    unrelated = replace(
        message("om_ingest0003", text="unrelated recent discussion"),
        created_at=now - timedelta(minutes=40),
    )
    source = FakeGroupSource(
        {("chat", "oc_example1234", None): Page((selected, unrelated), None, True)}
    )
    with store.engine.begin() as conn:
        conn.execute(insert(gateway_policies).values(scope=GROUP, mode="all_users"))
        settings = kb.update_settings(
            conn,
            GROUP,
            0,
            {
                "enabled": True,
                "notice_ref": "notice-example",
                "policy_ref": "policy-example",
            },
            "ou_admin0001",
        )
        conn.execute(
            group_knowledge_settings.update()
            .where(group_knowledge_settings.c.group_key == GROUP)
            .values(enabled_at=now - timedelta(days=1))
        )
        kb.enqueue(
            conn,
            GROUP,
            "ingest_message",
            "selected-window-message",
            settings["generation"],
            ref_payload={"message_id": selected.message_id, "reason": "learn"},
        )
    runtime = GroupBrainRuntime(
        store.engine, GroupBrainConfig(enabled=True), source=source
    )
    assert KnowledgeWorker(runtime, "worker-example").run_once()
    with store.engine.begin() as conn:
        messages = conn.execute(select(gb_messages)).mappings().all()
        discussions = conn.execute(select(gb_discussions)).mappings().all()
    assert [row["lark_message_id"] for row in messages] == [selected.message_id]
    assert len(discussions) == 1
    assert discussions[0]["root_message_id"] == selected.message_id
