from __future__ import annotations

from datetime import datetime, timezone

# Load the SDK compatibility boundary before its generated API modules.
from .sdk import lark  # noqa: F401

# isort: split
from lark_oapi.api.im.v1 import ListMessageRequest

from ..knowledge.lark_sources import Page, SourceError, SourceMessage
from .inbound import normalize


def _error_code(exc):
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return "timeout"
    response = getattr(exc, "response", None)
    status = (
        getattr(exc, "status_code", None)
        or getattr(exc, "http_status", None)
        or getattr(response, "status_code", None)
    )
    code = getattr(exc, "code", None)
    values = {str(value).lower() for value in (status, code) if value is not None}
    if values & {
        "403",
        "forbidden",
        "permission_denied",
        "access_denied",
        "99991400",
        "99991401",
    }:
        return "forbidden"
    if values & {"404", "not_found", "not found", "99991663"}:
        return "not_found"
    if values & {"429", "rate_limited", "too_many_requests", "99991429"}:
        return "rate_limited"
    if values & {"408", "504", "timeout", "timed_out"}:
        return "timeout"
    return "unavailable"


def _timestamp(value):
    try:
        milliseconds = int(value)
        return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        raise SourceError("bad_response") from None


def _map_message(item, requested_chat_id):
    if getattr(item, "chat_id", None) != requested_chat_id or getattr(
        item, "deleted", False
    ):
        return None
    message_id = getattr(item, "message_id", None)
    msg_type = getattr(item, "msg_type", None)
    sender = getattr(item, "sender", None)
    body = getattr(item, "body", None)
    if not isinstance(message_id, str) or not isinstance(msg_type, str):
        raise SourceError("bad_response")
    inbound = normalize(
        msg_type,
        getattr(body, "content", "") if body else "",
        message_id,
        getattr(item, "mentions", None) or (),
    )
    sender_id = getattr(sender, "id", None) if sender else None
    sender_type = getattr(sender, "sender_type", "unknown") if sender else "unknown"
    create_time = getattr(item, "create_time", None)
    if create_time is None:
        raise SourceError("bad_response")
    return SourceMessage(
        message_id=message_id,
        chat_id=requested_chat_id,
        thread_id=getattr(item, "thread_id", None) or None,
        root_id=getattr(item, "root_id", None) or None,
        parent_id=getattr(item, "parent_id", None) or None,
        sender_id=sender_id or None,
        sender_type=sender_type,
        created_at=_timestamp(create_time),
        text=inbound.text,
        msg_type=msg_type,
        edited=bool(getattr(item, "updated", None)),
        link=None,
    )


class SdkGroupSource:
    """Bounded read-only Group Brain history adapter for the Gateway's Lark client."""

    def __init__(self, client):
        self.client = client

    def list_messages(self, chat_id, *, page_token=None, limit=50):
        return self._list("chat", chat_id, chat_id, page_token, limit)

    def thread_messages(self, chat_id, thread_id, *, page_token=None, limit=50):
        return self._list("thread", thread_id, chat_id, page_token, limit)

    def _list(self, container_type, container_id, chat_id, page_token, limit):
        page_size = max(1, min(int(limit), 50))
        try:
            builder = (
                ListMessageRequest.builder()
                .container_id_type(container_type)
                .container_id(container_id)
                .sort_type("ByCreateTimeDesc")
                .page_size(page_size)
            )
            if page_token:
                builder.page_token(page_token)
            response = self.client.im.v1.message.list(builder.build())
        except Exception as exc:
            raise SourceError(_error_code(exc)) from None
        try:
            if not response.success():
                raise SourceError(_error_code(response))
            data = response.data
            if data is None:
                raise SourceError("bad_response")
            items = getattr(data, "items", None) or ()
            mapped = tuple(
                message
                for item in items
                if (message := _map_message(item, chat_id)) is not None
            )
            has_more = bool(getattr(data, "has_more", False))
            next_token = getattr(data, "page_token", None) if has_more else None
            if has_more and not isinstance(next_token, str):
                raise SourceError("bad_response")
            return Page(mapped, next_token, not has_more)
        except SourceError:
            raise
        except Exception:
            raise SourceError("bad_response") from None
