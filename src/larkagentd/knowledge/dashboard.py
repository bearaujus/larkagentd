"""The dashboard routes of Group Brain: /groups/<chat id>/knowledge.

`Access.dispatch` hands over after it has authenticated the session and checked that the group is
registered. Reads are for admins and managers; every change is admin only. Responses carry
metadata, counts and states. The review queue also carries a candidate's text, and only to an admin who
may read its source (see MemoryReview); document text is never returned."""

from datetime import datetime

from .dashboard_review import MemoryReview
from .library import serve_sources
from .models import CAPABILITIES
from .policy import effective, store
from .store import KnowledgeError, VersionConflict

SETTINGS = {
    "enabled",
    "recall_enabled",
    "memory_enabled",
    "learning_mode",
    "history_window_days",
    "notice_ref",
    "policy_ref",
}
JOB_STATES = ("queued", "running", "retry_wait", "failed", "succeeded", "cancelled")


def plain(value):
    """The value with every time as an RFC 3339 string: the HTTP layer writes plain JSON."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def integer(value, minimum=0):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise KnowledgeError("invalid_value")
    return value


def query_value(query, name):
    """The integer value of `name` in a query string, or None. Anything else is refused."""
    found = [pair.partition("=") for pair in (query or "").split("&") if pair]
    values = [value for key, _, value in found if key == name]
    if len(values) > 1 or any(key != name for key, _, _ in found):
        raise KnowledgeError("invalid_filter")
    if not values:
        return None
    if not values[0].isdigit() or len(values[0]) > 18:
        raise KnowledgeError("invalid_filter")
    return int(values[0])


class KnowledgeDashboard:
    def __init__(self, config, index_health=None, runtime=None):
        self.config = config
        # The runtime that holds the semantic index, so an approval that needs indexing enqueues it.
        self.memory = MemoryReview(config, runtime)
        # Returns 'ready' or 'unavailable' for the semantic index; None means nothing has proven it yet.
        self.index_health = index_health

    def handle(self, conn, actor, scope, rest, method, body, query=""):
        """(status, result) for a path under /groups/<id>/knowledge. Raises KnowledgeError."""
        if rest == ["sources"]:
            if actor["role"] not in ("admin", "group_admin"):
                raise KnowledgeError("admin_required", 403)
            return serve_sources(conn, scope, method, body, {})
        if (
            len(rest) == 3
            and rest[0] == "learned"
            and rest[-1] == "retire"
            and method == "POST"
        ):
            if actor["role"] not in ("admin", "group_admin"):
                raise KnowledgeError("admin_required", 403)
            return 200, self.memory.revoke(
                conn,
                {**actor, "role": "admin", "email": actor["email"] or actor["owner"]},
                scope,
                rest[1],
                body,
            )
        if rest == [] and method == "GET":
            return 200, self.view(conn, scope)
        if rest == [] and method == "PATCH":
            self.admin(actor)
            return 200, self.patch(conn, actor, scope, body)
        if len(rest) == 1 and rest[0] in ("pause", "resume") and method == "POST":
            self.admin(actor)
            self.exact(body, {"version"})
            store.transition(
                conn, scope, rest[0], actor["email"], integer(body["version"])
            )
            return 200, self.view(conn, scope)
        if rest == ["purge"] and method == "POST":
            self.admin(actor)
            self.exact(body, {"version", "confirm"})
            # The person types the group's chat id: a stale page or a wrong group cannot purge.
            if body["confirm"] != scope.removeprefix("group:"):
                raise KnowledgeError("invalid_confirmation")
            store.transition(
                conn, scope, "purge", actor["email"], integer(body["version"])
            )
            return 200, self.view(conn, scope)
        if rest == ["jobs"] and method == "GET":
            return 200, {"jobs": [self.job(j) for j in store.jobs_page(conn, scope)]}
        if (
            len(rest) == 3
            and rest[0] == "jobs"
            and rest[2] == "retry"
            and method == "POST"
        ):
            self.admin(actor)
            self.exact(body, set())
            return 200, self.retry(conn, actor, scope, rest[1])
        review = self.review(conn, actor, scope, rest, method, body)
        if review is not None:
            return review[0], plain(review[1])
        if rest == ["audit"] and method == "GET":
            before = query_value(query, "before")
            return 200, {
                "events": [
                    self.event(e) for e in store.audit_page(conn, scope, 50, before)
                ]
            }
        raise KnowledgeError("method_not_allowed", 405)

    def review(self, conn, actor, scope, rest, method, body):
        """The review queue for memory candidates and registered documents, or None for another path."""
        if rest == ["candidates"] and method == "GET":
            return 200, {"candidates": self.memory.candidates(conn, scope, actor)}
        if len(rest) == 3 and method == "POST":
            kind, item, action = rest
            routes = {
                ("candidates", "approve"): self.memory.approve,
                ("candidates", "reject"): self.memory.reject,
                ("memory", "revoke"): self.memory.revoke,
            }
            if (kind, action) in routes:
                return 200, routes[(kind, action)](conn, actor, scope, item, body)
        return None

    @staticmethod
    def admin(actor):
        if actor["role"] != "admin":
            raise KnowledgeError("admin_required", 403)

    @staticmethod
    def exact(body, fields):
        if not isinstance(body, dict) or set(body) != fields:
            raise KnowledgeError("invalid_fields")

    def patch(self, conn, actor, scope, body):
        if not isinstance(body, dict) or "version" not in body:
            raise KnowledgeError("invalid_fields")
        allowed = SETTINGS | {"version", "acknowledge_group_sharing"}
        if set(body) - allowed:
            raise KnowledgeError("invalid_fields")
        version = integer(body["version"])
        changes = {k: v for k, v in body.items() if k in SETTINGS}
        current = store.settings(conn, scope)
        if changes.get("enabled") is True and not current["enabled"]:
            # Turning it on is a decision about the group's data: it needs the recorded notice and
            # policy and an explicit acknowledgement that members of the group can retrieve it.
            if body.get("acknowledge_group_sharing") is not True:
                raise KnowledgeError("missing_group_notice", 409)
        elif "acknowledge_group_sharing" in body and not isinstance(
            body["acknowledge_group_sharing"], bool
        ):
            raise KnowledgeError("invalid_value")
        store.update_settings(
            conn,
            scope,
            version,
            changes,
            actor["email"],
            max_history_days=self.config.max_history_days,
        )
        return self.view(conn, scope)

    def retry(self, conn, actor, scope, job_id):
        kind = store.job_kind(conn, scope, job_id)
        if kind is None:
            raise KnowledgeError("not_found", 404)
        state = effective(conn, self.config, scope)
        if kind not in store.cleanup_kinds and not state.serving:
            raise KnowledgeError("group_disabled", 409)
        if not store.retry_job(conn, scope, job_id, state.generation):
            raise KnowledgeError("invalid_transition", 409)
        store.audit(conn, scope, actor["email"], "job_retried", target_id=job_id)
        return {"ok": True}

    def view(self, conn, scope):
        state = effective(conn, self.config, scope)
        settings = store.settings(conn, scope)
        counts = store.catalog_counts(conn, scope)
        jobs = {name: 0 for name in JOB_STATES}
        jobs.update(store.job_counts(conn, scope))
        return {
            "configured_enabled": bool(settings["enabled"]),
            "effective_state": state.state,
            "reason_codes": list(state.reasons),
            "learning_mode": settings["learning_mode"],
            "capabilities": {
                "recall": bool(settings["recall_enabled"]),
                "memory": bool(settings["memory_enabled"]),
                "documents": bool(settings["documents_enabled"]),
            },
            "effective_capabilities": {
                name: bool(state.capabilities.get(name)) for name in CAPABILITIES
            },
            "history_window_days": settings["history_window_days"],
            "max_history_days": self.config.max_history_days,
            "review_required": True,
            "version": settings["version"],
            "generation": settings["generation"],
            "notice_ref": settings["notice_ref"],
            "policy_ref": settings["policy_ref"],
            "counts": counts,
            "coverage": store.coverage(conn, scope),
            "health": {
                "semantic_index": self.semantic_health(),
                "lark_history": "unknown",
            },
            "jobs": jobs,
            "oldest_job_age_seconds": store.job_age_seconds(conn, scope),
            "updated_at": settings["updated_at"].isoformat()
            if settings["updated_at"]
            else None,
            "updated_by": settings["updated_by"],
        }

    def semantic_health(self):
        if not self.config.embedding_configured:
            return "not_configured"
        if self.index_health is None:
            return "unavailable"  # configured, but nothing has proven it works yet
        return "ready" if self.index_health() == "ready" else "unavailable"

    @staticmethod
    def job(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "state": row["state"],
            "attempts": row["attempts"],
            "error_code": row["error_code"],
            "generation": row["generation"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }

    @staticmethod
    def event(row):
        return {
            "id": row["id"],
            "actor_id": row["actor_id"],
            "action": row["action"],
            "target_id": row["target_id"],
            "outcome": row["outcome"],
            "reason_code": row["reason_code"],
            "policy_version": row["policy_version"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }


__all__ = ["KnowledgeDashboard", "KnowledgeError", "VersionConflict"]
