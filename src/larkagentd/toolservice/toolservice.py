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
from ..core.scopes import group_chat_scope, is_scopable
from ..core.store import Store
from ..gateway.service import Service
from ..knowledge.library import SourceLibrary
from ..knowledge.runtime import build_runtime
from ..skills.skills import SkillStore
from . import activity
from .gitlab import GitlabReader
from .google import GoogleReader
from .google import refusal as google_refusal
from .larkcli import LarkError, LarkReader
from .larkfiles import LarkFiles
from .registry import Decision, ToolRegistry
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
    # Allowed by the registry but not enabled for this group.
    "group_scope": "not_enabled_for_group",
}
MAX_BODY = 64 * 1024
MAX_RESULT = 64 * 1024
TOOL_PATH = re.compile(r"/v1/tools/([a-z][a-z0-9_]{0,63})")


def lark_refusal(code, reader_missing):
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
            ):
                context.conversation = payload
            else:
                context.lark_allowed = frozenset(
                    t for t in payload.get("lark_links") or () if isinstance(t, str)
                )
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
        if not decision.allowed:
            emit(
                "tool.denied",
                logging.WARNING,
                tool=name,
                actor=actor_ref(context.owner),
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
            }
        fields = dict(tool=name, actor=actor_ref(context.owner))
        try:
            result = tool.run(context, arguments)
            # Pictures travel beside the result, never inside it: they have their own bounded size, and
            # neither the size check, the activity row nor the logs ever see their bytes.
            pictures = result.pop("data_urls", None) if name == "lark_image" else None
            payload = json.dumps(result)
            if len(payload) > MAX_RESULT:
                raise ToolError("result_too_large")
        except (ToolError, LarkError) as exc:
            code = exc.code if isinstance(exc, ToolError) else str(exc)
            activity.finish(self.store, activity_id, started, "failed", error=code)
            self.note_login(context)
            self.done(fields, started, code)
            if name == "google_read":
                return 200, google_refusal(
                    code, self.google.email if self.google else None
                )
            if name in ("lark_read", "lark_image") and code == "not_configured":
                return 200, lark_refusal(code, reader_missing=self.lark is None)
            return 200, {"ok": False, "error": code}
        except Exception as exc:
            activity.finish(
                self.store, activity_id, started, "failed", error="tool_failed"
            )
            self.done(fields, started, "tool_failed", error_type=type(exc).__name__)
            return reject(500, "tool_failed")
        self.note_login(context)
        activity.finish(self.store, activity_id, started, "succeeded", result=result)
        self.done(fields, started, "ok")
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
        source_library=SourceLibrary(store),
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
