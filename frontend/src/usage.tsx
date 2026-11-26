import { Fragment, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  ChevronDown,
  ChevronRight,
  Download,
  Filter,
  RefreshCw,
} from "lucide-react";
import { api, download, exclusiveDate, reportParams } from "./api";
import {
  Badge,
  CopyId,
  Empty,
  Failure,
  Loading,
  Notice,
  Panel,
  number,
  shortNumber,
  time,
  useAction,
} from "./components";
import { Identity, useDirectory } from "./directory";
import { PageIntro } from "./policies";
import type {
  Dataset,
  Filters,
  Metrics,
  PageResult,
  Summary,
  Target,
  UsageRow,
  Tool,
} from "./types";

const datasets: [Dataset, string][] = [
  ["users", "Users"],
  ["models", "Models"],
  ["daily", "Daily"],
  ["llm", "Sessions"],
  ["queries", "query activity"],
];
export function ExportButton({
  dataset,
  filters,
}: {
  dataset: Dataset | "all";
  filters: Filters;
}) {
  const action = useAction();
  const [rows, setRows] = useState<string | null>(null);
  return (
    <div className="export-control">
      <button
        className="primary"
        disabled={action.busy}
        onClick={() =>
          void action.run(
            async () => setRows(await download(dataset, filters)),
            "Download ready.",
          )
        }
      >
        <Download size={16} />
        {action.busy ? "Preparing export..." : "Export all matching data"}
      </button>
      {action.feedback}
      {rows !== null && (
        <small>{number(Number(rows))} matching rows exported</small>
      )}
    </div>
  );
}
export function FilterBar({
  dataset,
  onApply,
}: {
  dataset: Dataset | "summary";
  onApply: (value: Filters) => void;
}) {
  const tools = useQuery({
    queryKey: ["tools"],
    queryFn: () => api<Tool[]>("/tools"),
    enabled: dataset === "queries",
  });
  const [period, setPeriod] = useState("month");
  const [draft, setDraft] = useState<Filters>({});
  const [invalid, setInvalid] = useState("");
  const set = (key: string, value: string) =>
    setDraft((old) => ({ ...old, [key]: value }));
  const field = (key: string, label: string, type = "text") => (
    <label key={key}>
      {label}
      <input
        type={type}
        value={draft[key] ?? ""}
        onChange={(e) => set(key, e.target.value)}
        autoComplete="off"
      />
    </label>
  );
  const select = (key: string, label: string, values: [string, string][]) => (
    <label key={key}>
      {label}
      <select
        aria-label={label}
        value={draft[key] ?? ""}
        onChange={(e) => set(key, e.target.value)}
      >
        <option value="">All</option>
        {values.map(([value, text]) => (
          <option key={value} value={value}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );
  return (
    <form
      className="filters"
      onSubmit={(e) => {
        e.preventDefault();
        setInvalid("");
        if (
          period === "custom" &&
          (!draft.from || !draft.through || draft.from > draft.through)
        ) {
          setInvalid("Choose a valid start and through date.");
          return;
        }
        const result = { ...draft };
        delete result.through;
        delete result.from;
        delete result.to;
        delete result.all_time;
        if (period === "all") result.all_time = "true";
        if (period === "custom") {
          result.from = draft.from;
          result.to = exclusiveDate(draft.through);
        }
        onApply(result);
      }}
    >
      <div className="filter-main">
        <label>
          Period · UTC
          <select value={period} onChange={(e) => setPeriod(e.target.value)}>
            <option value="month">This month</option>
            <option value="all">All history</option>
            <option value="custom">Custom dates</option>
          </select>
        </label>
        {period === "custom" && (
          <>
            {field("from", "From · UTC", "date")}
            {field("through", "Through · UTC", "date")}
          </>
        )}
        {field("email", "User email", "email")}
        <button type="submit">
          <Filter size={15} />
          Apply filters
        </button>
        <button
          type="button"
          className="text-button"
          onClick={() => {
            setPeriod("month");
            setDraft({});
            setInvalid("");
            onApply({});
          }}
        >
          Reset
        </button>
      </div>
      <details className="advanced-filters">
        <summary>More filters</summary>
        <div className="filter-grid">
          {field("requester", "Requester ID")}
          {select("chat_type", "Chat type", [
            ["group", "Group"],
            ["system", "System"],
            ["unknown", "Unknown"],
          ])}
          {field("job_id", "Job ID")}
          {field("session_id", "Session ID")}
          {(dataset === "llm" || dataset === "models") && (
            <>
              {field("model", "Recorded model")}
              {select("source", "Usage source", [
                ["session", "Provider usage recorded"],
                ["unavailable", "Usage unavailable"],
              ])}
            </>
          )}
          {dataset === "queries" && (
            <>
              {select(
                "tool",
                "Tool",
                (tools.data ?? [])
                  .filter((tool) => tool.activity_kind)
                  .map((tool) => [tool.name, tool.label || tool.name]),
              )}
              {field("project", "Project")}
              {select(
                "status",
                "Outcome",
                ["succeeded", "failed", "refused", "denied", "incomplete"].map(
                  (value) => [value, value],
                ),
              )}
            </>
          )}
        </div>
        <p className="muted">
          Exact matches. Email uses current saved profile data; requester ID is
          the stable identity.
        </p>
      </details>
      {invalid && <Notice tone="error">{invalid}</Notice>}
    </form>
  );
}
function estimatedUsd(value: string | null | undefined) {
  if (value == null) return "Unavailable";
  const amount = Number(value);
  if (amount > 0 && amount < 0.000001) return "< $0.000001";
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 6,
  }).format(amount);
}
function Coverage({ stats }: { stats: Metrics }) {
  return (
    <div className="coverage">
      <div>
        <span className="eyebrow">DATA COVERAGE</span>
        <p>
          Recorded usage only. Tracking failures and older query activity cannot
          be reconstructed.
        </p>
        {stats.priced_sessions !== undefined && (
          <p>
            Estimated model spend:{" "}
            <strong>{estimatedUsd(stats.estimated_cost_usd)}</strong>
            {" · "}
            {number(stats.priced_sessions)} priced of{" "}
            {number(stats.llm_sessions)} model sessions
          </p>
        )}
      </div>
      <div>
        <strong>{number(stats.unavailable_sessions)}</strong>
        <span>sessions without provider usage</span>
      </div>
      <div>
        <strong>{number(stats.query_estimate_missing_calls)}</strong>
        <span>successful queries without estimates</span>
      </div>
      <div>
        <strong>{number(stats.incomplete_calls)}</strong>
        <span>calls without a terminal outcome</span>
      </div>
    </div>
  );
}
function SummaryCards({ data }: { data: Summary }) {
  const stats = data.statistics;
  return (
    <>
      <div className="metrics-grid">
        {[
          ["Total tokens", stats.total_tokens, "Input, cache and output"],
          [
            "LLM sessions",
            stats.llm_sessions,
            `${number(stats.turns)} request turns`,
          ],
          [
            "Tool activity",
            stats.activity_calls,
            `${number(stats.query_calls)} query / preview calls`,
          ],
          [
            "Available query estimates",
            stats.available_query_estimated_input_gb,
            "GB · successful queries / previews only",
          ],
        ].map(([label, value, note]) => (
          <div className="metric" key={String(label)}>
            <span>{label}</span>
            <strong title={number(value as number | null)}>
              {shortNumber(value as number | null)}
            </strong>
            <small>{note}</small>
          </div>
        ))}
      </div>
      <Coverage stats={stats} />
    </>
  );
}
function Trend({ data }: { data: PageResult }) {
  const [metric, setMetric] = useState<"total_tokens" | "activity_calls">(
    "total_tokens",
  );
  return (
    <div className="trend">
      <div className="trend-heading">
        <div>
          <h3>Daily trend</h3>
          <p>
            UTC ·{" "}
            {data.items.length
              ? `Recorded days ${data.offset + 1}-${data.offset + data.items.length} of ${number(data.total)}`
              : "No recorded days"}
          </p>
        </div>
        <label className="sr-only" htmlFor="trend-metric">
          Trend metric
        </label>
        <select
          id="trend-metric"
          value={metric}
          onChange={(e) => setMetric(e.target.value as typeof metric)}
        >
          <option value="total_tokens">Tokens</option>
          <option value="activity_calls">Tool calls</option>
        </select>
      </div>
      {data.items.length ? (
        <>
          <div
            className="chart"
            role="group"
            aria-label={`Daily ${metric === "total_tokens" ? "tokens" : "tool calls"}; exact values in the daily table`}
          >
            <ResponsiveContainer width="100%" height={240}>
              <AreaChart
                data={data.items}
                margin={{ left: 0, right: 15, top: 12, bottom: 0 }}
                accessibilityLayer
              >
                <CartesianGrid
                  strokeDasharray="3 5"
                  vertical={false}
                  stroke="#e7eaf0"
                />
                <XAxis
                  dataKey="date"
                  tickFormatter={(value) => String(value).slice(5)}
                  tickLine={false}
                  axisLine={false}
                  minTickGap={32}
                  fontSize={12}
                />
                <YAxis
                  tickFormatter={(value) => shortNumber(Number(value))}
                  tickLine={false}
                  axisLine={false}
                  width={55}
                  fontSize={12}
                />
                <Tooltip formatter={(value) => number(Number(value))} />
                <Area
                  type="linear"
                  dataKey={metric}
                  name={metric === "total_tokens" ? "Tokens" : "Tool calls"}
                  stroke="#365fe5"
                  fill="#edf1ff"
                  strokeWidth={2}
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
          <p className="chart-note">
            The chart shows this page of recorded days. Missing days are not
            proof of zero usage.
          </p>
        </>
      ) : (
        <Empty title="No daily activity">
          Try a different period or remove filters.
        </Empty>
      )}
    </div>
  );
}
export function Pagination({
  data,
  setPage,
}: {
  data: PageResult;
  setPage: (page: { limit: number; offset: number }) => void;
}) {
  const end = data.offset + data.items.length;
  const capped = data.offset + data.limit > 100000;
  return (
    <div className="pagination">
      <span>
        {data.total
          ? `${number(data.offset + 1)}-${number(end)} of ${number(data.total)}`
          : "0 results"}
      </span>
      <div>
        <label>
          Rows
          <select
            aria-label="Rows"
            value={data.limit}
            onChange={(e) =>
              setPage({ offset: 0, limit: Number(e.target.value) })
            }
          >
            {[25, 50, 100, 200].map((n) => (
              <option key={n}>{n}</option>
            ))}
          </select>
        </label>
        <button
          disabled={data.offset === 0}
          onClick={() =>
            setPage({
              limit: data.limit,
              offset: Math.max(0, data.offset - data.limit),
            })
          }
        >
          Previous
        </button>
        <button
          disabled={end >= data.total || capped}
          onClick={() =>
            setPage({ limit: data.limit, offset: data.offset + data.limit })
          }
        >
          Next
        </button>
      </div>
      {capped && end < data.total && (
        <Notice>
          Narrow the filters to browse further, or export all matching data.
        </Notice>
      )}
    </div>
  );
}
const tokenFields = [
  ["input_tokens", "Uncached input"],
  ["cache_read_tokens", "Cache reads"],
  ["cache_write_tokens", "Cache writes"],
  ["output_tokens", "Output"],
  ["reasoning_tokens", "Reasoning (included in output)"],
] as const;
function TokenDetails({ row }: { row: UsageRow }) {
  return (
    <dl className="detail-grid">
      {tokenFields.map(([key, label]) => (
        <div key={key}>
          <dt>{label}</dt>
          <dd>{number(row[key])}</dd>
        </div>
      ))}
    </dl>
  );
}
function RecordDetails({ row }: { row: UsageRow }) {
  return (
    <div className="record-details">
      <dl className="detail-grid">
        {[
          ["Requester", row.requester],
          ["Principal", row.principal],
          ["Chat type", row.chat_type],
          ["Usage source", row.source],
          ...(row.quota_tier
            ? [
                [
                  "Quota tier",
                  row.quota_tier === "flex" ? "FlexTier" : "FreeTier",
                ],
              ]
            : []),
          ["Error code", row.error_code],
        ].map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value || "Unavailable"}</dd>
          </div>
        ))}
      </dl>
      <div className="record-ids">
        {row.session_id && (
          <span>
            Session <CopyId value={row.session_id} />
          </span>
        )}
        {row.job_id && (
          <span>
            Job <CopyId value={row.job_id} />
          </span>
        )}
      </div>
      {row.tool ? (
        <p className="muted">
          Tool invocation metadata only. No SQL, arguments or result contents
          are exposed.
        </p>
      ) : (
        <TokenDetails row={row} />
      )}
    </div>
  );
}
export function UsageTable({
  data,
  dataset,
}: {
  data: PageResult;
  dataset: Dataset;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const targets = data.items
    .filter((row) => row.email || row.requester)
    .map((row) => ({
      kind: row.email ? ("email" as const) : ("owner" as const),
      key: row.email || row.requester!,
    }));
  const directory = useDirectory(targets);
  const identity = (row: UsageRow) => {
    const target: Target = {
      kind: row.email ? "email" : "owner",
      key: row.email || row.requester || "unknown",
    };
    return (
      <Identity
        target={target}
        metadata={directory.get(target)}
        refresh={directory.refresh}
      />
    );
  };
  const baseHeaders =
    dataset === "users"
      ? ["User", "Tokens", "Sessions", "Turns", "Tool calls", "Missing usage"]
      : dataset === "models"
        ? ["Recorded model", "Tokens", "Sessions", "Turns", "Missing usage"]
        : dataset === "daily"
          ? [
              "Day · UTC",
              "Tokens",
              "Sessions",
              "Tool calls",
              "Incomplete calls",
            ]
          : dataset === "llm"
            ? ["Time · UTC / user", "Recorded model", "Tokens", "Source"]
            : [
                "Time · UTC / user",
                "Tool / project",
                "Outcome",
                "Duration",
                "Rows",
                "Scan estimate",
              ];
  const headers =
    dataset === "queries" ? baseHeaders : [...baseHeaders, "Estimated USD"];
  if (!data.items.length)
    return (
      <Empty title="No matching records">
        Try All history or remove a filter. Missing records do not establish
        zero usage.
      </Empty>
    );
  return (
    <div className="table-scroll">
      <table>
        <caption className="sr-only">
          {datasets.find(([key]) => key === dataset)?.[1]} usage, current page
        </caption>
        <thead>
          <tr>
            <th scope="col">
              <span className="sr-only">Details</span>
            </th>
            {headers.map((label) => (
              <th key={label} scope="col">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.items.map((row, index) => {
            const key =
              row.id ||
              row.session_id ||
              row.requester ||
              row.model ||
              row.date ||
              String(index);
            const open = expanded.has(key);
            return (
              <Fragment key={key}>
                <tr>
                  <td>
                    <button
                      className="icon-button"
                      aria-label={`${open ? "Collapse" : "Expand"} ${row.email || row.requester || row.model || row.date || row.session_id || "record"}`}
                      aria-expanded={open}
                      onClick={() =>
                        setExpanded((old) => {
                          const next = new Set(old);
                          if (open) next.delete(key);
                          else next.add(key);
                          return next;
                        })
                      }
                    >
                      {open ? (
                        <ChevronDown size={16} />
                      ) : (
                        <ChevronRight size={16} />
                      )}
                    </button>
                  </td>
                  {dataset === "users" && (
                    <>
                      <td>{identity(row)}</td>
                      <td className="numeric strong">
                        {number(row.total_tokens)}
                      </td>
                      <td className="numeric">{number(row.llm_sessions)}</td>
                      <td className="numeric">{number(row.turns)}</td>
                      <td className="numeric">{number(row.activity_calls)}</td>
                      <td className="numeric">
                        {number(row.unavailable_sessions)}
                      </td>
                    </>
                  )}
                  {dataset === "models" && (
                    <>
                      <td className="strong">{row.model || "Unknown model"}</td>
                      <td className="numeric strong">
                        {number(row.total_tokens)}
                      </td>
                      <td className="numeric">{number(row.llm_sessions)}</td>
                      <td className="numeric">{number(row.turns)}</td>
                      <td className="numeric">
                        {number(row.unavailable_sessions)}
                      </td>
                    </>
                  )}
                  {dataset === "daily" && (
                    <>
                      <td>{row.date}</td>
                      <td className="numeric strong">
                        {number(row.total_tokens)}
                      </td>
                      <td className="numeric">{number(row.llm_sessions)}</td>
                      <td className="numeric">{number(row.activity_calls)}</td>
                      <td className="numeric">
                        {number(row.incomplete_calls)}
                      </td>
                    </>
                  )}
                  {dataset === "llm" && (
                    <>
                      <td>
                        <span className="record-time">
                          {time(row.occurred_at)}
                        </span>
                        {identity(row)}
                      </td>
                      <td>{row.model || "Unknown model"}</td>
                      <td className="numeric strong">
                        {row.source === "unavailable"
                          ? "Unavailable"
                          : number(row.total_tokens)}
                      </td>
                      <td>
                        <Badge
                          tone={row.source === "unavailable" ? "amber" : ""}
                        >
                          {row.source === "unavailable"
                            ? "Usage unavailable"
                            : "Recorded"}
                        </Badge>
                      </td>
                    </>
                  )}
                  {dataset !== "queries" && (
                    <td className="numeric">
                      {estimatedUsd(row.estimated_cost_usd)}
                    </td>
                  )}
                  {dataset === "queries" && (
                    <>
                      <td>
                        <span className="record-time">
                          {time(row.occurred_at)}
                        </span>
                        {identity(row)}
                      </td>
                      <td>
                        <strong className="mono">{row.tool}</strong>
                        <span className="block muted">
                          {row.project || "Project unavailable"}
                        </span>
                      </td>
                      <td>
                        <Badge
                          tone={
                            row.status === "succeeded"
                              ? "green"
                              : row.status === "incomplete"
                                ? "amber"
                                : ""
                          }
                        >
                          {row.status}
                        </Badge>
                      </td>
                      <td className="numeric">
                        {row.duration_ms == null
                          ? "Unavailable"
                          : `${number(row.duration_ms)} ms`}
                      </td>
                      <td className="numeric">{number(row.row_count)}</td>
                      <td className="numeric">
                        {row.estimated_input_gb == null
                          ? "Unavailable"
                          : `${number(row.estimated_input_gb)} GB`}
                      </td>
                    </>
                  )}
                </tr>
                {open && (
                  <tr className="expanded-row">
                    <td colSpan={headers.length + 1}>
                      {dataset === "users" ? (
                        <>
                          <h3>Historical model breakdown</h3>
                          {row.models?.length ? (
                            <table className="model-breakdown">
                              <caption className="sr-only">
                                Historical models for{" "}
                                {row.email || row.requester}
                              </caption>
                              <thead>
                                <tr>
                                  <th>Recorded model</th>
                                  <th>Tokens</th>
                                  <th>Sessions</th>
                                  <th>Turns</th>
                                  <th>Missing usage</th>
                                  <th>Estimated USD</th>
                                </tr>
                              </thead>
                              <tbody>
                                {row.models.map((model) => (
                                  <tr key={model.model || "unknown"}>
                                    <td>{model.model || "Unknown model"}</td>
                                    <td>{number(model.total_tokens)}</td>
                                    <td>{number(model.llm_sessions)}</td>
                                    <td>{number(model.turns)}</td>
                                    <td>
                                      {number(model.unavailable_sessions)}
                                    </td>
                                    <td>
                                      {estimatedUsd(model.estimated_cost_usd)}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          ) : (
                            <p>
                              No recorded LLM models. This user may have tool
                              activity only.
                            </p>
                          )}
                          <p className="muted">
                            Models are preserved from each historical session.
                            Model turn counts are not additive.
                          </p>
                          <TokenDetails row={row} />
                        </>
                      ) : dataset === "llm" || dataset === "queries" ? (
                        <RecordDetails row={row} />
                      ) : (
                        <TokenDetails row={row} />
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
export function Usage({ overview = false }: { overview?: boolean }) {
  const [dataset, setDataset] = useState<Dataset>(overview ? "daily" : "users");
  const [filters, setFilters] = useState<Filters>({});
  const [page, setPage] = useState({ limit: 50, offset: 0 });
  const queryString = reportParams(filters, dataset, page).toString();
  const query = useQuery({
    queryKey: ["usage", dataset, queryString],
    queryFn: ({ signal }) =>
      api<PageResult>(`/usage/${dataset}?${queryString}`, { signal }),
  });
  const summaryString = reportParams(filters, "summary").toString();
  const summary = useQuery({
    queryKey: ["summary", summaryString],
    queryFn: ({ signal }) =>
      api<Summary>(`/usage/summary?${summaryString}`, { signal }),
    enabled: overview,
  });
  const period = query.data?.period ?? summary.data?.period;
  return (
    <>
      <PageIntro
        title={overview ? "Overview" : "Usage"}
        description={
          overview
            ? "A clear view of assistant activity and access."
            : "Recorded usage across people, models and data connectors. Statistics only."
        }
        action={
          <ExportButton
            dataset={overview ? "all" : dataset}
            filters={filters}
          />
        }
      />
      {!overview && (
        <nav className="tabs" aria-label="Usage reports">
          {datasets.map(([value, label]) => (
            <button
              key={value}
              aria-current={dataset === value ? "page" : undefined}
              onClick={() => {
                setDataset(value);
                setPage((old) => ({ ...old, offset: 0 }));
              }}
            >
              {label}
            </button>
          ))}
        </nav>
      )}
      <FilterBar
        dataset={overview ? "summary" : dataset}
        onApply={(value) => {
          setFilters(value);
          setPage((old) => ({ ...old, offset: 0 }));
        }}
      />
      <div className="report-context">
        <span>
          {period
            ? `${period.all_time ? "All recorded history" : `${period.from?.slice(0, 10)} onward`} · before ${time(period.to)} UTC`
            : "Period will appear when the report loads"}
        </span>
        <button
          className="text-button"
          onClick={() => {
            void query.refetch();
            if (overview) void summary.refetch();
          }}
          disabled={query.isFetching}
        >
          <RefreshCw size={14} />
          Refresh
        </button>
      </div>
      {overview &&
        (summary.isPending ? (
          <Loading label="Loading totals" />
        ) : summary.isError ? (
          <Failure error={summary.error} retry={() => void summary.refetch()} />
        ) : (
          <SummaryCards data={summary.data} />
        ))}
      <Panel
        title={
          overview
            ? "Activity over time"
            : datasets.find(([value]) => value === dataset)?.[1]
        }
        description={
          dataset === "queries"
            ? "Invocations, not distinct SQL jobs. Estimates are not measured scans. Incomplete does not mean still running."
            : dataset === "llm"
              ? "Each row is a recorded session or attempt, with its captured model."
              : undefined
        }
      >
        {query.isPending ? (
          <Loading />
        ) : query.isError ? (
          <Failure error={query.error} retry={() => void query.refetch()} />
        ) : (
          <>
            {dataset === "daily" && <Trend data={query.data} />}
            <UsageTable
              key={`${dataset}:${queryString}`}
              data={query.data}
              dataset={dataset}
            />
            <Pagination data={query.data} setPage={setPage} />
          </>
        )}
      </Panel>
      <p className="footnote">
        Usage may be incomplete. Unavailable counts do not mean free usage. Scan
        estimates cover available successful query/preview estimates only;
        historical query activity was not backfilled.
      </p>
    </>
  );
}
