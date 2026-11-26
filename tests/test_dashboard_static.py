"""Dashboard assets stay separate from the authenticated data surface."""

import os
import subprocess
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from test_core import store as store_fixture
from test_dashboard import access as access_fixture
from test_dashboard import issue

from larkagentd.gateway.dashboard import make_handler
from larkagentd.gateway.governance import Governance

store = store_fixture
access = access_fixture
ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def server_for(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def assets(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>Dashboard</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/app-123.js").write_text("'use strict';")
    (tmp_path / "assets/app-123.css").write_text("body{color:black}")
    return tmp_path


def test_static_routes_content_headers_and_head(assets):
    with server_for(
        make_handler(None, "http://localhost", static_root=assets)
    ) as server:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            for path in ("/", "/dashboard"):
                response = client.get(path)
                assert response.status_code == 302
                assert response.headers["location"] == "/dashboard/"
            for path in (
                "/dashboard/",
                "/dashboard/people",
                "/dashboard/governance",
                "/dashboard/groups/oc_example",
            ):
                response = client.get(path)
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-cache"
                assert response.headers["referrer-policy"] == "no-referrer"
                assert (
                    "frame-ancestors 'none'"
                    in response.headers["content-security-policy"]
                )
                assert "Set-Cookie" not in response.headers
                head = client.head(path)
                assert head.status_code == 200 and not head.content
                assert head.headers["content-length"] == str(len(response.content))
            js = client.get("/dashboard/assets/app-123.js")
            assert js.status_code == 200
            assert js.headers["content-type"] in (
                "text/javascript",
                "application/javascript",
            )
            assert "immutable" in js.headers["cache-control"]
            assert client.post("/dashboard/people").status_code == 405


def test_unknown_assets_traversal_symlinks_and_api_are_not_spa(assets, tmp_path):
    outside = tmp_path.parent / "private-ui-test.txt"
    outside.write_text("synthetic-private")
    (assets / "assets/leak.js").symlink_to(outside)
    try:
        with server_for(
            make_handler(None, "http://localhost", static_root=assets)
        ) as server:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{server.server_port}"
            ) as client:
                for path in (
                    "/dashboard/assets/absent.js",
                    "/dashboard/assets/leak.js",
                    "/dashboard/%2e%2e/private-ui-test.txt",
                    "/dashboard/assets/%2e%2e/index.html",
                    "/dashboard/.env",
                    "/dashboard/assets/app.js.map",
                    "/dashboard/not-a-page",
                    "/v1/not-dashboard",
                ):
                    response = client.get(path)
                    assert response.status_code == 404, path
                    assert "synthetic-private" not in response.text
                    assert response.headers["content-type"] == "application/json"
    finally:
        outside.unlink()


def test_missing_build_does_not_claim_success(tmp_path):
    with server_for(
        make_handler(None, "http://localhost", static_root=tmp_path)
    ) as server:
        response = httpx.get(f"http://127.0.0.1:{server.server_port}/dashboard/")
        assert response.status_code == 503
        assert response.json() == {"error": "dashboard_build_missing"}


@pytest.mark.skipif(
    os.getenv("DASHBOARD_BROWSER_TEST") != "1",
    reason="Explicit browser smoke requires built UI and installed Chromium",
)
def test_real_gateway_cookie_origin_and_logout_in_browser(access):
    import json

    with server_for(make_handler(access, "http://localhost")) as server:
        origin = f"http://127.0.0.1:{server.server_port}"
        server.RequestHandlerClass = make_handler(
            access, origin, governance=Governance(access, default_model="smoke-model")
        )
        result = subprocess.run(
            ["node", "tests/gateway-smoke.mjs"],
            cwd=ROOT / "frontend",
            input=json.dumps({"origin": origin, "token": issue(access)}),
            text=True,
            capture_output=True,
            timeout=45,
        )
        # Never echo subprocess input or a raw browser request on failure.
        assert result.returncode == 0, "Synthetic browser Gateway smoke failed"
