"""Shared semantic-index contract and validation."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Hit:
    provider_id: str
    text: str
    metadata: dict
    score: float


class ProviderError(Exception):
    """A stable, content-free provider failure."""

    CODES = frozenset(
        {
            "dependency_unavailable",
            "timeout",
            "bad_response",
            "not_found",
            "namespace_mismatch",
            "invalid_dimension",
            "invalid_request",
        }
    )

    def __init__(self, code: str):
        if code not in self.CODES:
            raise ValueError("unsupported provider error code")
        self.code = code
        super().__init__(code)


class SemanticIndex(Protocol):
    def add(self, *, namespace: str, text: str, metadata: dict) -> list[str]: ...
    def search(
        self,
        *,
        namespace: str,
        query: str,
        top_k: int,
        metadata_filters: dict | None = None,
    ) -> list[Hit]: ...
    def list_scoped(
        self, *, namespace: str, cursor: str | None = None, limit: int = 100
    ) -> tuple[list[Hit], str | None]: ...
    def delete(self, *, namespace: str, provider_id: str) -> None: ...
    def health(self) -> dict: ...


def namespace_for(deployment: str, group_key: str, kind: str) -> str:
    if kind not in ("discussion", "memory", "document"):
        raise ValueError("invalid namespace kind")
    parts = (deployment, group_key, kind)
    if any(not isinstance(part, str) for part in parts):
        raise ValueError("namespace parts must be strings")
    digest = hashlib.sha256(
        b"".join(
            len(part.encode()).to_bytes(8, "big") + part.encode() for part in parts
        )
    ).hexdigest()[:32]
    return f"gb:{digest}:{kind}"


def validate_text(text: str) -> None:
    if not isinstance(text, str) or not 1 <= len(text) <= 8000:
        raise ProviderError("invalid_request")


def validate_metadata(metadata: dict) -> dict:
    if not isinstance(metadata, dict) or len(metadata) > 20:
        raise ProviderError("invalid_request")
    for key, value in metadata.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 100:
            raise ProviderError("invalid_request")
        if not isinstance(value, (str, int, float, bool)) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            raise ProviderError("invalid_request")
        if isinstance(value, str) and len(value) > 500:
            raise ProviderError("invalid_request")
    return dict(metadata)


def validate_top_k(top_k: int) -> None:
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 100:
        raise ProviderError("invalid_request")


def validate_provider_id(provider_id: str) -> None:
    if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 200:
        raise ProviderError("invalid_request")


def make_hit(provider_id, text, metadata, score) -> Hit:
    if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 200:
        raise ProviderError("bad_response")
    if not isinstance(text, str):
        raise ProviderError("bad_response")
    if not isinstance(metadata, dict) or any(
        not isinstance(k, str) or not isinstance(v, (str, int, float, bool))
        for k, v in metadata.items()
    ):
        raise ProviderError("bad_response")
    if (
        not isinstance(score, (int, float))
        or isinstance(score, bool)
        or not math.isfinite(float(score))
    ):
        raise ProviderError("bad_response")
    return Hit(provider_id, text, dict(metadata), float(score))
