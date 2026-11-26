"""Hermes plugin: forwards the model's tool calls to the larkagentd tool service.

Who is asking comes from the Gateway turn bound to the Hermes session id, never from
model arguments. Standard library only, because this runs inside
Hermes's own Python environment.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOOLS = json.loads(Path(__file__).with_name("tools.json").read_text())
ERROR_CODE = re.compile(r"[a-z][a-z_]{0,39}")
PICTURE = re.compile(r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+\Z")
MAX_RESPONSE = 128 * 1024
# `lark_image` answers with pictures (data URLs) beside its result: they get their own, larger limit.
MAX_PICTURE_RESPONSE = 14 * 1024 * 1024
MAX_PICTURES = 3
# A query can run for minutes; every other tool answers within seconds (TOOL_SERVICE_TIMEOUT).
IMAGE_TIMEOUT = 150
# Internal service: an environment proxy must never sit in the middle.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
log = logging.getLogger(__name__)


def call(name, params, session_id, timeout=None, limit=MAX_RESPONSE):
    url = os.environ.get("TOOL_SERVICE_URL", "").rstrip("/")
    key = os.environ.get("TOOL_SERVICE_KEY", "")
    if not (url and key and session_id):
        return {"ok": False, "error": "tool_unavailable"}
    request = urllib.request.Request(
        f"{url}/v1/tools/{name}",
        data=json.dumps({"session_id": session_id, "arguments": params or {}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    timeout = (
        float(os.environ.get("TOOL_SERVICE_TIMEOUT", "90"))
        if timeout is None
        else timeout
    )
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return json.loads(response.read(limit))
    except urllib.error.HTTPError as exc:
        # Pass on the service's short error code, never its body.
        try:
            code = json.loads(exc.read(4096)).get("error", "")
        except Exception:
            code = ""
        if isinstance(code, str) and ERROR_CODE.fullmatch(code):
            return {"ok": False, "error": code}
        return {"ok": False, "error": "tool_unavailable"}
    except Exception:
        return {"ok": False, "error": "tool_unavailable"}


def discover_tools():
    url = os.getenv("TOOL_SERVICE_URL", "").rstrip("/")
    key = os.getenv("TOOL_SERVICE_KEY", "")
    if not url or not key:
        return TOOLS
    request = urllib.request.Request(
        f"{url}/v1/specs", headers={"Authorization": "Bearer " + key}
    )
    try:
        with OPENER.open(request, timeout=10) as response:
            specs = json.loads(response.read(MAX_RESPONSE + 1))
        if not isinstance(specs, list):
            raise ValueError
        return specs
    except (OSError, ValueError):
        log.warning("Tool discovery unavailable; using bundled core tools")
        return TOOLS


def with_pictures(outcome):
    """A tool result and the pictures that came with it, as Hermes's multimodal tool result: the model
    sees the pictures, and a model or provider that cannot gets the text only. Anything that is not a
    PNG, JPEG, GIF or WebP data URL is dropped."""
    pictures = outcome.pop("data_urls", None)
    text = json.dumps(outcome)
    urls = [
        url
        for url in (pictures if isinstance(pictures, list) else [])
        if isinstance(url, str) and PICTURE.match(url)
    ][:MAX_PICTURES]
    if not urls:
        return text
    return {
        "_multimodal": True,
        "content": [{"type": "text", "text": text}]
        + [{"type": "image_url", "image_url": {"url": url}} for url in urls],
        "text_summary": text,
    }


def make_handler(name, timeout=None):
    def handler(params, *, session_id="", **_):
        outcome = call(
            name,
            params,
            session_id,
            timeout=IMAGE_TIMEOUT if name == "lark_image" else timeout,
            limit=MAX_PICTURE_RESPONSE if name == "lark_image" else MAX_RESPONSE,
        )
        if name == "lark_image" and isinstance(outcome, dict):
            return with_pictures(outcome)
        return json.dumps(outcome)

    return handler


# --- the skill index, put in front of the model at the start of every turn -----------------
# Hermes's own agent lists its skills in the context up front. Ours are served by the tool
# service, so without this the model only sees a skill when it thinks to ask for the list, and
# a model that does not (it went straight to the knowledge base) never uses one. The hook adds
# each skill's name and description to the user message, never the system prompt, and fails
# open: with no service, no skills or any error the turn runs without it.

INDEX_TIMEOUT = 5
MAX_INDEX = 6000
INDEX_INTRO = (
    "Skills available to you, as name: description. If one fits the task, read it with "
    "read_skill before you answer, whether or not the user names it, and follow it instead "
    "of improvising. Never use the knowledge base in place of a matching skill. "
    "When document content is already supplied, review it directly without loading CLI fetch instructions. "
    "Skills describe workflows and do not grant tools or credentials. "
    "If a tool is refused or unavailable, do not retry it "
    "or claim its command ran."
)


def skill_index(session_id):
    """The skill list as one block of text, or "" when it cannot be built."""
    outcome = call("list_skills", {}, session_id, timeout=INDEX_TIMEOUT)
    result = outcome.get("result") if outcome.get("ok") else None
    if not isinstance(result, dict):
        return ""
    lines = []
    for item in result.get("global") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        description = item.get("description")
        line = f"- {item['name']}"
        if isinstance(description, str) and description:
            line += f": {description}"
        lines.append(" ".join(line.replace("<", " ").replace(">", " ").split()))
    if not lines:
        return ""
    body = "\n".join(lines)[:MAX_INDEX]
    return f"<available_skills>\n{INDEX_INTRO}\n{body}\n</available_skills>"


def pre_llm_call(*, session_id="", **_):
    try:
        text = skill_index(session_id)
    except Exception:
        return None
    return {"context": text} if text else None


def register(ctx):
    for tool in discover_tools():
        ctx.register_tool(
            name=tool["name"],
            toolset="larkagentd",
            schema={k: v for k, v in tool.items() if k != "timeout"},
            handler=make_handler(tool["name"], tool.get("timeout")),
        )
    ctx.register_hook("pre_llm_call", pre_llm_call)
