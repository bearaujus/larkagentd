import hashlib
import uuid
from contextlib import contextmanager

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    case,
    cast,
    create_engine,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert

from .database import database_url

# Arbitrary fixed key for the Postgres advisory lock that serializes agent admission.
ADMISSION_LOCK = 0x4D415841


def database_time():
    """Lease and persistence time must not depend on an individual worker's clock."""
    return func.extract("epoch", func.clock_timestamp())


metadata = MetaData()
users = Table(
    "users",
    metadata,
    Column("id", String, primary_key=True),
    Column("state", JSON, nullable=False),
)
events = Table("events", metadata, Column("id", String, primary_key=True))
gateway_policies = Table(
    "gateway_policies",
    metadata,
    Column("scope", String, primary_key=True),
    Column("mode", String, nullable=False),
    CheckConstraint(
        "mode IN ('all_users', 'specific_users')",
        name="gateway_policies_mode_check",
    ),
    CheckConstraint(
        "scope ~ '^group:[A-Za-z0-9_-]+$' AND length(scope) <= 262",
        name="gateway_policies_group_scope",
    ),
)
workspace_settings = Table(
    "workspace_settings",
    metadata,
    Column("singleton", Boolean, primary_key=True, server_default=text("true")),
    Column("bot_paused", Boolean, nullable=False, server_default=text("false")),
    CheckConstraint("singleton", name="workspace_settings_singleton"),
)
gateway_members = Table(
    "gateway_members",
    metadata,
    Column(
        "scope",
        String,
        ForeignKey("gateway_policies.scope", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("email", String, primary_key=True),
    Column("added_by", String, nullable=False, server_default=""),
)
# One row means the tool is enabled for that group; no row means it is not.
gateway_scope_tools = Table(
    "gateway_scope_tools",
    metadata,
    Column(
        "scope",
        String,
        ForeignKey("gateway_policies.scope", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("tool", String, primary_key=True),
)
dashboard_managers = Table(
    "dashboard_managers",
    metadata,
    Column("email", String, primary_key=True),
    Column("added_by", String, nullable=False, server_default=""),
)
gateway_identities = Table(
    "gateway_identities",
    metadata,
    Column("owner", String, primary_key=True),
    Column("email", String, nullable=False),
    Column("verified_at", Float, nullable=False),
)
dashboard_logins = Table(
    "dashboard_logins",
    metadata,
    Column("token_hash", String, primary_key=True),
    Column("owner", String, nullable=False, index=True),
    Column("email", String, nullable=False, index=True),
    Column("expires", Float, nullable=False),
    Column("group_scope", String),
)
dashboard_sessions = Table(
    "dashboard_sessions",
    metadata,
    Column("token_hash", String, primary_key=True),
    Column("owner", String, nullable=False),
    Column("email", String, nullable=False, index=True),
    Column("expires", Float, nullable=False),
    Column("group_scope", String),
)
dashboard_limits = Table(
    "dashboard_limits",
    metadata,
    Column("key", String, primary_key=True),
    Column("until", Float, nullable=False),
    Column("count", Integer, nullable=False),
)
directory_metadata = Table(
    "directory_metadata",
    metadata,
    Column("kind", String, primary_key=True),
    Column("key", String, primary_key=True),
    Column("data", JSON, nullable=False),
    Column("status", String, nullable=False),
    Column("checked_at", Float, nullable=False),
    Column("expires", Float, nullable=False),
)
USAGE_COUNTS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
governance_settings = Table(
    "governance_settings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("policy", JSONB, nullable=False),
    CheckConstraint("id = 1"),
)
governance_flex = Table(
    "governance_flex",
    metadata,
    Column("email", String, primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("active_limit", BigInteger),
    Column("pending_limit", BigInteger),
)
governance_rates = Table(
    "governance_rates",
    metadata,
    Column("model", String, primary_key=True),
    Column("effective_at", DateTime(timezone=True), primary_key=True),
    Column("rates", JSONB, nullable=False),
)
governance_history = Table(
    "governance_history",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column(
        "occurred_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    ),
    Column("actor_email", String, nullable=False),
    Column("resource", String, nullable=False),
    Column("target", String, nullable=False),
    Column("before", JSONB, nullable=False),
    Column("after", JSONB, nullable=False),
)
# What each turn used, per Hermes session (one per attempt). `requester` is the person whose
# message caused the turn (who is charged); `principal` is whose credentials ran it. input is
# the uncached part of the prompt; cache_read and cache_write are the cached part; reasoning
# is a part of output, shown apart.
token_usage = Table(
    "token_usage",
    metadata,
    Column("session_id", String, primary_key=True),
    Column("job_id", String, nullable=False),
    Column("requester", String, nullable=False),
    Column("principal", String, nullable=False),
    Column("chat_type", String, nullable=False),
    Column("chat_id", String, nullable=False, server_default=""),
    Column("thread", String, nullable=False, server_default=""),
    Column("model", String, nullable=False, server_default=""),
    *(
        Column(name, BigInteger, nullable=False, server_default="0")
        for name in USAGE_COUNTS
    ),
    Column("source", String, nullable=False, server_default="session"),
    # True when the person chose the model for this thread (/model), so it is not the default.
    Column("model_override", Boolean, nullable=False, server_default=text("false")),
    # When the turn was recorded, and the UTC calendar month it counts toward. The quota of a
    # person starts again at zero when the month changes.
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column(
        "month",
        Date,
        nullable=False,
        server_default=text("(date_trunc('month', timezone('utc', now())))::date"),
    ),
    Column("quota_tier", String),
    Column("quota_tracker", String),
    Column("quota_cycle_start", Date),
    Column("estimated_cost_usd", Numeric(38, 14)),
    Index(
        "token_usage_quota_tracker",
        "quota_tier",
        "quota_cycle_start",
        "quota_tracker",
    ),
    Index("token_usage_requester_month", "requester", "month"),
    Index("token_usage_created_session", "created_at", "session_id"),
)
query_activity = Table(
    "query_activity",
    metadata,
    Column("id", String, primary_key=True),
    Column("session_id", String, nullable=False),
    Column("job_id", String, nullable=False),
    Column("requester", String, nullable=False),
    Column("principal", String, nullable=False),
    Column("operation", String, nullable=False, server_default="query"),
    Column("tool", String, nullable=False),
    Column("project", String, nullable=False, server_default=""),
    Column("status", String, nullable=False, server_default="incomplete"),
    Column("error_code", String),
    Column("duration_ms", BigInteger),
    Column("estimated_input_gb", Float),
    Column("row_count", BigInteger),
    Column("chat_type", String, nullable=False, server_default="unknown"),
    Column("chat_id", String),
    Column("thread", String),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("finished_at", DateTime(timezone=True)),
    Index("query_activity_requester_created", "requester", "created_at"),
    Index("query_activity_created", "created_at", "id"),
)
tool_registry = Table(
    "tool_registry",
    metadata,
    Column("name", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("label", String, nullable=False, server_default=""),
    Column("authority", String, nullable=False, server_default=""),
    Column("activity_kind", String, nullable=False, server_default=""),
    Column("risk", String, nullable=False),
    Column("description", String, nullable=False, server_default=""),
    Column("implemented", Boolean, nullable=False, server_default=text("true")),
    Column("enabled", Boolean, nullable=False),
    Column("retired", Boolean, nullable=False, server_default=text("false")),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)
# Explicit per-principal decisions that beat the tool's global flag. A principal is
# `user:<owner id>` today and can be `group:<chat id>` later.
tool_grants = Table(
    "tool_grants",
    metadata,
    Column("tool", String, primary_key=True),
    Column("principal", String, primary_key=True),
    Column("allowed", Boolean, nullable=False),
    Column("granted_by", String, nullable=False, server_default=""),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)
leases = Table(
    "leases",
    metadata,
    Column("id", String, primary_key=True),
    Column("owner", String, nullable=False),
    Column("until", Float, nullable=False),
)
jobs = Table(
    "jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("owner", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("claim", String),
    Column("until", Float, nullable=False, default=0),
    Column("available", Float, nullable=False, default=0),
)
messages = Table(
    "messages",
    metadata,
    Column("id", String, primary_key=True),
    Column("owner", String, nullable=False),
    Column("created", Float, nullable=False),
    Column("payload", JSON, nullable=False),
)
turns = Table(
    "turns",
    metadata,
    Column("session_id", String, primary_key=True),
    Column("job_id", String, nullable=False),
    Column("owner", String, nullable=False),
    Column("credential_version", String),
    Column("request", JSON, nullable=False),
    Column("status", String, nullable=False, default="running"),
    Column("expires", Float, nullable=False),
    Column("created", Float, nullable=False),
    # A tool needed a sign-in this owner does not have (the tool service sets it).
    Column(
        "needs_login",
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    ),
    # The Hermes run this turn started, so /stop can cancel it.
    Column("run_id", String),
    # The Lark chat and message this turn answers (migration 0013), written into the header of
    # every query it runs. Optional for internally created turns without an originating message.
    Column("chat_id", String),
    Column("message_id", String),
    Column("chat_type", String, nullable=False, server_default="unknown"),
    Column("thread", String),
)

# Rows of a table preview, encrypted, until the Gateway attaches them to the reply.
artifacts = Table(
    "artifacts",
    metadata,
    Column("id", String, primary_key=True),
    Column("owner", String, nullable=False),
    Column("session_id", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("expires", Float, nullable=False),
    Column("created", Float, nullable=False),
)
# Indexes for the queue, turn, artifact and message lookups.
Index("jobs_available", jobs.c.available, jobs.c.until)
Index("turns_job", turns.c.job_id, turns.c.created)
Index("artifacts_session", artifacts.c.session_id, artifacts.c.created)
Index("messages_owner_created", messages.c.owner, messages.c.created)


class Store:
    def __init__(self, url):
        self.engine = create_engine(
            database_url(url) if isinstance(url, str) else url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 3},
        )

    @contextmanager
    def user(self, owner, event=None):
        with self.engine.begin() as conn:
            if event and event.startswith("action:"):
                conn.execute(text("SET LOCAL lock_timeout = '500ms'"))
                conn.execute(text("SET LOCAL statement_timeout = '1500ms'"))
            if event:
                added = conn.execute(
                    insert(events)
                    .values(id=event)
                    .on_conflict_do_nothing()
                    .returning(events.c.id)
                ).scalar_one_or_none()
                if added is None:
                    yield None, None
                    return
            conn.execute(
                insert(users).values(id=owner, state={}).on_conflict_do_nothing()
            )
            state = dict(
                conn.execute(
                    select(users.c.state).where(users.c.id == owner).with_for_update()
                ).scalar_one()
            )
            yield conn, state
            conn.execute(update(users).where(users.c.id == owner).values(state=state))

    @contextmanager
    def card_delivery(self, owner):
        """Serialize message mutations across workers without holding a user row lock."""
        key = int.from_bytes(
            hashlib.sha256(owner.encode()).digest()[:8], "big", signed=True
        )
        with self.engine.connect() as conn:
            acquired = conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            ).scalar_one()
            conn.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    conn.commit()

    def add_job(self, conn, owner, kind, payload, available=0, delay=0):
        job_id = str(uuid.uuid4())
        conn.execute(
            insert(jobs).values(
                id=job_id,
                owner=owner,
                kind=kind,
                payload=payload,
                available=available or database_time() + delay,
                until=0,
            )
        )
        return job_id

    def update_waiting(self, conn, job_id, change):
        """Change the payload of a job that has not started; False when it already has.

        A job is waiting while it has never been claimed and its start time is still ahead (the
        batching window). A job that was claimed, failed and released for a retry keeps its
        claim, so it is not waiting: it may already have a stored turn that a retry reuses, and a
        message added now would be lost. change(payload) returns the new payload, or None to
        leave the job alone. The row lock keeps a worker from claiming it halfway.
        """
        now = conn.execute(
            select(text("extract(epoch from clock_timestamp())"))
        ).scalar_one()
        row = (
            conn.execute(
                select(jobs.c.payload)
                .where(
                    jobs.c.id == job_id,
                    jobs.c.claim.is_(None),
                    jobs.c.until < now,
                    jobs.c.available > now,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        payload = change(dict(row["payload"]))
        if payload is None:
            return False
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(payload=payload))
        return True

    def claim(self, only=None, exclude=None, cap=None):
        """Claim one due job, optionally limited to a kind.

        cap bounds how many jobs of that selection may run at once across all
        instances; admission is serialized so two workers cannot both take the last slot.
        """
        kinds = []
        if only:
            kinds.append(jobs.c.kind == only)
        if exclude:
            kinds.append(jobs.c.kind != exclude)
        with self.engine.begin() as conn:
            now = conn.execute(
                select(text("extract(epoch from clock_timestamp())"))
            ).scalar_one()
            if cap is not None:
                conn.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMISSION_LOCK}
                )
                running = conn.execute(
                    select(func.count())
                    .select_from(jobs)
                    .where(jobs.c.claim.is_not(None), jobs.c.until > now, *kinds)
                ).scalar_one()
                if running >= cap:
                    return None
            job = (
                conn.execute(
                    select(jobs)
                    .where(jobs.c.until < now, jobs.c.available <= now, *kinds)
                    .order_by(jobs.c.available, jobs.c.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if not job:
                return None
            claim = str(uuid.uuid4())
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(claim=claim, until=float(now) + 60)
            )
            return dict(job, claim=claim)

    def finish(self, conn, job, retry=None):
        condition = (jobs.c.id == job["id"]) & (jobs.c.claim == job["claim"])
        if retry is None:
            conn.execute(delete(jobs).where(condition))
        else:
            conn.execute(
                update(jobs)
                .where(condition)
                .values(until=0, available=database_time() + retry)
            )

    def email_of(self, owner):
        """The Lark email the Gateway saved for this owner, or None. Read-only: an unknown
        owner must not get a row."""
        with self.engine.begin() as conn:
            state = conn.execute(
                select(users.c.state).where(users.c.id == owner)
            ).scalar()
        email = (state or {}).get("email")
        return email if isinstance(email, str) else None

    def access_of(self, owner, version):
        """(identity type, role label) of the sign-in a credential version belongs to, or
        (None, None). Read-only. Empty when the person has signed in again since: a query's
        header must describe the access its own credentials came from, never a newer one."""
        with self.engine.begin() as conn:
            state = conn.execute(
                select(users.c.state).where(users.c.id == owner)
            ).scalar()
        state = state or {}
        if not version or state.get("credential_version") != version:
            return None, None
        identity = state.get("identity")
        kind = identity.get("identity_type") if isinstance(identity, dict) else None
        label = state.get("role_label")
        return (
            kind if isinstance(kind, str) else None,
            label if isinstance(label, str) else None,
        )

    def owns(self, conn, job):
        return (
            conn.execute(
                select(jobs.c.id)
                .where(
                    jobs.c.id == job["id"],
                    jobs.c.claim == job["claim"],
                    jobs.c.until > database_time(),
                )
                .with_for_update()
            ).first()
            is not None
        )

    def lease(self, name, owner, seconds=15):
        with self.engine.begin() as conn:
            now = float(
                conn.execute(
                    select(text("extract(epoch from clock_timestamp())"))
                ).scalar_one()
            )
            conn.execute(
                insert(leases)
                .values(id=name, owner=owner, until=now + seconds)
                .on_conflict_do_nothing()
            )
            return bool(
                conn.execute(
                    update(leases)
                    .where(
                        leases.c.id == name,
                        (leases.c.owner == owner) | (leases.c.until < now),
                    )
                    .values(owner=owner, until=now + seconds)
                ).rowcount
            )

    def history(self, conn, owner, limit, session=None, shared=False):
        """The last messages of an owner, oldest first. With a session (a thread), only the
        messages of that thread: each thread is its own conversation.

        shared is for a group thread: every person's messages of that thread, not only the
        owner's. It needs the thread, or it would return other people's whole history."""
        if shared and session is None:
            raise ValueError("a shared history needs a thread")
        query = select(messages.c.payload)
        if not shared:
            query = query.where(messages.c.owner == owner)
        if session is not None:
            query = query.where(
                cast(messages.c.payload, JSONB)["session"].astext == session
            )
        rows = (
            conn.execute(
                query.order_by(messages.c.created.desc(), messages.c.id.desc()).limit(
                    limit
                )
            )
            .scalars()
            .all()
        )
        return list(reversed(rows))

    def default_model(self, conn):
        """The model Hermes runs by default, as its latest answered turn reported it. Turns on
        a model somebody picked for their thread are left out. None before the first answer."""
        return conn.execute(
            select(token_usage.c.model)
            .where(
                token_usage.c.source == "session",
                token_usage.c.model_override.is_(False),
                token_usage.c.model.notin_(("", "hermes-agent")),
            )
            .order_by(token_usage.c.created_at.desc())
            .limit(1)
        ).scalar()

    def sessions_of_job(self, conn, job_id):
        """Every Hermes session a job used, finished or not: each attempt cost tokens."""
        return list(
            conn.execute(select(turns.c.session_id).where(turns.c.job_id == job_id))
            .scalars()
            .all()
        )

    def record_usage(self, conn, row):
        """One row per Hermes session. Recording it again (a retry of the job) replaces its
        counts, so a retry never counts twice."""
        statement = insert(token_usage).values(**row)
        conn.execute(
            statement.on_conflict_do_update(
                index_elements=["session_id"],
                set_={
                    **{
                        key: statement.excluded[key]
                        for key in (*USAGE_COUNTS, "source")
                    },
                    "estimated_cost_usd": case(
                        (
                            statement.excluded.source == "session",
                            statement.excluded.estimated_cost_usd,
                        ),
                        else_=token_usage.c.estimated_cost_usd,
                    ),
                    # Recover a previously unknown model without rewriting confirmed
                    # historical attribution when the deployment's default changes.
                    "model": case(
                        (
                            token_usage.c.model.in_(("", "hermes-agent")),
                            statement.excluded.model,
                        ),
                        else_=token_usage.c.model,
                    ),
                },
                where=(token_usage.c.source != "session")
                | (statement.excluded.source == "session"),
            )
        )

    def tokens_used(self, conn, requester):
        """Tokens a person used this UTC month: everything the model processed for their
        messages, cached input included, plus output."""
        month = cast(func.date_trunc("month", func.timezone("utc", func.now())), Date)
        total = func.coalesce(
            func.sum(
                token_usage.c.input_tokens
                + token_usage.c.cache_read_tokens
                + token_usage.c.cache_write_tokens
                + token_usage.c.output_tokens
            ),
            0,
        )
        return conn.execute(
            select(total).where(
                token_usage.c.requester == requester, token_usage.c.month == month
            )
        ).scalar_one()

    def usage_sum(self, conn, *conditions):
        """The five counts and the number of questions for the rows that match."""
        columns = [
            func.coalesce(func.sum(token_usage.c[name]), 0).label(name)
            for name in USAGE_COUNTS
        ]
        row = (
            conn.execute(
                select(
                    *columns,
                    func.count(func.distinct(token_usage.c.job_id)).label("turns"),
                ).where(*conditions)
            )
            .mappings()
            .one()
        )
        return {key: int(value) for key, value in row.items()}

    def usage_month(self, conn, requester):
        """A person's counts for the current UTC month (the quota's own month)."""
        month = cast(func.date_trunc("month", func.timezone("utc", func.now())), Date)
        return self.usage_sum(
            conn, token_usage.c.requester == requester, token_usage.c.month == month
        )

    def usage_thread(self, conn, thread, requester=None):
        """What one thread used, all months: every person's questions in it, or with
        `requester` only theirs. A group thread is one shared session, so its total is the
        session's."""
        conditions = [token_usage.c.thread == thread]
        if requester is not None:
            conditions.append(token_usage.c.requester == requester)
        return self.usage_sum(conn, *conditions)

    def usage_job(self, conn, job_id):
        """What one request used, every attempt of it."""
        return self.usage_sum(conn, token_usage.c.job_id == job_id)

    def sync_tool_registry(self, conn, entries):
        """Make the registry match the tools the code knows: (added, updated, retired) names.

        A new tool is inserted with the code's default. An existing one keeps the `enabled` flag
        an administrator set and takes the code's kind, risk, description and `implemented`,
        because the code, not the database, says how dangerous a tool is. A tool the code no
        longer has is retired, never deleted, so its grants survive a rollback."""
        known = {
            row["name"]: row for row in conn.execute(select(tool_registry)).mappings()
        }
        added, updated, seen = [], [], set()
        for entry in entries:
            name = entry["name"]
            seen.add(name)
            fields = dict(
                kind=entry["kind"],
                risk=entry["risk"],
                label=entry.get("label", name),
                authority=entry.get("authority", ""),
                activity_kind=entry.get("activity_kind", ""),
                description=entry["description"],
                implemented=entry["implemented"],
            )
            row = known.get(name)
            if row is None:
                conn.execute(
                    insert(tool_registry).values(
                        name=name, enabled=entry["enabled"], retired=False, **fields
                    )
                )
                added.append(name)
            elif row["retired"] or any(row[k] != v for k, v in fields.items()):
                conn.execute(
                    update(tool_registry)
                    .where(tool_registry.c.name == name)
                    .values(retired=False, updated_at=func.now(), **fields)
                )
                updated.append(name)
        retired = [n for n, r in known.items() if n not in seen and not r["retired"]]
        if retired:
            conn.execute(
                update(tool_registry)
                .where(tool_registry.c.name.in_(retired))
                .values(retired=True, updated_at=func.now())
            )
        return added, updated, retired

    def tool_row(self, conn, name):
        row = (
            conn.execute(select(tool_registry).where(tool_registry.c.name == name))
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def tool_grants_for(self, conn, name, principals):
        """{principal: allowed} for the explicit grants of one tool among these principals."""
        rows = conn.execute(
            select(tool_grants.c.principal, tool_grants.c.allowed).where(
                tool_grants.c.tool == name,
                tool_grants.c.principal.in_(list(principals)),
            )
        )
        return {principal: allowed for principal, allowed in rows}

    def set_tool_enabled(self, conn, name, enabled):
        done = conn.execute(
            update(tool_registry)
            .where(tool_registry.c.name == name)
            .values(enabled=enabled, updated_at=func.now())
        )
        return done.rowcount == 1

    def set_tool_grant(self, conn, name, principal, allowed, by=""):
        conn.execute(
            insert(tool_grants)
            .values(tool=name, principal=principal, allowed=allowed, granted_by=by)
            .on_conflict_do_update(
                index_elements=["tool", "principal"],
                set_=dict(allowed=allowed, granted_by=by, updated_at=func.now()),
            )
        )

    def clear_tool_grant(self, conn, name, principal):
        done = conn.execute(
            delete(tool_grants).where(
                tool_grants.c.tool == name, tool_grants.c.principal == principal
            )
        )
        return done.rowcount == 1

    def tool_listing(self, conn):
        """Every registered tool with its explicit grants, for an administrator."""
        tools = [
            dict(row)
            for row in conn.execute(
                select(tool_registry).order_by(
                    tool_registry.c.kind, tool_registry.c.name
                )
            ).mappings()
        ]
        grants = {}
        for row in conn.execute(select(tool_grants).order_by(tool_grants.c.principal)):
            grants.setdefault(row.tool, []).append((row.principal, row.allowed))
        for tool in tools:
            tool["grants"] = grants.get(tool["name"], [])
        return tools

    def scope_tools(self, conn, scope):
        """The tools enabled for a group scope, as a frozenset (empty when none or unknown)."""
        return frozenset(
            conn.scalars(
                select(gateway_scope_tools.c.tool).where(
                    gateway_scope_tools.c.scope == scope
                )
            )
        )

    def set_scope_tools(self, conn, scope, tools):
        """Replace a group's enabled tools. The caller has validated the names and the scope."""
        conn.execute(
            delete(gateway_scope_tools).where(gateway_scope_tools.c.scope == scope)
        )
        names = sorted(set(tools))
        if names:
            conn.execute(
                insert(gateway_scope_tools),
                [dict(scope=scope, tool=name) for name in names],
            )

    def owner_exists(self, conn, owner):
        return (
            conn.execute(select(users.c.id).where(users.c.id == owner)).first()
            is not None
        )

    def owners_with_email(self, conn, email):
        """The owner ids whose saved Lark email is this one (normally one)."""
        return list(
            conn.execute(
                select(users.c.id).where(
                    func.lower(users.c.state["email"].as_string()) == email.lower()
                )
            ).scalars()
        )

    def message(self, conn, owner, payload):
        conn.execute(
            insert(messages).values(
                id=str(uuid.uuid4()),
                owner=owner,
                created=database_time(),
                payload=payload,
            )
        )

    def count_jobs(self, conn, kind):
        return conn.execute(
            select(func.count()).select_from(jobs).where(jobs.c.kind == kind)
        ).scalar_one()

    def start_turn(
        self,
        conn,
        session_id,
        job_id,
        owner,
        version,
        request,
        ttl,
        chat_id=None,
        message_id=None,
        chat_type="unknown",
        thread=None,
    ):
        """Register or extend the turn a Hermes call belongs to; a retry reuses the row."""
        conn.execute(
            insert(turns)
            .values(
                session_id=session_id,
                job_id=job_id,
                owner=owner,
                credential_version=version,
                request=request,
                status="running",
                expires=database_time() + ttl,
                created=database_time(),
                chat_id=chat_id,
                message_id=message_id,
                chat_type=chat_type,
                thread=thread,
            )
            .on_conflict_do_update(
                index_elements=[turns.c.session_id],
                set_=dict(status="running", expires=database_time() + ttl),
            )
        )

    def latest_turn(self, conn, job_id):
        row = (
            conn.execute(
                select(turns)
                .where(turns.c.job_id == job_id)
                .order_by(turns.c.created.desc(), turns.c.session_id.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def finished_sessions(self, conn, job_id):
        """Hermes sessions of a job whose turn is over; a running one is never listed."""
        return list(
            conn.execute(
                select(turns.c.session_id).where(
                    turns.c.job_id == job_id,
                    turns.c.status.in_(("done", "failed", "unknown")),
                )
            )
            .scalars()
            .all()
        )

    def job_sessions(self, conn, job_id):
        """Every Hermes session a job used, whatever its status."""
        return list(
            conn.execute(select(turns.c.session_id).where(turns.c.job_id == job_id))
            .scalars()
            .all()
        )

    def add_artifact(self, conn, ref, owner, session_id, payload, ttl):
        # Expired rows are removed here, so abandoned previews do not linger.
        conn.execute(delete(artifacts).where(artifacts.c.expires < database_time()))
        conn.execute(
            insert(artifacts).values(
                id=ref,
                owner=owner,
                session_id=session_id,
                payload=payload,
                expires=database_time() + ttl,
                created=database_time(),
            )
        )

    def count_artifacts(self, conn, owner, session_id):
        """How many stored artifacts this owner's turn holds right now (expired ones do not count)."""
        return conn.execute(
            select(func.count())
            .select_from(artifacts)
            .where(
                artifacts.c.owner == owner,
                artifacts.c.session_id == session_id,
                artifacts.c.expires >= database_time(),
            )
        ).scalar_one()

    def take_artifacts(self, conn, owner, session_ids):
        """Remove and return (session_id, id, payload) of this owner's artifacts, oldest first."""
        if not session_ids:
            return []
        rows = conn.execute(
            delete(artifacts)
            .where(artifacts.c.owner == owner, artifacts.c.session_id.in_(session_ids))
            .returning(
                artifacts.c.session_id,
                artifacts.c.id,
                artifacts.c.payload,
                artifacts.c.created,
            )
        ).all()
        return [r[:3] for r in sorted(rows, key=lambda r: r[3])]

    def live_turn(self, conn, session_id):
        """The turn a tool call may act for: still running and not expired (database time)."""
        row = (
            conn.execute(
                select(turns).where(
                    turns.c.session_id == session_id,
                    turns.c.status == "running",
                    turns.c.expires > database_time(),
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def mark_needs_login(self, conn, session_id, owner):
        """Flag a running turn of this owner. A finished turn, another owner's turn or a
        replayed tool call cannot flip it: one UPDATE, conditional on both."""
        conn.execute(
            update(turns)
            .where(
                turns.c.session_id == session_id,
                turns.c.owner == owner,
                turns.c.status == "running",
            )
            .values(needs_login=True)
        )

    def turn_needs_login(self, conn, job_id):
        """True when any attempt of this job had a tool that needed a sign-in."""
        return (
            conn.execute(
                select(turns.c.session_id)
                .where(turns.c.job_id == job_id, turns.c.needs_login.is_(True))
                .limit(1)
            ).first()
            is not None
        )

    def set_run_id(self, conn, session_id, run_id):
        conn.execute(
            update(turns)
            .where(turns.c.session_id == session_id, turns.c.status == "running")
            .values(run_id=run_id)
        )

    def run_to_stop(self, conn, job_id, owner):
        """The Hermes run of this owner's job while it is running, else None (not started
        yet, finished, or someone else's job)."""
        return conn.execute(
            select(turns.c.run_id)
            .where(
                turns.c.job_id == job_id,
                turns.c.owner == owner,
                turns.c.status == "running",
                turns.c.run_id.is_not(None),
            )
            .order_by(turns.c.created.desc(), turns.c.session_id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def running_turn(self, conn, job_id, owner):
        """(Hermes run id, seconds it has been running) of this owner's job while it runs, else
        None. The seconds come from the database clock."""
        row = conn.execute(
            select(turns.c.run_id, database_time() - turns.c.created)
            .where(
                turns.c.job_id == job_id,
                turns.c.owner == owner,
                turns.c.status == "running",
                turns.c.run_id.is_not(None),
            )
            .order_by(turns.c.created.desc(), turns.c.session_id.desc())
            .limit(1)
        ).first()
        return (row[0], float(row[1])) if row else None

    def job_payload(self, conn, job_id):
        """The payload of a job that still exists, else None."""
        return conn.execute(
            select(jobs.c.payload).where(jobs.c.id == job_id)
        ).scalar_one_or_none()

    def note_job(self, conn, job_id, **fields):
        """Add facts to the payload of a job that still exists, under a row lock, so a later tool
        call of its turn can check them (see `lark_links`). False when the job is gone."""
        current = conn.execute(
            select(jobs.c.payload).where(jobs.c.id == job_id).with_for_update()
        ).scalar_one_or_none()
        if current is None:
            return False
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(payload={**current, **fields})
        )
        return True

    def note_lark_found(self, conn, job_id, found, reads=0):
        """Remember, for the rest of this turn, the documents a document pointed to (`found`: token to
        the number of links away from the user's own) and how many reads were spent. The shallowest
        depth of a token is kept. False when the job is gone."""
        current = conn.execute(
            select(jobs.c.payload).where(jobs.c.id == job_id).with_for_update()
        ).scalar_one_or_none()
        if current is None:
            return False
        known = dict(current.get("lark_found") or {})
        for token, depth in found.items():
            known[token] = min(depth, known.get(token, depth))
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(
                payload={
                    **current,
                    "lark_found": known,
                    "lark_reads": int(current.get("lark_reads") or 0) + reads,
                }
            )
        )
        return True

    def note_history_file_links(self, conn, job_id, found, tool, generation=None):
        """Merge file references observed in a live turn, keeping the most recent 24."""
        current = conn.execute(
            select(jobs.c.payload).where(jobs.c.id == job_id).with_for_update()
        ).scalar_one_or_none()
        if current is None:
            return False
        refs = dict(current.get("lark_history_refs") or {})
        for url, message_id in found.items():
            previous = refs.pop(url, None)
            refs[url] = (
                previous
                if generation is not None
                and previous
                and previous["generation"] is None
                else {"message_id": message_id, "tool": tool, "generation": generation}
            )
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(
                payload={**current, "lark_history_refs": dict(list(refs.items())[-24:])}
            )
        )
        return True

    def end_turn(self, conn, session_id, status):
        conn.execute(
            update(turns).where(turns.c.session_id == session_id).values(status=status)
        )


# Group Brain tables register on the metadata above.
from ..knowledge import tables as _knowledge_tables  # noqa: E402,F401
