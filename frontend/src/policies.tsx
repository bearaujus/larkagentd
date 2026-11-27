import { useEffect, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useParams } from "react-router";
import { ArrowLeft, ChevronRight, Plus, ShieldCheck } from "lucide-react";
import { api, client, write } from "./api";
import {
  Badge,
  Hint,
  Confirm,
  CopyId,
  Empty,
  Failure,
  Loading,
  Notice,
  Panel,
  useAction,
} from "./components";
import { Identity, useDirectory } from "./directory";
import { AddDialog, suggestedGroupId } from "./add-dialog";
import type { GroupPolicy, Person, Policy, Session, Target } from "./types";
import { GroupTools } from "./group-tools";
import { KnowledgeLibrary } from "./knowledge-library";
import { GroupBrain } from "./group-brain";

const emailTarget = (key: string): Target => ({ kind: "email", key });
async function changed() {
  await Promise.all([
    client.invalidateQueries({ queryKey: ["policy"] }),
    client.invalidateQueries({ queryKey: ["roles"] }),
    client.invalidateQueries({ queryKey: ["groups"] }),
  ]);
}
export function People({ session }: { session: Session }) {
  const query = useQuery({
    queryKey: ["roles"],
    queryFn: ({ signal }) => api<Person[]>("/roles", { signal }),
  });
  const directory = useDirectory(
    (query.data ?? []).map((p) => emailTarget(p.email)),
  );
  const [removed, setRemoved] = useState("");
  const [adding, setAdding] = useState(false);
  if (query.isPending) return <Loading />;
  if (query.isError)
    return <Failure error={query.error} retry={() => void query.refetch()} />;
  function rows(role: "admin" | "manager") {
    const people = query.data!.filter((p) => p.role === role);
    return people.length ? (
      <ul className="person-list">
        {people.map((person) => (
          <li key={person.email}>
            <Identity
              target={emailTarget(person.email)}
              metadata={directory.get(emailTarget(person.email))}
              refresh={directory.refresh}
            />
            <div className="row-actions">
              <Badge tone={role === "admin" ? "blue" : ""}>
                {role === "admin" ? "Protected admin" : "Manager"}
              </Badge>
              {role === "manager" && session.role === "admin" && (
                <Confirm
                  label="Remove"
                  title="Remove manager?"
                  onConfirm={async () => {
                    await write(
                      `/managers/${encodeURIComponent(person.email)}`,
                      "DELETE",
                    );
                    await changed();
                    setRemoved(
                      "Manager removed. Their dashboard sessions and pending tokens were revoked.",
                    );
                  }}
                >
                  Remove dashboard access for {person.email}? All their
                  dashboard sessions and pending login tokens will be revoked.
                  Saved explicit chat memberships are retained.
                </Confirm>
              )}
            </div>
          </li>
        ))}
      </ul>
    ) : (
      <Empty title="No managers yet">
        An admin can add a manager using the Add manager button.
      </Empty>
    );
  }
  return (
    <>
      <PageIntro
        title="People & roles"
        description="The people who operate your assistant. Group membership rules are managed under Groups."
      />
      {removed && <Notice tone="success">{removed}</Notice>}
      <Panel
        title="Configured admins"
        description="Protected by configuration. Admins can manage managers and chat access."
        action={
          <Hint content="Configured admins are protected. Additions or removals require a configuration change and Gateway restart, not a dashboard action.">
            <button
              type="button"
              className="icon-button"
              aria-label="About configured admins"
            >
              <ShieldCheck size={19} />
            </button>
          </Hint>
        }
      >
        {rows("admin")}
      </Panel>
      <Panel
        title="Managers"
        description="Managers can manage chat access and view usage. Only admins can change this list."
        action={
          session.role === "admin" && (
            <button className="primary" onClick={() => setAdding(true)}>
              <Plus size={16} />
              Add manager
            </button>
          )
        }
      >
        {rows("manager")}
        {session.role === "admin" && (
          <AddDialog
            kind="manager"
            open={adding}
            onOpenChange={setAdding}
            conflict={(email) =>
              query.data?.some((person) => person.email === email)
                ? "This person already has a dashboard role. Configured admins are protected."
                : undefined
            }
            onAdd={async (email) => {
              await write(`/managers/${encodeURIComponent(email)}`, "PUT");
              await changed();
              setRemoved("Manager added.");
            }}
          />
        )}
      </Panel>
    </>
  );
}
export function PageIntro({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="page-intro">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </div>
  );
}
const modeOptions = {
  all_users: [
    "All users",
    "All current members can use the assistant in this group.",
  ],
  specific_users: [
    "Specific users",
    "Saved members and inherited admins and managers can pass.",
  ],
} satisfies Record<Policy["mode"], [string, string]>;

export function PolicyEditor({
  policy,
  path,
}: {
  policy: Policy;
  path: string;
}) {
  const [mode, setMode] = useState(policy.mode);
  const modes: Policy["mode"][] = ["all_users", "specific_users"];
  useEffect(() => setMode(policy.mode), [policy.mode]);
  const action = useAction();
  const [removed, setRemoved] = useState("");
  const [adding, setAdding] = useState(false);
  const inherited = new Set(policy.inherited_members.map((p) => p.email));
  const directory = useDirectory(
    [...policy.members, ...inherited].map(emailTarget),
  );
  return (
    <>
      <Panel
        title="Who can use the assistant in this group?"
        description="Choose a mode. Switching modes always preserves your saved member list."
      >
        <form
          className="panel-body"
          onSubmit={(e) => {
            e.preventDefault();
            void action.run(async () => {
              const saved = await write<Policy>(path, "PATCH", { mode });
              client.setQueryData(["policy", path], saved);
              await changed();
            }, "Access mode saved. Your member list was preserved.");
          }}
        >
          <fieldset className="mode-options">
            <legend className="sr-only">Access mode</legend>
            {modes.map((value) => (
              <label
                className={`mode-option ${mode === value ? "selected" : ""}`}
                key={value}
              >
                <input
                  type="radio"
                  name="mode"
                  value={value}
                  checked={mode === value}
                  onChange={() => setMode(value)}
                />
                <span>
                  <strong>{modeOptions[value][0]}</strong>
                  <span>{modeOptions[value][1]}</span>
                </span>
              </label>
            ))}
          </fieldset>
          <div className="save-row">
            <span className="muted">
              Current mode: {modeOptions[policy.mode][0]}
            </span>
            <button
              className="primary"
              disabled={mode === policy.mode || action.busy}
            >
              {action.busy ? "Saving..." : "Save mode"}
            </button>
          </div>
          {action.feedback}
        </form>
      </Panel>
      <Panel
        title="Inherited access"
        description="Admins and managers are always included in Specific users mode. Manage their roles in People."
      >
        <ul className="person-list">
          {policy.inherited_members.map((p) => (
            <li key={p.email}>
              <Identity
                target={emailTarget(p.email)}
                metadata={directory.get(emailTarget(p.email))}
                refresh={directory.refresh}
              />
              <Badge
                help={`${p.role === "admin" ? "Admins" : "Managers"} inherit access to every Specific users policy. Manage their role in People; this inherited access cannot be removed as ordinary membership.`}
              >
                {p.role} · inherited
              </Badge>
            </li>
          ))}
        </ul>
      </Panel>
      <Panel
        title={`Saved members (${policy.members.length})`}
        action={
          <button className="primary" onClick={() => setAdding(true)}>
            <Plus size={16} />
            Add member
          </button>
        }
        description={
          policy.mode === "specific_users"
            ? "Explicit membership for this chat only. Membership never grants dashboard access."
            : "Saved for Specific users mode."
        }
      >
        {removed && <Notice tone="success">{removed}</Notice>}
        {policy.members.length ? (
          <ul className="person-list">
            {policy.members.map((email) => (
              <li key={email}>
                <Identity
                  target={emailTarget(email)}
                  metadata={directory.get(emailTarget(email))}
                  refresh={directory.refresh}
                />
                {inherited.has(email) ? (
                  <Badge>Saved grant · inherited access</Badge>
                ) : (
                  <div className="row-actions">
                    <Badge>Member</Badge>
                    <Confirm
                      label="Remove"
                      title="Remove saved member?"
                      onConfirm={async () => {
                        await write(
                          `${path}/members/${encodeURIComponent(email)}`,
                          "DELETE",
                        );
                        await changed();
                        setRemoved("Saved member removed.");
                      }}
                    >
                      Remove {email} from this saved member list? Other chat
                      memberships are unchanged.
                    </Confirm>
                  </div>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <Empty title="No explicit members">
            Admins and managers still inherit access. Use Add member to review
            and add a person by email.
          </Empty>
        )}
        <AddDialog
          kind="member"
          open={adding}
          onOpenChange={setAdding}
          scope="this group"
          conflict={(email) =>
            inherited.has(email)
              ? "This person already inherits access as an admin or manager. Manage roles in People."
              : policy.members.includes(email)
                ? "This person is already a saved member of this chat."
                : undefined
          }
          onAdd={async (email) => {
            await write(`${path}/members/${encodeURIComponent(email)}`, "PUT");
            await changed();
            setRemoved("Member added to this chat.");
          }}
        />
      </Panel>
    </>
  );
}
const groupExplanation =
  "Mention the bot in a registered group. Group members can ask before opening a direct chat.";
export function Groups() {
  const notice = useLocation().state?.notice;
  const query = useQuery({
    queryKey: ["groups"],
    queryFn: ({ signal }) => api<GroupPolicy[]>("/groups", { signal }),
  });
  const [search, setSearch] = useState("");
  const [adding, setAdding] = useState(false);
  const [initialId, setInitialId] = useState("");
  const navigate = useNavigate();
  const directory = useDirectory(
    (query.data ?? []).map((p) => ({
      kind: "group" as const,
      key: p.group_id!,
    })),
  );
  const searchValue = search.trim();
  const visible = query.data?.filter((p) =>
    `${p.group_id} ${directory.get({ kind: "group", key: p.group_id! })?.name ?? ""}`
      .toLowerCase()
      .includes(searchValue.toLowerCase()),
  );
  const canSuggest =
    !visible?.length &&
    suggestedGroupId(searchValue) &&
    !query.data?.some((p) => p.group_id === searchValue);
  const openAdd = (id = "") => {
    setInitialId(id);
    setAdding(true);
  };
  return (
    <>
      <PageIntro
        title="Groups"
        description="Register Lark groups and manage their individual access policies."
        action={
          <button
            className="primary"
            disabled={!query.data}
            onClick={() => openAdd()}
          >
            <Plus size={16} />
            Add group
          </button>
        }
      />
      {notice && <Notice tone="success">{notice}</Notice>}
      <Notice>{groupExplanation}</Notice>
      <Panel
        title="Registered groups"
        description="Open a group to manage its access policy and saved members."
      >
        <div className="panel-body">
          <label>
            Find a registered group
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Group name or ID"
            />
          </label>
        </div>
        {query.isPending ? (
          <Loading />
        ) : query.isError ? (
          <Failure error={query.error} retry={() => void query.refetch()} />
        ) : visible?.length ? (
          <ul className="group-list">
            {visible.map((p) => {
              const target: Target = { kind: "group", key: p.group_id! };
              return (
                <li className="group-row" key={p.group_id}>
                  <Link
                    className="group-row-link"
                    to={`/groups/${encodeURIComponent(p.group_id!)}`}
                    aria-label={`Open ${directory.get(target)?.name || p.group_id} group settings`}
                  >
                    <span className="sr-only">Open group settings</span>
                  </Link>
                  <div className="group-summary">
                    <Identity
                      target={target}
                      metadata={directory.get(target)}
                      refresh={directory.refresh}
                    />
                    <div className="group-id">
                      <CopyId value={p.group_id!} />
                    </div>
                  </div>
                  <div className="row-actions">
                    <Badge>
                      {p.mode === "all_users" ? "All users" : "Specific users"}
                    </Badge>
                    <ChevronRight
                      className="row-chevron"
                      size={17}
                      aria-hidden="true"
                    />
                  </div>
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="group-empty">
            <Empty
              title={
                searchValue ? "No matching groups" : "No registered groups"
              }
            >
              {canSuggest
                ? "This looks like a Lark group ID. Review its details and register it."
                : searchValue
                  ? "Try another group name, or paste a Lark group ID to add it."
                  : "Use Add group to register your first group. New groups start in All users mode."}
            </Empty>
            {canSuggest && (
              <button className="primary" onClick={() => openAdd(searchValue)}>
                <Plus size={16} />
                Add this group
              </button>
            )}
          </div>
        )}
      </Panel>
      <AddDialog
        key={initialId}
        kind="group"
        open={adding}
        onOpenChange={setAdding}
        initialValue={initialId}
        conflict={(id) =>
          query.data?.some((group) => group.group_id === id)
            ? "This group is already registered. Close this dialog and open it from the group list."
            : undefined
        }
        onAdd={async (groupId) => {
          await write("/groups", "POST", { group_id: groupId });
          await changed();
          navigate(`/groups/${encodeURIComponent(groupId)}`, {
            state: { notice: "Group registered in All users mode." },
          });
        }}
      />
    </>
  );
}
export function GroupDetail({ showKnowledge }: { showKnowledge: boolean }) {
  const knowledgePage =
    new URLSearchParams(useLocation().search).get("view") === "knowledge";
  const notice = useLocation().state?.notice;
  const { id = "" } = useParams();
  const path = `/groups/${encodeURIComponent(id)}`;
  const query = useQuery({
    queryKey: ["policy", path],
    queryFn: ({ signal }) => api<GroupPolicy>(path, { signal }),
  });
  const target: Target = { kind: "group", key: id };
  const directory = useDirectory([target]);
  const navigate = useNavigate();
  return (
    <>
      <Link className="back-link" to="/groups">
        <ArrowLeft size={15} />
        All groups
      </Link>
      <PageIntro
        title={directory.get(target)?.name || id}
        description={knowledgePage ? "Group knowledge" : "Group access policy"}
        action={
          showKnowledge &&
          !knowledgePage &&
          query.data && (
            <Confirm
              label="Delete group"
              title="Delete this group?"
              onConfirm={async () => {
                await write(path, "DELETE");
                await changed();
                navigate("/groups", {
                  state: {
                    notice: "Group registration and saved members deleted.",
                  },
                });
              }}
            >
              Delete this registration, its saved members, and its added group
              sources? The bot will ignore this group. Registering it again
              starts with All users and a fresh member list.
            </Confirm>
          )
        }
      />
      {notice && <Notice tone="success">{notice}</Notice>}
      {!knowledgePage && (
        <div className="group-detail-meta">
          <Identity
            target={target}
            metadata={directory.get(target)}
            refresh={directory.refresh}
          />
          <CopyId value={id} />
        </div>
      )}
      <nav className="group-sections" aria-label="Group sections">
        <Link to={path} aria-current={!knowledgePage ? "page" : undefined}>
          Access and tools
        </Link>
        <Link
          to={path + "?view=knowledge"}
          aria-current={knowledgePage ? "page" : undefined}
        >
          Knowledge
        </Link>
      </nav>
      {!knowledgePage && <Notice>{groupExplanation}</Notice>}
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <Failure error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          {!knowledgePage && (
            <>
              <PolicyEditor key={id} path={path} policy={query.data} />
              <GroupTools key={id} policy={query.data} path={path} />
            </>
          )}
          {knowledgePage && showKnowledge && (
            <KnowledgeLibrary
              key={"knowledge:" + id}
              groupId={id}
              groupName={directory.get(target)?.name || undefined}
            />
          )}
          {knowledgePage && <GroupBrain key={id} groupId={id} />}
        </>
      )}
    </>
  );
}
