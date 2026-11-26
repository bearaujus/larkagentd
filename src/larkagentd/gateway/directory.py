"""Bounded, cached display metadata. This data never authenticates a user."""

import queue
import re
import threading
import time

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert

from ..core.diagnostics import emit
from ..core.store import database_time, directory_metadata
from ..lark.profile import profile_fields
from .access import AccessError, email_address, group_scope


class Directory:
    def __init__(self, store, transport=None, *, workers=4, wait_seconds=2):
        self.store, self.transport = store, transport
        self.wait_seconds = wait_seconds
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.pending = {}
        self.tasks = queue.Queue(maxsize=64)
        # Daemon workers preserve Gateway's bounded shutdown/receiver lease behavior.
        self.workers = [
            threading.Thread(target=self.worker, name=f"directory-{i}", daemon=True)
            for i in range(workers)
        ]
        for worker in self.workers:
            worker.start()

    def close(self):
        self.stop.set()
        with self.lock:
            for event in self.pending.values():
                event.set()
        for worker in self.workers:
            worker.join(timeout=0.2)

    def cached(self, targets):
        with self.store.engine.begin() as conn:
            now = float(conn.scalar(select(database_time())))
            rows = conn.execute(
                select(directory_metadata).where(
                    or_(
                        *(
                            and_(
                                directory_metadata.c.kind == kind,
                                directory_metadata.c.key == key,
                            )
                            for kind, key in targets
                        )
                    )
                )
            ).mappings()
            return {(row["kind"], row["key"]): dict(row) for row in rows}, now

    def schedule(self, target):
        if self.stop.is_set() or self.transport is None:
            return None
        with self.lock:
            if target in self.pending:
                return self.pending[target]
            if len(self.pending) >= 64:
                return None
            event = threading.Event()
            self.pending[target] = event
            self.tasks.put_nowait(target)
            return event

    def worker(self):
        while not self.stop.is_set():
            try:
                target = self.tasks.get(timeout=0.2)
            except queue.Empty:
                continue
            kind, key = target
            try:
                try:
                    if kind == "email":
                        found = self.transport.user_profile_by_email(key)
                    elif kind == "owner":
                        found = self.transport.user_profile(key.split(":", 1)[1])
                    else:
                        found = self.transport.group_profile(key)
                    status, data = (
                        ("available", profile_fields(found))
                        if found is not None
                        else ("not_found", {})
                    )
                except Exception as exc:
                    emit(
                        "directory.lookup_unavailable",
                        kind=kind,
                        error_type=type(exc).__name__,
                    )
                    status, data = "unavailable", {}
                if self.stop.is_set():
                    continue
                with self.store.engine.begin() as conn:
                    old = (
                        conn.execute(
                            select(directory_metadata).where(
                                directory_metadata.c.kind == kind,
                                directory_metadata.c.key == key,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    # Retain last-known display data during a transient API failure.
                    if status == "unavailable" and old:
                        data = old["data"]
                    ttl = (
                        900
                        if status == "available"
                        else (300 if status == "not_found" else 30)
                    )
                    row = {
                        "kind": kind,
                        "key": key,
                        "data": data,
                        "status": status,
                        "checked_at": database_time(),
                        "expires": database_time() + ttl,
                    }
                    conn.execute(
                        insert(directory_metadata)
                        .values(**row)
                        .on_conflict_do_update(
                            index_elements=[
                                directory_metadata.c.kind,
                                directory_metadata.c.key,
                            ],
                            set_={
                                name: row[name]
                                for name in ("data", "status", "checked_at", "expires")
                            },
                        )
                    )
            except Exception as exc:
                emit("directory.cache_unavailable", error_type=type(exc).__name__)
            finally:
                with self.lock:
                    event = self.pending.pop(target, None)
                    if event:
                        event.set()
                self.tasks.task_done()

    def lookup(self, resource, body):
        if not isinstance(body, dict) or (
            "refresh" in body and not isinstance(body["refresh"], bool)
        ):
            raise AccessError(400, "invalid_directory_request")
        allowed = (
            {"emails", "owners", "refresh"}
            if resource == "users"
            else {"group_ids", "refresh"}
        )
        if set(body) - allowed:
            raise AccessError(400, "invalid_directory_request")
        targets = []
        for field, kind in (
            (("emails", "email"), ("owners", "owner"))
            if resource == "users"
            else (("group_ids", "group"),)
        ):
            values = body.get(field, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                raise AccessError(400, "invalid_directory_request")
            for value in values:
                if kind == "email":
                    value = email_address(value)
                elif kind == "group":
                    group_scope(value)
                elif not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,128}:[A-Za-z0-9_-]{1,256}", value
                ):
                    raise AccessError(400, "invalid_owner")
                targets.append((kind, value))
        if not 1 <= len(targets) <= 20:
            raise AccessError(400, "invalid_directory_request")
        targets = list(dict.fromkeys(targets))
        cache, now = self.cached(targets)
        waiting = []
        for target in targets:
            if target[0] == "owner" and target[1].startswith("system:"):
                continue
            row = cache.get(target)
            if body.get("refresh") or row is None or row["expires"] <= now:
                if event := self.schedule(target):
                    waiting.append(event)
        deadline = time.monotonic() + self.wait_seconds
        for event in waiting:
            event.wait(max(0, deadline - time.monotonic()))
        cache, now = self.cached(targets)
        items = []
        with self.lock:
            pending = set(self.pending)
        for kind, key in targets:
            row = cache.get((kind, key))
            data = profile_fields(row["data"]) if row else {}
            refreshing = (kind, key) in pending
            status = (
                row["status"] if row else ("pending" if refreshing else "unavailable")
            )
            if (
                row
                and (row["expires"] <= now or status == "unavailable")
                and any(data.values())
            ):
                status = "stale"
            item = {
                "lookup_type": kind,
                "key": key,
                "name": data.get("name"),
                "avatar_url": data.get("avatar_url"),
                "email": key if kind == "email" else data.get("email"),
                "directory_email": data.get("email"),
                "metadata_status": status,
                "refreshing": refreshing,
                "checked_at": row["checked_at"] if row else None,
            }
            if kind == "owner":
                item["owner"] = key
                if key.startswith("system:"):
                    item.update(
                        name="System",
                        metadata_status="not_applicable",
                        refreshing=False,
                    )
            if kind == "group":
                item["group_id"] = key
            items.append(item)
        return {
            "items": items,
            "retry_after_seconds": 2
            if any(item["refreshing"] for item in items)
            else None,
        }
