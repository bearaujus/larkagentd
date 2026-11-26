import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest
from sqlalchemy import delete, insert
from test_core import store as store_fixture

from larkagentd.core.store import database_time, gateway_identities, gateway_policies
from larkagentd.gateway.access import Access
from larkagentd.gateway.dashboard import PREFIX, make_handler

store = store_fixture
ORIGIN = "http://localhost:18080"


def identify(store, owner, email):
    with store.engine.begin() as conn:
        conn.execute(
            insert(gateway_identities).values(
                owner=owner, email=email, verified_at=database_time()
            )
        )


@pytest.fixture
def access(store):
    with store.engine.begin() as conn:
        conn.execute(delete(gateway_policies))
        conn.execute(
            insert(gateway_policies).values(
                scope="group:oc_test", mode="specific_users"
            )
        )
    identify(store, "t:admin", "admin@example.com")
    identify(store, "t:root", "root@example.com")
    identify(store, "t:u", "user@example.com")
    return Access(store, ("admin@example.com", "root@example.com"))


def admin():
    return {"email": "admin@example.com", "owner": "t:admin", "role": "admin"}


def change(access, method, path, body=None, actor=None):
    with access.store.engine.begin() as conn:
        access.lock(conn)
        return access.dispatch(
            conn, actor or admin(), method, path.split("/"), body or {}
        )


def issue(access, owner="t:admin"):
    with access.store.engine.begin() as conn:
        return access.issue(conn, owner)[0]


@pytest.fixture
def served(access):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(access, ORIGIN))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(
        base_url=f"http://127.0.0.1:{server.server_port}",
        headers={"Origin": ORIGIN, "Host": ORIGIN.split("://", 1)[1]},
    ) as client:
        yield access, client
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def login(access, client, owner="t:admin"):
    response = client.post(
        PREFIX + "/auth/exchange", json={"token": issue(access, owner)}
    )
    assert response.status_code == 200, response.text
    return response


@pytest.mark.parametrize("origin", (ORIGIN, "https://dashboard.example.net"))
def test_dashboard_login_and_group_changes_from_each_approved_origin(access, origin):
    from types import SimpleNamespace

    # Simulate HTTPS terminating before the Gateway, preserving the original Host.
    public = "https://dashboard.example.net"
    directory = SimpleNamespace(
        lookup=lambda kind, body: {"items": [{"name": "Test group"}]}
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(access, public, directory, additional_origins=(ORIGIN,)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origins = {"Origin": origin, "Host": origin.split("://", 1)[1]}
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}", headers=origins
        ) as client:
            response = login(access, client)
            cookie = response.headers["Set-Cookie"]
            assert ("; Secure" in cookie) == origin.startswith("https:")
            assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
            assert "Domain=" not in cookie
            client.headers["Cookie"] = cookie.split(";", 1)[0]

            body = {"group_ids": ["oc_test"]}
            lookup = client.post(PREFIX + "/directory/groups", json=body)
            assert lookup.status_code == 200 and lookup.json()["items"]
            payload = {"group_id": "oc_new"}
            other = ORIGIN if origin == public else public
            headers = {"Origin": other, "X-Forwarded-Host": other.split("://", 1)[1]}
            blocked = client.post(PREFIX + "/groups", headers=headers, json=payload)
            assert blocked.status_code == 403
            assert blocked.json() == {"error": "invalid_origin"}
            added = client.post(PREFIX + "/groups", json=payload)
            assert added.status_code == 201
            ended = client.post(PREFIX + "/auth/logout")
            assert ended.status_code in (200, 204)
            assert ("; Secure" in ended.headers["Set-Cookie"]) == origin.startswith(
                "https:"
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
