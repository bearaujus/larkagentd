"""What an incoming Lark message says and what it carries.

Pure: no network. Downloading an image or file, and reading a forwarded thread or a
replied-to message, need the Lark API and happen later in a worker.

Lark sends a message as a rich-text "post" as soon as it has any formatting (a code span, bold,
a link, a list), so a chat assistant that only reads "text" rejects ordinary questions.
"""

import json
import re
from dataclasses import dataclass, field

from ..core.diagnostics import emit
from .links import separate_glued_links

IMAGE, FILE, AUDIO, VIDEO = "image", "file", "audio", "video"
MAX_CARD_LINES = 12


@dataclass(frozen=True)
class Attachment:
    """A resource that belongs to one message and is downloaded with that message's id."""

    kind: str
    key: str
    name: str = ""
    message_id: str = ""

    def asdict(self):
        return dict(
            kind=self.kind, key=self.key, name=self.name, message_id=self.message_id
        )

    @classmethod
    def load(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("key"), str):
            return None
        fields = {k: value.get(k) for k in ("kind", "name", "message_id")}
        if not all(isinstance(v, str) for v in fields.values() if v is not None):
            return None
        return cls(
            kind=fields["kind"] or FILE,
            key=value["key"],
            name=fields["name"] or "",
            message_id=fields["message_id"] or "",
        )


@dataclass
class Fetched:
    """A message read back from Lark: the one a user replied to, or a forwarded thread."""

    text: str
    assistant: bool = False
    attachments: list = field(default_factory=list)
    # The session key of the conversation the message belongs to: the thread root, else the thread id,
    # else the message itself (the same rule as `session_of`). Empty when unknown.
    session: str = ""


class ResourceTooLarge(RuntimeError):
    """A download is bigger than the caller will accept."""


@dataclass
class Inbound:
    kind: str  # the Lark message type, lowercased
    text: str = ""
    attachments: list = field(default_factory=list)

    @property
    def empty(self):
        return not self.text.strip() and not self.attachments


def load(content):
    try:
        body = json.loads(content) if content else {}
    except (TypeError, ValueError):
        return {"text": content} if isinstance(content, str) else {}
    return body if isinstance(body, dict) else {}


def clean(value):
    return value.strip() if isinstance(value, str) else ""


def style_has(style, name):
    if isinstance(style, dict):
        return bool(style.get(name))
    if isinstance(style, (list, tuple, set)):
        return name in style
    return False


def mention_names(mentions):
    names = {}
    for mention in mentions or ():
        key, name = getattr(mention, "key", None), getattr(mention, "name", None)
        if isinstance(key, str) and key:
            names[key] = name if isinstance(name, str) and name else "user"
    return names


def with_names(text, names):
    """Lark writes a mention as a placeholder (`@_user_1`); put the person's name back."""
    for key, name in sorted(names.items(), key=lambda item: -len(item[0])):
        text = text.replace(key, "@" + name)
    return text.replace("@_all", "@all")


def bot_mention_names(mentions, bot_id):
    """The names the bot itself was mentioned by; an empty list means it was not mentioned.

    Only the bot's own open id counts: a mention of anyone else, or of everyone, does not."""
    names = []
    for mention in mentions or ():
        who = getattr(mention, "id", None)
        if bot_id and getattr(who, "open_id", None) == bot_id:
            name = getattr(mention, "name", None)
            names.append(name if isinstance(name, str) and name else "user")
    return names


def without_names(text, names):
    """The text with `@name` removed for each name (the bot's own mention is not a question)."""
    for name in sorted(set(names), key=len, reverse=True):
        text = text.replace("@" + name, "")
    return text.strip()


TAKEOVER_EN = re.compile(
    r"^(?:(?:ok(?:ay)?|please|pls|then|can you|could you)[,\s]+)*"
    r"(?:(?:re-?)?run(?:\s+(?:it|that|this|the\s+(?:last|previous)\s+(?:request|query|one)))?"
    r"(?:\s+again)?\s+with|use|try(?:\s+with)?|retry\s+with|switch\s+to)\s+my(?:\s+own)?\s+"
    r"(?:auth(?:entication|orization)?|access|credentials?|login|sign-?in|account|role|"
    r"permissions?)(?:\s+(?:instead|please|then))?[\s.!?]*$",
    re.IGNORECASE,
)
TAKEOVER_ID = re.compile(
    r"^(?:tolong\s+)?(?:pakai|pake|gunakan)\s+(?:akses|auth|akun|kredensial|login|role|izin)"
    r"\s+(?:saya|aku|gue|gw|ku)(?:\s+(?:aja|saja|dong|ya))*[\s.!?]*$",
    re.IGNORECASE,
)


def takes_over(text):
    """True when the whole message only asks to run the previous request with the sender's
    own access ("use my auth"). A longer sentence that merely contains those words is not."""
    text = text.strip()
    return bool(TAKEOVER_EN.match(text) or TAKEOVER_ID.match(text))


# A whole message that only asks to connect, sign in, switch or leave the account.
# Anchored like the takeover patterns above, so a longer sentence that merely contains these words
# ("how do I connect to the database") is not caught; anything freer is left to the model, which has
# the open_setup tool.
_OWNER = r"(?:my|the|a|another|new|different|own)"
_PLACE = r"(?:account|work\s+account|cloud)"
_THING = (
    r"(?:account|access|auth(?:entication|orization)?|credentials?|login|sign-?in|"
    r"connection|role|permissions?|settings?)"
)
SETUP_EN = re.compile(
    r"^(?:(?:ok(?:ay)?|hi|hey|hello|please|pls|then|can you|could you|can i|i want to|"
    r"i would like to|i'd like to|i need to|let me|help me|just)[,\s]+)*"
    r"(?:"
    rf"(?:set\s*up|setup|connect|re-?connect|log\s*in|login|re-?login|sign\s*in|re-?sign\s*in|"
    rf"(?:re-?)?authenticate)(?:\s+(?:to|with|into|in))?(?:\s+{_OWNER})?(?:\s+{_PLACE})*(?:\s+{_THING})?"
    rf"|(?:switch|change|use\s+another|use\s+a\s+different)(?:\s+(?:to|my|the))?(?:\s+(?:another|new|different))?"
    rf"(?:\s+{_PLACE})*\s+{_THING}"
    rf"|(?:my\s+)?(?:{_PLACE}\s+)?(?:access|login|sign-?in|credentials?|connection)\s+"
    r"(?:has\s+|is\s+)?(?:expired|ended|stopped\s+working|(?:is\s+)?not\s+working|isn't\s+working)"
    rf"|(?:disconnect|log\s*out|sign\s*out)(?:\s+(?:from|my|the))*(?:\s+{_PLACE})*(?:\s+{_THING})?"
    r")(?:\s+(?:please|pls|now|again|first))*[\s.!?]*$",
    re.IGNORECASE,
)
SETUP_ID = re.compile(
    r"^(?:(?:tolong|bantu|mau|saya\s+mau|aku\s+mau|gue\s+mau|pengen|ingin|bisa)\s+)*"
    r"(?:"
    r"(?:hubungkan|sambungkan|koneksikan|setup|atur|login)(?:\s+(?:ulang|lagi))?(?:\s+(?:ke|dengan))?"
    r"(?:\s+(?:akun|akses|auth|account))*(?:\s+(?:saya|aku|gue|gw|ku))?"
    r"|(?:ganti|ubah|pindah)\s+(?:akun|akses|auth|role|login|kredensial)(?:\s+(?:saya|aku|gue|gw|ku))?"
    r"|(?:akses|login|akun)(?:\s+(?:saya|aku|gue|gw|ku))?\s+(?:sudah\s+|udah\s+)?"
    r"(?:expired|kedaluwarsa|habis|mati|berakhir)"
    r"|(?:putuskan|logout)(?:\s+(?:akun|akses|koneksi))?(?:\s+(?:saya|aku|gue|gw|ku))?"
    r")(?:\s+(?:dong|ya|aja|saja|deh|sekarang))*[\s.!?]*$",
    re.IGNORECASE,
)


def wants_setup(text):
    """True when the whole message only asks to connect, sign in, switch or leave the account."""
    text = text.strip()
    return bool(text and (SETUP_EN.match(text) or SETUP_ID.match(text)))


def code_block(language, code):
    code = code.replace("\r\n", "\n").strip("\n")
    language = "".join(c for c in language if c.isalnum() or c in "+-_#.")
    return f"```{language}\n{code}\n```"


def post_body(body):
    """The post itself: Lark may nest it under a locale key."""
    if isinstance(body.get("content"), list):
        return body
    return next(
        (v for v in body.values() if isinstance(v, dict) and "content" in v), {}
    )


STYLE_MARKS = (
    (("bold",), "**"),
    (("italic",), "*"),
    (("lineThrough", "line_through", "strikethrough"), "~~"),
)


def styled(text, style):
    """Text with its formatting kept as markdown: **bold**, *italic*, ~~struck~~, `code`.

    Spaces stay outside the marks, or markdown would not apply them.
    """
    core = text.strip()
    if not core:
        return text
    if style_has(style, "code"):
        shown = f"`{core}`"
    else:
        shown = core
        for names, mark in STYLE_MARKS:
            if any(style_has(style, name) for name in names):
                shown = f"{mark}{shown}{mark}"
    return text[: len(text) - len(text.lstrip())] + shown + text[len(text.rstrip()) :]


def post_element(element, names, attachments, message_id):
    if not isinstance(element, dict):
        return ""
    tag = clean(element.get("tag")).lower()
    text = element.get("text")
    text = text if isinstance(text, str) else ""
    if tag == "text":
        return styled(text, element.get("style"))
    if tag == "md":
        return text
    if tag == "code":
        return f"`{text}`" if text else ""
    if tag in ("code_block", "pre"):
        language = clean(element.get("language")) or clean(element.get("lang"))
        return "\n" + code_block(language, text) + "\n"
    if tag == "a":
        href = clean(element.get("href"))
        return text if not href or href == text else f"{text} ({href})".strip()
    if tag == "at":
        placeholder = clean(element.get("user_id"))
        if placeholder == "@_all":
            return "@all"
        name = names.get(placeholder) or clean(element.get("user_name"))
        return f"@{name}" if name else ""
    if tag in ("img", "image"):
        key = clean(element.get("image_key"))
        if key:
            attachments.append(Attachment(IMAGE, key, "", message_id))
        return ""
    if tag in ("media", "file", "audio", "video"):
        key = clean(element.get("file_key"))
        name = clean(element.get("file_name")) or clean(element.get("title"))
        if key:
            kind = tag if tag in (AUDIO, VIDEO) else (VIDEO if tag == "media" else FILE)
            attachments.append(Attachment(kind, key, name, message_id))
        return ""
    if tag == "hr":
        return "\n---\n"
    if tag in ("emotion", "emoji"):
        label = clean(element.get("emoji_type")) or text
        return f":{label}:" if label else ""
    return ""


def post(body, names, message_id):
    body = post_body(body)
    attachments, lines = [], []
    title = clean(body.get("title"))
    if title:
        lines.append(title)
    for paragraph in body.get("content") or []:
        if not isinstance(paragraph, list):
            continue
        lines.append(
            "".join(post_element(e, names, attachments, message_id) for e in paragraph)
        )
    return "\n".join(lines).strip(), attachments


def card_lines(value, found):
    """The readable strings of an interactive card, in order."""
    if len(found) >= MAX_CARD_LINES:
        return
    if isinstance(value, dict):
        content = value.get("content")
        if (
            isinstance(content, str)
            and content.strip()
            and clean(value.get("tag"))
            in (
                "plain_text",
                "lark_md",
                "markdown",
                "text",
            )
        ):
            found.append(content.strip())
        for item in value.values():
            card_lines(item, found)
    elif isinstance(value, list):
        for item in value:
            card_lines(item, found)


def card(body):
    found = []
    card_lines(body.get("card") if isinstance(body.get("card"), dict) else body, found)
    return "\n".join(dict.fromkeys(found))


def normalize(message_type, content, message_id="", mentions=()):
    """The words and attachments of a message. Words are empty for a type with none. A Lark link
    glued to the next word gets a space after its token (see `separate_glued_links`)."""
    inbound = _normalize(message_type, content, message_id, mentions)
    separated = separate_glued_links(inbound.text)
    if separated != inbound.text:
        # Names the shape only: never the text or the token.
        emit("inbound.link_separated", message_type=inbound.kind)
    inbound.text = separated
    return inbound


def _normalize(message_type, content, message_id="", mentions=()):
    kind = clean(message_type).lower()
    body = load(content)
    names = mention_names(mentions)
    if kind == "text":
        text = body.get("text")
        return Inbound(kind, with_names(text, names) if isinstance(text, str) else "")
    if kind == "post":
        text, attachments = post(body, names, message_id)
        return Inbound(kind, text, attachments)
    if kind == "image":
        key = clean(body.get("image_key"))
        return Inbound(
            kind, "", [Attachment(IMAGE, key, "", message_id)] if key else []
        )
    if kind in ("file", "audio", "media"):
        key = clean(body.get("file_key"))
        name = clean(body.get("file_name"))
        resource = VIDEO if kind == "media" else kind
        return Inbound(
            kind, "", [Attachment(resource, key, name, message_id)] if key else []
        )
    if kind == "sticker":
        return Inbound(kind, "[The user sent a sticker.]")
    if kind == "share_chat":
        name = clean(body.get("chat_name")) or clean(body.get("name"))
        return Inbound(kind, f"[The user shared a chat: {name}]" if name else "")
    if kind in ("interactive", "card"):
        return Inbound(kind, card(body))
    # merge_forward has its content in other messages; the worker fetches it by id.
    return Inbound(kind)


MAX_FORWARD_MESSAGES = 40
MAX_FORWARD_LINE = 500


def forward_text(items, message_id):
    """A forwarded conversation as `Person N: words` lines, in the order Lark returns them.

    Lark returns the forward and every message inside it in one list; the inner ones name the
    forward as their upper message. People are numbered by first appearance: the list carries
    ids, not names.
    """
    people, lines = {}, []
    for item in items:
        if getattr(item, "upper_message_id", None) != message_id:
            continue
        sender = getattr(getattr(item, "sender", None), "id", None) or "?"
        number = people.setdefault(sender, len(people) + 1)
        body = getattr(item, "body", None)
        inner = normalize(
            item.msg_type,
            body.content if body else "",
            item.message_id,
            getattr(item, "mentions", None) or (),
        )
        text = inner.text.strip() or (
            "[a forwarded conversation]"
            if inner.kind == "merge_forward"
            else "[an attachment]"
            if inner.attachments
            else ""
        )
        if text:
            lines.append(
                f"Person {number}: {' '.join(text.split())[:MAX_FORWARD_LINE]}"
            )
        if len(lines) >= MAX_FORWARD_MESSAGES:
            break
    return "\n".join(lines)


def session_of(message):
    """(the session a message belongs to, whether it was typed inside a thread).

    A thread is a session. Its key is the message that started it: Lark gives every message of a
    thread that message as `root_id`, next to a `thread_id`. A top-level message is the start of
    a new thread (the bot answers it in one), so it is its own key. A quote-reply in the main
    chat also has a `root_id` but no thread, so only `thread_id` marks a thread.
    """
    thread_id = getattr(message, "thread_id", None)
    if isinstance(thread_id, str) and thread_id:
        root = getattr(message, "root_id", None)
        return (root if isinstance(root, str) and root else thread_id), True
    return message.message_id, False
