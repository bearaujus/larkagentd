import { useQueries } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { CircleUserRound, UsersRound, RefreshCw, Bot } from "lucide-react";
import { api, authGeneration, client } from "./api";
import type { DirectoryResult, Metadata, Target } from "./types";
import { Hint } from "./components";

const targetKey = (target: Target) => `${target.kind}:${target.key}`;
export function batches(targets: Target[]) {
  const unique = [
    ...new Map(
      targets.filter((t) => t.key).map((t) => [targetKey(t), t]),
    ).values(),
  ].sort((a, b) => targetKey(a).localeCompare(targetKey(b)));
  const result: Target[][] = [];
  for (const category of ["users", "groups"]) {
    const items = unique.filter(
      (t) => (t.kind === "group") === (category === "groups"),
    );
    for (let i = 0; i < items.length; i += 20)
      result.push(items.slice(i, i + 20));
  }
  return result;
}
let inFlight = 0;
const waiting: (() => void)[] = [];
async function lookup(
  targets: Target[],
  refresh = false,
  signal?: AbortSignal,
) {
  const generation = authGeneration();
  if (inFlight >= 2)
    await new Promise<void>((resolve) => waiting.push(resolve));
  inFlight++;
  try {
    if (signal?.aborted || generation !== authGeneration())
      throw new DOMException("Cancelled", "AbortError");
    const groups = targets[0]?.kind === "group";
    const body = groups
      ? { group_ids: targets.map((t) => t.key), refresh }
      : {
          emails: targets.filter((t) => t.kind === "email").map((t) => t.key),
          owners: targets.filter((t) => t.kind === "owner").map((t) => t.key),
          refresh,
        };
    return await api<DirectoryResult>(
      `/directory/${groups ? "groups" : "users"}`,
      {
        method: "POST",
        body: JSON.stringify(body),
        signal,
      },
    );
  } finally {
    inFlight--;
    waiting.shift()?.();
  }
}
export function useDirectory(targets: Target[]) {
  const chunks = batches(targets);
  const results = useQueries({
    queries: chunks.map((chunk) => ({
      queryKey: ["directory", chunk],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        lookup(chunk, false, signal),
      staleTime: 60_000,
      refetchInterval: (query: {
        state: { data?: DirectoryResult; error?: unknown };
      }) =>
        !query.state.error &&
        query.state.data?.items.some((item) => item.refreshing)
          ? Math.max(1, query.state.data.retry_after_seconds ?? 2) * 1000
          : (false as const),
    })),
  });
  const data = new Map<string, Metadata>();
  results.forEach((result, i) => {
    chunks[i].forEach((target) =>
      data.set(targetKey(target), {
        lookup_type: target.kind,
        key: target.key,
        name: null,
        avatar_url: null,
        metadata_status: result.isError ? "unavailable" : "pending",
        refreshing: false,
        checked_at: null,
      }),
    );
    result.data?.items.forEach((item) =>
      data.set(
        `${item.lookup_type}:${item.key}`,
        result.isError
          ? {
              ...item,
              metadata_status:
                item.name || item.avatar_url ? "stale" : "unavailable",
              refreshing: false,
            }
          : item,
      ),
    );
  });
  async function refresh(target: Target) {
    const fresh = await lookup([target], true);
    let updated = false;
    client.setQueriesData<DirectoryResult>(
      { queryKey: ["directory"] },
      (old) => {
        if (
          old?.items.some(
            (item) => `${item.lookup_type}:${item.key}` === targetKey(target),
          )
        )
          updated = true;
        return (
          old && {
            ...old,
            retry_after_seconds:
              fresh.retry_after_seconds ?? old.retry_after_seconds,
            items: old.items.map((item) =>
              `${item.lookup_type}:${item.key}` === targetKey(target)
                ? (fresh.items[0] ?? item)
                : item,
            ),
          }
        );
      },
    );
    if (!updated) await client.invalidateQueries({ queryKey: ["directory"] });
  }
  return { get: (target: Target) => data.get(targetKey(target)), refresh };
}
export function Identity({
  target,
  metadata,
  refresh,
  secondary = true,
}: {
  target: Target;
  metadata?: Metadata;
  refresh: (target: Target) => Promise<unknown>;
  secondary?: boolean;
}) {
  const [broken, setBroken] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const attempted = useRef(false);
  useEffect(() => {
    setBroken(false);
    attempted.current = false;
    setRefreshFailed(false);
  }, [target.key]);
  useEffect(() => setBroken(false), [metadata?.avatar_url]);
  const system =
    metadata?.metadata_status === "not_applicable" ||
    target.key.startsWith("system:");
  const name = system ? "System" : metadata?.name || target.key;
  const email = target.kind === "email" ? target.key : metadata?.email;
  const initial = metadata?.name
    ?.trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
  const status = refreshFailed
    ? "unavailable"
    : (metadata?.metadata_status ?? "pending");
  const labels: Record<string, string> = {
    pending: "Loading profile",
    stale: "Last-known profile",
    not_found: "Not found in directory",
    unavailable: "Profile unavailable",
  };
  async function retry() {
    setRetrying(true);
    setRefreshFailed(false);
    try {
      await refresh(target);
    } catch {
      setRefreshFailed(true);
    } finally {
      setRetrying(false);
    }
  }
  return (
    <div className="identity">
      <span
        className={`avatar ${target.kind === "group" ? "group-avatar" : ""}`}
        aria-hidden="true"
      >
        {!system && metadata?.avatar_url?.startsWith("https://") && !broken ? (
          <img
            alt=""
            referrerPolicy="no-referrer"
            src={metadata.avatar_url}
            onError={() => {
              setBroken(true);
              if (!attempted.current) {
                attempted.current = true;
                void retry();
              }
            }}
          />
        ) : system ? (
          <Bot size={19} />
        ) : (
          initial ||
          (target.kind === "group" ? (
            <UsersRound size={19} />
          ) : (
            <CircleUserRound size={19} />
          ))
        )}
      </span>
      <span className="identity-text">
        <span className="identity-name">{name}</span>
        {secondary &&
          (email && email !== name ? (
            <span className="muted">{email}</span>
          ) : target.kind === "owner" && target.key !== name ? (
            <span className="muted mono">{target.key}</span>
          ) : null)}
        {metadata?.directory_email &&
          email &&
          metadata.directory_email !== email && (
            <span
              className="muted"
              title="Display metadata only; the policy email is unchanged"
            >
              Directory: {metadata.directory_email}
            </span>
          )}
        {labels[status] && (
          <span className="metadata-status">
            {labels[status]}
            {status !== "pending" && (
              <Hint content="Refresh this display profile from Lark. Refreshing does not change membership or access.">
                <button
                  className="icon-button"
                  disabled={retrying}
                  aria-label={`Refresh profile ${target.key}`}
                  onClick={() => void retry()}
                >
                  <RefreshCw size={12} />
                </button>
              </Hint>
            )}
          </span>
        )}
      </span>
    </div>
  );
}
