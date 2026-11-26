from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from ..toolservice.larkcli import LarkError, parse_link
from .worker import PermanentJobError


@dataclass(frozen=True)
class FetchedDocument:
    kind: str
    canonical_token: str
    wiki_token: str | None
    title: str
    markdown: str
    provider_revision: str | None
    url: str


class DocumentError(PermanentJobError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


_ERROR_MAP = {
    "forbidden": "forbidden",
    "not_found": "not_found",
    "rate_limited": "rate_limited",
    "timeout": "timeout",
    "bad_link": "unsupported",
    "unsupported": "unsupported",
    "lark_unavailable": "unavailable",
}


def validate_document_url(url):
    if not isinstance(url, str) or not 1 <= len(url) <= 2048:
        raise DocumentError("unsupported")
    try:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise DocumentError("unsupported")
        if not parsed.hostname:
            raise DocumentError("unsupported")
        kind, token, _ = parse_link(url)
    except (LarkError, ValueError):
        raise DocumentError("unsupported") from None
    if kind not in ("docx", "wiki"):
        raise DocumentError("unsupported")
    return kind, token, parsed


class CliDocumentSource:
    def __init__(self, reader):
        self.reader = reader

    @staticmethod
    def _call(fn, url):
        try:
            return fn(url)
        except LarkError as exc:
            raise DocumentError(_ERROR_MAP.get(str(exc), "unavailable")) from None

    def fetch(self, url):
        kind, token, _ = validate_document_url(url)
        result = self._call(self.reader.doc_markdown, url)
        return FetchedDocument(
            kind="lark_docx",
            canonical_token=result.get("canonical_token", token),
            wiki_token=result.get("wiki_token") if kind == "wiki" else None,
            title=result.get("title", ""),
            markdown=result.get("markdown", ""),
            provider_revision=result.get("provider_revision"),
            url=url,
        )

    def check(self, url):
        validate_document_url(url)
        # The CLI exposes no metadata-only document read, so this full read is necessary to verify ACL.
        result = self._call(self.reader.doc_markdown, url)
        return {
            "accessible": True,
            "provider_revision": result.get("provider_revision"),
        }
