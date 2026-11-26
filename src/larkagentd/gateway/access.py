"""Gateway admission and dashboard authentication. No external calls under DB locks."""

import hashlib
import re
import secrets
from urllib.parse import urlsplit

from sqlalchemy import case, delete, select, text, update
from sqlalchemy.dialects.postgresql import insert

from ..core.diagnostics import actor_ref, emit
from ..core.fields import normalize_email
from ..core.scopes import DEFAULT_GROUP_TOOLS, is_scopable
from ..core.store import (
    dashboard_limits,
    dashboard_logins,
    dashboard_managers,
    dashboard_sessions,
    database_time,
    gateway_identities,
    gateway_members,
    gateway_policies,
    tool_registry,
    workspace_settings,
)
from ..knowledge.dashboard import KnowledgeDashboard
from ..knowledge.library import GLOBAL, mcp_settings, serve_sources
from ..knowledge.models import GroupBrainConfig
from ..knowledge.store import KnowledgeError, KnowledgeStore

ACCESS_LOCK = 0x4D415844
LOGIN_TTL = 300
SESSION_TTL = 7 * 86400
IDENTITY_TTL = 300
MODES = {"all_users", "specific_users"}
MAX_SCOPE_TOOLS = 200


class AccessError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def email_address(value):
    try:
        if not isinstance(value, str) or len(value) > 254:
            raise ValueError
        return normalize_email(value)
    except ValueError:
        raise AccessError(400, "invalid_email") from None


def admin_emails(value):
    try:
        emails = tuple(
            sorted(
                {
                    email_address(part.strip())
                    for part in value.split(",")
                    if part.strip()
                }
            )
        )
        if not emails:
            raise ValueError
        return emails
    except (ValueError, AccessError):
        raise ValueError(
            "DASHBOARD_ADMIN_EMAILS requires at least one valid email."
        ) from None


def public_origin(value):
    try:
        url = urlsplit(value)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in ("", "/")
            or url.query
            or url.fragment
            or (
                url.scheme == "http"
                and url.hostname not in ("localhost", "127.0.0.1", "::1")
            )
        ):
            raise ValueError
        port = url.port
        host = url.hostname.lower()
        host = f"[{host}]" if ":" in host else host
        suffix = (
            f":{port}"
            if port and port != (443 if url.scheme == "https" else 80)
            else ""
        )
        return f"{url.scheme}://{host}{suffix}"
    except ValueError:
        raise ValueError(
            "DASHBOARD_PUBLIC_URL must be an HTTPS origin, or HTTP on loopback."
        ) from None


def group_scope(group_id):
    if not isinstance(group_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,256}", group_id
    ):
        raise AccessError(400, "invalid_group_id")
    return "group:" + group_id


def token_hash(token):
    if not isinstance(token, str) or not 1 <= len(token) <= 128:
        raise AccessError(401, "unauthorized")
    return hashlib.sha256(token.encode()).hexdigest()


class Access:
    def __init__(self, store, admins=(), group_brain=None, runtime=None):
        self.store = store
        self.admins = frozenset(email_address(email) for email in admins)
        # Group Brain's dashboard routes. Built with the default, disabled configuration unless
        # the Gateway hands over its own.
        self.knowledge_runtime = runtime
        self.knowledge = KnowledgeDashboard(
            group_brain or GroupBrainConfig(), runtime=runtime
        )
        self.knowledge_store = KnowledgeStore()
        self.knowledge_config = group_brain or GroupBrainConfig()
        self.knowledge_mcp = mcp_settings()

    def lock(self, conn):
        # A short transaction lock serializes role revocation, auth, and policy changes.
        # User transactions acquire their owner lock first; HTTP never takes owner locks.
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ACCESS_LOCK})

    def now(self, conn):
        return float(conn.scalar(select(database_time())))

    def role(self, conn, email):
        if email in self.admins:
            return "admin"
        if email and conn.scalar(
            select(dashboard_managers.c.email).where(
                dashboard_managers.c.email == email
            )
        ):
            return "manager"
        return None

    def actor_email(self, conn, owner):
        return (
            conn.scalar(
                select(gateway_identities.c.email).where(
                    gateway_identities.c.owner == owner,
                    gateway_identities.c.verified_at > database_time() - IDENTITY_TTL,
                )
            )
            or None
        )

    def resolve(self, owner, transport, *, force=False):
        """Only this verified adapter lookup may populate admission identities."""
        with self.store.engine.begin() as conn:
            row = (
                conn.execute(
                    select(gateway_identities).where(
                        gateway_identities.c.owner == owner
                    )
                )
                .mappings()
                .first()
            )
            now = self.now(conn)
        if (
            not force
            and row
            and now - row["verified_at"] < (IDENTITY_TTL if row["email"] else 30)
        ):
            return row["email"] or None
        try:
            found = email_address(transport.email_of(owner.split(":", 1)[1]))
        except Exception as exc:
            emit("access.identity_unavailable", error_type=type(exc).__name__)
            found = ""
        with self.store.engine.begin() as conn:
            conn.execute(
                insert(gateway_identities)
                .values(owner=owner, email=found, verified_at=database_time())
                .on_conflict_do_update(
                    index_elements=[gateway_identities.c.owner],
                    set_={"email": found, "verified_at": database_time()},
                )
            )
        return found or None

    def verify_for_admission(
        self, owner, transport, *, group=None, force=False, governance=None
    ):
        """Verify email only for dashboard, scoped group policy or quota checks."""
        with self.store.engine.begin() as conn:
            group_mode = (
                self.mode(conn, group_scope(group)) if group is not None else None
            )
            if group is not None and (group_mode is None or self.paused(conn)):
                return
            needed = (
                force
                or group_mode == "specific_users"
                or (governance is not None and governance.needs_email(conn))
            )
        if needed:
            self.resolve(owner, transport, force=force)

    def paused(self, conn):
        return bool(conn.scalar(select(workspace_settings.c.bot_paused)))

    def bot_access(self, conn, actor, method, body):
        """Workspace-wide pause, independent of group policy or dashboard role access."""
        self.lock(conn)
        if method == "GET":
            return 200, {"paused": self.paused(conn)}
        if method != "PATCH":
            raise AccessError(405, "method_not_allowed")
        if actor["role"] not in ("admin", "manager"):
            raise AccessError(403, "dashboard_role_required")
        self.fields(body, {"paused"})
        paused = body["paused"]
        if not isinstance(paused, bool):
            raise AccessError(400, "invalid_value")
        conn.execute(
            insert(workspace_settings)
            .values(singleton=True, bot_paused=paused)
            .on_conflict_do_update(
                index_elements=[workspace_settings.c.singleton],
                set_={"bot_paused": paused},
            )
        )
        emit("dashboard.bot_access", actor=actor_ref(actor["owner"]), paused=paused)
        return 200, {"paused": paused}

    def verify_setup_member(self, owner, group, transport):
        """Only a current member of the group that authorized setup can connect."""
        if not group:
            return False
        with self.store.engine.begin() as conn:
            if not self.mode(conn, group_scope(group)) or self.paused(conn):
                return False
        self.verify_for_admission(owner, transport, group=group)
        try:
            return transport.group_member(group, owner.split(":", 1)[1]) is True
        except Exception as exc:
            emit("access.membership_unavailable", error_type=type(exc).__name__)
            return False

    def mode(self, conn, scope):
        return conn.scalar(
            select(gateway_policies.c.mode).where(gateway_policies.c.scope == scope)
        )

    def allowed_scope(self, conn, scope, email):
        mode = self.mode(conn, scope)
        return mode == "all_users" or (
            mode == "specific_users"
            and bool(
                self.role(conn, email)
                or (
                    email
                    and conn.scalar(
                        select(gateway_members.c.email).where(
                            gateway_members.c.scope == scope,
                            gateway_members.c.email == email,
                        )
                    )
                )
            )
        )

    def admission(self, conn, owner, group):
        self.lock(conn)
        if not group or not self.mode(conn, scope := group_scope(group)):
            return "unregistered_group"
        if self.paused(conn):
            return "disabled"
        if not self.allowed_scope(conn, scope, self.actor_email(conn, owner)):
            return "not_allowed"
        return None

    def roles(self, conn):
        inherited = {email: "admin" for email in self.admins}
        for email in conn.scalars(select(dashboard_managers.c.email)):
            inherited.setdefault(email, "manager")
        return [
            {
                "email": email,
                "role": inherited[email],
                "managed_by_config": email in self.admins,
            }
            for email in sorted(inherited)
        ]

    def scopable_tools(self, conn):
        """The registry tools a group scope can switch: implemented, not retired, and neither
        the terminal nor an always-available skill tool."""
        rows = conn.execute(
            select(tool_registry)
            .where(tool_registry.c.implemented, ~tool_registry.c.retired)
            .order_by(tool_registry.c.kind, tool_registry.c.name)
        ).mappings()
        return [
            {
                "name": row["name"],
                "kind": row["kind"],
                "risk": row["risk"],
                "description": row["description"],
                "label": row["label"] or row["name"],
                "authority": row["authority"],
                "activity_kind": row["activity_kind"],
                "default": row["name"] in DEFAULT_GROUP_TOOLS,
                "enabled_globally": bool(row["enabled"]),
            }
            for row in rows
            if is_scopable(row["name"])
        ]

    def policy(self, conn, scope):
        mode = self.mode(conn, scope)
        if mode is None:
            raise AccessError(404, "group_not_found")
        names = {tool["name"] for tool in self.scopable_tools(conn)}
        return {
            "group_id": scope.removeprefix("group:"),
            "mode": mode,
            "members": list(
                conn.scalars(
                    select(gateway_members.c.email)
                    .where(gateway_members.c.scope == scope)
                    .order_by(gateway_members.c.email)
                )
            ),
            "inherited_members": self.roles(conn),
            "tools": sorted(self.store.scope_tools(conn, scope) & names),
        }

    def limit(self, conn, key, seconds, maximum):
        now = self.now(conn)
        row = conn.execute(
            insert(dashboard_limits)
            .values(key=key, until=now + seconds, count=1)
            .on_conflict_do_update(
                index_elements=[dashboard_limits.c.key],
                set_={
                    "until": case(
                        (dashboard_limits.c.until <= now, now + seconds),
                        else_=dashboard_limits.c.until,
                    ),
                    "count": case(
                        (dashboard_limits.c.until <= now, 1),
                        else_=dashboard_limits.c.count + 1,
                    ),
                },
            )
            .returning(dashboard_limits.c.count)
        ).scalar_one()
        return row <= maximum

    def exchange_limit(self, client_ip):
        with self.store.engine.begin() as conn:
            allowed = self.limit(
                conn,
                "exchange:" + hashlib.sha256(client_ip.encode()).hexdigest(),
                60,
                30,
            )
        if not allowed:
            raise AccessError(429, "rate_limited")

    def issue(self, conn, owner, scope=None):
        self.lock(conn)
        email = self.actor_email(conn, owner)
        if scope:
            if not self.knowledge_store.registered(conn, scope):
                raise AccessError(403, "dashboard_role_required")
        elif not self.role(conn, email):
            raise AccessError(403, "dashboard_role_required")
        if not self.limit(conn, "issue:" + owner, 30, 1):
            raise AccessError(429, "rate_limited")
        conn.execute(delete(dashboard_logins).where(dashboard_logins.c.owner == owner))
        token = secrets.token_urlsafe(32)
        digest = token_hash(token)
        expires = self.now(conn) + LOGIN_TTL
        conn.execute(
            insert(dashboard_logins).values(
                token_hash=digest,
                owner=owner,
                email=email or "",
                expires=expires,
                group_scope=scope,
            )
        )
        return token, digest

    def pending(self, conn, digest):
        self.lock(conn)
        row = (
            conn.execute(
                select(dashboard_logins).where(
                    dashboard_logins.c.token_hash == digest,
                    dashboard_logins.c.expires > database_time(),
                )
            )
            .mappings()
            .first()
        )
        return bool(row and self.dashboard_role(conn, row))

    def exchange(self, conn, token):
        self.lock(conn)
        row = (
            conn.execute(
                select(dashboard_logins).where(
                    dashboard_logins.c.token_hash == token_hash(token),
                    dashboard_logins.c.expires > database_time(),
                )
            )
            .mappings()
            .first()
        )
        if not row or not (role := self.dashboard_role(conn, row)):
            raise AccessError(401, "unauthorized")
        conn.execute(
            delete(dashboard_logins).where(
                dashboard_logins.c.token_hash == row["token_hash"]
            )
        )
        session = secrets.token_urlsafe(32)
        expires = self.now(conn) + SESSION_TTL
        conn.execute(
            insert(dashboard_sessions).values(
                token_hash=token_hash(session),
                owner=row["owner"],
                email=row["email"],
                expires=expires,
                group_scope=row["group_scope"],
            )
        )
        return session, {
            "email": row["email"],
            "role": role,
            "group_id": row["group_scope"].removeprefix("group:")
            if row["group_scope"]
            else None,
            "expires_at": expires,
        }

    def authenticate(self, conn, token):
        self.lock(conn)
        row = (
            conn.execute(
                select(dashboard_sessions).where(
                    dashboard_sessions.c.token_hash == token_hash(token),
                    dashboard_sessions.c.expires > database_time(),
                )
            )
            .mappings()
            .first()
        )
        if not row or not (role := self.dashboard_role(conn, row)):
            raise AccessError(401, "unauthorized")
        return {
            "email": row["email"],
            "owner": row["owner"],
            "role": role,
            "group_id": row["group_scope"].removeprefix("group:")
            if row["group_scope"]
            else None,
            "expires_at": row["expires"],
        }

    def dashboard_role(self, conn, row):
        if scope := row["group_scope"]:
            return (
                "group_admin" if self.knowledge_store.registered(conn, scope) else None
            )
        return self.role(conn, row["email"])

    def scoped_request(self, actor, parts, method):
        group = actor.get("group_id")
        if not group:
            return
        if parts in (["auth", "session"], ["auth", "logout"]):
            return
        if parts[:3] != ["groups", group, "knowledge"]:
            raise AccessError(403, "group_dashboard_scope")
        rest = parts[3:]
        if rest in (["sources"], ["learned"]) or (
            len(rest) == 3
            and rest[0] == "learned"
            and rest[2] == "retire"
            and method == "POST"
        ):
            return
        raise AccessError(403, "group_dashboard_scope")

    def dispatch(self, conn, actor, method, parts, body, query=""):
        """Called in the transaction that authenticated the session."""
        self.scoped_request(actor, parts, method)
        if parts == ["knowledge", "sources"]:
            if actor["role"] != "admin":
                raise AccessError(403, "admin_required")
            try:
                return serve_sources(conn, GLOBAL, method, body, self.knowledge_mcp)
            except KnowledgeError as exc:
                raise AccessError(exc.status, exc.code) from None
        if parts == ["roles"] and method == "GET":
            return 200, self.roles(conn)
        if parts == ["bot-access"]:
            return self.bot_access(conn, actor, method, body)
        if parts == ["tools"] and method == "GET":
            return 200, self.scopable_tools(conn)
        if len(parts) == 2 and parts[0] == "managers" and method in ("PUT", "DELETE"):
            if actor["role"] != "admin":
                raise AccessError(403, "admin_required")
            email = email_address(parts[1])
            if email in self.admins:
                raise AccessError(409, "managed_by_config")
            if method == "PUT":
                conn.execute(
                    insert(dashboard_managers)
                    .values(email=email, added_by=actor["owner"])
                    .on_conflict_do_nothing()
                )
            else:
                conn.execute(
                    delete(dashboard_managers).where(
                        dashboard_managers.c.email == email
                    )
                )
                conn.execute(
                    delete(dashboard_sessions).where(
                        dashboard_sessions.c.email == email
                    )
                )
                conn.execute(
                    delete(dashboard_logins).where(dashboard_logins.c.email == email)
                )
            return 204, None
        if parts == ["groups"]:
            if method == "GET":
                return 200, [
                    self.policy(conn, scope)
                    for scope in conn.scalars(
                        select(gateway_policies.c.scope).order_by(
                            gateway_policies.c.scope
                        )
                    )
                ]
            if method == "POST":
                if actor["role"] != "admin":
                    raise AccessError(403, "admin_required")
                self.fields(body, {"group_id"})
                scope = group_scope(body.get("group_id"))
                if self.mode(conn, scope):
                    raise AccessError(409, "group_exists")
                conn.execute(
                    insert(gateway_policies).values(scope=scope, mode="all_users")
                )
                # A new group starts with the common tools only; an administrator turns on the rest.
                self.store.set_scope_tools(conn, scope, DEFAULT_GROUP_TOOLS)
                self.knowledge_store.update_settings(
                    conn,
                    scope,
                    0,
                    {
                        "enabled": True,
                        "recall_enabled": True,
                        "memory_enabled": True,
                        "documents_enabled": False,
                        "learning_mode": "explicit",
                        "notice_ref": "Group history is searched on request.",
                        "policy_ref": "Group admission",
                    },
                    actor["email"],
                    max_history_days=self.knowledge_config.max_history_days,
                )
                return 201, self.policy(conn, scope)
        if len(parts) >= 2 and parts[0] == "groups":
            scope, rest = group_scope(parts[1]), parts[2:]
        else:
            raise AccessError(404, "not_found")
        self.policy(conn, scope)
        if not rest:
            if method in ("PATCH", "DELETE") and actor["role"] != "admin":
                raise AccessError(403, "admin_required")
            if method == "GET":
                return 200, self.policy(conn, scope)
            if method == "PATCH":
                self.fields(body, {"mode"})
                if not isinstance(body.get("mode"), str) or body["mode"] not in MODES:
                    raise AccessError(400, "invalid_mode")
                conn.execute(
                    update(gateway_policies)
                    .where(gateway_policies.c.scope == scope)
                    .values(mode=body["mode"])
                )
                return 200, self.policy(conn, scope)
            if method == "DELETE":
                if self.knowledge_store.has_data(conn, scope):
                    # Group Brain still holds this group's knowledge: purge it first, so its vectors
                    # and the record of the purge are not orphaned by the cascade.
                    raise AccessError(409, "group_brain_data_present")
                conn.execute(
                    delete(gateway_policies).where(gateway_policies.c.scope == scope)
                )
                return 204, None
        if rest and rest[0] == "knowledge":
            try:
                status, result = self.knowledge.handle(
                    conn, actor, scope, rest[1:], method, body, query
                )
            except KnowledgeError as exc:
                raise AccessError(exc.status, exc.code) from None
            if method != "GET":
                emit(
                    "dashboard.group_brain_changed",
                    actor=actor_ref(actor["owner"]),
                    action="/".join(rest[1:]) or method.lower(),
                )
            return status, result
        if rest == ["tools"] and method == "PUT":
            if actor["role"] != "admin":
                raise AccessError(403, "admin_required")
            self.fields(body, {"tools"})
            requested = body["tools"]
            known = {tool["name"] for tool in self.scopable_tools(conn)}
            if (
                not isinstance(requested, list)
                or len(requested) > MAX_SCOPE_TOOLS
                or not all(
                    isinstance(name, str) and name in known for name in requested
                )
            ):
                raise AccessError(400, "invalid_tool")
            self.store.set_scope_tools(conn, scope, requested)
            emit(
                "dashboard.group_tools_set",
                actor=actor_ref(actor["owner"]),
                tools=len(set(requested)),
            )
            return 200, self.policy(conn, scope)
        if len(rest) == 2 and rest[0] == "members" and method in ("PUT", "DELETE"):
            email = email_address(rest[1])
            if self.role(conn, email):
                raise AccessError(409, "inherited_member")
            if method == "PUT":
                conn.execute(
                    insert(gateway_members)
                    .values(scope=scope, email=email, added_by=actor["owner"])
                    .on_conflict_do_nothing()
                )
            else:
                conn.execute(
                    delete(gateway_members).where(
                        gateway_members.c.scope == scope,
                        gateway_members.c.email == email,
                    )
                )
            return 204, None
        raise AccessError(405, "method_not_allowed")

    @staticmethod
    def fields(body, allowed):
        if set(body) != allowed:
            raise AccessError(400, "invalid_fields")
