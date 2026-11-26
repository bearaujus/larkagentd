"""Initial LarkAgentD schema for a fresh PostgreSQL database."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("expires", sa.Float(), nullable=False),
        sa.Column("created", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "artifacts_session", "artifacts", ["session_id", "created"], unique=False
    )
    op.create_table(
        "dashboard_limits",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("until", sa.Float(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "dashboard_logins",
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("expires", sa.Float(), nullable=False),
        sa.Column("group_scope", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index(
        op.f("ix_dashboard_logins_email"), "dashboard_logins", ["email"], unique=False
    )
    op.create_index(
        op.f("ix_dashboard_logins_owner"), "dashboard_logins", ["owner"], unique=False
    )
    op.create_table(
        "dashboard_managers",
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("added_by", sa.String(), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("email"),
    )
    op.create_table(
        "dashboard_sessions",
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("expires", sa.Float(), nullable=False),
        sa.Column("group_scope", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index(
        op.f("ix_dashboard_sessions_email"),
        "dashboard_sessions",
        ["email"],
        unique=False,
    )
    op.create_table(
        "directory_metadata",
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("checked_at", sa.Float(), nullable=False),
        sa.Column("expires", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("kind", "key"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "gateway_identities",
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("verified_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("owner"),
    )
    op.create_table(
        "gateway_policies",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.CheckConstraint(
            "mode IN ('all_users', 'specific_users')",
            name="gateway_policies_mode_check",
        ),
        sa.CheckConstraint(
            "scope ~ '^group:[A-Za-z0-9_-]+$' AND length(scope) <= 262",
            name="gateway_policies_group_scope",
        ),
        sa.PrimaryKeyConstraint("scope"),
    )
    op.create_table(
        "gb_audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=True),
        sa.Column("policy_version", sa.BigInteger(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("correlation_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "gb_audit_group_time",
        "gb_audit_events",
        ["group_key", "created_at"],
        unique=False,
    )
    op.create_table(
        "governance_flex",
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("active_limit", sa.BigInteger(), nullable=True),
        sa.Column("pending_limit", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("email"),
    )
    op.create_table(
        "governance_history",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("actor_email", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "governance_rates",
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("model", "effective_at"),
    )
    op.create_table(
        "governance_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("id = 1"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "group_actions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("chat", sa.String(), nullable=False),
        sa.Column("thread", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("sealed", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("expires", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_group_actions_expires"), "group_actions", ["expires"], unique=False
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("claim", sa.String(), nullable=True),
        sa.Column("until", sa.Float(), nullable=False),
        sa.Column("available", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("jobs_available", "jobs", ["available", "until"], unique=False)
    op.create_table(
        "leases",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("until", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("created", sa.Float(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "messages_owner_created", "messages", ["owner", "created"], unique=False
    )
    op.create_table(
        "query_activity",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("requester", sa.String(), nullable=False),
        sa.Column("principal", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False, server_default="query"),
        sa.Column("tool", sa.String(), nullable=False),
        sa.Column("project", sa.String(), server_default="", nullable=False),
        sa.Column("status", sa.String(), server_default="incomplete", nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("estimated_input_gb", sa.Float(), nullable=True),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("chat_type", sa.String(), server_default="unknown", nullable=False),
        sa.Column("chat_id", sa.String(), nullable=True),
        sa.Column("thread", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "query_activity_created", "query_activity", ["created_at", "id"], unique=False
    )
    op.create_index(
        "query_activity_requester_created",
        "query_activity",
        ["requester", "created_at"],
        unique=False,
    )
    op.create_table(
        "token_usage",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("requester", sa.String(), nullable=False),
        sa.Column("principal", sa.String(), nullable=False),
        sa.Column("chat_type", sa.String(), nullable=False),
        sa.Column("chat_id", sa.String(), server_default="", nullable=False),
        sa.Column("thread", sa.String(), server_default="", nullable=False),
        sa.Column("model", sa.String(), server_default="", nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "cache_read_tokens", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "cache_write_tokens", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "reasoning_tokens", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("source", sa.String(), server_default="session", nullable=False),
        sa.Column(
            "model_override",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "month",
            sa.Date(),
            server_default=sa.text(
                "(date_trunc('month', timezone('utc', now())))::date"
            ),
            nullable=False,
        ),
        sa.Column("quota_tier", sa.String(), nullable=True),
        sa.Column("quota_tracker", sa.String(), nullable=True),
        sa.Column("quota_cycle_start", sa.Date(), nullable=True),
        sa.Column(
            "estimated_cost_usd", sa.Numeric(precision=38, scale=14), nullable=True
        ),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        "token_usage_created_session",
        "token_usage",
        ["created_at", "session_id"],
        unique=False,
    )
    op.create_index(
        "token_usage_quota_tracker",
        "token_usage",
        ["quota_tier", "quota_cycle_start", "quota_tracker"],
        unique=False,
    )
    op.create_index(
        "token_usage_requester_month",
        "token_usage",
        ["requester", "month"],
        unique=False,
    )
    op.create_table(
        "tool_effects",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("sealed", sa.String(), nullable=True),
        sa.Column("updated", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_tool_effects_updated"), "tool_effects", ["updated"], unique=False
    )
    op.create_table(
        "tool_grants",
        sa.Column("tool", sa.String(), nullable=False),
        sa.Column("principal", sa.String(), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("granted_by", sa.String(), server_default="", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tool", "principal"),
    )
    op.create_table(
        "tool_registry",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("authority", sa.String(), nullable=False, server_default=""),
        sa.Column("activity_kind", sa.String(), nullable=False, server_default=""),
        sa.Column("risk", sa.String(), nullable=False),
        sa.Column("description", sa.String(), server_default="", nullable=False),
        sa.Column(
            "implemented", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "retired", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "turns",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("credential_version", sa.String(), nullable=True),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("expires", sa.Float(), nullable=False),
        sa.Column("created", sa.Float(), nullable=False),
        sa.Column(
            "needs_login", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("chat_id", sa.String(), nullable=True),
        sa.Column("message_id", sa.String(), nullable=True),
        sa.Column("chat_type", sa.String(), server_default="unknown", nullable=False),
        sa.Column("thread", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index("turns_job", "turns", ["job_id", "created"], unique=False)
    op.create_table(
        "users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "workspace_settings",
        sa.Column(
            "singleton", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "bot_paused", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.CheckConstraint("singleton", name="workspace_settings_singleton"),
        sa.PrimaryKeyConstraint("singleton"),
    )
    op.create_table(
        "gateway_members",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("added_by", sa.String(), server_default="", nullable=False),
        sa.ForeignKeyConstraint(
            ["scope"], ["gateway_policies.scope"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("scope", "email"),
    )
    op.create_table(
        "gateway_scope_tools",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("tool", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope"], ["gateway_policies.scope"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("scope", "tool"),
    )
    op.create_table(
        "gb_discussions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("native_thread_id", sa.String(), nullable=True),
        sa.Column("root_message_id", sa.String(), nullable=True),
        sa.Column("stable_window_key", sa.String(), nullable=True),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), server_default="active", nullable=False),
        sa.Column("source_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("coverage", sa.String(), server_default="partial", nullable=False),
        sa.Column(
            "identifiers", sa.ARRAY(sa.String()), server_default="{}", nullable=False
        ),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(summary, ''))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(kind = 'native_thread' AND native_thread_id IS NOT NULL) OR (kind = 'chat_window' AND native_thread_id IS NULL AND stable_window_key IS NOT NULL)",
            name="gb_discussions_identity",
        ),
        sa.CheckConstraint(
            "coverage IN ('complete', 'partial')", name="gb_discussions_coverage"
        ),
        sa.CheckConstraint(
            "kind IN ('native_thread', 'chat_window')", name="gb_discussions_kind"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'superseded', 'revoked', 'failed', 'expired', 'purging')",
            name="gb_discussions_status",
        ),
        sa.ForeignKeyConstraint(
            ["group_key"], ["gateway_policies.scope"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_key", "id", name="gb_discussions_group_id"),
    )
    op.create_index(
        "gb_discussions_search",
        "gb_discussions",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "gb_discussions_thread",
        "gb_discussions",
        ["group_key", "native_thread_id"],
        unique=True,
        postgresql_where=sa.text("native_thread_id IS NOT NULL"),
    )
    op.create_index(
        "gb_discussions_time",
        "gb_discussions",
        ["group_key", "status", "started_at", "ended_at"],
        unique=False,
    )
    op.create_index(
        "gb_discussions_window",
        "gb_discussions",
        ["group_key", "stable_window_key"],
        unique=True,
        postgresql_where=sa.text("stable_window_key IS NOT NULL"),
    )
    op.create_table(
        "gb_documents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("provider_kind", sa.String(), nullable=False),
        sa.Column("canonical_doc_token", sa.String(), nullable=False),
        sa.Column("wiki_token", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("sharing_policy_ref", sa.String(), nullable=True),
        sa.Column("current_revision", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(), server_default="pending", nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "provider_kind IN ('lark_docx', 'lark_wiki')", name="gb_documents_kind"
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'approved_pending_index', 'active', 'suspended', 'superseded', 'revoked', 'failed', 'purging')",
            name="gb_documents_state",
        ),
        sa.ForeignKeyConstraint(
            ["group_key"], ["gateway_policies.scope"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_key", "canonical_doc_token", name="gb_documents_token"
        ),
        sa.UniqueConstraint("group_key", "id", name="gb_documents_group_id"),
    )
    op.create_table(
        "gb_index_entries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("canonical_id", sa.String(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("chunk_ordinal", sa.Integer(), server_default="0", nullable=False),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("provider_id", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(), server_default="pending", nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('discussion', 'memory', 'document')", name="gb_index_kind"
        ),
        sa.CheckConstraint(
            "state <> 'active' OR provider_id IS NOT NULL",
            name="gb_index_active_has_provider",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'indexing', 'active', 'stale', 'delete_pending', 'deleted', 'failed')",
            name="gb_index_state",
        ),
        sa.ForeignKeyConstraint(
            ["group_key"], ["gateway_policies.scope"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_key", "idempotency_key", name="gb_index_idempotency"
        ),
    )
    op.create_index(
        "gb_index_lookup",
        "gb_index_entries",
        ["group_key", "kind", "canonical_id", "state"],
        unique=False,
    )
    op.create_index(
        "gb_index_provider",
        "gb_index_entries",
        ["provider_id"],
        unique=True,
        postgresql_where=sa.text("provider_id IS NOT NULL"),
    )
    op.create_table(
        "gb_jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.Float(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_until", sa.Float(), server_default="0", nullable=False),
        sa.Column("fence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("ref_payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled')",
            name="gb_jobs_state",
        ),
        sa.ForeignKeyConstraint(
            ["group_key"], ["gateway_policies.scope"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_key", "idempotency_key", name="gb_jobs_idempotency"),
    )
    op.create_index("gb_jobs_claim", "gb_jobs", ["state", "available_at"], unique=False)
    op.create_table(
        "gb_memory",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(), server_default="pending", nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("supersedes_id", sa.String(), nullable=True),
        sa.CheckConstraint(
            "category IN ('decision', 'business_logic', 'procedure', 'technical_discovery', 'lesson', 'standard', 'project_context')",
            name="gb_memory_category",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'approved_pending_index', 'active', 'suspended', 'superseded', 'revoked', 'failed', 'purging')",
            name="gb_memory_state",
        ),
        sa.ForeignKeyConstraint(
            ["group_key"], ["gateway_policies.scope"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_key", "id", name="gb_memory_group_id"),
    )
    op.create_index(
        "gb_memory_state_idx", "gb_memory", ["group_key", "state"], unique=False
    )
    op.create_table(
        "gb_messages",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("lark_message_id", sa.String(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=True),
        sa.Column("root_id", sa.String(), nullable=True),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("sender_type", sa.String(), nullable=False),
        sa.Column("sender_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_link", sa.String(), nullable=True),
        sa.Column("sanitized_text", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column(
            "visibility_status", sa.String(), server_default="visible", nullable=False
        ),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), server_default="active", nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'superseded', 'revoked', 'failed', 'expired', 'purging')",
            name="gb_messages_status",
        ),
        sa.CheckConstraint(
            "visibility_status IN ('visible', 'recalled', 'unverified', 'blocked')",
            name="gb_messages_visibility",
        ),
        sa.ForeignKeyConstraint(
            ["group_key"], ["gateway_policies.scope"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_key", "id", name="gb_messages_group_id"),
        sa.UniqueConstraint("group_key", "lark_message_id", name="gb_messages_lark"),
    )
    op.create_index(
        "gb_messages_sender",
        "gb_messages",
        ["group_key", "sender_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "gb_messages_thread", "gb_messages", ["group_key", "thread_id"], unique=False
    )
    op.create_index(
        "gb_messages_time", "gb_messages", ["group_key", "created_at"], unique=False
    )
    op.create_table(
        "gb_source_refs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("source_kind", sa.String(), nullable=False),
        sa.Column("canonical_source_id", sa.String(), nullable=False),
        sa.Column("source_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("lark_message_id", sa.String(), nullable=True),
        sa.Column("document_id", sa.String(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "visibility_status", sa.String(), server_default="visible", nullable=False
        ),
        sa.Column("link_type", sa.String(), server_default="none", nullable=False),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.CheckConstraint(
            "(source_kind = 'message' AND lark_message_id IS NOT NULL) OR (source_kind = 'document' AND document_id IS NOT NULL)",
            name="gb_source_refs_target",
        ),
        sa.CheckConstraint(
            "link_type IN ('message', 'group_fallback', 'none')",
            name="gb_source_refs_link",
        ),
        sa.CheckConstraint(
            "source_kind IN ('message', 'document')", name="gb_source_refs_kind"
        ),
        sa.ForeignKeyConstraint(
            ["group_key"], ["gateway_policies.scope"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_key",
            "source_kind",
            "canonical_source_id",
            "source_revision",
            name="gb_source_refs_unique",
        ),
    )
    op.create_table(
        "gb_sync_state",
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("source_kind", sa.String(), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), server_default="idle", nullable=False),
        sa.Column("coverage_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["group_key"], ["gateway_policies.scope"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("group_key", "source_kind"),
    )
    op.create_table(
        "group_knowledge_settings",
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("state", sa.String(), server_default="disabled", nullable=False),
        sa.Column(
            "recall_enabled", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column(
            "memory_enabled", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column(
            "documents_enabled", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column(
            "review_required", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column(
            "history_window_days", sa.Integer(), server_default="30", nullable=False
        ),
        sa.Column("generation", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled_by", sa.String(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("notice_ref", sa.String(), nullable=True),
        sa.Column("policy_ref", sa.String(), nullable=True),
        sa.CheckConstraint(
            "state IN ('disabled', 'enabled', 'paused', 'purging')",
            name="gb_settings_state",
        ),
        sa.CheckConstraint(
            "history_window_days BETWEEN 1 AND 365", name="gb_settings_window"
        ),
        sa.CheckConstraint("review_required", name="gb_settings_review_locked"),
        sa.ForeignKeyConstraint(
            ["group_key"], ["gateway_policies.scope"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("group_key"),
    )
    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.String(), nullable=False, unique=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column(
            "group_scope",
            sa.String(),
            sa.Computed("NULLIF(scope, 'workspace')", persisted=True),
            nullable=True,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("server", sa.String(), nullable=True),
        sa.Column("uri", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["group_scope"], ["gateway_policies.scope"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("scope", "name"),
    )
    op.create_table(
        "gb_discussion_messages",
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("discussion_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_key", "discussion_id"],
            ["gb_discussions.group_key", "gb_discussions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_key", "message_id"],
            ["gb_messages.group_key", "gb_messages.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "discussion_id", "message_id", name="gb_discussion_messages_pk"
        ),
        sa.UniqueConstraint(
            "discussion_id", "ordinal", name="gb_discussion_messages_ordinal"
        ),
    )
    op.create_table(
        "gb_discussion_participants",
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("discussion_id", sa.String(), nullable=False),
        sa.Column("sender_id", sa.String(), nullable=False),
        sa.Column("first_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authored_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_key", "discussion_id"],
            ["gb_discussions.group_key", "gb_discussions.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "discussion_id", "sender_id", name="gb_discussion_participants_pk"
        ),
    )
    op.create_index(
        "gb_participants_sender",
        "gb_discussion_participants",
        ["group_key", "sender_id", "discussion_id"],
        unique=False,
    )
    op.create_table(
        "gb_document_revisions",
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("provider_revision", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("state", sa.String(), server_default="pending", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'approved_pending_index', 'active', 'suspended', 'superseded', 'revoked', 'failed', 'purging')",
            name="gb_document_revisions_state",
        ),
        sa.ForeignKeyConstraint(
            ["group_key", "document_id"],
            ["gb_documents.group_key", "gb_documents.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("document_id", "revision", name="gb_document_revisions_pk"),
        sa.UniqueConstraint(
            "group_key", "document_id", "revision", name="gb_document_revisions_group"
        ),
    )
    op.create_table(
        "gb_memory_revisions",
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("memory_id", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("sanitized_content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("proposed_by", sa.String(), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column(
            "evidence_status", sa.String(), server_default="explicit", nullable=False
        ),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column(
            "identifiers", sa.ARRAY(sa.String()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', coalesce(sanitized_content, ''))", persisted=True
            ),
            nullable=True,
        ),
        sa.CheckConstraint(
            "evidence_status IN ('explicit', 'supported', 'uncertain', 'conflicting')",
            name="gb_memory_revisions_evidence",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'active', 'rejected', 'superseded', 'revoked', 'suspended')",
            name="gb_memory_revisions_status",
        ),
        sa.ForeignKeyConstraint(
            ["group_key", "memory_id"],
            ["gb_memory.group_key", "gb_memory.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "group_key", "memory_id", "revision", name="gb_memory_revisions_group"
        ),
        sa.UniqueConstraint("memory_id", "revision", name="gb_memory_revisions_pk"),
    )
    op.create_index(
        "gb_memory_revisions_search",
        "gb_memory_revisions",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_table(
        "gb_document_chunks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("section_path", sa.Text(), server_default="", nullable=False),
        sa.Column("block_anchor", sa.String(), nullable=True),
        sa.Column("sanitized_content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', coalesce(sanitized_content, ''))", persisted=True
            ),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["group_key", "document_id", "revision"],
            [
                "gb_document_revisions.group_key",
                "gb_document_revisions.document_id",
                "gb_document_revisions.revision",
            ],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "revision", "ordinal", name="gb_document_chunks_pk"
        ),
    )
    op.create_index(
        "gb_document_chunks_search",
        "gb_document_chunks",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_table(
        "gb_memory_sources",
        sa.Column("group_key", sa.String(), nullable=False),
        sa.Column("memory_id", sa.String(), nullable=False),
        sa.Column("memory_revision", sa.Integer(), nullable=False),
        sa.Column("source_ref_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_key", "memory_id", "memory_revision"],
            [
                "gb_memory_revisions.group_key",
                "gb_memory_revisions.memory_id",
                "gb_memory_revisions.revision",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_ref_id"], ["gb_source_refs.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "memory_id", "memory_revision", "source_ref_id", name="gb_memory_sources_pk"
        ),
    )
    op.create_index(
        "gb_memory_sources_ref", "gb_memory_sources", ["source_ref_id"], unique=False
    )


def downgrade():
    op.drop_index("gb_memory_sources_ref", table_name="gb_memory_sources")
    op.drop_table("gb_memory_sources")
    op.drop_index(
        "gb_document_chunks_search",
        table_name="gb_document_chunks",
        postgresql_using="gin",
    )
    op.drop_table("gb_document_chunks")
    op.drop_index(
        "gb_memory_revisions_search",
        table_name="gb_memory_revisions",
        postgresql_using="gin",
    )
    op.drop_table("gb_memory_revisions")
    op.drop_table("gb_document_revisions")
    op.drop_index("gb_participants_sender", table_name="gb_discussion_participants")
    op.drop_table("gb_discussion_participants")
    op.drop_table("gb_discussion_messages")
    op.drop_table("knowledge_sources")
    op.drop_table("group_knowledge_settings")
    op.drop_table("gb_sync_state")
    op.drop_table("gb_source_refs")
    op.drop_index("gb_messages_time", table_name="gb_messages")
    op.drop_index("gb_messages_thread", table_name="gb_messages")
    op.drop_index("gb_messages_sender", table_name="gb_messages")
    op.drop_table("gb_messages")
    op.drop_index("gb_memory_state_idx", table_name="gb_memory")
    op.drop_table("gb_memory")
    op.drop_index("gb_jobs_claim", table_name="gb_jobs")
    op.drop_table("gb_jobs")
    op.drop_index(
        "gb_index_provider",
        table_name="gb_index_entries",
        postgresql_where=sa.text("provider_id IS NOT NULL"),
    )
    op.drop_index("gb_index_lookup", table_name="gb_index_entries")
    op.drop_table("gb_index_entries")
    op.drop_table("gb_documents")
    op.drop_index(
        "gb_discussions_window",
        table_name="gb_discussions",
        postgresql_where=sa.text("stable_window_key IS NOT NULL"),
    )
    op.drop_index("gb_discussions_time", table_name="gb_discussions")
    op.drop_index(
        "gb_discussions_thread",
        table_name="gb_discussions",
        postgresql_where=sa.text("native_thread_id IS NOT NULL"),
    )
    op.drop_index(
        "gb_discussions_search", table_name="gb_discussions", postgresql_using="gin"
    )
    op.drop_table("gb_discussions")
    op.drop_table("gateway_scope_tools")
    op.drop_table("gateway_members")
    op.drop_table("workspace_settings")
    op.drop_table("users")
    op.drop_index("turns_job", table_name="turns")
    op.drop_table("turns")
    op.drop_table("tool_registry")
    op.drop_table("tool_grants")
    op.drop_index(op.f("ix_tool_effects_updated"), table_name="tool_effects")
    op.drop_table("tool_effects")
    op.drop_index("token_usage_requester_month", table_name="token_usage")
    op.drop_index("token_usage_quota_tracker", table_name="token_usage")
    op.drop_index("token_usage_created_session", table_name="token_usage")
    op.drop_table("token_usage")
    op.drop_index("query_activity_requester_created", table_name="query_activity")
    op.drop_index("query_activity_created", table_name="query_activity")
    op.drop_table("query_activity")
    op.drop_index("messages_owner_created", table_name="messages")
    op.drop_table("messages")
    op.drop_table("leases")
    op.drop_index("jobs_available", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index(op.f("ix_group_actions_expires"), table_name="group_actions")
    op.drop_table("group_actions")
    op.drop_table("governance_settings")
    op.drop_table("governance_rates")
    op.drop_table("governance_history")
    op.drop_table("governance_flex")
    op.drop_index("gb_audit_group_time", table_name="gb_audit_events")
    op.drop_table("gb_audit_events")
    op.drop_table("gateway_policies")
    op.drop_table("gateway_identities")
    op.drop_table("events")
    op.drop_table("directory_metadata")
    op.drop_index(op.f("ix_dashboard_sessions_email"), table_name="dashboard_sessions")
    op.drop_table("dashboard_sessions")
    op.drop_table("dashboard_managers")
    op.drop_index(op.f("ix_dashboard_logins_owner"), table_name="dashboard_logins")
    op.drop_index(op.f("ix_dashboard_logins_email"), table_name="dashboard_logins")
    op.drop_table("dashboard_logins")
    op.drop_table("dashboard_limits")
    op.drop_index("artifacts_session", table_name="artifacts")
    op.drop_table("artifacts")
