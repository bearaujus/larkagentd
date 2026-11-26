import { useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, write } from "./api";
import { Confirm, Failure, Loading, useAction, time } from "./components";

type Source = {
  id: string;
  name: string;
  markdown: string;
  server: string | null;
  uri: string | null;
};
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

function LearnedFacts({
  endpoint,
  groupId,
}: {
  endpoint: string;
  groupId: string;
}) {
  const [pages, setPages] = useState([""]);
  const cursor = pages[pages.length - 1];
  const learned = useQuery({
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
          Refresh sources
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

export function KnowledgeLibrary({ groupId }: { groupId?: string }) {
  const endpoint = groupId
    ? "/groups/" + encodeURIComponent(groupId) + "/knowledge"
    : "/knowledge";
  const listing = useQuery({
    queryKey: [endpoint, "sources"],
    queryFn: ({ signal }) => api<Sources>(endpoint + "/sources", { signal }),
  });
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState("");
  const [name, setName] = useState("");
  const [markdown, setMarkdown] = useState("");
  const [server, setServer] = useState("");
  const [uri, setUri] = useState("");
  const action = useAction();
  function close() {
    setEditing("");
    setName("");
    setMarkdown("");
    setServer("");
    setUri("");
    setOpen(false);
  }
  function edit(item: Source) {
    setEditing(item.id);
    setName(item.name);
    setMarkdown(item.markdown);
    setServer(item.server ?? "");
    setUri(item.uri ?? "");
    setOpen(true);
  }
  function save(event: FormEvent) {
    event.preventDefault();
    void action.run(async () => {
      await write(endpoint + "/sources", editing ? "PATCH" : "POST", {
        ...(editing ? { id: editing } : {}),
        name,
        ...(server ? { server, uri } : { markdown }),
      });
      close();
      await listing.refetch();
    }, "Source saved.");
  }
  return (
    <>
      {groupId && <LearnedFacts groupId={groupId} endpoint={endpoint} />}
      <section className="panel knowledge-panel" aria-label="Added sources">
        <h2>{groupId ? "Added sources" : "Workspace sources"}</h2>
        {listing.isPending && <Loading />}
        {listing.isError && (
          <Failure error={listing.error} retry={() => void listing.refetch()} />
        )}
        {listing.data && (
          <>
            {!listing.data.sources.length && <p>No sources added yet.</p>}
            <ul className="knowledge-list">
              {listing.data.sources.map((item) => (
                <li key={item.id}>
                  <div>
                    <strong>{item.name}</strong>{" "}
                    <small>{item.server || "Markdown"}</small>
                  </div>
                  <div className="knowledge-actions">
                    <button className="button" onClick={() => edit(item)}>
                      Edit source
                    </button>
                    <Confirm
                      label="Remove source"
                      title={"Remove " + item.name + "?"}
                      onConfirm={async () => {
                        await write(endpoint + "/sources", "DELETE", {
                          id: item.id,
                        });
                        if (editing === item.id) close();
                        await listing.refetch();
                      }}
                    >
                      The assistant will stop using this source.
                    </Confirm>
                  </div>
                </li>
              ))}
            </ul>
            {!open && (
              <button className="button primary" onClick={() => setOpen(true)}>
                Add source
              </button>
            )}
            {open && (
              <form className="knowledge-form" onSubmit={save}>
                <label>
                  Source name
                  <input
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    maxLength={96}
                    required
                  />
                </label>
                {!!listing.data.mcp_servers.length && (
                  <label>
                    Source type
                    <select
                      value={server}
                      onChange={(event) => setServer(event.target.value)}
                    >
                      <option value="">Markdown</option>
                      {listing.data.mcp_servers.map((item) => (
                        <option key={item} value={item}>
                          {item}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                {server ? (
                  <label>
                    MCP resource URI
                    <input
                      value={uri}
                      onChange={(event) => setUri(event.target.value)}
                      required
                    />
                  </label>
                ) : (
                  <label>
                    Markdown
                    <textarea
                      value={markdown}
                      onChange={(event) => setMarkdown(event.target.value)}
                      maxLength={24000}
                      rows={6}
                      required
                    />
                  </label>
                )}
                <button className="button primary" disabled={action.busy}>
                  {editing ? "Save changes" : "Add source"}
                </button>
                <button type="button" className="button" onClick={close}>
                  Cancel
                </button>
              </form>
            )}
          </>
        )}
        {action.feedback}
      </section>
    </>
  );
}
