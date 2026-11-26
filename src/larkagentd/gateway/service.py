import hashlib
import logging
import re
import secrets
import time
import uuid
from datetime import date, datetime

from sqlalchemy import func, select, update

from ..core import group_actions
from ..core.auth import AuthError, transient_auth_error
from ..core.diagnostics import actor_ref, emit, job_fields
from ..core.googlelinks import links as google_links
from ..core.scopes import BASELINE, group_chat_scope
from ..core.store import (
    USAGE_COUNTS,
    database_time,
    jobs,
    token_usage,
    tool_registry,
    turns,
)
from ..knowledge.models import GroupBrainConfig
from ..knowledge.policy import effective as group_brain_effective
from ..knowledge.policy import still_valid as group_brain_valid
from ..knowledge.policy import store as group_brain_store
from ..lark import usage_text
from ..lark.attachments import resolve as resolve_attachments
from ..lark.attachments import thread_images
from ..lark.card import confirmation_fallback, reply_card
from ..lark.card_delivery import deliver_card
from ..lark.cards import choice_pages, json_card
from ..lark.inbound import IMAGE, takes_over, wants_setup
from ..lark.progress import Progress
from ..lark.render import answer_room, attach_code, with_blocks
from ..lark.ui_text import (
    AGENT_FAILURES,
    BUSY_REPLY,
    DIRECT_HELP,
    FAILURE_MESSAGES,
    GOVERNANCE_HELP,
    GROUP_BRAIN_HISTORY,
    GROUP_BRAIN_SUPPRESSED,
    GROUP_SETUP,
    GROUP_SIGN_IN,
    HELP_TEXT,
    IDENTITY_UNAVAILABLE,
    IMAGES_FAILED,
    NOT_ALLOWED,
    NOTHING_RUNNING,
    QUOTA_REACHED,
    RESUME_REQUEST,
    SETUP_GROUP_REQUIRED,
    SKILLS_QUESTION,
    STOP_FAILED,
    STOPPING,
    TAKEOVER_REQUEST,
    TOASTS,
    UNKNOWN_COMMAND,
    UNREADABLE_REPLY,
    images_skipped,
)
from .access import Access, AccessError
from .hermes import AgentError
from .knowledge_hooks import (
    finish_passive_learning,
    knowledge_command,
    prepare_passive_learning,
)


def sanitize(text):
    # Conservative protection for pasted secrets, in addition to excluding all auth state.
    text = re.sub(
        r"(?i)(bearer\s+|(?:access.?key.?secret|security.?token|password|api.?key)\s*[:=]\s*)[^\s,;]+",
        "[REDACTED]",
        text,
    )
    return re.sub(r"\b(?:sk-|LTAI|STS\.)[A-Za-z0-9_.-]{8,}", "[REDACTED]", text)


def speaker_of(owner):
    """A stable tag for a person inside stored group messages: a hash, never the id itself."""
    return hashlib.sha256(owner.encode()).hexdigest()[:10]


def group_speaker_names(transport, owner, remote):
    """Only look up the verified requester and people observed in this thread."""
    owners = [owner, *(message["owner"] for message in remote)]
    tenant = owner.partition(":")[0]
    ids = list(
        dict.fromkeys(
            person.partition(":")[2]
            for person in owners
            if person.partition(":")[0] == tenant and person.partition(":")[2]
        )
    )[:50]
    if not hasattr(transport, "display_names") or not ids:
        return {}
    try:
        names = transport.display_names(ids)
    except Exception as exc:
        emit("group.names_unavailable", error_type=type(exc).__name__)
        return {}
    return {
        speaker_of(f"{tenant}:{key}"): name
        for key, name in names.items()
        if key in ids and name
    }


def label_history(history, names, requester):
    labelled = []
    for message in history:
        if message.get("role") == "user" and message.get("speaker"):
            message = dict(message)
            speaker = message.pop("speaker")
            saved_name = message.pop("speaker_name", None)
            name = (
                names.get(speaker)
                or saved_name
                or ("Requester" if speaker == requester else "Group member")
            )
            message["content"] = f"{name}: {message.get('content', '')}"
        labelled.append(message)
    return labelled


def unseen_for_prompt(remote, history, earlier, own_ids, names):
    """Keep thread context up to the current question, using available display names."""
    known = {m.get("message_id") for m in (*history, *earlier) if m.get("message_id")}
    shown = []
    for message in remote:
        if message["message_id"] in own_ids:
            break  # nothing after the question belongs in the context
        if message["message_id"] in known:
            continue
        entry = dict(
            person=names.get(speaker_of(message["owner"]), "Group member"),
            text=sanitize(message["text"]),
        )
        if message.get("attachments"):
            # Internal: `thread_images` downloads them and removes this key before the request is sent.
            entry["_images"] = list(message["attachments"])
        shown.append(entry)
    return shown


MAX_TEXT = 32000
MAX_ATTACHMENTS = 20
RESUME_WINDOW = 1800  # seconds a request waits for its sign-in before it is forgotten
EMAIL_REFRESH = 86400  # a known email is read again after a day
EMAIL_RETRY = 3600  # a missing one is looked up again after an hour
STEERS_KEPT = 10  # corrections to a running turn kept for its history


def merged_payload(old, new):
    """A waiting request plus a message sent right after it, or None when it would not fit."""
    if new.get("chat") != old.get("chat") or new.get("thread") != old.get("thread"):
        return None  # typed inside another thread: a conversation of its own
    text = "\n\n".join(t for t in (old.get("text", ""), new["text"]) if t.strip())
    if len(text) > MAX_TEXT:
        return None
    return dict(
        old,
        text=text,
        attachments=[*old.get("attachments", []), *new["attachments"]][
            :MAX_ATTACHMENTS
        ],
        forwards=[*old.get("forwards", []), *new["forwards"]],
        reply_to=old.get("reply_to") or new["reply_to"],
        # Every Lark message that makes up this request, so thread context can skip them all.
        message_ids=[
            *old.get(
                "message_ids", [old["message_id"]] if old.get("message_id") else []
            ),
            *([new["message_id"]] if new.get("message_id") else []),
        ],
    )


def history_note(resolved):
    """What a past message carried, for the history: never the content of a file or image."""
    notes = ["[sent an image]"] * len(resolved.images)
    notes += [f"[attached file: {d['name']}]" for d in resolved.documents]
    notes += ["[forwarded a conversation]"] * len(resolved.forwarded)
    return "\n".join(notes)


class Service:
    def __init__(
        self,
        store,
        vault,
        cloud,
        history_limit=20,
        identity=None,
        agent=None,
        agent_capacity=None,
        artifacts=None,
        message_window=0,
        access=None,
        token_quota=0,
        token_summary=True,
        available_models=(),
        vision_models=(),
        provider_model="",
        governance=None,
        group_brain=None,
    ):
        self.store, self.vault, self.cloud = store, vault, cloud
        self.history_limit, self.identity = history_limit, identity
        self.agent, self.agent_capacity = agent, agent_capacity
        self.artifacts = artifacts
        self.message_window = message_window
        self.access = access if access is not None else Access(store)
        self.token_quota = token_quota
        self.token_summary = token_summary
        self.available_models = tuple(available_models)
        self.vision_models = tuple(vision_models)
        self.provider_model = provider_model
        self.governance = governance
        # Group Brain's global settings; the default is switched off.
        self.group_brain = group_brain or GroupBrainConfig()

    def guard_group_knowledge(self, conn, job, reply):
        """The answer, or a short notice when it was built from a group's knowledge and the group no longer
        allows that. Runs right before delivery, in the transaction that stores and queues the answer, so a
        disable that committed earlier is seen. Nothing built from the group is posted after it. An
        answer that did not use Group Brain is never touched."""
        if not job["payload"].get("group"):
            return reply, None
        noted = self.store.job_payload(conn, job["id"]) or {}
        group_key, generation = noted.get("gb_group"), noted.get("gb_generation")
        if not group_key:
            return reply, None
        if group_key == group_chat_scope(
            job["payload"].get("chat")
        ) and group_brain_valid(conn, self.group_brain, group_key, generation):
            # The answer used the group's knowledge and may go out; the send job is checked once more.
            return reply, (group_key, generation)
        emit("knowledge.delivery_suppressed", logging.WARNING, **job_fields(job))
        group_brain_store.audit(
            conn,
            group_key,
            "system",
            "delivery_suppressed",
            outcome="blocked",
            reason="policy_changed",
        )
        return GROUP_BRAIN_SUPPRESSED, None

    def group_brain_send_valid(self, payload):
        """False when a queued answer built from group knowledge may no longer be posted."""
        try:
            with self.store.engine.begin() as conn:
                return group_brain_valid(
                    conn,
                    self.group_brain,
                    payload["gb_group"],
                    payload["gb_generation"],
                )
        except Exception:
            return False

    def over_quota(self, conn, owner):
        """True when the person has used their monthly token quota. 0 means unlimited, and
        then nothing is read."""
        return bool(self.token_quota) and (
            self.store.tokens_used(conn, owner) >= self.token_quota
        )

    def governed_refusal(self, conn, quota):
        reason = self.governance.refusal(conn, quota)
        return {
            "identity": IDENTITY_UNAVAILABLE,
            "quota": "You've reached your token limit for this cycle. Try again after the next reset.",
            "pool": "Your tier's shared token pool has reached its limit. Try again after the next reset or ask an admin about the pool.",
        }.get(reason)

    def quota_denial(self, conn, owner):
        if self.governance:
            return self.governed_refusal(conn, self.governance.quota(conn, owner))
        return QUOTA_REACHED if self.over_quota(conn, owner) else None

    def record_usage(
        self, job, owner, thread, chat, group=False, override=False, model=None
    ):
        """Save what the turn cost, one row per Hermes session (a retry or a second attempt
        used tokens too). Runs before the sessions are deleted and never raises: accounting
        must not fail a turn."""
        if not self.agent or not hasattr(self.agent, "usage"):
            return
        try:
            with self.store.engine.begin() as conn:
                sessions = self.store.sessions_of_job(conn, job["id"])
            snapshot = job.get("payload", {}).get("governance")
            rows = []
            for session_id in sessions:
                counts = self.agent.usage(session_id)
                counts = dict(counts) if counts else None
                ran = (counts or {}).pop("model", "")  # what Hermes says it ran on
                row = dict(
                    session_id=session_id,
                    job_id=job["id"],
                    requester=owner,
                    principal=owner,
                    chat_type="group" if group else "p2p",
                    chat_id=chat or "",
                    thread=thread or "",
                    model=ran or model or getattr(self.agent, "model", "") or "",
                    model_override=override,
                    source="session" if counts else "unavailable",
                    **(counts or {name: 0 for name in USAGE_COUNTS}),
                )
                rows.append((row, counts, ran))
                if not counts:
                    emit("usage.unavailable", logging.WARNING, **job_fields(job))
            if not rows:
                return
            with self.store.engine.begin() as conn:
                if snapshot and self.governance:
                    # Admission and settlement must see whole turns as users share pools.
                    self.access.lock(conn)
                for row, counts, ran in rows:
                    if snapshot and self.governance:
                        previous = conn.scalar(
                            select(token_usage.c.model).where(
                                token_usage.c.session_id == row["session_id"],
                                token_usage.c.source == "session",
                                token_usage.c.model.notin_(("", "hermes-agent")),
                            )
                        )
                        recorded_model = previous or ran
                        row.update(
                            model=recorded_model,
                            quota_tier=snapshot["tier"],
                            quota_tracker=snapshot["tracker"],
                            quota_cycle_start=date.fromisoformat(
                                snapshot["cycle_start"]
                            ),
                            estimated_cost_usd=self.governance.estimate(
                                conn,
                                counts,
                                recorded_model,
                                datetime.fromisoformat(snapshot["admitted_at"]),
                            ),
                        )
                    self.store.record_usage(conn, row)
        except Exception as exc:
            emit(
                "usage.failed",
                logging.WARNING,
                **job_fields(job),
                error_type=type(exc).__name__,
            )

    def out(
        self,
        conn,
        owner,
        chat,
        text=None,
        state=None,
        note="",
        reply_to=None,
        preview_card=None,
        answer_to=None,
        thread=False,
        gb=None,
        single_reply=False,
        usage_footer="",
        fallback_card=None,
        attachments=(),
    ):
        guard = {"gb_group": gb[0], "gb_generation": gb[1]} if gb else {}
        self.store.add_job(
            conn,
            owner,
            "send",
            {
                **guard,
                "chat": chat,
                "reply_to": reply_to,
                "thread": thread,
                "text": text,
                "card_version": state.get("card_version") if state else None,
                "card_run": state.get("card_run") if state else None,
                "card_terminal": bool(state and state.get("card_closed")),
                "card": json_card(state, note) if state is not None else None,
                "preview_card": preview_card,
                **(
                    {
                        "single_reply": True,
                        "usage_footer": usage_footer,
                        "fallback_card": fallback_card,
                        "attachments": list(attachments),
                    }
                    if single_reply
                    else {}
                ),
                **({"answer_to": answer_to} if answer_to else {}),
            },
        )

    def reply(self, conn, owner, chat, text, message_id):
        """A reply to a user's message, in that message's thread."""
        self.out(conn, owner, chat, text, reply_to=message_id, thread=bool(message_id))

    def out_files(self, conn, owner, chat, files, reply_to=None, thread=False, gb=None):
        """One message per attached file, queued right after the answer's own message."""
        for file in files:
            self.store.add_job(
                conn,
                owner,
                "send",
                {
                    "chat": chat,
                    "reply_to": reply_to,
                    "thread": thread,
                    "text": None,
                    "card": None,
                    "preview_card": None,
                    "file": {"name": file["name"], "text": file["text"]},
                    **({"gb_group": gb[0], "gb_generation": gb[1]} if gb else {}),
                },
            )

    @staticmethod
    def fit_answer(reply, previews):
        """(answer, files): the answer with the code blocks that do not fit in one message sent
        as files instead, a preview of each staying in the message."""
        return attach_code(reply, answer_room([p["text"] for p in previews]))

    def ensure_card_run(self, state):
        runs = state.setdefault("card_runs", {})
        if not state.get("card_run"):
            state["card_run"] = str(uuid.uuid4())
        runs.setdefault(
            state["card_run"],
            {
                "message_id": state.get("card_message_id"),
                "created_at": state.get("card_created_at", 0),
                "closed": state.get("card_closed", False),
            },
        )
        return runs[state["card_run"]]

    def retire_display(
        self,
        conn,
        owner,
        state,
        title="Settings closed",
        note="Continue in the setup card below.",
    ):
        run = self.ensure_card_run(state)
        if run.get("closed"):
            return
        receipt = dict(
            state,
            card_closed=True,
            closed_title=title,
            card_note=note,
            card_version=str(uuid.uuid4()),
        )
        run.update(
            closed=True, terminal_version=receipt["card_version"], terminal_pending=True
        )
        self.out(conn, owner, state["chat"], state=receipt, note=note)

    def new_display(self, state):
        self.ensure_card_run(state)
        previous = state["card_run"]
        state["card_run"] = str(uuid.uuid4())
        state["card_runs"][state["card_run"]] = {"closed": False, "previous": previous}
        for key in (
            "card_message_id",
            "card_created_at",
            "card_delivery_failed",
        ):
            state.pop(key, None)
        state["card_closed"] = False

    def move_setup(self, conn, owner, state):
        self.ensure_card_run(state)
        if not state.get("card_closed") and state.get("card_version"):
            if state.get("card_expires", 0) <= time.time():
                self.close_card(
                    conn,
                    owner,
                    state,
                    "Setup expired",
                    "Continue in the new setup card below.",
                )
            else:
                self.retire_display(conn, owner, state)
        self.new_display(state)

    def show(self, conn, owner, state, note=""):
        self.ensure_card_run(state)
        if state.get("card_closed"):
            self.new_display(state)
        state.pop("card_delivery_failed", None)
        state.setdefault("setup_reopen_token", secrets.token_urlsafe(24))
        state["card_version"] = str(uuid.uuid4())
        state["card_closed"] = False
        state["card_note"] = note
        state["card_expires"] = time.time() + 900
        self.out(conn, owner, state["chat"], state=state, note=note)
        self.store.add_job(
            conn,
            owner,
            "expire_card",
            {"version": state["card_version"]},
            state["card_expires"],
        )

    def close_card(self, conn, owner, state, title="Settings closed", note=""):
        state.setdefault("setup_reopen_token", secrets.token_urlsafe(24))
        state["card_version"] = str(uuid.uuid4())
        run = self.ensure_card_run(state)
        run.update(
            closed=True, terminal_version=state["card_version"], terminal_pending=True
        )
        state["card_closed"] = True
        state["closed_title"] = title
        state["card_note"] = note
        state["login_version"] = str(uuid.uuid4())
        state.pop("auth_job", None)
        state.pop("setup_request", None)
        for key in (
            "attempt",
            "user_code",
            "verification_uri",
            "stage",
            "choices",
            "setup_sso",
            "pending_account",
            "pending_account_label",
            "auth_retry_notice",
        ):
            state.pop(key, None)
        self.out(conn, owner, state["chat"], state=state, note=note)

    def welcome(self, tenant, sender, chat, event, *, transport=None):
        owner = tenant + ":" + sender
        with self.store.user(owner, "entered:" + event) as (conn, state):
            if conn is None or state.get("chat"):
                return
            state["chat"] = chat
            self.out(conn, owner, chat, DIRECT_HELP)

    def queue_setup(self, conn, owner, state, group, *, reply_to=None, expired=False):
        state["setup_request"] = self.store.add_job(
            conn,
            owner,
            "setup",
            {
                "group": group,
                "chat": state["chat"],
                "reply_to": reply_to,
                "expired": expired,
            },
        )

    def receive_direct(self, conn, owner, state, chat, text, message_id):
        state["chat"] = chat
        command = text.strip().lower()
        if command == "/setup" or wants_setup(text):
            group = state.get("last_group")
            if self.cloud is None:
                reply = "No account connection is configured."
            elif not group or self.access.paused(conn):
                reply = SETUP_GROUP_REQUIRED
            else:
                self.queue_setup(conn, owner, state, group, reply_to=message_id)
                return
        elif command == "/usage":
            reply = self.usage_reply(conn, owner, None, False, False)
        else:
            reply = DIRECT_HELP
        self.reply(conn, owner, chat, reply, message_id)

    def work_setup(self, job, transport):
        owner, payload = job["owner"], job["payload"]
        group, chat = payload["group"], payload["chat"]
        member = self.cloud is not None and self.access.verify_setup_member(
            owner, group, transport
        )
        with self.store.user(owner) as (conn, state):
            if not self.store.owns(conn, job):
                return
            if state.get("setup_request") != job["id"] or state.get("chat") != chat:
                self.store.finish(conn, job)
                return
            state.pop("setup_request", None)
            if not member or self.access.admission(conn, owner, group):
                self.reply(conn, owner, chat, SETUP_GROUP_REQUIRED, payload["reply_to"])
            elif state.get("setup_group") != group and (
                state.get("auth_job") or state.get("stage") or state.get("attempt")
            ):
                self.reply(
                    conn,
                    owner,
                    chat,
                    "Finish or close the current setup before switching groups.",
                    payload["reply_to"],
                )
            else:
                state["setup_group"] = group
                self.move_setup(conn, owner, state)
                self.show(
                    conn,
                    owner,
                    state,
                    "Your sign-in expired. Connect again."
                    if payload["expired"]
                    else "",
                )
            self.store.finish(conn, job)

    def receive(
        self,
        tenant,
        sender,
        chat,
        event,
        message_id,
        text,
        ids=(),
        kind="text",
        attachments=(),
        reply_to=None,
        thread=None,
        in_thread=False,
        group=False,
        thread_ref=None,
        transport=None,
    ):
        owner = tenant + ":" + sender
        parts = text.strip().split()
        dashboard_login = len(parts) in (2, 3) and [
            part.lower() for part in parts[:2]
        ] == ["/dashboard", "login"]
        group_admin_scope = None
        if dashboard_login and len(parts) == 3 and not group:
            from .access import group_scope

            try:
                if transport is not None and transport.group_admin(parts[2], sender):
                    group_admin_scope = group_scope(parts[2])
            except (AccessError, RuntimeError, AttributeError):
                pass
        if transport is not None and (
            group or dashboard_login or text.strip().lower() == "/usage"
        ):
            self.access.verify_for_admission(
                owner,
                transport,
                group=chat if group else None,
                force=dashboard_login and not group and not group_admin_scope,
                governance=self.governance,
            )
        with self.store.user(owner, "message:" + event) as (conn, state):
            if conn is None:
                return
            if group:
                rejection = self.access.admission(conn, owner, chat)
                if rejection:
                    emit("request.rejected", actor=actor_ref(owner), reason=rejection)
                    if rejection != "unregistered_group":
                        self.reply(conn, owner, chat, NOT_ALLOWED, message_id)
                    return
                state["last_group"] = chat
            if group and (
                text.strip().lower() == "/knowledge"
                or text.strip().lower().startswith("/knowledge ")
            ):
                reply = knowledge_command(conn, self, text, chat_id=chat)
                self.reply(conn, owner, chat, reply, message_id)
                return
            if dashboard_login:
                if group:
                    self.reply(
                        conn,
                        owner,
                        chat,
                        "Request dashboard login in a direct chat.",
                        message_id,
                    )
                    return
                # Token delivery is the only effect of this command.
                state["chat"] = chat
                if len(parts) == 3 and group_admin_scope is None:
                    self.reply(
                        conn,
                        owner,
                        chat,
                        "Use a registered group you currently own or administer.",
                        message_id,
                    )
                    return
                try:
                    token, digest = self.access.issue(conn, owner, group_admin_scope)
                except AccessError as exc:
                    reply = (
                        "Please wait 30 seconds before requesting another dashboard token."
                        if exc.status == 429
                        else "Dashboard access requires an admin or manager role."
                    )
                    self.reply(conn, owner, chat, reply, message_id)
                    return
                sealed = self.vault.seal(
                    "dashboard-login:" + owner,
                    {"token": token, "chat": chat, "reply_to": message_id},
                )
                self.store.add_job(
                    conn, owner, "dashboard_login", {"sealed": sealed, "hash": digest}
                )
                return
            # Other Lark ids of this sender, from Lark's own event: Hermes names them by
            # one of these, not by the open id this owner key uses.
            known = {i for i in (*state.get("aliases", []), *ids) if i and i != sender}
            if known:
                state["aliases"] = sorted(known)
            if not group:
                self.receive_direct(conn, owner, state, chat, text, message_id)
                return
            if not text.strip() and not attachments and kind != "merge_forward":
                self.reply(conn, owner, chat, UNREADABLE_REPLY, message_id)
                return
            command = text.strip().lower()
            if command == "/setup":
                self.reply(
                    conn,
                    owner,
                    chat,
                    GROUP_SETUP
                    if self.cloud is not None
                    else "No account connection is configured.",
                    message_id,
                )
                return
            if takes_over(text):
                text = TAKEOVER_REQUEST
            if command == "/skills":
                text = SKILLS_QUESTION
            elif text.lstrip().startswith("/"):
                if command == "/stop":
                    reply = self.request_stop(conn, owner, state, chat, thread)
                elif command == "/model" or command.startswith("/model "):
                    argument = text.strip()[len("/model") :]
                    reply = self.model_reply(conn, argument)
                elif command == "/usage":
                    reply = self.usage_reply(conn, owner, thread, in_thread, group)
                elif command == "/help":
                    reply = GOVERNANCE_HELP if self.governance else HELP_TEXT
                    reply += f"\nGroup owners and admins can send /dashboard login {chat} in a DM."
                    if self.access.role(conn, self.access.actor_email(conn, owner)):
                        reply += "\n/dashboard login - get a one-time dashboard token in a DM."
                else:
                    reply = UNKNOWN_COMMAND
                self.reply(conn, owner, chat, reply, message_id)
                return
            new = dict(
                text=sanitize(text),
                attachments=[dict(a) for a in attachments][:MAX_ATTACHMENTS],
                forwards=[message_id] if kind == "merge_forward" else [],
                # Replying to the message that started the thread says nothing new.
                reply_to=reply_to if reply_to and reply_to != thread else None,
                thread=thread,
                in_thread=in_thread,
                chat=chat,
                group=True,
                thread_ref=thread_ref,
                message_id=message_id,
            )
            if refusal := self.quota_denial(conn, owner):
                emit(
                    "request.rejected",
                    actor=actor_ref(owner),
                    reason="identity_unavailable"
                    if refusal == IDENTITY_UNAVAILABLE
                    else "token_quota",
                )
                self.reply(conn, owner, chat, refusal, message_id)
                return
            if state.get("busy"):
                # A message sent right after another, before work starts, joins it: people
                # send a question, then an image, then a correction.
                if self.store.update_waiting(
                    conn, state["busy"], lambda payload: merged_payload(payload, new)
                ):
                    emit(
                        "request.merged",
                        actor=actor_ref(owner),
                        request_id=state["busy"],
                    )
                elif self.can_steer(conn, state["busy"], new):
                    # Work has started: the message goes to the running agent as a correction
                    # , the way Hermes's own gateway redirects a run.
                    self.store.add_job(
                        conn,
                        owner,
                        "steer",
                        dict(
                            target=state["busy"],
                            text=new["text"],
                            message_id=message_id,
                            chat=chat,
                            thread=new.get("thread"),
                            group=bool(new.get("group")),
                        ),
                    )
                    emit(
                        "request.steering",
                        actor=actor_ref(owner),
                        request_id=state["busy"],
                    )
                else:
                    emit("request.rejected", actor=actor_ref(owner), reason="busy")
                    self.reply(conn, owner, chat, BUSY_REPLY, message_id)
                return
            if len(text) > MAX_TEXT:
                self.reply(
                    conn,
                    owner,
                    chat,
                    "Send a message under 32,000 characters.",
                    message_id,
                )
                return
            job = self.store.add_job(
                conn,
                owner,
                "prepare",
                dict(
                    new,
                    message_id=message_id,
                    version=state.get("credential_version"),
                ),
                delay=self.message_window,
            )
            state["busy"] = job
            if (
                self.agent_capacity
                and self.store.count_jobs(conn, "prepare") > self.agent_capacity
            ):
                self.out(
                    conn,
                    owner,
                    chat,
                    "All answer slots are busy. Your question is queued and I'll "
                    "answer as soon as one frees up.",
                    reply_to=message_id,
                    thread=True,
                )
        emit("request.accepted", actor=actor_ref(owner), request_id=job)

    def action(
        self, tenant, sender, chat, event, value, message_id=None, *, transport=None
    ):
        owner = tenant + ":" + sender
        confirm = value.get("action") == "confirm_group_action"
        if transport is not None and confirm:
            self.access.verify_for_admission(
                owner,
                transport,
                group=chat,
                governance=self.governance,
            )
        with self.store.user(owner, "action:" + event) as (conn, state):
            if conn is None:
                return TOASTS["received"]
            if confirm:
                return self.confirm_group_action(conn, owner, state, chat, value)
            if self.cloud is None:
                return TOASTS["inactive"]
            action_group = state.get("setup_group")
            action = value.get("action")
            if action == "open_setup":
                token = value.get("reopen_token")
                expected = state.get("setup_reopen_token")
                known = set(state.get("setup_card_ids", []))
                if state.get("card_message_id"):
                    known.add(state["card_message_id"])
                if (
                    state.get("chat") != chat
                    or not message_id
                    or message_id not in known
                    or not isinstance(token, str)
                    or not isinstance(expected, str)
                    or not secrets.compare_digest(token.encode(), expected.encode())
                ):
                    return TOASTS["open_failed"]
                if not action_group:
                    return TOASTS["not_allowed"]
                self.queue_setup(conn, owner, state, action_group)
                return "Checking your group access."
            if state.get("chat") != chat or value.get("version") != state.get(
                "card_version"
            ):
                return TOASTS["inactive"]
            if message_id is not None and message_id != state.get("card_message_id"):
                return TOASTS["inactive"]
            if state.get("card_closed"):
                return TOASTS["closed"]
            if time.time() >= state.get("card_expires", 0):
                self.close_card(
                    conn,
                    owner,
                    state,
                    "Setup expired",
                    "This setup timed out. Open setup to try again.",
                )
                return TOASTS["expired"]
            if action == "close":
                if not state.get("credential"):
                    state.pop("resume", None)  # "Not now": nothing is left waiting
                self.close_card(
                    conn,
                    owner,
                    state,
                    note="You're still connected. Your chat is saved."
                    if state.get("credential")
                    else "Open setup when you're ready to connect.",
                )
                return TOASTS["settings_closed"]
            if state.get("busy"):
                return TOASTS["busy"]
            if state.get("auth_job") and action != "cancel":
                return TOASTS["updating"]
            if action == "page":
                page = int(value.get("page", 0))
                state["page"] = max(
                    0,
                    min(page, len(choice_pages(state.get("choices", []))) - 1),
                )
                self.show(conn, owner, state)
                return TOASTS["page_updated"]
            if action in ("account", "role"):
                expected = "accounts" if action == "account" else "roles"
                if state.get("stage") != expected or value.get("choice") not in {
                    x["id"] for x in state.get("choices", [])
                }:
                    return TOASTS["choose"]
            if action not in (
                "connect",
                "switch",
                "account",
                "role",
                "cancel",
                "disconnect",
            ):
                return TOASTS["unknown"]
            if action in ("cancel", "disconnect"):
                for key in (
                    "attempt",
                    "stage",
                    "choices",
                    "user_code",
                    "verification_uri",
                ):
                    state.pop(key, None)
                state.pop("resume", None)
                state["login_version"] = str(uuid.uuid4())
                if action == "disconnect":
                    for key in (
                        "credential",
                        "sso",
                        "identity",
                        "credential_version",
                        "expiration",
                    ):
                        state.pop(key, None)
                self.close_card(
                    conn,
                    owner,
                    state,
                    "Disconnected" if action == "disconnect" else "Setup cancelled",
                    "Disconnected. Your chat is saved."
                    if action == "disconnect"
                    else "No changes made.",
                )
                return TOASTS["done"]
            if not action_group or self.access.paused(conn):
                return TOASTS["not_allowed"]
            if action == "connect":
                for key in ("setup_sso", "pending_account", "pending_account_label"):
                    state.pop(key, None)
                state["login_version"] = str(uuid.uuid4())
            state["auth_job"] = self.store.add_job(
                conn,
                owner,
                "auth",
                {
                    "action": action,
                    "choice": value.get("choice"),
                    "group": action_group,
                    "login_version": state.get("login_version"),
                },
            )
            self.show(conn, owner, state)
            emit(
                "auth.requested",
                actor=actor_ref(owner),
                action=action,
                job_id=state["auth_job"],
            )
            return TOASTS["updating"]

    def confirm_group_action(self, conn, owner, state, chat, value):
        identifier = value.get("id")
        expired = "This confirmation is no longer available to you."
        if not isinstance(identifier, str) or len(identifier) != 32:
            return expired
        row = group_actions.pending(conn, owner, chat, identifier)
        if not row or self.access.admission(conn, owner, chat):
            return expired
        if state.get("busy"):
            return "Wait for your running request to finish, then confirm again."
        if refusal := self.quota_denial(conn, owner):
            return refusal
        original = self.vault.open("action:" + identifier, row["sealed"])
        if not original.get("message_id"):
            return expired
        if not group_actions.authorize(conn, identifier):
            return expired
        state["busy"] = self.store.add_job(
            conn,
            owner,
            "prepare",
            {
                "chat": chat,
                "message_id": original["message_id"],
                "thread": row["thread"],
                "thread_ref": original.get("thread_ref"),
                "in_thread": True,
                "group": True,
                "text": "The requester confirmed a pending action. Call run_confirmed_action with no arguments and use its result to answer the original request.",
                "approved_action": identifier,
                "version": state.get("credential_version"),
            },
        )
        return "Confirmed. I will continue in this thread."

    def pending_confirmation(self, conn, job):
        payload = self.store.job_payload(conn, job["id"]) or {}
        identifier = payload.get("confirmation_id")
        if not identifier:
            return None
        row = group_actions.pending(conn, job["owner"], payload["chat"], identifier)
        if not row:
            return None
        action = self.vault.open("action:" + identifier, row["sealed"])
        return {
            "label": action["label"],
            "value": {"action": "confirm_group_action", "id": identifier},
        }

    def remember_activity(self, job, message, started_at):
        with self.store.engine.begin() as conn:
            if self.store.owns(conn, job):
                self.store.note_job(
                    conn,
                    job["id"],
                    activity_message=message,
                    activity_started_at=started_at,
                )

    def resolve(self, owner, version, min_seconds=60):
        """The owner's credentials, renewed silently when they expire within min_seconds.

        If the SSO session has ended they are returned as they are while still valid; a
        caller that needs them to last (a long query) checks the remaining time itself."""
        with self.store.user(owner) as (_, state):
            if state.get("credential_version") != version or not state.get(
                "credential"
            ):
                raise AuthError("ConnectionChanged")
            cred = self.vault.open(owner + ":" + version, state["credential"])
            sso = self.vault.open(owner + ":sso", state["sso"])
            account, role = state["account"], state["role"]
        if cred["expires"] < time.time() + min_seconds:
            if sso["expires"] <= time.time():
                if cred["expires"] > time.time():
                    return cred
                raise AuthError("LoginExpired")
            cred = self.cloud.credentials(sso["token"], account, role)
            with self.store.user(owner) as (_, state):
                if state.get("credential_version") != version:
                    raise AuthError("ConnectionChanged")
                state["credential"] = self.vault.seal(owner + ":" + version, cred)
                state["expiration"] = cred["expiration"]
        return cred

    def auth(self, job, snapshot):
        owner, p = job["owner"], job["payload"]
        if p.get("login_version") != snapshot.get("login_version"):
            return {}, None
        action = p["action"]
        if action == "connect":
            attempt = self.cloud.start()
            return {
                "attempt": self.vault.seal(owner + ":attempt", attempt),
                "user_code": attempt["code"],
                "verification_uri": attempt["uri"],
                "stage": "device",
                "auth_retry_notice": False,
            }, "poll"
        if action == "poll":
            attempt = self.vault.open(owner + ":attempt", snapshot["attempt"])
            if time.time() >= attempt["deadline"]:
                raise AuthError("LoginExpired")
            sso = attempt.get("authorized_sso")
            if sso is None:
                sso = self.cloud.poll(attempt)
                # Persist authorization before a separate account-list request. A
                # retry must not consume the same device grant a second time.
                with self.store.user(owner) as (conn, current):
                    if not self.store.owns(conn, job) or current.get(
                        "login_version"
                    ) != p.get("login_version"):
                        return {}, None
                    attempt["authorized_sso"] = sso
                    current["attempt"] = self.vault.seal(owner + ":attempt", attempt)
                    current["stage"] = "loading_accounts"
                    for key in ("user_code", "verification_uri", "auth_retry_notice"):
                        current.pop(key, None)
                    self.show(conn, owner, current)
                emit("auth.browser_confirmed", **job_fields(job))
        else:
            if snapshot.get("setup_sso"):
                sso = self.vault.open(owner + ":setup_sso", snapshot["setup_sso"])
            elif snapshot.get("sso"):
                sso = self.vault.open(owner + ":sso", snapshot["sso"])
            else:
                raise AuthError("LoginRequired")
        if sso["expires"] <= time.time():
            raise AuthError("LoginExpired")
        changes = {
            "setup_sso": self.vault.seal(owner + ":setup_sso", sso),
            "page": 0,
            "auth_retry_notice": False,
        }
        if action in ("poll", "switch"):
            accounts = self.cloud.accounts(sso["token"])
            changes.update(
                stage="accounts",
                choices=accounts,
            )
        elif action == "account":
            roles = self.cloud.roles(sso["token"], p["choice"])
            changes.update(
                stage="roles",
                pending_account=p["choice"],
                pending_account_label=next(
                    x["label"] for x in snapshot["choices"] if x["id"] == p["choice"]
                ),
                choices=roles,
            )
        elif action == "role":
            # Revalidate authorization server-side; callback values are never credential authority.
            account = snapshot["pending_account"]
            roles = self.cloud.roles(sso["token"], account)
            if p["choice"] not in {x["id"] for x in roles}:
                raise AuthError("RoleUnavailable")
            cred = self.cloud.credentials(sso["token"], account, p["choice"])
            identity = self.identity(cred)
            if str(identity.get("account_id")) != str(account):
                raise AuthError("IdentityMismatch")
            version = str(uuid.uuid4())
            changes.update(
                stage="",
                choices=[],
                identity=identity,
                sso=self.vault.seal(owner + ":sso", sso),
                account=account,
                role=p["choice"],
                account_label=snapshot.get("pending_account_label", account),
                role_label=next(x["label"] for x in roles if x["id"] == p["choice"]),
                credential_version=version,
                credential=self.vault.seal(owner + ":" + version, cred),
                expiration=cred["expiration"],
            )
        return changes, None

    def checked_delivery(self, job, payload):
        if not payload.get("gb_group") or self.group_brain_send_valid(payload):
            return payload
        emit("knowledge.delivery_suppressed", logging.WARNING, **job_fields(job))
        return dict(
            payload,
            text=GROUP_BRAIN_SUPPRESSED,
            file=None,
            preview_card=None,
            fallback_card=None,
            usage_footer="",
            attachments=[],
            attachment_keys=[],
            gb_group=None,
        )

    def send_reply(self, job, transport):
        payload = self.checked_delivery(job, job["payload"])
        attachments = payload.get("attachments") or ()
        keys = list(payload.get("attachment_keys") or ())
        for file in attachments[len(keys) :]:
            keys.append(transport.upload_file(file))
            with self.store.engine.begin() as conn:
                if not self.store.owns(conn, job):
                    return
                self.store.note_job(conn, job["id"], attachment_keys=keys)
            # Retry handling rebases from this object, so retain each committed upload.
            job["payload"] = dict(job["payload"], attachment_keys=keys)
        if attachments:
            payload = self.checked_delivery(job, dict(payload, attachment_keys=keys))
        transport.send(payload, job["id"])
        with self.store.user(job["owner"]) as (conn, _):
            self.store.finish(conn, job)

    def work(self, job, transport):
        owner = job["owner"]
        with self.store.user(owner) as (conn, state):
            if not self.store.owns(conn, job):
                return
            snapshot = dict(state)
        if job["kind"] not in {
            "auth",
            "setup",
            "prepare",
            "send",
            "stop",
            "steer",
            "expire_card",
            "forget",
            "dashboard_login",
        }:
            with self.store.user(owner) as (conn, _):
                self.store.finish(conn, job)
            emit("job.discarded", reason="unsupported_kind")
            return
        if job["kind"] == "setup":
            self.work_setup(job, transport)
            return
        if job["kind"] == "dashboard_login":
            with self.store.user(owner) as (conn, _):
                if not self.store.owns(conn, job):
                    return
                pending = self.access.pending(conn, job["payload"]["hash"])
            if pending:
                delivery = self.vault.open(
                    "dashboard-login:" + owner, job["payload"]["sealed"]
                )
                transport.send(
                    dict(
                        chat=delivery["chat"],
                        reply_to=delivery["reply_to"],
                        thread=True,
                        card=None,
                        text="Dashboard login token (valid for 5 minutes, one use):\n"
                        + delivery["token"],
                    ),
                    job["id"],
                )
            with self.store.user(owner) as (conn, _):
                if self.store.owns(conn, job):
                    conn.execute(
                        update(jobs)
                        .where(jobs.c.id == job["id"])
                        .values(payload={"delivered": pending})
                    )
                    self.store.finish(conn, job)
            return
        if job["kind"] == "expire_card":
            with self.store.user(owner) as (conn, state):
                if not self.store.owns(conn, job):
                    return
                if state.get("card_version") == job["payload"][
                    "version"
                ] and not state.get("card_closed"):
                    self.close_card(
                        conn,
                        owner,
                        state,
                        "Setup expired",
                        "This setup timed out. Open setup to try again.",
                    )
                self.store.finish(conn, job)
            return
        if job["kind"] == "send" and job["payload"].get("card"):
            self.deliver_card(job, transport)
            return
        if job["kind"] == "send":
            self.send_reply(job, transport)
            return
        if job["kind"] == "stop":
            self.stop_run(job)
            return
        if job["kind"] == "steer":
            self.steer_run(job)
            return
        if job["kind"] == "prepare":
            if (
                not job["payload"].get("group")
                or not job["payload"].get("chat")
                or snapshot.get("busy") != job["id"]
            ):
                with self.store.user(owner) as (conn, state):
                    if state.get("busy") == job["id"]:
                        state.pop("busy", None)
                    self.store.finish(conn, job)
                return
            # A recovered pre-upgrade Hermes request/run is already started. New queued
            # jobs recheck admission once, before credentials, attachments or the model.
            if not job["payload"].get("admission_started") or (
                self.governance and not job["payload"].get("governance")
            ):
                self.access.verify_for_admission(
                    owner,
                    transport,
                    group=job["payload"].get("chat")
                    if job["payload"].get("group")
                    else None,
                    governance=self.governance,
                )
            with self.store.user(owner) as (conn, state):
                if not self.store.owns(conn, job):
                    return
                governance_snapshot = job["payload"].get("governance")
                started = (
                    job["payload"].get("admission_started")
                    and (not self.governance or governance_snapshot)
                ) or conn.execute(
                    select(turns.c.session_id).where(turns.c.job_id == job["id"])
                ).first()
                if not started:
                    rejection = self.access.admission(
                        conn,
                        owner,
                        job["payload"].get("chat")
                        if job["payload"].get("group")
                        else None,
                    )
                    refusal = NOT_ALLOWED if rejection else None
                    if not rejection and self.governance:
                        governance_snapshot = self.governance.quota(conn, owner)
                        refusal = self.governed_refusal(conn, governance_snapshot)
                    if refusal:
                        if rejection != "unregistered_group":
                            self.reply(
                                conn,
                                owner,
                                job["payload"].get("chat") or state["chat"],
                                refusal,
                                job["payload"]["message_id"],
                            )
                        if state.get("busy") == job["id"]:
                            state.pop("busy", None)
                        self.store.finish(conn, job)
                        return
                activity_start = job["payload"].get("activity_started_at")
                if activity_start is None and started:
                    activity_start = conn.scalar(
                        select(func.min(turns.c.created)).where(
                            turns.c.job_id == job["id"]
                        )
                    )
                job["payload"] = dict(job["payload"], admission_started=True)
                if governance_snapshot:
                    job["payload"]["governance"] = governance_snapshot
                conn.execute(
                    update(jobs)
                    .where(jobs.c.id == job["id"])
                    .values(payload=job["payload"])
                )
            version = job["payload"]["version"]
            if version:
                # Renews silently while the SSO session is valid. An expired sign-in no
                # longer refuses the question: it is cleared and answered without
                # credentials, and the card is offered if a tool needs one.
                version = self.expire_stale(owner, job, version)
            group_tools = self.group_tools(job["payload"])
            remote, remote_failed = self.unseen_messages(transport, job["payload"])
            names = (
                group_speaker_names(transport, owner, remote)
                if job["payload"].get("group")
                else {}
            )
            resolved = resolve_attachments(
                transport,
                job["payload"],
                lark_in_group=bool(
                    group_tools
                    and group_tools.intersection(
                        {"lark_read", "lark_image", "lark_file_read", "lark_file_edit"}
                    )
                ),
                owner=owner,
                remote=remote,
                lark_thread=bool(
                    group_tools
                    and group_tools.intersection(
                        {"lark_read", "lark_image", "lark_file_read", "lark_file_edit"}
                    )
                ),
            )
            if any(resolved.counts().values()):
                emit("message.resolved", **job_fields(job), **resolved.counts())
            self.person_email(owner, snapshot, transport)
            with self.store.user(owner) as (conn, state):
                if not self.store.owns(conn, job):
                    return
                thread = job["payload"].get("thread")
                group = bool(job["payload"].get("group"))
                model = None
                if governance_snapshot:
                    model = governance_snapshot["model"]
                history = self.store.history(
                    conn, owner, self.history_limit, session=thread, shared=group
                )
                # The conversation a quoted message belongs to: a person who replies to an old
                # answer, or to a message of another thread, brings that conversation along.
                earlier = []
                if resolved.quote_session and resolved.quote_session != thread:
                    earlier = self.store.history(
                        conn,
                        owner,
                        self.history_limit,
                        session=resolved.quote_session,
                        shared=group,
                    )
                chat = job["payload"].get("chat") or state["chat"]
                words = job["payload"]["text"]
                own_ids = {
                    *(job["payload"].get("message_ids") or ()),
                    job["payload"]["message_id"],
                }
                item = dict(
                    role="user",
                    content="\n".join(t for t in (words, history_note(resolved)) if t),
                    credential_version=version,
                    session=thread,
                    message_id=job["payload"]["message_id"],
                )
                unseen = unseen_for_prompt(remote, history, earlier, own_ids, names)
                if remote_failed:
                    resolved.notes.append(
                        "The earlier messages of this thread could not be read (the bot may lack the "
                        "permission to read message history). Mention it only if the user asks about them."
                    )
                if group:
                    requester = speaker_of(owner)
                    item["speaker"] = requester
                    if requester in names:
                        item["speaker_name"] = names[requester]
                    earlier = label_history(earlier, names, requester)
                    history = label_history(history, names, requester)
                request = dict(
                    schema_version=1,
                    request_id=job["id"],
                    owner=owner,
                    session_id=thread or state["session"],
                    message_id=job["payload"]["message_id"],
                    message=item,
                    history=history,
                    earlier_history=earlier,
                    thread_unseen=unseen,
                    cloud_identity=state.get("identity") if version else None,
                    credential_version=version,
                    artifact_references=[],
                    quote=resolved.quote,
                    documents=resolved.documents,
                    forwarded=resolved.forwarded,
                    notes=resolved.notes,
                    images=resolved.images,
                    text=words,
                    requester_name=item.get("speaker_name"),
                    group=group,
                    group_tools=sorted(group_tools | BASELINE)
                    if group and group_tools is not None
                    else None,
                    model=model,
                )
                if group and group_tools:
                    request["tool_labels"] = dict(
                        conn.execute(
                            select(tool_registry.c.name, tool_registry.c.label).where(
                                tool_registry.c.name.in_(group_tools),
                                ~tool_registry.c.retired,
                                tool_registry.c.label != tool_registry.c.name,
                                tool_registry.c.label != "",
                            )
                        ).all()
                    )
                if governance_snapshot and governance_snapshot["effort"] is not None:
                    request["reasoning_effort"] = governance_snapshot["effort"]
            image_notice = ""
            if resolved.images and self.can_read_images(model) is False:
                # The model in use cannot read images: leave them out, tell the person, and
                # tell the model so it does not pretend it saw them.
                count = len(resolved.images)
                request["images"] = []
                resolved.notes.append(
                    f"The user attached {count} image(s) but the model in use cannot read images. "
                    "Say so plainly and do not describe or guess what is in them."
                )
                nothing_else = not (
                    words.strip()
                    or resolved.documents
                    or resolved.quote
                    or resolved.forwarded
                )
                image_notice = images_skipped(
                    count,
                    group,
                    [] if group or self.governance else self.vision_choices(),
                )
                if nothing_else:
                    with self.store.user(owner) as (conn, state):
                        if self.store.owns(conn, job):
                            self.reply(
                                conn,
                                owner,
                                chat,
                                image_notice,
                                job["payload"]["message_id"],
                            )
                            state.pop("busy", None)
                            self.store.finish(conn, job)
                    self.forget(job)
                    return
            urls, hidden = thread_images(
                transport,
                request["thread_unseen"],
                len(request["images"]),
                self.can_read_images(model) is not False,
            )
            if urls:
                request["images"].extend(urls)
                resolved.notes.append(
                    f"{len(urls)} image(s) from other people in this thread are attached after the "
                    f"user's own {len(request['images']) - len(urls)}; thread messages mark them as "
                    "[image N], counting all attached images in order."
                )
            if hidden:
                resolved.notes.append(
                    f"{hidden} image(s) in this thread were not shown to you "
                    "(marked [image, not shown]). Say so if asked about them; do not guess."
                )
            google_allowed = google_links(job["payload"].get("text", ""))
            if google_allowed:
                request["google_links"] = True
                if not group or (
                    group_tools is not None and "google_read" in group_tools
                ):
                    with self.store.user(owner) as (conn, _):
                        if self.store.owns(conn, job):
                            self.store.note_job(
                                conn, job["id"], google_links=google_allowed
                            )
            if resolved.lark_links:
                request["lark_links"] = True
            if resolved.lark_thread_links:
                request["lark_thread_links"] = [
                    link.url for link in resolved.lark_thread_links
                ]
            if resolved.lark_allowed or resolved.lark_thread_links:
                # Keep earlier references separate so edits through them require confirmation.
                with self.store.user(owner) as (conn, _):
                    if self.store.owns(conn, job):
                        self.store.note_job(
                            conn,
                            job["id"],
                            lark_links=resolved.lark_allowed,
                            lark_thread_links=[
                                link.token for link in resolved.lark_thread_links
                            ],
                        )
            if self.agent is None:
                raise RuntimeError("Gateway agent is required")
            activity_start = (
                activity_start if activity_start is not None else time.time()
            )
            progress = Progress(
                transport,
                chat,
                job["payload"]["message_id"],
                job_fields(job),
                key=job["id"],
                previous=job["payload"].get("activity_message"),
                remember=lambda message: self.remember_activity(
                    job, message, activity_start
                ),
                prior_elapsed=max(0, time.time() - activity_start),
                lost=job["lost"].is_set if job.get("lost") else None,
            )
            progress.start()
            answered, failure = False, None
            try:
                session_id, reply = self.ask_agent(
                    job, owner, request, progress, chat=chat
                )
                answered = True
            except AgentError as exc:
                failure = exc.kind
                raise
            finally:
                progress.finish(ok=answered, failure=failure)
                self.record_usage(
                    job,
                    owner,
                    thread,
                    chat,
                    group,
                    override=bool(model) and not self.governance,
                    model=model,
                )
            learning = None
            if group:
                try:
                    learning = prepare_passive_learning(self, job, session_id)
                except Exception as exc:
                    emit(
                        "knowledge.passive",
                        logging.WARNING,
                        **job_fields(job),
                        outcome="verification_failed",
                        error_type=type(exc).__name__,
                    )
            with self.store.user(owner) as (conn, state):
                if not self.store.owns(conn, job):
                    return
                current = state.get("busy") == job["id"]
                if current:
                    reply, gb = self.guard_group_knowledge(conn, job, reply)
                    self.store.message(conn, owner, item)
                    # Corrections sent while it ran come right after the question, before the answer.
                    for correction in state.pop("steers", []):
                        self.store.message(conn, owner, correction)
                    self.store.message(
                        conn,
                        owner,
                        dict(
                            role="assistant",
                            content=GROUP_BRAIN_HISTORY if gb else reply,
                            session=thread,
                        ),
                    )
                    # Preview rows were never seen by the model: they are attached
                    # here, from our own store, and are not saved in history.
                    previews, files = self.take_previews(conn, owner, job, session_id)
                    footer = self.token_footer(conn, owner, job, in_group=True)
                    sign_in = self.prepare_reply_sign_in(conn, owner, state, job)
                    response = "\n\n".join(
                        part for part in (image_notice, reply, sign_in) if part
                    )
                    answer, code_files = self.fit_answer(response, previews)
                    body = with_blocks(answer, [p["text"] for p in previews])
                    confirmation = self.pending_confirmation(conn, job)
                    attachments = [*code_files, *files]
                    reply_to = job["payload"]["message_id"]
                    reply_chat = job["payload"].get("chat") or state["chat"]
                    try:
                        card = reply_card(answer, body, previews, footer, confirmation)
                    except (KeyError, ValueError, TypeError) as exc:
                        emit(
                            "card.build_failed",
                            logging.WARNING,
                            error_type=type(exc).__name__,
                        )
                        card = None
                    self.out(
                        conn,
                        owner,
                        reply_chat,
                        body,
                        reply_to=reply_to,
                        thread=True,
                        preview_card=card,
                        single_reply=True,
                        usage_footer=footer,
                        fallback_card=confirmation_fallback(
                            answer, footer, confirmation
                        ),
                        answer_to=owner.partition(":")[2] if group else None,
                        gb=gb,
                        attachments=attachments if confirmation is None else (),
                    )
                    if confirmation and attachments:
                        self.out_files(
                            conn,
                            owner,
                            reply_chat,
                            attachments,
                            reply_to=reply_to,
                            thread=True,
                            gb=gb,
                        )
                    finish_passive_learning(conn, job, learning)
                    self.store.end_turn(conn, session_id, "done")
                    state.pop("busy", None)
                    self.store.finish(conn, job)
            self.forget(job)
            if current:
                emit(
                    "handoff.prepared", **job_fields(job), history_messages=len(history)
                )
            return
        payload = job["payload"]
        group = payload.get("group")
        member = (
            self.cloud is not None
            and group == snapshot.get("setup_group")
            and payload.get("login_version") == snapshot.get("login_version")
            and self.access.verify_setup_member(owner, group, transport)
        )
        with self.store.user(owner) as (conn, state):
            if not self.store.owns(conn, job):
                return
            if not member or self.access.admission(conn, owner, group):
                self.finish_rejected_setup(conn, job, state)
                return
        changes, follow = self.auth(job, snapshot)
        if payload["action"] == "role":
            # The provider call may have outlasted group membership or a policy change.
            member = self.access.verify_setup_member(owner, group, transport)
        applied = False
        with self.store.user(owner) as (conn, state):
            if not self.store.owns(conn, job):
                return
            if not member or self.access.admission(conn, owner, group):
                self.finish_rejected_setup(conn, job, state)
                return
            if (
                payload.get("login_version") == state.get("login_version")
                and state.get("setup_group") == group
            ):
                state.update(changes)
                applied = True
                state.pop("auth_job", None)
                if follow:
                    attempt = self.vault.open(owner + ":attempt", state["attempt"])
                    self.store.add_job(
                        conn,
                        owner,
                        "auth",
                        dict(
                            action="poll",
                            login_version=state["login_version"],
                            group=group,
                        ),
                        time.time() + attempt["interval"],
                    )
                elif job["payload"]["action"] == "poll":
                    for key in ("attempt", "user_code", "verification_uri"):
                        state.pop(key, None)
                if job["payload"]["action"] == "role":
                    resumed = self.resume_request(conn, owner, state)
                    self.close_card(
                        conn,
                        owner,
                        state,
                        "Connected",
                        "You're connected. Continuing your request."
                        if resumed
                        else "You're connected. Send your question when ready.",
                    )
                else:
                    self.show(conn, owner, state)
            self.store.finish(conn, job)
        if applied and changes:
            name = {
                "connect": "auth.awaiting_browser",
                "poll": "auth.authorized",
                "account": "auth.roles_loaded",
                "switch": "auth.accounts_loaded",
                "role": "auth.connected",
            }.get(job["payload"]["action"], "auth.updated")
            emit(name, **job_fields(job), option_count=len(changes.get("choices", [])))

    def finish_rejected_setup(self, conn, job, state):
        if job["payload"].get("login_version") == state.get(
            "login_version"
        ) and state.get("chat"):
            state.pop("resume", None)
            self.close_card(
                conn, job["owner"], state, "Setup closed", SETUP_GROUP_REQUIRED
            )
        self.store.finish(conn, job)

    def model_reply(self, conn, argument):
        policy = self.governance.policy(conn)[0] if self.governance else {}
        model = (
            policy.get("model") or self.provider_model or self.store.default_model(conn)
        )
        effort = self.governance.active_effort(policy) if self.governance else None
        current = f"**Model:** {model or 'Hermes default'}"
        if effort:
            current += f"\n**Reasoning effort:** {effort}"
        if argument.strip():
            current += "\nThe dashboard controls the model for group requests."
        return current

    def request_stop(self, conn, owner, state, chat=None, thread=None):
        """Queue a stop for the turn in progress; the reply says what happened."""
        target = state.get("busy")
        if not target:
            return NOTHING_RUNNING
        payload = self.store.job_payload(conn, target) or {}
        if chat is not None and (
            payload.get("chat") != chat or payload.get("thread") != thread
        ):
            return NOTHING_RUNNING
        if state.get("stopping") != target:
            state["stopping"] = target
            self.store.add_job(conn, owner, "stop", {"target": target})
        return STOPPING

    def note_run(self, session_id):
        """Callback that saves the Hermes run id on the turn, so /stop can find it."""

        def note(run_id):
            with self.store.engine.begin() as conn:
                self.store.set_run_id(conn, session_id, run_id)

        return note

    stop_wait = 10  # seconds a stop waits for a run that has not started yet

    def stop_run(self, job):
        """Cancel the owner's running turn. The external call stays outside transactions.

        The run may not exist yet (history and credentials are prepared first), so wait
        briefly for it and give up when the turn ends. The turn then ends through the
        normal path: its follow sees the cancelled run and the failure handler says
        "Stopped." Only this owner's running turn can be found.
        """
        owner, target = job["owner"], job["payload"]["target"]
        deadline = time.monotonic() + self.stop_wait
        accepted = False
        while self.agent:
            with self.store.engine.begin() as conn:
                run_id = self.store.run_to_stop(conn, target, owner)
            if run_id:
                accepted = self.agent.stop(run_id)
                break
            with self.store.user(owner) as (_, state):
                running = state.get("busy") == target
            if not running or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        with self.store.user(owner) as (conn, state):
            if not self.store.owns(conn, job):
                return
            if not accepted and state.get("busy") == target:
                state.pop("stopping", None)
                target_payload = self.store.job_payload(conn, target) or {}
                if target_payload.get("chat") and target_payload.get("message_id"):
                    self.reply(
                        conn,
                        owner,
                        target_payload["chat"],
                        STOP_FAILED,
                        target_payload["message_id"],
                    )
            self.store.finish(conn, job)
        emit("stop.requested", **job_fields(job), accepted=accepted)

    def can_read_images(self, model):
        """True or False when VISION_MODELS names the models that read images, None when it does not
        (unknown: images are sent and a failure with one is reported). `model` is the person's pick
        for this thread, or None for the default."""
        if not self.vision_models:
            return None
        used = model or self.provider_model
        if not used:
            return None
        return used.lower() in {name.lower() for name in self.vision_models}

    def vision_choices(self):
        """The models a person may pick in a direct chat that read images."""
        wanted = {name.lower() for name in self.vision_models}
        return [name for name in self.available_models if name.lower() in wanted]

    def unseen_messages(self, transport, payload):
        """(messages, failed): what people wrote in this group thread without mentioning the bot, read
        from Lark. ([], False) anywhere else; ([], True) when Lark refuses or is unreachable."""
        ref = payload.get("thread_ref")
        if not (
            payload.get("group")
            and payload.get("in_thread")
            and ref
            and hasattr(transport, "thread_messages")
        ):
            return [], False
        try:
            return transport.thread_messages(ref, 40), False
        except Exception as exc:
            emit(
                "thread.context_unavailable",
                logging.WARNING,
                error_type=type(exc).__name__,
            )
            return [], True

    def group_tools(self, payload):
        """The tools enabled for the group a message came from, or None outside a group.
        An unreadable scope or a database error is an empty set: nothing beyond the always-on skills."""
        if not payload.get("group"):
            return None
        scope = group_chat_scope(payload.get("chat"))
        if scope is None:
            return frozenset()
        try:
            with self.store.engine.begin() as conn:
                return self.store.scope_tools(conn, scope)
        except Exception as exc:
            emit(
                "group.scope_unavailable", logging.ERROR, error_type=type(exc).__name__
            )
            return frozenset()

    def can_steer(self, conn, busy_job, new):
        """True when a message sent while a turn runs may go to the running agent: plain text, a
        Hermes agent to take it, and the same conversation as the turn (the same chat kind, and
        not typed inside another thread). Anything else gets the busy reply."""
        if not self.agent or not hasattr(self.agent, "steer"):
            return False
        text = new["text"]
        if new["attachments"] or new["forwards"] or not text.strip():
            return False
        if len(text) > MAX_TEXT:
            return False
        old = self.store.job_payload(conn, busy_job)
        return bool(
            old
            and old.get("chat") == new.get("chat")
            and old.get("thread") == new.get("thread")
        )

    def steer_run(self, job):
        """Pass a message sent during a turn to the owner's running agent. The external call stays
        outside transactions.

        Like a stop, it waits briefly for a run that has not started yet. When Hermes takes the
        message it also goes into the chat history (the turn's own answer is saved later, so the
        order stays right). The running activity card shows the accepted update.
        When Hermes does not take it (the run ended, or is not accepting input) the person gets
        the busy reply, as before.
        """
        owner, payload = job["owner"], job["payload"]
        target = payload["target"]
        deadline = time.monotonic() + self.stop_wait
        accepted = False
        while self.agent:
            with self.store.engine.begin() as conn:
                if self.governance and self.access.admission(
                    conn, owner, payload["chat"] if payload.get("group") else None
                ):
                    break
                running = self.store.running_turn(conn, target, owner)
            if running:
                accepted = self.agent.steer(running[0], payload["text"])
                break
            with self.store.user(owner) as (_, state):
                busy = state.get("busy") == target
            if not busy or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        with self.store.user(owner) as (conn, state):
            if not self.store.owns(conn, job):
                return
            thread, chat = payload.get("thread"), payload["chat"]
            if not accepted:
                self.reply(conn, owner, chat, BUSY_REPLY, payload["message_id"])
            else:
                item = dict(
                    role="user",
                    content=payload["text"],
                    credential_version=state.get("credential_version"),
                    session=thread,
                )
                if payload.get("group"):
                    item["speaker"] = speaker_of(owner)
                if state.get("busy") != target:
                    # The turn ended while Hermes was taking the text. Keep it as context for the
                    # next turn, in order after the answer, and do not claim a redirect.
                    self.store.message(conn, owner, item)
                else:
                    state["steers"] = [*state.get("steers", []), item][-STEERS_KEPT:]
            self.store.finish(conn, job)
        emit("steer.requested", **job_fields(job), accepted=accepted)

    def ask_agent(self, job, owner, request, progress=None, chat=None):
        """Ask Hermes for this job's reply and return (session_id, text).

        The turn row binds the Hermes session id to the owner for tool lookups. A retry
        reuses the stored session and body. Only unknown_execution (Hermes restarted
        mid-run) moves to a fresh session, once.
        """
        version, ttl = request["credential_version"], self.agent.deadline + 60
        with self.store.engine.begin() as conn:
            turn = self.store.latest_turn(conn, job["id"])
            session_id = turn["session_id"] if turn else f"{job['id']}:1"
            if turn is None and request.get("group"):
                runtime = self.access.knowledge_runtime
                scope = group_chat_scope(chat)
                if runtime is not None and runtime.source is not None and scope:
                    policy = group_brain_effective(conn, runtime.config, scope)
                    if policy.allows("memory"):
                        request["passive_learning"] = self.store.note_job(
                            conn, job["id"], passive_generation=policy.generation
                        )
            body = turn["request"] if turn else self.agent.build(request)
            self.store.start_turn(
                conn,
                session_id,
                job["id"],
                owner,
                version,
                body,
                ttl,
                chat_id=chat,
                message_id=request.get("message_id"),
                chat_type="group" if request.get("group") else "p2p",
                thread=request.get("session_id"),
            )
        started = time.monotonic()
        on_event = progress.event if progress else None
        try:
            reply = self.agent.send(
                body,
                session_id,
                on_event=on_event,
                on_run=self.note_run(session_id),
                images=request.get("images"),
            )
        except AgentError as exc:
            if exc.kind != "unknown_execution" or session_id.endswith(":2"):
                raise
            emit("agent.recovering", **job_fields(job), reason=exc.kind)
            if progress:
                progress.event("restarted")
            with self.store.engine.begin() as conn:
                self.store.end_turn(conn, session_id, "unknown")
                session_id = f"{job['id']}:2"
                self.store.start_turn(
                    conn,
                    session_id,
                    job["id"],
                    owner,
                    version,
                    body,
                    ttl,
                    chat_id=chat,
                    message_id=request.get("message_id"),
                    chat_type="group" if request.get("group") else "p2p",
                    thread=request.get("session_id"),
                )
            reply = self.agent.send(
                body,
                session_id,
                on_event=on_event,
                on_run=self.note_run(session_id),
                images=request.get("images"),
            )
        emit(
            "agent.answered",
            **job_fields(job),
            attempt=session_id.rsplit(":", 1)[1],
            duration_ms=round((time.monotonic() - started) * 1000),
            reply_chars=len(reply),
        )
        return session_id, reply

    def deliver_card(self, job, transport):
        return deliver_card(self.store, job, transport)

    def forget(self, job):
        """Delete the Hermes sessions of a finished turn: they hold its tool results.

        A session whose turn is still running is never touched. Best effort.
        """
        if not self.agent:
            return
        try:
            with self.store.engine.begin() as conn:
                sessions = self.store.finished_sessions(conn, job["id"])
        except Exception:
            emit("agent.forget_failed", logging.WARNING, **job_fields(job))
            return
        for session_id in sessions:
            try:
                deleted = self.agent.forget(session_id)
            except Exception:
                deleted = False
            if not deleted:
                emit("agent.forget_failed", logging.WARNING, **job_fields(job))

    def take_previews(self, conn, owner, job, keep=None):
        """Delete a job's stored previews and files; return (previews, files) of the session
        that answered."""
        if not self.artifacts:
            return [], []
        sessions = self.store.job_sessions(conn, job["id"])
        return self.artifacts.take(conn, owner, sessions, keep)

    def cleanup(self, job):
        """After a failed turn: delete what it left in Hermes and in our preview store."""
        self.forget(job)
        try:
            with self.store.engine.begin() as conn:
                self.take_previews(conn, job["owner"], job)
        except Exception:
            emit("turn.cleanup_failed", logging.WARNING, **job_fields(job))

    def failure(self, job, error):
        self.handle_failure(job, error)
        if job["kind"] == "prepare":
            self.cleanup(job)

    CONNECTION_KEYS = (
        "credential",
        "credential_version",
        "identity",
        "sso",
        "expiration",
        "setup_sso",
        "attempt",
        "auth_job",
        "stage",
        "choices",
        "auth_retry_notice",
    )

    def clear_connection(self, state):
        """Forget a connection whose SSO session has ended; a new login starts clean."""
        for key in self.CONNECTION_KEYS:
            state.pop(key, None)
        state["login_version"] = str(uuid.uuid4())
        state["welcome"] = False

    def person_email(self, owner, snapshot, transport):
        """Resolve the caller’s email for admission and reporting."""
        lookup = getattr(transport, "email_of", None)
        if lookup is None:
            return
        wait = EMAIL_REFRESH if snapshot.get("email") else EMAIL_RETRY
        if time.time() - snapshot.get("email_checked", 0) < wait:
            return
        try:
            found = lookup(owner.split(":", 1)[1])
        except Exception as exc:
            emit("email.lookup_failed", logging.WARNING, error_type=type(exc).__name__)
            found = snapshot.get("email")
        with self.store.user(owner) as (conn, state):
            if isinstance(found, str) and found:
                state["email"] = found
            else:
                state.pop("email", None)
            state["email_checked"] = time.time()

    def expire_stale(self, owner, job, version):
        """The version to run the turn with: `version`, or None if its sign-in has ended.

        resolve() renews a credential silently while the SSO session is valid. Only an
        ended session clears it, and only if this is still the owner's current connection,
        so a stale job never clears a newer one. Other failures still fail the turn.
        """
        try:
            self.resolve(owner, version)
        except AuthError as exc:
            if str(exc) not in ("LoginExpired", "LoginRequired"):
                raise
            with self.store.user(owner) as (conn, state):
                if (
                    self.store.owns(conn, job)
                    and state.get("credential_version") == version
                ):
                    self.clear_connection(state)
                    state["sign_in_expired"] = True
            return None
        return version

    def prepare_reply_sign_in(self, conn, owner, state, job):
        """Offer private setup and put its guidance in the group answer."""
        expired = state.pop("sign_in_expired", False)
        if self.cloud is None or not self.store.turn_needs_login(conn, job["id"]):
            return ""
        payload = job["payload"]
        notice = ""
        if not state.get("credential"):
            state["resume"] = dict(
                message_id=payload["message_id"],
                thread=payload.get("thread"),
                at=time.time(),
                chat=payload.get("chat"),
                group=bool(payload.get("group")),
            )
            if payload.get("group"):
                notice = GROUP_SIGN_IN if state.get("chat") else GROUP_SETUP
        if state.get("chat"):
            self.queue_setup(conn, owner, state, payload["chat"], expired=expired)
        return notice

    def token_footer(self, conn, owner, job, in_group):
        """The line under an answer: what this request used. Under an answer shown to a group
        it is this request only; the month so far is a person's own business."""
        if not self.token_summary:
            return ""
        used = self.store.usage_job(conn, job["id"])
        used["attempts"] = len(self.store.job_sessions(conn, job["id"]))
        if self.governance:
            return usage_text.footer(used)
        month = (
            None if in_group else usage_text.total(self.store.usage_month(conn, owner))
        )
        return usage_text.footer(used, month, 0 if in_group else self.token_quota)

    def usage_reply(self, conn, owner, thread, in_thread, group):
        """/usage: in a direct chat, this month and, inside a thread, that conversation. In a
        group, only the thread's own total: a person's month stays in their direct chat."""
        if group:
            return usage_text.thread_report(self.store.usage_thread(conn, thread))
        mine = self.store.usage_thread(conn, thread, owner) if in_thread else None
        if self.governance:
            quota = self.governance.quota(conn, owner)
            if quota is None:
                return IDENTITY_UNAVAILABLE
            used, missing = self.governance.usage(conn, quota)
            return usage_text.governance_report(quota, used, missing, mine)
        month = self.store.usage_month(conn, owner)
        return usage_text.report(month, mine, self.token_quota)

    def resume_request(self, conn, owner, state):
        """Continue the request that needed this sign-in, without the user sending anything.

        Only a request that was left waiting for a sign-in within RESUME_WINDOW, and only
        when nothing else is running. The continuation is a normal turn in the original
        thread: the history already holds the question and the answer that asked for the
        sign-in, so the model is only told that the user has signed in."""
        waiting = state.pop("resume", None)
        if (
            not waiting
            or not waiting.get("group")
            or not waiting.get("chat")
            or waiting["chat"] != state.get("setup_group")
            or state.get("busy")
            or time.time() - waiting.get("at", 0) > RESUME_WINDOW
        ):
            return False
        reply_chat = waiting.get("chat") or state["chat"]
        rejection = self.access.admission(conn, owner, reply_chat)
        if rejection:
            if rejection != "unregistered_group":
                self.reply(conn, owner, reply_chat, NOT_ALLOWED, waiting["message_id"])
            return False
        if refusal := self.quota_denial(conn, owner):
            self.reply(conn, owner, reply_chat, refusal, waiting["message_id"])
            return False
        job = self.store.add_job(
            conn,
            owner,
            "prepare",
            dict(
                text=RESUME_REQUEST,
                attachments=[],
                forwards=[],
                reply_to=None,
                thread=waiting.get("thread"),
                in_thread=True,
                message_id=waiting["message_id"],
                version=state.get("credential_version"),
                resumed=True,
                chat=waiting.get("chat"),
                group=bool(waiting.get("group")),
            ),
        )
        state["busy"] = job
        emit("request.resumed", actor=actor_ref(owner), request_id=job)
        return True

    def handle_failure(self, job, error):
        with self.store.user(job["owner"]) as (conn, state):
            if not self.store.owns(conn, job):
                return
            if job["kind"] == "auth" and job["payload"].get(
                "login_version"
            ) != state.get("login_version"):
                self.store.finish(conn, job)
                return
            if job["kind"] == "prepare":
                if state.get("busy") == job["id"]:
                    state.pop("busy", None)
                state.pop(
                    "steers", None
                )  # the question is not saved, so neither are its corrections
                if self.agent:
                    turn = self.store.latest_turn(conn, job["id"])
                    if turn:
                        self.store.end_turn(conn, turn["session_id"], "failed")
                reauth = (
                    isinstance(error, AuthError)
                    and str(error) in ("LoginExpired", "LoginRequired")
                    and job["payload"].get("version") == state.get("credential_version")
                )
                if reauth:
                    self.clear_connection(state)
                    self.reply(
                        conn,
                        job["owner"],
                        job["payload"]["chat"],
                        "Your account connection expired. Send /setup in a direct chat to reconnect.",
                        job["payload"]["message_id"],
                    )
                else:
                    # Only the kind and the HTTP status: an error's own text can carry upstream
                    # content, and without this line a failed turn left no trace in the log.
                    emit(
                        "turn.failed",
                        logging.WARNING,
                        **job_fields(job),
                        failure=error.kind
                        if isinstance(error, AgentError)
                        else type(error).__name__,
                        http_status=error.status
                        if isinstance(error, AgentError)
                        else None,
                    )
                    if isinstance(error, AgentError):
                        message = AGENT_FAILURES.get(
                            error.kind, AGENT_FAILURES["other"]
                        )
                        if error.kind in ("incomplete", "rejected") and any(
                            isinstance(a, dict) and a.get("kind") == IMAGE
                            for a in job["payload"].get("attachments") or ()
                        ):
                            message = IMAGES_FAILED
                    elif transient_auth_error(error):
                        message = (
                            "Couldn't reach the account provider. Please try again."
                        )
                    else:
                        message = FAILURE_MESSAGES.get(
                            str(error),
                            "Couldn't process that message. Please try again.",
                        )
                    footer = self.token_footer(conn, job["owner"], job, in_group=True)
                    self.out(
                        conn,
                        job["owner"],
                        job["payload"].get("chat") or state["chat"],
                        message,
                        reply_to=job["payload"].get("message_id"),
                        thread=bool(job["payload"].get("message_id")),
                        single_reply=True,
                        preview_card=reply_card(message, message, [], footer, None),
                        usage_footer=footer,
                        answer_to=job["owner"].partition(":")[2]
                        if job["payload"].get("group")
                        else None,
                    )
                self.store.finish(conn, job)
                return
            poll = job["kind"] == "auth" and job["payload"].get("action") == "poll"
            pending = isinstance(error, AuthError) and str(error) in (
                "sign_in_pending",
                "poll_slowly",
            )
            if poll and (pending or transient_auth_error(error)):
                attempt = self.vault.open(job["owner"] + ":attempt", state["attempt"])
                remaining = attempt["deadline"] - time.time()
                if remaining <= 0:
                    emit("auth.retry_expired", **job_fields(job))
                    error = AuthError("LoginExpired")
                elif transient_auth_error(error):
                    failures = job["payload"].get("transient_failures", 0) + 1
                    payload = dict(job["payload"], transient_failures=failures)
                    conn.execute(
                        update(jobs)
                        .where(jobs.c.id == job["id"], jobs.c.claim == job["claim"])
                        .values(payload=payload)
                    )
                    delay = max(
                        attempt["interval"], min(30, 5 * 2 ** min(failures - 1, 3))
                    )
                    retry_after = getattr(error, "retry_after", None)
                    if isinstance(retry_after, int):
                        delay = max(delay, retry_after)
                    delay = min(delay, remaining)
                    if not state.get("auth_retry_notice"):
                        state["auth_retry_notice"] = True
                        self.show(conn, job["owner"], state)
                    self.store.finish(conn, job, retry=delay)
                    emit(
                        "auth.retry_scheduled",
                        **job_fields(job),
                        error_type=type(error).__name__,
                        retry_seconds=round(delay, 2),
                        remaining_seconds=round(remaining, 2),
                        retry_count=failures,
                    )
                    return
                else:
                    if job["payload"].get("transient_failures"):
                        conn.execute(
                            update(jobs)
                            .where(jobs.c.id == job["id"], jobs.c.claim == job["claim"])
                            .values(payload=dict(job["payload"], transient_failures=0))
                        )
                    if state.pop("auth_retry_notice", False):
                        self.show(conn, job["owner"], state)
            if (
                poll
                and isinstance(error, AuthError)
                and str(error)
                in (
                    "sign_in_pending",
                    "poll_slowly",
                )
            ):
                attempt = self.vault.open(job["owner"] + ":attempt", state["attempt"])
                interval = attempt["interval"] + (
                    5 if str(error) == "poll_slowly" else 0
                )
                attempt["interval"] = interval
                state["attempt"] = self.vault.seal(job["owner"] + ":attempt", attempt)
                self.store.finish(
                    conn,
                    job,
                    retry=min(attempt["interval"], attempt["deadline"] - time.time()),
                )
                return
            if job["kind"] == "send":
                # Bound delivery attempts; leave diagnostic metadata, never raw responses.
                attempts = job["payload"].get("attempts", 0) + 1
                if attempts < 5:
                    conn.execute(
                        update(jobs)
                        .where(jobs.c.id == job["id"])
                        .values(payload=dict(job["payload"], attempts=attempts))
                    )
                    self.store.finish(conn, job, retry=min(60, 2**attempts))
                    return
                run_id = job.get("payload", {}).get("card_run")
                run = state.get("card_runs", {}).get(run_id)
                if run and job["payload"].get("card_terminal"):
                    run["terminal_pending"] = False
                elif run_id == state.get("card_run") and job["payload"].get(
                    "card_version"
                ) == state.get("card_version"):
                    state["card_delivery_failed"] = True
            else:
                if state.get("busy") == job["id"]:
                    state.pop("busy", None)
                if state.get("auth_job") == job["id"]:
                    state.pop("auth_job", None)
                if job["kind"] == "auth":
                    for key in (
                        "attempt",
                        "user_code",
                        "verification_uri",
                        "auth_retry_notice",
                        "setup_sso",
                        "pending_account",
                        "pending_account_label",
                    ):
                        state.pop(key, None)
                    state["stage"] = ""
                    state["welcome"] = False
                    state["login_version"] = str(uuid.uuid4())
                self.show(
                    conn,
                    job["owner"],
                    state,
                    FAILURE_MESSAGES.get(
                        str(error), "That didn't work. Please try again."
                    ),
                )
            self.store.finish(conn, job)

    def heartbeat(self, job):
        with self.store.engine.begin() as conn:
            return bool(
                conn.execute(
                    update(jobs)
                    .where(
                        jobs.c.id == job["id"],
                        jobs.c.claim == job["claim"],
                        jobs.c.until > database_time(),
                    )
                    .values(until=database_time() + 60)
                ).rowcount
            )
