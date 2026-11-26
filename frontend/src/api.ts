import { QueryClient } from "@tanstack/react-query";
import type { Dataset, Filters, Session } from "./types";

export const client = new QueryClient({
  defaultOptions: {
    queries: { retry: false, staleTime: 15_000, refetchOnWindowFocus: false },
    mutations: { retry: false },
  },
});
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
  ) {
    super(code);
  }
}
let generation = 0;
export const authGeneration = () => generation;
const active = new Set<AbortController>();
export function clearPrivateState() {
  generation++;
  for (const controller of active) controller.abort();
  active.clear();
  void client.cancelQueries();
  client.clear();
  window.dispatchEvent(new Event("dashboard-signed-out"));
}
export async function response(path: string, init: RequestInit = {}) {
  const at = generation;
  const controller = new AbortController();
  active.add(controller);
  const abort = () => controller.abort();
  init.signal?.addEventListener("abort", abort, { once: true });
  if (init.signal?.aborted) controller.abort();
  try {
    const result = await fetch(`/v1/dashboard${path}`, {
      ...init,
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
      headers: {
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
    });
    if (at !== generation) throw new DOMException("Cancelled", "AbortError");
    if (!result.ok) {
      if (result.status === 401 && path !== "/auth/exchange")
        clearPrivateState();
      const error = await result.json().catch(() => ({}));
      throw new ApiError(
        result.status,
        typeof error.error === "string" ? error.error : "request_failed",
      );
    }
    return result;
  } finally {
    active.delete(controller);
    init.signal?.removeEventListener("abort", abort);
  }
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const at = generation;
  const result = await response(path, init);
  const data = result.status === 204 ? undefined : await result.json();
  if (at !== generation) throw new DOMException("Cancelled", "AbortError");
  return data as T;
}
export const write = <T = void>(path: string, method: string, body?: object) =>
  api<T>(path, { method, ...(body ? { body: JSON.stringify(body) } : {}) });
export async function session() {
  const value = await api<Session>("/auth/session");
  if (!["admin", "manager", "group_admin"].includes(value.role)) {
    clearPrivateState();
    throw new ApiError(403, "dashboard_role_required");
  }
  return value;
}
export function message(error: unknown) {
  if (!(error instanceof ApiError))
    return "Unable to connect. Check your connection and try again.";
  const codes: Record<string, string> = {
    group_admin_required: "Your current Lark group role could not be verified.",
    group_dashboard_scope: "This session is limited to your group knowledge.",
    invalid_source: "Check the source name and content or MCP resource URI.",
    invalid_origin:
      "This address can't make dashboard changes. Open an approved dashboard address and sign in there.",
    admin_required: "Only admins can manage managers or change group tools.",
    source_name_in_use: "A source with this name already exists.",
    source_not_found: "This source has been removed. Refresh the source list.",
    group_membership_required:
      "Current group membership is required to read conversation evidence.",
    knowledge_changed:
      "Group knowledge changed while checking sources. Refresh to see the latest facts.",
    governance_admin_required: "Only admins can edit governance settings.",
    dashboard_role_required:
      "Dashboard access is limited to admins and managers.",
    managed_by_config:
      "Configured admins are protected. Contact your configuration owner.",
    group_exists:
      "This group is already registered. Open it from the group list.",
    version_conflict:
      "Someone else changed learning settings. Reload and try again.",
    hash_mismatch:
      "This note changed after it was shown. Reload the review list and try again.",
    source_unavailable:
      "The note's source is no longer available to this reviewer.",
    sharing_authorization_required:
      "Confirm that this item may be shared with everyone in the group.",
    missing_group_notice: "Add a valid group notice reference before enabling.",
    missing_scope:
      "A required group scope is missing. Check the group settings.",
    purging:
      "Conversation knowledge is being purged. Wait for the purge to finish.",
    group_brain_data_present:
      "Conversation knowledge must be cleared for this group. Purge it first, then remove the group.",
    invalid_transition: "This learning state change is not allowed.",
    not_registered: "This group is no longer registered. Refresh the page.",
    invalid_value:
      "One or more learning settings are outside the allowed range.",
    invalid_reference: "The notice or policy reference is not valid.",
    invalid_confirmation:
      "The confirmation did not match this group's chat ID.",
    invalid_email: "Enter a valid email address.",
    invalid_group_id: "Enter a valid Lark group ID.",
    invalid_tool:
      "One or more selected tools are not available for this group. Refresh and try again.",
    invalid_fields:
      "The request was not valid. Refresh the page and try again.",
    invalid_model: "Choose a configured model.",
    invalid_effort: "Choose an effort supported by this model.",
    invalid_rate: "Enter a nonnegative rate with up to eight decimal places.",
    invalid_history_cursor: "Reload the quota history and try again.",
    invalid_token_limit:
      "Enter a nonnegative whole number within the allowed range.",
    invalid_filter: "Check the filters for this report.",
    invalid_time_range: "Choose an end date after the start date.",
  };
  return (
    codes[error.code] ??
    {
      400: "Check the entered values and try again.",
      401: "Your login has expired or was revoked. Request a new token in Lark.",
      403: "You do not have permission to perform this action.",
      404: "This item no longer exists. Refresh the page.",
      409: "This change conflicts with the current configuration. Refresh and try again.",
      429: "Too many attempts. Wait one minute before trying again.",
      503: "The service is temporarily unavailable. Try again shortly.",
    }[error.status] ??
    "The request could not be completed. Try again."
  );
}
export function reportParams(
  filters: Filters,
  dataset: Dataset | "summary" | "all",
  page?: { limit: number; offset: number },
) {
  const allowed = [
    "from",
    "to",
    "all_time",
    "requester",
    "email",
    "chat_type",
    "job_id",
    "session_id",
    ...(dataset === "models" || dataset === "llm" ? ["model", "source"] : []),
    ...(dataset === "queries" ? ["tool", "project", "status"] : []),
  ];
  const params = new URLSearchParams();
  for (const key of allowed)
    if (
      filters[key] &&
      !(filters.all_time === "true" && ["from", "to"].includes(key))
    )
      params.set(key, filters[key]);
  if (page) {
    params.set("limit", String(page.limit));
    params.set("offset", String(page.offset));
  }
  return params;
}
export function exclusiveDate(day: string) {
  const date = new Date(`${day}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString().slice(0, 10);
}
export async function download(dataset: Dataset | "all", filters: Filters) {
  const at = generation;
  const params = reportParams(filters, dataset);
  params.set("dataset", dataset);
  const result = await response(`/usage/export?${params}`);
  if (!result.headers.get("content-type")?.startsWith("text/csv"))
    throw new ApiError(503, "invalid_download");
  const blob = await result.blob();
  if (at !== generation) throw new DOMException("Cancelled", "AbortError");
  const filename =
    result.headers
      .get("content-disposition")
      ?.match(/filename="([a-zA-Z0-9._-]+)"/)?.[1] ?? `usage-${dataset}.csv`;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return result.headers.get("X-Export-Row-Count");
}
