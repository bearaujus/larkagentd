import itertools

from test_agent import FakeAgent
from test_card_lifecycle import Transport, drain
from test_core import setup

GROUP = "grp"
ids = itertools.count(1)


def room(store, reply="answer"):
    svc, _ = setup(store)
    agent = FakeAgent(reply)
    svc.agent = agent
    return svc, agent, Transport()


def direct(svc, t, user, chat, text="/help"):
    """A person's first direct message: it is where the bot learns their direct chat."""
    n = next(ids)
    svc.receive("t", user, chat, f"dev{n}", f"dm{n}", text, thread=f"dm{n}")
    drain(svc, t)


def ask(svc, user, text, message_id=None, thread=None, chat=GROUP):
    """A message in a group that mentions the bot (the adapter has already checked that)."""
    message_id = message_id or f"g{next(ids)}"
    svc.receive(
        "t",
        user,
        chat,
        f"gev-{message_id}",
        message_id,
        text,
        thread=thread or message_id,
        in_thread=thread is not None,
        group=True,
    )
    return message_id


def owner_state(store, owner):
    with store.user(owner) as (_, s):
        return dict(s)


def to(t, chat):
    return [s for s in t.sent if s.get("chat") == chat]


def texts(t, chat):
    return [s.get("text") for s in to(t, chat)]
