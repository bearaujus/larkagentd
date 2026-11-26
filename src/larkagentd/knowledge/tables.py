"""Group Brain tables. They share the application metadata, so the test fixtures create them
and the migrations in migrations/versions mirror them.

Every group-scoped row carries `group_key`, the registered group's scope (`group:<chat id>`), and
references `gateway_policies.scope` with ON DELETE CASCADE: removing a registered group removes its
settings, jobs and, later, its canonical knowledge. Audit rows have no foreign key: they are
metadata only and must outlive the group they describe."""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    func,
    text,
)

from ..core.store import metadata
from .models import JOB_STATES, LEARNING_MODES, STATES


def _in(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def group_fk():
    return ForeignKey("gateway_policies.scope", ondelete="CASCADE")


group_knowledge_settings = Table(
    "group_knowledge_settings",
    metadata,
    Column("group_key", String, group_fk(), primary_key=True),
    Column("enabled", Boolean, nullable=False, default=False, server_default="false"),
    Column(
        "state", String, nullable=False, default="disabled", server_default="disabled"
    ),
    Column(
        "recall_enabled", Boolean, nullable=False, default=True, server_default="true"
    ),
    Column(
        "memory_enabled", Boolean, nullable=False, default=True, server_default="true"
    ),
    Column(
        "documents_enabled",
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    ),
    Column(
        "learning_mode",
        String,
        nullable=False,
        default="explicit",
        server_default="explicit",
    ),
    Column(
        "review_required", Boolean, nullable=False, default=True, server_default="true"
    ),
    Column(
        "history_window_days",
        Integer,
        nullable=False,
        default=30,
        server_default="30",
    ),
    # Raised by every change that affects what may be served or collected; work captures it.
    Column("generation", BigInteger, nullable=False, default=1, server_default="1"),
    # Raised by every change; a dashboard write must name the version it read.
    Column("version", BigInteger, nullable=False, default=1, server_default="1"),
    Column("enabled_at", DateTime(timezone=True)),
    Column("enabled_by", String),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_by", String),
    Column("notice_ref", String),
    Column("policy_ref", String),
    CheckConstraint(_in("state", STATES), name="gb_settings_state"),
    CheckConstraint(_in("learning_mode", LEARNING_MODES), name="gb_settings_mode"),
    CheckConstraint("review_required", name="gb_settings_review_locked"),
    CheckConstraint("history_window_days BETWEEN 1 AND 365", name="gb_settings_window"),
)

gb_audit_events = Table(
    "gb_audit_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    # No foreign key: the record of a purge must outlive the group.
    Column("group_key", String, nullable=False),
    Column("actor_id", String, nullable=False),
    Column("action", String, nullable=False),
    Column("target_id", String),
    Column("policy_version", BigInteger),
    Column("outcome", String, nullable=False),
    Column("reason_code", String),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("correlation_id", String),
    Index("gb_audit_group_time", "group_key", "created_at"),
)

gb_jobs = Table(
    "gb_jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("group_key", String, group_fk(), nullable=False),
    Column("kind", String, nullable=False),
    Column("idempotency_key", String, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("source_revision", BigInteger),
    Column("state", String, nullable=False, default="queued", server_default="queued"),
    Column("attempts", Integer, nullable=False, default=0, server_default="0"),
    Column("available_at", Float, nullable=False, default=0, server_default="0"),
    Column("lease_owner", String),
    Column("lease_until", Float, nullable=False, default=0, server_default="0"),
    # Raised on every claim: a worker that lost its lease can no longer finish the job.
    Column("fence", BigInteger, nullable=False, default=0, server_default="0"),
    Column("error_code", String),
    # References and counters only, never message text.
    Column("ref_payload", JSON, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    CheckConstraint(_in("state", JOB_STATES), name="gb_jobs_state"),
    UniqueConstraint("group_key", "idempotency_key", name="gb_jobs_idempotency"),
    Index("gb_jobs_claim", "state", "available_at"),
)

# Canonical tables that hold a group's knowledge, parents before children. A purge deletes them in
# reverse. Later phases append here; the settings, audit and job tables are not part of it.
CATALOG_TABLES = []


# --- Phase 2: the conversation catalog ----------------------------------------------------
# Canonical text and lineage for discussions. The semantic index only holds derived copies of what is
# here; a vector is served only when an `active` row of gb_index_entries names it.
from sqlalchemy import ARRAY, Computed, ForeignKeyConstraint, Text  # noqa: E402
from sqlalchemy.dialects.postgresql import TSVECTOR  # noqa: E402

MESSAGE_STATUS = ("visible", "recalled", "unverified", "blocked")
ROW_STATUS = (
    "active",
    "suspended",
    "superseded",
    "revoked",
    "failed",
    "expired",
    "purging",
)
INDEX_STATES = (
    "pending",
    "indexing",
    "active",
    "stale",
    "delete_pending",
    "deleted",
    "failed",
)
INDEX_KINDS = ("discussion", "memory", "document")

gb_messages = Table(
    "gb_messages",
    metadata,
    Column("id", String, primary_key=True),
    Column("group_key", String, group_fk(), nullable=False),
    Column("lark_message_id", String, nullable=False),
    Column("thread_id", String),
    Column("root_id", String),
    Column("parent_id", String),
    Column("sender_type", String, nullable=False),
    Column("sender_id", String),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("edited_at", DateTime(timezone=True)),
    # Only a verified official message link, never one built from ids or positions.
    Column("message_link", String),
    # Sanitized text only. Null when the message was blocked: nothing about it is kept but its id.
    Column("sanitized_text", Text),
    Column("content_hash", String, nullable=False),
    Column(
        "visibility_status",
        String,
        nullable=False,
        default="visible",
        server_default="visible",
    ),
    Column("revision", Integer, nullable=False, default=1, server_default="1"),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("status", String, nullable=False, default="active", server_default="active"),
    UniqueConstraint("group_key", "lark_message_id", name="gb_messages_lark"),
    UniqueConstraint("group_key", "id", name="gb_messages_group_id"),
    CheckConstraint(
        _in("visibility_status", MESSAGE_STATUS), name="gb_messages_visibility"
    ),
    CheckConstraint(_in("status", ROW_STATUS), name="gb_messages_status"),
    Index("gb_messages_thread", "group_key", "thread_id"),
    Index("gb_messages_time", "group_key", "created_at"),
    Index("gb_messages_sender", "group_key", "sender_id", "created_at"),
)

gb_discussions = Table(
    "gb_discussions",
    metadata,
    Column("id", String, primary_key=True),
    Column("group_key", String, group_fk(), nullable=False),
    Column("kind", String, nullable=False),
    # A real Lark thread id, only for kind native_thread. A synthetic window never has one.
    Column("native_thread_id", String),
    Column("root_message_id", String),
    Column("stable_window_key", String),
    Column("title", Text, nullable=False, default="", server_default=""),
    Column("summary", Text, nullable=False, default="", server_default=""),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True), nullable=False),
    Column("status", String, nullable=False, default="active", server_default="active"),
    Column("source_revision", Integer, nullable=False, default=1, server_default="1"),
    Column(
        "coverage", String, nullable=False, default="partial", server_default="partial"
    ),
    Column(
        "identifiers", ARRAY(String), nullable=False, default=list, server_default="{}"
    ),
    Column(
        "search_vector",
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(summary, ''))",
            persisted=True,
        ),
    ),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("group_key", "id", name="gb_discussions_group_id"),
    CheckConstraint(
        _in("kind", ("native_thread", "chat_window")), name="gb_discussions_kind"
    ),
    CheckConstraint(_in("status", ROW_STATUS), name="gb_discussions_status"),
    CheckConstraint(
        _in("coverage", ("complete", "partial")), name="gb_discussions_coverage"
    ),
    CheckConstraint(
        "(kind = 'native_thread' AND native_thread_id IS NOT NULL) OR "
        "(kind = 'chat_window' AND native_thread_id IS NULL AND stable_window_key IS NOT NULL)",
        name="gb_discussions_identity",
    ),
    Index(
        "gb_discussions_thread",
        "group_key",
        "native_thread_id",
        unique=True,
        postgresql_where=text("native_thread_id IS NOT NULL"),
    ),
    Index(
        "gb_discussions_window",
        "group_key",
        "stable_window_key",
        unique=True,
        postgresql_where=text("stable_window_key IS NOT NULL"),
    ),
    Index("gb_discussions_time", "group_key", "status", "started_at", "ended_at"),
    Index("gb_discussions_search", "search_vector", postgresql_using="gin"),
)

gb_discussion_messages = Table(
    "gb_discussion_messages",
    metadata,
    Column("group_key", String, nullable=False),
    Column("discussion_id", String, nullable=False),
    Column("message_id", String, nullable=False),
    Column("ordinal", Integer, nullable=False),
    ForeignKeyConstraint(
        ["group_key", "discussion_id"],
        ["gb_discussions.group_key", "gb_discussions.id"],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["group_key", "message_id"],
        ["gb_messages.group_key", "gb_messages.id"],
        ondelete="CASCADE",
    ),
    UniqueConstraint("discussion_id", "message_id", name="gb_discussion_messages_pk"),
    UniqueConstraint("discussion_id", "ordinal", name="gb_discussion_messages_ordinal"),
)

gb_discussion_participants = Table(
    "gb_discussion_participants",
    metadata,
    Column("group_key", String, nullable=False),
    Column("discussion_id", String, nullable=False),
    Column("sender_id", String, nullable=False),
    Column("first_at", DateTime(timezone=True), nullable=False),
    Column("last_at", DateTime(timezone=True), nullable=False),
    Column("authored_count", Integer, nullable=False),
    ForeignKeyConstraint(
        ["group_key", "discussion_id"],
        ["gb_discussions.group_key", "gb_discussions.id"],
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "discussion_id", "sender_id", name="gb_discussion_participants_pk"
    ),
    Index("gb_participants_sender", "group_key", "sender_id", "discussion_id"),
)

gb_source_refs = Table(
    "gb_source_refs",
    metadata,
    Column("id", String, primary_key=True),
    Column("group_key", String, group_fk(), nullable=False),
    Column("source_kind", String, nullable=False),
    Column("canonical_source_id", String, nullable=False),
    Column("source_revision", Integer, nullable=False, default=1, server_default="1"),
    Column("lark_message_id", String),
    Column("document_id", String),
    Column("verified_at", DateTime(timezone=True)),
    Column(
        "visibility_status",
        String,
        nullable=False,
        default="visible",
        server_default="visible",
    ),
    Column("link_type", String, nullable=False, default="none", server_default="none"),
    Column("source_url", String),
    CheckConstraint(
        _in("source_kind", ("message", "document")), name="gb_source_refs_kind"
    ),
    CheckConstraint(
        _in("link_type", ("message", "group_fallback", "none")),
        name="gb_source_refs_link",
    ),
    CheckConstraint(
        "(source_kind = 'message' AND lark_message_id IS NOT NULL) OR "
        "(source_kind = 'document' AND document_id IS NOT NULL)",
        name="gb_source_refs_target",
    ),
    UniqueConstraint(
        "group_key",
        "source_kind",
        "canonical_source_id",
        "source_revision",
        name="gb_source_refs_unique",
    ),
)

gb_index_entries = Table(
    "gb_index_entries",
    metadata,
    Column("id", String, primary_key=True),
    Column("group_key", String, group_fk(), nullable=False),
    Column("kind", String, nullable=False),
    Column("canonical_id", String, nullable=False),
    Column("source_revision", Integer, nullable=False),
    Column("chunk_ordinal", Integer, nullable=False, default=0, server_default="0"),
    Column("namespace", String, nullable=False),
    Column("provider_id", String),
    Column("idempotency_key", String, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column(
        "state", String, nullable=False, default="pending", server_default="pending"
    ),
    Column("content_hash", String, nullable=False),
    Column("embedding_model", String, nullable=False),
    Column("dimensions", Integer, nullable=False),
    Column("indexed_at", DateTime(timezone=True)),
    Column("last_error_code", String),
    CheckConstraint(_in("kind", INDEX_KINDS), name="gb_index_kind"),
    CheckConstraint(_in("state", INDEX_STATES), name="gb_index_state"),
    # An entry serves only while it names the record the provider holds.
    CheckConstraint(
        "state <> 'active' OR provider_id IS NOT NULL",
        name="gb_index_active_has_provider",
    ),
    UniqueConstraint("group_key", "idempotency_key", name="gb_index_idempotency"),
    Index(
        "gb_index_provider",
        "provider_id",
        unique=True,
        postgresql_where=text("provider_id IS NOT NULL"),
    ),
    Index("gb_index_lookup", "group_key", "kind", "canonical_id", "state"),
)

gb_sync_state = Table(
    "gb_sync_state",
    metadata,
    Column("group_key", String, group_fk(), primary_key=True),
    Column("source_kind", String, primary_key=True),
    Column("cursor", Text),
    Column("watermark", DateTime(timezone=True)),
    Column("last_success_at", DateTime(timezone=True)),
    Column("status", String, nullable=False, default="idle", server_default="idle"),
    Column("coverage_from", DateTime(timezone=True)),
    Column("coverage_to", DateTime(timezone=True)),
    Column("last_error_code", String),
)

# Parents first; a purge deletes in reverse.
CATALOG_TABLES.extend(
    [
        gb_messages,
        gb_discussions,
        gb_discussion_messages,
        gb_discussion_participants,
        gb_source_refs,
        gb_index_entries,
        gb_sync_state,
    ]
)


# --- Phase 3: collective memory and registered documents -------------------------------------
MEMORY_CATEGORIES = (
    "decision",
    "business_logic",
    "procedure",
    "technical_discovery",
    "lesson",
    "standard",
    "project_context",
)
MEMORY_STATES = (
    "pending",
    "approved_pending_index",
    "active",
    "suspended",
    "superseded",
    "revoked",
    "failed",
    "purging",
)
REVISION_STATES = (
    "pending",
    "approved",
    "active",
    "rejected",
    "superseded",
    "revoked",
    "suspended",
)
EVIDENCE_STATUS = ("explicit", "supported", "uncertain", "conflicting")


def _search_vector(column):
    return Column(
        "search_vector",
        TSVECTOR,
        Computed(f"to_tsvector('simple', coalesce({column}, ''))", persisted=True),
    )


gb_memory = Table(
    "gb_memory",
    metadata,
    Column("id", String, primary_key=True),
    Column("group_key", String, group_fk(), nullable=False),
    Column("category", String, nullable=False),
    # The revision that is served; null until one is approved and activated.
    Column("current_revision", Integer),
    Column(
        "state", String, nullable=False, default="pending", server_default="pending"
    ),
    # The open id of whoever proposed it (a verified turn), never a name.
    Column("created_by", String, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("supersedes_id", String),
    UniqueConstraint("group_key", "id", name="gb_memory_group_id"),
    CheckConstraint(_in("category", MEMORY_CATEGORIES), name="gb_memory_category"),
    CheckConstraint(_in("state", MEMORY_STATES), name="gb_memory_state"),
    Index("gb_memory_state_idx", "group_key", "state"),
)

gb_memory_revisions = Table(
    "gb_memory_revisions",
    metadata,
    Column("group_key", String, nullable=False),
    Column("memory_id", String, nullable=False),
    Column("revision", Integer, nullable=False),
    # Immutable once written: a correction is a new revision.
    Column("sanitized_content", Text, nullable=False),
    Column("content_hash", String, nullable=False),
    Column("proposed_by", String, nullable=False),
    Column("approved_by", String),
    Column(
        "status", String, nullable=False, default="pending", server_default="pending"
    ),
    Column(
        "evidence_status",
        String,
        nullable=False,
        default="explicit",
        server_default="explicit",
    ),
    Column("reason", String),
    Column(
        "identifiers", ARRAY(String), nullable=False, default=list, server_default="{}"
    ),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("approved_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True)),
    _search_vector("sanitized_content"),
    ForeignKeyConstraint(
        ["group_key", "memory_id"],
        ["gb_memory.group_key", "gb_memory.id"],
        ondelete="CASCADE",
    ),
    UniqueConstraint("memory_id", "revision", name="gb_memory_revisions_pk"),
    UniqueConstraint(
        "group_key", "memory_id", "revision", name="gb_memory_revisions_group"
    ),
    CheckConstraint(_in("status", REVISION_STATES), name="gb_memory_revisions_status"),
    CheckConstraint(
        _in("evidence_status", EVIDENCE_STATUS), name="gb_memory_revisions_evidence"
    ),
    Index("gb_memory_revisions_search", "search_vector", postgresql_using="gin"),
)

gb_memory_sources = Table(
    "gb_memory_sources",
    metadata,
    Column("group_key", String, nullable=False),
    Column("memory_id", String, nullable=False),
    Column("memory_revision", Integer, nullable=False),
    Column("source_ref_id", String, nullable=False),
    ForeignKeyConstraint(
        ["group_key", "memory_id", "memory_revision"],
        [
            "gb_memory_revisions.group_key",
            "gb_memory_revisions.memory_id",
            "gb_memory_revisions.revision",
        ],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(["source_ref_id"], ["gb_source_refs.id"], ondelete="CASCADE"),
    UniqueConstraint(
        "memory_id", "memory_revision", "source_ref_id", name="gb_memory_sources_pk"
    ),
    # Finds every memory a source supports, so a recalled source can suspend them.
    Index("gb_memory_sources_ref", "source_ref_id"),
)

DOCUMENT_STATES = MEMORY_STATES

gb_documents = Table(
    "gb_documents",
    metadata,
    Column("id", String, primary_key=True),
    Column("group_key", String, group_fk(), nullable=False),
    Column("provider_kind", String, nullable=False),
    # The canonical document token after a wiki node is resolved; the key of the document.
    Column("canonical_doc_token", String, nullable=False),
    Column("wiki_token", String),
    Column("source_url", String, nullable=False),
    Column("title", Text, nullable=False, default="", server_default=""),
    # The administrator's recorded authorization to share this document with the group.
    Column("sharing_policy_ref", String),
    Column("current_revision", Integer),
    Column(
        "state", String, nullable=False, default="pending", server_default="pending"
    ),
    Column("created_by", String, nullable=False),
    Column("last_checked_at", DateTime(timezone=True)),
    Column("last_synced_at", DateTime(timezone=True)),
    UniqueConstraint("group_key", "id", name="gb_documents_group_id"),
    UniqueConstraint("group_key", "canonical_doc_token", name="gb_documents_token"),
    CheckConstraint(
        _in("provider_kind", ("lark_docx", "lark_wiki")), name="gb_documents_kind"
    ),
    CheckConstraint(_in("state", DOCUMENT_STATES), name="gb_documents_state"),
)

gb_document_revisions = Table(
    "gb_document_revisions",
    metadata,
    Column("group_key", String, nullable=False),
    Column("document_id", String, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("provider_revision", String),
    Column("content_hash", String, nullable=False),
    Column(
        "state", String, nullable=False, default="pending", server_default="pending"
    ),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("checked_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ["group_key", "document_id"],
        ["gb_documents.group_key", "gb_documents.id"],
        ondelete="CASCADE",
    ),
    UniqueConstraint("document_id", "revision", name="gb_document_revisions_pk"),
    UniqueConstraint(
        "group_key", "document_id", "revision", name="gb_document_revisions_group"
    ),
    CheckConstraint(_in("state", DOCUMENT_STATES), name="gb_document_revisions_state"),
)

gb_document_chunks = Table(
    "gb_document_chunks",
    metadata,
    Column("id", String, primary_key=True),
    Column("group_key", String, nullable=False),
    Column("document_id", String, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("section_path", Text, nullable=False, default="", server_default=""),
    Column("block_anchor", String),
    Column("sanitized_content", Text, nullable=False),
    Column("content_hash", String, nullable=False),
    Column("token_count", Integer, nullable=False),
    _search_vector("sanitized_content"),
    ForeignKeyConstraint(
        ["group_key", "document_id", "revision"],
        [
            "gb_document_revisions.group_key",
            "gb_document_revisions.document_id",
            "gb_document_revisions.revision",
        ],
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "document_id", "revision", "ordinal", name="gb_document_chunks_pk"
    ),
    Index("gb_document_chunks_search", "search_vector", postgresql_using="gin"),
)

CATALOG_TABLES.extend(
    [
        gb_memory,
        gb_memory_revisions,
        gb_memory_sources,
        gb_documents,
        gb_document_revisions,
        gb_document_chunks,
    ]
)
