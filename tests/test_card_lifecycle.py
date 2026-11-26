import json
import time

from sqlalchemy import select, update

from larkagentd.core.store import jobs


class Transport:
    def __init__(self):
        self.messages = {}
        self.created = []
        self.updates = []
        self.recalls = []
        self.allow_recall = True
        self.fail_patch = False
        self.sent = []
        self.progress = []
        self.fail_progress = False

    def send(self, payload, key):
        if key in self.messages:
            return key
        self.created.append(key)
        self.sent.append(payload)
        self.messages[key] = (
            json.loads(payload["card"]) if payload.get("card") else payload["text"]
        )
        return key

    def patch(self, message_id, content):
        if self.fail_patch:
            raise RuntimeError("temporary failure")
        if message_id not in self.messages:
            return False
        self.messages[message_id] = json.loads(content)
        self.updates.append(message_id)
        return True

    def recall(self, message_id):
        self.recalls.append(message_id)
        if self.allow_recall:
            self.messages.pop(message_id, None)
        return self.allow_recall

    def _progress(self, *event):
        if self.fail_progress:
            raise RuntimeError("Lark reaction failed (code=1)")
        self.progress.append(event)

    def react(self, message_id):
        self._progress("react", message_id)
        return "reaction-1"

    def unreact(self, message_id, reaction_id):
        self._progress("unreact", message_id, reaction_id)

    def status(self, message_id, text, working=False):
        self._progress("status", message_id, text)
        return "status-1"

    def edit(self, message_id, text, working=False):
        self._progress("edit", message_id, text)

    def edit_note(self, message_id, text):
        self._progress("edit_note", message_id, text)

    def note(self, chat, text, reply_to=None):
        self._progress("note", chat, text, reply_to)
        return "note-1"


def ready_jobs(store, kind):
    with store.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.kind == kind).values(available=0))


def drain(svc, transport):
    for _ in range(100):
        job = svc.store.claim()
        if job is None:
            with svc.store.engine.connect() as conn:
                soon = conn.execute(
                    select(jobs.c.id).where(
                        jobs.c.kind == "send", jobs.c.available < time.time() + 1
                    )
                ).first()
            if soon:
                time.sleep(0.55)
                continue
            return
        svc.work(job, transport)
    raise AssertionError("queue did not drain")
