import { describe, expect, it, vi } from "vitest";
import { validGroupId, suggestedGroupId } from "../src/add-dialog";
import { batches } from "../src/directory";
import {
  api,
  clearPrivateState,
  exclusiveDate,
  reportParams,
  write,
} from "../src/api";
import type {
  GroupKnowledge,
  GroupKnowledgeAuditEvent,
  GroupKnowledgeJob,
  GroupPolicy,
  Policy,
  Target,
  Tool,
} from "../src/types";
const toolShape = {
  name: "connection_status",
  kind: "builtin",
  risk: "read",
  description: "Whether the person is connected and how.",
  default: true,
  enabled_globally: true,
} satisfies Tool;
const groupPolicyShape = {
  mode: "all_users",
  members: [],
  inherited_members: [],
  group_id: "oc_example",
  tools: ["connection_status"],
} satisfies GroupPolicy;
const dmPolicyShape = {
  mode: "specific_users",
  members: [],
  inherited_members: [],
} satisfies Policy;
const groupKnowledgeShape = {
  configured_enabled: false,
  effective_state: "disabled",
  reason_codes: ["group_disabled"],
  learning_mode: "explicit",
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
} satisfies GroupKnowledge;
const groupKnowledgeJobShape = {
  id: "1",
  kind: "index",
  state: "failed",
  attempts: 1,
  error_code: "index_unavailable",
  generation: 0,
  created_at: "2026-11-25T10:00:00Z",
} satisfies GroupKnowledgeJob;
const groupKnowledgeAuditShape = {
  id: "1",
  actor_id: "admin@example.com",
  action: "settings_changed",
  target_id: "oc_example",
  outcome: "success",
  reason_code: null,
  policy_version: 0,
  created_at: "2026-11-25T10:00:00Z",
} satisfies GroupKnowledgeAuditEvent;

it("models the complete metadata-only Group Brain contract", () => {
  expect(groupKnowledgeShape.review_required).toBe(true);
  expect(groupKnowledgeShape.effective_capabilities).toEqual({
    recall: false,
    memory: false,
    documents: false,
  });
  expect(groupKnowledgeJobShape.error_code).toBe("index_unavailable");
  expect(groupKnowledgeAuditShape.target_id).toBe("oc_example");
});

it("sends exact Group Brain review mutation bodies", async () => {
  const fetchMock = vi.fn(
    async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  try {
    const base = "/groups/oc_example1234/knowledge";
    await write(`${base}/candidates/mem_example0001/approve`, "POST", {
      revision: 3,
      content_hash: "sha256_example",
      acknowledge_group_sharing: true,
    });
    await write(`${base}/candidates/mem_example0001/reject`, "POST", {
      revision: 3,
      reason_code: "not_for_group",
    });
    await write(`${base}/memory/mem_example0001/revoke`, "POST", {});
    await write(`${base}/documents/doc_example0001/approve`, "POST", {
      sharing_policy_ref: "policy_example",
      acknowledge_group_sharing: true,
    });
    await write(`${base}/documents/doc_example0001/reject`, "POST", {});
    await write(`${base}/documents/doc_example0001/refresh`, "POST", {});
    await write(`${base}/sources/doc_example0001/revoke`, "POST", {});

    expect(
      fetchMock.mock.calls.map(([url, init]) => ({
        url,
        method: init?.method,
        body: JSON.parse(String(init?.body)),
      })),
    ).toEqual([
      {
        url: `/v1/dashboard${base}/candidates/mem_example0001/approve`,
        method: "POST",
        body: {
          revision: 3,
          content_hash: "sha256_example",
          acknowledge_group_sharing: true,
        },
      },
      {
        url: `/v1/dashboard${base}/candidates/mem_example0001/reject`,
        method: "POST",
        body: { revision: 3, reason_code: "not_for_group" },
      },
      {
        url: `/v1/dashboard${base}/memory/mem_example0001/revoke`,
        method: "POST",
        body: {},
      },
      {
        url: `/v1/dashboard${base}/documents/doc_example0001/approve`,
        method: "POST",
        body: {
          sharing_policy_ref: "policy_example",
          acknowledge_group_sharing: true,
        },
      },
      {
        url: `/v1/dashboard${base}/documents/doc_example0001/reject`,
        method: "POST",
        body: {},
      },
      {
        url: `/v1/dashboard${base}/documents/doc_example0001/refresh`,
        method: "POST",
        body: {},
      },
      {
        url: `/v1/dashboard${base}/sources/doc_example0001/revoke`,
        method: "POST",
        body: {},
      },
    ]);
  } finally {
    vi.unstubAllGlobals();
  }
});

it("models scoped tools on groups without adding them to the DM policy", () => {
  expect(toolShape).toMatchObject({
    name: "connection_status",
    default: true,
    enabled_globally: true,
  });
  expect(groupPolicyShape.tools).toEqual(["connection_status"]);
  expect(dmPolicyShape).not.toHaveProperty("tools");
});

it("rejects a late private response after the session is cleared", async () => {
  let resolve!: (value: Response) => void;
  vi.stubGlobal("window", new EventTarget());
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    ),
  );
  try {
    const request = api("/roles");
    clearPrivateState();
    resolve(new Response(JSON.stringify([{ email: "synthetic@example.com" }])));
    await expect(request).rejects.toMatchObject({ name: "AbortError" });
  } finally {
    vi.unstubAllGlobals();
  }
});

describe("report contracts", () => {
  it("exports all history without pagination or conflicting dates", () => {
    const params = reportParams(
      {
        all_time: "true",
        from: "2026-01-01",
        to: "2026-02-01",
        email: "person@example.com",
        limit: "50",
        offset: "50",
      },
      "users",
    );
    expect(Object.fromEntries(params)).toEqual({
      all_time: "true",
      email: "person@example.com",
    });
  });
  it("scopes specialized filters to supported datasets", () => {
    const filters = {
      model: "model-a",
      status: "failed",
      tool: "run_query",
      source: "session",
      project: "example",
      requester: "t:u",
    };
    expect(Object.fromEntries(reportParams(filters, "llm"))).toEqual({
      model: "model-a",
      source: "session",
      requester: "t:u",
    });
    expect(Object.fromEntries(reportParams(filters, "queries"))).toEqual({
      status: "failed",
      tool: "run_query",
      project: "example",
      requester: "t:u",
    });
    expect(Object.fromEntries(reportParams(filters, "summary"))).toEqual({
      requester: "t:u",
    });
  });
  it("uses UTC exclusive bounds including month/year/leap rollover", () => {
    expect(exclusiveDate("2026-12-31")).toBe("2027-01-01");
    expect(exclusiveDate("2024-02-29")).toBe("2024-03-01");
  });
});
describe("directory batches", () => {
  it("deduplicates and limits combined email and owner targets to 20", () => {
    const targets: Target[] = Array.from({ length: 43 }, (_, i) => ({
      kind: i % 2 ? "email" : "owner",
      key: String(i),
    }));
    const chunks = batches([
      ...targets,
      ...targets,
      { kind: "group", key: "oc_example" },
    ]);
    expect(chunks.map((chunk) => chunk.length)).toEqual([20, 20, 3, 1]);
    expect(chunks.flat()).toHaveLength(44);
    expect(
      chunks.every(
        (chunk) =>
          chunk.every((t) => t.kind === "group") ||
          chunk.every((t) => t.kind !== "group"),
      ),
    ).toBe(true);
  });
});

it("validates opaque group IDs and avoids treating a name search as a registration suggestion", () => {
  expect(validGroupId("oc_example")).toBe(true);
  expect(validGroupId("x".repeat(256))).toBe(true);
  expect(validGroupId("x".repeat(257))).toBe(false);
  expect(validGroupId("oc_bad/id")).toBe(false);
  expect(suggestedGroupId("Platform")).toBe(false);
  expect(suggestedGroupId("oc_example")).toBe(true);
});
