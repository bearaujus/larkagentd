"""Database-backed token allowances, run settings, and effective-dated model rates."""

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from urllib.parse import parse_qs

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from ..core.store import (
    governance_flex,
    governance_history,
    governance_rates,
    governance_settings,
    token_usage,
)
from .access import AccessError, email_address

BILLABLE = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
CADENCES = {"weekly", "monthly"}
TIERS = ("free", "flex")
# Exactly representable in both the dashboard and the database.
MAX_LIMIT = 2**53 - 1


def cycle_start(day, cadence):
    if cadence == "weekly":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def next_reset(day, cadence):
    start = cycle_start(day, cadence)
    if cadence == "weekly":
        return start + timedelta(days=7)
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1)


def active_cycle(policy, day):
    return max(
        date.fromisoformat(policy["cadence_since"]),
        cycle_start(day, policy["cadence"]),
    )


def pool_settings(policy, tier):
    return policy.get("pools", {}).get(tier, {})


def active_pool_limit(settings, start):
    adjustment = settings.get("adjustment")
    if adjustment and adjustment["cycle_start"] == start.isoformat():
        return adjustment["limit"], True
    return settings.get("limit"), False


def token_limit(value, *, unlimited=False):
    if value is None and unlimited:
        return None
    if type(value) is not int or not 0 <= value <= MAX_LIMIT:
        raise AccessError(400, "invalid_token_limit")
    return value


class Governance:
    def __init__(
        self, access, *, seed_limit=0, default_model="", models=(), efforts=None
    ):
        self.access = access
        self.seed_limit = seed_limit or None
        self.default_model = default_model
        self.models = tuple(
            dict.fromkeys(model for model in (default_model, *models) if model)
        )
        self.efforts = efforts or {}

    def now(self, conn):
        return datetime.fromtimestamp(self.access.now(conn), timezone.utc)

    def policy(self, conn):
        """Roll due changes forward under the same lock as admission and dashboard edits."""
        self.access.lock(conn)
        now = self.now(conn)
        today = now.date()
        policy = conn.scalar(select(governance_settings.c.policy))
        if policy is None:
            start = cycle_start(today, "monthly").isoformat()
            policy = dict(
                cadence="monthly",
                free_limit=self.seed_limit,
                legacy_cycle=start,
                cadence_since=start,
                pending=None,
                model=None,
                effort=None,
            )
            conn.execute(insert(governance_settings).values(id=1, policy=policy))
        pending = policy["pending"]
        rollover = pending and date.fromisoformat(pending["effective_on"]) <= today
        if rollover:
            if policy["cadence"] != pending["cadence"]:
                policy["cadence_since"] = pending["effective_on"]
            policy.update(
                cadence=pending["cadence"],
                free_limit=pending["free_limit"],
                pending=None,
            )
            conn.execute(
                update(governance_flex)
                .where(governance_flex.c.pending_limit.is_not(None))
                .values(
                    active_limit=governance_flex.c.pending_limit, pending_limit=None
                )
            )
        for pool in policy.get("pools", {}).values():
            next_limit = pool.get("pending")
            if next_limit and date.fromisoformat(next_limit["effective_on"]) <= today:
                pool.update(limit=next_limit["limit"], pending=None)
                rollover = True
        if rollover:
            self.save(conn, policy)
        return policy, now

    @staticmethod
    def save(conn, policy):
        conn.execute(
            update(governance_settings)
            .where(governance_settings.c.id == 1)
            .values(policy=policy)
        )

    def needs_email(self, conn):
        self.policy(conn)
        return self.has_active_flex(conn)

    @staticmethod
    def has_active_flex(conn):
        return bool(
            conn.scalar(
                select(governance_flex.c.email)
                .where(
                    governance_flex.c.enabled,
                    governance_flex.c.active_limit.is_not(None),
                )
                .limit(1)
            )
        )

    def quota(self, conn, owner):
        policy, now = self.policy(conn)
        email = self.access.actor_email(conn, owner)
        if not email and self.has_active_flex(conn):
            return None
        flex = self.flex(conn, email) if email else None
        active = flex and flex["enabled"] and flex["active_limit"] is not None
        tier, tracker = ("flex", email) if active else ("free", owner)
        allowance = flex["active_limit"] if active else policy["free_limit"]
        start = active_cycle(policy, now.date())
        return dict(
            tier=tier,
            tracker=tracker,
            limit=allowance,
            pool_limit=active_pool_limit(pool_settings(policy, tier), start)[0],
            cycle_start=start.isoformat(),
            resets_on=next_reset(now.date(), policy["cadence"]).isoformat(),
            legacy=not active and start.isoformat() == policy["legacy_cycle"],
            admitted_at=now.isoformat(),
            model=policy["model"] or self.default_model or None,
            effort=self.active_effort(policy),
        )

    def active_effort(self, policy):
        effort = policy["effort"]
        return effort if effort in self.efforts.get(policy["model"], ()) else None

    @staticmethod
    def flex(conn, email):
        return (
            conn.execute(
                select(governance_flex).where(governance_flex.c.email == email)
            )
            .mappings()
            .first()
        )

    @staticmethod
    def sum_usage(conn, matching):
        u = token_usage.c
        used, missing = conn.execute(
            select(
                func.coalesce(func.sum(sum(u[name] for name in BILLABLE)), 0),
                func.count().filter(u.source == "unavailable"),
            ).where(matching)
        ).one()
        return int(used), missing

    def usage(self, conn, quota):
        u = token_usage.c
        start = date.fromisoformat(quota["cycle_start"])
        matching = and_(
            u.quota_tier == quota["tier"],
            u.quota_tracker == quota["tracker"],
            u.quota_cycle_start == start,
        )
        if quota["legacy"]:
            matching = or_(
                matching,
                and_(
                    u.quota_tier.is_(None),
                    u.requester == quota["tracker"],
                    u.month == start,
                ),
            )
        return self.sum_usage(conn, matching)

    def pool_usage(self, conn, tier, start, legacy):
        u = token_usage.c
        matching = and_(u.quota_tier == tier, u.quota_cycle_start == start)
        if tier == "free" and legacy:
            matching = or_(
                matching,
                and_(
                    u.quota_tier.is_(None),
                    u.month == start,
                    u.chat_type.in_(("p2p", "group")),
                ),
            )
        return self.sum_usage(conn, matching)

    def refusal(self, conn, quota):
        if quota is None:
            return "identity"
        limit = quota["limit"]
        if limit is not None and (limit == 0 or self.usage(conn, quota)[0] >= limit):
            return "quota"
        pool_limit = quota["pool_limit"]
        if pool_limit is not None and (
            pool_limit == 0
            or self.pool_usage(
                conn,
                quota["tier"],
                date.fromisoformat(quota["cycle_start"]),
                quota["legacy"],
            )[0]
            >= pool_limit
        ):
            return "pool"
        return None

    def read_pools(self, conn, policy, start):
        pools = {}
        legacy = start.isoformat() == policy["legacy_cycle"]
        for tier in TIERS:
            settings = pool_settings(policy, tier)
            limit, adjusted = active_pool_limit(settings, start)
            used, missing = self.pool_usage(conn, tier, start, legacy)
            pools[tier] = dict(
                recurring_limit=settings.get("limit"),
                limit=limit,
                adjusted=adjusted,
                pending=settings.get("pending"),
                used=used,
                unavailable_sessions=missing,
            )
        return pools

    @staticmethod
    def estimate(conn, counts, model, admitted_at):
        if not counts or not model:
            return None
        rates = conn.scalar(
            select(governance_rates.c.rates)
            .where(
                governance_rates.c.model == model,
                governance_rates.c.effective_at <= admitted_at,
            )
            .order_by(governance_rates.c.effective_at.desc())
            .limit(1)
        )
        if (
            not rates
            or all(rate is None for rate in rates.values())
            or any(counts[name] and rates[name] is None for name in BILLABLE)
        ):
            return None
        with localcontext() as context:
            context.prec = 40
            return sum(
                Decimal(rates[name]) * counts[name]
                for name in BILLABLE
                if rates[name] is not None
            ) / Decimal(1_000_000)

    def read(self, conn, policy, now):
        effective_on = next_reset(now.date(), policy["cadence"]).isoformat()
        start = active_cycle(policy, now.date())
        assignments = list(
            conn.execute(
                select(governance_flex).order_by(governance_flex.c.email)
            ).mappings()
        )
        u = token_usage.c
        usage = dict(
            conn.execute(
                select(u.quota_tracker, func.sum(sum(u[name] for name in BILLABLE)))
                .where(u.quota_tier == "flex", u.quota_cycle_start == start)
                .group_by(u.quota_tracker)
            ).all()
        )
        rate_versions = select(
            governance_rates.c.model,
            governance_rates.c.rates,
            func.row_number()
            .over(
                partition_by=governance_rates.c.model,
                order_by=governance_rates.c.effective_at.desc(),
            )
            .label("position"),
        ).subquery()
        rates = conn.execute(
            select(rate_versions.c.model, rate_versions.c.rates).where(
                rate_versions.c.position == 1
            )
        ).all()
        return dict(
            model=policy["model"],
            effective_model=policy["model"] or self.default_model,
            effort=self.active_effort(policy),
            models=[
                dict(id=model, efforts=list(self.efforts.get(model, ())))
                for model in self.models
            ],
            prices=dict(rates),
            pools=self.read_pools(conn, policy, start),
            quota=dict(
                cadence=policy["cadence"],
                free_limit=policy["free_limit"],
                pending=policy["pending"]
                if policy["pending"]
                and (policy["pending"]["cadence"], policy["pending"]["free_limit"])
                != (policy["cadence"], policy["free_limit"])
                else None,
                cycle_start=start.isoformat(),
                resets_on=effective_on,
            ),
            flex=[
                dict(row, used=int(usage.get(row["email"], 0))) for row in assignments
            ],
        )

    @staticmethod
    def quota_snapshot(policy, setting):
        snapshot = {setting: policy[setting]}
        pending = policy["pending"]
        if pending and pending[setting] != policy[setting]:
            pending_field = (
                "pending_limit" if setting == "free_limit" else "pending_cadence"
            )
            snapshot.update(
                {
                    pending_field: pending[setting],
                    "effective_on": pending["effective_on"],
                }
            )
        return snapshot

    @staticmethod
    def pool_snapshot(settings, start):
        limit, adjusted = active_pool_limit(settings, start)
        snapshot = {"limit": settings.get("limit")}
        if adjusted:
            snapshot["current_cycle_limit"] = limit
        if pending := settings.get("pending"):
            snapshot.update(
                pending_limit=pending["limit"], effective_on=pending["effective_on"]
            )
        return snapshot

    def flex_snapshot(self, conn, policy, email):
        row = self.flex(conn, email)
        if row is None:
            return {}
        snapshot = {"enabled": row["enabled"]}
        if row["active_limit"] is not None:
            snapshot["limit"] = row["active_limit"]
        if row["pending_limit"] is not None and policy["pending"]:
            snapshot.update(
                pending_limit=row["pending_limit"],
                effective_on=policy["pending"]["effective_on"],
            )
        return snapshot

    @staticmethod
    def history(conn, query):
        try:
            values = parse_qs(query, keep_blank_values=True, max_num_fields=1)
            if values and set(values) != {"before"}:
                raise ValueError
            before = values.get("before", [None])[0]
            if before is not None and (
                not re.fullmatch(r"[1-9][0-9]{0,18}", before) or int(before) >= 2**63
            ):
                raise ValueError
        except ValueError:
            raise AccessError(400, "invalid_history_cursor") from None
        statement = select(governance_history)
        if before is not None:
            statement = statement.where(governance_history.c.id < int(before))
        size = 25
        rows = (
            conn.execute(
                statement.order_by(governance_history.c.id.desc()).limit(size + 1)
            )
            .mappings()
            .all()
        )
        return {
            "items": [
                dict(
                    row,
                    id=str(row["id"]),
                    occurred_at=row["occurred_at"].astimezone(timezone.utc).isoformat(),
                )
                for row in rows[:size]
            ],
            "next_before": str(rows[size - 1]["id"]) if len(rows) > size else None,
        }

    @staticmethod
    def record_change(conn, actor, resource, target, before, after):
        if before != after:
            conn.execute(
                insert(governance_history).values(
                    actor_email=actor["email"],
                    resource=resource,
                    target=target,
                    before=before,
                    after=after,
                )
            )

    def dispatch(self, conn, actor, method, parts, body, *, query=""):
        if parts == ["history"]:
            if method != "GET":
                raise AccessError(405, "method_not_allowed")
            return 200, self.history(conn, query)
        if not parts:
            if method != "GET":
                raise AccessError(405, "method_not_allowed")
            return 200, self.read(conn, *self.policy(conn))
        if actor["role"] != "admin":
            raise AccessError(403, "governance_admin_required")
        policy, now = self.policy(conn)
        if parts == ["model"] and method == "PATCH":
            self.access.fields(body, {"model", "effort"})
            model, effort = body["model"], body["effort"]
            if model is not None and model not in self.models:
                raise AccessError(400, "invalid_model")
            if effort is not None and effort not in self.efforts.get(model, ()):
                raise AccessError(400, "invalid_effort")
            policy.update(model=model, effort=effort)
            self.save(conn, policy)
        elif parts == ["quota"] and method == "PATCH":
            self.set_quota(conn, actor, policy, now.date(), body)
        elif len(parts) == 2 and parts[0] == "pools" and method == "PATCH":
            tier = parts[1]
            if tier not in TIERS:
                raise AccessError(404, "not_found")
            start = active_cycle(policy, now.date())
            before = self.pool_snapshot(pool_settings(policy, tier), start)
            self.set_pool(conn, policy, tier, body)
            self.record_change(
                conn,
                actor,
                "shared_pool",
                tier,
                before,
                self.pool_snapshot(pool_settings(policy, tier), start),
            )
        elif len(parts) == 2 and parts[0] == "flex" and method == "PATCH":
            email = email_address(parts[1])
            before = self.flex_snapshot(conn, policy, email)
            self.set_flex(conn, email, body)
            self.record_change(
                conn,
                actor,
                "flex_assignment",
                email,
                before,
                self.flex_snapshot(conn, policy, email),
            )
        elif len(parts) == 2 and parts[0] == "prices" and method == "PUT":
            self.set_prices(conn, parts[1], body, now)
        else:
            raise AccessError(404, "not_found")
        return 200, self.read(conn, policy, now)

    def set_quota(self, conn, actor, policy, today, body):
        if not body or set(body) - {"cadence", "free_limit"}:
            raise AccessError(400, "invalid_fields")
        before = {key: self.quota_snapshot(policy, key) for key in body}
        pending = policy["pending"] or policy
        cadence = body.get("cadence", pending["cadence"])
        if not isinstance(cadence, str) or cadence not in CADENCES:
            raise AccessError(400, "invalid_cadence")
        next_limit = pending["free_limit"]
        if "free_limit" in body:
            next_limit = token_limit(body["free_limit"], unlimited=True)
            policy["free_limit"] = next_limit
        pending_flex = conn.scalar(
            select(governance_flex.c.email)
            .where(governance_flex.c.pending_limit.is_not(None))
            .limit(1)
        )
        policy["pending"] = (
            dict(
                cadence=cadence,
                free_limit=next_limit,
                effective_on=next_reset(today, policy["cadence"]).isoformat(),
            )
            if pending_flex
            or (cadence, next_limit) != (policy["cadence"], policy["free_limit"])
            else None
        )
        self.save(conn, policy)
        for key, resource in (
            ("free_limit", "free_personal"),
            ("cadence", "quota_schedule"),
        ):
            if key in before:
                self.record_change(
                    conn,
                    actor,
                    resource,
                    "free" if key == "free_limit" else "global",
                    before[key],
                    self.quota_snapshot(policy, key),
                )

    def set_pool(self, conn, policy, tier, body):
        self.access.fields(body, {"limit"})
        limit = token_limit(body["limit"], unlimited=True)
        policy.setdefault("pools", {})[tier] = {"limit": limit}
        self.save(conn, policy)

    def set_flex(self, conn, email, body):
        if not body or set(body) - {"enabled", "token_limit"}:
            raise AccessError(400, "invalid_fields")
        current = self.flex(conn, email)
        if current is None and "token_limit" not in body:
            raise AccessError(404, "flex_not_found")
        enabled = body.get("enabled", current["enabled"] if current else True)
        if type(enabled) is not bool:
            raise AccessError(400, "invalid_enabled")
        changes = {"enabled": enabled}
        if "token_limit" in body:
            changes.update(
                active_limit=token_limit(body["token_limit"]), pending_limit=None
            )
        conn.execute(
            insert(governance_flex)
            .values(email=email, **changes)
            .on_conflict_do_update(
                index_elements=[governance_flex.c.email], set_=changes
            )
        )

    def set_prices(self, conn, model, rates, now):
        if model not in self.models:
            raise AccessError(400, "invalid_model")
        self.access.fields(rates, set(BILLABLE))
        for rate in rates.values():
            if rate is not None and (
                not isinstance(rate, str)
                or not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,8})?", rate)
            ):
                raise AccessError(400, "invalid_rate")
        conn.execute(
            insert(governance_rates).values(model=model, effective_at=now, rates=rates)
        )
