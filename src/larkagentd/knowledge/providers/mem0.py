"""Mem0 OSS adapter. SDK imports and calls remain lazy and bounded."""

from __future__ import annotations

import concurrent.futures
import os
from urllib.parse import parse_qs, unquote, urlsplit

from ..models import GroupBrainConfig
from .base import (
    Hit,
    ProviderError,
    make_hit,
    validate_metadata,
    validate_provider_id,
    validate_text,
    validate_top_k,
)


def harden_mem0(imported=False):
    """Keep Mem0 from sending telemetry and from writing files outside our control.

    Mem0 reads MEM0_TELEMETRY and MEM0_DIR once, when its modules are first imported, so the environment
    is set before the first import. If something imported mem0 earlier (a test module, another library)
    those values are already fixed, so after the import the module constants are forced as well. With
    telemetry off Mem0 neither posts events nor opens its own local migrations store."""
    import sys
    import tempfile

    os.environ["MEM0_TELEMETRY"] = "false"
    if "MEM0_DIR" not in os.environ and "mem0" not in sys.modules:
        # A private folder that vanishes with the process: nothing of Mem0's lands in a home folder.
        os.environ["MEM0_DIR"] = tempfile.mkdtemp(prefix="group-brain-mem0-")
    if imported:
        for name in ("mem0.memory.main", "mem0.memory.telemetry"):
            module = sys.modules.get(name)
            if module is not None and hasattr(module, "MEM0_TELEMETRY"):
                module.MEM0_TELEMETRY = False


class Mem0Index:
    def __init__(
        self,
        config: GroupBrainConfig,
        *,
        database_url: str | None = None,
        vector_store: dict | None = None,
        executor=None,
    ):
        self._config = config
        self._database_url = database_url
        # Test seam: supply an isolated vector-store config (for example Qdrant local mode).
        self._vector_store_override = vector_store
        self._executor = executor or concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, config.worker_concurrency),
            thread_name_prefix="group-brain-index",
        )
        self._memory = None
        self._not_configured = not config.embedding_configured

    @staticmethod
    def pgvector_config(database_url: str) -> dict:
        try:
            parsed = urlsplit(database_url)
            if (
                parsed.scheme != "postgresql+psycopg"
                or not parsed.hostname
                or not parsed.path.strip("/")
                or parsed.username is None
                or parsed.password is None
            ):
                raise ValueError
            query = parse_qs(parsed.query, keep_blank_values=True)
            sslmode = query.get("sslmode", [None])[0]
            port = parsed.port or 5432
            result = {
                "dbname": unquote(parsed.path.lstrip("/")),
                "user": unquote(parsed.username),
                "password": unquote(parsed.password),
                "host": parsed.hostname,
                "port": port,
            }
            if sslmode is not None:
                result["sslmode"] = sslmode
            return result
        except Exception:
            raise ProviderError("invalid_request") from None

    def _ensure(self):
        if self._not_configured:
            raise ProviderError("dependency_unavailable")
        if self._memory is not None:
            return self._memory
        try:
            harden_mem0()
            from mem0 import Memory
        except ImportError:
            raise ProviderError("dependency_unavailable") from None
        harden_mem0(imported=True)
        cfg = {
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": self._config.embedding_model,
                    "embedding_dims": self._config.embedding_dimensions,
                    "openai_base_url": self._config.embedding_base_url,
                    "api_key": self._config.embedding_api_key,
                },
            },
            # infer is always false. This explicit inert endpoint prevents Mem0's default LLM.
            "llm": {
                "provider": "openai",
                "config": {
                    "model": self._config.llm_model,
                    "openai_base_url": self._config.llm_base_url
                    or self._config.embedding_base_url,
                    "api_key": self._config.llm_api_key
                    or self._config.embedding_api_key,
                },
            },
            "history_db_path": ":memory:",
        }
        if self._vector_store_override is not None:
            cfg["vector_store"] = self._vector_store_override
        else:
            if not self._database_url:
                raise ProviderError("dependency_unavailable")
            cfg["vector_store"] = {
                "provider": "pgvector",
                "config": {
                    **self.pgvector_config(self._database_url),
                    "collection_name": self._config.collection,
                    "embedding_model_dims": self._config.embedding_dimensions,
                },
            }
        try:
            self._memory = Memory.from_config(cfg)
        except ImportError:
            raise ProviderError("dependency_unavailable") from None
        except Exception:
            raise ProviderError("dependency_unavailable") from None
        return self._memory

    def _call(self, fn, *args, **kwargs):
        try:
            future = self._executor.submit(fn, *args, **kwargs)
            # A timed-out thread is not cancelled, so callers must reconcile uncertain writes.
            return future.result(timeout=self._config.provider_timeout_seconds)
        except concurrent.futures.TimeoutError:
            raise ProviderError("timeout") from None
        except ProviderError:
            raise
        except Exception as exc:
            message = str(exc).lower()
            if "dimension" in message or "vector size" in message:
                raise ProviderError("invalid_dimension") from None
            raise ProviderError("dependency_unavailable") from None

    @staticmethod
    def _results(response):
        if not isinstance(response, dict) or not isinstance(
            response.get("results"), list
        ):
            raise ProviderError("bad_response")
        return response["results"]

    def add(self, *, namespace: str, text: str, metadata: dict) -> list[str]:
        validate_text(text)
        md = validate_metadata(metadata)
        if not isinstance(namespace, str) or not namespace:
            raise ProviderError("invalid_request")
        response = self._call(
            self._ensure().add, text, user_id=namespace, metadata=md, infer=False
        )
        rows = self._results(response)
        ids = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get("event") != "ADD"
                or row.get("memory") != text
            ):
                raise ProviderError("bad_response")
            hit = make_hit(row.get("id"), row["memory"], md, 1.0)
            ids.append(hit.provider_id)
        if not ids:
            raise ProviderError("bad_response")
        return ids

    def search(
        self,
        *,
        namespace: str,
        query: str,
        top_k: int,
        metadata_filters: dict | None = None,
    ) -> list[Hit]:
        validate_text(query)
        validate_top_k(top_k)
        filters = validate_metadata(metadata_filters or {})
        if "user_id" in filters:
            if filters.pop("user_id") != namespace:
                return []
        response = self._call(
            self._ensure().search,
            query,
            filters={"user_id": namespace, **filters},
            top_k=top_k,
            threshold=0.0,
        )
        hits = []
        for row in self._results(response):
            if not isinstance(row, dict) or "metadata" not in row or "score" not in row:
                raise ProviderError("bad_response")
            metadata = row["metadata"]
            owner = metadata.get(
                "user_id", row.get("user_id", metadata.get("namespace"))
            )
            if owner is not None and owner != namespace:
                raise ProviderError("namespace_mismatch")
            hits.append(
                make_hit(row.get("id"), row.get("memory"), metadata, row["score"])
            )
        return hits

    def _all(self, namespace: str):
        response = self._call(
            self._ensure().get_all, filters={"user_id": namespace}, top_k=10000
        )
        rows = self._results(response)
        hits = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("metadata"), dict):
                raise ProviderError("bad_response")
            owner = row["metadata"].get(
                "user_id", row.get("user_id", row["metadata"].get("namespace"))
            )
            if owner is not None and owner != namespace:
                raise ProviderError("namespace_mismatch")
            hits.append(
                make_hit(row.get("id"), row.get("memory"), row["metadata"], 0.0)
            )
        return hits

    def list_scoped(
        self, *, namespace: str, cursor: str | None = None, limit: int = 100
    ) -> tuple[list[Hit], str | None]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise ProviderError("invalid_request")
        offset = 0
        if cursor is not None:
            try:
                offset = int(cursor)
                if str(offset) != cursor or offset < 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise ProviderError("invalid_request") from None
        rows = self._all(namespace)
        page = rows[offset : offset + limit]
        return page, str(offset + limit) if offset + limit < len(rows) else None

    def delete(self, *, namespace: str, provider_id: str) -> None:
        validate_provider_id(provider_id)
        rows = self._all(namespace)
        if not any(hit.provider_id == provider_id for hit in rows):
            raise ProviderError("not_found")
        self._call(self._ensure().delete, provider_id)

    def health(self) -> dict:
        if self._not_configured:
            return {
                "status": "not_configured",
                "reason": "dependency_unavailable",
                "model": self._config.embedding_model or None,
                "dimensions": self._config.embedding_dimensions or None,
            }
        try:
            from importlib.util import find_spec

            installed = find_spec("mem0") is not None
        except (ImportError, ValueError):
            installed = False
        return {
            "status": "ready" if installed else "unavailable",
            "reason": None if installed else "dependency_unavailable",
            "model": self._config.embedding_model,
            "dimensions": self._config.embedding_dimensions,
        }
