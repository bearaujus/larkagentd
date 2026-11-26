import hashlib

import pytest
from sqlalchemy import insert

from larkagentd.core.store import gateway_identities, gateway_policies
from larkagentd.knowledge.dashboard_review import MemoryReview
from larkagentd.knowledge.memory import Memory
from larkagentd.knowledge.models import GroupBrainConfig
from larkagentd.knowledge.providers.base import namespace_for
from larkagentd.knowledge.providers.fake import FakeIndex
from larkagentd.knowledge.runtime import GroupBrainRuntime
from larkagentd.knowledge.store import KnowledgeStore
from larkagentd.knowledge.tables import (
    gb_index_entries,
    gb_memory,
    gb_memory_revisions,
)

pytest_plugins = ("test_core",)


@pytest.fixture
def groups(store):
    with store.engine.begin() as conn:
        conn.execute(
            insert(gateway_policies).values(scope="group:other", mode="all_users")
        )
    return store


def test_proposal_is_pending_and_identical_content_is_deduplicated(groups):
    memory = Memory()
    with groups.engine.begin() as conn:
        first = memory.propose(
            conn,
            "group:grp",
            "ou_alice0001",
            "The review window is 14 days.",
            "standard",
        )
        second = memory.propose(
            conn,
            "group:grp",
            "ou_bob000001",
            "The review window is 14 days.",
            "standard",
        )
    assert first == {
        "memory_id": first["memory_id"],
        "revision": 1,
        "state": "pending",
        "duplicate": False,
    }
    assert second["memory_id"] == first["memory_id"] and second["duplicate"] is True


def test_sensitive_proposal_is_refused_without_storing_text(groups):
    with groups.engine.begin() as conn:
        result = Memory().propose(
            conn,
            "group:grp",
            "ou_alice0001",
            "Contact alice@example.com about the review.",
            "lesson",
        )
        rows = conn.execute(gb_memory.select()).all()
    assert result == {"state": "refused", "reason": "sensitive"}
    assert rows == []


def test_correction_is_a_new_pending_revision_without_hiding_active_fact(groups):
    memory = Memory()
    with groups.engine.begin() as conn:
        original = memory.propose(
            conn,
            "group:grp",
            "ou_alice0001",
            "The review window is 14 days.",
            "standard",
        )
        digest = hashlib.sha256(b"The review window is 14 days.").hexdigest()
        memory.approve(
            conn,
            "group:grp",
            original["memory_id"],
            1,
            digest,
            "reviewer@example.com",
            True,
        )
        corrected = memory.correct(
            conn,
            "group:grp",
            "ou_alice0001",
            original["memory_id"],
            "The review window is 21 days.",
            "Updated policy",
        )
        rows = (
            conn.execute(
                gb_memory_revisions.select()
                .where(gb_memory_revisions.c.memory_id == original["memory_id"])
                .order_by(gb_memory_revisions.c.revision)
            )
            .mappings()
            .all()
        )
        canonical = (
            conn.execute(
                gb_memory.select().where(gb_memory.c.id == original["memory_id"])
            )
            .mappings()
            .one()
        )
    assert corrected["revision"] == 2 and corrected["state"] == "pending"
    assert [row["sanitized_content"] for row in rows] == [
        "The review window is 14 days.",
        "The review window is 21 days.",
    ]
    assert rows[0]["status"] == "active" and rows[1]["status"] == "pending"
    assert canonical["state"] == "active" and canonical["current_revision"] == 1


def test_unknown_or_cross_group_correction_looks_not_found(groups):
    memory = Memory()
    with groups.engine.begin() as conn:
        found = memory.propose(
            conn,
            "group:grp",
            "ou_alice0001",
            "The review window is 14 days.",
            "standard",
        )
        result = memory.correct(
            conn,
            "group:other",
            "ou_alice0001",
            found["memory_id"],
            "Different text.",
            "Correction",
        )
    assert result == {"status": "not_found"}


def test_approval_requires_confirmation_and_exact_hash(groups):
    memory = Memory()
    with groups.engine.begin() as conn:
        proposed = memory.propose(
            conn,
            "group:grp",
            "ou_alice0001",
            "The review window is 14 days.",
            "standard",
        )
        content_hash = hashlib.sha256(b"The review window is 14 days.").hexdigest()
        no_ack = memory.approve(
            conn,
            "group:grp",
            proposed["memory_id"],
            1,
            content_hash,
            "reviewer@example.com",
            False,
        )
        wrong_hash = memory.approve(
            conn,
            "group:grp",
            proposed["memory_id"],
            1,
            "0" * 64,
            "reviewer@example.com",
            True,
        )
        approved = memory.approve(
            conn,
            "group:grp",
            proposed["memory_id"],
            1,
            content_hash,
            "reviewer@example.com",
            True,
        )
        again = memory.approve(
            conn,
            "group:grp",
            proposed["memory_id"],
            1,
            content_hash,
            "reviewer@example.com",
            True,
        )
        stored = (
            conn.execute(
                gb_memory.select().where(gb_memory.c.id == proposed["memory_id"])
            )
            .mappings()
            .one()
        )
    assert no_ack["status"] == "sharing_confirmation_required"
    assert wrong_hash["status"] == "hash_mismatch"
    assert approved == {"status": "active", "lexical_only": True}
    assert again["status"] == "not_pending"
    assert stored["state"] == "active" and stored["current_revision"] == 1


def test_reviewer_must_have_verified_group_identity_and_participation(groups):
    with groups.engine.begin() as conn:
        conn.execute(
            insert(gateway_identities),
            [
                {
                    "owner": "tn_example:ou_alice0001",
                    "email": "alice@example.com",
                    "verified_at": 1,
                },
                {
                    "owner": "tn_example:ou_outsider0001",
                    "email": "outsider@example.com",
                    "verified_at": 1,
                },
            ],
        )
        proposed = Memory().propose(
            conn,
            "group:grp",
            "ou_alice0001",
            "The review window is 14 days.",
            "standard",
        )
        member_can_read = Memory.reviewer_can_read(
            conn, "group:grp", "alice@example.com", proposed["memory_id"]
        )
        outsider_can_read = Memory.reviewer_can_read(
            conn, "group:grp", "outsider@example.com", proposed["memory_id"]
        )
    assert member_can_read is True
    assert outsider_can_read is False


def test_only_source_authorized_admin_sees_candidate_text(groups):
    with groups.engine.begin() as conn:
        conn.execute(
            insert(gateway_identities).values(
                owner="tn_example:ou_alice0001",
                email="alice@example.com",
                verified_at=1,
            )
        )
        Memory().propose(
            conn,
            "group:grp",
            "ou_alice0001",
            "The review window is 14 days.",
            "standard",
        )
        review = MemoryReview()
        manager_item = review.candidates(
            conn, "group:grp", {"role": "manager", "email": "alice@example.com"}
        )[0]
        admin_item = review.candidates(
            conn, "group:grp", {"role": "admin", "email": "alice@example.com"}
        )[0]
    assert "content" not in manager_item
    assert admin_item["content"] == "The review window is 14 days."


def test_search_ignores_semantic_hits_without_active_current_mapping(groups):
    config = GroupBrainConfig(
        enabled=True,
        embedding_base_url="https://example.com",
        embedding_api_key="x",
        embedding_dimensions=16,
    )
    index = FakeIndex(dimensions=16)
    runtime = GroupBrainRuntime(groups.engine, config, index=index)
    with groups.engine.begin() as conn:
        settings = KnowledgeStore().update_settings(
            conn,
            "group:grp",
            0,
            {"enabled": True, "notice_ref": "notice", "policy_ref": "policy"},
            "admin@example.com",
        )
        proposed = Memory().propose(
            conn,
            "group:grp",
            "ou_alice0001",
            "The review window is fourteen days.",
            "standard",
        )
        digest = hashlib.sha256(b"The review window is fourteen days.").hexdigest()
        Memory().approve(
            conn,
            "group:grp",
            proposed["memory_id"],
            1,
            digest,
            "reviewer@example.com",
            True,
        )
        namespace = namespace_for(config.collection, "group:grp", "memory")
        valid_id = index.add(
            namespace=namespace,
            text="The review window is fourteen days.",
            metadata={"kind": "memory"},
        )[0]
        index.add(
            namespace=namespace,
            text="Uncatalogued vector about unmatchedtermzz.",
            metadata={"kind": "memory", "canonical_id": "mem_orphan"},
        )
        conn.execute(
            gb_index_entries.insert().values(
                id="entry_valid",
                group_key="group:grp",
                kind="memory",
                canonical_id=proposed["memory_id"],
                source_revision=1,
                chunk_ordinal=0,
                namespace=namespace,
                provider_id=valid_id,
                idempotency_key="valid-entry-key",
                generation=settings["generation"],
                state="active",
                content_hash=digest,
                embedding_model=config.embedding_model,
                dimensions=16,
            )
        )
        result = Memory(runtime).search(
            runtime, conn, "group:grp", "unmatchedtermzz", limit=5
        )
    assert [row["memory_id"] for row in result["results"]] == [proposed["memory_id"]]
