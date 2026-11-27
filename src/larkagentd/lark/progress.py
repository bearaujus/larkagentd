"""A single, best-effort activity message in the originating Lark thread."""

import logging
import time
import uuid

from ..core.diagnostics import emit
from .card import activity_card
from .ui_text import STATUS_STEPS

EDIT_GAP = 3.0
CLOCK_GAP = 20.0
LIVE_EDITS = 14  # reserve one edit for the terminal state
VISIBLE_STEPS = 6
QUIET_TOOLS = {"group_memory_propose", "list_skills", "read_skill"}


def elapsed(seconds):
    seconds = int(max(0, seconds))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


class Progress:
    def __init__(
        self,
        transport,
        chat,
        message_id,
        fields=None,
        *,
        key=None,
        previous=None,
        remember=None,
        prior_elapsed=0,
        lost=None,
        clock=time.monotonic,
    ):
        self.transport, self.chat, self.message_id = transport, chat, message_id
        self.fields = fields or {}
        self.key = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"lark-activity:{key or message_id}")
        )
        self.message = tuple(previous) if previous else None
        self.remember = remember
        self.lost = lost or (lambda: False)
        self.clock = clock
        self.began = self.last_edit = clock()
        self.prior_elapsed = prior_elapsed
        self.pending = []
        self.seen = set()
        self.tool_names = set()
        self.steps = {}
        self.edits = 0
        self.dirty = self.closed = self.edit_failed = self.redirected = self.retried = (
            False
        )

    def start(self):
        if self.message or self.lost():
            return
        card, text = self.render(self.began)
        try:
            self.message = self.transport.open_activity(
                self.chat, self.message_id, self.key, card, text
            )
            if self.message and self.remember:
                self.remember(self.message)
        except Exception as exc:
            self.log_failure("open", exc)

    def event(self, phase, tool=None, detail=None):
        if self.closed or self.lost() or tool in QUIET_TOOLS:
            return
        detail = detail or {}
        call = detail.get("call")
        if phase == "started" and (not call or call not in self.seen):
            if call:
                self.seen.add(call)
            self.tool_names.add(tool)
            label = STATUS_STEPS.get(tool, "Using a tool")
            stats = self.steps.pop(label, [0, 0, 0])
            stats[0] += 1
            stats[2] += 1
            self.steps[label] = stats
            self.pending.append((tool, call, label))
            self.dirty = True
        elif phase == "completed":
            for index, (running, started, label) in enumerate(self.pending):
                if running == tool and (not call or started == call):
                    self.pending.pop(index)
                    self.steps[label][1] += detail.get("error") is True
                    self.steps[label][2] -= 1
                    self.dirty = True
                    break
        elif phase == "restarted":
            self.retried = self.dirty = True
        elif phase == "steered":
            self.redirected = self.dirty = True
        elif phase != "tick":
            return
        now = self.clock()
        gap = now - self.last_edit
        if self.edits < LIVE_EDITS and (
            (self.dirty and gap >= EDIT_GAP) or gap >= CLOCK_GAP
        ):
            self.write(now)

    def render(self, now, outcome=None, failure=None):
        phase = {None: "Working", True: "Reply ready", False: "Ended early"}[outcome]
        if failure == "unknown_execution":
            phase = "Session interrupted"
        elif failure == "cancelled":
            phase = "Stopped"
        elif failure == "timeout":
            phase = "Timed out"
        count = sum(row[0] for row in self.steps.values())
        failed = sum(row[1] for row in self.steps.values())
        status = f"**{phase}** · {elapsed(self.prior_elapsed + now - self.began)}"
        if count:
            status += f" · {count} {'step' if count == 1 else 'steps'}"
        if failed:
            status += f" · {failed} {'issue' if failed == 1 else 'issues'}"
        lines = ["Session restarted"] if self.retried else []
        if self.redirected:
            lines.append("Request updated")
        if len(self.steps) > VISIBLE_STEPS:
            lines.append(f"{len(self.steps) - VISIBLE_STEPS} earlier activities")
        for label, (calls, errors, running) in list(self.steps.items())[
            -VISIBLE_STEPS:
        ]:
            repeat = f" · {calls} calls" if calls > 1 else ""
            if running:
                state = "Running" if outcome is None else "Result unavailable"
            elif 0 < errors < calls:
                state, repeat = f"{calls - errors} of {calls} succeeded", ""
            else:
                state = "Issue" if errors else "Done"
            lines.append(f"{label} · {state}{repeat}")
        text = "\n".join([status, *[f"- {line}" for line in lines]])
        return activity_card(status, lines, expanded=outcome is None), text

    def finish(self, ok=True, failure=None):
        if self.closed:
            return
        self.closed = True
        self.write(self.clock(), bool(ok), failure)

    def write(self, now, outcome=None, failure=None):
        if not self.message or self.edit_failed or self.lost():
            return
        card, text = self.render(now, outcome, failure)
        try:
            self.transport.update_activity(*self.message, card, text)
        except Exception as exc:
            self.edit_failed = True
            self.log_failure("update", exc)
        else:
            self.edits += 1
            self.last_edit = now
            self.dirty = False

    def log_failure(self, step, exc):
        emit(
            "progress.failed",
            logging.WARNING,
            step=step,
            error_type=type(exc).__name__,
            **self.fields,
        )
