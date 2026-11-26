"""Group learning evidence for authenticated dashboard sessions."""

import re

from ..knowledge.learned import check_sources, list_records
from ..knowledge.policy import effective
from .access import AccessError, group_scope


def snapshot(access, token, parts, scope, after):
    with access.store.engine.begin() as conn:
        actor = access.authenticate(conn, token)
        access.scoped_request(actor, parts, "GET")
        if actor["role"] not in ("admin", "group_admin"):
            raise AccessError(403, "admin_required")
        access.policy(conn, scope)
        state = effective(conn, access.knowledge_config, scope)
        rows, cursor = (
            list_records(conn, scope, after) if state.allows("memory") else ([], None)
        )
        return state, rows, cursor


def learned_page(access, token, parts, query, actor, transport, verify_group_admin):
    scope = group_scope(parts[1])
    if query and not re.fullmatch(
        r"after=[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", query
    ):
        raise AccessError(400, "invalid_filter")
    after = query.removeprefix("after=")
    state, rows, cursor = snapshot(access, token, parts, scope, after)
    group_name = None
    if transport:
        try:
            group_name = transport.group_profile(parts[1]).get("name")
        except (RuntimeError, OSError, AttributeError):
            pass
    source = access.knowledge_runtime.source if access.knowledge_runtime else None
    evidence = check_sources(source, scope, [r["refs"] for r in rows])
    # External reads may outlast a session, source revision, membership or policy change.
    verify_group_admin(actor)
    if rows and not actor.get("group_id"):
        owner = actor["owner"].split(":", 1)[-1]
        if not transport or not transport.group_member(parts[1], owner):
            raise AccessError(403, "group_membership_required")
    latest, current_rows, current_cursor = snapshot(access, token, parts, scope, after)
    if (
        latest.generation != state.generation
        or current_rows != rows
        or cursor != current_cursor
    ):
        raise AccessError(409, "knowledge_changed")
    records = [
        {
            "id": row["id"],
            "category": row["category"],
            "activation": "source_checked"
            if row["approved_by"] == "verified_group_source"
            else "reviewed",
            "content": row["sanitized_content"]
            if checked["status"] == "verified"
            else None,
            **checked,
        }
        for row, checked in zip(rows, evidence)
    ]
    return {
        "records": records,
        "next": cursor,
        "group_name": group_name,
        "state": state.state,
        "learning": state.allows("memory"),
        "mode": state.learning_mode,
    }
