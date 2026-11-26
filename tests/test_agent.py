import base64
import json

import httpx

from larkagentd.config import Config
from larkagentd.gateway.hermes import HermesAgent


def done(text="hi", **flags):
    event = dict(
        event="run.completed",
        output=text,
        completed=True,
        interrupted=False,
        partial=False,
    )
    return {**event, **flags}


class Runs:
    """A small Hermes runs API: create a run, stream its events, read its status."""

    def __init__(self, output="hi", events=None, create=None, stream=None, statuses=()):
        self.events = events if events is not None else [done(output)]
        self.create, self.stream, self.statuses = create, stream, list(statuses)
        self.output, self.requests = output, []
        self.stop_response = None

    def __call__(self, request):
        self.requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/stop"):
            return self.stop_response or httpx.Response(
                200, json={"status": "stopping"}
            )
        if request.method == "POST":
            if isinstance(self.create, Exception):
                raise self.create
            return self.create or httpx.Response(
                202, json={"run_id": "run-1", "status": "started"}
            )
        if request.url.path.endswith("/events"):
            if isinstance(self.stream, Exception):
                raise self.stream
            if self.stream is not None:
                return self.stream
            body = "".join(
                f": keepalive\n\ndata: {json.dumps(e)}\n\n" for e in self.events
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body.encode(),
            )
        status = (
            self.statuses.pop(0)
            if len(self.statuses) > 1
            else self.statuses[0]
            if self.statuses
            else {"status": "completed", "output": self.output}
        )
        return httpx.Response(200, json=status)


def adapter(handler, deadline=5):
    return HermesAgent(
        "http://hermes.test:8642",
        "test-api-key",
        deadline=deadline,
        transport=httpx.MockTransport(handler),
        poll=0.01,
    )


class FakeAgent:
    deadline = 30
    model = "hermes-agent"
    google_email = None

    def __init__(self, reply="ok", script=(), events=()):
        self.reply, self.script, self.calls = reply, list(script), []
        self.events = list(events)
        self.requests, self.images = [], []  # what build() and send() were given
        self.forgotten, self.forget_result = [], True
        self.tokens, self.order = None, []  # what usage() reports; the order of calls
        self.hook = None  # called with the session id, as a tool would during a turn
        self.stopped, self.stop_result = [], True

    def build(self, request):
        self.requests.append(request)
        contents = [m["content"] for m in request["history"]]
        return dict(messages=contents + [request["message"]["content"]])

    def send(self, body, session_id, on_event=None, on_run=None, images=None):
        self.images.append(images)
        self.calls.append((session_id, body))
        if on_run:
            on_run("run-" + session_id)
        if self.hook:
            self.hook(session_id)
        for event in self.events:
            if on_event:
                on_event(*event)
        step = self.script.pop(0) if self.script else self.reply
        if isinstance(step, Exception):
            raise step
        return step

    def stop(self, run_id):
        self.stopped.append(run_id)
        return self.stop_result

    def forget(self, session_id):
        self.order.append(("forget", session_id))
        self.forgotten.append(session_id)
        if isinstance(self.forget_result, Exception):
            raise self.forget_result
        return self.forget_result

    def usage(self, session_id):
        self.order.append(("usage", session_id))
        if isinstance(self.tokens, Exception):
            raise self.tokens
        return self.tokens


BASE_ENV = dict(
    DASHBOARD_ADMIN_EMAILS="admin@example.com",
    AGENT_URL="http://hermes:8642",
    DATABASE_URL="postgresql+psycopg://user@db.example.com/app",
    LARK_APP_ID="app-id",
    LARK_APP_SECRET="app-secret",
    CREDENTIAL_ENCRYPTION_KEY=base64.b64encode(b"k" * 32).decode(),
)


def load(monkeypatch, **extra):
    for name in (
        "AGENT_URL",
        "AGENT_API_KEY",
        "AGENT_MODEL",
        "AGENT_MAX_CONCURRENT",
        "AGENT_DEADLINE_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**BASE_ENV, **extra}.items():
        monkeypatch.setenv(name, value)
    return Config.load()
