from .documents import Documents
from .store import KnowledgeError, KnowledgeStore


class DocumentsReview:
    def __init__(self, config=None):
        self.config = config
        self.store = KnowledgeStore()

    def documents(self, conn, scope, actor):
        if not isinstance(actor, dict) or actor.get("role") not in ("admin", "manager"):
            raise KnowledgeError("admin_required", 403)
        return Documents(config=self.config).list_candidates(conn, scope)

    def approve(self, conn, actor, scope, document_id, body):
        self._admin(actor)
        self._exact(body, {"sharing_policy_ref", "acknowledge_group_sharing"})
        if body["acknowledge_group_sharing"] is not True:
            raise KnowledgeError("sharing_authorization_required", 403)
        return Documents(config=self.config).approve(
            conn, scope, document_id, actor["email"], body["sharing_policy_ref"], True
        )

    def reject(self, conn, actor, scope, document_id, body):
        self._admin(actor)
        self._exact(body, set())
        return Documents(config=self.config).reject(
            conn, scope, document_id, actor["email"]
        )

    def refresh(self, conn, actor, scope, document_id):
        self._admin(actor)
        return Documents(config=self.config).request_refresh(
            conn, scope, document_id, actor["email"]
        )

    def revoke(self, conn, actor, scope, document_id, body):
        self._admin(actor)
        self._exact(body, set())
        return Documents(config=self.config).revoke(
            conn, scope, document_id, actor["email"]
        )

    @staticmethod
    def _admin(actor):
        if not isinstance(actor, dict) or actor.get("role") != "admin":
            raise KnowledgeError("admin_required", 403)

    @staticmethod
    def _exact(body, fields):
        if not isinstance(body, dict) or set(body) != fields:
            raise KnowledgeError("invalid_fields")
