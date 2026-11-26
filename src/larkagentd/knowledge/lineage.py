from __future__ import annotations

import uuid

from sqlalchemy import func, select

from .recall import Recall, opaque
from .tables import gb_discussion_messages, gb_discussions, gb_messages, gb_source_refs


def resolve_sources(runtime, group_key, discussion_or_source_ids):
    """Resolve bounded message refs from this group's catalog, with a live recall fallback."""
    if not group_key or not discussion_or_source_ids:
        return []
    requested = tuple(
        dict.fromkeys(
            str(value)
            for value in discussion_or_source_ids
            if isinstance(value, str) and value
        )
    )[:20]
    if not requested:
        return []
    found = []
    with runtime.connection() as conn:
        messages = (
            conn.execute(
                select(gb_messages)
                .where(
                    gb_messages.c.group_key == group_key,
                    gb_messages.c.status == "active",
                    gb_messages.c.visibility_status == "visible",
                    gb_messages.c.expires_at > func.now(),
                    gb_messages.c.lark_message_id.in_(requested),
                )
                .limit(20)
            )
            .mappings()
            .all()
        )
        found.extend(dict(row) for row in messages)
        missing = set(requested) - {row["lark_message_id"] for row in messages}
        if missing:
            discussions = (
                conn.execute(
                    select(gb_discussions.c.id)
                    .where(
                        gb_discussions.c.group_key == group_key,
                        gb_discussions.c.id.in_(missing),
                        gb_discussions.c.status == "active",
                        gb_discussions.c.expires_at > func.now(),
                    )
                    .limit(20)
                )
                .scalars()
                .all()
            )
            if discussions:
                more = (
                    conn.execute(
                        select(gb_messages)
                        .join(
                            gb_discussion_messages,
                            (
                                gb_discussion_messages.c.group_key
                                == gb_messages.c.group_key
                            )
                            & (gb_discussion_messages.c.message_id == gb_messages.c.id),
                        )
                        .where(
                            gb_discussion_messages.c.group_key == group_key,
                            gb_discussion_messages.c.discussion_id.in_(discussions),
                            gb_messages.c.status == "active",
                            gb_messages.c.visibility_status == "visible",
                            gb_messages.c.expires_at > func.now(),
                        )
                        .limit(20)
                    )
                    .mappings()
                    .all()
                )
                found.extend(dict(row) for row in more)
    missing_ids = set(requested) - {row["lark_message_id"] for row in found}
    if missing_ids and runtime.source is not None:
        try:
            state = runtime.effective(group_key)
            if state.allows("recall"):
                from datetime import datetime, timedelta, timezone

                days = min(30, getattr(runtime.config, "max_history_days", 30))
                now = datetime.now(timezone.utc)
                scan = Recall(runtime).scan(
                    group_key,
                    now - timedelta(days=days),
                    now,
                    [],
                    [],
                    expand=False,
                )
                live_ids = set()
                discussion_ids = set()
                for discussion, _ in scan["discussions"]:
                    key = (
                        discussion.native_thread_id
                        or discussion.window_key
                        or discussion.root_message_id
                    )
                    opaque_id = opaque("d", group_key, discussion.kind, key)
                    if opaque_id in missing_ids:
                        discussion_ids.update(discussion.message_ids)
                for source_id, message in scan["messages"].items():
                    if message.chat_id != group_key.removeprefix("group:") or (
                        source_id not in missing_ids
                        and opaque("src", group_key, source_id) not in missing_ids
                        and source_id not in discussion_ids
                    ):
                        continue
                    live_ids.add(source_id)
                    found.append(
                        {
                            "group_key": group_key,
                            "lark_message_id": message.message_id,
                            "sender_id": message.sender_id,
                            "created_at": message.created_at,
                            "sanitized_text": scan["texts"].get(source_id),
                            "visibility_status": "visible",
                        }
                    )
                missing_ids.difference_update(live_ids)
        except Exception:
            return found
    return found[:20]


def source_refs_for_messages(conn, group_key, message_ids):
    """Create verified source reference rows only for visible catalog messages."""
    results = []
    for message_id in tuple(dict.fromkeys(message_ids))[:20]:
        message = (
            conn.execute(
                select(gb_messages).where(
                    gb_messages.c.group_key == group_key,
                    gb_messages.c.lark_message_id == message_id,
                    gb_messages.c.status == "active",
                    gb_messages.c.visibility_status == "visible",
                    gb_messages.c.expires_at > func.now(),
                )
            )
            .mappings()
            .first()
        )
        if message is None:
            continue
        found = (
            conn.execute(
                select(gb_source_refs).where(
                    gb_source_refs.c.group_key == group_key,
                    gb_source_refs.c.source_kind == "message",
                    gb_source_refs.c.canonical_source_id == message["id"],
                    gb_source_refs.c.source_revision == message["revision"],
                )
            )
            .mappings()
            .first()
        )
        if found is None:
            ref_id = str(uuid.uuid4())
            conn.execute(
                gb_source_refs.insert().values(
                    id=ref_id,
                    group_key=group_key,
                    source_kind="message",
                    canonical_source_id=message["id"],
                    source_revision=message["revision"],
                    lark_message_id=message["lark_message_id"],
                    verified_at=func.now(),
                    visibility_status="visible",
                    link_type="message" if message["message_link"] else "none",
                    source_url=message["message_link"],
                )
            )
            found = (
                conn.execute(
                    select(gb_source_refs).where(gb_source_refs.c.id == ref_id)
                )
                .mappings()
                .one()
            )
        results.append(dict(found))
    return results
