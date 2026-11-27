"""Internal HTTP service that runs tools for Hermes.

Hermes's plugin sends the session id Hermes itself supplies; the service maps it to the
owner through the turn row the Gateway registered. Only the service (and the Gateway)
hold the credential encryption key.
"""

import hmac
import json
import logging
import os
import re
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from sqlalchemy import text

from ..config import ToolConfig
from ..core import group_actions
from ..core.artifacts import Artifacts
from ..core.crypto import Vault
from ..core.diagnostics import actor_ref, configure_logging, emit
from ..core.extensions import load_extension
from ..core.fields import lark_app_owner
from ..core.scopes import HUMAN_GROUP_TOOLS, group_chat_scope, is_scopable
from ..core.store import Store
from ..gateway.service import Service
from ..knowledge.attachment_sync import AttachmentSync
from ..knowledge.library import SourceLibrary
from ..knowledge.runtime import build_runtime
from ..knowledge.sanitize import sanitize
from ..knowledge.wiki import WikiReader
from ..lark.message_links import MESSAGE_ID, verified_link
from ..lark.profile import label as profile_label
from ..lark.ui_text import APP_FILE_REFUSALS
from ..skills.skills import SkillStore
from . import activity
from .gitlab import GitlabReader
from .google import GoogleReader
from .google import refusal as google_refusal
from .larkcli import LarkError, LarkReader
from .larkfiles import LarkFiles
from .registry import READ, Decision, ToolRegistry, capability
from .tools import TOOLS, ToolContext, ToolError, specs, validate
from .web_search import WebSearch

# What the model is told when a tool is not allowed for the caller: a fixed code, never the reason
# in detail. Not being granted and being switched off read the same to the person asking.
DENIED_CODES = {
    "unknown": "tool_unavailable",
    "not_implemented": "tool_unavailable",
    "unavailable": "tool_unavailable",
    "disabled": "tool_not_allowed",
    "needs_grant": "tool_not_allowed",
    "denied": "tool_not_allowed",
    "app_read_only": "app_read_only",
    # Allowed by the registry but not enabled for this group.
    "group_scope": "not_enabled_for_group",
}
MAX_BODY = 64 * 1024
MAX_RESULT = 64 * 1024
TOOL_PATH = re.compile(r"/v1/tools/([a-z][a-z0-9_]{0,63})")

LARK_READ_REFUSALS = {
    "sheet_tab_not_found": (
        "I opened the workbook, but couldn't find the requested tab. "
        "Check the tab name or ask me to list its tabs."
    ),
    "lark_permission_unverified": (
        "I couldn't verify that everyone in this group can view the file. "
        "Ask the document owner or bot administrator to check how the bot can verify group access."
    ),
    "lark_bot_view_denied": (
        "Lark says this bot can't view the file. Ask the owner to give the bot View "
        "and check that this group can view it."
    ),
    "lark_share_required": (
        "I couldn't find a Lark View grant for this group or a verified organization link "
        "that covers it. Ask the owner to check this group's View access."
    ),
    "lark_history_source_unavailable": (
        "The earlier message could no longer be verified. Please share the file link here."
    ),
    "link_not_from_user": (
        "I don't have an eligible link for this file. Please include it in your message."
    ),
    "link_not_shared": (
        "I don't have an eligible link for this file. Please include it in your message."
    ),
    "forbidden": (
        "Lark refused the bot's request. The bot administrator can check the app's "
        "read scopes and the owner can check its file access."
    ),
    "not_found": (
        "Lark couldn't find the file for this bot. Check the link and the bot's access."
    ),
}

LARK_EDIT_REFUSALS = {
    "lark_bot_edit_denied": (
        "Lark reports that the bot lacks Edit access. Ask the file owner to give the bot Edit."
    ),
    "lark_write_permission": (
        "Lark's collaborator list shows no Edit or Manage grant for this group. "
        "Ask the file owner to check the group's grant."
    ),
    "lark_edit_unverified": (
        "The bot has Edit access. Lark won't let it see the collaborator list, so it "
        "can't verify this group's Edit grant. Ask the file owner to let the bot view "
        "this file's collaborators."
    ),
    "link_not_shared": (
        "I couldn't verify this link from your message or an earlier message you wrote "
        "in this thread. Please include the link in your edit request."
    ),
    "forbidden": (
        "Lark refused the bot's edit request or permission check. Ask the bot administrator "
        "to check the app's write scopes and the file owner's settings."
    ),
    "write_outcome_unknown": (
        "I couldn't verify the result of the edit. Check the document before trying again."
    ),
    "write_no_change": (
        "The document content is unchanged after the edit. Check the text to replace or the content to append."
    ),
}

LARK_CREATE_REFUSALS = {
    "bad_slide": (
        "Provide one to six slides. Each needs a title of up to 72 characters and either "
        "one to five bullets of up to 100 characters each or validated grouped chart data."
    ),
    "lark_slides_create_scope": (
        "Lark denied the bot's Slides create API call. Ask the app administrator to "
        "check and release the bot scope slides:presentation:create."
    ),
    "artifact_incomplete": (
        "Lark returned a file ID, but setup or sharing failed. An incomplete file may exist. "
        "Ask the bot administrator to check the creation stage before trying again."
    ),
    "artifact_unverified": (
        "I couldn't verify that Lark created the file. Check for an existing file before trying again."
    ),
}


def lark_refusal(code, reader_missing):
    if code != "not_configured":
        return {
            "ok": False,
            "error": code,
            "retryable": False,
            "note": LARK_READ_REFUSALS[code],
        }
    return {
        "ok": False,
        "error": code,
        "retryable": False,
        "note": (
            (
                "Lark document reading is not configured for this bot. No request reached Lark; "
                if reader_missing
                else "The Lark reader setup or authentication is unavailable; the document was not read. "
            )
            + "document permissions and Lark service availability have not been checked. "
            "Ask a bot administrator to enable the reader. Do not retry or claim that Lark is down."
        ),
    }


def reject(status, code):
    return status, {"ok": False, "error": code}


class ToolService:
    def __init__(
        self,
        store,
        service,
        key,
        *,
        registry,
        tools=TOOLS,
        artifacts=None,
        skills=None,
        gitlab=None,
        lark=None,
        google=None,
        group_brain=None,
        vault=None,
        web=None,
        source_library=None,
        gateway_app=None,
    ):
        self.store, self.service, self.tools = store, service, tools
        self.artifacts = artifacts
        self.skills = skills
        if registry is None:
            raise ValueError("A tool registry is required")
        self.registry = registry
        self.key = key.encode()
        self.gitlab, self.lark = gitlab, lark
        self.google = google
        self.vault = vault
        self.files = (
            LarkFiles(lark, store, vault, gateway_app=gateway_app)
            if lark is not None and vault is not None
            else None
        )
        self.web = web
        self.source_library = source_library
        # Group Brain's runtime: None while the global switch is off.
        self.group_brain = group_brain

    def authorized(self, authorization):
        return hmac.compare_digest(
            (authorization or "").encode(), b"Bearer " + self.key
        )

    def _note_found(self, job_id, found, reads):
        with self.store.engine.begin() as conn:
            return self.store.note_lark_found(conn, job_id, found, reads)

    def _note_history(self, job_id, found, tool, generation=None):
        with self.store.engine.begin() as conn:
            return self.store.note_history_file_links(
                conn, job_id, found, tool, generation
            )

    def _note_job(self, job_id, **fields):
        with self.store.engine.begin() as conn:
            return self.store.note_job(conn, job_id, **fields)

    def handle(self, authorization, name, body):
        started = time.monotonic()
        if not self.authorized(authorization):
            emit("tool.rejected", logging.WARNING, reason="unauthorized")
            return reject(401, "unauthorized")
        tool = self.tools.get(name)
        if tool is None:
            emit("tool.rejected", logging.WARNING, reason="unknown_tool")
            return reject(404, "unknown_tool")
        try:
            request = json.loads(body)
            if not isinstance(request, dict) or set(request) - {
                "session_id",
                "arguments",
            }:
                raise ValueError
            session_id = request["session_id"]
            if not isinstance(session_id, str) or not 0 < len(session_id) <= 200:
                raise ValueError
            arguments = request.get("arguments", {})
            validate(tool.parameters, arguments)
        except (ValueError, KeyError, TypeError, AttributeError, ToolError):
            emit("tool.rejected", logging.WARNING, tool=name, reason="bad_request")
            return reject(400, "bad_request")
        with self.store.engine.begin() as conn:
            turn = self.store.live_turn(conn, session_id)
        if turn is None:
            emit("tool.rejected", logging.WARNING, tool=name, reason="invalid_session")
            return reject(403, "invalid_session")
        if turn.get("chat_type") != "group" or not turn.get("chat_id"):
            return reject(403, "group_turn_required")
        history_files = []
        message_sources = {}

        def observe_source(url, message_id, text, author=None, time_label=None):
            url = verified_link(url, chat_id=turn["chat_id"])
            if (
                not url
                or not isinstance(message_id, str)
                or not MESSAGE_ID.fullmatch(message_id)
            ):
                return
            excerpt = sanitize(text, max_chars=1600)
            if excerpt.text:
                message_sources[url] = {
                    "message_id": message_id,
                    "excerpt": excerpt.text,
                    "truncated": "truncated" in excerpt.reasons,
                    "author": profile_label(author),
                    "time": time_label,
                }

        context = ToolContext(
            turn["owner"],
            turn["credential_version"],
            self.service,
            session_id,
            self.artifacts,
            skills=self.skills,
            gitlab=self.gitlab,
            lark=self.lark,
            google=self.google,
            chat_id=turn["chat_id"],
            group_brain=self.group_brain,
            allowed=lambda tool: self.decide(turn, tool).allowed,
            chat_type=turn["chat_type"],
            turn=turn,
            files=self.files,
            web=self.web,
            source_library=self.source_library,
            store=self.store,
            vault=self.vault,
            request_action=lambda kind, data, label: group_actions.action_request(
                self.store, self.vault, turn, kind, data, label
            ),
            job_note=lambda **fields: self._note_job(turn["job_id"], **fields),
            lark_history_record=lambda *proof: history_files.append(proof),
            observe_source=observe_source,
        )
        if name in (
            "lark_read",
            "lark_image",
            "lark_thread",
            "google_read",
            "lark_file_read",
            "lark_file_edit",
            "run_confirmed_action",
            "group_knowledge_search",
            "group_knowledge_sources",
            "group_memory_propose",
        ):
            with self.store.engine.begin() as conn:
                payload = self.store.job_payload(conn, turn["job_id"]) or {}
            if name == "google_read":
                context.google_allowed = frozenset(
                    t for t in payload.get("google_links") or () if isinstance(t, str)
                )
            elif name in (
                "lark_thread",
                "run_confirmed_action",
                "group_knowledge_search",
                "group_knowledge_sources",
                "group_memory_propose",
            ):
                context.conversation = payload
            else:
                context.lark_allowed = frozenset(
                    t for t in payload.get("lark_links") or () if isinstance(t, str)
                )
                context.lark_thread_allowed = frozenset(
                    t
                    for t in payload.get("lark_thread_links") or ()
                    if isinstance(t, str)
                )
                if name in ("lark_read", "lark_image", "lark_file_read"):
                    context.lark_allowed = context.lark_allowed.union(
                        context.lark_thread_allowed
                    )
                if name in ("lark_read", "lark_image", "lark_file_read"):
                    context.lark_history_refs = payload.get("lark_history_refs")
                found = payload.get("lark_found")
                context.lark_found = {
                    t: d
                    for t, d in (found if isinstance(found, dict) else {}).items()
                    if isinstance(t, str) and isinstance(d, int)
                }
                context.lark_reads = int(payload.get("lark_reads") or 0)
                context.lark_record = lambda found, reads: self._note_found(
                    turn["job_id"], found, reads
                )
        decision = self.decide(turn, name)
        activity_id = (
            activity.start(self.store, turn, name, tool.project, tool.activity_kind)
            if tool.activity_kind
            else None
        )
        fields = dict(tool=name, actor=actor_ref(context.owner), job_id=turn["job_id"])
        if not decision.allowed:
            emit(
                "tool.denied",
                logging.WARNING,
                **fields,
                reason=decision.reason,
            )
            code = DENIED_CODES[decision.reason]
            activity.finish(self.store, activity_id, started, "denied", error=code)
            if name == "google_read":
                return 200, google_refusal(
                    code, self.google.email if self.google else None
                )
            return 200, {
                "ok": False,
                "error": code,
                **(
                    {
                        "note": APP_FILE_REFUSALS.get(
                            name,
                            "This request came from a Lark app with read-only access.",
                        )
                    }
                    if code == "app_read_only"
                    else {}
                ),
            }
        try:
            result = tool.run(context, arguments)
            # Pictures travel beside the result, never inside it: they have their own bounded size, and
            # neither the size check, the activity row nor the logs ever see their bytes.
            pictures = result.pop("data_urls", None) if name == "lark_image" else None
            payload = json.dumps(result)
            if len(payload) > MAX_RESULT:
                raise ToolError("result_too_large")
            for proof in history_files:
                self._note_history(turn["job_id"], *proof)
            if message_sources and (
                name == "lark_thread" or result.get("status") in {"ok", "partial"}
            ):
                self._note_job(
                    turn["job_id"],
                    message_citations={
                        "session_id": session_id,
                        "links": message_sources,
                    },
                )
        except (ToolError, LarkError) as exc:
            code = exc.code
            activity.finish(self.store, activity_id, started, "failed", error=code)
            self.note_login(context)
            diagnostics = exc.log_fields() if isinstance(exc, LarkError) else {}
            self.done(fields, started, code, **diagnostics)
            if name == "google_read":
                return 200, google_refusal(
                    code, self.google.email if self.google else None
                )
            if name == "run_query" and code == "maxcompute_estimate_denied":
                return 200, {
                    "ok": False,
                    "error": code,
                    "retryable": False,
                    "query_executed": False,
                    "note": "The scan estimate was denied. The query was not submitted.",
                }
            if name in ("lark_read", "lark_image", "lark_file_read") and (
                code == "not_configured" or code in LARK_READ_REFUSALS
            ):
                result = lark_refusal(code, reader_missing=self.lark is None)
                if (
                    code == "lark_permission_unverified"
                    and diagnostics.get("bot_access") == "granted"
                ):
                    result["note"] = (
                        "The bot has View access. Lark blocked its collaborator check, so I "
                        "couldn't verify this group's access. Ask the file owner to let the bot "
                        "see the collaborators for this file."
                    )
                return 200, result
            if (
                name in ("lark_file_edit", "run_confirmed_action")
                and code in LARK_EDIT_REFUSALS
            ):
                return 200, {
                    "ok": False,
                    "error": code,
                    "retryable": False,
                    "note": LARK_EDIT_REFUSALS[code],
                }
            if name == "lark_file_create" and code in LARK_CREATE_REFUSALS:
                return 200, {
                    "ok": False,
                    "error": code,
                    "retryable": code == "bad_slide",
                    "note": LARK_CREATE_REFUSALS[code],
                }
            return 200, {"ok": False, "error": code}
        except Exception as exc:
            activity.finish(
                self.store, activity_id, started, "failed", error="tool_failed"
            )
            self.done(fields, started, "tool_failed", error_type=type(exc).__name__)
            return reject(500, "tool_failed")
        self.note_login(context)
        activity.finish(self.store, activity_id, started, "succeeded", result=result)
        stage = None
        access_basis = None
        if name in ("lark_file_edit", "run_confirmed_action"):
            if result.get("confirmation_requested") is True:
                stage = "confirmation_requested"
            elif result.get("updated") is True:
                stage = "write_verified"
            if stage and result.get("access_basis") == "bot_edit":
                access_basis = "bot_edit"
        elif name == "lark_file_create" and result.get("verified") is True:
            stage = "create_verified"
        diagnostics = {}
        if name in (
            "group_search_discussions",
            "group_knowledge_search",
            "group_knowledge_sources",
            "group_get_discussion",
            "group_memory_propose",
            "group_memory_correct",
        ):
            diagnostics = {
                "result_status": result.get("status"),
                "reason": result.get("reason"),
            }
        self.done(
            fields, started, "ok", stage=stage, access_basis=access_basis, **diagnostics
        )
        if pictures:
            return 200, {"ok": True, "result": result, "data_urls": pictures}
        return 200, {"ok": True, "result": result}

    def note_login(self, context):
        """Tell the Gateway this turn wanted a sign-in, so it offers the setup card once."""
        if context.login_needed and context.session_id:
            with self.store.engine.begin() as conn:
                self.store.mark_needs_login(conn, context.session_id, context.owner)
            emit("tool.login_needed", logging.INFO)

    def decide(self, turn, name):
        """The registry's answer, then the group's: a scope only narrows."""
        decision = self.registry.decide(turn["owner"], name)
        if not decision.allowed:
            return decision
        if lark_app_owner(turn["owner"]):
            tool = self.tools.get(name)
            if tool is None or capability(tool)[1] != READ or name in HUMAN_GROUP_TOOLS:
                return Decision(False, "app_read_only")
        return self.group_decision(turn, name)

    def group_decision(self, turn, name):
        """In a group, a tool must be enabled for it. The terminal and the skill tools that are always
        on are not scoped. A group turn without a usable chat id, or a database error, is a refusal."""
        if turn.get("chat_type") != "group" or not is_scopable(name):
            return Decision(True, "ok")
        scope = group_chat_scope(turn.get("chat_id"))
        if scope is None:
            return Decision(False, "group_scope")
        try:
            with self.store.engine.begin() as conn:
                enabled = self.store.scope_tools(conn, scope)
        except Exception as exc:
            emit("tool.scope_unavailable", logging.ERROR, error_type=type(exc).__name__)
            return Decision(False, "group_scope")
        return (
            Decision(True, "ok") if name in enabled else Decision(False, "group_scope")
        )

    @staticmethod
    def done(fields, started, outcome, **extra):
        level = logging.INFO if outcome == "ok" else logging.WARNING
        emit(
            "tool.called",
            level,
            **fields,
            outcome=outcome,
            duration_ms=round((time.monotonic() - started) * 1000),
            **extra,
        )

    def ready(self):
        try:
            with self.store.engine.begin() as conn:
                conn.execute(text("SET LOCAL statement_timeout = '5s'"))
                for table in (
                    "users",
                    "turns",
                    "artifacts",
                    "tool_registry",
                    "tool_grants",
                    "query_activity",
                    "group_actions",
                    "tool_effects",
                    "knowledge_sources",
                    "knowledge_lark_attachments",
                    "knowledge_lark_passages",
                ):
                    conn.execute(text(f"SELECT 1 FROM {table} LIMIT 0"))
        except Exception:
            return False
        return True


def make_handler(tools):
    class Handler(BaseHTTPRequestHandler):
        server_version = "larkagentd-tools"
        sys_version = ""
        timeout = 15

        def reply(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def read_body(self):
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.reply(411, {"ok": False, "error": "length_required"})
                return None
            if not 0 <= length <= MAX_BODY:
                self.reply(413, {"ok": False, "error": "too_large"})
                return None
            return self.rfile.read(length)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                self.reply(200, {"status": "ok"})
            elif path == "/readyz":
                ok = tools.ready()
                self.reply(
                    200 if ok else 503, {"status": "ok" if ok else "unavailable"}
                )
            elif path == "/v1/specs":
                if not tools.authorized(self.headers.get("Authorization")):
                    self.reply(*reject(401, "unauthorized"))
                    return
                self.reply(200, specs(tools.tools))
            else:
                self.reply(404, {"ok": False, "error": "not_found"})

        def do_POST(self):
            match = TOOL_PATH.fullmatch(urlsplit(self.path).path)
            if not match:
                self.reply(404, {"ok": False, "error": "not_found"})
                return
            body = self.read_body()
            if body is None:
                return
            self.reply(*tools.handle(self.headers.get("Authorization"), match[1], body))

        def log_message(self, *_):
            pass

    return Handler


def build_tools(config, store):
    """The tool service with its collaborators. The preview store shares the vault with
    the Gateway's, so what is stored here can be opened there."""
    vault = Vault(config.encryption_key)
    lark_reader = (
        LarkReader(
            config.lark_command,
            config.lark_app_id,
            config.lark_app_secret,
            config.lark_home,
            base_domain=config.lark_brand,
            follow_links=config.lark_follow_links,
        )
        if config.lark_app_id
        else None
    )
    extension = load_extension()
    auth = extension.authentication() if extension else None
    tools = dict(TOOLS)
    if extension:
        extra = extension.tools(store)
        if tools.keys() & extra.keys() or any(
            name != tool.name for name, tool in extra.items()
        ):
            raise ValueError("Extension tool names must be unique")
        tools.update(extra)
    return ToolService(
        store,
        Service(store, vault, auth, identity=auth.identify if auth else None),
        config.service_key,
        tools=tools,
        vault=vault,
        source_library=SourceLibrary(
            store, wiki=WikiReader(lark_reader) if lark_reader else None
        ),
        gateway_app=os.getenv("LARK_APP_ID"),
        web=WebSearch(os.environ["BRAVE_SEARCH_API_KEY"])
        if os.getenv("BRAVE_SEARCH_API_KEY")
        else None,
        artifacts=Artifacts(store, vault),
        skills=SkillStore(config.skill_roots),
        registry=ToolRegistry(store),
        gitlab=(
            GitlabReader(config.gitlab_url, config.gitlab_token)
            if config.gitlab_url
            else None
        ),
        lark=lark_reader,
        group_brain=build_runtime(store, config, lark_reader, config.database_url),
        google=(
            GoogleReader(config.google_service_account_json)
            if config.google_service_account_json
            else None
        ),
    )


def main():
    try:
        configure_logging(
            os.getenv("LOG_LEVEL", "INFO").upper(),
            os.getenv("LOG_FORMAT", "text").lower(),
        )
    except ValueError as exc:
        configure_logging()
        emit("startup.failed", logging.ERROR, stage="logging", reason=str(exc))
        raise SystemExit(1) from None
    try:
        config = ToolConfig.load()
    except ValueError as exc:
        emit("startup.failed", logging.ERROR, stage="configuration", reason=str(exc))
        raise SystemExit(1) from None
    store = Store(config.database_url)
    tools = build_tools(config, store)
    emit("lark.reader_configured", enabled=tools.lark is not None)
    if tools.ready() and tools.registry is not None:
        tools.registry.sync(tools.tools)
    if not tools.ready():
        emit(
            "startup.failed",
            logging.ERROR,
            stage="database",
            hint="Check_DATABASE_URL_and_run_alembic_upgrade_head",
        )
        raise SystemExit(1)
    server = ThreadingHTTPServer((config.host, config.port), make_handler(tools))
    # Documents are read here (the Lark CLI is in this process), so their jobs run here too.
    knowledge_stop = threading.Event()
    if tools.source_library.wiki:
        sync = AttachmentSync(store, tools.source_library.wiki, config.group_brain)
        threading.Thread(
            target=sync.run,
            args=(knowledge_stop,),
            daemon=True,
            name="lark-source-sync",
        ).start()
    if tools.group_brain is not None and tools.group_brain.doc_source is not None:
        from ..knowledge.handlers import DOCUMENT_KINDS
        from ..knowledge.worker import start_worker_threads

        start_worker_threads(
            tools.group_brain,
            knowledge_stop,
            "toolservice-knowledge",
            config.group_brain.worker_concurrency,
            kinds=DOCUMENT_KINDS,
        )

    def stop(signum, _frame):
        emit("shutdown.requested", signal=signal.Signals(signum).name)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    emit(
        "toolservice.listening",
        host=config.host,
        port=config.port,
        tools=",".join(TOOLS),
    )
    try:
        server.serve_forever()
    finally:
        knowledge_stop.set()
        server.server_close()
        store.engine.dispose()
        emit("toolservice.stopped")


if __name__ == "__main__":
    main()
