"""Best-effort, content-free invocation metadata. Never store arguments or result bodies."""

import math
import re
import time
import uuid

from sqlalchemy import func, insert, update

from ..core.diagnostics import emit
from ..core.store import query_activity

ERRORS = frozenset(
    (
        "login_required",
        "credentials_unavailable",
        "connection_changed",
        "no_permission",
        "maxcompute_estimate_denied",
        "query_result_unavailable",
        "attachment_unavailable",
        "attachment_limit",
        "busy",
        "query_in_progress",
        "timeout",
        "cancelled",
        "tool_unavailable",
        "tool_not_allowed",
        "not_enabled_for_group",
        "sandbox_unavailable",
        "not_configured",
        "bad_arguments",
        "result_too_large",
        "tool_failed",
        "sql_rejected",
        "too_large",
        "no_estimate",
    )
)


def start(store, turn, name, project, operation):
    identifier = str(uuid.uuid4())
    project = (
        project
        if isinstance(project, str) and re.fullmatch(r"[A-Za-z0-9_-]{0,256}", project)
        else ""
    )
    try:
        with store.engine.begin() as conn:
            conn.execute(
                insert(query_activity).values(
                    id=identifier,
                    session_id=turn["session_id"],
                    job_id=turn["job_id"],
                    requester=turn["owner"],
                    principal=turn["owner"],
                    tool=name,
                    operation=operation,
                    project=project,
                    chat_type=turn.get("chat_type") or "unknown",
                    chat_id=turn.get("chat_id"),
                    thread=turn.get("thread"),
                )
            )
        return identifier
    except Exception as exc:
        emit("activity.unavailable", stage="start", error_type=type(exc).__name__)
        return None


def number(value, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if integer:
        return value if isinstance(value, int) and 0 <= value < 2**63 else None
    return float(value) if math.isfinite(value) and 0 <= value <= 1e18 else None


def finish(store, identifier, started, status, result=None, error=None):
    if identifier is None:
        return
    facts = result if isinstance(result, dict) else {}
    if facts.get("refused"):
        status, error = "refused", facts["refused"]
    estimate = number(facts.get("estimated_input_gb"))
    if estimate is None and isinstance(facts.get("estimate"), dict):
        estimate = number(facts["estimate"].get("input_size_gb"))
        if estimate is None:
            size = number(facts["estimate"].get("input_size_bytes"))
            estimate = size / 1024**3 if size is not None else None
    try:
        with store.engine.begin() as conn:
            conn.execute(
                update(query_activity)
                .where(query_activity.c.id == identifier)
                .values(
                    status=status,
                    error_code=error
                    if isinstance(error, str) and error in ERRORS
                    else ("tool_failed" if error else None),
                    duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                    estimated_input_gb=estimate,
                    row_count=number(facts.get("row_count"), integer=True),
                    finished_at=func.now(),
                )
            )
    except Exception as exc:
        emit("activity.unavailable", stage="finish", error_type=type(exc).__name__)
