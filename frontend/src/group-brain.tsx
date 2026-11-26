import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Dialog } from "radix-ui";
import { api, client, message, write } from "./api";
import { Failure, Loading, Panel, useAction } from "./components";
import type {
  GroupKnowledge,
  GroupKnowledgeAuditEvent,
  GroupKnowledgeCandidate,
  GroupKnowledgeJob,
  Session,
} from "./types";

type Draft = {
  configured_enabled: boolean;
  recall: boolean;
  memory: boolean;
  learning_mode: GroupKnowledge["learning_mode"];
  history_window_days: number;
  notice_ref: string;
  policy_ref: string;
};
const reasonWords: Record<string, string> = {
  global_disabled: "Disabled globally",
  group_disabled: "Disabled for this group",
  not_registered: "Group is not registered",
  paused: "Paused by an administrator",
  purging: "Purge is in progress",
  missing_group_notice: "Group notice is missing",
  missing_scope: "Required access scope is missing",
  visibility_unverified: "Group visibility is not verified",
  embedding_unavailable: "Embedding service is unavailable",
  index_unavailable: "Search index is unavailable",
  revalidation_pending: "Source access is being revalidated",
};
const initialDraft = (value: GroupKnowledge): Draft => ({
  configured_enabled: value.configured_enabled,
  recall: value.capabilities.recall,
  memory: value.capabilities.memory,
  learning_mode: value.learning_mode,
  history_window_days: value.history_window_days,
  notice_ref: value.notice_ref ?? "",
  policy_ref: value.policy_ref ?? "",
});
const fieldLabels = [
  ["recall", "Conversation recall"],
  ["memory", "Collective memory"],
] as const;
const stateLabel = (state: GroupKnowledge["effective_state"]) =>
  ({
    disabled: "disabled",
    enabled: "active",
    degraded: "degraded",
    paused: "paused",
    purging: "purging",
    setup_blocked: "setup blocked",
  })[state];
const display = (value: string | null) => value ?? "Not available";
export function GroupBrain({ groupId }: { groupId: string }) {
  const enableRef = useRef<HTMLInputElement>(null);
  const base = `/groups/${encodeURIComponent(groupId)}/knowledge`;
  const knowledge = useQuery({
    queryKey: ["group-knowledge", groupId],
    queryFn: ({ signal }) => api<GroupKnowledge>(base, { signal }),
  });
  const session = useQuery({
    queryKey: ["auth-session"],
    queryFn: ({ signal }) => api<Session>("/auth/session", { signal }),
  });
  const jobs = useQuery({
    queryKey: ["group-knowledge-jobs", groupId],
    queryFn: ({ signal }) =>
      api<{ jobs: GroupKnowledgeJob[] }>(`${base}/jobs`, { signal }),
  });
  const audit = useQuery({
    queryKey: ["group-knowledge-audit", groupId],
    queryFn: ({ signal }) =>
      api<{ events: GroupKnowledgeAuditEvent[] }>(`${base}/audit`, { signal }),
  });
  const reviewEnabled = knowledge.data?.configured_enabled === true;
  const candidates = useQuery({
    queryKey: ["group-knowledge-candidates", groupId],
    queryFn: ({ signal }) =>
      api<{ candidates: GroupKnowledgeCandidate[] }>(`${base}/candidates`, {
        signal,
      }),
    enabled: reviewEnabled,
  });
  const [draft, setDraft] = useState<Draft | null>(null);
  const [enableOpen, setEnableOpen] = useState(false);
  const [purgeOpen, setPurgeOpen] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [confirmId, setConfirmId] = useState("");
  const [purgeError, setPurgeError] = useState("");
  const [auditEvents, setAuditEvents] = useState<GroupKnowledgeAuditEvent[]>(
    [],
  );
  const [auditHasMore, setAuditHasMore] = useState(true);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState("");
  const [noteAcknowledgements, setNoteAcknowledgements] = useState<
    Record<string, string>
  >({});
  const [rejectReasons, setRejectReasons] = useState<Record<string, string>>(
    {},
  );
  const action = useAction();

  useEffect(() => {
    setDraft(null);
    setAuditEvents([]);
    setAuditHasMore(true);
    setAuditError("");
    setNoteAcknowledgements({});
    setRejectReasons({});
  }, [groupId]);
  useEffect(() => {
    if (knowledge.data && !draft) setDraft(initialDraft(knowledge.data));
  }, [knowledge.data, draft]);
  useEffect(() => {
    if (audit.data) {
      setAuditEvents(audit.data.events);
      setAuditHasMore(audit.data.events.length > 0);
    }
  }, [audit.data]);

  if (knowledge.isPending || session.isPending) {
    return (
      <Panel title="Learning settings">
        <Loading label="Loading learning settings" />
      </Panel>
    );
  }
  if (knowledge.isError) {
    return (
      <Panel title="Learning settings">
        <Failure
          error={knowledge.error}
          retry={() => void knowledge.refetch()}
        />
      </Panel>
    );
  }
  if (session.isError) {
    return (
      <Panel title="Learning settings">
        <Failure error={session.error} retry={() => void session.refetch()} />
      </Panel>
    );
  }
  if (!draft)
    return (
      <Panel title="Learning settings">
        <Loading label="Preparing learning settings" />
      </Panel>
    );

  const value = knowledge.data;
  const canEdit = session.data.role === "admin";
  const busy = action.busy;
  const locked = busy || !canEdit || value.effective_state === "purging";
  const isDirty =
    draft.configured_enabled !== value.configured_enabled ||
    fieldLabels.some(([key]) => draft[key] !== value.capabilities[key]) ||
    draft.learning_mode !== value.learning_mode ||
    draft.history_window_days !== value.history_window_days ||
    draft.notice_ref !== (value.notice_ref ?? "") ||
    draft.policy_ref !== (value.policy_ref ?? "");
  const windowInvalid =
    !Number.isInteger(draft.history_window_days) ||
    draft.history_window_days < 1 ||
    draft.history_window_days > value.max_history_days;
  const updateDraft = (change: Partial<Draft>) =>
    setDraft((current) => (current ? { ...current, ...change } : current));
  const refreshAfter = async (saved: GroupKnowledge) => {
    client.setQueryData(["group-knowledge", groupId], saved);
    updateDraft(initialDraft(saved));
    await Promise.all([
      client.invalidateQueries({ queryKey: [base, "learned"] }),
      client.invalidateQueries({ queryKey: ["group-knowledge-jobs", groupId] }),
      client.invalidateQueries({
        queryKey: ["group-knowledge-audit", groupId],
      }),
    ]);
  };
  const save = async () => {
    const old = value;
    const changes: Record<string, unknown> = {};
    if (draft.configured_enabled !== old.configured_enabled)
      changes.enabled = draft.configured_enabled;
    for (const [key] of fieldLabels) {
      if (draft[key] !== old.capabilities[key])
        changes[`${key}_enabled`] = draft[key];
    }
    if (draft.learning_mode !== old.learning_mode)
      changes.learning_mode = draft.learning_mode;
    if (draft.history_window_days !== old.history_window_days)
      changes.history_window_days = draft.history_window_days;
    if (draft.notice_ref !== (old.notice_ref ?? ""))
      changes.notice_ref = draft.notice_ref;
    if (draft.policy_ref !== (old.policy_ref ?? ""))
      changes.policy_ref = draft.policy_ref;
    if (!Object.keys(changes).length) return;
    if (draft.configured_enabled && !old.configured_enabled) {
      changes.notice_ref = draft.notice_ref;
      changes.policy_ref = draft.policy_ref;
      changes.acknowledge_group_sharing = acknowledged;
    }
    let conflict = false;
    const success = await action.run(async () => {
      try {
        const saved = await write<GroupKnowledge>(base, "PATCH", {
          version: old.version,
          ...changes,
        });
        await refreshAfter(saved);
      } catch (error) {
        conflict =
          error instanceof Error && error.message === "version_conflict";
        throw error;
      }
    }, "Learning settings saved.");
    if (!success && conflict) {
      // Reload the server baseline but deliberately keep the local draft intact.
      await knowledge.refetch();
    }
  };
  const transition = (name: "pause" | "resume") =>
    action.run(async () => {
      const saved = await write<GroupKnowledge>(`${base}/${name}`, "POST", {
        version: value.version,
      });
      await refreshAfter(saved);
    }, `Learning ${name}d.`);
  const reviewAction = (path: string, body: object, success: string) =>
    action.run(async () => {
      await write(`${base}/${path}`, "POST", body);
      await Promise.all([candidates.refetch(), knowledge.refetch()]);
    }, success);
  const purge = async () => {
    setPurgeError("");
    await action.run(async () => {
      try {
        const saved = await write<GroupKnowledge>(`${base}/purge`, "POST", {
          version: value.version,
          confirm: groupId,
        });
        await refreshAfter(saved);
        setPurgeOpen(false);
      } catch (error) {
        setPurgeError(message(error));
        throw error;
      }
    }, "Purge request accepted.");
  };
  const showHistoryMore = async () => {
    if (auditEvents.length === 0 || auditLoading) return;
    const numericId = Number(auditEvents[auditEvents.length - 1].id);
    if (!Number.isSafeInteger(numericId)) {
      setAuditError("This audit page has no usable continuation cursor.");
      return;
    }
    setAuditLoading(true);
    setAuditError("");
    try {
      const result = await api<{ events: GroupKnowledgeAuditEvent[] }>(
        `${base}/audit?before=${numericId}`,
      );
      setAuditEvents((current) => [...current, ...result.events]);
      setAuditHasMore(result.events.length > 0);
    } catch (error) {
      setAuditError(message(error));
    } finally {
      setAuditLoading(false);
    }
  };
  const canLoadMore = auditHasMore && auditEvents.length > 0;
  const reasonText = value.reason_codes
    .map((code) => reasonWords[code] ?? code.replaceAll("_", " "))
    .join(", ");

  return (
    <Panel
      title="Learning settings"
      description="Group-level memory and knowledge settings."
    >
      <form
        className="panel-body group-brain"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        {!canEdit && (
          <p className="group-brain-readonly">
            You can view learning settings. Workspace admins can change them.
          </p>
        )}
        <p className="group-brain-status" aria-live="polite">
          Configured:{" "}
          <strong>{value.configured_enabled ? "enabled" : "disabled"}</strong>
          <span>
            Effective:{" "}
            <strong>
              {value.reason_codes.includes("global_disabled")
                ? "not active"
                : stateLabel(value.effective_state)}
            </strong>
          </span>
          {reasonText && <span>Reasons: {reasonText}</span>}
        </p>
        <label className="group-brain-toggle">
          <input
            ref={enableRef}
            type="checkbox"
            checked={draft.configured_enabled}
            disabled={locked}
            onChange={(event) => {
              if (event.target.checked) {
                setAcknowledged(false);
                setEnableOpen(true);
              } else updateDraft({ configured_enabled: false });
            }}
          />
          Enable group learning
        </label>
        {draft.configured_enabled && (
          <>
            <fieldset className="group-brain-capabilities" disabled={locked}>
              <legend>Capabilities</legend>
              {fieldLabels.map(([key, label]) => (
                <label key={key}>
                  <input
                    type="checkbox"
                    checked={draft[key]}
                    onChange={(event) =>
                      updateDraft({ [key]: event.target.checked })
                    }
                  />
                  {label}
                </label>
              ))}
            </fieldset>
            <fieldset className="group-brain-modes" disabled={locked}>
              <legend>Learning mode</legend>
              <label>
                <input
                  type="radio"
                  name={`brain-mode-${groupId}`}
                  value="explicit"
                  checked={draft.learning_mode === "explicit"}
                  onChange={() => updateDraft({ learning_mode: "explicit" })}
                />
                Explicit
              </label>
              <label>
                <input
                  type="radio"
                  name={`brain-mode-${groupId}`}
                  value="interaction"
                  checked={draft.learning_mode === "interaction"}
                  onChange={() => updateDraft({ learning_mode: "interaction" })}
                />
                Interaction
              </label>
              <label className="group-brain-passive">
                <input
                  type="radio"
                  disabled
                  checked={value.learning_mode === "passive"}
                  readOnly
                />
                Passive <span>Needs a separate approval</span>
              </label>
            </fieldset>
            <label className="group-brain-history">
              History window (days)
              <input
                type="number"
                min={1}
                max={value.max_history_days}
                step={1}
                value={draft.history_window_days}
                disabled={locked}
                onChange={(event) =>
                  updateDraft({
                    history_window_days: Number(event.target.value),
                  })
                }
              />
              <span>1 to {value.max_history_days} days</span>
            </label>
          </>
        )}
        <p className="group-brain-lock">
          New facts can become active after a source check. Proposed corrections
          need review.
        </p>
        <dl className="group-brain-facts">
          <div>
            <dt>Discussions</dt>
            <dd>{value.counts.discussions}</dd>
          </div>
          <div>
            <dt>Memories</dt>
            <dd>{value.counts.memories}</dd>
          </div>
          <div></div>
          <div>
            <dt>Pending review</dt>
            <dd>{value.counts.pending_review}</dd>
          </div>
          <div>
            <dt>Coverage</dt>
            <dd>
              {display(value.coverage.from)} to {display(value.coverage.to)} (
              {value.coverage.complete ? "complete" : "partial"})
            </dd>
          </div>
          <div>
            <dt>Semantic index</dt>
            <dd>{value.health.semantic_index.replaceAll("_", " ")}</dd>
          </div>
          <div>
            <dt>Lark history</dt>
            <dd>{value.health.lark_history}</dd>
          </div>
          {Object.entries(value.jobs).map(([state, count]) => (
            <div key={state}>
              <dt>{state.replaceAll("_", " ")} jobs</dt>
              <dd>{count}</dd>
            </div>
          ))}
          <div>
            <dt>Oldest job age</dt>
            <dd>{value.oldest_job_age_seconds} seconds</dd>
          </div>
        </dl>
        <p className="group-brain-updated">
          Updated {display(value.updated_at)} by {display(value.updated_by)}
        </p>
        <div className="group-brain-actions">
          <button
            className="primary"
            aria-label="Save learning settings"
            disabled={locked || !isDirty || windowInvalid}
          >
            {busy ? "Saving..." : "Save"}
          </button>
          {value.effective_state === "paused" ? (
            <button
              type="button"
              disabled={locked}
              onClick={() => void transition("resume")}
            >
              Resume
            </button>
          ) : (
            <button
              type="button"
              disabled={locked || !draft.configured_enabled}
              onClick={() => void transition("pause")}
            >
              Pause
            </button>
          )}
          <button
            type="button"
            className="danger-button"
            disabled={locked || !draft.configured_enabled}
            onClick={() => {
              setConfirmId("");
              setPurgeError("");
              setPurgeOpen(true);
            }}
          >
            Purge
          </button>
        </div>
        {action.feedback}
      </form>
      {reviewEnabled && (
        <div className="panel-body group-brain-lists group-brain-review-lists">
          <section aria-labelledby={`brain-review-${groupId}`}>
            <h3 id={`brain-review-${groupId}`}>
              Notes to review{" "}
              <span className="group-brain-count">
                {value.counts.pending_review}
              </span>
            </h3>
            {candidates.isPending ? (
              <Loading label="Loading notes for review" />
            ) : candidates.isError ? (
              <Failure
                error={candidates.error}
                retry={() => void candidates.refetch()}
              />
            ) : candidates.data.candidates.length === 0 ? (
              <p>No notes waiting for review.</p>
            ) : (
              <ul>
                {candidates.data.candidates.map((candidate) => {
                  const canReadSource =
                    candidate.content !== undefined &&
                    candidate.reason !== undefined;
                  const itemPath = `candidates/${encodeURIComponent(candidate.memory_id)}`;
                  const approvalIdentity = `${candidate.revision}:${candidate.content_hash}`;
                  return (
                    <li
                      className="group-brain-review-item"
                      key={`${candidate.memory_id}-${candidate.revision}`}
                    >
                      <div className="group-brain-review-copy">
                        <strong>{candidate.category}</strong>
                        <span>
                          {candidate.state} · revision {candidate.revision} ·
                          proposed by {candidate.proposed_by} ·{" "}
                          {candidate.created_at}
                        </span>
                        {canReadSource ? (
                          <>
                            <p>{candidate.content}</p>
                            <p>Reason: {candidate.reason}</p>
                          </>
                        ) : (
                          <p>
                            Text is hidden because the reviewer cannot see its
                            source.
                          </p>
                        )}
                        <span>Content hash: {candidate.content_hash}</span>
                      </div>
                      {canEdit && (
                        <div className="group-brain-review-controls">
                          {canReadSource && candidate.state === "candidate" && (
                            <label className="group-brain-ack">
                              <input
                                type="checkbox"
                                checked={
                                  noteAcknowledgements[candidate.memory_id] ===
                                  approvalIdentity
                                }
                                disabled={busy}
                                onChange={(event) =>
                                  setNoteAcknowledgements((current) => ({
                                    ...current,
                                    [candidate.memory_id]: event.target.checked
                                      ? approvalIdentity
                                      : "",
                                  }))
                                }
                              />
                              I confirm this note may be shared with everyone in
                              this group
                            </label>
                          )}
                          {candidate.state === "candidate" && (
                            <>
                              {canReadSource && (
                                <button
                                  type="button"
                                  className="primary"
                                  disabled={
                                    busy ||
                                    locked ||
                                    noteAcknowledgements[
                                      candidate.memory_id
                                    ] !== approvalIdentity
                                  }
                                  onClick={() =>
                                    void reviewAction(
                                      `${itemPath}/approve`,
                                      {
                                        revision: candidate.revision,
                                        content_hash: candidate.content_hash,
                                        acknowledge_group_sharing: true,
                                      },
                                      "Note approved.",
                                    )
                                  }
                                >
                                  Approve
                                </button>
                              )}
                              <label>
                                Rejection reason
                                <select
                                  value={
                                    rejectReasons[candidate.memory_id] ??
                                    "not_accurate"
                                  }
                                  disabled={busy}
                                  onChange={(event) =>
                                    setRejectReasons((current) => ({
                                      ...current,
                                      [candidate.memory_id]: event.target.value,
                                    }))
                                  }
                                >
                                  <option value="not_accurate">
                                    Not accurate
                                  </option>
                                  <option value="not_for_group">
                                    Not for this group
                                  </option>
                                  <option value="sensitive">Sensitive</option>
                                  <option value="duplicate">Duplicate</option>
                                </select>
                              </label>
                              <button
                                type="button"
                                className="danger-button"
                                disabled={busy || locked}
                                onClick={() =>
                                  void reviewAction(
                                    `${itemPath}/reject`,
                                    {
                                      revision: candidate.revision,
                                      reason_code:
                                        rejectReasons[candidate.memory_id] ??
                                        "not_accurate",
                                    },
                                    "Note rejected.",
                                  )
                                }
                              >
                                Reject
                              </button>
                            </>
                          )}
                          {candidate.state === "active" && (
                            <button
                              type="button"
                              className="danger-button"
                              disabled={busy || locked}
                              onClick={() =>
                                void reviewAction(
                                  `memory/${encodeURIComponent(candidate.memory_id)}/revoke`,
                                  {},
                                  "Approved note revoked.",
                                )
                              }
                            >
                              Revoke
                            </button>
                          )}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        </div>
      )}
      <details className="panel-body">
        <summary>Jobs and audit history</summary>
        <div className="group-brain-lists">
          <section aria-labelledby={`brain-jobs-${groupId}`}>
            <h3 id={`brain-jobs-${groupId}`}>Jobs</h3>
            {jobs.isPending ? (
              <Loading label="Loading job metadata" />
            ) : jobs.isError ? (
              <Failure error={jobs.error} retry={() => void jobs.refetch()} />
            ) : jobs.data.jobs.length === 0 ? (
              <p>No learning jobs have been recorded.</p>
            ) : (
              <ul>
                {jobs.data.jobs.map((job) => (
                  <li key={job.id}>
                    <span>
                      <strong>{job.kind}</strong> · {job.state} · {job.attempts}{" "}
                      attempts · generation {job.generation} · {job.created_at}
                      {job.error_code && ` · ${job.error_code}`}
                    </span>
                    {job.state === "failed" && (
                      <button
                        type="button"
                        disabled={
                          !canEdit ||
                          busy ||
                          value.effective_state === "purging"
                        }
                        onClick={() =>
                          void action.run(async () => {
                            await write(
                              `${base}/jobs/${encodeURIComponent(job.id)}/retry`,
                              "POST",
                              {},
                            );
                            await client.invalidateQueries({
                              queryKey: ["group-knowledge-jobs", groupId],
                            });
                          }, "Retry queued.")
                        }
                      >
                        Retry
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section aria-labelledby={`brain-audit-${groupId}`}>
            <h3 id={`brain-audit-${groupId}`}>Audit</h3>
            {audit.isPending ? (
              <Loading label="Loading audit metadata" />
            ) : audit.isError ? (
              <Failure error={audit.error} retry={() => void audit.refetch()} />
            ) : auditEvents.length === 0 ? (
              <p>No learning audit events have been recorded.</p>
            ) : (
              <ul>
                {auditEvents.map((event, index) => (
                  <li key={`${event.id}-${index}`}>
                    <span>
                      {event.action} · {event.outcome} · actor {event.actor_id}{" "}
                      · target {display(event.target_id)} · reason{" "}
                      {display(event.reason_code)} · policy version{" "}
                      {event.policy_version} · {event.created_at}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {auditError && <p role="alert">{auditError}</p>}
            {canLoadMore && (
              <button
                type="button"
                disabled={auditLoading || auditEvents.length === 0}
                onClick={() => void showHistoryMore()}
              >
                {auditLoading ? "Loading..." : "Load more"}
              </button>
            )}
          </section>
        </div>
      </details>

      <Dialog.Root open={enableOpen} onOpenChange={setEnableOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="overlay" />
          <Dialog.Content
            className="dialog group-brain-dialog"
            onCloseAutoFocus={() => enableRef.current?.focus()}
          >
            <Dialog.Title>Enable group learning?</Dialog.Title>
            <Dialog.Description>
              The assistant can recall discussions and save facts from cited
              messages when the corresponding setting is enabled.
            </Dialog.Description>
            <p>
              Only members of this group can retrieve its conversation evidence.
              Source-backed facts can become active automatically. Corrections
              require review.
            </p>
            <p>Learning mode: {draft.learning_mode}.</p>
            <label>
              Notice reference
              <input
                value={draft.notice_ref}
                onChange={(event) =>
                  updateDraft({ notice_ref: event.target.value })
                }
              />
            </label>
            <label>
              Policy reference
              <input
                value={draft.policy_ref}
                onChange={(event) =>
                  updateDraft({ policy_ref: event.target.value })
                }
              />
            </label>
            <label className="group-brain-ack">
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(event) => setAcknowledged(event.target.checked)}
              />
              I acknowledge that information is shared with members of this
              group.
            </label>
            <div className="dialog-actions">
              <Dialog.Close asChild>
                <button type="button">Cancel</button>
              </Dialog.Close>
              <button
                type="button"
                className="primary"
                disabled={
                  !draft.notice_ref.trim() ||
                  !draft.policy_ref.trim() ||
                  !acknowledged
                }
                onClick={() => {
                  updateDraft({ configured_enabled: true });
                  setEnableOpen(false);
                }}
              >
                Confirm enablement
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      <Dialog.Root
        open={purgeOpen}
        onOpenChange={(open) => {
          if (!busy) setPurgeOpen(open);
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="overlay" />
          <Dialog.Content
            className="dialog group-brain-dialog"
            onEscapeKeyDown={(event) => {
              if (busy) event.preventDefault();
            }}
            onPointerDownOutside={(event) => {
              if (busy) event.preventDefault();
            }}
          >
            <Dialog.Title>Purge conversation knowledge?</Dialog.Title>
            <Dialog.Description>
              This permanently removes the group’s discussion history and
              learned records. Added Markdown sources are managed separately.
              Backups follow the retention policy.
            </Dialog.Description>
            {purgeError && <p role="alert">{purgeError}</p>}
            <label>
              Type this group's chat ID to confirm: <code>{groupId}</code>
              <input
                value={confirmId}
                disabled={busy}
                onChange={(event) => setConfirmId(event.target.value)}
                autoComplete="off"
              />
            </label>
            <div className="dialog-actions">
              <Dialog.Close asChild>
                <button type="button" disabled={busy}>
                  Cancel
                </button>
              </Dialog.Close>
              <button
                type="button"
                className="danger-button"
                disabled={busy || confirmId !== groupId}
                onClick={() => void purge()}
              >
                {busy ? "Purging..." : "Purge conversation knowledge"}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </Panel>
  );
}
