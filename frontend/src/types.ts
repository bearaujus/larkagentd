export type Role = "admin" | "manager";
export type Session = {
  email: string;
  role: Role | "group_admin";
  group_id?: string | null;
  expires_at: number;
};
export type Person = { email: string; role: Role; managed_by_config: boolean };
export type Policy = {
  mode: "all_users" | "specific_users";
  members: string[];
  inherited_members: Person[];
  group_id?: string;
};
export type Tool = {
  label?: string;
  authority?: string;
  activity_kind?: string;
  name: string;
  kind: string;
  risk: string;
  description: string;
  default: boolean;
  enabled_globally: boolean;
};
export type GroupPolicy = Policy & {
  group_id: string;
  tools: string[];
};
export type GroupKnowledgeState =
  "disabled" | "enabled" | "degraded" | "paused" | "purging" | "setup_blocked";
export type GroupKnowledge = {
  configured_enabled: boolean;
  effective_state: GroupKnowledgeState;
  reason_codes: string[];
  capabilities: { recall: boolean; memory: boolean; documents: boolean };
  effective_capabilities: {
    recall: boolean;
    memory: boolean;
    documents: boolean;
  };
  history_window_days: number;
  max_history_days: number;
  review_required: true;
  version: number;
  generation: number;
  notice_ref: string | null;
  policy_ref: string | null;
  counts: {
    discussions: number;
    memories: number;
    documents: number;
    pending_review: number;
  };
  coverage: { from: string | null; to: string | null; complete: boolean };
  health: {
    semantic_index: "ready" | "unavailable" | "not_configured";
    lark_history: "ready" | "unknown";
  };
  jobs: {
    queued: number;
    running: number;
    retry_wait: number;
    failed: number;
    succeeded: number;
    cancelled: number;
  };
  oldest_job_age_seconds: number;
  updated_at: string | null;
  updated_by: string | null;
};
export type GroupKnowledgeCandidate = {
  memory_id: string;
  revision: number;
  category: string;
  state: string;
  content_hash: string;
  proposed_by: string;
  created_at: string;
  content?: string;
  reason?: string;
};
export type GroupKnowledgeDocument = {
  id: string;
  provider_kind: string;
  source_url: string;
  title: string;
  state: string;
  current_revision: number;
  last_checked_at: string | null;
  last_synced_at: string | null;
};
export type GroupKnowledgeJob = {
  id: string;
  kind: string;
  state: string;
  attempts: number;
  error_code: string | null;
  generation: number;
  created_at: string;
};
export type GroupKnowledgeAuditEvent = {
  id: string;
  actor_id: string;
  action: string;
  target_id: string | null;
  outcome: string;
  reason_code: string | null;
  policy_version: number;
  created_at: string;
};
export type Metadata = {
  lookup_type: "email" | "owner" | "group";
  key: string;
  email?: string | null;
  directory_email?: string | null;
  owner?: string;
  group_id?: string;
  name: string | null;
  avatar_url: string | null;
  metadata_status:
    | "available"
    | "pending"
    | "stale"
    | "not_found"
    | "unavailable"
    | "not_applicable";
  refreshing: boolean;
  checked_at: number | null;
};
export type DirectoryResult = {
  items: Metadata[];
  retry_after_seconds: number | null;
};
export type Target = { kind: Metadata["lookup_type"]; key: string };
export type Period = { from: string | null; to: string; all_time: boolean };
export type Metrics = {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  turns: number;
  llm_sessions: number;
  estimated_cost_usd?: string | null;
  priced_sessions?: number;
  unpriced_sessions?: number;
  unavailable_sessions: number;
  activity_calls: number;
  query_calls: number;
  estimate_calls: number;
  dry_run_calls: number;
  terminal_calls: number;
  succeeded_calls: number;
  failed_calls: number;
  denied_calls: number;
  refused_calls: number;
  incomplete_calls: number;
  available_query_estimated_input_gb: number | null;
  query_estimate_missing_calls: number;
};
export type UsageRow = Partial<Metrics> & {
  requester?: string;
  email?: string | null;
  principal?: string;
  model?: string | null;
  models?: UsageRow[];
  date?: string;
  occurred_at?: string;
  id?: string;
  job_id?: string;
  session_id?: string;
  chat_type?: string;
  source?: string;
  quota_tier?: "free" | "flex" | null;
  tool?: string;
  project?: string;
  status?: string;
  duration_ms?: number | null;
  estimated_input_gb?: number | null;
  row_count?: number | null;
  error_code?: string | null;
  finished_at?: string | null;
};
export type PageResult = {
  period: Period;
  items: UsageRow[];
  total: number;
  limit: number;
  offset: number;
};
export type Summary = {
  period: Period;
  statistics: Metrics;
  monetary_costs_available: boolean;
};
export type Dataset = "users" | "models" | "daily" | "llm" | "queries";
export type Filters = Record<string, string>;
