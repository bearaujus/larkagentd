"""Public application shell only; all dashboard data stays behind the JSON API."""

import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).with_name("static")
PAGES = {
    "",
    "overview",
    "login",
    "people",
    "groups",
    "usage",
    "governance",
    "knowledge",
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' https:; font-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
)


def serve(handler, root=ROOT):
    """Return True only for owned frontend routes. Never fall back over API paths."""
    raw = urlsplit(handler.path).path
    if raw not in ("/", "/dashboard") and not raw.startswith("/dashboard/"):
        return False
    if handler.command not in ("GET", "HEAD"):
        handler.respond(405, {"error": "method_not_allowed"})
        return True
    if raw in ("/", "/dashboard"):
        handler.send_response(302)
        handler.send_header("Location", "/dashboard/")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True
    relative = unquote(raw[len("/dashboard/") :])
    parts = relative.split("/")
    if (
        any(part in (".", "..") for part in parts)
        or "\\" in relative
        or "\x00" in relative
    ):
        handler.respond(404, {"error": "not_found"})
        return True
    page = relative.rstrip("/") in PAGES or (
        len(parts) == 2 and parts[0] == "groups" and bool(parts[1])
    )
    favicon = relative == "larkagentd.svg"
    if not page and not favicon and (len(parts) != 2 or parts[0] != "assets"):
        handler.respond(404, {"error": "not_found"})
        return True
    root = Path(root).resolve()
    file = (root / ("index.html" if page else relative)).resolve()
    if not file.is_relative_to(root) or not file.is_file():
        handler.respond(
            503 if page else 404,
            {"error": "dashboard_build_missing" if page else "not_found"},
        )
        return True
    content_type = mimetypes.guess_type(file.name)[0]
    if content_type not in (
        "text/html",
        "text/css",
        "text/javascript",
        "application/javascript",
        "image/svg+xml",
        "image/png",
        "font/woff2",
    ):
        handler.respond(404, {"error": "not_found"})
        return True
    content = file.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(content)))
    handler.send_header(
        "Cache-Control",
        "no-cache" if page or favicon else "public, max-age=31536000, immutable",
    )
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Content-Security-Policy", CSP)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(content)
    return True
