"""Presentation of account data verified by the configured identity provider."""

from datetime import datetime, timezone


def entity_display(state):
    identity = state.get("identity") or {}
    account = str(state.get("account") or identity.get("account_id") or "")
    label = str(state.get("account_label") or "")
    if label and account and label != account:
        return f"{label} ({account})"
    return label or account or "Unavailable"


def principal_display(identity):
    principal = (identity or {}).get("principal")
    return principal if isinstance(principal, str) and principal else "Unavailable"


def expiry_display(value):
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            return "Unavailable"
        return date.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    except (ValueError, AttributeError, TypeError):
        return "Unavailable"
