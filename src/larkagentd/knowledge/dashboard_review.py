from __future__ import annotations

import hashlib

from sqlalchemy import select

from .memory import Memory
from .policy import effective
from .store import KnowledgeError, KnowledgeStore
from .tables import gb_memory_revisions


class MemoryReview:
    def __init__(self, config=None, runtime=None, store=None):
        self.config = config or (runtime.config if runtime else None)
        self.runtime = runtime
        self.store = store or KnowledgeStore()

    @staticmethod
    def _admin(actor):
        if not isinstance(actor, dict) or actor.get("role") != "admin":
            raise KnowledgeError("admin_required", 403)

    def candidates(self, conn, scope, actor):
        email = (
            actor.get("email")
            if isinstance(actor, dict) and actor.get("role") == "admin"
            else None
        )
        return Memory(self.runtime).list_candidates(conn, scope, reviewer_email=email)

    def approve(self, conn, actor, scope, memory_id, body):
        self._admin(actor)
        if self.config is None or not effective(conn, self.config, scope).allows(
            "memory"
        ):
            raise KnowledgeError("disabled", 409)
        if not isinstance(body, dict) or set(body) != {
            "revision",
            "content_hash",
            "acknowledge_group_sharing",
        }:
            raise KnowledgeError("invalid_fields")
        revision = body["revision"]
        content_hash = body["content_hash"]
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or not isinstance(content_hash, str)
            or len(content_hash) != 64
            or any(c not in "0123456789abcdef" for c in content_hash)
            or body["acknowledge_group_sharing"] is not True
        ):
            raise KnowledgeError("invalid_value")
        if actor.get("correction_evidence") != (scope, memory_id, revision):
            raise KnowledgeError("source_unavailable", 403)
        row = conn.execute(
            select(gb_memory_revisions.c.content_hash).where(
                gb_memory_revisions.c.group_key == scope,
                gb_memory_revisions.c.memory_id == memory_id,
                gb_memory_revisions.c.revision == revision,
            )
        ).scalar_one_or_none()
        if row is None:
            raise KnowledgeError("not_found", 404)
        if row != content_hash:
            raise KnowledgeError("hash_mismatch", 409)
        result = Memory(self.runtime).approve(
            conn, scope, memory_id, revision, content_hash, actor.get("email", ""), True
        )
        status = result.get("status")
        if status not in ("active", "approved_pending_index"):
            raise KnowledgeError(status or "approval_failed", 409)
        if status == "approved_pending_index":
            settings = self.store.settings(conn, scope)
            key = hashlib.sha256(
                f"{scope}\x1fmemory\x1f{memory_id}\x1f{revision}\x1findex".encode()
            ).hexdigest()
            self.store.enqueue(
                conn,
                scope,
                "index_revision",
                key,
                settings["generation"],
                source_revision=revision,
                ref_payload={
                    "kind": "memory",
                    "memory_id": memory_id,
                    "revision": revision,
                },
            )
        self.store.audit(
            conn, scope, actor.get("email", ""), "memory_approved", target_id=memory_id
        )
        return result

    def reject(self, conn, actor, scope, memory_id, body):
        self._admin(actor)
        if not isinstance(body, dict) or set(body) != {"revision", "reason_code"}:
            raise KnowledgeError("invalid_fields")
        revision, reason = body["revision"], body["reason_code"]
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or not isinstance(reason, str)
            or not reason
            or len(reason) > 64
            or not reason.replace("_", "").isalnum()
        ):
            raise KnowledgeError("invalid_value")
        result = Memory(self.runtime).reject(conn, scope, memory_id, revision, reason)
        if result["status"] != "rejected":
            raise KnowledgeError("not_found", 404)
        self.store.audit(
            conn, scope, actor.get("email", ""), "memory_rejected", target_id=memory_id
        )
        return result

    def revoke(self, conn, actor, scope, memory_id, body=None):
        self._admin(actor)
        if body not in (None, {}):
            raise KnowledgeError("invalid_fields")
        result = Memory(self.runtime).revoke(
            conn, scope, memory_id, actor.get("email", "")
        )
        if result["status"] != "revoked":
            raise KnowledgeError("not_found", 404)
        self.store.audit(
            conn, scope, actor.get("email", ""), "memory_revoked", target_id=memory_id
        )
        return result
