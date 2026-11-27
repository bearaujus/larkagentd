import hashlib
import io
import json
import logging
import uuid

from .sdk import lark

# SDK compatibility must run before importing its generated API submodules.
# isort: split
from lark_oapi.api.contact.v3 import (
    BatchGetIdUserRequest,
    BatchGetIdUserRequestBody,
    BatchUserRequest,
    GetUserRequest,
)
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageRequest,
    GetChatMembersRequest,
    GetChatRequest,
    GetMessageRequest,
    GetMessageResourceRequest,
    ListMessageRequest,
    MergeForwardMessageRequest,
    MergeForwardMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    UpdateMessageRequest,
    UpdateMessageRequestBody,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from ..core.diagnostics import emit
from ..core.fields import LARK_APP_ID, normalize_email
from ..knowledge.sanitize import sanitize
from . import render
from .inbound import (
    IMAGE,
    Fetched,
    ResourceTooLarge,
    bot_mention_names,
    forward_text,
    normalize,
    session_of,
    without_names,
)
from .message_links import MESSAGE_ID, THREAD_ID
from .profile import label, profile_fields
from .ui_text import TOASTS


class ObservedWebSocket(lark.ws.Client):
    """Narrow SDK adapter for actual initial/reconnect status; tested with lark-oapi 1.7.5."""

    def __init__(self, *args, on_connection, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_connection = on_connection

    async def _connect(self):
        try:
            await super()._connect()
        except Exception as exc:
            self.on_connection(False, type(exc).__name__)
            raise
        if self._conn is not None:
            self.on_connection(True)

    async def _disconnect(self):
        self.on_connection(False)
        await super()._disconnect()


class Lark:
    def __init__(self, config, service):
        self.config, self.service = config, service
        self.bot_id = None
        # The SDK's logger writes connection URLs and raw exception details to stdout.
        # Replace it with first-party, credential-free lifecycle events.
        sdk_log = logging.getLogger("Lark")
        sdk_log.handlers[:] = [logging.NullHandler()]
        sdk_log.propagate = False
        self.client = (
            lark.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .domain(lark.LARK_DOMAIN)
            .timeout(10)
            .log_level(lark.LogLevel.ERROR)
            .build()
        )

    def send(self, payload, key):
        if payload["card"]:
            return self.deliver(
                payload["chat"], None, "interactive", payload["card"], key
            )
        if payload.get("file"):
            return self.send_file(payload, key)
        reply_to = payload.get("reply_to")
        thread = bool(payload.get("thread"))
        if (
            thread
            and payload.get("gb_group") == "group:" + payload["chat"]
            and payload.get("source_message_id")
        ):
            forwarded = self.forward_source(
                payload["chat"], reply_to, payload["source_message_id"], key
            )
            if forwarded:
                return forwarded
        # A long answer goes out as several messages, in order, from this one job; a short one
        # is a single part. The first part picks the way it is delivered: the table card first
        # when there is one, then markdown, then plain text if Lark rejects the post; quoted
        # reply first, a plain message if the original was recalled. The same uuid keeps it
        # once-only.
        answer_to = payload.get("answer_to")
        mention = render.mention_id(answer_to)
        native_reply = payload.get("native_reply")
        body = render.inert_mentions(payload["text"]) if answer_to else payload["text"]
        parts = (
            [render.final_post(body, payload.get("usage_footer", ""))]
            if payload.get("single_reply")
            else render.split_message(body)
        )
        failed = []
        attachments = payload.get("attachment_keys") or ()
        if attachments:
            # A post's attachment zone keeps the answer and all its files in one reply.
            attempts = [("post", render.post(parts[0], mention, attachments))]
            if mention:
                attempts.append(
                    ("post", render.post(parts[0], attachments=attachments))
                )
        else:
            cards = [
                payload[name]
                for name in ("preview_card", "fallback_card")
                if payload.get(name)
            ]
            attempts = [
                (
                    "interactive",
                    render.addressed_card(card, mention) if answer_to else card,
                )
                for card in cards
            ]
            if mention:
                attempts.extend(
                    ("interactive", render.addressed_card(card, None)) for card in cards
                )
            if native_reply:
                attempts.insert(
                    0,
                    (
                        "text",
                        render.plain(render.inert_mentions(native_reply), mention),
                    ),
                )
            attempts.extend(
                [
                    ("post", render.post(parts[0], mention)),
                    ("text", render.plain(parts[0], mention)),
                ]
            )
            if mention:
                attempts.extend(
                    [
                        ("post", render.post(parts[0])),
                        ("text", render.plain(parts[0])),
                    ]
                )
        for target in (
            (reply_to,)
            if payload.get("thread") and reply_to
            else (reply_to, None)
            if reply_to
            else (None,)
        ):
            for msg_type, content in attempts:
                try:
                    message_id = self.deliver(
                        payload["chat"], target, msg_type, content, key, thread=thread
                    )
                except RuntimeError as exc:
                    failed.append(f"{'reply' if target else 'create'}:{msg_type}:{exc}")
                    continue
                if failed:
                    # Our own errors carry only an operation and a Lark code.
                    emit(
                        "delivery.fallback",
                        logging.WARNING,
                        delivered=f"{'reply' if target else 'create'}:{msg_type}",
                        failed=";".join(failed)[:200],
                    )
                if msg_type != "interactive":
                    self.send_parts(
                        payload["chat"], target, msg_type, parts[1:], key, thread
                    )
                return message_id
        raise RuntimeError("Lark delivery failed")

    def forward_source(self, chat, reply_to, source_id, key):
        """Deliver a native source card in the verified reply thread."""
        if not reply_to or not MESSAGE_ID.fullmatch(source_id):
            return None
        try:
            target, _ = self.message_items(reply_to)
            source, _ = self.message_items(source_id)
        except RuntimeError:
            return None
        thread = getattr(target, "thread_id", None)
        if (
            not isinstance(thread, str)
            or not THREAD_ID.fullmatch(thread)
            or target.chat_id != chat
            or source.chat_id != chat
            or source.msg_type != "text"
            or not source.body
            or not source.sender
            or source.sender.sender_type != "user"
        ):
            return None
        body = normalize(
            source.msg_type,
            source.body.content,
            source_id,
            getattr(source, "mentions", None) or (),
        )
        if sanitize(body.text).reasons:
            return None
        result = self.client.im.v1.message.merge_forward(
            MergeForwardMessageRequest.builder()
            .receive_id_type("thread_id")
            .uuid(key)
            .request_body(
                MergeForwardMessageRequestBody.builder()
                .receive_id(thread)
                .message_id_list([source_id])
                .build()
            )
            .build()
        )
        if not result.success():
            emit(
                "delivery.source_preview",
                logging.INFO,
                outcome="fallback",
                provider_code=result.code,
            )
            return None
        if result.data and result.data.message and result.data.message.message_id:
            emit("delivery.source_preview", logging.INFO, outcome="forwarded")
            return result.data.message.message_id
        if result.data and result.data.invalid_message_id_list:
            return None
        raise RuntimeError("Lark accepted source forwarding without a receipt")

    def send_parts(self, chat, target, msg_type, parts, key, thread):
        """The rest of a long answer, one message each, in order, to the same place and in the
        same form as the first part (the one Lark accepted). Each part has its own uuid, made
        from the job's, so a retried job cannot send a part twice."""
        for number, part in enumerate(parts, 1):
            content = render.plain(part) if msg_type == "text" else render.post(part)
            self.deliver(
                chat,
                target,
                msg_type,
                content,
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}:{number}")),
                thread=thread,
            )
        if parts:
            emit("delivery.split", logging.INFO, parts=len(parts) + 1)

    def upload_file(self, file):
        name, data = file["name"], file["text"].encode()
        uploaded = self.client.im.v1.file.create(
            CreateFileRequest.builder()
            .request_body(
                CreateFileRequestBody.builder()
                .file_type("stream")
                .file_name(name)
                .file(io.BytesIO(data))
                .build()
            )
            .build()
        )
        if not uploaded.success():
            raise RuntimeError(f"Lark upload failed (code={uploaded.code})")
        return uploaded.data.file_key

    def send_file(self, payload, key):
        """Deliver a standalone file when an interactive answer also needs a button."""
        content = json.dumps({"file_key": self.upload_file(payload["file"])})
        reply_to = payload.get("reply_to")
        failure = None
        for target in (
            (reply_to,)
            if payload.get("thread") and reply_to
            else (reply_to, None)
            if reply_to
            else (None,)
        ):
            try:
                return self.deliver(
                    payload["chat"],
                    target,
                    "file",
                    content,
                    key,
                    thread=bool(payload.get("thread")),
                )
            except RuntimeError as exc:
                failure = exc
        raise failure

    def deliver(self, chat, reply_to, msg_type, content, key, thread=False):
        if reply_to:
            body = (
                ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .uuid(key)
            )
            if thread:
                # A reply to a message that starts no thread opens one; inside a thread it
                # stays there.
                body = body.reply_in_thread(True)
            result = self.client.im.v1.message.reply(
                ReplyMessageRequest.builder()
                .message_id(reply_to)
                .request_body(body.build())
                .build()
            )
        else:
            result = self.client.im.v1.message.create(
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat)
                    .msg_type(msg_type)
                    .content(content)
                    .uuid(key)
                    .build()
                )
                .build()
            )
        if not result.success():
            raise RuntimeError(f"Lark delivery failed (code={result.code})")
        return result.data.message_id

    def open_activity(self, chat, reply_to, key, card, text):
        image = self.config.loading_image_key
        if image:
            with_icon = json.loads(card)
            with_icon["body"]["elements"][0]["icon"] = {
                "tag": "custom_icon",
                "img_key": image,
            }
            try:
                sent = self.deliver(
                    chat,
                    reply_to,
                    "interactive",
                    json.dumps(with_icon),
                    key,
                    thread=True,
                )
                return sent, "interactive"
            except RuntimeError:
                pass
        try:
            return self.deliver(
                chat, reply_to, "interactive", card, key, thread=True
            ), "interactive"
        except RuntimeError:
            return self.deliver(
                chat, reply_to, "post", render.post(text), key, thread=True
            ), "post"

    def update_activity(self, message_id, kind, card, text):
        if kind == "interactive":
            if not self.patch(message_id, card):
                raise RuntimeError("Lark activity card update failed")
        elif not self.update(message_id, "post", render.post(text)).success():
            raise RuntimeError("Lark activity post update failed")

    def update(self, message_id, msg_type, content):
        return self.client.im.v1.message.update(
            UpdateMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                UpdateMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )

    def delete(self, message_id):
        result = self.client.im.v1.message.delete(
            DeleteMessageRequest.builder().message_id(message_id).build()
        )
        if not result.success():
            raise RuntimeError(f"Lark delete failed (code={result.code})")

    def patch(self, message_id, content):
        result = self.client.im.v1.message.patch(
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(PatchMessageRequestBody.builder().content(content).build())
            .build()
        )
        if result.success():
            return True
        # A user may have recalled the card manually. Confirm deletion before creating
        # a replacement; generic permission/network errors must never cause chat spam.
        existing = self.client.im.v1.message.get(
            GetMessageRequest.builder().message_id(message_id).build()
        )
        if existing.success() and existing.data and existing.data.items:
            if any(
                item.message_id == message_id and item.deleted is True
                for item in existing.data.items
            ):
                return False
        raise RuntimeError(f"Lark card update failed (code={result.code})")

    def bot_open_id(self):
        """The bot's own open id, read from Lark once. A mention of this id is how a group
        message is addressed to the bot. Empty when Lark could not say: group messages are then
        ignored, never answered on a guess, and the next one asks again."""
        if self.bot_id is None:
            request = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({lark.AccessTokenType.TENANT})
                .build()
            )
            try:
                response = self.client.request(request)
                body = json.loads(response.raw.content)
                found = (body.get("bot") or {}).get("open_id")
            except Exception as exc:
                emit(
                    "lark.bot_info_failed",
                    logging.WARNING,
                    error_type=type(exc).__name__,
                )
                return ""
            if not isinstance(found, str) or not found:
                emit("lark.bot_info_failed", logging.WARNING, error_type="NoOpenId")
                return ""
            self.bot_id = found
        return self.bot_id

    def email_of(self, open_id):
        """The person's Lark email, the company address first, or None when the profile has
        none. Admission and dashboard login use it. Raises when Lark refuses, so the caller can tell a
        failed lookup from a person without an address. The rest of the profile is not read."""
        result = self.client.contact.v3.user.get(
            GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
        )
        if not result.success() or not result.data or not result.data.user:
            raise RuntimeError(f"Lark user read failed (code={result.code})")
        for field in ("enterprise_email", "email"):
            try:
                return normalize_email(getattr(result.data.user, field, None))
            except ValueError:
                continue
        return None

    def display_names(self, open_ids):
        response = self.client.contact.v3.user.batch(
            BatchUserRequest.builder()
            .user_id_type("open_id")
            .user_ids(open_ids)
            .build()
        )
        if not response.success() or not response.data:
            return {}
        return {
            person.open_id: name
            for person in response.data.items or ()
            if person.open_id in open_ids
            and (name := label(person.name or person.en_name))
        }

    def user_profile(self, open_id):
        """Display-only profile from the bot's contact API; no phone/departments returned."""
        result = self.client.contact.v3.user.get(
            GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
        )
        if not result.success() or not result.data or not result.data.user:
            raise RuntimeError("Lark profile unavailable")
        person = result.data.user
        email = None
        for field in ("enterprise_email", "email"):
            try:
                email = normalize_email(getattr(person, field, None))
                break
            except ValueError:
                continue
        avatar = getattr(person, "avatar", None)
        return profile_fields(
            {
                "name": getattr(person, "name", None)
                or getattr(person, "en_name", None),
                "email": email,
                "avatar_url": next(
                    (
                        getattr(avatar, key, None)
                        for key in (
                            "avatar_240",
                            "avatar_72",
                            "avatar_640",
                            "avatar_origin",
                        )
                        if getattr(avatar, key, None)
                    ),
                    None,
                ),
            }
        )

    def user_profile_by_email(self, email):
        email = normalize_email(email)
        result = self.client.contact.v3.user.batch_get_id(
            BatchGetIdUserRequest.builder()
            .user_id_type("open_id")
            .request_body(
                BatchGetIdUserRequestBody.builder()
                .emails([email])
                .include_resigned(False)
                .build()
            )
            .build()
        )
        if not result.success() or not result.data:
            raise RuntimeError("Lark email lookup unavailable")
        for person in result.data.user_list or ():
            try:
                matched = normalize_email(person.email) == email
            except ValueError:
                continue
            if matched and isinstance(person.user_id, str) and person.user_id:
                return self.user_profile(person.user_id)
        return None

    def group_member(self, chat_id, open_id):
        """Check the exact Lark actor in a registered chat with the app's member-list API."""
        cursor = None
        for _ in range(50):
            request = (
                GetChatMembersRequest.builder()
                .chat_id(chat_id)
                .member_id_type("open_id")
                .page_size(100)
            )
            if cursor:
                request.page_token(cursor)
            response = self.client.im.v1.chat_members.get(request.build())
            if not response.success() or not response.data:
                return False
            if any(member.member_id == open_id for member in response.data.items or ()):
                return True
            next_cursor = response.data.page_token
            if not response.data.has_more or not next_cursor or next_cursor == cursor:
                return False
            cursor = next_cursor
        return False

    def group_admin(self, chat_id, open_id):
        if not isinstance(chat_id, str) or not chat_id.startswith("oc_") or not open_id:
            return False
        result = self.client.im.v1.chat.get(
            GetChatRequest.builder().chat_id(chat_id).user_id_type("open_id").build()
        )
        if not result.success() or not result.data:
            return False
        return open_id == result.data.owner_id or open_id in (
            result.data.user_manager_id_list or ()
        )

    def group_profile(self, chat_id):
        result = self.client.im.v1.chat.get(
            GetChatRequest.builder().chat_id(chat_id).build()
        )
        if not result.success() or not result.data:
            raise RuntimeError("Lark group metadata unavailable")
        return profile_fields(
            {"name": result.data.name, "avatar_url": result.data.avatar}
        )

    def message(self, data):
        event = data.event
        message = event.message
        if message.chat_type not in ("p2p", "group", "topic_group"):
            return
        group = message.chat_type != "p2p"
        sender_type = event.sender.sender_type
        human = sender_type == "user"
        allowed = self.config.lark_allowed_senders
        if human:
            if "user" not in allowed:
                return
        elif (
            not group
            or sender_type not in ("app", "bot")
            or not any(entry != "user" for entry in allowed)
        ):
            return
        mentions = getattr(message, "mentions", None) or ()
        bot_names = []
        if group:
            # In a group the bot answers only when it is mentioned: everything else is
            # people talking to each other.
            if not mentions:
                return
            bot_names = bot_mention_names(mentions, self.bot_open_id())
            if not bot_names:
                return
        if human:
            who = event.sender.sender_id
            sender = getattr(who, "open_id", None)
            if (
                not isinstance(sender, str)
                or not sender
                or LARK_APP_ID.fullmatch(sender)
            ):
                return
            ids = (who.user_id, who.union_id)
            event_id = data.header.event_id
        else:
            # The callback's open ID can differ from the app ID. Read the signed-in
            # Lark record for the same message and chat before admitting the request.
            sender = self.app_sender(message)
            if sender not in allowed or sender == self.config.app_id:
                return
            ids = ()
            event_id = "app-message:" + message.message_id
        inbound = normalize(
            message.message_type,
            message.content,
            message.message_id,
            mentions,
        )
        thread, in_thread = session_of(message)
        self.service.receive(
            data.header.tenant_key,
            sender,
            message.chat_id,
            event_id,
            message.message_id,
            without_names(inbound.text, bot_names) if group else inbound.text,
            ids=ids,
            kind=inbound.kind,
            attachments=[a.asdict() for a in inbound.attachments],
            reply_to=getattr(message, "parent_id", None)
            or getattr(message, "upper_message_id", None)
            or None,
            thread=thread,
            in_thread=in_thread,
            group=group,
            thread_ref=getattr(message, "thread_id", None) or None,
            transport=self,
        )

    def message_items(self, message_id):
        result = self.client.im.v1.message.get(
            GetMessageRequest.builder().message_id(message_id).build()
        )
        items = result.data.items if result.success() and result.data else None
        if not items:
            raise RuntimeError(f"Lark message read failed (code={result.code})")
        item = next((i for i in items if i.message_id == message_id), None)
        if item is None or item.deleted:
            raise RuntimeError("Lark message was missing or deleted")
        return item, items

    def app_sender(self, message):
        try:
            item, _ = self.message_items(message.message_id)
        except RuntimeError:
            emit("lark.app_sender_unverified", logging.WARNING)
            return None
        sender = item.sender
        if (
            item.chat_id == message.chat_id
            and sender
            and sender.sender_type in ("app", "bot")
            and sender.id_type == "app_id"
            and isinstance(sender.id, str)
            and LARK_APP_ID.fullmatch(sender.id)
        ):
            return sender.id
        return None

    def fetch_message(self, message_id):
        """A message read back from Lark (a replied-to message, a forwarded thread)."""
        item, items = self.message_items(message_id)
        assistant = bool(item.sender and item.sender.sender_type == "app")
        # The conversation this message belongs to, by the rule `session_of` uses for an event.
        session = session_of(item)[0] or ""
        if item.msg_type == "merge_forward":
            return Fetched(forward_text(items, message_id), assistant, session=session)
        inbound = normalize(
            item.msg_type,
            item.body.content if item.body else "",
            item.message_id,
            item.mentions or (),
        )
        return Fetched(
            inbound.text,
            assistant,
            inbound.attachments,
            session,
            chat_id=item.chat_id or "",
            sender_id=item.sender.id
            if item.sender and item.sender.sender_type == "user"
            else "",
        )

    def thread_messages(self, thread_id, limit=40):
        """The latest messages of a thread, oldest first, written by people (not apps), as dicts with
        `message_id`, `owner` (`tenant:open_id`, the key the Gateway uses), `text`, `created` and the
        `attachments` that are images (a message that is only an image is kept).
        Raises when Lark refuses: the bot needs permission to read message history."""
        result = self.client.im.v1.message.list(
            ListMessageRequest.builder()
            .container_id_type("thread")
            .container_id(thread_id)
            .sort_type("ByCreateTimeDesc")
            .page_size(max(1, min(int(limit), 50)))
            .build()
        )
        if not result.success() or not result.data:
            raise RuntimeError(f"Lark thread read failed (code={result.code})")
        found = []
        for item in reversed(result.data.items or []):
            if item.deleted or not item.sender or item.sender.sender_type != "user":
                continue
            inbound = normalize(
                item.msg_type,
                item.body.content if item.body else "",
                item.message_id,
                item.mentions or (),
            )
            images = [a.asdict() for a in inbound.attachments if a.kind == IMAGE]
            if not inbound.text.strip() and not images:
                continue
            found.append(
                dict(
                    message_id=item.message_id,
                    owner=f"{item.sender.tenant_key or ''}:{item.sender.id or ''}",
                    text=inbound.text,
                    created=int(item.create_time or 0),
                    attachments=images,
                )
            )
        return found

    def download(self, attachment, limit):
        """(bytes, file name) of an image or file in a message; ResourceTooLarge above limit."""
        result = self.client.im.v1.message_resource.get(
            GetMessageResourceRequest.builder()
            .message_id(attachment.message_id)
            .file_key(attachment.key)
            .type("image" if attachment.kind == IMAGE else "file")
            .build()
        )
        if not result.success() or result.file is None:
            raise RuntimeError(f"Lark download failed (code={result.code})")
        data = result.file.read(limit + 1)
        if len(data) > limit:
            raise ResourceTooLarge()
        return data, result.file_name or ""

    def action(self, data):
        event = data.event
        context = event.context
        # Some card callbacks lack an event ID. Token + actor + card + value form a stable retry key.
        value = dict(event.action.value or {})
        if event.action.option is not None:
            value["choice"] = event.action.option
        key = (
            data.header.event_id
            or hashlib.sha256(
                json.dumps(
                    [
                        event.token,
                        event.operator.open_id,
                        context.open_message_id,
                        value,
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        )
        result = self.service.action(
            data.header.tenant_key,
            event.operator.open_id,
            context.open_chat_id,
            key,
            value,
            message_id=context.open_message_id,
            transport=self,
        )
        return P2CardActionTriggerResponse(
            {"toast": {"type": "info", "content": result}}
        )

    def chat_entered(self, data):
        try:
            self.service.welcome(
                data.header.tenant_key,
                data.event.operator_id.open_id,
                data.event.chat_id,
                data.header.event_id,
                transport=self,
            )
        except Exception as exc:
            emit("lark.welcome_failed", logging.ERROR, error_type=type(exc).__name__)
            raise

    def safe_message(self, data):
        try:
            return self.message(data)
        except Exception as exc:
            emit(
                "lark.message_handler_failed",
                logging.ERROR,
                error_type=type(exc).__name__,
            )
            raise

    def safe_action(self, data):
        try:
            return self.action(data)
        except Exception as exc:
            emit(
                "lark.card_handler_failed", logging.ERROR, error_type=type(exc).__name__
            )
            return P2CardActionTriggerResponse(
                {
                    "toast": {
                        "type": "error",
                        "content": TOASTS["action_failed"],
                    }
                }
            )

    def run(self, on_connection=lambda *args: None):
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.safe_message)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self.chat_entered)
            .register_p2_card_action_trigger(self.safe_action)
            .build()
        )
        client = ObservedWebSocket(
            self.config.app_id,
            self.config.app_secret,
            on_connection=on_connection,
            domain=lark.LARK_DOMAIN,
            event_handler=handler,
            log_level=lark.LogLevel.ERROR,
        )
        client.start()
