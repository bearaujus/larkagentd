"""The vocabulary of Group Brain: states, reasons, settings and the global configuration."""

from dataclasses import dataclass, field

# Per-group lifecycle. `disabled` is also what a missing settings row means.
STATES = ("disabled", "enabled", "paused", "purging")
LEARNING_MODES = ("explicit", "interaction", "passive")
CAPABILITIES = ("recall", "memory", "documents")

# Why a group is not serving. A list of these is returned next to the effective state.
REASONS = (
    "global_disabled",
    "group_disabled",
    "not_registered",
    "paused",
    "purging",
    "missing_group_notice",
    "missing_scope",
    "visibility_unverified",
    "embedding_unavailable",
    "index_unavailable",
    "revalidation_pending",
)

# Tool result statuses (SPEC.md). `disabled` and `not_authorized` never say which sources exist.
STATUSES = (
    "ok",
    "partial",
    "disabled",
    "not_authorized",
    "source_unavailable",
    "dependency_unavailable",
    "invalid_request",
    "rate_limited",
)

JOB_KINDS = (
    "backfill_group",
    "ingest_message",
    "refresh_discussion",
    "extract_memory",
    "index_revision",
    "verify_source",
    "refresh_document",
    "invalidate_source",
    "reconcile_index",
    "purge_group",
)
# Jobs that may run while the feature is off: they only remove things and never call a model.
CLEANUP_JOBS = frozenset({"purge_group", "invalidate_source", "reconcile_index"})
JOB_STATES = ("queued", "running", "retry_wait", "succeeded", "failed", "cancelled")

# What changes of a group's settings invalidate work that is already queued or running.
POLICY_FIELDS = (
    "enabled",
    "state",
    "recall_enabled",
    "memory_enabled",
    "documents_enabled",
    "learning_mode",
    "history_window_days",
)


@dataclass(frozen=True)
class GroupBrainConfig:
    """The global settings (GROUP_BRAIN_*). Per-group settings live in the database.

    `enabled` is the emergency master switch: false means no group can serve, collect or index.
    Secrets are never part of repr or logs."""

    enabled: bool = False
    provider: str = "mem0_oss"
    embedding_base_url: str = ""
    embedding_api_key: str = field(default="", repr=False)
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024
    llm_base_url: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = "qwen3.8-flash"
    collection: str = "group_brain_v1"
    worker_concurrency: int = 2
    provider_timeout_seconds: int = 15
    job_max_attempts: int = 4
    max_history_days: int = 30
    max_backfill_messages: int = 5000
    max_docs_per_group: int = 100
    max_document_tokens: int = 100_000
    chunk_tokens: int = 512
    chunk_overlap: int = 64
    max_candidates: int = 100
    top_k: int = 5
    raw_buffer_hours: int = 24
    source_retention_days: int = 30
    memory_review_days: int = 90

    @property
    def embedding_configured(self):
        """True when an approved embedding endpoint is fully set. Anything less means the semantic
        index is unavailable and recall is lexical and live only."""
        return bool(
            self.embedding_base_url and self.embedding_api_key and self.embedding_model
        )

    @property
    def llm_configured(self):
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)
