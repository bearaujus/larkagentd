from datetime import timedelta

from gb_fakes import message
from sqlalchemy import insert, select
from test_core import store as store_fixture

from larkagentd.core.store import gateway_policies
from larkagentd.knowledge.catalog import Catalog
from larkagentd.knowledge.discussions import reconstruct
from larkagentd.knowledge.tables import gb_discussions, gb_messages

store = store_fixture
GROUP = "group:oc_example1234"


def register(conn):
    conn.execute(insert(gateway_policies).values(scope=GROUP, mode="all_users"))


def test_upsert_is_idempotent_and_revisions_edits(store):
    catalog = Catalog()
    source = message("om_catalog0001", text="GTID_PURGED is needed")
    with store.engine.begin() as conn:
        register(conn)
        first = catalog.upsert_messages(conn, "group:oc_example1234", [source])
        second = catalog.upsert_messages(conn, "group:oc_example1234", [source])
        changed = catalog.upsert_messages(
            conn,
            "group:oc_example1234",
            [message("om_catalog0001", text="GTID_PURGED changed")],
        )
        row = (
            conn.execute(
                gb_messages.select().where(
                    gb_messages.c.id == first[source.message_id][0]
                )
            )
            .mappings()
            .one()
        )
    assert first[source.message_id][1] is True
    assert second[source.message_id][1] is False
    assert changed[source.message_id][1] is True
    assert row["revision"] == 2


def test_blocked_message_is_persisted_without_text(store):
    catalog = Catalog()
    private = "contact " + "alice" + "@example.com"
    source = message("om_catalog0002", text=private)
    with store.engine.begin() as conn:
        register(conn)
        result = catalog.upsert_messages(conn, "group:oc_example1234", [source])
        row = (
            conn.execute(
                gb_messages.select().where(
                    gb_messages.c.id == result[source.message_id][0]
                )
            )
            .mappings()
            .one()
        )
    assert row["sanitized_text"] is None
    assert row["visibility_status"] == "blocked"
    assert private not in repr(row)


def test_save_discussion_is_idempotent_and_group_scoped(store):
    catalog = Catalog()
    source = message(
        "om_catalog0003",
        thread_id="omt_thread01",
        sender_id="ou_alice0001",
        text="Exact GTID_PURGED identifier appears here.",
    )
    texts = {source.message_id: source.text}
    discussion = reconstruct([source], thread_complete={source.thread_id})[0]
    with store.engine.begin() as conn:
        register(conn)
        catalog.upsert_messages(conn, "group:oc_example1234", [source], texts)
        first = catalog.save_discussion(conn, "group:oc_example1234", discussion, texts)
        same = catalog.save_discussion(conn, "group:oc_example1234", discussion, texts)
        found = catalog.lexical_search(
            conn,
            "group:oc_example1234",
            ["identifier"],
            ["gtid_purged"],
            requester_id="ou_alice0001",
            limit=10,
        )
        isolated = catalog.lexical_search(
            conn,
            "group:oc_elsewhere",
            ["identifier"],
            ["gtid_purged"],
            limit=10,
        )
    assert first["changed"] and first["revision"] == 1
    assert same["changed"] is False
    assert found and found[0]["participant_match"] is True
    assert isolated == []


def test_native_thread_participant_and_date_bounds_use_catalog_metadata(store):
    catalog = Catalog()
    messages = [
        message(
            "om_boundary0001",
            thread_id="omt_thread01",
            sender_id="ou_alice0001",
            minute=10,
            text="Question about replication",
        ),
        message(
            "om_boundary0002",
            thread_id="omt_thread01",
            root_id="om_boundary0001",
            parent_id="om_boundary0001",
            sender_id="ou_bob0001",
            minute=11,
            text="GTID_PURGED is the resolution",
        ),
    ]
    discussion = reconstruct(messages, thread_complete={"omt_thread01"})[0]
    texts = {item.message_id: item.text for item in messages}
    with store.engine.begin() as conn:
        register(conn)
        catalog.upsert_messages(conn, GROUP, messages, texts)
        saved = catalog.save_discussion(conn, GROUP, discussion, texts)
        at_start = catalog.lexical_search(
            conn,
            GROUP,
            ["resolution"],
            [],
            start=discussion.ended_at,
            end=discussion.started_at + timedelta(microseconds=1),
            self_only=True,
            requester_id="ou_alice0001",
        )
        after_start = catalog.lexical_search(
            conn,
            GROUP,
            ["resolution"],
            [],
            start=discussion.ended_at + timedelta(microseconds=1),
            requester_id="ou_alice0001",
        )
        at_end = catalog.lexical_search(
            conn,
            GROUP,
            ["resolution"],
            [],
            end=discussion.started_at,
            requester_id="ou_alice0001",
        )
        row = (
            conn.execute(
                select(gb_discussions).where(
                    gb_discussions.c.id == saved["discussion_id"]
                )
            )
            .mappings()
            .one()
        )
    assert row["kind"] == "native_thread"
    assert row["native_thread_id"] == "omt_thread01"
    assert at_start and at_start[0]["participant_match"] is True
    assert after_start == []
    assert at_end == []
