"""The local sanitizer for group knowledge text.

Deterministic rules only, no model. Text that holds a credential, a private key, an email address, a
phone number or an identity-number shape is BLOCKED whole: nothing of it is kept, and the reasons name
the kind, never the match. Everything that is kept is what was scanned.

Safety of the sanitizer itself: it runs on text written by any group member, inside the Gateway. The
text is normalized first (NFKC, no zero-width or other format characters, one-line whitespace), cut to a
fixed scan limit before any pattern runs, and every pattern is bounded, so no input can make it slow."""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass

# No pattern ever sees more than this many characters, whatever `max_chars` is.
SCAN_LIMIT = 12_000
MAX_DECODED_SEGMENTS = 5


@dataclass(frozen=True)
class Sanitized:
    text: str | None
    blocked: bool
    reasons: tuple[str, ...]


# A label that names a secret, then a separator (ASCII or fullwidth colon, equals, an arrow or "is"),
# then a value that is not just punctuation.
_SECRET_LABEL = (
    r"(?:pass(?:word|wd)?|pwd|secret|(?:app|client|access|refresh|auth|api|session|security)[ _-]?"
    r"(?:secret|token|key)|secret[ _-]?key|private[ _-]?key|token|credentials?)"
)
_SEPARATOR = r"(?:\s{0,3}[:=\uff1a\uff1d]\s{0,3}|\s{0,3}=>\s{0,3})"

_RULES = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z]{2,10} ){0,3}PRIVATE KEY(?: BLOCK)?-----", re.I
        ),
    ),
    (
        # Any scheme with user:password@ in front of a host: postgresql+psycopg://, https://, amqp://.
        "dsn_password",
        re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,24}://[^\s/:@]{1,128}:[^\s/@]{1,256}@"),
    ),
    (
        "dsn_password",
        re.compile(r"(?i)\bjdbc:[a-z0-9]{2,20}:[^\s]{0,200}password=[^\s&;]{1,128}"),
    ),
    (
        "credential",
        re.compile(
            rf"(?i)\b(?:bearer|basic|token)\s{{1,3}}(?=[A-Za-z0-9._~+/=-]{{0,512}}\d)[A-Za-z0-9._~+/=-]{{12,512}}"
            rf"|\bauthorization\s{{0,3}}[:=]\s{{0,3}}\S{{3,512}}"
            # Only these labels are followed by a plain "is": "the password is hunter2".
            rf"|\b(?:pass(?:word|wd)?|pwd|secret|private[ _-]?key)\s+is\s+[^\s,;]{{6,512}}"
            rf"|\b{_SECRET_LABEL}{_SEPARATOR}[^\s,;]{{6,512}}"
        ),
    ),
    (
        "credential",
        re.compile(
            r"\b(?:sk-|sk_live_|sk_test_|rk_live_)[A-Za-z0-9_-]{12,200}"
            r"|\b(?:AKIA|ASIA|LTAI)[A-Za-z0-9]{12,40}"
            r"|\bAIza[0-9A-Za-z_-]{30,60}"
            r"|\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,100}"
            r"|\bgithub_pat_[A-Za-z0-9_]{30,200}"
            r"|\bglpat-[A-Za-z0-9_-]{16,100}"
            r"|\bxox[abprs]-[A-Za-z0-9-]{10,200}"
            r"|\bSTS\.[A-Za-z0-9]{12,200}"
        ),
    ),
    (
        "credential",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{5,2000}\.eyJ[A-Za-z0-9_-]{5,2000}\.[A-Za-z0-9_-]{5,2000}\b"
        ),
    ),
    (
        "pii_email",
        re.compile(r"(?i)\b[a-z0-9._%+-]{1,64}@[a-z0-9.-]{1,255}\.[a-z]{2,24}\b"),
    ),
    ("pii_id_number", re.compile(r"(?<!\d)\d{12,19}(?!\d)")),
)

# A long mixed-case token is treated as a secret even without a label (a key, a signed value).
# A phone number needs at least nine digits and must not be a date or a time stamp.
_PHONE = re.compile(r"(?<![\w-])\+?\d[\d ().-]{7,30}\d(?!\w)")
_DATE_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_-]{28,200}")
_BASE64_SEGMENT = re.compile(r"[A-Za-z0-9+/]{24,400}={0,2}")


def normalize(text: str) -> str:
    """One line of NFKC text with no format characters (zero-width joiners and the like) and
    single spaces. Cheap and linear."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return " ".join(text.split())


def _long_secret_token(text: str) -> bool:
    for match in _LONG_TOKEN.finditer(text):
        token = match.group()
        if (
            any(c.islower() for c in token)
            and any(c.isupper() for c in token)
            and any(c.isdigit() for c in token)
        ):
            return True
    return False


def _phone_like(text: str) -> bool:
    for match in _PHONE.finditer(text):
        shape = match.group()
        if sum(c.isdigit() for c in shape) >= 9 and not _DATE_LIKE.fullmatch(
            shape.strip()
        ):
            return True
    return False


def _kinds(text: str) -> list[str]:
    found = [reason for reason, pattern in _RULES if pattern.search(text)]
    if _phone_like(text):
        found.append("pii_phone")
    if _long_secret_token(text):
        found.append("credential")
    return found


def _decoded_kinds(text: str) -> list[str]:
    """Reasons found inside a few base64 segments: a secret wrapped in base64 is still a secret."""
    found: list[str] = []
    for match in list(_BASE64_SEGMENT.finditer(text))[:MAX_DECODED_SEGMENTS]:
        segment = match.group()
        try:
            decoded = base64.b64decode(
                segment + "=" * (-len(segment) % 4), validate=True
            )
            inner = decoded.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if inner.isprintable():
            found.extend(_kinds(normalize(inner)))
    return found


def sanitize(text: str, *, max_chars: int = 4000) -> Sanitized:
    if not isinstance(text, str) or not text.strip():
        return Sanitized(None, True, ("empty",))
    limit = max(SCAN_LIMIT, max_chars)
    # Normalizing is linear; the cut keeps every later pattern bounded by `limit`.
    normalized = normalize(text[: limit * 4])[:limit]
    if not normalized:
        return Sanitized(None, True, ("empty",))
    reasons = tuple(dict.fromkeys([*_kinds(normalized), *_decoded_kinds(normalized)]))
    if reasons:
        return Sanitized(None, True, reasons)
    if len(normalized) > max_chars:
        return Sanitized(normalized[:max_chars], False, ("truncated",))
    return Sanitized(normalized, False, ())


def token_count(text: str) -> int:
    return len(text) // 4 + 1
