"""Validate jump links supplied by the Lark message API."""

from urllib.parse import parse_qs, urlsplit

# The applink paths that open one message or one chat.
LINK_PATHS = (
    "/client/message/link/open",
    "/client/message/open",
    "/client/chat/open",
    "/client/thread/open",
)
CHAT_PARAMS = {"chat_id", "chatid", "open_chat_id", "openchatid"}


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
