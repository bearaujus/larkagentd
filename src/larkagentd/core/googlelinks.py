"""Google document identifiers, never user-selected HTTP destinations."""

import re
from urllib.parse import parse_qs, urlsplit


def parse_link(value):
    try:
        parts = urlsplit(value)
        match = re.fullmatch(
            r"/(spreadsheets|document)/(?:u/\d+/)?d/([A-Za-z0-9_-]{8,128})(?:/(?:edit|view|preview|copy))?/?",
            parts.path,
        )
        if (
            parts.scheme != "https"
            or parts.hostname != "docs.google.com"
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
            or not match
        ):
            return None
        kind, token = match.groups()
        params = {**parse_qs(parts.query), **parse_qs(parts.fragment)}
        gid = params.get("gid", [None])[0]
        if gid is not None and not re.fullmatch(r"\d{1,12}", gid):
            return None
        return kind, token, gid
    except (ValueError, TypeError, AttributeError):
        return None


def links(text):
    found = []
    for value in re.findall(r"https://[A-Za-z0-9._~:/?#@!$&*+,;=%-]+", text or ""):
        parsed = parse_link(value.rstrip(".,;:!?)}"))
        if parsed:
            identity = ":".join(parsed[:2])
            if identity not in found:
                found.append(identity)
            if len(found) == 3:
                break
    return found
