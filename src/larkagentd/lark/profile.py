"""Display-only fields of a Lark profile. This data never authenticates a user."""

from urllib.parse import parse_qs, urlsplit

from ..core.fields import normalize_email


def label(value):
    if not isinstance(value, str):
        return None
    return "".join(char for char in value if ord(char) >= 32).strip()[:256] or None


def avatar_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
        ):
            return None
        if {key.lower() for key in parse_qs(url.query)} & {
            "access_token",
            "tenant_access_token",
            "user_access_token",
            "app_secret",
        }:
            return None
        return value
    except ValueError:
        return None


def profile_fields(profile):
    profile = profile if isinstance(profile, dict) else {}
    email = profile.get("email")
    try:
        if not isinstance(email, str) or len(email) > 254:
            raise ValueError
        email = normalize_email(email)
    except ValueError:
        email = None
    return {
        "name": label(profile.get("name")),
        "email": email,
        "avatar_url": avatar_url(profile.get("avatar_url")),
    }
