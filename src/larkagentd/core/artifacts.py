"""Rows of a table preview and whole files, held encrypted until the Gateway attaches them.

The tool service stores a preview, or a file such as a job's SQL, under the turn's Hermes
session id. The model only learns the column names and the row count, or the file's name and
size, so the content never passes through the model's own output (where a long file would be
cut), Hermes, its logs or the model provider. The Gateway takes them, in the transaction that
queues the reply, and they are deleted as they are read. Anything left behind expires on its
own.
"""

import uuid

TTL_SECONDS = 1800
# Previews and files one turn may hold: a model that loops cannot flood the chat with them.
MAX_PER_SESSION = 6


class ArtifactLimit(Exception):
    """The turn already holds MAX_PER_SESSION attachments."""


class Artifacts:
    def __init__(self, store, vault, ttl=TTL_SECONDS):
        self.store, self.vault, self.ttl = store, vault, ttl

    @staticmethod
    def context(owner, session_id, ref):
        # Bound into the encryption, so a copied row cannot be opened for someone else.
        return f"artifact:{owner}:{session_id}:{ref}"

    def put(self, owner, session_id, text, requested=None, limit=None):
        body = {"text": text, "requested": requested, "limit": limit}
        return self.add(owner, session_id, body)

    def put_file(self, owner, session_id, name, text):
        """Store a file to be sent with the reply; ArtifactLimit when the turn holds too many."""
        return self.add(
            owner, session_id, {"file": {"name": name, "text": text}}, capped=True
        )

    def add(self, owner, session_id, body, capped=False):
        ref = uuid.uuid4().hex
        sealed = self.vault.seal(self.context(owner, session_id, ref), body)
        with self.store.engine.begin() as conn:
            if (
                capped
                and self.store.count_artifacts(conn, owner, session_id)
                >= MAX_PER_SESSION
            ):
                raise ArtifactLimit
            self.store.add_artifact(conn, ref, owner, session_id, sealed, self.ttl)
        return ref

    def take(self, conn, owner, session_ids, keep=None):
        """Delete this owner's artifacts of those sessions; return (previews, files) of `keep`.

        Each preview is {"text", "requested", "limit"}, each file {"name", "text"} (the same file
        twice is sent once). Artifacts of any other session (an abandoned first attempt) are
        discarded.
        """
        previews, files = [], []
        for session_id, ref, sealed in self.store.take_artifacts(
            conn, owner, session_ids
        ):
            if session_id != keep:
                continue
            try:
                body = self.vault.open(self.context(owner, session_id, ref), sealed)
                if "file" in body:
                    file = {"name": body["file"]["name"], "text": body["file"]["text"]}
                    if file not in files:
                        files.append(file)
                    continue
                previews.append(
                    {
                        "text": body["text"],
                        "requested": body.get("requested"),
                        "limit": body.get("limit"),
                    }
                )
            except Exception:
                continue  # unreadable rows are dropped, never shown
        return previews, files
