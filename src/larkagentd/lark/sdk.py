"""Compatibility boundary for the pinned Lark SDK, without global warning suppression."""

import asyncio
import importlib
import warnings


def load_lark():
    # The SDK captures the current loop during import. Supply one explicitly when modern
    # Python refuses or deprecates implicitly creating a loop; retain an existing usable loop.
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        try:
            loop = asyncio.get_event_loop()
        except (RuntimeError, DeprecationWarning):
            loop = None
    if loop is None or loop.is_closed():
        asyncio.set_event_loop(asyncio.new_event_loop())
    with warnings.catch_warnings():
        # SDK 1.7.5 bundles an older protobuf implementation. Its import-time naive UTC
        # epoch constant uses utcfromtimestamp(0). Until upstream changes it, acknowledge
        # only that exact vendored deprecation; all other warnings and errors remain visible.
        warnings.filterwarnings(
            "ignore",
            message=r"datetime\.datetime\.utcfromtimestamp\(\) is deprecated.*",
            category=DeprecationWarning,
            module=r"lark_oapi\.ws\.pb\.google\.protobuf\.internal\.well_known_types",
        )
        return importlib.import_module("lark_oapi")


lark = load_lark()
