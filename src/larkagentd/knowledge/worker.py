from __future__ import annotations

import hashlib
import random
import threading
import time
from contextlib import contextmanager

from sqlalchemy import func, select

from . import policy
from .lark_sources import SourceError
from .models import CLEANUP_JOBS
from .providers.base import ProviderError
from .store import KnowledgeStore
from .tables import group_knowledge_settings


class PermanentJobError(Exception):
    """A content-free error code that always fails the job without retrying."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class JobContext:
    def __init__(self, runtime, group_key, job, store):
        self.runtime = runtime
        self.group_key = group_key
        self.job = job
        self.ref_payload = dict(job.get("ref_payload") or {})
        self.generation = job["generation"]
        self.store = store
        self.fence = job["fence"]

    @contextmanager
    def conn(self):
        with self.runtime.connection() as connection:
            yield connection

    def owns(self):
        with self.conn() as conn:
            return self.store.owns(conn, self.job["id"], self.fence)

    def heartbeat(self):
        with self.conn() as conn:
            return self.store.heartbeat(conn, self.job["id"], self.fence)

    def still_valid(self, capability=None):
        with self.conn() as conn:
            return policy.still_valid(
                conn, self.runtime.config, self.group_key, self.generation, capability
            )


class KnowledgeWorker:
    def __init__(
        self, runtime, owner, handlers=None, clock=None, kinds=None, exclude_kinds=None
    ):
        self.runtime = runtime
        self.owner = owner
        # Which kinds of job this worker takes: the Tool Service's takes the document jobs only, since
        # only it can read documents, and the Gateway's takes everything else.
        self.kinds, self.exclude_kinds = kinds, exclude_kinds
        self.store = KnowledgeStore()
        self.clock = clock or time.time
        if handlers is None:
            from .handlers import handlers as all_handlers

            handlers = all_handlers()
        self.handlers = dict(handlers)
        self._stop = threading.Event()

    def _schedule_maintenance(self, conn):
        maintenance_day = (
            conn.execute(select(func.date_trunc("day", func.now()))).scalar_one().date()
        )
        groups = conn.execute(
            select(group_knowledge_settings.c.group_key).where(
                group_knowledge_settings.c.state == "enabled",
                group_knowledge_settings.c.enabled.is_(True),
            )
        ).scalars()
        for group_key in groups:
            state = policy.effective(conn, self.runtime.config, group_key)
            if state.serving:
                self.store.enqueue(
                    conn,
                    group_key,
                    "reconcile_index",
                    f"maintenance:{maintenance_day.isoformat()}",
                    state.generation,
                )

    def run_once(self):
        with self.runtime.connection() as conn:
            job = self.store.claim(
                conn, self.owner, kinds=self.kinds, exclude_kinds=self.exclude_kinds
            )
            if job is None and self.runtime.config.enabled:
                self._schedule_maintenance(conn)
                job = self.store.claim(
                    conn, self.owner, kinds=self.kinds, exclude_kinds=self.exclude_kinds
                )
        if job is None:
            return False
        context = JobContext(self.runtime, job["group_key"], job, self.store)
        handler = self.handlers.get(job["kind"])
        state = None
        error_code = None
        retry_in = None
        if handler is None:
            state, error_code = "failed", "unknown_job_kind"
        elif job["kind"] not in CLEANUP_JOBS and not context.still_valid():
            state, error_code = "cancelled", "stale_generation"
        else:
            try:
                context.heartbeat()
                handler(context)
                state = "succeeded"
            except Exception as exc:
                if isinstance(exc, (ProviderError, SourceError, PermanentJobError)):
                    code = exc.code
                else:
                    code = "job_failed"
                error_code = code
                retryable = (
                    isinstance(exc, ProviderError)
                    and code in {"dependency_unavailable", "timeout", "bad_response"}
                ) or (
                    isinstance(exc, SourceError)
                    and code
                    in {"rate_limited", "timeout", "unavailable", "bad_response"}
                )
                if retryable and job["attempts"] < self.runtime.config.job_max_attempts:
                    state = "retry_wait"
                    retry_in = self._backoff(job)
                else:
                    state = "failed"
        with self.runtime.connection() as conn:
            self.store.finish(
                conn,
                job["id"],
                job["fence"],
                state,
                error_code=error_code,
                retry_in=retry_in,
            )
        return True

    def _backoff(self, job):
        attempt = max(1, int(job["attempts"]))
        base = min(300.0, float(2 ** min(attempt, 8)))
        seed = int(
            hashlib.sha256(f"{job['id']}:{attempt}".encode()).hexdigest()[:8], 16
        )
        return base * random.Random(seed).uniform(0.8, 1.2)

    def run_forever(self, stop_event, poll_seconds=1.0):
        while not stop_event.is_set():
            if not self.run_once():
                stop_event.wait(poll_seconds)


def start_worker_threads(runtime, stop, prefix, concurrency, **filters):
    """Daemon threads that each run a KnowledgeWorker until `stop` is set, restarting it after an
    unexpected error. `filters` are the worker's `kinds` or `exclude_kinds`."""
    import logging
    import uuid

    from ..core.diagnostics import emit

    threads = []
    for index in range(concurrency):
        owner = f"{prefix}-{index}-{uuid.uuid4()}"

        def run(owner=owner):
            while not stop.is_set():
                try:
                    KnowledgeWorker(runtime, owner=owner, **filters).run_forever(stop)
                except Exception as exc:
                    emit(
                        "knowledge.worker_unavailable",
                        logging.WARNING,
                        error_type=type(exc).__name__,
                    )
                if stop.wait(2):
                    break

        thread = threading.Thread(target=run, name=f"{prefix}-{index}", daemon=True)
        thread.start()
        threads.append(thread)
    return threads
