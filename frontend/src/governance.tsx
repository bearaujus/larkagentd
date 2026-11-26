import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, client, write } from "./api";
import { Failure, Loading, Notice, Panel, useAction } from "./components";
import { PageIntro } from "./policies";
import { QuotaHistory } from "./quota-history";
import type { Session } from "./types";

const priceFields = [
  ["input_tokens", "Uncached input"],
  ["output_tokens", "Output"],
  ["cache_read_tokens", "Cache reads"],
  ["cache_write_tokens", "Cache writes"],
] as const;

type Rates = Record<(typeof priceFields)[number][0], string | null>;
type Cadence = "monthly" | "weekly";
type Flex = {
  email: string;
  enabled: boolean;
  active_limit: number | null;
  pending_limit: number | null;
  used: number;
};
type Tier = "free" | "flex";
type Pool = {
  limit: number | null;
  recurring_limit: number | null;
  adjusted: boolean;
  pending: { limit: number | null; effective_on: string } | null;
  used: number;
  unavailable_sessions: number;
};
const poolTiers = [
  ["free", "FreeTier"],
  ["flex", "FlexTier"],
] as const;
type GovernanceData = {
  model: string | null;
  effective_model: string;
  effort: string | null;
  models: { id: string; efforts: string[] }[];
  prices: Record<string, Rates>;
  pools: Record<Tier, Pool>;
  quota: {
    cadence: Cadence;
    free_limit: number | null;
    cycle_start: string;
    resets_on: string;
    pending: {
      cadence: Cadence;
      free_limit: number | null;
      effective_on: string;
    } | null;
  };
  flex: Flex[];
};
const allowance = (value: number | null) =>
  value === null ? "Unlimited" : value.toLocaleString();
const reset = (date: string) => `${date} at 00:00 UTC`;
const inputLimit = (value: number | null) =>
  value === null ? "" : String(value);
const maxTokens = Number.MAX_SAFE_INTEGER;
type Save = (
  path: string,
  body: object,
  success: string,
  method?: "PATCH" | "PUT",
) => void;
type Controls = { canEdit: boolean; busy: boolean; save: Save };

function ModelSettings({
  data,
  canEdit,
  busy,
  save,
}: Controls & { data: GovernanceData }) {
  const [model, setModel] = useState(data.model ?? "");
  const [effort, setEffort] = useState(data.effort ?? "");
  const efforts =
    data.models.find((option) => option.id === model)?.efforts ?? [];
  const active = data.effective_model || "Hermes default";
  return (
    <Panel
      title="Global model"
      description={`In use for new turns: ${active}${data.effort ? ` (${data.effort} effort)` : ""}.`}
    >
      <form
        className="panel-body"
        onSubmit={(e) => {
          e.preventDefault();
          save(
            "/model",
            { model: model || null, effort: effort || null },
            "Model settings saved.",
          );
        }}
      >
        <fieldset className="governance-fields" disabled={!canEdit || busy}>
          <label>
            Model
            <select
              value={model}
              onChange={(e) => {
                setModel(e.target.value);
                setEffort("");
              }}
            >
              <option value="">Use the Hermes default</option>
              {data.models.map(({ id }) => (
                <option key={id}>{id}</option>
              ))}
              {model && !data.models.some(({ id }) => id === model) && (
                <option disabled>{model}</option>
              )}
            </select>
          </label>
          <label>
            Reasoning effort
            <select
              value={effort}
              disabled={!efforts.length}
              onChange={(e) => setEffort(e.target.value)}
            >
              <option value="">
                {efforts.length
                  ? "Model default"
                  : "No verified effort options"}
              </option>
              {efforts.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
        </fieldset>
        {canEdit && (
          <div className="save-row">
            <button
              className="primary"
              disabled={
                busy ||
                (model === (data.model ?? "") && effort === (data.effort ?? ""))
              }
            >
              Save model
            </button>
          </div>
        )}
      </form>
    </Panel>
  );
}

function RateForm({
  model,
  rates,
  canEdit,
  busy,
  save,
}: Controls & { model: string; rates: Rates | undefined }) {
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        const fields = new FormData(e.currentTarget);
        const values = Object.fromEntries(
          priceFields.map(([key]) => [
            key,
            String(fields.get(key)).trim() || null,
          ]),
        );
        save(
          `/prices/${encodeURIComponent(model)}`,
          values,
          "Model rates saved.",
          "PUT",
        );
      }}
    >
      <fieldset className="governance-fields" disabled={!canEdit || busy}>
        {priceFields.map(([key, label]) => (
          <label key={key}>
            {label}
            <input
              name={key}
              inputMode="decimal"
              pattern="[0-9]{1,8}([.][0-9]{1,8})?"
              defaultValue={rates?.[key] ?? ""}
              placeholder="Unpriced"
            />
          </label>
        ))}
      </fieldset>
      {canEdit && (
        <div className="save-row">
          <button className="primary" disabled={busy}>
            Save rates
          </button>
        </div>
      )}
    </form>
  );
}

function ModelRates({
  data,
  ...controls
}: Controls & { data: GovernanceData }) {
  const [model, setModel] = useState(data.model || data.models[0]?.id || "");
  return (
    <Panel
      title="Model rates"
      description="USD per million tokens. Rate changes apply to newly started turns. Leave a rate blank when pricing is unavailable."
    >
      <div className="panel-body">
        {data.models.length ? (
          <>
            <label>
              Model to price
              <select
                value={model}
                disabled={controls.busy}
                onChange={(e) => setModel(e.target.value)}
              >
                {data.models.map(({ id }) => (
                  <option key={id}>{id}</option>
                ))}
              </select>
            </label>
            <RateForm
              key={JSON.stringify([model, data.prices[model]])}
              model={model}
              rates={data.prices[model]}
              {...controls}
            />
          </>
        ) : (
          <p className="muted">No provider models are configured.</p>
        )}
      </div>
    </Panel>
  );
}

function TokenLimitForm({
  label,
  button,
  limit,
  existingEdit,
  busy,
  onSave,
}: {
  label: string;
  button: string;
  limit: number | null;
  existingEdit?: { limit: number | null };
  busy: boolean;
  onSave: (limit: number | null) => void;
}) {
  const [draft, setDraft] = useState(
    inputLimit(existingEdit ? existingEdit.limit : limit),
  );
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSave(draft === "" ? null : Number(draft));
      }}
    >
      <label>
        {label}
        <input
          type="number"
          min={0}
          max={maxTokens}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Unlimited"
          disabled={busy}
        />
      </label>
      <div className="save-row">
        <button
          className="primary"
          disabled={busy || (!existingEdit && draft === inputLimit(limit))}
        >
          {button}
        </button>
      </div>
    </form>
  );
}

function SharedPools({
  pools,
  resetsOn,
  canEdit,
  busy,
  save,
}: Controls & { pools: GovernanceData["pools"]; resetsOn: string }) {
  return (
    <Panel
      title="Shared token pools"
      description="Each tier shares one pool. Every user also has a personal allowance. Saved limits take effect immediately and carry forward after usage resets."
    >
      <div className="panel-body">
        {poolTiers.map(([tier, name]) => {
          const pool = pools[tier];
          const remaining =
            pool.limit === null ? null : Math.max(pool.limit - pool.used, 0);
          return (
            <section
              className="governance-pool"
              aria-label={`${name} pool`}
              key={tier}
            >
              <h3>{name} pool</h3>
              <p>
                <strong>{pool.used.toLocaleString()}</strong> tokens used
                {" / "}
                {allowance(pool.limit)}
                {remaining !== null &&
                  ` · ${remaining.toLocaleString()} remaining`}
              </p>
              <p className="muted">Usage resets {reset(resetsOn)}.</p>
              {pool.adjusted && (
                <p className="muted">
                  A previous temporary limit is active. The saved limit is{" "}
                  {allowance(pool.recurring_limit)}.
                </p>
              )}
              {pool.pending && (
                <p>
                  Previously scheduled limit {allowance(pool.pending.limit)}{" "}
                  from {reset(pool.pending.effective_on)}.
                </p>
              )}
              {pool.unavailable_sessions > 0 && (
                <p className="muted">
                  Usage unavailable for{" "}
                  {pool.unavailable_sessions.toLocaleString()} model session(s).
                </p>
              )}
              {canEdit && (
                <TokenLimitForm
                  key={JSON.stringify([
                    pool.limit,
                    pool.pending,
                    pool.adjusted,
                  ])}
                  label={`${name} pool token limit`}
                  button="Save pool limit"
                  limit={pool.limit}
                  existingEdit={
                    pool.pending ??
                    (pool.adjusted ? { limit: pool.limit } : undefined)
                  }
                  busy={busy}
                  onSave={(limit) =>
                    save(
                      `/pools/${tier}`,
                      { limit },
                      `${name} pool limit saved.`,
                    )
                  }
                />
              )}
            </section>
          );
        })}
      </div>
    </Panel>
  );
}

function QuotaSettings({
  quota,
  canEdit,
  busy,
  save,
}: Controls & { quota: GovernanceData["quota"] }) {
  const pending = quota.pending;
  const nextCadence =
    pending && pending.cadence !== quota.cadence ? pending : null;
  const nextLimit =
    pending && pending.free_limit !== quota.free_limit ? pending : null;
  const savedCadence = nextCadence?.cadence ?? quota.cadence;
  const [cadence, setCadence] = useState(savedCadence);
  useEffect(() => setCadence(savedCadence), [savedCadence]);
  return (
    <Panel
      title="Personal FreeTier limit and reset schedule"
      description="The FreeTier per-user limit takes effect immediately. Reset frequency changes start after the current cycle ends."
    >
      <div className="panel-body">
        <p className="muted">
          Active limit {allowance(quota.free_limit)}
          {quota.free_limit !== null && " tokens per user"}. Current schedule{" "}
          {quota.cadence}. Next reset {reset(quota.resets_on)}.
        </p>
        {nextLimit && (
          <Notice>
            Previously scheduled limit {allowance(nextLimit.free_limit)} from{" "}
            {reset(nextLimit.effective_on)}. Save the limit to apply it now.
          </Notice>
        )}
        {nextCadence && (
          <Notice>
            Reset frequency changes to {nextCadence.cadence} on{" "}
            {reset(nextCadence.effective_on)}.
          </Notice>
        )}
        {canEdit && (
          <div className="governance-fields">
            <TokenLimitForm
              key={JSON.stringify([quota.free_limit, nextLimit?.free_limit])}
              label="FreeTier token limit"
              button="Save FreeTier limit"
              limit={quota.free_limit}
              existingEdit={
                nextLimit ? { limit: nextLimit.free_limit } : undefined
              }
              busy={busy}
              onSave={(free_limit) =>
                save("/quota", { free_limit }, "FreeTier limit saved.")
              }
            />
            <form
              onSubmit={(e) => {
                e.preventDefault();
                save("/quota", { cadence }, "Reset schedule saved.");
              }}
            >
              <label>
                Reset frequency
                <select
                  value={cadence}
                  disabled={busy}
                  onChange={(e) => setCadence(e.target.value as Cadence)}
                >
                  <option value="monthly">Monthly · first day</option>
                  <option value="weekly">Weekly · Monday</option>
                </select>
              </label>
              <div className="save-row">
                <button
                  className="primary"
                  disabled={busy || cadence === savedCadence}
                >
                  Save reset frequency
                </button>
              </div>
            </form>
          </div>
        )}
      </div>
    </Panel>
  );
}

const flexPath = (email: string) => `/flex/${encodeURIComponent(email)}`;
function FlexAssignments({
  data,
  canEdit,
  busy,
  save,
}: Controls & { data: GovernanceData }) {
  const [email, setEmail] = useState("");
  const [limit, setLimit] = useState("");
  const entryExists = data.flex.some(
    (row) => row.email === email.trim().toLowerCase(),
  );
  return (
    <Panel
      title="FlexTier assignments"
      description="Use the person’s Lark email. Enabled assignments and saved limits take effect immediately."
    >
      {canEdit && (
        <form
          className="panel-body"
          onSubmit={(e) => {
            e.preventDefault();
            save(
              flexPath(email.trim()),
              { token_limit: Number(limit) },
              "FlexTier assignment saved.",
            );
          }}
        >
          <fieldset className="governance-fields" disabled={busy}>
            <label>
              Lark email
              <input
                type="email"
                maxLength={254}
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
            <label>
              New FlexTier token limit
              <input
                type="number"
                min={0}
                max={maxTokens}
                required
                value={limit}
                onChange={(e) => setLimit(e.target.value)}
              />
            </label>
          </fieldset>
          {entryExists && (
            <p className="muted">
              Use the existing row to edit this assignment.
            </p>
          )}
          <div className="save-row">
            <button className="primary" disabled={busy || entryExists}>
              Add FlexTier assignment
            </button>
          </div>
        </form>
      )}
      {data.flex.length ? (
        <div
          className="table-scroll"
          role="region"
          aria-label="FlexTier assignment table"
          tabIndex={0}
        >
          <table>
            <caption className="sr-only">
              FlexTier assignments and usage in the current cycle
            </caption>
            <thead>
              <tr>
                {[
                  "Email",
                  "Enabled",
                  "Active limit",
                  "Tokens used",
                  canEdit ? "Edit limit" : "Scheduled limit",
                ].map((label) => (
                  <th key={label} scope="col">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.flex.map((row) => (
                <tr key={row.email}>
                  <td>{row.email}</td>
                  <td>
                    <input
                      aria-label={`Enable FlexTier for ${row.email}`}
                      type="checkbox"
                      checked={row.enabled}
                      disabled={!canEdit || busy}
                      onChange={(e) =>
                        save(
                          flexPath(row.email),
                          { enabled: e.target.checked },
                          "FlexTier assignment updated.",
                        )
                      }
                    />
                  </td>
                  <td>
                    {row.active_limit?.toLocaleString() ??
                      "Pending first reset"}
                  </td>
                  <td>{row.used.toLocaleString()}</td>
                  <td>
                    {canEdit ? (
                      <form
                        className="governance-row-form"
                        key={JSON.stringify([
                          row.active_limit,
                          row.pending_limit,
                        ])}
                        onSubmit={(e) => {
                          e.preventDefault();
                          const fields = new FormData(e.currentTarget);
                          save(
                            flexPath(row.email),
                            { token_limit: Number(fields.get("limit")) },
                            "FlexTier limit updated.",
                          );
                        }}
                      >
                        <input
                          aria-label={`FlexTier limit for ${row.email}`}
                          name="limit"
                          type="number"
                          min={0}
                          max={maxTokens}
                          required
                          defaultValue={
                            row.pending_limit ?? row.active_limit ?? ""
                          }
                          disabled={busy}
                        />
                        <button disabled={busy}>Save limit</button>
                      </form>
                    ) : (
                      (row.pending_limit?.toLocaleString() ?? "No change")
                    )}
                    {row.pending_limit !== null && (
                      <small className="muted">
                        Effective {reset(data.quota.resets_on)}
                      </small>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="panel-body muted">No FlexTier assignments.</p>
      )}
    </Panel>
  );
}

function BotAccess() {
  const path = "/bot-access";
  const query = useQuery({
    queryKey: ["bot-access"],
    queryFn: ({ signal }) => api<{ paused: boolean }>(path, { signal }),
  });
  const action = useAction();
  return (
    <Panel
      title="Group assistant"
      description="Pause new group requests and account setup across the workspace. Dashboard login stays available."
    >
      <div className="panel-body bot-access">
        {query.isPending ? (
          <Loading />
        ) : query.isError ? (
          <Failure error={query.error} retry={() => void query.refetch()} />
        ) : (
          <>
            <p role="status">
              {query.data.paused ? "Paused" : "Accepting requests"}
            </p>
            <button
              type="button"
              disabled={action.busy}
              onClick={() =>
                void action.run(
                  async () => {
                    const next = !query.data.paused;
                    const result = await write<{ paused: boolean }>(
                      path,
                      "PATCH",
                      { paused: next },
                    );
                    client.setQueryData(["bot-access"], result);
                  },
                  query.data.paused
                    ? "Group requests resumed."
                    : "New group requests paused.",
                )
              }
            >
              {query.data.paused ? "Resume requests" : "Pause new requests"}
            </button>
          </>
        )}
        {action.feedback}
      </div>
    </Panel>
  );
}

export function Governance({ session }: { session: Session }) {
  const path = "/governance";
  const query = useQuery({
    queryKey: ["governance"],
    queryFn: ({ signal }) => api<GovernanceData>(path, { signal }),
  });
  const action = useAction();
  if (query.isPending) return <Loading />;
  if (query.isError)
    return <Failure error={query.error} retry={() => void query.refetch()} />;
  const data = query.data;
  const controls: Controls = {
    canEdit: session.role === "admin",
    busy: action.busy,
    save: (part, body, success, method = "PATCH") => {
      void action.run(async () => {
        await client.cancelQueries({ queryKey: ["governance"] });
        const result = await write<GovernanceData>(path + part, method, body);
        client.setQueryData(["governance"], result);
        void client.invalidateQueries({ queryKey: ["governance-history"] });
      }, success);
    },
  };
  return (
    <>
      <PageIntro
        title="Governance"
        description="Manage models, token allowances, and estimated spend."
      />
      <BotAccess />
      {!controls.canEdit && <Notice>Admins can change these settings.</Notice>}
      {action.feedback}
      <ModelSettings
        key={JSON.stringify([data.model, data.effort])}
        data={data}
        {...controls}
      />
      <ModelRates data={data} {...controls} />
      <SharedPools
        pools={data.pools}
        resetsOn={data.quota.resets_on}
        {...controls}
      />
      <QuotaSettings quota={data.quota} {...controls} />
      <FlexAssignments data={data} {...controls} />
      <QuotaHistory />
    </>
  );
}
