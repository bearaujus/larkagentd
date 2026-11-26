"""Store requester-bound confirmations and prevent duplicate external writes."""

import hashlib
import json
import uuid

from sqlalchemy import Column, Float, String, Table, select, update
from sqlalchemy.dialects.postgresql import insert

from .store import database_time, metadata

actions = Table(
    "group_actions",
    metadata,
    Column("id", String, primary_key=True),
    Column("owner", String, nullable=False),
    Column("chat", String, nullable=False),
    Column("thread", String, nullable=False),
    Column("job_id", String, nullable=False),
    Column("sealed", String, nullable=False),
    Column("status", String, nullable=False),
    Column("expires", Float, nullable=False, index=True),
)
effects = Table(
    "tool_effects",
    metadata,
    Column("id", String, primary_key=True),
    Column("sealed", String),
    Column("updated", Float, nullable=False, index=True),
)


def action_request(store, vault, turn, kind, data, label):
    if vault is None or not turn.get("thread"):
        return False
    identifier = uuid.uuid4().hex
    with store.engine.begin() as conn:
        live = store.live_turn(conn, turn["session_id"])
        if not live or live["job_id"] != turn["job_id"]:
            return False
        from .store import jobs

        conn.execute(
            select(jobs.c.id).where(jobs.c.id == turn["job_id"]).with_for_update()
        )
        payload = store.job_payload(conn, turn["job_id"]) or {}
        if payload.get("confirmation_id") or payload.get("approved_action"):
            return False
        conn.execute(actions.delete().where(actions.c.expires < database_time() - 3600))
        conn.execute(
            actions.insert().values(
                id=identifier,
                owner=turn["owner"],
                chat=turn["chat_id"],
                thread=turn["thread"],
                job_id=turn["job_id"],
                sealed=vault.seal(
                    "action:" + identifier,
                    {
                        "kind": kind,
                        "data": data,
                        "label": label,
                        "message_id": payload["message_id"],
                        "thread_ref": payload.get("thread_ref"),
                    },
                ),
                status="pending",
                expires=database_time() + 600,
            )
        )
        store.note_job(conn, turn["job_id"], confirmation_id=identifier)
    return True


def pending(conn, owner, chat, identifier):
    return (
        conn.execute(
            select(actions).where(
                actions.c.id == identifier,
                actions.c.owner == owner,
                actions.c.chat == chat,
                actions.c.status == "pending",
                actions.c.expires > database_time(),
            )
        )
        .mappings()
        .first()
    )


def authorize(conn, identifier):
    return (
        conn.execute(
            update(actions)
            .where(actions.c.id == identifier, actions.c.status == "pending")
            .values(status="approved")
        ).rowcount
        == 1
    )


def consume(conn, vault, turn, identifier):
    sealed = conn.scalar(
        update(actions)
        .where(
            actions.c.id == identifier,
            actions.c.owner == turn["owner"],
            actions.c.chat == turn["chat_id"],
            actions.c.thread == turn["thread"],
            actions.c.status == "approved",
            actions.c.expires > database_time(),
        )
        .values(status="consumed")
        .returning(actions.c.sealed)
    )
    return vault.open("action:" + identifier, sealed) if sealed else None


def reserve(store, vault, turn, operation, arguments):
    digest = hashlib.sha256(
        json.dumps([turn["job_id"], operation, arguments], sort_keys=True).encode()
    ).hexdigest()
    with store.engine.begin() as conn:
        conn.execute(
            effects.delete().where(effects.c.updated < database_time() - 86_400)
        )
        inserted = conn.scalar(
            insert(effects)
            .values(id=digest, updated=database_time())
            .on_conflict_do_nothing()
            .returning(effects.c.id)
        )
        if inserted:
            return digest, None
        sealed = conn.scalar(select(effects.c.sealed).where(effects.c.id == digest))
        previous = vault.open("effect:" + digest, sealed) if sealed else None
        return None, previous


def checkpoint(store, vault, digest, data):
    with store.engine.begin() as conn:
        conn.execute(
            update(effects)
            .where(effects.c.id == digest)
            .values(
                sealed=vault.seal("effect:" + digest, data), updated=database_time()
            )
        )
