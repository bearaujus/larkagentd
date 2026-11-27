"""Group learning evidence for authenticated dashboard sessions."""

import re

from ..knowledge.learned import check_sources, list_records, references
from ..knowledge.memory import Memory
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
            "saved_at": row["approved_at"].isoformat() if row["approved_at"] else None,
            "expires_at": min(
                [row["expires_at"]]
                + [ref["expires_at"] for ref in row["refs"] if ref["expires_at"]]
            ).isoformat(),
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
    }


def correction_review(access, token, parts, method, body, actor, transport):
    """Verify membership and Lark sources outside the database transaction."""
    scope = group_scope(parts[1])
    access.scoped_request(actor, parts, method)
    if actor["role"] != "admin":
        raise AccessError(403, "admin_required")

    def is_member():
        try:
            return transport is not None and transport.group_member(
                parts[1], actor["owner"].split(":", 1)[-1]
            )
        except (RuntimeError, OSError, AttributeError):
            raise AccessError(503, "group_role_unavailable") from None

    member = is_member()
    if method == "POST" and not member:
        raise AccessError(403, "group_membership_required")

    def snapshot_corrections(conn, current):
        if current["role"] != "admin" or current["owner"] != actor["owner"]:
            raise AccessError(403, "admin_required")
        access.policy(conn, scope)
        state = effective(conn, access.knowledge_config, scope)
        items = Memory().list_candidates(conn, scope, can_read=member)
        if method == "POST":
            items = [
                item
                for item in items
                if item["memory_id"] == parts[4]
                and item["revision"] == body.get("revision")
            ]
        refs = (
            [
                references(conn, scope, item["memory_id"], item["revision"])
                for item in items
            ]
            if member
            else []
        )
        return state, items, refs

    with access.store.engine.begin() as conn:
        initial = snapshot_corrections(conn, access.authenticate(conn, token))
    state, items, refs = initial
    if not member:
        return 200, {"candidates": items}
    source = access.knowledge_runtime.source if access.knowledge_runtime else None
    checked = check_sources(source, scope, refs)
    if items and not is_member():
        raise AccessError(403, "group_membership_required")
    with access.store.engine.begin() as conn:
        current = access.authenticate(conn, token)
        access.scoped_request(current, parts, method)
        if snapshot_corrections(conn, current) != initial:
            raise AccessError(409, "knowledge_changed")
        if method == "POST":
            if not state.allows("memory"):
                raise AccessError(409, "disabled")
            if not checked:
                raise AccessError(404, "not_found")
            if checked[0]["status"] != "verified":
                raise AccessError(409, "source_unavailable")
            return access.dispatch(
                conn,
                {
                    **current,
                    "correction_evidence": (scope, parts[4], items[0]["revision"]),
                },
                method,
                parts,
                body,
            )
        for item, evidence in zip(items, checked):
            if evidence["status"] != "verified":
                item.pop("content", None)
                item.pop("reason", None)
            item.update(evidence)
        return 200, {"candidates": items}
