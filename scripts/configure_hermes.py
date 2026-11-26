"""Prepare ignored Hermes config from non-secret model settings. Never print values."""

import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yaml


def merge(base, override):
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def configure(root, env):
    root = Path(root)
    reference = yaml.safe_load((root / "deploy/hermes/config.yaml").read_text())
    destination = root / "hermes_data/config.yaml"
    previous = yaml.safe_load(destination.read_text()) if destination.exists() else {}
    if not isinstance(previous, dict):
        raise ValueError("Hermes config.yaml must contain a mapping")
    config = merge(reference, {"model": previous.get("model", {})})
    model = config.setdefault("model", {})
    if not isinstance(model, dict):
        raise ValueError("Hermes model settings must contain a mapping")
    if env.get("HERMES_PROVIDER"):
        model["provider"] = env["HERMES_PROVIDER"]
    if env.get("HERMES_MODEL"):
        model["default"] = env["HERMES_MODEL"]
    if not model.get("default"):
        raise ValueError("Set HERMES_MODEL in .env before make run")
    if env.get("HERMES_OPENAI_ENDPOINT"):
        endpoint = env["HERMES_OPENAI_ENDPOINT"]
        parsed = urlparse(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("HERMES_OPENAI_ENDPOINT must be a plain http(s) API URL")
        model["base_url"] = endpoint
    # The app plugin replaces shared-state tools. Keep it enabled for the API route.
    plugins = config.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list):
        raise ValueError("Hermes plugins.enabled must be a list")
    if "larkagentd_tools" not in enabled:
        enabled.append("larkagentd_tools")
    config.pop("mcp_servers", None)
    plugins["enabled"] = ["larkagentd_tools"]
    config.setdefault("platform_toolsets", {})["api_server"] = reference[
        "platform_toolsets"
    ]["api_server"]
    terminal = config.setdefault("terminal", {})
    if not isinstance(terminal, dict):
        raise ValueError("Hermes terminal settings must contain a mapping")
    terminal["backend"] = reference["terminal"]["backend"]
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix="config-", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as output:
            yaml.safe_dump(config, output, sort_keys=False)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def main():
    try:
        configure(Path.cwd(), os.environ)
    except (OSError, ValueError, yaml.YAMLError):
        # Never echo YAML exception text: an existing config may contain secrets.
        print(
            "Hermes config could not be prepared. Check HERMES_MODEL, HERMES_OPENAI_ENDPOINT and hermes_data/config.yaml.",
            file=sys.stderr,
        )
        return 1
    print("Hermes config ready: model settings applied; no secrets printed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
