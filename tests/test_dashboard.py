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
        base_url=f"http://127.0.0.1:{server.server_port}", headers={"Origin": ORIGIN}
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
