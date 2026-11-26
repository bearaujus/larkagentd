import pytest
from sqlalchemy import insert, select
from test_core import store as store_fixture

from larkagentd.core.store import gateway_policies
from larkagentd.knowledge.doc_sources import (
    CliDocumentSource,
    DocumentError,
    validate_document_url,
)
from larkagentd.knowledge.documents import Documents, index_document_revision
from larkagentd.knowledge.models import GroupBrainConfig
from larkagentd.knowledge.providers.fake import FakeIndex
from larkagentd.knowledge.runtime import GroupBrainRuntime
from larkagentd.knowledge.store import KnowledgeStore
from larkagentd.knowledge.tables import (
    gb_document_chunks,
    gb_document_revisions,
    gb_documents,
    gb_index_entries,
)
from larkagentd.knowledge.worker import KnowledgeWorker
from larkagentd.toolservice.larkcli import LarkError, LarkReader

store = store_fixture
GROUP = "group:oc_example1234"
kb = KnowledgeStore()


class Reader:
    def __init__(self):
        self.calls = []

    def doc_markdown(self, url):
        self.calls.append(url)
        return {
            "canonical_token": "docx_example123456",
            "wiki_token": None,
            "title": "Example",
            "markdown": "# Example\nText",
            "provider_revision": "r1",
        }


def test_document_source_uses_only_validated_lark_link_and_keeps_full_markdown():
    reader = Reader()
    source = CliDocumentSource(reader)
    fetched = source.fetch("https://example.larksuite.com/docx/docx_example123456")
    assert fetched.markdown == "# Example\nText"
    assert fetched.provider_revision == "r1"
    assert len(reader.calls) == 1
    for url in (
        "https://example.com/docx/docx_example123456",
        "https://user@example.larksuite.com/docx/docx_example123456",
        "https://example.larksuite.com:444/docx/docx_example123456",
        "file:///etc/passwd",
    ):
        with pytest.raises(DocumentError):
            validate_document_url(url)
    assert len(reader.calls) == 1


def test_doc_markdown_keeps_full_content_and_rejects_unsafe_urls():
    reader = LarkReader.__new__(LarkReader)
    content = "safe " * 10_000
    reader._call = lambda command, args: {
        "document": {"content": content, "title": "Example", "revision": "rev1"}
    }
    result = reader.doc_markdown(
        "https://example.larksuite.com/docx/docx_example123456"
    )
    assert result["markdown"] == content
    assert result["provider_revision"] == "rev1"
    for url in (
        "http://example.larksuite.com/docx/docx_example123456",
        "https://user@example.larksuite.com/docx/docx_example123456",
        "https://example.larksuite.com:444/docx/docx_example123456",
    ):
        with pytest.raises(LarkError):
            reader.doc_markdown(url)


def test_document_source_maps_lark_errors_without_leaking_message():
    class BrokenReader:
        def doc_markdown(self, url):
            raise LarkError("timeout")

    with pytest.raises(DocumentError) as error:
        CliDocumentSource(BrokenReader()).fetch(
            "https://example.larksuite.com/docx/docx_example123456"
        )
    assert error.value.code == "timeout"
    assert str(error.value) == "timeout"


def test_document_revision_activates_atomically_and_search_ignores_orphan_vectors(
    store,
):
    class Source:
        def __init__(self):
            self.checks = 0

        def check(self, url):
            self.checks += 1
            return {"accessible": True, "provider_revision": "rev1"}

    config = GroupBrainConfig(enabled=True)
    index = FakeIndex()
    source = Source()
    runtime = GroupBrainRuntime(store.engine, config, index=index, doc_source=source)
    with store.engine.begin() as conn:
        conn.execute(insert(gateway_policies).values(scope=GROUP, mode="all_users"))
        settings = kb.update_settings(
            conn,
            GROUP,
            0,
            {
                "enabled": True,
                "documents_enabled": True,
                "notice_ref": "notice-example",
                "policy_ref": "policy-example",
            },
            "ou_admin0001",
        )
        conn.execute(
            insert(gb_documents).values(
                id="doc_example0001",
                group_key=GROUP,
                provider_kind="lark_docx",
                canonical_doc_token="docx_example123456",
                wiki_token=None,
                source_url="https://example.larksuite.com/docx/docx_example123456",
                title="Example",
                sharing_policy_ref="share-policy-example",
                current_revision=None,
                state="approved_pending_index",
                created_by="ou_alice0001",
            )
        )
        conn.execute(
            insert(gb_document_revisions).values(
                group_key=GROUP,
                document_id="doc_example0001",
                revision=1,
                provider_revision="rev1",
                content_hash="hash-example",
                state="pending",
            )
        )
        conn.execute(
            insert(gb_document_chunks).values(
                id="chunk_example0001",
                group_key=GROUP,
                document_id="doc_example0001",
                revision=1,
                ordinal=0,
                section_path="Example",
                block_anchor=None,
                sanitized_content="Heading safe document text",
                content_hash="chunkhash-example",
                token_count=7,
            )
        )
        kb.enqueue(
            conn,
            GROUP,
            "index_revision",
            "index-document-example",
            settings["generation"],
            1,
            {"kind": "document", "document_id": "doc_example0001", "revision": 1},
        )
    worker = KnowledgeWorker(
        runtime, "worker-example", {"index_revision": index_document_revision}
    )
    assert worker.run_once()
    namespace = __import__(
        "larkagentd.knowledge.providers.base", fromlist=["namespace_for"]
    ).namespace_for(config.collection, GROUP, "document")
    orphan_id = index.add(
        namespace=namespace,
        text="orphan only",
        metadata={
            "kind": "document",
            "canonical_id": "doc_example0001",
            "revision": 99,
            "ordinal": 99,
            "index_key": "orphan-key",
        },
    )[0]
    checks_before = source.checks
    results = Documents(runtime).search(runtime, None, GROUP, "Heading", 5)
    assert len(results) == 1
    assert results[0]["document_id"] == "doc_example0001"
    assert results[0]["revision"] == 1
    assert source.checks == checks_before + 1
    with store.engine.begin() as conn:
        document = (
            conn.execute(
                select(gb_documents).where(gb_documents.c.id == "doc_example0001")
            )
            .mappings()
            .one()
        )
        entries = list(
            conn.execute(
                select(gb_index_entries).where(
                    gb_index_entries.c.canonical_id == "doc_example0001"
                )
            ).mappings()
        )
    assert document["state"] == "active" and document["current_revision"] == 1
    assert len(entries) == 1 and entries[0]["state"] == "active"
    assert entries[0]["provider_id"] != orphan_id
