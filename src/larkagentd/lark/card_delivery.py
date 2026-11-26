"""Versioned card displays: closed receipts are never reopened or recalled."""

import logging
import time

log = logging.getLogger(__name__)
UPDATE_WINDOW = 14 * 24 * 60 * 60


def deliver_card(store, job, transport):
    owner, payload = job["owner"], job["payload"]
    run_id = payload.get("card_run")
    terminal = payload.get("card_terminal", False)
    with store.card_delivery(owner) as acquired:
        if not acquired:
            with store.user(owner) as (conn, _):
                store.finish(conn, job, retry=0.5)
            return
        with store.user(owner) as (conn, state):
            if not store.owns(conn, job):
                return
            run = state.get("card_runs", {}).get(run_id)
            # Old queued cleanup has no authority to alter any newer display.
            if not run:
                store.finish(conn, job)
                return
            valid = (
                (
                    run.get("closed")
                    and payload.get("card_version") == run.get("terminal_version")
                )
                if terminal
                else (
                    run_id == state.get("card_run")
                    and not run.get("closed")
                    and payload.get("card_version") == state.get("card_version")
                )
            )
            if not valid:
                store.finish(conn, job)
                return
            pending_receipts = any(
                x.get("terminal_pending")
                for key, x in state.get("card_runs", {}).items()
                if key != run_id
            )
            if not terminal and pending_receipts:
                store.finish(conn, job, retry=0.5)
                return
            target = dict(run)
        message_id = target.get("message_id")
        created = target.get("created_at", 0)
        if terminal:
            # Recover an ambiguous create response with the same per-display UUID.
            # Truly unsent superseded displays are discarded without cluttering chat.
            if not message_id and target.get("creation_attempted"):
                message_id = transport.send(payload, run_id)
                created = time.time()
            if message_id and time.time() - created < UPDATE_WINDOW:
                transport.patch(message_id, payload["card"])
            elif message_id:
                log.warning(
                    "Old setup card cannot be edited; actions remain invalidated"
                )
        else:
            if message_id and time.time() - created >= UPDATE_WINDOW:
                message_id = None
            if message_id and not transport.patch(message_id, payload["card"]):
                message_id = None
            if not message_id:
                # One idempotency key per display, even if state changes after a timeout.
                with store.user(owner) as (conn, state):
                    if not store.owns(conn, job):
                        return
                    state["card_runs"][run_id]["creation_attempted"] = True
                message_id = transport.send(payload, run_id)
                created = time.time()
                # If a previous create was accepted but its response was lost, the UUID
                # may return that older content. Make the latest desired state explicit.
                if target.get("creation_attempted"):
                    transport.patch(message_id, payload["card"])
        with store.user(owner) as (conn, state):
            if not store.owns(conn, job):
                return
            record = state["card_runs"][run_id]
            if message_id:
                known = set(state.get("setup_card_ids", []))
                if record.get("message_id"):
                    known.add(record["message_id"])
                known.add(message_id)
                state["setup_card_ids"] = sorted(known)
                record.update(message_id=message_id, created_at=created)
            if terminal:
                record["terminal_pending"] = False
            # A new user message may have moved setup during this network call.
            # Never attach an older display to the new display's current pointer.
            if state.get("card_run") == run_id:
                state["card_message_id"] = message_id
                state["card_created_at"] = created
            store.finish(conn, job)
