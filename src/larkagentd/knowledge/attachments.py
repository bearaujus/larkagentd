"""Dashboard-managed Lark attachments and searchable, source-owned passages."""

import re
from urllib.parse import urlsplit

from sqlalchemy import (
    Column,
    Computed,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR

from ..core.store import metadata
from ..lark.links import TOKEN, DocumentLink, lark_host
from .store import KnowledgeError

attachments = Table(
    "knowledge_lark_attachments",
    metadata,
    Column(
        "source_id",
        String,
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("url", String, nullable=False),
    Column("title", Text),
    Column("kind", String, nullable=False),
    Column("status", String, nullable=False, server_default="queued"),
    Column("due", Float, nullable=False, server_default="0"),
    Column("lease", String),
    Column("last_success", Float),
    Column("indexed", Integer, nullable=False, server_default="0"),
    Column("skipped", Integer, nullable=False, server_default="0"),
    Column("error", String),
    Index("knowledge_lark_due", "due"),
)

passages = Table(
    "knowledge_lark_passages",
    metadata,
    Column(
        "source_id",
        String,
        ForeignKey("knowledge_lark_attachments.source_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("doc", String, primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    Column("doc_kind", String, nullable=False),
    Column("node", String),
    Column("title", Text, nullable=False),
    Column("section", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("digest", String, nullable=False),
    Column("seen", String, nullable=False),
    Column(
        "search_vector",
        TSVECTOR,
        Computed(
            "to_tsvector('simple', title || ' ' || section || ' ' || body)",
            persisted=True,
        ),
    ),
    Index("knowledge_lark_search", "search_vector", postgresql_using="gin"),
)


def attachment_link(value):
    """Extract only the identifier. Requests go to the configured Lark API."""
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or not value.isascii()
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise KnowledgeError("invalid_lark_url")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or not lark_host(host)
            or "\\" in value
        ):
            raise ValueError
        parts = parsed.path.removesuffix("/").split("/")
        if (
            len(parts) == 4
            and parts[1:3] == ["wiki", "settings"]
            and re.fullmatch(r"[0-9]{1,24}", parts[3])
        ):
            kind, token = "space", parts[3]
        elif (
            len(parts) == 3
            and parts[1] in ("wiki", "docs", "docx")
            and (parts[1:] != ["wiki", "settings"])
            and TOKEN.fullmatch(parts[2])
        ):
            kind, token = parts[1:]
        else:
            raise ValueError
    except ValueError:
        raise KnowledgeError("invalid_lark_url") from None
    return DocumentLink(kind, token, f"https://{host}{parsed.path.rstrip('/')}")
