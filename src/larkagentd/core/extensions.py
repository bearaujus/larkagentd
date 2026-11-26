"""Load only the explicitly configured deployment extension."""

import os
from importlib.metadata import entry_points
from typing import Protocol, TypedDict


class Choice(TypedDict):
    id: str
    label: str


class AccountProvider(Protocol):
    """Provider-owned credentials carry expires (epoch seconds) and expiration (display time)."""

    def accounts(self, token: str) -> list[Choice]: ...
    def roles(self, token: str, account: str) -> list[Choice]: ...
    def credentials(self, token: str, account: str, role: str) -> dict: ...
    def identify(self, credentials: dict) -> dict: ...
    def start(self) -> dict: ...
    def poll(self, attempt: dict) -> dict: ...


class Extension(Protocol):
    def authentication(self) -> AccountProvider | None: ...
    def tools(self, store) -> dict: ...


def load_extension() -> Extension | None:
    name = os.getenv("LARKAGENTD_EXTENSION", "").strip()
    if not name:
        return None
    candidates = entry_points(group="larkagentd.extensions", name=name)
    if len(candidates) != 1:
        raise ValueError("LARKAGENTD_EXTENSION must identify one installed extension")
    (entry_point,) = candidates
    return entry_point.load()()
