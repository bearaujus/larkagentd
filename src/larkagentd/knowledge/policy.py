"""The one rule for whether Group Brain may serve a group.

Effective state = global switch + registered group + group opt-in + lifecycle state + the notice and
policy the administrator recorded. Nothing here calls a provider or Lark: disabling takes effect with
the database commit. Every serving path (tools, dashboard counts, delivery) asks this module."""

from dataclasses import dataclass, field

from .models import CAPABILITIES
from .store import KnowledgeStore

store = KnowledgeStore()


@dataclass(frozen=True)
class Effective:
    configured_enabled: bool
    state: str  # disabled, enabled, degraded, paused, purging, setup_blocked
    reasons: tuple = ()
    capabilities: dict = field(default_factory=dict)
    generation: int = 0
    version: int = 0
    history_window_days: int = 30

    @property
    def serving(self):
        """True when this group may collect, index and search right now."""
        return self.state in ("enabled", "degraded")

    def allows(self, capability):
        return self.serving and bool(self.capabilities.get(capability))


def effective(conn, config, group_key):
    """The group's effective state. Fails closed: anything unknown is `disabled`."""
    settings = store.settings(conn, group_key)
    configured = bool(settings["enabled"])
    base = dict(
        configured_enabled=configured,
        generation=settings["generation"],
        version=settings["version"],
        history_window_days=min(
            settings["history_window_days"], config.max_history_days
        ),
    )
    off = {name: False for name in CAPABILITIES}
    if not config.enabled:
        return Effective(
            state="disabled", reasons=("global_disabled",), capabilities=off, **base
        )
    if not store.registered(conn, group_key):
        return Effective(
            state="disabled", reasons=("not_registered",), capabilities=off, **base
        )
    if settings["state"] == "purging":
        return Effective(
            state="purging", reasons=("purging",), capabilities=off, **base
        )
    if not configured or settings["state"] == "disabled":
        return Effective(
            state="disabled", reasons=("group_disabled",), capabilities=off, **base
        )
    if settings["state"] == "paused":
        return Effective(state="paused", reasons=("paused",), capabilities=off, **base)
    if not settings["notice_ref"] or not settings["policy_ref"]:
        return Effective(
            state="setup_blocked",
            reasons=("missing_group_notice",),
            capabilities=off,
            **base,
        )
    capabilities = {
        "recall": bool(settings["recall_enabled"]),
        "memory": bool(settings["memory_enabled"]),
        "documents": bool(settings["documents_enabled"]),
    }
    reasons = []
    if capabilities["documents"] and not config.embedding_configured:
        # Passive facts use lexical retrieval. Document indexing needs embeddings.
        reasons.append("embedding_unavailable")
    return Effective(
        state="degraded" if reasons else "enabled",
        reasons=tuple(reasons),
        capabilities=capabilities,
        **base,
    )


def still_valid(conn, config, group_key, generation, capability=None):
    """True only when the group still serves under the generation work started with. Every policy
    change (a capability switch included) raises the generation, so equality is enough; `capability`
    adds a check for one of them. Asked again just before anything built from group knowledge is
    delivered."""
    state = effective(conn, config, group_key)
    if capability is not None and not state.allows(capability):
        return False
    return state.serving and state.generation == generation
