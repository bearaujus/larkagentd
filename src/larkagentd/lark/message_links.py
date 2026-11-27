"""Validate jump links supplied by the Lark message API."""

import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

# The applink paths that open one message or one chat.
LINK_PATHS = (
    "/client/message/link/open",
    "/client/message/open",
    "/client/chat/open",
    "/client/thread/open",
)
CHAT_PARAMS = {"chat_id", "chatid", "open_chat_id", "openchatid"}
MESSAGE_ID = re.compile(r"om_[A-Za-z0-9_-]{1,128}")
THREAD_ID = re.compile(r"omt_[A-Za-z0-9_-]{1,128}")
MESSAGE_LINK = re.compile(r"\[([^\]\r\n]+)\]\((https://[^\s()]+)\)")
BARE_MESSAGE_LINK = re.compile(r'https://[^\s<>()\[\]"“”]+')
FORWARD_REQUEST = re.compile(
    r"\bforward (?:me |us )?(?:(?:the|this|that) )?(?:original )?(?:lark )?message\b",
    re.I,
)


def verified_link(
    url, *, chat_id=None, hosts=("applink.larksuite.com", "applink.feishu.cn")
) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in hosts
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        if parsed.port not in (None, 443):
            return None
        # Only the paths that open a message or a chat. Anything else on an applink host (a redirect, an
        # invite, a share link, a web view) is not a message reference.
        if parsed.path not in LINK_PATHS or any(
            word in parsed.path.lower() for word in ("invite", "share", "join")
        ):
            return None
        params = parse_qs(parsed.query, keep_blank_values=True)
        carried = [
            value
            for name, values in params.items()
            if name.lower() in CHAT_PARAMS
            for value in values
        ]
        if parsed.path == "/client/thread/open" and (
            not carried
            or not (params.get("open_thread_id") or params.get("openthreadid"))
        ):
            return None
        if chat_id is not None and any(value != chat_id for value in carried):
            return None
        return url
    except (ValueError, UnicodeError):
        return None


def message_link(
    message, chat_id, *, hosts=("applink.larksuite.com", "applink.feishu.cn")
):
    if not chat_id or message.get("chat_id", chat_id) != chat_id:
        return None
    for field in ("message_app_link", "link"):
        url = verified_link(message.get(field), chat_id=chat_id, hosts=hosts)
        if url:
            return url
    return None


def cited_message(reply, noted, session_id, chat_id):
    """Resolve a citation against the live evidence of this attempt."""
    evidence = noted.get("message_citations") or {}
    if evidence.get("session_id") != session_id or evidence.get(
        "generation"
    ) != noted.get("gb_generation"):
        return None
    links = evidence.get("links") or {}
    canonical = {message_link_key(url): (url, source) for url, source in links.items()}
    matches = [(match[0], match[1], match[2]) for match in MESSAGE_LINK.finditer(reply)]
    matches.extend(
        (match[0].rstrip(".,;"), "the original message", match[0].rstrip(".,;"))
        for match in BARE_MESSAGE_LINK.finditer(reply)
    )
    for markup, label, candidate in matches:
        url = verified_link(unescape(candidate), chat_id=chat_id)
        found = canonical.get(message_link_key(url)) if url else None
        if found:
            original_url, source = found
            return {
                **source,
                "url": original_url,
                "markup": markup,
                "label": "the original message"
                if label.startswith("https://")
                else label,
            }
    return None


def message_link_key(url):
    parts = urlsplit(url)
    query = tuple(
        sorted(
            (name, tuple(values))
            for name, values in parse_qs(parts.query, keep_blank_values=True).items()
        )
    )
    return parts.hostname, parts.path, query, parts.fragment


def message_citation_only(reply):
    reply = reply.strip()
    labeled = MESSAGE_LINK.fullmatch(reply)
    if labeled:
        return bool(verified_link(unescape(labeled[2])))
    plain = BARE_MESSAGE_LINK.fullmatch(reply)
    return bool(plain and verified_link(unescape(plain[0])))


def copied_message_links(text):
    """Recognize Lark's issued links without constructing or decoding a token."""
    for match in BARE_MESSAGE_LINK.finditer(text):
        url = verified_link(unescape(match[0].rstrip(".,;")))
        if not url:
            continue
        parsed = urlsplit(url)
        values = parse_qs(parsed.query).get("token", [])
        if (
            parsed.path == "/client/message/link/open"
            and len(values) == 1
            and values[0]
        ):
            yield url


def native_copied_reply(answer, provided, source=None):
    allowed = set(copied_message_links(provided))
    if source:
        allowed.update(copied_message_links(source["url"]))
    used = set(copied_message_links(answer))
    if not used or not used <= allowed:
        return None
    return MESSAGE_LINK.sub(
        lambda match: unescape(match[2]) if unescape(match[2]) in allowed else match[0],
        answer,
    )
