import { test as base, expect, type BrowserContext } from "@playwright/test";
import type {
  GroupKnowledge,
  GroupKnowledgeAuditEvent,
  GroupKnowledgeJob,
  GroupPolicy,
  GroupKnowledgeCandidate,
  GroupKnowledgeDocument,
  Metadata,
  Metrics,
  Person,
  Policy,
  Tool,
  UsageRow,
} from "../../src/types";

export const metrics: Metrics = {
  input_tokens: 100,
  output_tokens: 40,
  cache_read_tokens: 30,
  cache_write_tokens: 10,
  reasoning_tokens: 7,
  total_tokens: 400,
  turns: 2,
  llm_sessions: 2,
  active_people: 2,
  active_groups: 1,
  unavailable_sessions: 0,
  activity_calls: 3,
  query_calls: 2,
  estimate_calls: 1,
  dry_run_calls: 0,
  terminal_calls: 0,
  succeeded_calls: 2,
  failed_calls: 0,
  denied_calls: 1,
  refused_calls: 0,
  incomplete_calls: 1,
  available_query_estimated_input_gb: 1.5,
  query_estimate_missing_calls: 1,
};
export const roles: Person[] = [
  { email: "admin@example.com", role: "admin", managed_by_config: true },
  { email: "manager@example.com", role: "manager", managed_by_config: false },
];
export const tools: Tool[] = [
  {
    name: "connection_status",
    kind: "builtin",
    risk: "read",
    description: "Whether the person is connected and how.",
    default: true,
    enabled_globally: true,
  },
  {
    name: "run_query",
    kind: "mc",
    risk: "read",
    description: "Run one read-only query.",
    default: false,
    enabled_globally: true,
  },
  {
    name: "workflow_info",
    kind: "workflow",
    risk: "write",
    description: "Inspect an workflow.",
    default: true,
    enabled_globally: false,
  },
  {
    name: "inspect_workspace",
    kind: "builtin",
    risk: "read",
    description: "Inspect synthetic workspace metadata.",
    default: false,
    enabled_globally: true,
  },
  {
    name: "read_table_sample",
    kind: "mc",
    risk: "read",
    description: "Read a bounded synthetic table sample.",
    default: false,
    enabled_globally: true,
  },
  {
    name: "estimate_query",
    kind: "mc",
    risk: "read",
    description: "Estimate a synthetic query.",
    default: false,
    enabled_globally: true,
  },
  {
    name: "create_job_draft",
    kind: "workflow",
    risk: "write",
    description: "Create a synthetic job draft.",
    default: false,
    enabled_globally: true,
  },
  {
    name: "list_job_runs",
    kind: "workflow",
    risk: "read",
    description: "List synthetic job runs.",
    default: false,
    enabled_globally: true,
  },
  {
    name: "find_repository_file",
    kind: "gitlab",
    risk: "read",
    description: "Find a file in a synthetic repository.",
    default: false,
    enabled_globally: true,
  },
  {
    name: "compare_change",
    kind: "gitlab",
    risk: "read",
    description: "Compare synthetic changes.",
    default: false,
    enabled_globally: true,
  },
  {
    name: "read_document",
    kind: "lark",
    risk: "read",
    description: "Read an explicitly provided synthetic document.",
    default: false,
    enabled_globally: true,
  },
  {
    name: "inspect_sheet",
    kind: "lark",
    risk: "read",
    description: "Inspect a synthetic sheet.",
    default: false,
    enabled_globally: true,
  },
  {
    name: "estimate_cost",
    kind: "mc",
    risk: "read",
    description: "Estimate synthetic query cost.",
    default: false,
    enabled_globally: true,
  },
  {
    name: "review_patch",
    kind: "gitlab",
    risk: "read",
    description: "Review a synthetic patch.",
    default: false,
    enabled_globally: true,
  },
  {
    name: "inspect_job_config",
    kind: "workflow",
    risk: "read",
    description: "Inspect a synthetic job configuration.",
    default: false,
    enabled_globally: true,
  },
  {
    name: "open_document_metadata",
    kind: "lark",
    risk: "read",
    description: "View synthetic document metadata.",
    default: false,
    enabled_globally: true,
  },
];
const initialGroup = (group_id: string): GroupPolicy => ({
  ...initialPolicy(),
  group_id,
  tools: ["connection_status"],
});
const initialPolicy = (): Policy => ({
  mode: "specific_users",
  members: ["alex@example.com"],
  inherited_members: [...roles],
});
const initialKnowledge = (): GroupKnowledge => ({
  configured_enabled: false,
  effective_state: "disabled",
  reason_codes: ["group_disabled"],
  capabilities: { recall: true, memory: true, documents: true },
  effective_capabilities: { recall: false, memory: false, documents: false },
  history_window_days: 30,
  max_history_days: 90,
  review_required: true,
  version: 0,
  generation: 0,
  notice_ref: null,
  policy_ref: null,
  counts: { discussions: 0, memories: 0, documents: 0, pending_review: 0 },
  coverage: { from: null, to: null, complete: false },
  health: { semantic_index: "not_configured", lark_history: "unknown" },
  jobs: {
    queued: 0,
    running: 0,
    retry_wait: 0,
    failed: 0,
    succeeded: 0,
    cancelled: 0,
  },
  oldest_job_age_seconds: 0,
  updated_at: null,
  updated_by: null,
});
export type State = {
  sessionSeconds: number;
  role: "admin" | "manager" | "member";
  loggedIn: boolean;
  expired: boolean;
  metadata: Metadata["metadata_status"];
  botPaused: boolean;
  groups: GroupPolicy[];
  requests: {
    path: string;
    method: string;
    params: URLSearchParams;
    body: Record<string, unknown>;
  }[];
  knowledge: Record<string, GroupKnowledge>;
  knowledgeJobs: Record<string, GroupKnowledgeJob[]>;
  knowledgeAudit: Record<string, GroupKnowledgeAuditEvent[]>;
  knowledgeCandidates: Record<string, GroupKnowledgeCandidate[]>;
  knowledgeDocuments: Record<string, GroupKnowledgeDocument[]>;
  conflictNextKnowledgeSave: boolean;
  failWrite: boolean;
  failUsage: boolean;
  pricing?: "none" | "zero";
  empty: boolean;
  brokenImage: boolean;
  pollCount: number;
};
async function mock(context: BrowserContext, state: State) {
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== "http://127.0.0.1:4173") return route.abort();
    if (!url.pathname.startsWith("/v1/dashboard/")) return route.continue();
    const path = url.pathname.slice("/v1/dashboard".length);
    const method = route.request().method();
    const body = route.request().postDataJSON() ?? {};
    // Do not retain even synthetic login tokens in request records.
    state.requests.push({
      path,
      method,
      params: url.searchParams,
      body: path === "/auth/exchange" ? {} : body,
    });
    const json = (data: unknown, status = 200) =>
      route.fulfill({ status, json: data });
    const session = {
      email: `${state.role}@example.com`,
      role: state.role,
      expires_at: Date.now() / 1000 + state.sessionSeconds,
    };
    if (path === "/auth/exchange") {
      if (state.role === "member" || state.expired)
        return json({ error: "invalid_login" }, 401);
      state.loggedIn = true;
      return json(session);
    }
    if (!state.loggedIn || state.role === "member")
      return json({ error: "login_required" }, 401);
    if (path === "/auth/session") return json(session);
    if (path === "/auth/logout") {
      state.loggedIn = false;
      return route.fulfill({ status: 204 });
    }
    if (
      state.failWrite &&
      ["PATCH", "PUT", "DELETE", "POST"].includes(method) &&
      !path.startsWith("/directory")
    )
      return json({ error: "temporarily_unavailable" }, 503);
    if (path === "/bot-access") {
      if (method === "PATCH") state.botPaused = body.paused === true;
      return json({ paused: state.botPaused });
    }
    if (path === "/roles") return json(roles);
    if (path === "/tools") return json(tools);
    if (path.startsWith("/managers/")) {
      if (state.role !== "admin") return json({ error: "admin_required" }, 403);
      return route.fulfill({ status: 204 });
    }
    if (path === "/groups" && method === "GET") return json(state.groups);
    if (path === "/groups" && method === "POST") {
      if (state.groups.some((p) => p.group_id === body.group_id))
        return json({ error: "group_exists" }, 409);
      const policy: GroupPolicy = {
        ...initialPolicy(),
        mode: "all_users",
        members: [],
        group_id: String(body.group_id),
        tools: tools
          .filter((tool) => tool.default)
          .map((tool) => tool.name)
          .sort(),
      };
      state.groups.push(policy);
      state.knowledge[policy.group_id] = {
        ...initialKnowledge(),
        configured_enabled: true,
        effective_state: "enabled",
        reason_codes: [],
        capabilities: { recall: true, memory: true, documents: false },
        effective_capabilities: {
          recall: true,
          memory: true,
          documents: false,
        },
        notice_ref: "The assistant can save useful facts from group turns.",
        policy_ref: "Group admission",
        version: 1,
        generation: 1,
      };
      return json(policy, 201);
    }
    if (path.startsWith("/groups/") && path.endsWith("/tools")) {
      if (state.role !== "admin") return json({ error: "admin_required" }, 403);
      const groupId = decodeURIComponent(path.split("/")[2]);
      const group = state.groups.find((item) => item.group_id === groupId);
      if (!group) return json({ error: "group_not_found" }, 404);
      if (
        !Array.isArray(body.tools) ||
        body.tools.some(
          (name: unknown) =>
            typeof name !== "string" ||
            !tools.some((tool) => tool.name === name),
        )
      )
        return json({ error: "invalid_tool" }, 400);
      group.tools = [...new Set(body.tools as string[])].sort();
      return json(group);
    }
    if (
      method === "GET" &&
      (path === "/knowledge/sources" ||
        /^\/groups\/[^/]+\/knowledge\/sources$/.test(path))
    ) {
      return json({ sources: [], mcp_servers: [] });
    }
    if (
      method === "GET" &&
      /^\/groups\/[^/]+\/knowledge\/learned$/.test(path)
    ) {
      return json({ records: [] });
    }
    const knowledgeMatch = path.match(
      /^\/groups\/([^/]+)\/knowledge(?:\/(.*))?$/,
    );
    if (knowledgeMatch) {
      const groupId = decodeURIComponent(knowledgeMatch[1]);
      if (!state.groups.some((group) => group.group_id === groupId))
        return json({ error: "not_registered" }, 404);
      state.knowledge[groupId] ??= initialKnowledge();
      state.knowledgeJobs[groupId] ??= [
        {
          id: "2",
          kind: "index",
          state: "failed",
          attempts: 2,
          error_code: "index_unavailable",
          generation: 0,
          created_at: "2026-11-25T10:00:00Z",
        },
      ];
      state.knowledgeAudit[groupId] ??= [
        {
          id: "2",
          actor_id: "admin@example.com",
          action: "settings_changed",
          target_id: groupId,
          outcome: "success",
          reason_code: null,
          policy_version: 1,
          created_at: "2026-11-25T09:00:00Z",
        },
        {
          id: "1",
          actor_id: "admin@example.com",
          action: "group_registered",
          target_id: groupId,
          outcome: "success",
          reason_code: null,
          policy_version: 0,
          created_at: "2026-11-24T09:00:00Z",
        },
      ];
      const knowledge = state.knowledge[groupId];
      const suffix = knowledgeMatch[2] ?? "";
      const writes = ["PATCH", "POST"].includes(method);
      if (writes && state.role !== "admin")
        return json({ error: "admin_required" }, 403);
      if (suffix === "" && method === "GET") return json(knowledge);
      if (suffix === "" && method === "PATCH") {
        if (state.conflictNextKnowledgeSave) {
          state.conflictNextKnowledgeSave = false;
          return json({ error: "version_conflict" }, 409);
        }
        if (body.version !== knowledge.version)
          return json({ error: "version_conflict" }, 409);
        if (body.enabled === true) {
          if (!body.notice_ref)
            return json({ error: "missing_group_notice" }, 409);
          if (!body.policy_ref || body.acknowledge_group_sharing !== true)
            return json({ error: "invalid_fields" }, 400);
          knowledge.notice_ref = String(body.notice_ref);
          knowledge.policy_ref = String(body.policy_ref);
        }
        if (typeof body.enabled === "boolean")
          knowledge.configured_enabled = body.enabled;
        for (const key of ["recall", "memory", "documents"] as const) {
          const field = `${key}_enabled`;
          if (typeof body[field] === "boolean")
            knowledge.capabilities[key] = Boolean(body[field]);
        }
        if (typeof body.history_window_days === "number")
          knowledge.history_window_days = body.history_window_days;
        knowledge.version++;
        knowledge.updated_at = "2026-11-26T12:00:00Z";
        knowledge.updated_by = "admin@example.com";
        knowledge.reason_codes = knowledge.configured_enabled
          ? []
          : ["group_disabled"];
        knowledge.effective_state = knowledge.configured_enabled
          ? "enabled"
          : "disabled";
        knowledge.effective_capabilities = knowledge.configured_enabled
          ? { ...knowledge.capabilities }
          : { recall: false, memory: false, documents: false };
        return json(knowledge);
      }
      state.knowledgeCandidates[groupId] ??= [];
      state.knowledgeDocuments[groupId] ??= [];
      if (suffix === "candidates" && method === "GET")
        return json({ candidates: state.knowledgeCandidates[groupId] });
      const candidateMatch = suffix.match(
        /^candidates\/([^/]+)\/(approve|reject)$/,
      );
      if (candidateMatch && method === "POST") {
        const memoryId = decodeURIComponent(candidateMatch[1]);
        const candidate = state.knowledgeCandidates[groupId].find(
          (item) => item.memory_id === memoryId,
        );
        if (!candidate) return json({ error: "not_found" }, 404);
        if (candidateMatch[2] === "approve") {
          if (
            body.revision !== candidate.revision ||
            body.content_hash !== candidate.content_hash
          )
            return json({ error: "hash_mismatch" }, 409);
          if (
            body.acknowledge_group_sharing !== true ||
            Object.keys(body).length !== 3
          )
            return json({ error: "invalid_fields" }, 400);
          state.knowledgeCandidates[groupId] = state.knowledgeCandidates[
            groupId
          ].filter((item) => item !== candidate);
          knowledge.counts.pending_review = Math.max(
            0,
            knowledge.counts.pending_review - 1,
          );
          return json({ status: "active" });
        }
        if (
          body.revision !== candidate.revision ||
          !["not_accurate", "not_for_group", "sensitive", "duplicate"].includes(
            String(body.reason_code),
          ) ||
          Object.keys(body).length !== 2
        )
          return json({ error: "invalid_fields" }, 400);
        state.knowledgeCandidates[groupId] = state.knowledgeCandidates[
          groupId
        ].filter((item) => item !== candidate);
        knowledge.counts.pending_review = Math.max(
          0,
          knowledge.counts.pending_review - 1,
        );
        return json({ status: "rejected" });
      }
      const revokeMemory = suffix.match(/^memory\/([^/]+)\/revoke$/);
      if (revokeMemory && method === "POST") {
        const memoryId = decodeURIComponent(revokeMemory[1]);
        const candidate = state.knowledgeCandidates[groupId].find(
          (item) => item.memory_id === memoryId,
        );
        if (candidate) candidate.state = "revoked";
        return json({ status: "revoked" });
      }
      if (suffix === "documents" && method === "GET")
        return json({ documents: state.knowledgeDocuments[groupId] });
      const documentMatch = suffix.match(
        /^documents\/([^/]+)\/(approve|reject|refresh)$/,
      );
      if (documentMatch && method === "POST") {
        const documentId = decodeURIComponent(documentMatch[1]);
        const document = state.knowledgeDocuments[groupId].find(
          (item) => item.id === documentId,
        );
        if (!document) return json({ error: "not_found" }, 404);
        if (documentMatch[2] === "approve") {
          if (
            Object.keys(body).length !== 2 ||
            !body.sharing_policy_ref ||
            body.acknowledge_group_sharing !== true
          )
            return json({ error: "invalid_fields" }, 400);
          document.state = "active";
          return json({ status: "active" });
        }
        if (Object.keys(body).length !== 0)
          return json({ error: "invalid_fields" }, 400);
        if (documentMatch[2] === "reject") {
          document.state = "rejected";
          return json({ status: "rejected" });
        }
        return json({ status: "queued" });
      }
      const revokeSource = suffix.match(/^sources\/([^/]+)\/revoke$/);
      if (revokeSource && method === "POST") {
        const document = state.knowledgeDocuments[groupId].find(
          (item) => item.id === decodeURIComponent(revokeSource[1]),
        );
        if (!document) return json({ error: "not_found" }, 404);
        if (Object.keys(body).length !== 0)
          return json({ error: "invalid_fields" }, 400);
        document.state = "suspended";
        return json({ status: "revoked" });
      }
      if (suffix === "jobs" && method === "GET")
        return json({ jobs: state.knowledgeJobs[groupId] });
      const retry = suffix.match(/^jobs\/([^/]+)\/retry$/);
      if (retry && method === "POST") {
        const job = state.knowledgeJobs[groupId].find(
          (item) => item.id === decodeURIComponent(retry[1]),
        );
        if (!job) return json({ error: "not_found" }, 404);
        if (job.state !== "failed")
          return json({ error: "invalid_transition" }, 409);
        job.state = "queued";
        return json({ ok: true });
      }
      if (suffix === "audit" && method === "GET") {
        const before = Number(url.searchParams.get("before"));
        const events = state.knowledgeAudit[groupId].filter(
          (event) =>
            !url.searchParams.has("before") || Number(event.id) < before,
        );
        return json({ events: events.slice(0, 1) });
      }
      if (suffix === "pause" || suffix === "resume" || suffix === "purge") {
        if (body.version !== knowledge.version)
          return json({ error: "version_conflict" }, 409);
        if (suffix === "purge" && body.confirm !== groupId)
          return json({ error: "invalid_confirmation" }, 400);
        knowledge.version++;
        knowledge.updated_at = "2026-11-26T12:00:00Z";
        knowledge.updated_by = "admin@example.com";
        if (suffix === "pause") {
          knowledge.effective_state = "paused";
          knowledge.reason_codes = ["paused"];
        } else if (suffix === "resume") {
          knowledge.effective_state = knowledge.configured_enabled
            ? "enabled"
            : "disabled";
          knowledge.reason_codes = knowledge.configured_enabled
            ? []
            : ["group_disabled"];
        } else {
          knowledge.configured_enabled = false;
          knowledge.effective_state = "purging";
          knowledge.reason_codes = ["purging"];
        }
        return json(knowledge);
      }
      return json({ error: "not_found" }, 404);
    }
    if (path.startsWith("/groups/")) {
      const parts = path.split("/");
      const policy = state.groups.find(
        (p) => p.group_id === decodeURIComponent(parts[2]),
      );
      if (!policy) return json({ error: "not_found" }, 404);
      if (path.includes("/members/")) {
        const email = decodeURIComponent(parts.at(-1)!);
        if (method === "PUT" && !policy.members.includes(email))
          policy.members.push(email);
        if (method === "DELETE")
          policy.members = policy.members.filter((e) => e !== email);
        return route.fulfill({ status: 204 });
      }
      if (method === "PATCH") policy.mode = body.mode;
      if (method === "DELETE") {
        state.groups = state.groups.filter((p) => p !== policy);
        return route.fulfill({ status: 204 });
      }
      return json(policy);
    }
    if (path.startsWith("/directory/")) {
      const targets = [
        ...(body.emails ?? []).map((key: string) => ({
          key,
          lookup_type: "email",
        })),
        ...(body.owners ?? []).map((key: string) => ({
          key,
          lookup_type: "owner",
        })),
        ...(body.group_ids ?? []).map((key: string) => ({
          key,
          lookup_type: "group",
        })),
      ];
      if (targets.length > 20) return json({ error: "too_many_targets" }, 400);
      state.pollCount++;
      const status =
        state.metadata === "pending" && state.pollCount > 2
          ? "available"
          : state.metadata;
      return json({
        items: targets.map(({ key, lookup_type }) => ({
          key,
          lookup_type,
          email: lookup_type === "email" ? key : null,
          directory_email:
            key === "admin@example.com"
              ? "directory-admin@example.com"
              : key === "alias@example.com"
                ? "preferred@example.com"
                : null,
          name: ["not_found", "unavailable", "pending"].includes(status)
            ? null
            : key.startsWith("oc_")
              ? "Platform Engineering"
              : key === "alex@example.com"
                ? "Alex Morgan"
                : key === "admin@example.com"
                  ? "Jamie Chen"
                  : key === "manager@example.com"
                    ? "Sam Rivera"
                    : key.startsWith("system:")
                      ? "System"
                      : "Example Person",
          avatar_url: state.brokenImage
            ? "https://images.example.com/unavailable.png"
            : null,
          metadata_status: key.startsWith("system:")
            ? "not_applicable"
            : status,
          refreshing: status === "pending",
          checked_at: 1794268800,
        })),
        retry_after_seconds: status === "pending" ? 1 : null,
      });
    }
    if (path.startsWith("/usage/")) {
      if (state.failUsage)
        return json({ error: "temporarily_unavailable" }, 503);
      const dataset = path.split("/").at(-1);
      const limit = Number(url.searchParams.get("limit") || 50);
      const offset = Number(url.searchParams.get("offset") || 0);
      const period = {
        from: url.searchParams.has("all_time") ? null : "2026-11-01T00:00:00Z",
        to: "2026-11-10T10:00:00Z",
        all_time: url.searchParams.has("all_time"),
      };
      if (dataset === "summary")
        return json({
          period,
          statistics: {
            ...metrics,
            estimated_cost_usd:
              state.pricing === "none"
                ? null
                : state.pricing === "zero"
                  ? "0"
                  : "21.60915171000000",
            priced_sessions:
              state.pricing === "none"
                ? 0
                : state.pricing === "zero"
                  ? 438
                  : 350,
            unpriced_sessions:
              state.pricing === "none"
                ? 438
                : state.pricing === "zero"
                  ? 0
                  : 88,
            total_tokens: 1280400,
            llm_sessions: 438,
            active_people: 74,
            active_groups: 9,
            turns: 397,
            activity_calls: 892,
            succeeded_calls: 826,
            failed_calls: 29,
            denied_calls: 22,
            refused_calls: 13,
            query_calls: 614,
            unavailable_sessions: 4,
            incomplete_calls: 2,
            query_estimate_missing_calls: 9,
            available_query_estimated_input_gb: 128.4,
          },
          model_spend:
            state.pricing === "none"
              ? []
              : state.pricing === "zero"
                ? [{ model: "free-model", estimated_cost_usd: "0" }]
                : [
                    { model: "model-a", estimated_cost_usd: "18.24495345" },
                    { model: "model-b", estimated_cost_usd: "3.36419826" },
                  ],
          monetary_costs_available: state.pricing !== "none",
        });
      if (dataset === "export") {
        if (url.searchParams.has("limit") || url.searchParams.has("offset"))
          return json({ error: "invalid_filter" }, 400);
        return route.fulfill({
          status: 200,
          contentType: "text/csv; charset=utf-8",
          headers: {
            "Content-Disposition": 'attachment; filename="usage-users.csv"',
            "X-Export-Row-Count": "61",
          },
          body:
            "\ufeffrequester,model,total_tokens\n" +
            Array.from({ length: 61 }, (_, i) => `t:${i},model-a,400`).join(
              "\n",
            ),
        });
      }
      let items: UsageRow[] = [];
      if (dataset === "users")
        items = Array.from({ length: 60 }, (_, i) => ({
          ...metrics,
          requester: `tenant:user-${i}`,
          email: i === 0 ? "alex@example.com" : `person${i}@example.com`,
          models: [
            { ...metrics, model: "model-a", total_tokens: 160 },
            { ...metrics, model: "model-b", total_tokens: 240 },
          ],
        }));
      if (dataset === "models")
        items = [
          { ...metrics, model: "model-a", total_tokens: 160 },
          { ...metrics, model: "model-b", total_tokens: 240 },
        ];
      if (dataset === "daily")
        items = Array.from(
          { length: url.searchParams.has("all_time") ? 60 : 9 },
          (_, i) => ({
            ...metrics,
            date: new Date(
              Date.UTC(2026, url.searchParams.has("all_time") ? 8 : 10, i + 1),
            )
              .toISOString()
              .slice(0, 10),
            estimated_cost_usd:
              state.pricing === "none"
                ? null
                : state.pricing === "zero"
                  ? "0"
                  : i === 0
                    ? null
                    : i === 1
                      ? "0"
                      : (i * 0.028).toFixed(6),
            priced_sessions: i === 0 ? 0 : 1,
            total_tokens: [
              16000, 23500, 19000, 37000, 31000, 43000, 38000, 56000, 49000,
            ][i % 9],
          }),
        );
      if (dataset === "llm")
        items = [
          {
            ...metrics,
            requester: "tenant:user-0",
            email: "alex@example.com",
            model: "model-a",
            source: "session",
            session_id: "session-a",
            job_id: "job-a",
            occurred_at: "2026-11-09T08:30:00Z",
          },
          {
            ...metrics,
            requester: "system:skill-promotion",
            model: "unknown",
            source: "unavailable",
            session_id: "system-session",
            occurred_at: "2026-11-09T08:31:00Z",
          },
        ];
      if (dataset === "queries")
        items = [
          {
            requester: "tenant:user-0",
            email: "alex@example.com",
            id: "call-a",
            tool: "run_query",
            project: "example",
            status: "incomplete",
            duration_ms: null,
            row_count: null,
            estimated_input_gb: null,
            occurred_at: "2026-11-09T08:30:00Z",
          },
        ];
      if (state.empty) items = [];
      return json({
        period,
        items: items.slice(offset, offset + limit),
        total: items.length,
        limit,
        offset,
      });
    }
    return json({ error: "not_found" }, 404);
  });
}
export const test = base.extend<{ state: State }>({
  state: async ({ context }, use) => {
    const state: State = {
      sessionSeconds: 604800,
      role: "admin",
      loggedIn: true,
      expired: false,
      metadata: "available",
      botPaused: false,
      groups: [initialGroup("oc_platform")],
      requests: [],
      knowledge: { oc_platform: initialKnowledge() },
      knowledgeJobs: {},
      knowledgeAudit: {},
      knowledgeCandidates: {},
      knowledgeDocuments: {},
      conflictNextKnowledgeSave: false,
      failWrite: false,
      failUsage: false,
      empty: false,
      brokenImage: false,
      pollCount: 0,
    };
    await mock(context, state);
    await use(state);
  },
});
export { expect };
