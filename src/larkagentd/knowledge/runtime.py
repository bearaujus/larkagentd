"""What the Group Brain tools and workers share: the database, the global settings and the adapters.

Built once per process. With the global switch off no adapter is created and nothing here calls out."""

import logging
from contextlib import contextmanager

from sqlalchemy import text

from ..core.diagnostics import emit
from .policy import effective


class GroupBrainRuntime:
    def __init__(
        self, engine, config, source=None, index=None, llm=None, doc_source=None
    ):
        self.engine = engine
        self.config = config
        self.source = source  # a GroupSource: reads the group's own Lark history
        self.index = (
            index  # a SemanticIndex, or None when no embedding endpoint is configured
        )
        self.llm = llm  # a structured-extraction client, or None
        self.doc_source = (
            doc_source  # a DocumentSource that reads registered Lark documents, or None
        )

    @contextmanager
    def connection(self):
        with self.engine.begin() as conn:
            yield conn

    def effective(self, group_key):
        with self.connection() as conn:
            return effective(conn, self.config, group_key)


def build_runtime(store, config, lark_reader=None, database_url=None):
    """The runtime for a process, or None while the global switch is off: with it off no adapter is
    built and no tool can reach Lark or a provider."""
    settings = config.group_brain
    if not settings.enabled:
        return None
    from .doc_sources import CliDocumentSource
    from .lark_sources import CliGroupSource

    source = CliGroupSource(lark_reader) if lark_reader is not None else None
    doc_source = CliDocumentSource(lark_reader) if lark_reader is not None else None
    index = build_index(store.engine, settings, database_url) if database_url else None
    return GroupBrainRuntime(
        store.engine, settings, source=source, index=index, doc_source=doc_source
    )


def vector_ready(engine, collection, dimensions):
    """(True, None) only when the operator has already prepared the pgvector collection: the `vector`
    extension is installed and a table named `collection` has a `vector` column of exactly `dimensions`.
    Otherwise (False, reason). It only reads the catalog and never creates anything: Mem0 would run DDL
    against the application database on first use, so the semantic index is not built until this holds."""
    try:
        with engine.connect() as conn:
            if not conn.execute(
                text("select 1 from pg_extension where extname = 'vector'")
            ).first():
                return False, "extension_missing"
            found = conn.execute(
                text(
                    "select format_type(a.atttypid, a.atttypmod) from pg_attribute a "
                    "join pg_class c on c.oid = a.attrelid "
                    "join pg_namespace n on n.oid = c.relnamespace "
                    "where c.relname = :table and n.nspname = current_schema() "
                    "and a.attname = 'vector' and not a.attisdropped"
                ),
                {"table": collection},
            ).scalar()
    except Exception:
        return False, "database_unavailable"
    if found is None:
        return False, "collection_missing"
    if found != f"vector({dimensions})":
        return False, "dimension_mismatch"
    return True, None


def build_index(engine, config, database_url):
    """The semantic index for this process, or None with a logged reason. Needs an approved embedding
    endpoint, the optional Mem0 package and a collection the operator prepared (see vector_ready). A
    missing piece is never an error: recall is then lexical and live only."""
    if not config.embedding_configured:
        return None
    ready, reason = vector_ready(engine, config.collection, config.embedding_dimensions)
    if not ready:
        emit("knowledge.index_unavailable", logging.WARNING, reason=reason)
        return None
    try:
        from .providers.mem0 import Mem0Index

        return Mem0Index(config, database_url=database_url)
    except Exception as exc:
        emit("knowledge.index_unavailable", logging.WARNING, reason=type(exc).__name__)
        return None
