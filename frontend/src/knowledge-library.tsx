import { useId, useRef, useState, type FormEvent } from "react";
import { BookOpen, ExternalLink, FileText, Library, Plus } from "lucide-react";
import { Dialog } from "radix-ui";
import { Link, useSearchParams } from "react-router";
import { useDirectory } from "./directory";
import type { GroupPolicy } from "./types";
import { useQuery } from "@tanstack/react-query";
import { api, write } from "./api";
import { Confirm, Failure, Loading, useAction, time } from "./components";

type Source = {
  id: string;
  name: string;
  custom_name?: string | null;
  markdown: string;
  server: string | null;
  uri: string | null;
  kind?: "lark_doc" | "lark_wiki" | null;
  url?: string | null;
  status?: string;
  indexed?: number;
  skipped?: number;
  error?: string | null;
  last_success?: number | null;
};
function detectLink(value: string) {
  try {
    const url = new URL(value);
    if (
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      url.port ||
      !/\.(larksuite\.com|feishu\.cn|larkoffice\.com)$/.test(url.hostname)
    )
      return null;
    const path = url.pathname.replace(/\/$/, "");
    if (/^\/doc[xs]\/[\w-]{8,128}$/.test(path))
      return { kind: "lark_doc", coverage: "This document only." };
    if (/^\/wiki\/settings\/\d{1,24}$/.test(path))
      return { kind: "lark_wiki", coverage: "All Docs in this wiki space." };
    if (path !== "/wiki/settings" && /^\/wiki\/[\w-]{8,128}$/.test(path))
      return {
        kind: "lark_wiki",
        coverage: "This wiki page and its children.",
      };
  } catch {
    /* Incomplete URL while typing. */
  }
  return null;
}

type Sources = { sources: Source[]; mcp_servers: string[] };
type Fact = {
  id: string;
  category: string;
  activation: string;
  content: string | null;
  status: string;
  sources: {
    source_id: string;
    url: string | null;
    time: string;
    excerpt: string;
  }[];
};
type Page = {
  records: Fact[];
  next: string | null;
  group_name: string | null;
  learning: boolean;
  state: string;
};

export function useLearnedPage(endpoint: string, cursor = "") {
  return useQuery({
    queryKey: [endpoint, "learned", cursor],
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: true,
    refetchInterval: 60000,
    queryFn: ({ signal }) =>
      api<Page>(
        endpoint +
          "/learned" +
          (cursor ? "?after=" + encodeURIComponent(cursor) : ""),
        { signal },
      ),
  });
}

function LearnedFacts({
  endpoint,
  groupId,
}: {
  endpoint: string;
  groupId: string;
}) {
  const [pages, setPages] = useState([""]);
  const cursor = pages[pages.length - 1];
  const learned = useLearnedPage(endpoint, cursor);
  if (learned.isPending) return <Loading label="Checking group sources" />;
  if (learned.isError)
    return (
      <Failure error={learned.error} retry={() => void learned.refetch()} />
    );
  const page = learned.data;
  return (
    <section
      className="panel knowledge-panel"
      aria-label="Learned from conversations"
    >
      <h2>Learned from conversations</h2>
      <p>
        <strong>{page.group_name || groupId}</strong> ·{" "}
        {page.learning
          ? "Learning enabled"
          : page.state === "paused"
            ? "Learning paused"
            : "Learning disabled"}
      </p>
      {page.learning && (
        <p className="muted">
          The assistant can save useful facts from this group after each turn.{" "}
          Workspace admins manage learning settings.
        </p>
      )}
      {page.learning && !page.records.length && (
        <p>No active facts on this page.</p>
      )}
      <ul className="fact-list">
        {page.records.map((fact) => (
          <li key={fact.id}>
            {fact.status === "verified" ? (
              <>
                <strong>{fact.content}</strong>
                <small>
                  {fact.category.replaceAll("_", " ")} ·{" "}
                  {fact.activation === "reviewed"
                    ? "Approved by a reviewer"
                    : "Activated after a source check"}
                </small>
                {fact.sources.map((source) => (
                  <blockquote key={source.source_id}>
                    <p>{source.excerpt}</p>
                    {source.url ? (
                      <a href={source.url} target="_blank" rel="noreferrer">
                        Open source message
                      </a>
                    ) : (
                      <span>Source message · link unavailable</span>
                    )}{" "}
                    · {time(source.time)}
                  </blockquote>
                ))}
              </>
            ) : (
              <p>
                {fact.status === "temporarily_unavailable"
                  ? "Lark is temporarily unavailable. Refresh to check this fact."
                  : "The source changed or is no longer accessible. This fact is hidden."}
              </p>
            )}
            <Confirm
              label="Stop using this fact"
              title="Stop using this fact?"
              onConfirm={async () => {
                await write(
                  endpoint + "/learned/" + fact.id + "/retire",
                  "POST",
                );
                await learned.refetch();
              }}
            >
              The assistant will stop citing this fact in the group. The
              original Lark messages stay in the conversation.
            </Confirm>
          </li>
        ))}
      </ul>
      <div className="knowledge-actions">
        <button
          className="button"
          disabled={learned.isFetching}
          onClick={() => void learned.refetch()}
        >
          Refresh facts
        </button>
        {pages.length > 1 && (
          <button
            className="button"
            onClick={() => setPages(pages.slice(0, -1))}
          >
            Previous facts
          </button>
        )}
        {page.next && (
          <button
            className="button"
            onClick={() => setPages([...pages, page.next!])}
          >
            Next facts
          </button>
        )}
      </div>
    </section>
  );
}

export function WorkspaceKnowledge() {
  const groups = useQuery({
    queryKey: ["groups"],
    queryFn: ({ signal }) => api<GroupPolicy[]>("/groups", { signal }),
  });
  const [params, setParams] = useSearchParams();
  const groupId = params.get("group") || undefined;
  const selected = groups.data?.find((group) => group.group_id === groupId);
  const directory = useDirectory(
    (groups.data ?? []).map((g) => ({ kind: "group", key: g.group_id })),
  );
  return (
    <>
      <header className="page-intro knowledge-intro">
        <div>
          <h1>Knowledge</h1>
          <p>
            Manage the sources the assistant can cite in group conversations.
          </p>
        </div>
        <label>
          View sources for
          <select
            value={groupId ?? ""}
            onChange={(e) =>
              setParams(e.target.value ? { group: e.target.value } : {})
            }
          >
            <option value="">Workspace</option>
            {groups.data?.map((g) => (
              <option key={g.group_id} value={g.group_id}>
                {directory.get({ kind: "group", key: g.group_id })?.name ||
                  g.group_id}
              </option>
            ))}
          </select>
        </label>
      </header>
      {groups.isError && (
        <Failure error={groups.error} retry={() => void groups.refetch()} />
      )}
      {!groupId ? (
        <KnowledgeLibrary key="workspace" />
      ) : groups.isPending ? (
        <Loading label="Loading group" />
      ) : selected ? (
        <KnowledgeLibrary
          key={groupId}
          groupId={groupId}
          groupName={
            directory.get({ kind: "group", key: groupId })?.name || groupId
          }
        />
      ) : (
        !groups.isError && (
          <p role="status">
            This group is no longer registered.{" "}
            <Link to="/knowledge">View workspace sources</Link>
          </p>
        )
      )}
    </>
  );
}

const syncLabels: Record<string, string> = {
  queued: "Waiting to sync",
  syncing: "Syncing pages",
  partial: "Sync incomplete",
  paused: "Group knowledge paused",
};
function SyncStatus({ item }: { item: Source }) {
  const count = item.indexed ?? 0;
  const ready = item.status === "ready";
  const label = ready
    ? count
      ? "Ready to use"
      : "No searchable pages"
    : (syncLabels[item.status ?? ""] ?? "Status unavailable");
  const reasons: Record<string, string> = {
    forbidden: "Give the bot view access in Lark, then refresh.",
    not_found: "Check that the page still exists in Lark.",
    rate_limited: "Lark is busy. Sync will retry automatically.",
    timeout: "Lark took too long to respond. Sync will retry automatically.",
  };
  return (
    <div className="source-sync">
      <span
        className={
          "source-state " +
          (ready && count
            ? "available"
            : item.status === "partial"
              ? "attention"
              : "")
        }
      >
        {label}
      </span>
      {(ready || item.status === "partial" || count > 0) && (
        <span>
          {count} searchable {count === 1 ? "page" : "pages"}
          {item.skipped ? " · " + item.skipped + " skipped" : ""}
        </span>
      )}
      {item.error && (
        <span className="sync-reason">
          {reasons[item.error] ??
            "Some pages could not be checked. Sync will retry automatically."}
        </span>
      )}
    </div>
  );
}

type Editor = { item?: Source; trigger: HTMLButtonElement };
function SourceEditor({
  editor,
  endpoint,
  audience,
  servers,
  close,
  saved,
}: {
  editor: Editor;
  endpoint: string;
  audience: string;
  servers: string[];
  close: () => void;
  saved: (message: string) => void;
}) {
  const item = editor.item;
  const initialType = item?.server
    ? "mcp:" + item.server
    : item && !item.kind
      ? "markdown"
      : "lark";
  const [type, setType] = useState(initialType);
  const [url, setUrl] = useState(item?.url ?? "");
  const [name, setName] = useState(
    item?.custom_name === null ? "" : (item?.name ?? ""),
  );
  const [markdown, setMarkdown] = useState(item?.markdown ?? "");
  const [uri, setUri] = useState(item?.uri ?? "");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const linkField = useRef<HTMLInputElement>(null);
  const formatField = useRef<HTMLSelectElement>(null);
  const hint = useId();
  const link = detectLink(url);
  const invalid = Boolean(url && !link);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || (type === "lark" && !link)) return;
    setBusy(true);
    setError(null);
    try {
      await write(endpoint + "/sources", item ? "PATCH" : "POST", {
        ...(item ? { id: item.id } : {}),
        name: name.trim(),
        ...(type === "lark" && link
          ? { kind: link.kind, url: url.trim() }
          : type === "markdown"
            ? { markdown }
            : { server: type.slice(4), uri }),
      });
      saved(item ? "Source updated." : "Source added.");
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }
  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (!open && !busy) close();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="overlay" />
        <Dialog.Content
          className="dialog source-editor"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            (linkField.current ?? formatField.current)?.focus();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            editor.trigger.focus();
          }}
        >
          <Dialog.Title>{item ? "Edit source" : "Add a source"}</Dialog.Title>
          <Dialog.Description>{audience}</Dialog.Description>
          <form className="knowledge-form" onSubmit={submit}>
            <label>
              Source type
              <select
                ref={formatField}
                value={type}
                onChange={(e) => {
                  setType(e.target.value);
                  setError(null);
                }}
              >
                <option value="lark">Lark link</option>
                <option value="markdown">Markdown</option>
                {servers.map((server) => (
                  <option key={server} value={"mcp:" + server}>
                    {server}
                  </option>
                ))}
              </select>
            </label>
            {type === "lark" ? (
              <>
                <label>
                  Lark link
                  <input
                    ref={linkField}
                    type="url"
                    value={url}
                    onChange={(e) => setUrl(e.target.value.trim())}
                    placeholder="Paste a Lark Wiki or Doc link"
                    required
                    maxLength={2048}
                    aria-describedby={hint}
                    aria-invalid={invalid}
                  />
                </label>
                <p
                  id={hint}
                  aria-live="polite"
                  className={invalid ? "source-field-error" : "source-coverage"}
                >
                  {link
                    ? link.coverage
                    : invalid
                      ? "Use a wiki space settings link, wiki page link or Doc link."
                      : "Supports wiki spaces, wiki pages and individual Docs."}
                </p>
              </>
            ) : type === "markdown" ? (
              <label>
                Markdown
                <textarea
                  value={markdown}
                  onChange={(e) => setMarkdown(e.target.value)}
                  maxLength={24000}
                  rows={5}
                  required
                />
              </label>
            ) : (
              <label>
                MCP resource URI
                <input
                  value={uri}
                  onChange={(e) => setUri(e.target.value)}
                  required
                />
              </label>
            )}
            <label>
              {type === "lark" ? "Custom name (optional)" : "Source name"}
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={
                  type === "lark"
                    ? "Use the title from Lark"
                    : "e.g. Office handbook"
                }
                maxLength={96}
                required={type !== "lark"}
              />
            </label>
            {error != null && <Failure error={error} />}
            <div className="dialog-actions">
              <button type="button" onClick={close} disabled={busy}>
                Cancel
              </button>
              <button
                className="primary"
                disabled={busy || (type === "lark" ? !link : !name.trim())}
              >
                {busy ? "Saving..." : item ? "Save changes" : "Add source"}
              </button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function KnowledgeLibrary({
  groupId,
  groupName,
}: {
  groupId?: string;
  groupName?: string;
}) {
  const endpoint = groupId
    ? "/groups/" + encodeURIComponent(groupId) + "/knowledge"
    : "/knowledge";
  const listing = useQuery<Sources>({
    queryKey: [endpoint, "sources"],
    queryFn: ({ signal }) => api<Sources>(endpoint + "/sources", { signal }),
    refetchInterval: (query) => {
      const rows = query.state.data?.sources ?? [];
      if (rows.some((s) => s.status === "queued" || s.status === "syncing"))
        return 2000;
      return rows.some((s) => s.kind) ? 30000 : false;
    },
  });
  const [editor, setEditor] = useState<Editor | null>(null);
  const [notice, setNotice] = useState("");
  const action = useAction();
  const audience = groupId
    ? "Available in " + (groupName || "this group") + " only."
    : "Available across registered groups.";
  return (
    <>
      <section
        className="panel source-library"
        aria-label={groupId ? "Group sources" : "Workspace sources"}
      >
        <div className="panel-heading">
          <div>
            <h2>{groupId ? "Group sources" : "Workspace sources"}</h2>
            <p>{audience} Lark sharing permissions apply.</p>
          </div>
          {listing.data && (
            <button
              className="primary source-add"
              onClick={(e) => {
                setNotice("");
                setEditor({ trigger: e.currentTarget });
              }}
            >
              <Plus size={16} aria-hidden="true" /> Add source
            </button>
          )}
        </div>
        {listing.isPending && <Loading />}
        {listing.isError && (
          <Failure error={listing.error} retry={() => void listing.refetch()} />
        )}
        {listing.data &&
          (!listing.data.sources.length ? (
            <div className="source-empty">
              <Library size={26} aria-hidden="true" />
              <h3>No sources yet</h3>
              <p>
                Add a Lark Wiki, a Doc or Markdown for the assistant to use.
              </p>
            </div>
          ) : (
            <ul className="source-list">
              {listing.data.sources.map((item) => (
                <li key={item.id}>
                  <div className="source-symbol">
                    {item.kind === "lark_wiki" ? (
                      <BookOpen size={20} />
                    ) : (
                      <FileText size={20} />
                    )}
                  </div>
                  <div className="source-info">
                    <small>
                      {item.kind === "lark_wiki"
                        ? "Lark Wiki"
                        : item.kind === "lark_doc"
                          ? "Lark Doc"
                          : item.server || "Markdown"}
                    </small>
                    <h3>
                      {item.url ? (
                        <a href={item.url} target="_blank" rel="noreferrer">
                          {item.name}{" "}
                          <ExternalLink size={13} aria-hidden="true" />
                        </a>
                      ) : (
                        item.name
                      )}
                    </h3>
                    {item.kind && (
                      <>
                        <SyncStatus item={item} />
                        {item.last_success ? (
                          <small className="sync-time">
                            Last synced{" "}
                            {time(
                              new Date(item.last_success * 1000).toISOString(),
                            )}{" "}
                            UTC
                          </small>
                        ) : null}
                      </>
                    )}
                  </div>
                  <div className="source-actions">
                    {item.kind && (
                      <button
                        disabled={action.busy || item.status === "syncing"}
                        onClick={() => {
                          setNotice("");
                          void action.run(async () => {
                            await write(endpoint + "/sources", "PATCH", {
                              id: item.id,
                              refresh: true,
                            });
                            await listing.refetch();
                          }, "Refresh queued.");
                        }}
                      >
                        Refresh now
                      </button>
                    )}
                    <button
                      onClick={(e) => {
                        setNotice("");
                        setEditor({ item, trigger: e.currentTarget });
                      }}
                    >
                      Edit source
                    </button>
                    <Confirm
                      label="Remove source"
                      title={"Remove " + item.name + "?"}
                      onConfirm={async () => {
                        await write(endpoint + "/sources", "DELETE", {
                          id: item.id,
                        });
                        setNotice("Source removed.");
                        await listing.refetch();
                      }}
                    >
                      The assistant will stop using this source.
                    </Confirm>
                  </div>
                </li>
              ))}
            </ul>
          ))}
        {notice && (
          <p role="status" className="source-notice">
            {notice}
          </p>
        )}
        {!notice && action.feedback}
      </section>
      {editor && (
        <SourceEditor
          editor={editor}
          endpoint={endpoint}
          audience={audience}
          servers={listing.data?.mcp_servers ?? []}
          close={() => setEditor(null)}
          saved={(message) => {
            setEditor(null);
            setNotice(message);
            void listing.refetch();
          }}
        />
      )}
      {groupId && <LearnedFacts groupId={groupId} endpoint={endpoint} />}
    </>
  );
}
