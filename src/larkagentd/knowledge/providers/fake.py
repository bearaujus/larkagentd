"""Deterministic in-memory semantic index for tests and local callers."""

from __future__ import annotations

import hashlib
import math

from .base import (
    Hit,
    ProviderError,
    make_hit,
    validate_metadata,
    validate_provider_id,
    validate_text,
    validate_top_k,
)


class FakeIndex:
    def __init__(self, dimensions: int = 16):
        if not isinstance(dimensions, int) or dimensions < 1:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions
        self._records: dict[str, list[Hit]] = {}
        self._failure: dict[str, str] = {}
        self.timeout_after_write = False
        self.duplicate_adds = False
        self._unavailable = False

    def fail_next(self, operation: str, code: str) -> None:
        if (
            operation not in {"add", "search", "list_scoped", "delete"}
            or code not in ProviderError.CODES
        ):
            raise ValueError("invalid failure injection")
        self._failure[operation] = code

    def _fail(self, operation: str) -> None:
        code = self._failure.pop(operation, None)
        if code:
            raise ProviderError(code)

    def _vector(self, text: str) -> list[float]:
        raw = hashlib.shake_256(text.encode("utf-8")).digest(self.dimensions * 4)
        values = [
            int.from_bytes(raw[i : i + 4], "big") / 2147483647.5 - 1
            for i in range(0, len(raw), 4)
        ]
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]

    def add(self, *, namespace: str, text: str, metadata: dict) -> list[str]:
        self._fail("add")
        validate_text(text)
        md = validate_metadata(metadata)
        if not isinstance(namespace, str) or not namespace:
            raise ProviderError("invalid_request")
        count = 2 if self.duplicate_adds else 1
        ids = []
        for _ in range(count):
            provider_id = hashlib.sha256(
                f"{namespace}\0{len(self._records.get(namespace, []))}\0{text}".encode()
            ).hexdigest()
            self._records.setdefault(namespace, []).append(
                make_hit(provider_id, text, md, 1.0)
            )
            ids.append(provider_id)
        if self.timeout_after_write:
            self.timeout_after_write = False
            raise ProviderError("timeout")
        return ids

    def search(
        self,
        *,
        namespace: str,
        query: str,
        top_k: int,
        metadata_filters: dict | None = None,
    ) -> list[Hit]:
        self._fail("search")
        validate_text(query)
        validate_top_k(top_k)
        filters = validate_metadata(metadata_filters or {})
        if "user_id" in filters:
            if filters.pop("user_id") != namespace:
                return []
        target = self._vector(query)
        ranked = []
        for hit in self._records.get(namespace, []):
            if all(hit.metadata.get(key) == value for key, value in filters.items()):
                vector = self._vector(hit.text)
                ranked.append((sum(a * b for a, b in zip(target, vector)), hit))
        ranked.sort(key=lambda item: (-item[0], item[1].provider_id))
        return [
            make_hit(hit.provider_id, hit.text, hit.metadata, score)
            for score, hit in ranked[:top_k]
        ]

    def list_scoped(
        self, *, namespace: str, cursor: str | None = None, limit: int = 100
    ) -> tuple[list[Hit], str | None]:
        self._fail("list_scoped")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise ProviderError("invalid_request")
        if cursor is None:
            offset = 0
        else:
            try:
                offset = int(cursor)
                if str(offset) != cursor or offset < 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise ProviderError("invalid_request") from None
        records = self._records.get(namespace, [])
        page = records[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(records) else None
        return list(page), next_cursor

    def delete(self, *, namespace: str, provider_id: str) -> None:
        self._fail("delete")
        validate_provider_id(provider_id)
        records = self._records.get(namespace, [])
        for index, hit in enumerate(records):
            if hit.provider_id == provider_id:
                del records[index]
                return
        if any(
            hit.provider_id == provider_id
            for values in self._records.values()
            for hit in values
        ):
            raise ProviderError("namespace_mismatch")
        raise ProviderError("not_found")

    def health(self) -> dict:
        return {
            "status": "unavailable" if self._unavailable else "ready",
            "reason": "dependency_unavailable" if self._unavailable else None,
            "model": "fake-hash",
            "dimensions": self.dimensions,
        }

    def unavailable(self, value: bool = True) -> None:
        self._unavailable = value

    def records(self, namespace: str) -> list[Hit]:
        return list(self._records.get(namespace, []))
