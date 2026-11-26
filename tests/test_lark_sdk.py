import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_sdk_import_is_strict_and_retains_other_warning_visibility():
    script = """
import asyncio
import warnings
original = warnings.filters[:]
from larkagentd.lark.adapter import Lark
import lark_oapi.ws.client as client
assert not client.loop.is_closed()
assert warnings.filters == original
try:
    warnings.warn("unrelated warning", DeprecationWarning)
except DeprecationWarning:
    pass
else:
    raise AssertionError("warning hidden")
client.loop.close()
asyncio.set_event_loop(None)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-Werror::DeprecationWarning",
            "-c",
            script,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr


def test_sdk_loader_creates_missing_loop_and_preserves_existing_loop():
    from larkagentd.lark.sdk import load_lark

    errors = []

    def check():
        import asyncio

        try:
            asyncio.set_event_loop(None)
            load_lark()
            loop = asyncio.get_event_loop()
            assert not loop.is_closed()
            load_lark()
            assert asyncio.get_event_loop() is loop
            loop.close()
            asyncio.set_event_loop(None)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=check)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert not errors
