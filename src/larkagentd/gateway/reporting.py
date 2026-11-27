"""Read-only usage reports, configured cost estimates, and complete CSV downloads."""

import csv
import io
import re
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs

from sqlalchemy import (
    BigInteger,
    Date,
    case,
    cast,
    func,
    literal,
    null,
    select,
    text,
    union_all,
)

from ..core.store import (
    USAGE_COUNTS,
    gateway_identities,
    query_activity,
    token_usage,
    users,
)
from .access import AccessError, email_address

COMMON = {
    "from",
    "to",
    "all_time",
    "requester",
    "email",
    "chat_type",
    "job_id",
    "session_id",
}
DATASETS = {"summary", "users", "models", "daily", "llm", "queries", "all"}
STATES = {"succeeded", "refused", "failed", "denied", "incomplete"}
EVENT_COLUMNS = (
    "resource_type",
    "operation",
    "occurred_at",
    "id",
    "requester",
    "email",
    "principal",
    "model",
    "job_id",
    "session_id",
    "chat_type",
    "chat_id",
    "thread",
    "source",
    *USAGE_COUNTS,
    "total_tokens",
    "quota_tier",
    "estimated_cost_usd",
    "tool",
    "project",
    "status",
    "error_code",
    "duration_ms",
    "estimated_input_gb",
    "row_count",
    "finished_at",
)
LLM_COLUMNS = (
    "occurred_at",
    "requester",
    "email",
    "principal",
    "model",
    "job_id",
    "session_id",
    "chat_type",
    "chat_id",
    "thread",
    "source",
    *USAGE_COUNTS,
    "total_tokens",
    "quota_tier",
    "estimated_cost_usd",
)
QUERY_COLUMNS = (
    "occurred_at",
    "id",
    "requester",
    "email",
    "principal",
    "model",
    "job_id",
    "session_id",
    "chat_type",
    "chat_id",
    "thread",
    "tool",
    "project",
    "status",
    "error_code",
    "duration_ms",
    "estimated_input_gb",
    "row_count",
    "finished_at",
)


def timestamp(value):
    try:
        if len(value) == 10:
            return datetime.combine(
                date.fromisoformat(value), datetime.min.time(), timezone.utc
            )
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise AccessError(400, "invalid_time_range") from None


def plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return value


def record(row):
    return {
        key: format(value, "f")
        if key == "estimated_cost_usd" and value is not None
        else plain(value)
        for key, value in row.items()
    }


def safe_cell(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    value = plain(value)
    if isinstance(value, str) and (
        value.startswith(("\t", "\r", "\n"))
        or value.lstrip().startswith(("=", "+", "-", "@"))
    ):
        return "'" + value
    return value


def parameters(raw, *, export=False):
    try:
        parsed = parse_qs(raw, keep_blank_values=True, max_num_fields=20)
    except ValueError:
        raise AccessError(400, "invalid_filter") from None
    if any(
        len(values) != 1 or not values[0] or len(values[0]) > 512
        for values in parsed.values()
    ):
        raise AccessError(400, "invalid_filter")
    values = {key: items[0] for key, items in parsed.items()}
    dataset = values.pop("dataset", "all") if export else None
    if export and dataset not in DATASETS:
        raise AccessError(400, "invalid_dataset")
    return values, dataset


def email_join(table, owner, prefix):
    identity = gateway_identities.alias(prefix + "_identity")
    person = users.alias(prefix + "_person")
    joined = table.outerjoin(identity, identity.c.owner == owner).outerjoin(
        person, person.c.id == owner
    )
    email = func.lower(
        func.coalesce(
            func.nullif(identity.c.email, ""),
            func.nullif(person.c.state["email"].as_string(), ""),
        )
    )
    return joined, email


def event_table():
    """Union immutable session attribution with metadata-only tool invocations."""
    u, q = token_usage, query_activity
    source, email = email_join(u, u.c.requester, "llm")
    fields = dict(
        resource_type=literal("llm"),
        operation=null(),
        occurred_at=u.c.created_at,
        id=u.c.session_id,
        requester=u.c.requester,
        email=email,
        principal=u.c.principal,
        model=func.coalesce(func.nullif(u.c.model, ""), "unknown"),
        job_id=u.c.job_id,
        session_id=u.c.session_id,
        chat_type=u.c.chat_type,
        chat_id=u.c.chat_id,
        thread=u.c.thread,
        source=u.c.source,
        **{name: u.c[name] for name in USAGE_COUNTS},
        total_tokens=u.c.input_tokens
        + u.c.output_tokens
        + u.c.cache_read_tokens
        + u.c.cache_write_tokens,
        quota_tier=u.c.quota_tier,
        estimated_cost_usd=u.c.estimated_cost_usd,
        tool=null(),
        project=null(),
        status=case((u.c.source == "session", "recorded"), else_="unavailable"),
        error_code=null(),
        duration_ms=null(),
        estimated_input_gb=null(),
        row_count=null(),
        finished_at=null(),
    )
    llm = select(*(fields[name].label(name) for name in EVENT_COLUMNS)).select_from(
        source
    )
    source, email = email_join(
        q.outerjoin(u, u.c.session_id == q.c.session_id), q.c.requester, "tool"
    )
    fields = dict(
        resource_type=literal("query"),
        operation=q.c.operation,
        occurred_at=q.c.created_at,
        id=q.c.id,
        requester=q.c.requester,
        email=email,
        principal=q.c.principal,
        model=func.coalesce(func.nullif(u.c.model, ""), "unknown"),
        job_id=q.c.job_id,
        session_id=q.c.session_id,
        chat_type=func.coalesce(
            func.nullif(q.c.chat_type, "unknown"), u.c.chat_type, "unknown"
        ),
        chat_id=q.c.chat_id,
        thread=func.coalesce(q.c.thread, u.c.thread),
        source=literal("tool_invocation"),
        **{name: cast(literal(0), BigInteger) for name in USAGE_COUNTS},
        total_tokens=cast(literal(0), BigInteger),
        quota_tier=null(),
        estimated_cost_usd=null(),
        tool=q.c.tool,
        project=q.c.project,
        status=q.c.status,
        error_code=q.c.error_code,
        duration_ms=q.c.duration_ms,
        estimated_input_gb=q.c.estimated_input_gb,
        row_count=q.c.row_count,
        finished_at=q.c.finished_at,
    )
    activity = select(
        *(fields[name].label(name) for name in EVENT_COLUMNS)
    ).select_from(source)
    return union_all(llm, activity).subquery("usage_events")


class Reporting:
    def __init__(self, store):
        self.store = store

    @contextmanager
    def snapshot(self):
        # Authentication finishes before this read transaction: long exports must not
        # hold the policy lock or block chat admission and manager revocation.
        with self.store.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as conn:
            with conn.begin():
                conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text("SET LOCAL statement_timeout = '30s'"))
                yield conn

    def filtered(self, conn, dataset, values, *, export=False):
        extras = (
            {"model", "source"}
            if dataset in ("llm", "models")
            else ({"tool", "status", "project"} if dataset == "queries" else set())
        )
        allowed = COMMON | extras | (set() if export else {"limit", "offset"})
        if set(values) - allowed:
            raise AccessError(400, "invalid_filter")
        now = conn.scalar(select(func.now())).astimezone(timezone.utc)
        all_time = values.get("all_time", "false")
        if all_time not in ("true", "false") or (
            all_time == "true" and ("from" in values or "to" in values)
        ):
            raise AccessError(400, "invalid_filter")
        end = timestamp(values["to"]) if "to" in values else now
        start = (
            None
            if all_time == "true"
            else (
                timestamp(values["from"])
                if "from" in values
                else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            )
        )
        if start is not None and end <= start:
            raise AccessError(400, "invalid_time_range")
        events = event_table()
        e = events.c
        conditions = [e.occurred_at < end]
        if start is not None:
            conditions.append(e.occurred_at >= start)
        if "requester" in values:
            conditions.append(e.requester == values["requester"])
        for key in ("job_id", "session_id"):
            if key in values:
                conditions.append(e[key] == values[key])
        if "email" in values:
            conditions.append(e.email == email_address(values["email"]))
        if "chat_type" in values:
            if values["chat_type"] not in ("p2p", "group", "system", "unknown"):
                raise AccessError(400, "invalid_filter")
            conditions.append(e.chat_type == values["chat_type"])
        for key in extras:
            if key not in values:
                continue
            if key == "tool" and not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", values[key]):
                raise AccessError(400, "invalid_filter")
            if key == "status" and values[key] not in STATES:
                raise AccessError(400, "invalid_filter")
            if key == "source" and values[key] not in ("session", "unavailable"):
                raise AccessError(400, "invalid_filter")
            conditions.append(e[key] == values[key])
        if dataset in ("llm", "models"):
            conditions.append(e.resource_type == "llm")
        elif dataset == "queries":
            conditions.append(e.resource_type == "query")
        return select(events).where(*conditions).subquery("filtered_usage"), {
            "from": plain(start),
            "to": plain(end),
            "all_time": all_time == "true",
        }

    @staticmethod
    def metrics(e):
        def count(condition):
            return func.count().filter(condition)

        sums = [
            func.coalesce(func.sum(e[name]), 0).label(name)
            for name in (*USAGE_COUNTS, "total_tokens")
        ]
        query = e.operation == "query"
        return [
            *sums,
            func.count(func.distinct(e.job_id))
            .filter(e.resource_type == "llm")
            .label("turns"),
            count(e.resource_type == "llm").label("llm_sessions"),
            func.count(func.distinct(func.nullif(e.requester, "")))
            .filter(e.resource_type == "llm")
            .label("active_people"),
            func.count(func.distinct(func.nullif(e.chat_id, "")))
            .filter((e.resource_type == "llm") & (e.chat_type == "group"))
            .label("active_groups"),
            func.sum(e.estimated_cost_usd).label("estimated_cost_usd"),
            count(e.estimated_cost_usd.is_not(None)).label("priced_sessions"),
            count(
                (e.resource_type == "llm")
                & (e.source == "session")
                & e.estimated_cost_usd.is_(None)
            ).label("unpriced_sessions"),
            count((e.resource_type == "llm") & (e.source != "session")).label(
                "unavailable_sessions"
            ),
            count(e.resource_type == "query").label("activity_calls"),
            count(query).label("query_calls"),
            count(e.operation == "estimate").label("estimate_calls"),
            count(e.operation == "validate").label("dry_run_calls"),
            count(e.operation == "execute").label("terminal_calls"),
            *(
                count((e.resource_type == "query") & (e.status == status)).label(
                    status + "_calls"
                )
                for status in sorted(STATES)
            ),
            func.sum(e.estimated_input_gb)
            .filter(query & (e.status == "succeeded"))
            .label("available_query_estimated_input_gb"),
            count(
                query & (e.status == "succeeded") & e.estimated_input_gb.is_(None)
            ).label("query_estimate_missing_calls"),
        ]

    def query(self, frame, dataset):
        e = frame.c
        if dataset in ("llm", "queries", "all"):
            names = (
                LLM_COLUMNS
                if dataset == "llm"
                else (QUERY_COLUMNS if dataset == "queries" else EVENT_COLUMNS)
            )
            return select(*(e[name] for name in names)).order_by(
                e.occurred_at, e.resource_type, e.id
            )
        if dataset == "summary":
            return select(*self.metrics(e)).select_from(frame)
        if dataset == "models":
            return select(e.model, *self.metrics(e)).group_by(e.model).order_by(e.model)
        if dataset == "daily":
            day = cast(func.timezone("UTC", e.occurred_at), Date).label("date")
            return select(day, *self.metrics(e)).group_by(day).order_by(day)
        if dataset == "users":
            # The CSV flattens nested model breakdowns into one row per user/model.
            return (
                select(
                    e.requester,
                    func.min(e.email).label("email"),
                    e.model,
                    *self.metrics(e),
                )
                .group_by(e.requester, e.model)
                .order_by(e.requester, e.model)
            )
        raise AccessError(404, "not_found")

    @staticmethod
    def model_spend(conn, frame):
        """Rank the priced model totals for the full filtered period in the report snapshot."""
        e = frame.c
        cost = func.sum(e.estimated_cost_usd)
        ranked = (
            select(
                e.model,
                cost.label("cost"),
                func.row_number()
                .over(order_by=(cost.desc(), e.model))
                .label("position"),
            )
            .where(e.resource_type == "llm", e.estimated_cost_usd.is_not(None))
            .group_by(e.model)
            .subquery()
        )
        # A null bucket groups all models after the first three; zero is a valid estimate.
        bucket = case((ranked.c.position <= 3, ranked.c.model)).label("model")
        rows = conn.execute(
            select(bucket, func.sum(ranked.c.cost).label("estimated_cost_usd"))
            .group_by(bucket)
            .order_by(func.min(ranked.c.position))
        ).mappings()
        return [record(row) for row in rows]

    @staticmethod
    def page(values):
        parsed = []
        for key, default, minimum, maximum in (
            ("limit", "50", 1, 200),
            ("offset", "0", 0, 100000),
        ):
            value = values.get(key, default)
            if (
                not re.fullmatch(r"[0-9]{1,6}", value)
                or not minimum <= int(value) <= maximum
            ):
                raise AccessError(400, "invalid_pagination")
            parsed.append(int(value))
        return parsed

    def get(self, dataset, raw):
        if dataset not in DATASETS - {"all"}:
            raise AccessError(404, "not_found")
        values, _ = parameters(raw)
        limit, offset = self.page(values)
        with self.snapshot() as conn:
            frame, period = self.filtered(conn, dataset, values)
            if dataset == "summary":
                statistics = record(
                    conn.execute(self.query(frame, dataset)).mappings().one()
                )
                return {
                    "period": period,
                    "statistics": statistics,
                    "model_spend": self.model_spend(conn, frame),
                    "monetary_costs_available": bool(statistics["priced_sessions"]),
                }
            if dataset == "users":
                e = frame.c
                total = conn.scalar(
                    select(func.count(func.distinct(e.requester))).select_from(frame)
                )
                owners = list(
                    conn.scalars(
                        select(e.requester)
                        .distinct()
                        .order_by(e.requester)
                        .limit(limit)
                        .offset(offset)
                    )
                )
                rows = conn.execute(
                    select(
                        e.requester, func.min(e.email).label("email"), *self.metrics(e)
                    )
                    .where(e.requester.in_(owners))
                    .group_by(e.requester)
                    .order_by(e.requester)
                ).mappings()
                items = [dict(record(row), models=[]) for row in rows]
                by_owner = {item["requester"]: item for item in items}
                models = self.query(frame, "users").where(
                    e.requester.in_(owners), e.resource_type == "llm"
                )
                for row in conn.execute(models).mappings():
                    row = record(row)
                    owner = row.pop("requester")
                    row.pop("email")
                    by_owner[owner]["models"].append(row)
            else:
                query = self.query(frame, dataset)
                total = conn.scalar(
                    select(func.count()).select_from(query.order_by(None).subquery())
                )
                items = [
                    record(row)
                    for row in conn.execute(
                        query.limit(limit).offset(offset)
                    ).mappings()
                ]
            return {
                "period": period,
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    @contextmanager
    def export(self, raw):
        values, dataset = parameters(raw, export=True)
        with tempfile.SpooledTemporaryFile(max_size=2 * 1024**2, mode="w+b") as spool:
            wrapper = io.TextIOWrapper(
                spool, encoding="utf-8-sig", newline="", write_through=True
            )
            try:
                with self.snapshot() as conn:
                    frame, _ = self.filtered(conn, dataset, values, export=True)
                    result = (
                        conn.execution_options(stream_results=True)
                        .execute(self.query(frame, dataset))
                        .yield_per(500)
                    )
                    writer = csv.writer(wrapper)
                    writer.writerow(result.keys())
                    count = 0
                    for row in result:
                        writer.writerow([safe_cell(value) for value in row])
                        count += 1
                    result.close()
                wrapper.flush()
            finally:
                wrapper.detach()
            size = spool.tell()
            spool.seek(0)
            # Build fully before sending headers: DB/CSV errors cannot masquerade as a
            # successful partial download. Only bounded, private temporary storage is used.
            yield spool, size, count, dataset
