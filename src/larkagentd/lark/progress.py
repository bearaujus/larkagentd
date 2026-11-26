"""Best-effort chat feedback while an agent turn runs, in the user's thread.

A Typing reaction on the user's message. The steps the agent takes collect in one progress message
that is edited as they come, as Hermes's own Lark gateway does: a line each with an emoji, what it
is doing and, for a few tools, the search query or the statement in a code block. A step that
repeats adds a count to its line, and a burst of steps is one edit. The agent's own sentence is a
message of its own, and the next step then starts a new progress message below it. One status line
shows the clock, the step count and what is running now while a long turn runs, and says Finished
or Stopped early at the end; a tool with no label is only counted there.
Everything stays in the thread after the answer (only the reaction goes), so the user can follow
and review how the agent worked. Nothing here may fail or delay a turn: every call is guarded,
and a path that fails is reported once and then skipped.

The fixed wording comes from ui_text. Only two kinds of text from the agent appear, both
redacted and cut short: a detail that hermes.py passes for a few tools (a skill name, a path, a
search query, the user's own statement) and the agent's own sentence between steps. Results
never appear here.
"""

import logging
import re
import time

from ..core.diagnostics import emit
from .ui_text import (
    PREVIEW_TOOLS,
    STATUS_CLOCK,
    STATUS_DONE,
    STATUS_STEPS,
    STATUS_STOPPED,
)

SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
MAX_DETAIL = 160  # characters of a name or a search query
MAX_NOTE = 800  # characters of one sentence of the agent
MAX_NOTES = 40  # sentences in one turn
MAX_STEPS = 80  # step lines in one turn; more steps still run and are counted
STATUS_AFTER = 45.0  # seconds before the status line appears
CLOCK_GAP = 60.0  # seconds between edits that only move the clock
GROUPED = frozenset(
    {"read_skill", "list_skills"}
)  # a repeat of these, wherever it was first listed, adds to that line's count
MAX_BUBBLE_LINES = 12  # lines in one progress message; the next step opens a new one
MAX_BUBBLE_CHARS = (
    3000  # characters in one progress message, a statement block included
)
EDIT_GAP = 1.5  # seconds between edits of one message: a burst of steps is one edit
MAX_EDITS = 15  # edits of one message; Lark limits how often a message can be edited


def squash(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def elapsed(seconds):
    seconds = int(seconds)
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


class Block:
    """One progress message in the thread and the lines it holds: [text, count] each."""

    def __init__(self, message, text, now):
        self.message, self.last = message, now
        self.lines = [[text, 1]]
        self.edits, self.stale = 0, False

    def render(self):
        # A hard line break (two spaces, then a newline) reads as a line break whether or not
        # Lark's markdown treats a bare newline as one.
        return "  \n".join(t if n == 1 else f"{t} {n}x" for t, n in self.lines)

    def room(self, text):
        """Whether one more line fits: under MAX_BUBBLE_LINES lines and MAX_BUBBLE_CHARS characters,
        the line break between the lines counted."""
        return (
            len(self.lines) < MAX_BUBBLE_LINES
            and len(self.render()) + len("  \n") + len(text) <= MAX_BUBBLE_CHARS
        )


class Progress:
    def __init__(
        self,
        transport,
        chat,
        message_id,
        fields=None,
        redact=None,
        private=False,
        lost=None,
        clock=time.monotonic,
        hold=None,
    ):
        self.transport, self.chat, self.message_id = transport, chat, message_id
        self.fields = fields or {}
        self.redact = redact or (lambda text: text)
        self.clock = clock
        self.began = clock()
        self.reaction = self.status = None
        self.steps = self.failed = self.notes = self.listed = 0
        self.last_edit = 0.0
        self.quiet = False
        # True once another worker holds this job (see main.keep_lease): that worker posts the
        # progress now, so this one must not post the same steps and sentences again.
        self.lost = lost or (lambda: False)
        # True while the agent's own sentences must not be posted: after a Group Brain tool returned
        # evidence, what the agent says is held with the answer and checked before it goes out.
        self.hold = hold or (lambda: False)
        # In a group the steps show a fixed label only, without the statement they run: that can
        # carry data the other people there may not see. The agent's own sentence before a step
        # is shown the same way in a group and in a direct chat.
        self.private = private
        self.notes_ok = self.steps_ok = self.clock_ok = True
        self.reported = set()
        self.holding = {}  # a skill step's text -> the Block that holds its line
        self.open = None  # the Block that is the last message in the thread, if any
        self.stale = []  # Blocks with lines that are not edited into their message yet
        self.merge_ok = True
        self.current = None  # what the agent is doing right now, for the status line

    def start(self):
        self.reaction = self.step(
            "react", lambda: self.transport.react(self.message_id)
        )

    def event(self, phase, tool=None, detail=None):
        """phase: started or completed (a tool step), said (the agent's sentence), tick."""
        if (
            self.quiet
            or self.lost()
            or phase not in ("started", "completed", "said", "tick")
        ):
            return
        detail = detail if isinstance(detail, dict) else {}
        self.flush()
        if phase == "said":
            self.say(detail.get("text"))
        elif phase == "started":
            self.steps += 1
            self.current = STATUS_STEPS[tool][1] if tool in STATUS_STEPS else None
            self.list_step(tool, detail)
        elif phase == "completed":
            self.current = None
            if detail.get("error") is True:
                self.failed += 1
        self.draw()

    def post(self, name, text):
        # Whatever was open is no longer the last message in the thread.
        self.open = None
        return self.step(
            name, lambda: self.transport.note(self.chat, text, self.message_id)
        )

    def say(self, text):
        """The agent's own sentence, as an ordinary message in the thread."""
        if not (isinstance(text, str) and text.strip()):
            return
        if not self.notes_ok or self.notes >= MAX_NOTES or self.hold():
            return
        if self.post("note", squash(self.redact(text), MAX_NOTE)) is None:
            self.notes_ok = False  # a failing path is not retried for every sentence
            return
        self.notes += 1

    def list_step(self, tool, detail):
        """The step as a line in the open progress message (see join), or as a new message. A tool
        with no label is not listed: it counts in the status line only."""
        if not self.steps_ok or tool not in STATUS_STEPS:
            return
        emoji, label = STATUS_STEPS[tool]
        text = f"{emoji} {label}"
        shown = self.detail(tool, detail)
        if shown:
            text += f": {shown}"
        if self.join(tool, text):
            return
        if self.listed >= MAX_STEPS:
            if self.listed == MAX_STEPS:
                self.listed += 1
                self.post("step", f"\u2026 more steps than I list ({MAX_STEPS} shown).")
            return
        posted = self.post("step", text)
        if posted is None:
            self.steps_ok = False
            return
        block = Block(posted, text, self.clock())
        self.open = block
        if tool in GROUPED:
            self.holding[text] = block
        self.listed += 1

    def join(self, tool, text):
        """True when the step went into a progress message that already exists.

        - A skill step (a skill read, the skill list) that was listed before, wherever, adds to
          the count on its line (`Reading skill: name 3x`).
        - Any other step identical to the line just before it adds to that line's count. A
          statement in a code block is never folded.
        - Otherwise it is a new line in the open message, until that message holds MAX_BUBBLE_LINES
          lines or MAX_BUBBLE_CHARS characters. A sentence of the agent closes the open message, so
          the next step starts a new one below it.

        The edit is made now, or at the next event once EDIT_GAP has passed (see touch). When an
        edit fails (Lark refuses it, or the message was edited too often) the step is listed as a
        message of its own, and no further edit is tried this turn: a refusal would otherwise be
        repeated on every step. A message that has reached MAX_EDITS keeps counting without being
        edited, and is written once more when the turn ends."""
        if not self.merge_ok:
            return False
        block = self.holding.get(text) if tool in GROUPED else None
        if block is not None:
            next(line for line in block.lines if line[0] == text)[1] += 1
            return self.touch(block)
        block = self.open
        if block is None or not block.room(text):
            return False
        if block.lines[-1][0] == text:
            block.lines[-1][1] += 1
        else:
            if self.listed >= MAX_STEPS:
                return False
            block.lines.append([text, 1])
            self.listed += 1
            if tool in GROUPED:
                self.holding[text] = block
        return self.touch(block)

    def touch(self, block):
        """Mark the block's message out of date and write it if it may be written now. False only
        when Lark refused the edit."""
        block.stale = True
        if block not in self.stale:
            self.stale.append(block)
        return self.write(block)

    def write(self, block, force=False):
        """Edit the block's message with its lines now, unless it was edited less than EDIT_GAP
        ago or has used its MAX_EDITS (the end of the turn forces it). False only on a refusal."""
        now = self.clock()
        if not block.stale:
            return True
        if not force and (block.edits >= MAX_EDITS or now - block.last < EDIT_GAP):
            return True
        self.step(
            "merge", lambda: self.transport.edit_note(block.message, block.render())
        )
        if self.errored:
            self.merge_ok = False
            return False
        block.edits += 1
        block.stale, block.last = False, now
        return True

    def flush(self):
        """Edit in the lines that arrived inside the last EDIT_GAP."""
        for block in self.stale:
            if block.stale and self.merge_ok:
                self.write(block)
        self.stale = [block for block in self.stale if block.stale]

    def detail(self, tool, detail):
        preview = detail.get("preview")
        if self.private or tool not in PREVIEW_TOOLS or not isinstance(preview, str):
            return ""
        shown = self.redact(preview)
        shown = squash(shown, MAX_DETAIL)
        if tool == "read_skill" and not SKILL_NAME.fullmatch(shown):
            return ""  # a skill name is the only thing that may appear for it
        return shown

    def render(self, now, outcome=None):
        head = {None: STATUS_CLOCK, True: STATUS_DONE}.get(outcome, STATUS_STOPPED)
        text = f"{head} \u00b7 {elapsed(now - self.began)}"
        if self.steps:
            text += f" \u00b7 {self.steps} step{'s' if self.steps != 1 else ''}"
        if self.failed:
            text += f" \u00b7 {self.failed} failed"
        if outcome is None and self.current:
            text += f" \u00b7 {self.current.lower()}"
        return text

    def draw(self):
        """The status line: it appears once a turn runs long, then only its clock moves."""
        if not self.clock_ok:
            return
        now = self.clock()
        if self.status is None:
            if now - self.began < STATUS_AFTER:
                return
            self.status = self.step(
                "status",
                lambda: self.transport.status(
                    self.message_id, self.render(now), working=True
                ),
            )
            if self.errored or self.status is None:
                self.status, self.clock_ok = None, False
                return
            self.last_edit = now
        elif now - self.last_edit >= CLOCK_GAP:
            self.step(
                "edit",
                lambda: self.transport.edit(
                    self.status, self.render(now), working=True
                ),
            )
            if self.errored:
                self.clock_ok = False
                return
            self.last_edit = now

    def finish(self, ok=True):
        """End the turn: the reaction goes; the steps and the status line stay.

        ok is False when the turn did not produce an answer, so the status line says it
        stopped early instead of finished."""
        status, reaction = self.status, self.reaction
        self.status = self.reaction = None
        self.quiet = True
        if status and self.clock_ok:
            text = self.render(self.clock(), outcome=bool(ok))
            self.step("edit", lambda: self.transport.edit(status, text))
        for block in self.stale:
            # Lines that were not edited in yet (inside the edit gap, or the message had reached
            # its edit limit) are written once more at the end, so the last figure shown is right.
            if block.stale and self.merge_ok:
                self.write(block, force=True)
        if reaction:
            self.step(
                "unreact", lambda: self.transport.unreact(self.message_id, reaction)
            )

    def step(self, name, action):
        """Run a transport call. A failure is reported once, then `errored` is set; the result
        is None on failure, and also for a call that returns nothing, so check `errored`."""
        self.errored = False
        try:
            return action()
        except Exception as exc:
            self.errored = True
            if name not in self.reported:
                self.reported.add(name)
                # Our own transport errors carry only an operation and a Lark code.
                reason = (
                    str(exc)[:60] if type(exc) is RuntimeError else type(exc).__name__
                )
                emit(
                    "progress.failed",
                    logging.WARNING,
                    step=name,
                    reason=reason,
                    **self.fields,
                )
            return None
