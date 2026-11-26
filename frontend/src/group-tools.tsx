import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, client, write } from "./api";
import { Failure, Loading, Panel, useAction } from "./components";
import type { GroupPolicy, Session, Tool } from "./types";

type ToolStatus = "all" | "enabled" | "disabled";

const kindLabel = (kind: string) =>
  kind.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());

export function GroupTools({
  policy,
  path,
}: {
  policy: GroupPolicy;
  path: string;
}) {
  const tools = useQuery({
    queryKey: ["tools"],
    queryFn: ({ signal }) => api<Tool[]>("/tools", { signal }),
  });
  const session = useQuery({
    queryKey: ["auth-session"],
    queryFn: ({ signal }) => api<Session>("/auth/session", { signal }),
  });
  const [draft, setDraft] = useState(policy.tools);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<ToolStatus>("all");
  const [collapsed, setCollapsed] = useState<string[]>([]);
  useEffect(() => setDraft(policy.tools), [policy.tools]);
  const action = useAction();
  const canEdit = session.data?.role === "admin";
  const change = (name: string, checked: boolean) => {
    setDraft((current) =>
      checked
        ? [...new Set([...current, name])].sort()
        : current.filter((tool) => tool !== name),
    );
  };
  const allTools = tools.data ?? [];
  const visibleTools = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return allTools.filter((tool) => {
      const matchesSearch =
        !query ||
        `${tool.name} ${tool.label ?? ""} ${tool.kind} ${tool.description}`
          .toLocaleLowerCase()
          .includes(query);
      const enabled = draft.includes(tool.name);
      return (
        matchesSearch &&
        (status === "all" || (status === "enabled" ? enabled : !enabled))
      );
    });
  }, [allTools, draft, search, status]);
  const groupedTools = useMemo(() => {
    const groups = new Map<string, Tool[]>();
    for (const tool of visibleTools) {
      groups.set(tool.kind, [...(groups.get(tool.kind) ?? []), tool]);
    }
    return [...groups.entries()].sort(([left], [right]) =>
      left.localeCompare(right),
    );
  }, [visibleTools]);
  const enabledCount = allTools.filter((tool) =>
    draft.includes(tool.name),
  ).length;
  const toggleGroup = (kind: string) =>
    setCollapsed((current) =>
      current.includes(kind)
        ? current.filter((item) => item !== kind)
        : [...current, kind],
    );

  return (
    <Panel
      title="Tools"
      description="Choose which registered tools the agent may use in this group."
      className="group-tools-panel"
    >
      {session.isPending || tools.isPending ? (
        <Loading label="Loading group tools" />
      ) : session.isError ? (
        <Failure error={session.error} retry={() => void session.refetch()} />
      ) : tools.isError ? (
        <Failure error={tools.error} retry={() => void tools.refetch()} />
      ) : (
        <form
          className="panel-body group-tools"
          onSubmit={(event) => {
            event.preventDefault();
            void action.run(async () => {
              // Save the whole draft, not just the current filtered view.
              const saved = await write<GroupPolicy>(`${path}/tools`, "PUT", {
                tools: [...new Set(draft)].sort(),
              });
              client.setQueryData(["policy", path], saved);
              await Promise.all([
                client.invalidateQueries({ queryKey: ["groups"] }),
                client.invalidateQueries({ queryKey: ["policy", path] }),
              ]);
              setDraft(saved.tools);
            }, "Group tools saved.");
          }}
        >
          {!canEdit && (
            <p className="group-tools-readonly">
              Managers can view group tools but only admins can change them.
            </p>
          )}
          {allTools.length === 0 ? (
            <p className="group-tools-empty">
              No tools are available to enable. This group can use skills and
              the knowledge base only.
            </p>
          ) : (
            <>
              <p className="group-tools-summary" aria-live="polite">
                {enabledCount} of {allTools.length} enabled
              </p>
              <div className="group-tools-filters">
                <label className="group-tools-search">
                  Search tools
                  <input
                    type="search"
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder="Name, kind, or description"
                    disabled={!canEdit || action.busy}
                  />
                </label>
                <fieldset
                  className="group-tools-status"
                  disabled={!canEdit || action.busy}
                >
                  <legend>Show tools</legend>
                  {(["all", "enabled", "disabled"] as const).map((value) => (
                    <label key={value}>
                      <input
                        type="radio"
                        name="tool-status"
                        value={value}
                        checked={status === value}
                        onChange={() => setStatus(value)}
                      />
                      {value[0].toUpperCase() + value.slice(1)}
                    </label>
                  ))}
                </fieldset>
              </div>
              <p className="sr-only" aria-live="polite">
                {visibleTools.length}{" "}
                {visibleTools.length === 1 ? "tool" : "tools"} shown
              </p>
              {visibleTools.length === 0 ? (
                <p className="group-tools-empty-state">
                  No tools match these filters.
                </p>
              ) : (
                <div className="group-tools-groups">
                  {groupedTools.map(([kind, groupTools]) => {
                    const isCollapsed = collapsed.includes(kind);
                    const enabledInGroup = allTools.filter(
                      (tool) => tool.kind === kind && draft.includes(tool.name),
                    ).length;
                    const totalInGroup = allTools.filter(
                      (tool) => tool.kind === kind,
                    ).length;
                    return (
                      <section className="group-tools-kind" key={kind}>
                        <h3>
                          <button
                            type="button"
                            className="group-tools-kind-toggle"
                            aria-expanded={!isCollapsed}
                            onClick={() => toggleGroup(kind)}
                            disabled={!canEdit || action.busy}
                          >
                            <span>{kindLabel(kind)}</span>
                            <span>
                              {enabledInGroup} of {totalInGroup} enabled
                            </span>
                          </button>
                        </h3>
                        {!isCollapsed && (
                          <fieldset
                            className="group-tools-list"
                            disabled={!canEdit || action.busy}
                          >
                            <legend className="sr-only">
                              {kindLabel(kind)} tools
                            </legend>
                            {groupTools.map((tool) => {
                              const checked = draft.includes(tool.name);
                              return (
                                <label className="group-tool" key={tool.name}>
                                  <input
                                    type="checkbox"
                                    checked={checked}
                                    disabled={!canEdit || action.busy}
                                    onChange={(event) =>
                                      change(tool.name, event.target.checked)
                                    }
                                  />
                                  <span className="group-tool-content">
                                    <span className="group-tool-heading">
                                      <strong>{tool.label || tool.name}</strong>
                                      <span className="group-tool-risk">
                                        Risk: {tool.risk}
                                      </span>
                                      {!tool.enabled_globally && (
                                        <span className="group-tool-off">
                                          Off for everyone: no effect until it
                                          is turned on
                                        </span>
                                      )}
                                    </span>
                                    <span className="group-tool-description">
                                      {tool.description}{" "}
                                      {tool.authority && (
                                        <small>Access: {tool.authority}</small>
                                      )}
                                    </span>
                                  </span>
                                </label>
                              );
                            })}
                          </fieldset>
                        )}
                      </section>
                    );
                  })}
                </div>
              )}
            </>
          )}
          <p className="group-tools-note">
            Skills and the knowledge base are always available in a group. The
            terminal is not controlled here.
          </p>
          <div className="group-tools-actions">
            <button
              type="button"
              disabled={!canEdit || action.busy || !allTools.length}
              onClick={() => setDraft(allTools.map((tool) => tool.name).sort())}
            >
              Enable all
            </button>
            <button
              type="button"
              disabled={!canEdit || action.busy || !allTools.length}
              onClick={() => setDraft([])}
            >
              Disable all
            </button>
            <button
              type="button"
              disabled={!canEdit || action.busy || !allTools.length}
              onClick={() =>
                setDraft(
                  allTools
                    .filter((tool) => tool.default)
                    .map((tool) => tool.name)
                    .sort(),
                )
              }
            >
              Reset to default
            </button>
            <button
              className="primary"
              disabled={!canEdit || action.busy || !allTools.length}
            >
              {action.busy ? "Saving..." : "Save"}
            </button>
          </div>
          {draft.length === 0 && (
            <p className="group-tools-empty-state">
              No tools are enabled. This group can use skills and the knowledge
              base only.
            </p>
          )}
          {action.feedback}
        </form>
      )}
    </Panel>
  );
}
