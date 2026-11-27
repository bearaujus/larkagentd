"""The tool registry: which tools exist, how risky each is, and who may call it.

The code owns what a tool *is*: its kind and its risk class, which no database value can change.
The database owns what an administrator decided: a tool's global `enabled` flag and explicit
per-principal grants (today `user:<owner id>`; a group principal can follow). The rules:

- A tool the registry does not know, a retired one and one without an executor are never allowed.
- An explicit grant for the caller decides, allow or deny, before anything else.
- Otherwise a tool whose risk may be open to everyone (read, own, costly) follows its `enabled`
  flag, and a tool whose risk is write, destructive or execute is allowed only by a grant.

Every new tool starts at the safest class the code gives it, so a tool added to the code without
an entry here is treated as `execute`: it needs a grant until someone classifies it.
"""

import logging
from dataclasses import dataclass

from ..core.diagnostics import emit

READ, OWN, COSTLY = "read", "own", "costly"
WRITE, DESTRUCTIVE, EXECUTE = "write", "destructive", "execute"
# Switched on for everyone by the code's default.
ON_BY_DEFAULT = frozenset((READ, OWN, "group_action"))
# May be switched on for everyone by an administrator. Anything else needs an explicit grant.
OPEN = ON_BY_DEFAULT | {COSTLY}

# name -> (kind, risk) of every tool that has an executor in the code.
CAPABILITIES = {
    "list_skills": ("skills", READ),
    "read_skill": ("skills", READ),
    "gitlab_mr": ("gitlab", READ),
    "gitlab_compare": ("gitlab", READ),
    "gitlab_path": ("gitlab", READ),
    "gitlab_search": ("gitlab", READ),
    "lark_read": ("lark", READ),
    "google_read": ("google", READ),
    "lark_image": ("lark", READ),
    "group_search_discussions": ("group_brain", READ),
    "group_get_discussion": ("group_brain", READ),
    "group_knowledge_search": ("group_brain", READ),
    "group_knowledge_sources": ("group_brain", READ),
    "group_memory_propose": ("group_brain", READ),
    "group_memory_correct": ("group_brain", READ),
    "group_document_propose": ("group_brain", READ),
    "lark_thread": ("lark", READ),
}


def first_sentence(text, limit=200):
    head = " ".join(str(text).split())
    cut = head.find(". ")
    head = head[: cut + 1] if cut != -1 else head
    return head if len(head) <= limit else head[: limit - 1].rstrip() + "\u2026"


def capability(tool):
    return CAPABILITIES.get(tool.name, (tool.kind, tool.risk))


def catalog(tools):
    """The registry rows the code implies, from the tools it has and the ones it plans."""
    entries = []
    for name, tool in tools.items():
        kind, risk = capability(tool)
        entries.append(
            dict(
                name=name,
                kind=kind,
                risk=risk,
                label=tool.label or name,
                authority=tool.authority,
                activity_kind=tool.activity_kind,
                description=first_sentence(tool.description),
                implemented=True,
                enabled=risk in ON_BY_DEFAULT,
            )
        )
    return entries


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: (
        str  # ok, unknown, not_implemented, disabled, needs_grant, denied, unavailable
    )


def decide(row, grants, principals):
    """The rules above, as a pure function. `principals` run from most to least specific."""
    if row is None or row["retired"]:
        return Decision(False, "unknown")
    if not row["implemented"]:
        return Decision(False, "not_implemented")
    for principal in principals:
        if principal in grants:
            return (
                Decision(True, "ok") if grants[principal] else Decision(False, "denied")
            )
    if row["risk"] not in OPEN:
        return Decision(False, "needs_grant")
    return Decision(True, "ok") if row["enabled"] else Decision(False, "disabled")


def user_principal(owner):
    return f"user:{owner}"


class ToolRegistry:
    def __init__(self, store):
        self.store = store

    def sync(self, tools):
        """Bring the registry up to date with the code. Safe to run at every start."""
        with self.store.engine.begin() as conn:
            added, updated, retired = self.store.sync_tool_registry(
                conn, catalog(tools)
            )
        emit(
            "tools.registry_synced",
            added=len(added),
            updated=len(updated),
            retired=len(retired),
        )
        return added, updated, retired

    def decide(self, owner, name):
        """May this owner call this tool right now? A database failure is a refusal."""
        principals = (user_principal(owner),)
        try:
            with self.store.engine.connect() as conn:
                row = self.store.tool_row(conn, name)
                grants = self.store.tool_grants_for(conn, name, principals)
        except Exception:
            emit("tools.registry_failed", logging.ERROR, tool=name)
            return Decision(False, "unavailable")
        return decide(row, grants, principals)

    # --- what an administrator changes ---

    def set_enabled(self, name, enabled):
        with self.store.engine.begin() as conn:
            return self.store.set_tool_enabled(conn, name, enabled)

    def grant(self, name, owner, allowed=True, by=""):
        with self.store.engine.begin() as conn:
            if self.store.tool_row(conn, name) is None:
                return False
            self.store.set_tool_grant(conn, name, user_principal(owner), allowed, by)
        return True

    def revoke(self, name, owner):
        with self.store.engine.begin() as conn:
            return self.store.clear_tool_grant(conn, name, user_principal(owner))

    def listing(self):
        with self.store.engine.connect() as conn:
            return self.store.tool_listing(conn)
