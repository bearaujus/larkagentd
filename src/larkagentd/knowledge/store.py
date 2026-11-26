"""Group Brain persistence: settings (versioned, with a generation), the audit log and the job queue.

Every method takes the caller's connection so a settings change, its audit row and the jobs it
cancels or queues commit together. External calls never happen inside these methods."""

import uuid

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..core.store import database_time, gateway_policies
from .models import CLEANUP_JOBS, JOB_KINDS, POLICY_FIELDS
from .tables import (
    CATALOG_TABLES,
    gb_audit_events,
    gb_discussions,
    gb_documents,
    gb_index_entries,
    gb_jobs,
    gb_memory,
    gb_sync_state,
    group_knowledge_settings,
)

# The most items per kind that a resume queues for indexing again; the rest stay on lexical search.
RESUME_LIMIT = 2000

SETTING_FIELDS = (
    "enabled",
    "recall_enabled",
    "memory_enabled",
    "documents_enabled",
    "history_window_days",
    "notice_ref",
    "policy_ref",
)
REF_LIMIT = 200
# What a group without a settings row is: disabled, at version 0 so the first write names version 0.
DEFAULTS = dict(
    enabled=False,
    state="disabled",
    recall_enabled=True,
    memory_enabled=True,
    documents_enabled=True,
    review_required=True,
    history_window_days=30,
    generation=0,
    version=0,
    enabled_at=None,
    enabled_by=None,
    updated_at=None,
    updated_by=None,
    notice_ref=None,
    policy_ref=None,
)


class KnowledgeError(Exception):
    """A refused change. `code` is a short fixed token the API returns; it never holds content."""

    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


class VersionConflict(KnowledgeError):
    def __init__(self):
        super().__init__("version_conflict", 409)


def ref(value):
    """A notice or policy reference: short printable text, or None."""
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > REF_LIMIT
        or not value.isprintable()
    ):
        raise KnowledgeError("invalid_reference")
    return value.strip()


class KnowledgeStore:
    cleanup_kinds = CLEANUP_JOBS

    # --- groups and settings ---

    def registered(self, conn, group_key):
        """True when `group_key` is a registered group (`group:<chat id>`), never a DM or unknown scope."""
        if not isinstance(group_key, str) or not group_key.startswith("group:"):
            return False
        return (
            conn.execute(
                select(gateway_policies.c.scope).where(
                    gateway_policies.c.scope == group_key
                )
            ).first()
            is not None
        )

    def row(self, conn, group_key, lock=False):
        query = select(group_knowledge_settings).where(
            group_knowledge_settings.c.group_key == group_key
        )
        if lock:
            query = query.with_for_update()
        return conn.execute(query).mappings().first()

    def settings(self, conn, group_key):
        """The group's settings as a dict. A missing row reads as disabled (GB-001)."""
        found = self.row(conn, group_key)
        return dict(found) if found else dict(DEFAULTS, group_key=group_key)

    def update_settings(
        self, conn, group_key, expected_version, changes, actor, max_history_days=90
    ):
        """Change a group's settings from a dashboard write that read `expected_version`.

        Raises KnowledgeError: not_registered, invalid_fields, invalid_value, version_conflict,
        missing_group_notice (enabling without a notice and policy reference), and purging."""
        if not self.registered(conn, group_key):
            raise KnowledgeError("not_registered", 404)
        if not isinstance(changes, dict) or set(changes) - set(SETTING_FIELDS):
            raise KnowledgeError("invalid_fields")
        values = self.clean(changes, max_history_days)
        current = self.row(conn, group_key, lock=True)
        version = current["version"] if current else 0
        if expected_version != version:
            raise VersionConflict()
        before = dict(current) if current else dict(DEFAULTS)
        if before["state"] == "purging":
            raise KnowledgeError("purging", 409)
        after = {**before, **values}
        enabling = values.get("enabled") is True and not before["enabled"]
        if after["enabled"] and (not after["notice_ref"] or not after["policy_ref"]):
            raise KnowledgeError("missing_group_notice", 409)
        # `state` follows `enabled`: turning it on starts serving, turning it off stops everything.
        # A paused group stays paused until it is resumed on purpose.
        if "enabled" in values:
            if values["enabled"]:
                after["state"] = (
                    before["state"] if before["state"] == "paused" else "enabled"
                )
            else:
                after["state"] = "disabled"
        policy_changed = any(after[f] != before[f] for f in POLICY_FIELDS)
        row = {f: after[f] for f in SETTING_FIELDS}
        row.update(
            state=after["state"],
            version=version + 1,
            generation=(
                before["generation"] + 1
                if policy_changed
                else max(before["generation"], 1)
            ),
            updated_by=actor,
            updated_at=func.now(),
        )
        if enabling:
            row.update(enabled_at=func.now(), enabled_by=actor)
        if current is None:
            conn.execute(
                insert(group_knowledge_settings).values(group_key=group_key, **row)
            )
        else:
            conn.execute(
                update(group_knowledge_settings)
                .where(group_knowledge_settings.c.group_key == group_key)
                .values(**row)
            )
        cancelled = 0
        if policy_changed and not after["enabled"]:
            cancelled = self.cancel_jobs(conn, group_key)
        self.audit(
            conn,
            group_key,
            actor,
            "settings_updated",
            policy_version=version + 1,
            reason=",".join(sorted(f for f in values if after[f] != before[f])) or None,
        )
        if cancelled:
            self.audit(
                conn, group_key, actor, "jobs_cancelled", target_id=str(cancelled)
            )
        return self.settings(conn, group_key)

    def clean(self, changes, max_history_days):
        """Validated values for the fields named in `changes`."""
        values = {}
        for name, value in changes.items():
            if name in (
                "enabled",
                "recall_enabled",
                "memory_enabled",
                "documents_enabled",
            ):
                if not isinstance(value, bool):
                    raise KnowledgeError("invalid_value")
            elif name == "history_window_days":
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or not 1 <= value <= max_history_days
                ):
                    raise KnowledgeError("invalid_value")
            else:
                value = ref(value)
            values[name] = value
        return values

    def transition(self, conn, group_key, action, actor, expected_version=None):
        """pause, resume or purge. Each raises the generation, so work queued under the old one is
        fenced out. Returns the new settings.

        pause: enabled to paused. resume: paused to enabled (nothing is reactivated until sources are
        revalidated). purge: any state to purging; cancels jobs and queues the cleanup."""
        if not self.registered(conn, group_key):
            raise KnowledgeError("not_registered", 404)
        current = self.row(conn, group_key, lock=True)
        if current is None:
            raise KnowledgeError("group_disabled", 409)
        if expected_version is not None and current["version"] != expected_version:
            raise VersionConflict()
        state = current["state"]
        if action == "pause":
            if state != "enabled":
                raise KnowledgeError("invalid_transition", 409)
            new = dict(state="paused")
        elif action == "resume":
            if state != "paused" or not current["enabled"]:
                raise KnowledgeError("invalid_transition", 409)
            new = dict(state="enabled")
        elif action == "purge":
            if state == "purging":
                raise KnowledgeError("invalid_transition", 409)
            new = dict(state="purging", enabled=False)
        else:
            raise KnowledgeError("invalid_transition", 409)
        generation = current["generation"] + 1
        conn.execute(
            update(group_knowledge_settings)
            .where(group_knowledge_settings.c.group_key == group_key)
            .values(
                **new,
                generation=generation,
                version=current["version"] + 1,
                updated_by=actor,
                updated_at=func.now(),
            )
        )
        cancelled = self.cancel_jobs(conn, group_key)
        if action == "resume":
            self.revalidate_after_resume(conn, group_key, generation)
        if action == "purge":
            self.enqueue(
                conn, group_key, "purge_group", f"purge:{generation}", generation
            )
        self.audit(
            conn,
            group_key,
            actor,
            action,
            policy_version=current["version"] + 1,
            target_id=str(cancelled) if cancelled else None,
        )
        return self.settings(conn, group_key)

    def revalidate_after_resume(self, conn, group_key, generation):
        """Resuming raises the generation, which retires every index entry written before the pause, and
        a document may have lost access meanwhile. So nothing is trusted as before: each document is read
        again, and everything retained is indexed again under the new generation. Nothing is collected
        from Lark that was not already approved (no backfill)."""
        for document_id, revision in conn.execute(
            select(gb_documents.c.id, gb_documents.c.current_revision).where(
                gb_documents.c.group_key == group_key, gb_documents.c.state == "active"
            )
        ).all()[:RESUME_LIMIT]:
            self.enqueue(
                conn, group_key, "refresh_document", f"resume:{generation}:refresh:{document_id}",
                generation, ref_payload={"document_id": document_id},
            )  # fmt: skip
            self.enqueue(
                conn, group_key, "index_revision", f"resume:{generation}:document:{document_id}",
                generation, source_revision=revision,
                ref_payload={"kind": "document", "document_id": document_id, "revision": revision},
            )  # fmt: skip
        for memory_id, revision in conn.execute(
            select(gb_memory.c.id, gb_memory.c.current_revision).where(
                gb_memory.c.group_key == group_key, gb_memory.c.state == "active"
            )
        ).all()[:RESUME_LIMIT]:
            self.enqueue(
                conn, group_key, "index_revision", f"resume:{generation}:memory:{memory_id}",
                generation, source_revision=revision,
                ref_payload={"kind": "memory", "memory_id": memory_id, "revision": revision},
            )  # fmt: skip
        for discussion_id, revision in conn.execute(
            select(gb_discussions.c.id, gb_discussions.c.source_revision)
            .where(
                gb_discussions.c.group_key == group_key,
                gb_discussions.c.status == "active",
            )
            .order_by(gb_discussions.c.ended_at.desc())
        ).all()[:RESUME_LIMIT]:
            self.enqueue(
                conn, group_key, "index_revision", f"resume:{generation}:discussion:{discussion_id}",
                generation, source_revision=revision,
                ref_payload={"discussion_id": discussion_id, "revision": revision},
            )  # fmt: skip

    def finish_purge(self, conn, group_key, actor="system"):
        """The cleanup is done: the group goes back to disabled. Only a purging group moves."""
        current = self.row(conn, group_key, lock=True)
        if current is None or current["state"] != "purging":
            return False
        conn.execute(
            update(group_knowledge_settings)
            .where(group_knowledge_settings.c.group_key == group_key)
            .values(
                state="disabled",
                enabled=False,
                generation=current["generation"] + 1,
                version=current["version"] + 1,
                updated_by=actor,
                updated_at=func.now(),
            )
        )
        self.audit(
            conn,
            group_key,
            actor,
            "purge_completed",
            policy_version=current["version"] + 1,
        )
        return True

    def purge_catalog(self, conn, group_key):
        """Delete every canonical row of the group, children first. Returns rows removed per table.
        The settings row and the audit log stay."""
        removed = {}
        for table in reversed(CATALOG_TABLES):
            result = conn.execute(table.delete().where(table.c.group_key == group_key))
            removed[table.name] = result.rowcount
        return removed

    # --- audit ---

    def audit(
        self,
        conn,
        group_key,
        actor,
        action,
        outcome="ok",
        target_id=None,
        reason=None,
        policy_version=None,
        correlation_id=None,
    ):
        """Append a metadata-only event: who, what, which object, the outcome and a reason code."""
        conn.execute(
            insert(gb_audit_events).values(
                group_key=group_key,
                actor_id=actor,
                action=action,
                target_id=target_id,
                policy_version=policy_version,
                outcome=outcome,
                reason_code=reason,
                correlation_id=correlation_id,
            )
        )

    def audit_page(self, conn, group_key, limit=50, before=None):
        query = (
            select(gb_audit_events)
            .where(gb_audit_events.c.group_key == group_key)
            .order_by(gb_audit_events.c.id.desc())
            .limit(max(1, min(limit, 200)))
        )
        if before is not None:
            query = query.where(gb_audit_events.c.id < before)
        return [dict(r) for r in conn.execute(query).mappings()]

    # --- jobs ---

    def enqueue(
        self,
        conn,
        group_key,
        kind,
        idempotency_key,
        generation,
        source_revision=None,
        ref_payload=None,
        delay=0,
    ):
        """Queue a job once: (job id, created). The same idempotency key returns the existing job.
        `ref_payload` holds references and counters only, never text."""
        if kind not in JOB_KINDS:
            raise KnowledgeError("invalid_job_kind")
        job_id = str(uuid.uuid4())
        inserted = conn.execute(
            pg_insert(gb_jobs)
            .values(
                id=job_id,
                group_key=group_key,
                kind=kind,
                idempotency_key=idempotency_key,
                generation=generation,
                source_revision=source_revision,
                ref_payload=ref_payload or {},
                available_at=database_time() + delay,
            )
            .on_conflict_do_nothing(constraint="gb_jobs_idempotency")
            .returning(gb_jobs.c.id)
        ).first()
        if inserted:
            return job_id, True
        existing = conn.execute(
            select(gb_jobs.c.id).where(
                gb_jobs.c.group_key == group_key,
                gb_jobs.c.idempotency_key == idempotency_key,
            )
        ).scalar_one()
        return existing, False

    def claim(self, conn, owner, lease_seconds=60, kinds=None, exclude_kinds=None):
        """Take one due job: a queued or waiting one, or a running one whose lease ran out. The fence
        rises on every claim, so a worker that lost its lease can no longer finish the job."""
        now = float(conn.execute(select(database_time())).scalar_one())
        due = (
            gb_jobs.c.state.in_(("queued", "retry_wait"))
            & (gb_jobs.c.available_at <= now)
        ) | ((gb_jobs.c.state == "running") & (gb_jobs.c.lease_until < now))
        query = select(gb_jobs).where(due)
        if kinds:
            query = query.where(gb_jobs.c.kind.in_(tuple(kinds)))
        if exclude_kinds:
            query = query.where(gb_jobs.c.kind.not_in(tuple(exclude_kinds)))
        job = (
            conn.execute(
                query.order_by(gb_jobs.c.available_at, gb_jobs.c.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            .mappings()
            .first()
        )
        if job is None:
            return None
        conn.execute(
            update(gb_jobs)
            .where(gb_jobs.c.id == job["id"])
            .values(
                state="running",
                lease_owner=owner,
                lease_until=now + lease_seconds,
                fence=job["fence"] + 1,
                attempts=job["attempts"] + 1,
            )
        )
        return dict(
            job, state="running", fence=job["fence"] + 1, attempts=job["attempts"] + 1
        )

    def heartbeat(self, conn, job_id, fence, lease_seconds=60):
        """Extend the lease of a job this worker still holds. False when it no longer does."""
        result = conn.execute(
            update(gb_jobs)
            .where(
                gb_jobs.c.id == job_id,
                gb_jobs.c.fence == fence,
                gb_jobs.c.state == "running",
            )
            .values(lease_until=database_time() + lease_seconds)
        )
        return result.rowcount == 1

    def owns(self, conn, job_id, fence):
        """True while this worker's claim is the current one and its lease has not run out."""
        found = conn.execute(
            select(gb_jobs.c.id)
            .where(
                gb_jobs.c.id == job_id,
                gb_jobs.c.fence == fence,
                gb_jobs.c.state == "running",
                gb_jobs.c.lease_until > database_time(),
            )
            .with_for_update()
        ).first()
        return found is not None

    def finish(self, conn, job_id, fence, state, error_code=None, retry_in=None):
        """Record the outcome: succeeded, failed, or retry_wait after `retry_in` seconds. Only the
        current claim may do it; False means the job moved on without this worker."""
        values = dict(state=state, error_code=error_code, lease_until=0)
        if state == "retry_wait":
            values["available_at"] = database_time() + (retry_in or 0)
        result = conn.execute(
            update(gb_jobs)
            .where(
                gb_jobs.c.id == job_id,
                gb_jobs.c.fence == fence,
                gb_jobs.c.state == "running",
            )
            .values(**values)
        )
        return result.rowcount == 1

    def cancel_jobs(self, conn, group_key, keep=CLEANUP_JOBS):
        """Cancel the group's open jobs except cleanup ones. A running job is fenced out: its fence
        rises and its lease ends, so it cannot finish or activate anything. Returns how many."""
        result = conn.execute(
            update(gb_jobs)
            .where(
                gb_jobs.c.group_key == group_key,
                gb_jobs.c.state.in_(("queued", "retry_wait", "running")),
                gb_jobs.c.kind.not_in(tuple(keep)),
            )
            .values(state="cancelled", fence=gb_jobs.c.fence + 1, lease_until=0)
        )
        return result.rowcount

    def retry_job(self, conn, group_key, job_id, generation):
        """Queue a failed job again under the group's current generation."""
        result = conn.execute(
            update(gb_jobs)
            .where(
                gb_jobs.c.id == job_id,
                gb_jobs.c.group_key == group_key,
                gb_jobs.c.state == "failed",
            )
            .values(
                state="queued",
                generation=generation,
                attempts=0,
                error_code=None,
                available_at=database_time(),
            )
        )
        return result.rowcount == 1

    def jobs_page(self, conn, group_key, limit=50, before=None):
        """Metadata of the group's jobs, newest first. No payload."""
        query = (
            select(
                gb_jobs.c.id,
                gb_jobs.c.kind,
                gb_jobs.c.state,
                gb_jobs.c.attempts,
                gb_jobs.c.error_code,
                gb_jobs.c.generation,
                gb_jobs.c.created_at,
            )
            .where(gb_jobs.c.group_key == group_key)
            .order_by(gb_jobs.c.created_at.desc(), gb_jobs.c.id.desc())
            .limit(max(1, min(limit, 200)))
        )
        if before is not None:
            query = query.where(gb_jobs.c.created_at < before)
        return [dict(r) for r in conn.execute(query).mappings()]

    def has_data(self, conn, group_key):
        """True while Group Brain holds anything for the group: an enabled or non-disabled state, or any
        canonical row. Removing the group's registration then would delete the rows but not the vectors
        or the audit of a purge, so it is refused until the group has been purged."""
        row = self.row(conn, group_key)
        if row and (row["enabled"] or row["state"] != "disabled"):
            return True
        return any(
            conn.execute(
                select(1)
                .select_from(table)
                .where(table.c.group_key == group_key)
                .limit(1)
            ).first()
            for table in CATALOG_TABLES
        )

    def job_kind(self, conn, group_key, job_id):
        """The kind of one of the group's jobs, or None for a job that is not this group's."""
        return conn.execute(
            select(gb_jobs.c.kind).where(
                gb_jobs.c.id == job_id, gb_jobs.c.group_key == group_key
            )
        ).scalar()

    def catalog_counts(self, conn, group_key):
        """Return safe counts for this group's canonical catalog."""
        discussions = conn.execute(
            select(func.count())
            .select_from(gb_discussions)
            .where(
                gb_discussions.c.group_key == group_key,
                gb_discussions.c.status == "active",
                gb_discussions.c.expires_at > func.now(),
            )
        ).scalar_one()
        memories = conn.execute(
            select(func.count())
            .select_from(gb_memory)
            .where(
                gb_memory.c.group_key == group_key,
                gb_memory.c.state == "active",
            )
        ).scalar_one()
        documents = conn.execute(
            select(func.count())
            .select_from(gb_index_entries)
            .where(
                gb_index_entries.c.group_key == group_key,
                gb_index_entries.c.kind == "document",
                gb_index_entries.c.state == "active",
            )
        ).scalar_one()
        pending = conn.execute(
            select(func.count())
            .select_from(gb_memory)
            .where(
                gb_memory.c.group_key == group_key,
                gb_memory.c.state == "pending",
            )
        ).scalar_one()
        return {
            "discussions": int(discussions),
            "memories": int(memories),
            "documents": int(documents),
            "pending_review": int(pending),
        }

    def coverage(self, conn, group_key):
        row = (
            conn.execute(
                select(gb_sync_state).where(
                    gb_sync_state.c.group_key == group_key,
                    gb_sync_state.c.source_kind == "conversation",
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return {"from": None, "to": None, "complete": False}
        return {
            "from": row["coverage_from"],
            "to": row["coverage_to"],
            "complete": row["status"] == "complete",
        }

    def job_counts(self, conn, group_key):
        rows = conn.execute(
            select(gb_jobs.c.state, func.count())
            .where(gb_jobs.c.group_key == group_key)
            .group_by(gb_jobs.c.state)
        ).all()
        return {state: count for state, count in rows}

    def job_age_seconds(self, conn, group_key):
        """How long the oldest waiting job has waited, or 0."""
        oldest = conn.execute(
            select(func.min(gb_jobs.c.available_at)).where(
                gb_jobs.c.group_key == group_key,
                gb_jobs.c.state.in_(("queued", "retry_wait")),
            )
        ).scalar()
        if oldest is None:
            return 0
        now = float(
            conn.execute(
                text("select extract(epoch from clock_timestamp())")
            ).scalar_one()
        )
        return max(0, int(now - oldest))
