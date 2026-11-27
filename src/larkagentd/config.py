import base64
import ipaddress
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .core.database import database_url
from .core.diagnostics import emit
from .core.fields import LARK_APP_ID
from .core.googlefeedback import configured_email
from .gateway.access import admin_emails, public_origin
from .knowledge.models import GroupBrainConfig


@dataclass(frozen=True)
class Config:
    database_url: str
    app_id: str
    app_secret: str
    encryption_key: bytes
    history_messages: int = 20
    health_port: int = 8080
    status_interval: int = 60
    agent_url: str = ""
    agent_api_key: str = ""
    agent_model: str = "hermes-agent"
    agent_capacity: int = 5
    agent_deadline: int = 3600
    # Messages sent within this many seconds of each other are answered as one request.
    message_window: float = 1.5
    dashboard_admin_emails: tuple = ()
    dashboard_public_url: str = "http://localhost:18080"
    dashboard_additional_origins: tuple[str, ...] = ()
    # Tokens one person may use in a UTC calendar month (input including cache, plus output);
    # 0 means unlimited.
    user_token_quota: int = 0
    # A line under each answer with the tokens the request used (TOKEN_SUMMARY, default true).
    token_summary: bool = True
    # An animated image (a Lark image key) shown in the status line while a turn runs, in place of
    # the activity card when it opens (LOADING_IMAGE_KEY).
    loading_image_key: str | None = None
    # Models a person may pick for one thread in a direct chat (/model). Empty: none.
    available_models: tuple = ()
    # Models that can read images (VISION_MODELS) and the model Hermes uses when a person has not picked
    # one (HERMES_MODEL). With a list, a model that is not on it never receives an image and the person
    # is told. With none, whether the model reads images is unknown: images are sent, and a turn that
    # fails with one says so.
    vision_models: tuple = ()
    model_reasoning_efforts: dict = field(default_factory=dict)
    provider_model: str = ""
    google_service_account_email: str | None = field(default=None, repr=False)
    # Group Brain: the global switch and limits. Per-group settings are in the database.
    group_brain: GroupBrainConfig = field(default_factory=GroupBrainConfig)
    lark_allowed_senders: tuple[str, ...] = ("user",)

    @classmethod
    def load(cls):
        db, key = _shared(("LARK_APP_ID", "LARK_APP_SECRET", "AGENT_URL"))
        try:
            history = int(os.getenv("HISTORY_MESSAGES", "20"))
            health_port = int(os.getenv("HEALTH_PORT", "8080"))
            status_interval = int(os.getenv("LOG_STATUS_INTERVAL", "60"))
        except ValueError:
            raise ValueError(
                "HISTORY_MESSAGES, HEALTH_PORT, and LOG_STATUS_INTERVAL must be integers"
            ) from None
        if not 1 <= health_port <= 65535:
            raise ValueError("HEALTH_PORT must be between 1 and 65535")
        if status_interval != 0 and status_interval < 10:
            raise ValueError("LOG_STATUS_INTERVAL must be 0 or at least 10 seconds")
        if not 1 <= history <= 200:
            raise ValueError("HISTORY_MESSAGES must be between 1 and 200")
        try:
            window = float(os.getenv("MESSAGE_BATCH_SECONDS", "1.5"))
        except ValueError:
            raise ValueError("MESSAGE_BATCH_SECONDS must be a number") from None
        if not 0 <= window <= 10:
            raise ValueError("MESSAGE_BATCH_SECONDS must be between 0 and 10")
        agent = _agent_settings()
        available = _model_names("AVAILABLE_MODELS")
        provider = next(iter(_model_names("HERMES_MODEL")), "")
        return cls(
            db,
            os.environ["LARK_APP_ID"],
            os.environ["LARK_APP_SECRET"],
            key,
            history,
            health_port,
            status_interval,
            *agent,
            message_window=window,
            dashboard_admin_emails=admin_emails(
                os.getenv("DASHBOARD_ADMIN_EMAILS", "")
            ),
            dashboard_public_url=public_origin(
                os.getenv("DASHBOARD_PUBLIC_URL", "http://localhost:18080")
            ),
            dashboard_additional_origins=tuple(
                public_origin(value.strip(), "DASHBOARD_ADDITIONAL_ORIGINS")
                for value in os.getenv("DASHBOARD_ADDITIONAL_ORIGINS", "").split(",")
                if value.strip()
            ),
            user_token_quota=_token_quota(),
            token_summary=_flag("TOKEN_SUMMARY", "true"),
            loading_image_key=_image_key("LOADING_IMAGE_KEY"),
            available_models=available,
            model_reasoning_efforts=_model_reasoning_efforts(
                {provider, *available} - {""}
            ),
            vision_models=_model_names("VISION_MODELS"),
            provider_model=provider,
            google_service_account_email=configured_email(
                os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
            ),
            group_brain=_group_brain_settings(),
            lark_allowed_senders=_lark_senders(os.environ["LARK_APP_ID"]),
        )


def _lark_senders(app_id):
    entries = (
        entry.strip() for entry in os.getenv("LARK_ALLOWED_SENDERS", "").split(",")
    )
    senders = tuple(dict.fromkeys(entry for entry in entries if entry))
    if any(entry != "user" and not LARK_APP_ID.fullmatch(entry) for entry in senders):
        raise ValueError("LARK_ALLOWED_SENDERS must contain user or exact Lark app IDs")
    if app_id in senders:
        raise ValueError("LARK_ALLOWED_SENDERS cannot contain the receiving app")
    return senders or ("user",)


def _token_quota():
    """Tokens one person may use per month. Unset or 0 means unlimited."""
    try:
        quota = int((os.getenv("USER_TOKEN_QUOTA") or "0").strip())
    except ValueError:
        quota = -1
    if quota < 0:
        raise ValueError("USER_TOKEN_QUOTA must be a whole number, 0 or more")
    return quota


MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")


def _model_reasoning_efforts(models):
    """Operator-declared values verified against this deployment's Hermes and provider."""
    known = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
    try:
        data = json.loads(os.getenv("MODEL_REASONING_EFFORTS") or "{}")
        if not isinstance(data, dict) or any(
            model not in models
            or not isinstance(values, list)
            or any(value not in known for value in values)
            for model, values in data.items()
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            "MODEL_REASONING_EFFORTS must map configured models to supported effort values."
        ) from None
    return {
        model: tuple(value for value in known if value in values)
        for model, values in data.items()
    }


def _model_names(name):
    """A comma-separated list of model names, blanks and repeats dropped. A name that is not
    plain letters, digits and `._:/-` stops startup: it goes to the model provider as it is."""
    items = (item.strip() for item in (os.getenv(name) or "").split(","))
    names = tuple(dict.fromkeys(item for item in items if item))
    for item in names:
        if not MODEL_NAME.fullmatch(item) or item.lower() == "default":
            raise ValueError(f"{name} holds a name that is not allowed")
    return names


IMAGE_KEY = re.compile(r"img_v\d+_[A-Za-z0-9_-]{4,120}")


def _image_key(name):
    """A Lark image key, or None when the setting is empty; anything else stops startup."""
    value = (os.getenv(name) or "").strip()
    if not value:
        return None
    if not IMAGE_KEY.fullmatch(value):
        raise ValueError(f"{name} must be a Lark image key such as img_v3_...")
    return value


def _flag(name, default="false"):
    """A true or false setting, `default` when unset; anything else stops startup."""
    value = (os.getenv(name) or default).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


LOCAL_AGENT_API_KEY = "larkagentd-local-development"


def _local_agent_host(host):
    if host in {"localhost", "hermes"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _agent_settings():
    url = os.getenv("AGENT_URL", "").rstrip("/")
    key = os.getenv("AGENT_API_KEY", "")
    model = os.getenv("AGENT_MODEL", "hermes-agent")
    try:
        capacity = int(os.getenv("AGENT_MAX_CONCURRENT", "5"))
        deadline = int(os.getenv("AGENT_DEADLINE_SECONDS", "3600"))
    except ValueError:
        raise ValueError(
            "AGENT_MAX_CONCURRENT and AGENT_DEADLINE_SECONDS must be integers"
        ) from None
    if not 1 <= capacity <= 64:
        raise ValueError("AGENT_MAX_CONCURRENT must be between 1 and 64")
    if not 10 <= deadline <= 14_400:
        raise ValueError("AGENT_DEADLINE_SECONDS must be between 10 and 14400")
    if url:
        try:
            parsed = urlparse(url)
            parsed.port
        except ValueError:
            raise ValueError("AGENT_URL must be a valid http(s) URL") from None
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("AGENT_URL must be a plain http(s) origin with no path")
        local = _local_agent_host(parsed.hostname)
        if not key and local:
            key = LOCAL_AGENT_API_KEY
        if len(key) < 8 or (key == LOCAL_AGENT_API_KEY and not local):
            raise ValueError(
                "AGENT_API_KEY (8+ characters) is required for remote AGENT_URL; local development may leave it empty"
            )
        if key == LOCAL_AGENT_API_KEY:
            emit("agent.development_key", logging.WARNING, scope="local_only")
    return url, key, model, capacity, deadline


def _shared(extra=()):
    """Validate the settings every service needs; returns (database_url, key)."""
    names = [
        "DATABASE_URL",
        *extra,
    ]
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise ValueError("Missing settings: " + ", ".join(missing))
    value = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")
    if not value:
        raise ValueError(
            "Set CREDENTIAL_ENCRYPTION_KEY to a base64-encoded 32-byte key"
        )
    try:
        key = base64.b64decode(value, validate=True)
    except ValueError:
        raise ValueError("CREDENTIAL_ENCRYPTION_KEY must be base64") from None
    if len(key) != 32:
        raise ValueError("CREDENTIAL_ENCRYPTION_KEY must decode to 32 bytes")
    db = database_url(os.getenv("DATABASE_URL", ""))
    return db, key


def _bounded_int(name, default, low, high):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


GROUP_BRAIN_COLLECTION = re.compile(r"[a-z][a-z0-9_]{0,62}")


def _http_url(name):
    """An empty string, or an http(s) URL with a host and nothing a request could hide in
    (no credentials, query or fragment). The message never repeats the value."""
    value = (os.getenv(name) or "").strip()
    if not value:
        return ""
    parts = urlparse(value)
    # The key and the text travel in this request: TLS, except to this machine.
    loopback = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if (
        parts.scheme not in ("http", "https")
        or (parts.scheme == "http" and not loopback)
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            f"{name} must be an https URL (http only for this machine) without credentials or query"
        )
    return value.rstrip("/")


def _group_brain_settings():
    """The global Group Brain settings (GROUP_BRAIN_*). Enabled by default.

    With the switch off nothing here is required. An embedding or model endpoint is all or nothing:
    a half-set endpoint stops startup, an unset one means that capability is unavailable (recall is
    then lexical and live only). Nothing is inferred from the Hermes endpoint."""
    enabled = _flag("GROUP_BRAIN_ENABLED", "true")
    provider = (os.getenv("GROUP_BRAIN_PROVIDER") or "mem0_oss").strip()
    if provider != "mem0_oss":
        raise ValueError("GROUP_BRAIN_PROVIDER must be mem0_oss")
    collection = (os.getenv("GROUP_BRAIN_COLLECTION") or "group_brain_v1").strip()
    if not GROUP_BRAIN_COLLECTION.fullmatch(collection):
        raise ValueError(
            "GROUP_BRAIN_COLLECTION must be lowercase letters, digits and _"
        )
    values = {}
    for kind in ("EMBEDDING", "LLM"):
        url = _http_url(f"GROUP_BRAIN_{kind}_BASE_URL")
        key = (os.getenv(f"GROUP_BRAIN_{kind}_API_KEY") or "").strip()
        if bool(url) != bool(key):
            raise ValueError(
                f"GROUP_BRAIN_{kind}_BASE_URL and GROUP_BRAIN_{kind}_API_KEY are needed together"
            )
        values[kind] = (url, key)
    models = {}
    for name, default in (
        ("GROUP_BRAIN_EMBEDDING_MODEL", "text-embedding-v4"),
        ("GROUP_BRAIN_LLM_MODEL", "qwen3.8-flash"),
    ):
        models[name] = (os.getenv(name) or default).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", models[name]):
            raise ValueError(f"{name} holds a name that is not allowed")
    chunk_tokens = _bounded_int("GROUP_BRAIN_CHUNK_TOKENS", 512, 64, 4096)
    overlap = _bounded_int("GROUP_BRAIN_CHUNK_OVERLAP", 64, 0, 1024)
    if overlap >= chunk_tokens:
        raise ValueError(
            "GROUP_BRAIN_CHUNK_OVERLAP must be smaller than the chunk size"
        )
    return GroupBrainConfig(
        enabled=enabled,
        provider=provider,
        embedding_base_url=values["EMBEDDING"][0],
        embedding_api_key=values["EMBEDDING"][1],
        embedding_model=models["GROUP_BRAIN_EMBEDDING_MODEL"],
        embedding_dimensions=_bounded_int(
            "GROUP_BRAIN_EMBEDDING_DIMENSIONS", 1024, 8, 4096
        ),
        llm_base_url=values["LLM"][0],
        llm_api_key=values["LLM"][1],
        llm_model=models["GROUP_BRAIN_LLM_MODEL"],
        collection=collection,
        worker_concurrency=_bounded_int("GROUP_BRAIN_WORKER_CONCURRENCY", 2, 1, 8),
        provider_timeout_seconds=_bounded_int(
            "GROUP_BRAIN_PROVIDER_TIMEOUT_SECONDS", 15, 1, 120
        ),
        job_max_attempts=_bounded_int("GROUP_BRAIN_JOB_MAX_ATTEMPTS", 4, 1, 10),
        max_history_days=_bounded_int("GROUP_BRAIN_MAX_HISTORY_DAYS", 30, 1, 90),
        max_backfill_messages=_bounded_int(
            "GROUP_BRAIN_MAX_BACKFILL_MESSAGES", 5000, 100, 50_000
        ),
        max_docs_per_group=_bounded_int("GROUP_BRAIN_MAX_DOCS_PER_GROUP", 100, 1, 1000),
        max_document_tokens=_bounded_int(
            "GROUP_BRAIN_MAX_DOCUMENT_TOKENS", 100_000, 1000, 1_000_000
        ),
        chunk_tokens=chunk_tokens,
        chunk_overlap=overlap,
        max_candidates=_bounded_int("GROUP_BRAIN_MAX_CANDIDATES", 100, 10, 1000),
        top_k=_bounded_int("GROUP_BRAIN_TOP_K", 5, 1, 10),
        raw_buffer_hours=_bounded_int("GROUP_BRAIN_RAW_BUFFER_HOURS", 24, 1, 168),
        source_retention_days=_bounded_int(
            "GROUP_BRAIN_SOURCE_RETENTION_DAYS", 30, 1, 365
        ),
        memory_review_days=_bounded_int("GROUP_BRAIN_MEMORY_REVIEW_DAYS", 90, 1, 730),
    )


def _gitlab_settings():
    """(url, token) for the read-only GitLab tools, or (None, None) while both are unset.
    The token is a `read_api` token held only by the tool service. It is not GITLAB_TOKEN, which the
    provisioner uses and the long-running service never holds. A malformed value stops
    startup without repeating it."""
    url = (os.getenv("GITLAB_READ_URL") or "").strip().rstrip("/")
    token = (os.getenv("GITLAB_READ_TOKEN") or "").strip()
    if not url and not token:
        return None, None
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("GITLAB_READ_URL must be a plain https origin")
    if len(token) < 10:
        raise ValueError("GITLAB_READ_TOKEN is required with GITLAB_READ_URL")
    return url, token


def lark_read_settings(toolkit):
    """(command, app id, app secret, brand, home) for the read-only Lark tool, or Nones while
    the app id and secret are unset. The app can be the Gateway's or, better, a separate one with
    read scopes only. `brand` is `lark` (international) or `feishu`."""
    app_id = (os.getenv("LARK_READ_APP_ID") or "").strip()
    secret = (os.getenv("LARK_READ_APP_SECRET") or "").strip()
    if not app_id and not secret:
        return None, None, None, None, None
    if not app_id or not secret:
        raise ValueError(
            "LARK_READ_APP_ID and LARK_READ_APP_SECRET are needed together"
        )
    brand = (os.getenv("LARK_READ_BRAND") or "lark").strip().lower()
    if brand not in ("lark", "feishu"):
        raise ValueError("LARK_READ_BRAND must be lark or feishu")
    command = (os.getenv("LARK_CLI_BIN") or str(toolkit / "bin" / "lark-cli")).strip()
    default_home = os.path.join(
        tempfile.gettempdir(), f"larkagentd-lark-read-{os.getuid()}"
    )
    home = (os.getenv("LARK_READ_HOME") or default_home).strip()
    for name, value in (("LARK_CLI_BIN", command), ("LARK_READ_HOME", home)):
        if not re.fullmatch(r"/[\w./+-]{1,300}", value):
            raise ValueError(f"{name} must be an absolute path")
    return (command,), app_id, secret, brand, home


@dataclass(frozen=True)
class ToolConfig:
    """Server-owned tool settings, including optional read-only Lark app credentials."""

    database_url: str
    encryption_key: bytes
    service_key: str
    host: str = "127.0.0.1"
    port: int = 8081
    skill_roots: tuple = ()
    # Read-only GitLab: the origin and a token that never appears in a repr or a log.
    gitlab_url: str | None = None
    gitlab_token: str | None = field(default=None, repr=False)
    # Read-only Lark as the bot: the lark-cli command, the app, its brand and a private home.
    lark_command: tuple = ()
    lark_app_id: str | None = None
    lark_app_secret: str | None = field(default=None, repr=False)
    lark_brand: str | None = None
    lark_home: str | None = None
    # Whether `lark_read` may open links found inside a document it read.
    lark_follow_links: bool = False
    # Group Brain: the same global settings the Gateway reads.
    group_brain: GroupBrainConfig = field(default_factory=GroupBrainConfig)
    google_service_account_json: str | None = field(default=None, repr=False)

    @classmethod
    def load(cls):
        db, key = _shared(("TOOL_SERVICE_KEY",))
        service_key = os.environ["TOOL_SERVICE_KEY"]
        if len(service_key) < 16:
            raise ValueError("TOOL_SERVICE_KEY must be at least 16 characters")
        try:
            port = int(os.getenv("TOOL_SERVICE_PORT", "8081"))
        except ValueError:
            raise ValueError("TOOL_SERVICE_PORT must be an integer") from None
        if not 1 <= port <= 65535:
            raise ValueError("TOOL_SERVICE_PORT must be between 1 and 65535")
        toolkit = Path(os.getenv("TOOLKIT_DIR", "/opt/toolkit"))
        skill_roots = (
            Path(__file__).parent / "skills" / "builtin",
            toolkit / "skills-global",
        )
        config = cls(
            db,
            key,
            service_key,
            os.getenv("TOOL_SERVICE_HOST", "127.0.0.1"),
            port,
            skill_roots=skill_roots,
            **dict(zip(("gitlab_url", "gitlab_token"), _gitlab_settings())),
            **dict(
                zip(
                    (
                        "lark_command",
                        "lark_app_id",
                        "lark_app_secret",
                        "lark_brand",
                        "lark_home",
                    ),
                    lark_read_settings(toolkit),
                )
            ),
            lark_follow_links=_flag("LARK_FOLLOW_LINKS", "false"),
            group_brain=_group_brain_settings(),
            google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
            or None,
        )
        if config.google_service_account_json:
            from .toolservice.google import credentials

            credentials(config.google_service_account_json)
        return config
