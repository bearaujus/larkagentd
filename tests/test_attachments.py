import base64

from larkagentd.lark.attachments import (
    MAX_FILE_CHARS,
    MAX_FILES,
    MAX_IMAGES,
    MAX_QUOTE_CHARS,
    MAX_TOTAL_CHARS,
    resolve,
)
from larkagentd.lark.inbound import Fetched, ResourceTooLarge

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "


class FakeLark:
    """Serves resources by key and messages by id; a missing key fails like Lark does."""

    def __init__(self, resources=None, messages=None):
        self.resources, self.messages = resources or {}, messages or {}
        self.downloads = []

    def download(self, attachment, limit):
        self.downloads.append((attachment.key, limit))
        value = self.resources[attachment.key]
        if isinstance(value, Exception):
            raise value
        if len(value) > limit:
            raise ResourceTooLarge()
        return value, attachment.name

    def fetch_message(self, message_id):
        value = self.messages[message_id]
        if isinstance(value, Exception):
            raise value
        return value


def attachment(kind, key, name=""):
    return dict(kind=kind, key=key, name=name, message_id="m1")


def test_a_message_with_nothing_attached_resolves_to_nothing():
    resolved = resolve(FakeLark(), dict(text="hi"))
    assert not any(resolved.counts().values())


# --- images ---


def test_images_become_data_urls_by_what_they_are_not_by_their_name():
    lark = FakeLark({"a": PNG, "b": JPEG, "c": WEBP})
    payload = dict(attachments=[attachment("image", k) for k in "abc"])
    urls = resolve(lark, payload).images
    assert [u.split(";")[0] for u in urls] == [
        "data:image/png",
        "data:image/jpeg",
        "data:image/webp",
    ]
    assert base64.b64decode(urls[0].split(",", 1)[1]) == PNG


def test_something_that_is_not_an_image_is_a_note_not_a_data_url():
    resolved = resolve(
        FakeLark({"a": b"<html>not an image</html>"}),
        dict(attachments=[attachment("image", "a")]),
    )
    assert resolved.images == [] and "cannot view" in resolved.notes[0]


def test_only_a_few_images_are_sent_and_the_agent_is_told():
    lark = FakeLark({str(i): PNG for i in range(MAX_IMAGES + 2)})
    payload = dict(
        attachments=[attachment("image", str(i)) for i in range(MAX_IMAGES + 2)]
    )
    resolved = resolve(lark, payload)
    assert len(resolved.images) == MAX_IMAGES
    assert any(f"first {MAX_IMAGES}" in n for n in resolved.notes)
    assert len(lark.downloads) == MAX_IMAGES  # the rest are not even downloaded


# --- files ---


def test_a_text_file_is_read_whole_and_a_big_one_is_cut_and_marked():
    lark = FakeLark({"s": b"select 1;\n", "big": b"x" * (MAX_FILE_CHARS + 500)})
    payload = dict(
        attachments=[
            attachment("file", "s", "query.sql"),
            attachment("file", "big", "dump.csv"),
        ]
    )
    docs = resolve(lark, payload).documents
    assert docs[0] == dict(name="query.sql", text="select 1;\n", cut=False)
    assert docs[1]["cut"] is True and len(docs[1]["text"]) == MAX_FILE_CHARS


def test_files_together_never_exceed_the_total_budget():
    each = MAX_TOTAL_CHARS // 2 + 100
    lark = FakeLark({k: b"y" * each for k in "abc"})
    payload = dict(attachments=[attachment("file", k, f"{k}.txt") for k in "abc"])
    docs = resolve(lark, payload).documents
    assert sum(len(d["text"]) for d in docs) <= MAX_TOTAL_CHARS
    assert docs[-1]["cut"] is True


def test_only_a_few_files_are_read():
    lark = FakeLark({str(i): b"z" for i in range(MAX_FILES + 1)})
    payload = dict(
        attachments=[
            attachment("file", str(i), f"{i}.txt") for i in range(MAX_FILES + 1)
        ]
    )
    resolved = resolve(lark, payload)
    assert len(resolved.documents) == MAX_FILES
    assert any("at most" in n for n in resolved.notes)


def test_a_file_type_we_cannot_read_is_named_in_a_note_and_not_downloaded():
    lark = FakeLark({"p": b"%PDF-1.7"})
    resolved = resolve(lark, dict(attachments=[attachment("file", "p", "report.pdf")]))
    assert resolved.documents == [] and lark.downloads == []
    assert "report.pdf" in resolved.notes[0] and "pdf" in resolved.notes[0]


def test_a_binary_file_with_a_text_name_is_refused():
    resolved = resolve(
        FakeLark({"f": b"PK\x03\x04\x00\x00data"}),
        dict(attachments=[attachment("file", "f", "notes.txt")]),
    )
    assert resolved.documents == [] and "not a text file" in resolved.notes[0]


def test_audio_and_video_are_acknowledged_not_ignored():
    lark = FakeLark()
    payload = dict(
        attachments=[attachment("audio", "a", "v.m4a"), attachment("video", "b")]
    )
    resolved = resolve(lark, payload)
    assert len(resolved.notes) == 2 and lark.downloads == []
    assert "v.m4a" in resolved.notes[0] and "audio" in resolved.notes[0]


# --- failures never fail the turn ---


def test_an_oversized_or_failing_download_becomes_a_note():
    lark = FakeLark(
        {
            "big": PNG + b"\x00" * 9_000_000,
            "bad": RuntimeError("Lark download failed (code=99991672)"),
        }
    )
    payload = dict(
        attachments=[attachment("image", "big"), attachment("file", "bad", "a.csv")]
    )
    notes = resolve(lark, payload).notes
    assert "larger than 8 MB" in notes[0]
    assert notes[1] == "a.csv could not be downloaded."
    assert "99991672" not in " ".join(notes)  # error text never reaches the agent


def test_malformed_attachment_entries_are_skipped():
    payload = dict(attachments=[None, "x", {}, {"key": 5}, attachment("image", "a")])
    assert len(resolve(FakeLark({"a": PNG}), payload).images) == 1


# --- quoted and forwarded messages ---


def test_the_message_being_replied_to_is_attached_and_labelled():
    lark = FakeLark(messages={"p1": Fetched("here are 12 tables", assistant=True)})
    resolved = resolve(lark, dict(reply_to="p1"))
    assert resolved.quote == dict(text="here are 12 tables", assistant=True)


def test_a_long_quote_is_cut_and_an_unreadable_one_is_skipped():
    long = Fetched("q" * (MAX_QUOTE_CHARS + 50))
    assert len(
        resolve(FakeLark(messages={"p": long}), dict(reply_to="p")).quote["text"]
    ) == (MAX_QUOTE_CHARS)
    broken = FakeLark(messages={"p": RuntimeError("Lark message read failed (code=1)")})
    assert resolve(broken, dict(reply_to="p")).quote is None
    assert (
        resolve(FakeLark(messages={"p": Fetched("  ")}), dict(reply_to="p")).quote
        is None
    )


def test_a_quoted_attachment_still_counts_as_a_quote():
    lark = FakeLark(messages={"p": Fetched("", attachments=[object()])})
    assert resolve(lark, dict(reply_to="p")).quote == dict(
        text="(an attachment)", assistant=False
    )


def test_a_forwarded_conversation_is_read_and_a_failure_is_a_note():
    lark = FakeLark(
        messages={
            "f1": Fetched("Person 1: hi"),
            "f2": RuntimeError("Lark message read failed"),
        }
    )
    resolved = resolve(lark, dict(forwards=["f1", "f2"]))
    assert resolved.forwarded == ["Person 1: hi"]
    assert resolved.notes == ["A forwarded conversation could not be read."]


def test_thread_references_are_from_requester_before_current_message():
    document = "https://example.larksuite.com/docx/ExampleDocumentToken"
    other = "https://example.larksuite.com/docx/OtherDocumentToken"
    transport = FakeLark(
        messages={
            "quote": Fetched(other, chat_id="other-chat", sender_id="person"),
            "root": Fetched(other, chat_id="group", sender_id="other"),
        }
    )
    payload = dict(
        group=True,
        in_thread=True,
        chat="group",
        thread="root",
        reply_to="quote",
        message_id="current",
        text="Try again?",
    )
    remote = [
        {"message_id": "previous", "owner": "tenant:person", "text": document},
        {"message_id": "other", "owner": "tenant:other", "text": other},
        {"message_id": "current", "owner": "tenant:person", "text": ""},
        {"message_id": "later", "owner": "tenant:person", "text": other},
    ]
    result = resolve(
        transport, payload, True, owner="tenant:person", remote=remote, lark_thread=True
    )
    assert [link.url for link in result.lark_thread_links] == [document]
    assert not result.lark_allowed
    missing_anchor = resolve(
        transport,
        payload,
        True,
        owner="tenant:person",
        remote=remote[:2],
        lark_thread=True,
    )
    assert not missing_anchor.lark_thread_links
