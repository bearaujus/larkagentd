import json
import logging
import os
import signal
import threading
import time
import uuid
from contextlib import nullcontext
from http.server import ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import text

from ..config import Config, lark_read_settings
from ..core.artifacts import Artifacts
from ..core.crypto import Vault
from ..core.database import classify_database_error
from ..core.diagnostics import configure_logging, emit, job_fields, log_job_error
from ..core.extensions import load_extension
from ..core.store import Store
from ..knowledge.lark_sources import CliGroupSource
from ..knowledge.runtime import GroupBrainRuntime, build_index
from ..toolservice.larkcli import LarkReader
from .access import Access
from .dashboard import make_handler
from .directory import Directory
from .governance import Governance
from .hermes import HermesAgent
from .knowledge_hooks import start_knowledge_workers
from .runtime_status import RuntimeStatus
from .service import Service


def check_database(store):
    with store.engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5s'"))
        for table in (
            "users",
            "events",
            "leases",
            "jobs",
            "messages",
            "turns",
            "artifacts",
            "gateway_policies",
            "workspace_settings",
            "gateway_members",
            "dashboard_managers",
            "gateway_identities",
            "dashboard_logins",
            "dashboard_sessions",
            "dashboard_limits",
            "token_usage",
            "query_activity",
            "directory_metadata",
            "governance_settings",
            "governance_flex",
            "governance_rates",
            "governance_history",
            "knowledge_sources",
            "knowledge_lark_attachments",
            "group_actions",
            "tool_effects",
        ):
            conn.execute(text(f"SELECT 1 FROM {table} LIMIT 0"))


def build_service(config, store, agent, knowledge_index=None):
    """The Gateway's Service. Its preview store shares the vault with the tool service's,
    because it opens what the tool service stored encrypted."""
    vault = Vault(config.encryption_key)
    command, reader_id, secret, brand, home = lark_read_settings(Path("/opt/toolkit"))
    source = (
        CliGroupSource(LarkReader(command, reader_id, secret, home, base_domain=brand))
        if reader_id
        else None
    )
    access = Access(
        store,
        config.dashboard_admin_emails,
        config.group_brain,
        # The dashboard's approvals queue indexing through this runtime.
        runtime=(
            GroupBrainRuntime(
                store.engine, config.group_brain, index=knowledge_index, source=source
            )
            if config.group_brain.enabled
            else None
        ),
    )
    extension = load_extension()
    auth = extension.authentication() if extension else None
    return Service(
        store,
        vault,
        auth,
        config.history_messages,
        identity=auth.identify if auth else None,
        agent=agent,
        agent_capacity=config.agent_capacity,
        artifacts=Artifacts(store, vault),
        message_window=config.message_window,
        access=access,
        governance=Governance(
            access,
            seed_limit=config.user_token_quota,
            default_model=config.provider_model,
            models=config.available_models,
            efforts=config.model_reasoning_efforts,
        ),
        token_summary=config.token_summary,
        available_models=config.available_models,
        vision_models=config.vision_models,
        group_brain=config.group_brain,
        provider_model=config.provider_model,
        lark_app_senders=set(config.lark_allowed_senders) - {"user"},
    )


def run_job(service, job, transport):
    start = time.monotonic()
    fields = job_fields(job)
    emit("job.started", logging.DEBUG, **fields)
    try:
        service.work(job, transport)
    except Exception as exc:
        log_job_error(job, exc)
        service.failure(job, exc)
    finally:
        # Work may defer/stale-skip: this means the attempt ended, not that the job completed.
        emit(
            "job.attempt_finished",
            logging.DEBUG,
            **fields,
            duration_ms=round((time.monotonic() - start) * 1000),
        )


LEASE_DEADLINE = (
    10  # seconds without a renewal before the receiver exits (the lease lasts 15)
)


def renew_receiver_lease(store, owner, last_renewal, on_failure):
    """One renewal attempt. False only when another instance owns the lease.

    A failed attempt (the database was slow or briefly unreachable) is not a loss: the lease
    still has time left, and the watchdog ends the process if no renewal succeeds within
    LEASE_DEADLINE, before the lease can pass to anyone else.
    """
    try:
        held = store.lease("lark-receiver", owner)
    except Exception as exc:
        on_failure(classify_database_error(exc))
        return True
    if held:
        last_renewal[0] = time.monotonic()
    return held


def keep_lease(service, job, done, lost, interval=10):
    """Renew the job's lease every `interval` seconds until `done` is set.

    A failed renewal (the database was slow or briefly unreachable) is reported once per run of
    failures and tried again at the next interval, as the receiver lease is. Giving up at the
    first one let the 60 second lease lapse under a job that was still running: another worker
    claimed it, followed the same Hermes run, and every step and sentence reached the chat once
    more per follower. Only a lease that another worker holds ends the loop, and then `lost` is
    set so this runner stops posting progress.
    """
    failing = False
    while not done.wait(interval):
        try:
            renewed = service.heartbeat(job)
        except Exception as exc:
            if not failing:
                emit(
                    "job.heartbeat_failed",
                    logging.WARNING,
                    **job_fields(job),
                    error_type=type(exc).__name__,
                )
            failing = True
            continue
        failing = False
        if renewed:
            continue
        emit("job.lease_lost", logging.WARNING, **job_fields(job))
        lost.set()
        return


def main():
    try:
        configure_logging(
            os.getenv("LOG_LEVEL", "INFO").upper(),
            os.getenv("LOG_FORMAT", "text").lower(),
        )
    except ValueError as exc:
        configure_logging()
        emit("startup.failed", logging.ERROR, stage="logging", reason=str(exc))
        raise SystemExit(1) from None
    emit("daemon.starting", pid=os.getpid(), version="0.1.0")
    try:
        config = Config.load()
    except ValueError as exc:
        emit("startup.failed", logging.ERROR, stage="configuration", reason=str(exc))
        raise SystemExit(1) from None
    emit(
        "configuration.loaded",
        status_interval=config.status_interval,
        agent="hermes",
        agent_slots=config.agent_capacity,
        agent_deadline_seconds=config.agent_deadline,
        dashboard_admin_count=len(config.dashboard_admin_emails),
        token_quota=config.user_token_quota,
        token_summary=config.token_summary,
        available_models=config.available_models,
        loading_image=bool(config.loading_image_key),
        knowledge_enabled=bool(config.group_brain.enabled),
    )
    store = None
    try:
        store = Store(config.database_url)
        emit("database.connecting")
        check_database(store)
    except Exception as exc:
        # URLs and driver exception messages can contain credentials.
        emit(
            "startup.failed",
            logging.ERROR,
            stage="database",
            error_type=type(exc).__name__,
            hint="Check_DATABASE_URL_and_run_alembic_upgrade_head",
        )
        if store:
            store.engine.dispose()
        raise SystemExit(1) from None
    emit("database.ready", schema="verified")
    agent = HermesAgent(
        config.agent_url,
        config.agent_api_key,
        config.agent_model,
        config.agent_deadline,
        google_email=config.google_service_account_email,
    )
    knowledge_index = (
        build_index(store.engine, config.group_brain, config.database_url)
        if config.group_brain.enabled
        else None
    )
    service = build_service(config, store, agent, knowledge_index)
    directory = Directory(store)
    stop = threading.Event()
    owner = str(uuid.uuid4())
    status = RuntimeStatus()
    server = None
    threads = []

    def shutdown(signum, _frame):
        emit("shutdown.requested", signal=signal.Signals(signum).name)
        status.stopping = True
        stop.set()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    class Health(
        make_handler(
            service.access,
            config.dashboard_public_url,
            directory,
            governance=service.governance,
            additional_origins=config.dashboard_additional_origins,
        )
    ):
        def do_GET(self):
            if self.path not in ("/healthz", "/readyz"):
                return super().do_GET()
            db_ok = None
            if self.path == "/readyz":
                db_ok = True
                try:
                    check_database(store)
                except Exception:
                    db_ok = False
            result = status.snapshot(db_ok)
            code = 200 if self.path == "/healthz" or result["ready"] else 503
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        def log_message(self, *_):
            pass

    def spawn(target, name):
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        threads.append(thread)

    try:
        try:
            server = ThreadingHTTPServer(("0.0.0.0", config.health_port), Health)
        except OSError as exc:
            emit(
                "startup.failed",
                logging.ERROR,
                stage="health_server",
                error_type=type(exc).__name__,
                port=config.health_port,
                hint="Check_HEALTH_PORT_is_available",
            )
            raise SystemExit(1) from None
        spawn(server.serve_forever, "health")
        emit("health.listening", port=config.health_port, endpoints="/healthz,/readyz")
        emit("lark.sdk_loading")
        # Defer the large SDK import until startup feedback is already visible.
        from ..lark.adapter import Lark

        transport = Lark(config, service)
        directory.transport = transport
        if config.group_brain.enabled:
            threads.extend(
                start_knowledge_workers(
                    config, store, transport.client, stop, knowledge_index
                )
            )

        def worker(select):
            last_error = 0
            with status.lock:
                status.workers += 1
            try:
                while not stop.wait(0.25):
                    try:
                        gate = (
                            status.agent_work()
                            if select.get("only") == "prepare"
                            else nullcontext(True)
                        )
                        with gate as admitted:
                            if not admitted:
                                continue
                            job = store.claim(**select)
                            if not job:
                                continue
                            done, lost = threading.Event(), threading.Event()
                            job["lost"] = lost
                            thread = threading.Thread(
                                target=keep_lease,
                                args=(service, job, done, lost),
                                daemon=True,
                            )
                            thread.start()
                            try:
                                run_job(service, job, transport)
                            finally:
                                done.set()
                                thread.join(timeout=2)
                    except Exception as exc:
                        if time.monotonic() - last_error >= 30:
                            emit(
                                "worker.unavailable",
                                logging.WARNING,
                                error_type=type(exc).__name__,
                                reason=classify_database_error(exc),
                                retry_seconds=2,
                            )
                            last_error = time.monotonic()
                        stop.wait(2)
            finally:
                with status.lock:
                    status.workers -= 1

        # Agent turns get their own workers so slow Hermes calls cannot starve cards,
        # replies and login polling.
        pools = [
            (dict(exclude="prepare"), 4, "worker"),
            (
                dict(only="prepare", cap=config.agent_capacity),
                config.agent_capacity,
                "agent",
            ),
        ]
        for select, count, label in pools:
            for i in range(count):
                spawn(lambda select=select: worker(select), f"{label}-{i}")
        emit(
            "workers.started",
            count=sum(count for _, count, _ in pools),
            agent_slots=config.agent_capacity,
            instance=owner,
        )
        status.role = "standby"
        if config.status_interval:

            def report_status():
                while not stop.wait(config.status_interval):
                    database_ok = True
                    try:
                        check_database(store)
                    except Exception:
                        database_ok = False
                    emit("daemon.status", **status.snapshot(database_ok))

            spawn(report_status, "status")
        emit("receiver.electing")
        waiting = False
        last_error = 0
        while not stop.is_set():
            try:
                if store.lease("lark-receiver", owner):
                    break
                if not waiting:
                    emit(
                        "receiver.standby",
                        reason="Another_instance_owns_the_Lark_connection",
                        workers="active",
                    )
                    waiting = True
            except Exception as exc:
                if time.monotonic() - last_error >= 30:
                    emit(
                        "receiver.election_failed",
                        logging.WARNING,
                        error_type=type(exc).__name__,
                        reason=classify_database_error(exc),
                        retry_seconds=3,
                    )
                    last_error = time.monotonic()
            stop.wait(3)
        status.role = "receiver"
        emit("receiver.acquired", instance=owner)
        last_renewal = [time.monotonic()]
        last_failure = [None, 0.0]  # cause label, when it was last reported

        def fail_leadership(reason, cause=None):
            emit(
                "receiver.lease_lost",
                logging.CRITICAL,
                reason=reason,
                cause=cause,
                action="Exit_for_safe_failover",
            )
            os._exit(1)

        def lease_watchdog():
            while not stop.wait(1):
                if time.monotonic() - last_renewal[0] > LEASE_DEADLINE:
                    fail_leadership("Renewal_deadline_exceeded", last_failure[0])

        def renewal_failed(cause):
            last_failure[0] = cause
            if time.monotonic() - last_failure[1] >= 10:
                emit(
                    "receiver.renewal_failed",
                    logging.WARNING,
                    reason=cause,
                    seconds_left=max(
                        0, round(LEASE_DEADLINE - (time.monotonic() - last_renewal[0]))
                    ),
                )
                last_failure[1] = time.monotonic()

        def leader_guard():
            while not stop.wait(3):
                if not renew_receiver_lease(store, owner, last_renewal, renewal_failed):
                    fail_leadership("Ownership_changed")

        spawn(lease_watchdog, "lease-watchdog")
        spawn(leader_guard, "lease-renewal")
        emit("lark.connecting", transport="websocket")
        transport.run(status.connection)
        if not stop.is_set():
            emit(
                "daemon.failed",
                logging.ERROR,
                reason="Websocket_runner_returned_unexpectedly",
            )
            raise SystemExit(1)
    except Exception as exc:
        emit(
            "daemon.failed",
            logging.ERROR,
            error_type=type(exc).__name__,
            hint="Check_Lark_app_configuration_and_network",
        )
        raise SystemExit(1) from None
    finally:
        status.stopping = True
        stop.set()
        directory.close()
        if server:
            server.shutdown()
            server.server_close()
        deadline = time.monotonic() + 3
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        # Do not release receiver leadership early: the SDK socket closes at process exit.
        # Another instance may take over when its short lease expires.
        store.engine.dispose()
        emit("daemon.stopped", in_flight_jobs="Recoverable_after_lease_expiry")


if __name__ == "__main__":
    main()
