"""The job handlers of Group Brain, assembled in one place.

One job kind, `index_revision`, serves three kinds of content; `ref_payload["kind"]` picks which
('discussion' when absent). Everything else maps one kind of job to one handler."""

from . import documents, extraction, ingest

# Jobs that need the document reader (the Tool Service holds the Lark CLI). The Gateway's workers leave
# these to the Tool Service's workers, and the Tool Service's workers take only these.
DOCUMENT_KINDS = ("refresh_document", "verify_source")


def index_revision(ctx):
    kind = ctx.ref_payload.get("kind", "discussion")
    if kind == "memory":
        return extraction.index_memory_revision(ctx)
    if kind == "document":
        return documents.index_document_revision(ctx)
    return ingest.index_revision(ctx)


def handlers():
    merged = {}
    for part in (ingest.handlers(), extraction.handlers(), documents.handlers()):
        merged.update(part)
    merged["index_revision"] = index_revision
    return merged
