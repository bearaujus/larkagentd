"""Dashboard shell and API. Only a session cookie supplies caller identity."""

import json
import shutil
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote, urlsplit

from sqlalchemy import delete

from ..core.diagnostics import actor_ref, emit
from ..core.store import dashboard_sessions
from .access import SESSION_TTL, AccessError, public_origin, token_hash
from .learned_page import learned_page
from .reporting import Reporting
from .static_assets import ROOT, serve

PREFIX = "/v1/dashboard"
COOKIE = "larkagentd_dashboard"
MAX_BODY = 64 * 1024


def make_handler(
    access, public_url, directory=None, *, static_root=ROOT, governance=None
):
    origin = public_origin(public_url)

    def verify_group_admin(actor):
        if not actor.get("group_id"):
            return
        transport = directory.transport if directory is not None else None
        try:
            valid = transport is not None and transport.group_admin(
                actor["group_id"], actor["owner"].split(":", 1)[-1]
            )
        except (RuntimeError, OSError, AttributeError):
            raise AccessError(503, "group_role_unavailable") from None
        if not valid:
            raise AccessError(401, "group_admin_required")

    class DashboardHandler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass

        def respond(self, status, body=None, cookie=None):
            encoded = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(encoded)

        def cookie(self, value, *, clear=False):
            jar = SimpleCookie()
            jar[COOKIE] = value
            item = jar[COOKIE]
            item["path"] = PREFIX
            item["httponly"] = True
            item["samesite"] = "Strict"
            item["max-age"] = 0 if clear else SESSION_TTL
            if origin.startswith("https:"):
                item["secure"] = True
            return item.OutputString()

        def session_token(self):
            try:
                jar = SimpleCookie(self.headers.get("Cookie", ""))
                return jar[COOKIE].value if COOKIE in jar else ""
            except CookieError:
                return ""

        def body(self):
            if self.headers.get("Transfer-Encoding"):
                raise AccessError(400, "invalid_body")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise AccessError(400, "invalid_body") from None
            if length < 0 or length > MAX_BODY:
                raise AccessError(413, "body_too_large")
            if not length:
                return {}
            if self.headers.get_content_type() != "application/json":
                raise AccessError(415, "json_required")
            try:
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError
                return body
            except (ValueError, UnicodeError):
                raise AccessError(400, "invalid_json") from None

        def handle_dashboard(self):
            cookie = None
            actor = None
            download_started = False
            try:
                if serve(self, static_root):
                    return
                url = urlsplit(self.path)
                path = url.path
                if not path.startswith(PREFIX + "/"):
                    raise AccessError(404, "not_found")
                parts = [unquote(part) for part in path[len(PREFIX) + 1 :].split("/")]
                if self.command not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                    raise AccessError(405, "method_not_allowed")
                if self.command != "GET" and self.headers.get("Origin") != origin:
                    raise AccessError(403, "invalid_origin")
                body = self.body()
                if parts and parts[0] == "directory":
                    if (
                        self.command != "POST"
                        or len(parts) != 2
                        or parts[1] not in ("users", "groups")
                    ):
                        raise AccessError(405, "method_not_allowed")
                    if urlsplit(self.path).query:
                        raise AccessError(400, "invalid_filter")
                    with access.store.engine.begin() as conn:
                        actor = access.authenticate(conn, self.session_token())
                        access.scoped_request(actor, parts, self.command)
                    if directory is None:
                        raise AccessError(503, "directory_unavailable")
                    self.respond(200, directory.lookup(parts[1], body))
                    return
                if parts and parts[0] == "usage":
                    if self.command != "GET" or len(parts) != 2:
                        raise AccessError(405, "method_not_allowed")
                    if body:
                        raise AccessError(400, "invalid_body")
                    # Authorize every report using the same admin/manager cookie. Release
                    # that short auth transaction before expensive reads/CSV generation.
                    with access.store.engine.begin() as conn:
                        actor = access.authenticate(conn, self.session_token())
                        access.scoped_request(actor, parts, self.command)
                    reporting = Reporting(access.store)
                    query = urlsplit(self.path).query
                    if parts[1] == "export":
                        with reporting.export(query) as (stream, size, count, dataset):
                            download_started = True
                            self.send_response(200)
                            self.send_header("Content-Type", "text/csv; charset=utf-8")
                            self.send_header(
                                "Content-Disposition",
                                f'attachment; filename="usage-{dataset}.csv"',
                            )
                            self.send_header("Content-Length", str(size))
                            self.send_header("X-Export-Row-Count", str(count))
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("X-Content-Type-Options", "nosniff")
                            self.end_headers()
                            shutil.copyfileobj(stream, self.wfile, length=64 * 1024)
                    else:
                        self.respond(200, reporting.get(parts[1], query))
                    return
                if parts == ["auth", "exchange"] and self.command == "POST":
                    access.fields(body, {"token"})
                    access.exchange_limit(self.client_address[0])
                    with access.store.engine.begin() as conn:
                        session, result = access.exchange(conn, body["token"])
                    if result.get("group_id"):
                        with access.store.engine.begin() as conn:
                            scope_actor = access.authenticate(conn, session)
                        try:
                            verify_group_admin(scope_actor)
                        except AccessError:
                            with access.store.engine.begin() as conn:
                                conn.execute(
                                    delete(dashboard_sessions).where(
                                        dashboard_sessions.c.token_hash
                                        == token_hash(session)
                                    )
                                )
                            raise
                    status, cookie = 200, self.cookie(session)
                else:
                    token = self.session_token()
                    with access.store.engine.begin() as conn:
                        prior = access.authenticate(conn, token)
                    if (
                        len(parts) == 4
                        and parts[0] == "groups"
                        and parts[2:] == ["knowledge", "learned"]
                        and self.command == "GET"
                    ):
                        access.scoped_request(prior, parts, self.command)
                        verify_group_admin(prior)
                        self.respond(
                            200,
                            learned_page(
                                access,
                                token,
                                parts,
                                url.query,
                                prior,
                                directory.transport if directory else None,
                                verify_group_admin,
                            ),
                        )
                        return
                    if parts != ["auth", "logout"]:
                        verify_group_admin(prior)
                    with access.store.engine.begin() as conn:
                        actor = access.authenticate(conn, token)
                        access.scoped_request(actor, parts, self.command)
                        if parts == ["auth", "session"] and self.command == "GET":
                            status, result = (
                                200,
                                {
                                    key: actor[key]
                                    for key in (
                                        "email",
                                        "role",
                                        "expires_at",
                                        "group_id",
                                    )
                                },
                            )
                        elif parts == ["auth", "logout"] and self.command == "POST":
                            access.fields(body, set())
                            conn.execute(
                                delete(dashboard_sessions).where(
                                    dashboard_sessions.c.token_hash == token_hash(token)
                                )
                            )
                            status, result, cookie = (
                                204,
                                None,
                                self.cookie("", clear=True),
                            )
                        elif parts[0] == "governance" and governance is not None:
                            if self.command == "GET" and body:
                                raise AccessError(400, "invalid_body")
                            status, result = governance.dispatch(
                                conn,
                                actor,
                                self.command,
                                parts[1:],
                                body,
                                query=url.query,
                            )
                        else:
                            # Only a create or an update carries a body, and the one PUT that replaces
                            # a group's tool list. Member PUTs and DELETEs never do.
                            takes_body = (
                                self.command in ("POST", "PATCH")
                                or (self.command == "DELETE" and parts[-1] == "sources")
                                or (
                                    self.command == "PUT"
                                    and len(parts) == 3
                                    and parts[0] == "groups"
                                    and parts[2] == "tools"
                                )
                            )
                            if body and not takes_body:
                                raise AccessError(400, "invalid_fields")
                            status, result = access.dispatch(
                                conn,
                                actor,
                                self.command,
                                parts,
                                body,
                                query=urlsplit(self.path).query,
                            )
                if actor and self.command != "GET":
                    emit(
                        "dashboard.changed",
                        actor=actor_ref(actor["owner"]),
                        resource=parts[0],
                        method=self.command,
                    )
                self.respond(status, result, cookie)
            except AccessError as exc:
                self.close_connection = True
                if download_started:
                    return
                self.respond(exc.status, {"error": exc.code})
            except Exception as exc:
                self.close_connection = True
                emit("dashboard.unavailable", error_type=type(exc).__name__)
                if download_started:
                    return
                self.respond(503, {"error": "temporarily_unavailable"})

        do_GET = handle_dashboard
        do_POST = handle_dashboard
        do_PUT = handle_dashboard
        do_PATCH = handle_dashboard
        do_DELETE = handle_dashboard
        do_OPTIONS = handle_dashboard
        do_HEAD = handle_dashboard

    return DashboardHandler
