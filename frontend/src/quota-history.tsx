import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { Empty, Failure, Loading, Panel } from "./components";

type Snapshot = Record<string, string | number | boolean | null>;
type Change = {
  id: string;
  occurred_at: string;
  actor_email: string;
  resource:
    "free_personal" | "shared_pool" | "flex_assignment" | "quota_schedule";
  target: string;
  before: Snapshot;
  after: Snapshot;
};
type History = { items: Change[]; next_before: string | null };
const time = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "medium",
  timeZone: "UTC",
});
const fields: Record<string, string> = {
  limit: "Saved limit",
  free_limit: "Personal limit",
  current_cycle_limit: "Temporary limit",
  recurring_limit: "Recurring limit",
  pending_limit: "Scheduled limit",
  effective_on: "Scheduled date",
  adjustment_ends_on: "Adjustment ends",
  cadence: "Reset frequency",
  pending_cadence: "Scheduled frequency",
  enabled: "FlexTier enabled",
};

function setting(change: Change) {
  if (change.resource === "free_personal") return "Personal FreeTier";
  if (change.resource === "shared_pool")
    return change.target === "free"
      ? "FreeTier shared pool"
      : "FlexTier shared pool";
  if (change.resource === "flex_assignment") return change.target;
  return "Quota settings";
}

function valueOf(field: string, value: Snapshot[string] | undefined) {
  if (value === undefined) return "None";
  if (value === null) return field.endsWith("limit") ? "Unlimited" : "None";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return `${value.toLocaleString()} tokens`;
  if (value === "weekly" || value === "monthly")
    return value === "weekly" ? "Weekly" : "Monthly";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return `${value} at 00:00 UTC`;
  return value;
}

export function QuotaHistory() {
  const [cursor, setCursor] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["governance-history", cursor],
    queryFn: ({ signal }) =>
      api<History>(
        `/governance/history${cursor ? `?before=${encodeURIComponent(cursor)}` : ""}`,
        { signal },
      ),
  });
  return (
    <Panel title="Quota change history">
      {query.isPending ? (
        <Loading label="Loading quota changes" />
      ) : query.isError ? (
        <div className="panel-body">
          <Failure error={query.error} retry={() => void query.refetch()} />
        </div>
      ) : query.data.items.length ? (
        <>
          <div
            className="table-scroll"
            role="region"
            aria-label="Quota changes"
            tabIndex={0}
          >
            <table>
              <caption className="sr-only">Quota changes, newest first</caption>
              <thead>
                <tr>
                  <th scope="col">Time (UTC)</th>
                  <th scope="col">Changed by</th>
                  <th scope="col">Setting</th>
                  <th scope="col">Changes</th>
                </tr>
              </thead>
              <tbody>
                {query.data.items.map((change) => {
                  const keys = [
                    ...new Set([
                      ...Object.keys(change.before),
                      ...Object.keys(change.after),
                    ]),
                  ];
                  return (
                    <tr key={change.id}>
                      <td>{time.format(new Date(change.occurred_at))}</td>
                      <td>{change.actor_email}</td>
                      <td>{setting(change)}</td>
                      <td>
                        <dl className="quota-history-changes">
                          {keys
                            .filter(
                              (key) => change.before[key] !== change.after[key],
                            )
                            .map((key) => (
                              <div key={key}>
                                <dt>{fields[key] ?? key}</dt>
                                <dd>
                                  {valueOf(key, change.before[key])} {"->"}{" "}
                                  {valueOf(key, change.after[key])}
                                </dd>
                              </div>
                            ))}
                        </dl>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {(cursor || query.data.next_before) && (
            <div className="panel-body save-row">
              {cursor && (
                <button onClick={() => setCursor(null)}>Newest changes</button>
              )}
              {query.data.next_before && (
                <button onClick={() => setCursor(query.data.next_before)}>
                  Older changes
                </button>
              )}
            </div>
          )}
        </>
      ) : (
        <Empty title="No quota changes recorded">
          New quota edits will show who made them and what changed.
        </Empty>
      )}
    </Panel>
  );
}
