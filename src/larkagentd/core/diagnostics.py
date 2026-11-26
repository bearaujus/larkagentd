"""Operational diagnostics without credentials, response bodies, or exception text."""

import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone

from .auth import AuthError, transient_auth_error

log = logging.getLogger("larkagentd")
SAFE_CODES = {
    "sign_in_pending",
    "sign_in_code_expired",
    "poll_slowly",
    "provider_access_denied",
    "provider_unavailable",
    "InvalidResponse",
    "InvalidVerificationURI",
    "InvalidPagination",
    "TooManyPages",
    "ConnectionChanged",
    "LoginExpired",
    "LoginRequired",
    "RoleUnavailable",
    "IdentityMismatch",
}
SAFE_OPERATIONS = {
    "begin_sign_in",
    "poll_sign_in",
    "list_accounts",
    "list_roles",
    "issue_credentials",
}


def log_job_error(job, error):
    fields = job_fields(job)
    if (
        job["kind"] == "auth"
        and job.get("payload", {}).get("action") == "poll"
        and transient_auth_error(error)
    ):
        emit(
            "auth.network_interrupted",
            logging.WARNING,
            **fields,
            error_type=type(error).__name__,
        )
        return
    if isinstance(error, AuthError):
        code = str(error) if str(error) in SAFE_CODES else "provider_unavailable"
        expected = (
            job["kind"] == "auth"
            and job.get("payload", {}).get("action") == "poll"
            and code in ("sign_in_pending", "poll_slowly")
        )
        emit(
            "auth.pending" if expected else "auth.failed",
            logging.DEBUG if expected else logging.WARNING,
            **fields,
            code=code,
            http_status=error.status if isinstance(error.status, int) else None,
            operation=error.operation if error.operation in SAFE_OPERATIONS else None,
            error_type="AuthError",
        )
        return
    if job["kind"] == "send":
        attempt = job.get("payload", {}).get("attempts", 0) + 1
        emit(
            "delivery.retry" if attempt < 5 else "delivery.exhausted",
            logging.WARNING if attempt < 5 else logging.ERROR,
            **fields,
            error_type=type(error).__name__,
            attempt=f"{attempt}/5",
            retry=attempt < 5,
        )
    else:
        emit("job.failed", logging.WARNING, **fields, error_type=type(error).__name__)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "time": datetime.fromtimestamp(
                    record.created, timezone.utc
                ).isoformat(),
                "level": record.levelname,
                "event": getattr(record, "event", record.getMessage()),
                **getattr(record, "fields", {}),
            }
        )


def configure_logging(level="INFO", output_format="text"):
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, or ERROR")
    if output_format not in {"text", "json"}:
        raise ValueError("LOG_FORMAT must be text or json")
    handler = logging.StreamHandler(sys.stderr)
    formatter = (
        JsonFormatter()
        if output_format == "json"
        else logging.Formatter(
            "%(asctime)sZ %(levelname)-7s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
        )
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    log.handlers[:] = [handler]
    log.setLevel(level)
    log.propagate = False
    # Never enable request/response logging when application DEBUG is requested.
    for name in ("httpx", "httpcore", "urllib3", "sqlalchemy.engine", "websockets"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def emit(name, level=logging.INFO, **fields):
    # Call sites supply only fixed messages and non-secret metadata. Never pass exceptions,
    # request/response bodies, configuration objects, SQL, or credentials here.
    fields = {k: v for k, v in fields.items() if v is not None}
    log.log(
        level,
        "%s%s",
        name,
        " " + " ".join(f"{k}={v}" for k, v in fields.items()) if fields else "",
        extra={"event": name, "fields": fields},
    )


def actor_ref(owner):
    return hashlib.sha256(owner.encode()).hexdigest()[:12]


def job_fields(job):
    fields = {"job_id": job.get("id"), "kind": job["kind"]}
    if job.get("owner"):
        fields["actor"] = actor_ref(job["owner"])
    action = job.get("payload", {}).get("action")
    if action in {"connect", "poll", "switch", "account", "role"}:
        fields["action"] = action
    return fields
