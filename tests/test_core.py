import os
import time
import uuid

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import create_engine, event, insert, text
from sqlalchemy.engine import make_url

from larkagentd.core import group_actions  # noqa: F401
from larkagentd.core.crypto import Vault
from larkagentd.core.store import Store, gateway_policies, metadata
from larkagentd.gateway.service import Service
from larkagentd.knowledge import library  # noqa: F401


def test_vault_owner_binding_and_tampering():
    vault = Vault(b"x" * 32)
    sealed = vault.seal("alice:v1", {"secret": "test-value"})
    assert "test-value" not in sealed
    assert vault.open("alice:v1", sealed)["secret"] == "test-value"
    with pytest.raises(InvalidTag):
        vault.open("bob:v1", sealed)
    with pytest.raises(InvalidTag):
        Vault(b"y" * 32).open("alice:v1", sealed)


_worker_databases = {}


def worker_database(url):
    """The database for this pytest-xdist worker; the given one when tests run serially.

    A schema keeps tests apart, but Postgres advisory locks belong to the whole database, and the
    code under test takes them by owner (nearly every test is the owner `t:u`). Workers sharing
    one database therefore block each other and fail at random. One database per worker, created
    once, needs a role that may CREATE DATABASE.
    """
    worker = os.getenv("PYTEST_XDIST_WORKER")
    if not worker:
        return url
    if worker not in _worker_databases:
        parsed = make_url(url)
        name = f"{parsed.database}_{worker}"
        admin = create_engine(url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            known = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).first()
            if not known:
                conn.execute(
                    text(f'CREATE DATABASE "{name}" TEMPLATE template0 ENCODING "UTF8"')
                )
        admin.dispose()
        _worker_databases[worker] = parsed.set(database=name).render_as_string(
            hide_password=False
        )
    return _worker_databases[worker]


@pytest.fixture
def store():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for PostgreSQL integration tests")
    url = worker_database(url)
    # Each test owns a generated schema; never clear shared application tables.
    schema = "larkagentd_test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    s = Store(url)

    @event.listens_for(s.engine, "connect")
    def set_schema(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        connection.commit()

    try:
        metadata.create_all(s.engine)
        # Flow tests use a registered synthetic group.
        with s.engine.begin() as conn:
            conn.execute(
                insert(gateway_policies).values(scope="group:grp", mode="all_users"),
            )
        yield s
    finally:
        s.engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


class FakeCloud:
    def start(self):
        return dict(
            verifier="v",
            device="d",
            code="CODE",
            uri="https://example.com",
            # Long, so a slow machine cannot make a poll fall due in the middle of a test:
            # tests that need the poll make it due themselves (ready_jobs).
            interval=120,
            deadline=time.time() + 600,
        )

    def poll(self, attempt):
        return dict(token="test-token", expires=time.time() + 3600)

    def accounts(self, token):
        return [dict(id="123", label="Test")]

    def roles(self, token, account):
        return [dict(id="role1", label="Reader")]

    def credentials(self, token, account, role):
        return dict(
            expiration="2099-01-01T00:00:00Z",
            expires=time.time() + 3600,
        )


class Transport:
    def __init__(self):
        self.sent = []

    def send(self, payload, key):
        self.sent.append(payload)
        return key

    def patch(self, message_id, content):
        return True

    def recall(self, message_id):
        return True


class RecordingAgent:
    deadline = 30
    model = "test-agent"

    def __init__(self, requests):
        self.requests = requests

    def build(self, request):
        self.requests.append(request)
        return {"input": request["message"]["content"]}

    def send(self, body, session_id, **kwargs):
        return "Test answer"

    def usage(self, session_id):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "model": self.model,
        }

    def forget(self, session_id):
        return True


def setup(store):
    output = []
    svc = Service(
        store,
        Vault(b"x" * 32),
        FakeCloud(),
        identity=lambda c: dict(account_id="123", arn="test-role"),
        agent=RecordingAgent(output),
    )
    return svc, output


def state(store):
    with store.user("t:u") as (_, s):
        return dict(s)
