from datetime import datetime, timedelta, timezone

from larkagentd.knowledge.lark_sources import SourceMessage


class FakeGroupSource:
    def __init__(self, pages=None):
        self.pages = pages or {}

    def list_messages(self, chat_id, *, page_token=None, limit=50):
        return self.pages.get(
            ("chat", chat_id, page_token), self.pages.get(("chat", chat_id, None))
        )

    def thread_messages(self, chat_id, thread_id, *, page_token=None, limit=50):
        return self.pages.get(("thread", chat_id, thread_id, page_token))


def message(
    message_id,
    *,
    chat_id="oc_example1234",
    thread_id=None,
    root_id=None,
    parent_id=None,
    sender_id="ou_example1234",
    sender_type="user",
    minute=0,
    text="message",
    msg_type="text",
    link=None,
):
    return SourceMessage(
        message_id,
        chat_id,
        thread_id,
        root_id,
        parent_id,
        sender_id,
        sender_type,
        datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minute),
        text,
        msg_type,
        False,
        link,
    )


class FakeReader:
    def __init__(self, data=None, error=None):
        self.data = data or {}
        self.error = error
        self.calls = []

    def raw_chat_messages(self, chat_id, page_token, limit):
        self.calls.append(("chat", chat_id, page_token, limit))
        if self.error:
            raise self.error
        return self.data

    def raw_thread_messages(self, thread_id, page_token, limit):
        self.calls.append(("thread", thread_id, page_token, limit))
        if self.error:
            raise self.error
        return self.data
