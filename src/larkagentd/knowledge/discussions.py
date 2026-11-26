from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from .lark_sources import SourceMessage
from .sanitize import token_count


@dataclass(frozen=True)
class Discussion:
    kind: str
    native_thread_id: str | None
    root_message_id: str | None
    window_key: str | None
    message_ids: tuple[str, ...]
    participants: tuple[tuple[str, int], ...]
    started_at: object
    ended_at: object
    partial: bool


def reconstruct(
    messages, *, gap_minutes=15, max_messages=50, max_tokens=3000, thread_complete=None
):
    unique = {}
    for message in messages:
        if isinstance(message, SourceMessage):
            unique.setdefault(message.message_id, message)
    ordered = sorted(unique.values(), key=lambda m: (m.created_at, m.message_id))
    threads = {}
    ordinary = []
    for msg in ordered:
        (
            threads.setdefault(msg.thread_id, []).append(msg)
            if msg.thread_id
            else ordinary.append(msg)
        )
    result = []
    for thread_id, group in threads.items():
        roots = [m.message_id for m in group if not m.parent_id]
        root_id = next((m.root_id for m in group if m.root_id), None) or (
            roots[0] if roots else group[0].message_id
        )
        complete = thread_complete is not None and thread_id in thread_complete
        result.append(
            _discussion("native_thread", group, thread_id, None, root_id, not complete)
        )

    by_id = {m.message_id: m for m in ordinary}
    parent = {
        m.message_id: (
            m.parent_id
            if m.parent_id in by_id
            else m.root_id
            if m.root_id in by_id
            else None
        )
        for m in ordinary
    }
    groups = []
    for m in ordinary:
        chain, cursor = [], m.message_id
        while cursor in by_id and cursor not in chain:
            chain.append(cursor)
            cursor = parent.get(cursor)
        anchor = chain[-1]
        groups.append((anchor, m))
    trees = {}
    for anchor, m in groups:
        trees.setdefault(anchor, []).append(m)
    # Keep reply-connected messages together; independent roots are then separated by inactivity.
    streams = []
    for tree in trees.values():
        tree.sort(key=lambda m: (m.created_at, m.message_id))
        if (
            streams
            and streams[-1][-1].chat_id == tree[0].chat_id
            and tree[0].created_at - streams[-1][-1].created_at
            <= timedelta(minutes=gap_minutes)
        ):
            streams[-1].extend(tree)
            streams[-1].sort(key=lambda m: (m.created_at, m.message_id))
        else:
            streams.append(tree)
    for stream in streams:
        chunks, chunk, used = [], [], 0
        for msg in stream:
            cost = token_count(msg.text)
            if chunk and (
                len(chunk) >= max_messages
                or used + cost > max_tokens
                or msg.created_at - chunk[-1].created_at
                > timedelta(minutes=gap_minutes)
            ):
                chunks.append(chunk)
                chunk, used = [], 0
            chunk.append(msg)
            used += cost
        if chunk:
            chunks.append(chunk)
        for chunk in chunks:
            first = chunk[0]
            key = hashlib.sha256(
                (first.chat_id + first.message_id).encode()
            ).hexdigest()
            result.append(
                _discussion("chat_window", chunk, None, key, first.message_id, False)
            )
    return sorted(
        result,
        key=lambda d: (d.started_at, d.kind, d.native_thread_id or d.window_key or ""),
    )


def _discussion(kind, messages, thread_id, key, root, partial):
    authored = {}
    for msg in messages:
        if msg.sender_type == "user" and msg.sender_id:
            authored[msg.sender_id] = authored.get(msg.sender_id, 0) + 1
    ordered = sorted(messages, key=lambda m: (m.created_at, m.message_id))
    return Discussion(
        kind,
        thread_id,
        root,
        key,
        tuple(m.message_id for m in ordered if m.sender_type == "user"),
        tuple(sorted(authored.items())),
        ordered[0].created_at,
        ordered[-1].created_at,
        partial,
    )
