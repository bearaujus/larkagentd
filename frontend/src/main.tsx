import { StrictMode, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import {
  BrowserRouter,
  Link,
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router";
import { Dialog, Tooltip } from "radix-ui";
import {
  ArrowRight,
  ChartNoAxesCombined,
  LayoutDashboard,
  LogOut,
  Menu,
  ShieldCheck,
  Users,
  UsersRound,
  X,
} from "lucide-react";
import {
  ApiError,
  api,
  clearPrivateState,
  client,
  session as readSession,
  write,
} from "./api";
import { Badge, Failure, Hint, Loading, Notice, useAction } from "./components";
import { Identity, useDirectory } from "./directory";
import { KnowledgeLibrary } from "./knowledge-library";
import { GroupDetail, Groups, People } from "./policies";
import { Usage } from "./usage";
import { Governance } from "./governance";
import type { Session } from "./types";
import "./styles.css";

function Login({
  onLogin,
  expired,
}: {
  onLogin: (value: Session) => void;
  expired: boolean;
}) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  return (
    <main className="login-page">
      <div className="login-brand">
        <span className="brand-icon">
          <img src="/dashboard/larkagentd.svg" alt="" />
        </span>
        larkagentd<span>OPERATIONS</span>
      </div>
      <div className="login-card">
        <span className="eyebrow">YOUR ASSISTANT, IN VIEW</span>
        <h1>Welcome to operations.</h1>
        <p>
          Manage access. Understand usage.
          <br />
          Keep your assistant running clearly.
        </p>
        <div className="login-instruction">
          <span className="step">1</span>
          <div>
            Open a direct message with the Lark bot.
            <br />
            Send <code>/dashboard login</code>
          </div>
        </div>
        <div className="login-instruction">
          <span className="step">2</span>
          <div>
            Paste the one-use token below.
            <br />
            <span className="muted">Tokens expire after five minutes.</span>
          </div>
        </div>
        {expired && (
          <Notice>
            Your session ended. Request a new login token in Lark.
          </Notice>
        )}
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError(undefined);
            const submitted = token.trim();
            setToken("");
            try {
              await api<Session>("/auth/exchange", {
                method: "POST",
                body: JSON.stringify({ token: submitted }),
              });
              onLogin(await readSession());
            } catch (err) {
              setError(err);
            } finally {
              setBusy(false);
            }
          }}
        >
          <label>
            Login token
            <input
              type="password"
              value={token}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              required
              placeholder="Paste your token"
              onChange={(e) => setToken(e.target.value)}
            />
          </label>
          {error instanceof ApiError && error.status === 401 ? (
            <Notice tone="error">
              This token is invalid, expired or already used. Send /dashboard
              login in Lark for a new one.
            </Notice>
          ) : error ? (
            <Failure error={error} />
          ) : null}
          <button
            className="primary login-submit"
            disabled={busy || !token.trim()}
          >
            {busy ? "Signing in..." : "Open dashboard"}
            <ArrowRight size={16} />
          </button>
        </form>
        <div className="login-security">
          <ShieldCheck size={16} />
          <span>
            Admins and managers only. Your browser session lasts seven days
            unless revoked or signed out.
          </span>
        </div>
      </div>
      <p className="login-footer">larkagentd · Access & activity</p>
    </main>
  );
}
const navItems = [
  ["/", "Overview", LayoutDashboard],
  ["/people", "People", Users],
  ["/groups", "Groups", UsersRound],
  ["/usage", "Usage", ChartNoAxesCombined],
  ["/governance", "Governance", ShieldCheck],
  ["/knowledge", "Knowledge", UsersRound],
] as const;
function Shell({ session }: { session: Session }) {
  const location = useLocation();
  const [open, setOpen] = useState(false);
  const action = useAction();
  const target = { kind: "email" as const, key: session.email };
  const directory = useDirectory([target]);
  useEffect(() => {
    setOpen(false);
    window.scrollTo(0, 0);
  }, [location.pathname]);
  const navigation = (
    <>
      <Link className="brand" to="/">
        <span className="brand-icon">
          <img src="/dashboard/larkagentd.svg" alt="" />
        </span>
        <span>
          larkagentd<small>OPERATIONS</small>
        </span>
      </Link>
      <div className="nav-label">WORKSPACE</div>
      <nav aria-label="Main navigation">
        {navItems
          .filter(([to]) => to !== "/knowledge" || session.role === "admin")
          .map(([to, label, Icon]) => (
            <NavLink key={to} end={to === "/"} to={to}>
              <Icon size={18} />
              {label}
            </NavLink>
          ))}
      </nav>
      <div className="sidebar-bottom">
        <div className="sidebar-note">
          <Hint content="Only admins and managers can open this dashboard. Chat membership does not grant dashboard or cloud/tool access.">
            <button
              type="button"
              className="icon-button"
              aria-label="About dashboard access"
            >
              <ShieldCheck size={17} />
            </button>
          </Hint>
          <span>
            Access is managed
            <br />
            by your workspace.
          </span>
        </div>
      </div>
    </>
  );
  return (
    <div className="app">
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <aside className="sidebar">{navigation}</aside>
      <div className="workspace">
        <header className="topbar">
          <Dialog.Root open={open} onOpenChange={setOpen}>
            <Dialog.Trigger asChild>
              <button
                className="icon-button mobile-menu"
                aria-label="Open navigation"
              >
                <Menu size={21} />
              </button>
            </Dialog.Trigger>
            <Dialog.Portal>
              <Dialog.Overlay className="overlay" />
              <Dialog.Content className="nav-drawer">
                <Dialog.Title className="sr-only">Navigation</Dialog.Title>
                <Dialog.Description className="sr-only">
                  Dashboard sections
                </Dialog.Description>
                <Dialog.Close
                  className="icon-button drawer-close"
                  aria-label="Close navigation"
                >
                  <X size={20} />
                </Dialog.Close>
                {navigation}
              </Dialog.Content>
            </Dialog.Portal>
          </Dialog.Root>
          <span className="topbar-title">Assistant operations</span>
          <div className="account">
            <Identity
              target={target}
              metadata={directory.get(target)}
              refresh={directory.refresh}
              secondary={false}
            />
            <Badge>{session.role}</Badge>
            <Hint content="Sign out of this dashboard session. Account connections stay active.">
              <button
                className="icon-button"
                disabled={action.busy}
                aria-label="Sign out"
                onClick={() =>
                  void action.run(async () => {
                    await write("/auth/logout", "POST");
                    clearPrivateState();
                  }, "Signed out.")
                }
              >
                <LogOut size={17} />
              </button>
            </Hint>
          </div>
        </header>
        <main id="main" tabIndex={-1}>
          {action.feedback}
          <Routes>
            <Route path="/" element={<Usage key="overview" overview />} />
            <Route path="/overview" element={<Navigate to="/" replace />} />
            <Route path="/login" element={<Navigate to="/" replace />} />
            <Route path="/people" element={<People session={session} />} />
            <Route path="/groups" element={<Groups />} />
            {session.role === "admin" && (
              <Route path="/knowledge" element={<KnowledgeLibrary />} />
            )}
            <Route
              path="/groups/:id"
              element={<GroupDetail showKnowledge={session.role === "admin"} />}
            />
            <Route
              path="/governance"
              element={<Governance session={session} />}
            />
            <Route path="/usage" element={<Usage key="usage" />} />
            <Route
              path="*"
              element={
                <>
                  <h1>Page not found</h1>
                  <Link to="/">Return to overview</Link>
                </>
              }
            />
          </Routes>
        </main>
        <footer className="app-footer">
          <span>larkagentd</span>
          <span>All report times are UTC · Usage statistics only</span>
        </footer>
      </div>
    </div>
  );
}
function GroupKnowledge({ groupId }: { groupId: string }) {
  const action = useAction();
  return (
    <main className="group-knowledge-page">
      <header>
        <img src="/dashboard/larkagentd.svg" width={42} height={42} alt="" />
        <h1>Group knowledge</h1>
        <button
          className="button"
          onClick={() =>
            void action.run(async () => {
              await write("/auth/logout", "POST");
              clearPrivateState();
            }, "Signed out.")
          }
        >
          Sign out
        </button>
      </header>
      <KnowledgeLibrary groupId={groupId} />
      {action.feedback}
    </main>
  );
}

function App() {
  const hadSession = useRef(false);
  const [current, setCurrent] = useState<Session | null>(null);
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState<unknown>();
  const [expired, setExpired] = useState(false);
  async function check() {
    try {
      setCurrent(await readSession());
      hadSession.current = true;
      setError(undefined);
    } catch (err) {
      if (
        !(err instanceof ApiError && err.status === 401) &&
        !(err instanceof DOMException && err.name === "AbortError")
      )
        setError(err);
    } finally {
      setChecking(false);
    }
  }
  useEffect(() => {
    const signedOut = () => {
      setCurrent(null);
      setChecking(false);
      setExpired(hadSession.current);
      setError(undefined);
    };
    window.addEventListener("dashboard-signed-out", signedOut);
    void check();
    return () => window.removeEventListener("dashboard-signed-out", signedOut);
  }, []);
  useEffect(() => {
    if (!current) return;
    const timeout = setTimeout(
      clearPrivateState,
      Math.max(0, current.expires_at * 1000 - Date.now()),
    );
    const refresh = () => {
      if (document.visibilityState === "visible") void check();
    };
    const interval = setInterval(refresh, 60_000);
    window.addEventListener("focus", refresh);
    return () => {
      clearTimeout(timeout);
      clearInterval(interval);
      window.removeEventListener("focus", refresh);
    };
  }, [current]);
  if (checking)
    return (
      <main className="auth-check">
        <Loading label="Checking your session" />
      </main>
    );
  if (!current && error)
    return (
      <main className="auth-check">
        <h1>Unable to check access</h1>
        <Failure error={error} retry={() => void check()} />
      </main>
    );
  if (current?.group_id)
    return <GroupKnowledge key={current.group_id} groupId={current.group_id} />;
  return current ? (
    <Shell key={current.email} session={current} />
  ) : (
    <Login
      expired={expired}
      onLogin={(value) => {
        hadSession.current = true;
        setExpired(false);
        setError(undefined);
        setCurrent(value);
      }}
    />
  );
}
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <Tooltip.Provider delayDuration={250} skipDelayDuration={100}>
        <BrowserRouter basename="/dashboard">
          <App />
        </BrowserRouter>
      </Tooltip.Provider>
    </QueryClientProvider>
  </StrictMode>,
);
