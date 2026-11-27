import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router";
import type { UseQueryResult } from "@tanstack/react-query";
import {
  ArrowUpRight,
  ChevronLeft,
  ChevronRight,
  Maximize2,
  Minimize2,
} from "lucide-react";
import {
  Empty,
  Failure,
  Loading,
  Notice,
  number,
  shortNumber,
} from "./components";
import type { Filters, Metrics, PageResult, Summary } from "./types";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
export function estimatedUsd(value: string | null | undefined) {
  if (value == null) return "Unavailable";
  const amount = Number(value);
  if (amount > 0 && amount < 0.000001) return "< $0.000001";
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 6,
  }).format(amount);
}

function displayDay(value: string, year = false) {
  return new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    ...(year ? ({ year: "numeric" } as const) : {}),
    timeZone: "UTC",
  }).format(new Date(value));
}
function headlineUsd(value: string | null | undefined) {
  if (value == null) return "Unavailable";
  if (Number(value) > 0 && Number(value) < 0.01) return "< $0.01";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
  }).format(Number(value));
}

function SpendMetrics({ stats }: { stats: Metrics }) {
  const priced = stats.priced_sessions ?? 0;
  const finished =
    stats.succeeded_calls +
    stats.failed_calls +
    stats.denied_calls +
    stats.refused_calls;
  return (
    <dl className="cost-kpis">
      <div className="spend-kpi">
        <dt>Estimated model spend</dt>
        <dd title={estimatedUsd(stats.estimated_cost_usd)}>
          {headlineUsd(stats.estimated_cost_usd)}
        </dd>
        <dd className="kpi-caption">
          {number(priced)} of {number(stats.llm_sessions)}{" "}
          {stats.llm_sessions === 1 ? "session" : "sessions"} priced
        </dd>
      </div>
      <div>
        <dt>Active people</dt>
        <dd>{number(stats.active_people)}</dd>
        <dd className="kpi-caption">
          Across {number(stats.active_groups)} active{" "}
          {stats.active_groups === 1 ? "group" : "groups"}
        </dd>
      </div>
      <div>
        <dt>Total tokens</dt>
        <dd title={number(stats.total_tokens)}>
          {shortNumber(stats.total_tokens)}
        </dd>
        <dd className="kpi-caption">
          {number(stats.turns)}{" "}
          {stats.turns === 1 ? "request turn" : "request turns"}
          {" · "}
          {number(stats.llm_sessions)} model{" "}
          {stats.llm_sessions === 1 ? "session" : "sessions"}
        </dd>
      </div>
      <div>
        <dt>Tool calls</dt>
        <dd>{number(stats.activity_calls)}</dd>
        <dd className="kpi-caption">
          {finished
            ? `${Math.round((100 * stats.succeeded_calls) / finished)}% of finished calls succeeded`
            : "No finished calls"}
        </dd>
      </div>
    </dl>
  );
}

function ModelSpend({ summary }: { summary: Summary }) {
  const rows = summary.model_spend ?? [];
  const total = Number(summary.statistics.estimated_cost_usd);
  const missing =
    summary.statistics.llm_sessions - (summary.statistics.priced_sessions ?? 0);
  return (
    <section className="model-spend">
      <h3>{rows.length ? "Spend by model" : "Model pricing"}</h3>
      {rows.length > 0 ? (
        <>
          <p>Share of estimated spend</p>
          <ul>
            {rows.map((row) => {
              const share =
                total > 0
                  ? Math.max(
                      0,
                      Math.min(
                        100,
                        (100 * Number(row.estimated_cost_usd)) / total,
                      ),
                    )
                  : null;
              return (
                <li key={row.model === null ? "other" : "model:" + row.model}>
                  <div>
                    <span>{row.model ?? "Other models"}</span>
                    <strong title={estimatedUsd(row.estimated_cost_usd)}>
                      {headlineUsd(row.estimated_cost_usd)}
                    </strong>
                  </div>
                  {share !== null && (
                    <div className="cost-share">
                      <div aria-hidden="true">
                        <i style={{ width: share + "%" }} />
                      </div>
                      <small>
                        {share > 0 && share < 1 ? "<1" : Math.round(share)}%
                      </small>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      ) : (
        <p>
          {summary.statistics.llm_sessions
            ? "A session needs recorded usage and model rates to have a USD estimate."
            : "No model sessions in this period."}
        </p>
      )}
      {missing > 0 && (
        <p className="rate-note">
          {number(missing)} {missing === 1 ? "session has" : "sessions have"} no
          USD estimate. Rates apply to new sessions.
        </p>
      )}
      {missing > 0 && (
        <Link className="rates-link" to="/governance#model-rates">
          View model rates <ArrowUpRight size={14} />
        </Link>
      )}
    </section>
  );
}

export function Coverage({ stats }: { stats: Metrics }) {
  const values = [
    ["Sessions without provider usage", number(stats.unavailable_sessions)],
    [
      "Successful queries without scan estimates",
      number(stats.query_estimate_missing_calls),
    ],
    ["Tool calls without a final outcome", number(stats.incomplete_calls)],
    [
      "Estimated query input",
      stats.available_query_estimated_input_gb == null
        ? "Unavailable"
        : number(stats.available_query_estimated_input_gb) + " GB",
    ],
  ];
  return (
    <details className="coverage-details">
      <summary>Reporting coverage</summary>
      <dl>
        {values.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <p>
        Scan estimates cover successful queries and previews with recorded
        estimates.
      </p>
    </details>
  );
}

export function CostOverview({
  filters,
  daily,
  summary,
  turnPage,
}: {
  filters: Filters;
  daily: UseQueryResult<PageResult>;
  summary: UseQueryResult<Summary>;
  turnPage: (offset: number) => void;
}) {
  const board = useRef<HTMLElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [displayError, setDisplayError] = useState(false);
  useEffect(() => {
    const change = () =>
      setExpanded(document.fullscreenElement === board.current);
    document.addEventListener("fullscreenchange", change);
    return () => document.removeEventListener("fullscreenchange", change);
  }, []);
  const applied = Object.entries(filters).filter(
    ([key, v]) => v && !["all_time", "from", "to"].includes(key),
  );
  const period = summary.data?.period ?? daily.data?.period;
  const range = period
    ? period.all_time
      ? "All recorded history"
      : [
          period.from && displayDay(period.from, true),
          period.to &&
            displayDay(new Date(Date.parse(period.to) - 1).toISOString(), true),
        ]
          .filter(Boolean)
          .join(" to ")
    : "";
  return (
    <section ref={board} className="overview-board" aria-label="Cost overview">
      <div className="overview-head">
        <div>
          <h2>
            {applied.length
              ? "Filtered daily usage"
              : "Organization-wide daily usage"}
          </h2>
          <p>
            {[
              range && range + " · UTC",
              ...applied.map(([k, v]) => k.replaceAll("_", " ") + " · " + v),
            ]
              .filter(Boolean)
              .join(" / ")}
          </p>
        </div>
        {document.fullscreenEnabled && (
          <button
            aria-label={expanded ? "Exit expanded report" : "Expand report"}
            title={expanded ? "Exit expanded report" : "Expand report"}
            aria-pressed={expanded}
            onClick={async () => {
              try {
                setDisplayError(false);
                if (expanded) await document.exitFullscreen();
                else await board.current?.requestFullscreen();
              } catch {
                setDisplayError(true);
              }
            }}
          >
            {expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
            <span className="expand-text">
              {expanded ? "Exit expanded view" : "Expand"}
            </span>
          </button>
        )}
      </div>
      {displayError && (
        <Notice>Expanded view is unavailable in this browser.</Notice>
      )}
      {summary.isPending ? (
        <Loading label="Loading totals" />
      ) : summary.isError ? (
        <Failure error={summary.error} retry={() => void summary.refetch()} />
      ) : (
        <SpendMetrics stats={summary.data.statistics} />
      )}
      <div className="overview-charts">
        {daily.isPending ? (
          <Loading label="Loading daily usage" />
        ) : daily.isError ? (
          <Failure error={daily.error} retry={() => void daily.refetch()} />
        ) : (
          <Trend
            data={daily.data}
            unpricedPeriod={
              summary.data ? !summary.data.monetary_costs_available : undefined
            }
            turnPage={turnPage}
          />
        )}
        {summary.data && <ModelSpend summary={summary.data} />}
      </div>
    </section>
  );
}
export function Trend({
  data,
  unpricedPeriod,
  turnPage,
}: {
  data: PageResult;
  unpricedPeriod?: boolean;
  turnPage?: (offset: number) => void;
}) {
  const labels = {
    estimated_cost_usd: "Estimated spend (USD)",
    total_tokens: "Tokens",
    activity_calls: "Tool calls",
  };
  const [selection, setMetric] = useState<keyof typeof labels | null>(null);
  const unpriced =
    unpricedPeriod ?? data.items.every((row) => row.estimated_cost_usd == null);
  const activityMetric = data.items.some((row) => (row.total_tokens ?? 0) > 0)
    ? "total_tokens"
    : data.items.some((row) => (row.activity_calls ?? 0) > 0)
      ? "activity_calls"
      : "estimated_cost_usd";
  const metric =
    selection ?? (unpriced ? activityMetric : "estimated_cost_usd");
  const firstDay = data.items[0]?.date,
    lastDay = data.items.at(-1)?.date;
  const dates =
    firstDay && lastDay
      ? new Intl.DateTimeFormat("en", {
          month: "short",
          day: "numeric",
          year: "numeric",
          timeZone: "UTC",
        }).formatRange(new Date(firstDay), new Date(lastDay))
      : "";
  const metricId = useId();
  const spend = metric === "estimated_cost_usd";
  const rows = data.items.map((row) => ({
    date: row.date,
    value: row[metric] == null ? null : Number(row[metric]),
  }));
  const formatValue = (value: number) =>
    spend ? estimatedUsd(String(value)) : number(value);
  return (
    <div className="trend">
      <div className="trend-heading">
        <div>
          <h3>
            {spend
              ? "Estimated daily spend"
              : "Daily " + labels[metric].toLowerCase()}
          </h3>
          <p>
            {dates ? dates + " · UTC" : "No recorded days"}
            {data.total > data.limit && (
              <span className="chart-day-count">
                Recorded days {data.offset + 1}-
                {data.offset + data.items.length} of {number(data.total)}
              </span>
            )}
          </p>
        </div>
        <label className="sr-only" htmlFor={metricId}>
          Trend metric
        </label>
        <select
          id={metricId}
          value={metric}
          onChange={(e) => setMetric(e.target.value as typeof metric)}
        >
          <option value="estimated_cost_usd">Estimated spend (USD)</option>
          <option value="total_tokens">Tokens</option>
          <option value="activity_calls">Tool calls</option>
        </select>
      </div>
      {data.items.length && spend && rows.every((row) => row.value == null) ? (
        <div className="spend-empty">
          <p>No USD estimates for these days.</p>
          <button onClick={() => setMetric("total_tokens")}>
            Show recorded tokens
          </button>
        </div>
      ) : data.items.length ? (
        <>
          <div
            className="chart"
            role="group"
            aria-label={`Daily ${labels[metric]} bar chart. Exact values in the daily table.`}
          >
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={rows}
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
                  tickFormatter={(value) => displayDay(String(value))}
                  tickLine={false}
                  axisLine={false}
                  minTickGap={32}
                  fontSize={12}
                />
                <YAxis
                  tickFormatter={(value) =>
                    spend
                      ? formatValue(Number(value))
                      : shortNumber(Number(value))
                  }
                  tickLine={false}
                  axisLine={false}
                  width={spend ? 65 : 53}
                  fontSize={12}
                />
                <Tooltip
                  labelFormatter={(value) => displayDay(String(value), true)}
                  formatter={(value) => formatValue(Number(value))}
                />
                <Bar
                  dataKey="value"
                  name={labels[metric]}
                  fill="#365fe5"
                  radius={[3, 3, 0, 0]}
                  maxBarSize={48}
                  isAnimationActive={false}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="chart-caption">
            <p className="chart-note">
              {spend
                ? "Each bar totals the priced sessions for one UTC day. Days without a USD estimate have no bar."
                : "Each bar shows one recorded UTC day."}
            </p>
            {turnPage && data.total > data.limit && (
              <div className="chart-paging">
                <button
                  aria-label="Earlier chart days"
                  disabled={!data.offset}
                  onClick={() =>
                    turnPage(Math.max(0, data.offset - data.limit))
                  }
                >
                  <ChevronLeft size={16} />
                </button>
                <button
                  aria-label="Later chart days"
                  disabled={
                    data.offset + data.limit >= data.total ||
                    data.offset + data.limit > 100000
                  }
                  onClick={() => turnPage(data.offset + data.limit)}
                >
                  <ChevronRight size={16} />
                </button>
              </div>
            )}
          </div>
        </>
      ) : (
        <Empty title="No daily activity">
          Try a different period or remove filters.
        </Empty>
      )}
    </div>
  );
}
