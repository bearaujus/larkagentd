"""The Lark links a user wrote in a message.

The Gateway does not read documents: `lark_read` is the one reader. The Gateway only
finds the links, so it can tell the model the user shared some and record which tokens
`lark_read` may open. A link selects a document identifier, never an HTTP destination.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_LINKS = 3
TOKEN = re.compile(r"[A-Za-z0-9_-]{8,128}")
# A link is the ASCII URL characters from "https://" on, so it ends at whitespace, a bracket, a quote,
# CJK punctuation or any CJK letter: "(https://...)and" and "https://...<token>请看" are both one link.
URL = re.compile(r"https?://[A-Za-z0-9._~:/?#@!$&*+,;=%-]+")
# The kinds `lark_read` can open: a document, a wiki page, a spreadsheet and a Base.
KINDS = {"docx", "wiki", "sheets", "base", "slides"}
HOSTS = ("larksuite.com", "feishu.cn", "larkoffice.com")
# Lark document, wiki, sheet and Base tokens are 27 characters. A link chip followed by words arrives as
# "https://host/docx/<27 characters>and summarize ...": the path runs on into the next word.
LARK_TOKEN_LENGTH = 27
GLUED = re.compile(
    r"(https://[A-Za-z0-9.-]+/(?:docx|wiki|sheets|base)/)"
    rf"([A-Za-z0-9_-]{{{LARK_TOKEN_LENGTH + 1},}})"
)


def lark_host(host):
    return any(host.endswith("." + domain) for domain in HOSTS)


def separate_glued_links(text):
    """Put a space after the 27-character token of a Lark link that runs into the next word, so the
    words are not read as part of the token and the model sees the same link the user sent."""

    def split(match):
        if not lark_host(urlsplit(match.group(1)).hostname or ""):
            return match.group(0)
        token = match.group(2)
        return (
            f"{match.group(1)}{token[:LARK_TOKEN_LENGTH]} {token[LARK_TOKEN_LENGTH:]}"
        )

    return GLUED.sub(split, text) if isinstance(text, str) else text


@dataclass(frozen=True)
class DocumentLink:
    kind: str
    token: str
    url: str


def parse_link(value):
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (
            parts.scheme != "https"
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
            or not lark_host(host)
        ):
            return None
        route = parts.path.strip("/").split("/")
        if len(route) != 2 or route[0] not in KINDS or not TOKEN.fullmatch(route[1]):
            return None
        return DocumentLink(route[0], route[1], f"https://{host}/{route[0]}/{route[1]}")
    except ValueError:
        return None


def links(text):
    found, seen = [], set()
    for candidate in URL.findall(text or ""):
        link = parse_link(candidate.rstrip(".,;:!?)]}。，；）】'"))
        if link and (link.kind, link.token) not in seen:
            seen.add((link.kind, link.token))
            found.append(link)
            if len(found) > MAX_LINKS:
                break
    return found
