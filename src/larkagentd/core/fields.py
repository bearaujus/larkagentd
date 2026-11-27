"""Validation shared by identity and connector boundaries."""

import re

EMAIL = re.compile(r"[a-z0-9._%+-]{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,24}")
LARK_APP_ID = re.compile(r"cli_[A-Za-z0-9_-]{1,128}")

SECRET = re.compile(
    r"LTAI[0-9A-Za-z]{12,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|glpat-[0-9A-Za-z_-]{10,}"
    r"|eyJ[0-9A-Za-z_-]{20,}\.[0-9A-Za-z_-]{10,}\."
    r"|(?i:(?:secret|password|passwd)\s*[:=]\s*['\"]?[0-9A-Za-z/+=_-]{12,})"
)


def normalize_email(value):
    email = value.strip().lower() if isinstance(value, str) else ""
    if not EMAIL.fullmatch(email) or ".." in email:
        raise ValueError("bad_email")
    return email


def lark_app_owner(owner):
    return bool(LARK_APP_ID.fullmatch(owner.partition(":")[2]))
