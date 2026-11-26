from sqlalchemy import insert
from test_core import store as store_fixture

from larkagentd.core.store import gateway_policies
from larkagentd.knowledge.catalog import Catalog
from larkagentd.knowledge.models import GroupBrainConfig
from larkagentd.knowledge.providers.base import namespace_for
from larkagentd.knowledge.providers.fake import FakeIndex
from larkagentd.knowledge.retrieval import HybridRetrieval
from larkagentd.knowledge.runtime import GroupBrainRuntime
from larkagentd.knowledge.store import KnowledgeStore

store = store_fixture
GROUP = "group:oc_example1234"


def test_orphan_semantic_candidate_is_not_served(store):
    with store.engine.begin() as conn:
        conn.execute(insert(gateway_policies).values(scope=GROUP, mode="all_users"))
        KnowledgeStore().update_settings(
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
    config = GroupBrainConfig(
        enabled=True,
        embedding_base_url="https://example.com/embeddings",
        embedding_api_key="key-" + "x" * 24,
    )
    index = FakeIndex()
    namespace = namespace_for(config.collection, GROUP, "discussion")
    index.add(
        namespace=namespace,
        text="GTID_PURGED resolution",
        metadata={"canonical_id": "forged"},
    )
    runtime = GroupBrainRuntime(store.engine, config, index=index)
    candidates, coverage = HybridRetrieval(runtime).search(
        GROUP,
        "GTID_PURGED resolution",
        ["resolution"],
        ["gtid_purged"],
        None,
        None,
        "ou_alice0001",
    )
    assert candidates == []
    assert coverage["mode"] == "hybrid"


def test_empty_exact_identifier_list_cannot_select_all_discussions(store):
    with store.engine.begin() as conn:
        result = Catalog().lexical_search(conn, GROUP, [], [], limit=10)
    assert result == []
