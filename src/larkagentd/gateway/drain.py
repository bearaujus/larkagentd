"""Pause local agent admission while Compose waits to restart the stack."""

import json
import os
import time
from pathlib import Path
from urllib.request import urlopen

MARKER = Path("/tmp/larkagentd-drain")
TIMEOUT_SECONDS = 300


def is_draining():
    try:
        return time.time() - MARKER.stat().st_mtime < TIMEOUT_SECONDS + 30
    except FileNotFoundError:
        return False


def pending_delivery(store):
    from sqlalchemy import func, or_, select, text

    from larkagentd.core.store import database_time, jobs

    with store.engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5s'"))
        return conn.scalar(
            select(func.count())
            .select_from(jobs)
            .where(
                or_(
                    (jobs.c.kind == "prepare") & (jobs.c.until > database_time()),
                    jobs.c.kind == "send",
                )
            )
        )


def wait_until_idle(snapshot, pending, *, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + TIMEOUT_SECONDS
    idle = False
    while clock() < deadline:
        status = snapshot()
        # The old image can drain existing work but does not yet honor the admission marker.
        draining = status.get("draining", True)
        empty = draining and not status.get("active_turns") and not pending()
        if empty and idle:
            return True
        idle = empty
        sleep(1)
    return False


def main():
    from larkagentd.core.store import Store

    port = int(os.getenv("HEALTH_PORT", "8080"))
    store = Store(os.environ["DATABASE_URL"])

    def snapshot():
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            return json.load(response)

    MARKER.touch()
    print("Waiting for active Maxx turns and replies to finish.", flush=True)
    ready = False
    try:
        ready = wait_until_idle(snapshot, lambda: pending_delivery(store))
    except Exception:
        print("Could not verify that the Gateway is ready to restart.", flush=True)
    finally:
        store.engine.dispose()
        if not ready:
            MARKER.unlink(missing_ok=True)
    if not ready:
        raise SystemExit("Restart cancelled. The Gateway is still running.")


if __name__ == "__main__":
    main()
