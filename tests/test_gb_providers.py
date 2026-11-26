import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from larkagentd.knowledge.models import GroupBrainConfig
from larkagentd.knowledge.providers import (
    FakeIndex,
    Mem0Index,
    ProviderError,
    namespace_for,
)


class EmbeddingHandler(BaseHTTPRequestHandler):
    dimensions = 8
    requests = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(self.path)
        values = body["input"] if isinstance(body["input"], list) else [body["input"]]
        rows = []
        for index, text in enumerate(values):
            digest = hashlib.sha256(text.encode()).digest()
            vector = [((byte / 127.5) - 1) for byte in digest[: self.dimensions]]
            rows.append({"object": "embedding", "index": index, "embedding": vector})
        payload = json.dumps(
            {
                "object": "list",
                "data": rows,
                "model": "local-test",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def real_index(tmp_path, monkeypatch):
    from larkagentd.knowledge.providers.mem0 import harden_mem0

    harden_mem0()  # before the first import, so nothing is written to a home folder
    pytest.importorskip("mem0")
    home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", home)
    EmbeddingHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), EmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    path = str(tmp_path / "qdrant")
    config = GroupBrainConfig(
        embedding_base_url=base,
        embedding_api_key="REDACTED_TEST_VALUE",
        embedding_model="local-test",
        embedding_dimensions=8,
        provider_timeout_seconds=15,
    )
    index = Mem0Index(
        config,
        vector_store={
            "provider": "qdrant",
            "config": {
                "collection_name": "synthetic-contract",
                "path": path,
                "embedding_model_dims": 8,
                "on_disk": False,
            },
        },
    )
    yield index, server, home
    server.shutdown()


@pytest.fixture(params=["fake", "mem0"])
def index(request):
    if request.param == "fake":
        return FakeIndex()
    return request.getfixturevalue("real_index")[0]


class TestSemanticIndexContract:
    def test_scope_filters_duplicate_delete_and_pagination(self, index):
        a, b = "gb:scope-a:discussion", "gb:scope-b:discussion"
        text = "Synthetic exact text: partition scheduling policy"
        a_id = index.add(
            namespace=a,
            text=text,
            metadata={"source_kind": "discussion", "revision": 1},
        )[0]
        b_id = index.add(
            namespace=b,
            text=text,
            metadata={"source_kind": "discussion", "revision": 1},
        )[0]
        assert (
            index.records(a)[0].text == text
            if isinstance(index, FakeIndex)
            else index.search(namespace=a, query=text, top_k=5)[0].text == text
        )
        assert {
            hit.provider_id for hit in index.search(namespace=a, query=text, top_k=10)
        } == {a_id}
        assert {
            hit.provider_id
            for hit in index.search(
                namespace=a, query=text, top_k=10, metadata_filters={"revision": 2}
            )
        } == set()
        assert {
            hit.provider_id
            for hit in index.search(
                namespace=a, query=text, top_k=10, metadata_filters={"revision": 1}
            )
        } == {a_id}
        assert (
            index.search(
                namespace=a, query=text, top_k=10, metadata_filters={"user_id": b}
            )
            == []
        )
        assert {
            hit.provider_id
            for hit in index.search(
                namespace=b, query=text, top_k=10, metadata_filters={"user_id": b}
            )
        } == {b_id}
        index.add(
            namespace=a,
            text=text,
            metadata={"source_kind": "discussion", "revision": 1},
        )
        assert len(index.list_scoped(namespace=a)[0]) == 2
        records, cursor = index.list_scoped(namespace=a, limit=1)
        assert len(records) == 1 and cursor
        rest, cursor2 = index.list_scoped(namespace=a, cursor=cursor, limit=1)
        assert len(rest) == 1 and cursor2 is None
        with pytest.raises(ProviderError) as error:
            index.delete(namespace=a, provider_id=b_id)
        assert error.value.code in {"namespace_mismatch", "not_found"}
        assert len(index.list_scoped(namespace=b)[0]) == 1
        index.delete(namespace=a, provider_id=a_id)
        assert all(hit.provider_id != a_id for hit in index.list_scoped(namespace=a)[0])

    def test_invalid_inputs_and_health(self, index):
        for text in ("", "x" * 8001):
            with pytest.raises(ProviderError):
                index.add(namespace="n", text=text, metadata={})
        with pytest.raises(ProviderError):
            index.add(namespace="n", text="ok", metadata={"nested": {"x": 1}})
        with pytest.raises(ProviderError):
            index.search(namespace="n", query="ok", top_k=101)
        health = index.health()
        assert set(health) == {"status", "reason", "model", "dimensions"}
        assert health["status"] in {"ready", "unavailable", "not_configured"}


def test_namespace_is_stable_unambiguous_and_kind_checked():
    assert namespace_for("d", "ab", "discussion") == namespace_for(
        "d", "ab", "discussion"
    )
    assert namespace_for("d", "ab", "discussion") != namespace_for(
        "d", "a", "discussion"
    )
    assert namespace_for("d", "g", "discussion") != namespace_for("d", "g", "memory")
    assert namespace_for("d", "ab", "discussion") != namespace_for(
        "e", "ab", "discussion"
    )
    assert namespace_for("ab", "c", "discussion") != namespace_for(
        "a", "bc", "discussion"
    )
    with pytest.raises(ValueError):
        namespace_for("d", "g", "other")


def test_fake_failure_injection_timeout_and_unavailable():
    index = FakeIndex()
    index.fail_next("search", "bad_response")
    with pytest.raises(ProviderError) as error:
        index.search(namespace="n", query="text", top_k=1)
    assert error.value.code == "bad_response"
    index.timeout_after_write = True
    with pytest.raises(ProviderError) as error:
        index.add(namespace="n", text="stored", metadata={})
    assert error.value.code == "timeout" and len(index.records("n")) == 1
    index.duplicate_adds = True
    assert len(index.add(namespace="n", text="stored", metadata={})) == 2
    index.unavailable()
    assert index.health()["status"] == "unavailable"


def test_mem0_config_and_history_contract(real_index, monkeypatch):
    index, server, home = real_index
    monkeypatch.setenv("MEM0_TELEMETRY", "true")
    index.add(
        namespace="gb:history:discussion",
        text="Exact synthetic history body",
        metadata={},
    )
    assert os.environ["MEM0_TELEMETRY"] == "false"
    assert not list(__import__("pathlib").Path(home).rglob("*"))
    assert EmbeddingHandler.requests and all(
        request.endswith("/v1/embeddings") for request in EmbeddingHandler.requests
    )
    assert server.server_port > 0


def test_mem0_dimension_mismatch_timeout_and_secret_redaction(tmp_path):
    pytest.importorskip("mem0")
    EmbeddingHandler.dimensions = 8
    server = HTTPServer(("127.0.0.1", 0), EmbeddingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = GroupBrainConfig(
        embedding_base_url=f"http://127.0.0.1:{server.server_port}/v1",
        embedding_api_key="REDACTED_TEST_VALUE",
        embedding_dimensions=9,
        provider_timeout_seconds=2,
    )
    index = Mem0Index(
        config,
        vector_store={
            "provider": "qdrant",
            "config": {
                "collection_name": "dimension-test",
                "path": str(tmp_path / "dimension-qdrant"),
                "embedding_model_dims": 9,
            },
        },
    )
    assert "REDACTED_TEST_VALUE" not in repr(
        index
    ) and "REDACTED_TEST_VALUE" not in str(index)
    try:
        with pytest.raises(ProviderError) as error:
            index.add(namespace="n", text="dimension mismatch", metadata={})
        assert error.value.code == "invalid_dimension"
    finally:
        server.shutdown()

    unreachable = Mem0Index(
        GroupBrainConfig(
            embedding_base_url="http://127.0.0.1:1/v1",
            embedding_api_key="REDACTED_TEST_VALUE",
            embedding_dimensions=8,
            provider_timeout_seconds=2,
        ),
        vector_store={
            "provider": "qdrant",
            "config": {
                "collection_name": "unreachable-test",
                "path": ":memory:",
                "embedding_model_dims": 8,
            },
        },
    )
    with pytest.raises(ProviderError) as error:
        unreachable.add(namespace="n", text="local endpoint unavailable", metadata={})
    assert error.value.code in {"dependency_unavailable", "timeout"}


def test_mem0_missing_dependency_path_and_url_parsing(monkeypatch):
    assert Mem0Index.pgvector_config(
        "postgresql+psycopg://u:p@127.0.0.1:5432/db?sslmode=require"
    ) == {
        "dbname": "db",
        "user": "u",
        "password": "p",
        "host": "127.0.0.1",
        "port": 5432,
        "sslmode": "require",
    }
    for url in (
        "postgres://u:p@127.0.0.1/db",
        "postgresql+psycopg://u:p@/db",
        "postgresql+psycopg://u:p@host/",
    ):
        with pytest.raises(ProviderError) as error:
            Mem0Index.pgvector_config(url)
        assert error.value.code == "invalid_request" and url not in str(error.value)
    config = GroupBrainConfig(
        embedding_base_url="http://127.0.0.1/v1",
        embedding_api_key="REDACTED_TEST_VALUE",
    )
    index = Mem0Index(
        config,
        vector_store={
            "provider": "qdrant",
            "config": {
                "collection_name": "missing-test",
                "path": ":memory:",
                "embedding_model_dims": 1024,
            },
        },
    )
    import builtins

    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "mem0":
            raise ImportError("not installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(ProviderError) as error:
        index.add(namespace="n", text="safe", metadata={})
    assert error.value.code == "dependency_unavailable"


def test_telemetry_stays_off_even_when_mem0_was_imported_with_it_on(real_index):
    """Mem0 reads the setting once at import. Whoever imported it first must not decide."""
    import mem0.memory.main as mem0_main

    index, _server, _home = real_index
    mem0_main.MEM0_TELEMETRY = True
    index.add(
        namespace="gb:telemetry:discussion",
        text="Exact synthetic telemetry body",
        metadata={},
    )
    assert mem0_main.MEM0_TELEMETRY is False
