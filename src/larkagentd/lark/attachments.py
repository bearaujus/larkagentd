"""Turn what a message carries into what the agent can use.

Runs in a worker, outside any database transaction: it downloads from Lark. A failure never
fails the turn. Whatever cannot be read becomes a note, so the agent can tell the user.
"""

import base64
from dataclasses import dataclass, field

from .inbound import AUDIO, FILE, IMAGE, VIDEO, Attachment, ResourceTooLarge
from .links import MAX_LINKS, links

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 8_000_000
# Images of other people's thread messages: few and small in total, newest first.
MAX_THREAD_IMAGES = 4
MAX_THREAD_IMAGE_BYTES = 12_000_000
MAX_FILES = 3
MAX_FILE_BYTES = 2_000_000
MAX_FILE_CHARS = 60_000
MAX_TOTAL_CHARS = 120_000
MAX_QUOTE_CHARS = 2_000
MAX_FORWARD_CHARS = 8_000
TEXT_SUFFIXES = frozenset(
    "txt md csv tsv json jsonl yaml yml xml html sql log py sh toml ini cfg conf "
    "properties js ts tsx jsx java go rs rb php sql ddl dml tf hcl".split()
)
IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


@dataclass
class Resolved:
    images: list = field(default_factory=list)  # data: URLs, never stored
    documents: list = field(
        default_factory=list
    )  # attached files: dict(name, text, cut)
    notes: list = field(default_factory=list)  # what could not be read, for the agent
    quote: dict = None  # dict(text, assistant)
    forwarded: list = field(default_factory=list)  # texts of forwarded threads
    lark_links: bool = False
    # The conversation the quoted message belongs to, so its history can be brought along.
    quote_session: str = ""
    # Links in the current request, also eligible for editing with the appropriate grants.
    lark_allowed: list = field(default_factory=list)
    lark_thread_links: list = field(
        default_factory=list
    )  # read-only links from the requester

    def counts(self):
        return dict(
            images=len(self.images),
            documents=len(self.documents),
            notes=len(self.notes),
            quoted=bool(self.quote),
            forwarded=len(self.forwarded),
        )


def image_type(data):
    for signature, mime in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def suffix(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


KIND_NAMES = {
    IMAGE: "an image",
    AUDIO: "an audio message",
    VIDEO: "a video",
    FILE: "a file",
}


def label(attachment):
    return attachment.name or KIND_NAMES.get(attachment.kind, "an attachment")


def decode(data):
    """Text of a file, or None when it is binary."""
    if b"\x00" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def read_image(transport, attachment, resolved):
    if len(resolved.images) >= MAX_IMAGES:
        resolved.notes.append(f"Only the first {MAX_IMAGES} images were shown to you.")
        return
    data, _ = transport.download(attachment, MAX_IMAGE_BYTES)
    mime = image_type(data)
    if mime is None:
        resolved.notes.append("An image was in a format you cannot view.")
        return
    resolved.images.append(f"data:{mime};base64," + base64.b64encode(data).decode())


def thread_images(transport, unseen, taken, allowed):
    """Download the images of unseen thread messages and label them in the message lines.

    `unseen` are the `{person, text, _images}` items of the thread; `taken` is how many images the
    request already carries (the user's own, which come first). The newest MAX_THREAD_IMAGES images are
    attached, oldest of those first, numbered after the user's. Each line gets `[image N]` for an image
    the model can see and `[image, not shown]` for one it cannot (too many, too big, unreadable, or the
    model cannot read images). Returns (data URLs in order, how many images were not shown).

    Runs outside any database transaction: it downloads from Lark. A failure leaves that image unshown."""
    refs = []
    for item in unseen:
        for value in item.pop("_images", ()):
            attachment = Attachment.load(value)
            if attachment is not None:
                refs.append((item, attachment))
    chosen = {id(r[1]) for r in refs[-MAX_THREAD_IMAGES:]} if allowed else set()
    urls, hidden, used = [], 0, 0
    marks = {}
    for item, attachment in refs:
        mark = "[image, not shown]"
        if id(attachment) in chosen:
            try:
                data, _ = transport.download(
                    attachment, min(MAX_IMAGE_BYTES, MAX_THREAD_IMAGE_BYTES - used)
                )
                mime = image_type(data)
                if mime is not None and used + len(data) <= MAX_THREAD_IMAGE_BYTES:
                    used += len(data)
                    urls.append(
                        f"data:{mime};base64," + base64.b64encode(data).decode()
                    )
                    mark = f"[image {taken + len(urls)}]"
            except Exception:
                pass  # Our own errors name an operation and a Lark code, never content.
        if mark == "[image, not shown]":
            hidden += 1
        marks.setdefault(id(item), []).append(mark)
    for item in unseen:
        if id(item) in marks:
            item["text"] = " ".join(
                t for t in (item.get("text", ""), *marks[id(item)]) if t
            )
    return urls, hidden


def read_file(transport, attachment, resolved, used):
    name = label(attachment)
    if suffix(attachment.name) not in TEXT_SUFFIXES:
        kind = suffix(attachment.name) or "this type"
        resolved.notes.append(
            f"{name} was not readable: {kind} files are not supported yet. "
            "Text, CSV, JSON, SQL and similar files, and images, are."
        )
        return used
    if len(resolved.documents) >= MAX_FILES:
        resolved.notes.append(
            f"{name} was not read: at most {MAX_FILES} files per message."
        )
        return used
    data, _ = transport.download(attachment, MAX_FILE_BYTES)
    text = decode(data)
    if text is None:
        resolved.notes.append(f"{name} was not readable: it is not a text file.")
        return used
    room = max(0, min(MAX_FILE_CHARS, MAX_TOTAL_CHARS - used))
    cut = len(text) > room
    resolved.documents.append(dict(name=name, text=text[:room], cut=cut))
    return used + min(len(text), room)


def note_links(payload, resolved, lark_in_group=False):
    """Collect current-request links without opening the files they point to."""
    group = bool(payload.get("group"))
    source = payload.get("text", "")
    if resolved.quote and not resolved.quote["assistant"] and not group:
        source += "\n" + resolved.quote["text"]
    references = links(source)
    resolved.lark_links = bool(references)
    if not references:
        return
    if group and not lark_in_group:
        resolved.notes.append(
            "Lark links are not opened in this group. Ask an administrator to enable the Lark file tools for this group."
        )
        return
    resolved.lark_allowed = [link.token for link in references[:MAX_LINKS]]
    if len(references) > MAX_LINKS:
        resolved.notes.append(
            f"Only the first {MAX_LINKS} Lark links can be opened per message."
        )


def thread_link_candidates(transport, payload, owner, remote, quoted):
    """Find the requester's links in a same-chat quote or earlier live thread messages."""
    _, _, requester = owner.partition(":")
    if not requester:
        return []

    def own_links(message):
        if (
            message
            and not message.assistant
            and message.sender_id == requester
            and message.chat_id == payload.get("chat")
        ):
            return links(message.text)
        return []

    if references := own_links(quoted):
        return references
    if not payload.get("in_thread"):
        return []
    current_ids = {*(payload.get("message_ids") or ()), payload.get("message_id")}
    for index, message in enumerate(remote):
        if message["message_id"] in current_ids:
            for earlier in reversed(remote[:index]):
                if earlier["owner"].partition(":")[2] == requester:
                    if references := links(earlier["text"]):
                        return references
            break
    root = payload.get("thread")
    if root and root not in current_ids and root != payload.get("reply_to"):
        try:
            return own_links(transport.fetch_message(root))
        except Exception:
            pass
    return []


def resolve(
    transport, payload, lark_in_group=False, *, owner="", remote=(), lark_thread=False
):
    """Everything a prepare job's message carries, as a Resolved. Never raises."""
    resolved = Resolved()
    used = 0
    for value in payload.get("attachments") or ():
        attachment = Attachment.load(value)
        if attachment is None:
            continue
        try:
            if attachment.kind == IMAGE:
                read_image(transport, attachment, resolved)
            elif attachment.kind == FILE:
                used = read_file(transport, attachment, resolved, used)
            elif attachment.kind in (AUDIO, VIDEO):
                resolved.notes.append(
                    f"{label(attachment)} was not processed: {attachment.kind} messages "
                    "are not supported yet."
                )
        except ResourceTooLarge:
            limit = MAX_IMAGE_BYTES if attachment.kind == IMAGE else MAX_FILE_BYTES
            resolved.notes.append(
                f"{label(attachment)} was not read: it is larger than {limit // 1_000_000} MB."
            )
        except Exception:
            # Our own errors name an operation and a Lark code, never content.
            resolved.notes.append(f"{label(attachment)} could not be downloaded.")
    for message_id in payload.get("forwards") or ():
        try:
            fetched = transport.fetch_message(message_id)
        except Exception:
            fetched = None
        if fetched and fetched.text.strip():
            resolved.forwarded.append(fetched.text[:MAX_FORWARD_CHARS])
        else:
            resolved.notes.append("A forwarded conversation could not be read.")
    reply_to = payload.get("reply_to")
    fetched = None
    if reply_to:
        try:
            fetched = transport.fetch_message(reply_to)
        except Exception:
            fetched = None
        if fetched and (fetched.text.strip() or fetched.attachments):
            text = fetched.text.strip() or "(an attachment)"
            resolved.quote = dict(
                text=text[:MAX_QUOTE_CHARS], assistant=bool(fetched.assistant)
            )
            resolved.quote_session = getattr(fetched, "session", "") or ""
    note_links(payload, resolved, lark_in_group)
    if lark_thread and not resolved.lark_links:
        references = thread_link_candidates(transport, payload, owner, remote, fetched)
        resolved.lark_thread_links = references[:MAX_LINKS]
    return resolved
