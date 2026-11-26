from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..lark.message_links import message_link
from ..lark.message_links import verified_link as verified_link
from ..toolservice.larkcli import LarkError, message_time


@dataclass(frozen=True)
class SourceMessage:
    message_id: str
    chat_id: str
    thread_id: str | None
    root_id: str | None
    parent_id: str | None
    sender_id: str | None
    sender_type: str
    created_at: datetime
    text: str
    msg_type: str
    edited: bool
    link: str | None


@dataclass(frozen=True)
class Page:
    messages: tuple[SourceMessage, ...]
    next_page_token: str | None
    complete: bool


class SourceError(Exception):
    def __init__(self, code: str):
        self.code = (
            code
            if code
            in {
                "forbidden",
                "not_found",
                "rate_limited",
                "timeout",
                "unavailable",
                "bad_response",
            }
            else "unavailable"
        )
        super().__init__(self.code)


class GroupSource(Protocol):
    def list_messages(
        self, chat_id: str, *, page_token: str | None = None, limit: int = 50
    ) -> Page: ...
    def thread_messages(
        self,
        chat_id: str,
        thread_id: str,
        *,
        page_token: str | None = None,
        limit: int = 50,
    ) -> Page: ...


def _text(content):
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (ValueError, TypeError):
            return content
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        return " ".join(part for value in content.values() if (part := _text(value)))
    if isinstance(content, list):
        return " ".join(part for value in content if (part := _text(value)))
    return ""


def _map_error(exc):
    code = str(exc)
    return {
        "forbidden": "forbidden",
        "not_found": "not_found",
        "rate_limited": "rate_limited",
        "timeout": "timeout",
    }.get(code, "unavailable")


class CliGroupSource:
    def __init__(self, reader, hosts=("applink.larksuite.com", "applink.feishu.cn")):
        self.reader = reader
        self.hosts = hosts
        self.skipped = 0

    def list_messages(self, chat_id, *, page_token=None, limit=50):
        try:
            data = self.reader.raw_chat_messages(chat_id, page_token, limit)
        except LarkError as exc:
            raise SourceError(_map_error(exc)) from None
        return self._page(data, chat_id)

    def thread_messages(self, chat_id, thread_id, *, page_token=None, limit=50):
        try:
            data = self.reader.raw_thread_messages(thread_id, page_token, limit)
        except LarkError as exc:
            raise SourceError(_map_error(exc)) from None
        return self._page(data, chat_id)

    def messages(self, chat_id, ids):
        try:
            data = self.reader.raw_messages(ids)
        except LarkError as exc:
            raise SourceError(_map_error(exc)) from None
        # A lookup by ID must affirm the message's chat. List responses are already chat-bound.
        raw = (
            data.get("messages", data.get("items", []))
            if isinstance(data, dict)
            else []
        )
        items = [
            item
            for item in raw
            if isinstance(item, dict)
            and item.get("chat_id") == chat_id
            and item.get("message_id") in ids
        ]
        return self._page({"messages": items}, chat_id).messages

    def _page(self, data, chat_id):
        if not isinstance(data, dict):
            raise SourceError("bad_response")
        raw = data.get("messages", data.get("items", []))
        if not isinstance(raw, list):
            raise SourceError("bad_response")
        parsed = []
        skipped = 0
        for item in raw:
            if (
                not isinstance(item, dict)
                or item.get("deleted")
                or item.get("recalled")
            ):
                skipped += 1
                continue
            sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
            message_id = item.get("message_id") or item.get("id")
            actual_chat = item.get("chat_id", chat_id)
            stamp = message_time(item.get("create_time"))
            kind = item.get("msg_type") or "text"
            content = item.get("content", "") or (item.get("body") or {}).get(
                "content", ""
            )
            body = _text(content) if kind in ("text", "post") else ""
            if (
                not isinstance(message_id, str)
                or not isinstance(actual_chat, str)
                or stamp is None
                or not body.strip()
            ):
                skipped += 1
                continue
            if actual_chat != chat_id:
                skipped += 1
                continue
            link = message_link(item, chat_id, hosts=self.hosts)
            sender_type = (
                sender.get("sender_type")
                or sender.get("type")
                or item.get("sender_type")
                or item.get("type")
                or "unknown"
            )
            sender_type = {
                "bot": "app",
                "user": "user",
                "app": "app",
                "system": "system",
            }.get(str(sender_type).lower(), "unknown")
            parsed.append(
                SourceMessage(
                    message_id,
                    actual_chat,
                    item.get("thread_id"),
                    item.get("root_id"),
                    item.get("parent_id"),
                    sender.get("id") or sender.get("open_id") or item.get("sender_id"),
                    sender_type,
                    stamp,
                    body,
                    str(kind),
                    bool(item.get("updated") or item.get("edited")),
                    link,
                )
            )
        self.skipped += skipped
        return Page(
            tuple(parsed),
            data.get("next_page_token", data.get("page_token")),
            not bool(data.get("has_more", False)),
        )
