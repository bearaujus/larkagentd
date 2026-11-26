from sqlalchemy import insert, select
from test_core import store as store_fixture

from larkagentd.core.store import gateway_policies
from larkagentd.knowledge import ingest
from larkagentd.knowledge.lark_sources import SourceError
from larkagentd.knowledge.models import GroupBrainConfig
from larkagentd.knowledge.runtime import GroupBrainRuntime
from larkagentd.knowledge.store import KnowledgeStore
from larkagentd.knowledge.tables import gb_jobs
from larkagentd.knowledge.worker import KnowledgeWorker, PermanentJobError

store = store_fixture
GROUP = "group:oc_example1234"
kb = KnowledgeStore()


def enabled_group(store):
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
            conn, GROUP, "ingest_message", "synthetic-message", settings["generation"]
        )
        return settings["generation"]


def test_retryable_source_error_stores_only_fixed_code(store):
    enabled_group(store)

    def handler(_context):
        raise SourceError("timeout")

    config = GroupBrainConfig(enabled=True)
    runtime = GroupBrainRuntime(store.engine, config)
    worker = KnowledgeWorker(runtime, "worker-example", {"ingest_message": handler})
    assert worker.run_once()
    with store.engine.begin() as conn:
        job = conn.execute(select(gb_jobs)).mappings().one()
    assert job["state"] == "retry_wait"
    assert job["error_code"] == "timeout"
    assert "message body" not in repr(job)


def test_unexpected_handler_exception_does_not_leak_text(store):
    enabled_group(store)

    def handler(_context):
        raise RuntimeError("synthetic private payload must not escape")

    runtime = GroupBrainRuntime(store.engine, GroupBrainConfig(enabled=True))
    worker = KnowledgeWorker(runtime, "worker-example", {"ingest_message": handler})
    assert worker.run_once()
    with store.engine.begin() as conn:
        job = conn.execute(select(gb_jobs)).mappings().one()
    assert job["state"] == "failed"
    assert job["error_code"] == "job_failed"
    assert "synthetic private payload" not in repr(job)


def test_permanent_job_error_is_failed_without_retry_and_uses_code(store):
    enabled_group(store)

    def handler(_context):
        raise PermanentJobError("dependency_unavailable")

    runtime = GroupBrainRuntime(
        store.engine, GroupBrainConfig(enabled=True, job_max_attempts=9)
    )
    worker = KnowledgeWorker(runtime, "worker-example", {"ingest_message": handler})
    assert worker.run_once()
    with store.engine.begin() as conn:
        job = conn.execute(select(gb_jobs)).mappings().one()
    assert job["state"] == "failed"
    assert job["error_code"] == "dependency_unavailable"


def test_empty_queue_schedules_cleanup_for_each_serving_group(store):
    with store.engine.begin() as conn:
        conn.execute(insert(gateway_policies).values(scope=GROUP, mode="all_users"))
        kb.update_settings(
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
    runtime = GroupBrainRuntime(store.engine, GroupBrainConfig(enabled=True))
    failures = []

    def reconcile(ctx):
        try:
            ingest.reconcile_index(ctx)
        except Exception as exc:
            failures.append(repr(exc))
            raise

    assert KnowledgeWorker(
        runtime, "worker-example", {"reconcile_index": reconcile}
    ).run_once()
    assert not failures, failures
    with store.engine.begin() as conn:
        job = conn.execute(select(gb_jobs)).mappings().one()
    assert job["kind"] == "reconcile_index"
    assert job["state"] == "succeeded", job["error_code"]
    assert job["idempotency_key"].startswith("maintenance:")
