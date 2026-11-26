import json
from types import SimpleNamespace as N

import pytest

from larkagentd.lark.inbound import Attachment, forward_text, normalize


def words(message_type, body, **kwargs):
    return normalize(message_type, json.dumps(body), "m1", **kwargs)


def para(*elements):
    return [list(elements)]


def text(value, **extra):
    return {"tag": "text", "text": value, **extra}


# --- what a user typed ---


def test_plain_text_is_unchanged_and_a_missing_field_is_empty():
    assert words("text", {"text": "hi there"}).text == "hi there"
    assert words("text", {}).text == ""


def test_a_mention_placeholder_gets_the_persons_name_back():
    mentions = [N(key="@_user_1", name="Dana"), N(key="@_user_10", name="Lee")]
    result = words(
        "text", {"text": "@_user_1 and @_user_10 and @_all"}, mentions=mentions
    )
    assert result.text == "@Dana and @Lee and @all"


def test_formatting_makes_lark_send_a_post_and_the_words_survive():
    body = {
        "title": "",
        "content": para(
            text("move the "),
            text("p_example.demo.sample_table", style=["code"]),
            text(" run earlier?"),
        ),
    }
    assert (
        words("post", body).text
        == "move the `p_example.demo.sample_table` run earlier?"
    )


def test_code_style_is_read_from_a_list_or_a_dict():
    for style in (["code"], {"code": True}):
        assert words("post", {"content": para(text("x", style=style))}).text == "`x`"
    assert (
        words("post", {"content": para(text("x", style={"code": False}))}).text == "x"
    )


def test_a_question_with_a_bold_table_name_is_an_ordinary_question():
    """The case that was rejected: plain words around a table name in bold."""
    body = {
        "title": "",
        "content": para(
            text("please analyse if I preponed the "),
            text("p_example.demo.sample_table", style=["bold"]),
            text(" run, what is the safest time?"),
        ),
    }
    assert words("post", body).text == (
        "please analyse if I preponed the **p_example.demo.sample_table** "
        "run, what is the safest time?"
    )


@pytest.mark.parametrize(
    "style,expected",
    [
        (["bold"], "**x**"),
        (["italic"], "*x*"),
        (["lineThrough"], "~~x~~"),
        (["bold", "italic"], "***x***"),
        ({"bold": True, "underline": True}, "**x**"),  # underline has no markdown
        (["underline"], "x"),
    ],
)
def test_bold_italic_and_strikethrough_are_kept_as_markdown(style, expected):
    assert words("post", {"content": para(text("x", style=style))}).text == expected


def test_spaces_stay_outside_the_marks_so_markdown_still_applies():
    body = {"content": para(text("a"), text(" big ", style=["bold"]), text("b"))}
    assert words("post", body).text == "a **big** b"
    # a bold run of only spaces is left as spaces
    body = {"content": para(text("a"), text("  ", style=["bold"]), text("b"))}
    assert words("post", body).text == "a  b"


def test_markdown_the_user_typed_into_plain_text_is_left_alone():
    typed = "analyse **p_example.demo.sample_table** and `other_table`\n- one\n- two"
    assert words("text", {"text": typed}).text == typed
    assert words("post", {"content": para({"tag": "md", "text": typed})}).text == typed


def test_paragraphs_become_lines_and_a_title_comes_first():
    body = {"title": "Request", "content": [[text("one")], [text("two")]]}
    assert words("post", body).text == "Request\none\ntwo"


def test_a_post_may_be_nested_under_a_locale_key():
    body = {"en_us": {"title": "", "content": para(text("hello"))}}
    assert words("post", body).text == "hello"


def test_a_link_keeps_its_address_unless_the_address_is_the_text():
    body = {
        "content": para(
            {"tag": "a", "text": "the doc", "href": "https://example.com/d"},
            text(" and "),
            {
                "tag": "a",
                "text": "https://example.com/x",
                "href": "https://example.com/x",
            },
        )
    }
    assert (
        words("post", body).text
        == "the doc (https://example.com/d) and https://example.com/x"
    )


def test_a_pasted_code_block_stays_a_fenced_block():
    body = {
        "content": [
            [text("why does this fail?")],
            [{"tag": "code_block", "language": "sql", "text": "select 1\n"}],
        ]
    }
    assert words("post", body).text == "why does this fail?\n\n```sql\nselect 1\n```"


def test_a_language_cannot_break_out_of_the_fence():
    body = {
        "content": para({"tag": "code_block", "language": "sql\n```\nx", "text": "y"})
    }
    assert "\n```\nx" not in words("post", body).text


def test_mentions_and_emoji_in_a_post():
    body = {
        "content": para(
            {"tag": "at", "user_id": "@_user_1", "user_name": "Someone"},
            text(" status?"),
            {"tag": "emotion", "emoji_type": "SMILE"},
        )
    }
    assert words("post", body, mentions=[N(key="@_user_1", name="Dana")]).text == (
        "@Dana status?:SMILE:"
    )
    assert (
        words("post", {"content": para({"tag": "at", "user_id": "@_all"})}).text
        == "@all"
    )


def test_invalid_text_content_is_read_as_the_text_itself():
    assert (
        normalize("text", "plain words, not json", "m1").text == "plain words, not json"
    )


# --- what a message carries ---


def test_an_image_message_is_an_attachment_with_no_words():
    result = words("image", {"image_key": "img-1"})
    assert result.text == "" and result.attachments == [
        Attachment("image", "img-1", "", "m1")
    ]


def test_images_and_files_inside_a_post_are_attachments_too():
    body = {
        "content": [
            [text("see this")],
            [{"tag": "img", "image_key": "img-2"}],
            [{"tag": "media", "file_key": "vid-1", "file_name": "clip.mp4"}],
        ]
    }
    result = words("post", body)
    assert result.text == "see this"
    assert [(a.kind, a.key, a.name) for a in result.attachments] == [
        ("image", "img-2", ""),
        ("video", "vid-1", "clip.mp4"),
    ]
    assert {a.message_id for a in result.attachments} == {"m1"}


@pytest.mark.parametrize(
    "message_type,kind", [("file", "file"), ("audio", "audio"), ("media", "video")]
)
def test_files_audio_and_video_keep_their_key_and_name(message_type, kind):
    result = words(message_type, {"file_key": "f-1", "file_name": "report.csv"})
    assert [(a.kind, a.key, a.name) for a in result.attachments] == [
        (kind, "f-1", "report.csv")
    ]


def test_an_attachment_without_a_key_is_dropped():
    assert words("image", {}).attachments == []
    assert words("file", {"file_name": "x.csv"}).attachments == []


def test_a_card_is_read_as_its_visible_text():
    card = {
        "header": {"title": {"tag": "plain_text", "content": "Run report"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**3** jobs failed"}},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**3** jobs failed"}},
        ],
    }
    assert words("interactive", card).text == "Run report\n**3** jobs failed"


def test_a_sticker_and_a_shared_chat_say_what_they_are():
    assert "sticker" in words("sticker", {"file_key": "s"}).text
    assert "Data team" in words("share_chat", {"chat_name": "Data team"}).text


def test_a_type_with_no_words_is_empty_not_an_error():
    for kind in ("location", "todo", "share_user", "system", "merge_forward", "weird"):
        result = words(kind, {"anything": 1})
        assert result.empty and result.kind == kind


def test_hostile_payload_shapes_never_raise():
    for content in (
        None,
        "",
        "null",
        "[1]",
        '"x"',
        '{"content": "x"}',
        '{"content": [1, [2], [{}]]}',
    ):
        for kind in ("text", "post", "image", "file", "interactive", "weird"):
            normalize(kind, content, "m1")


# --- attachments survive the job payload ---


def test_an_attachment_round_trips_through_json_and_bad_ones_are_refused():
    original = Attachment("file", "k", "a.csv", "m1")
    assert Attachment.load(json.loads(json.dumps(original.asdict()))) == original
    for bad in (
        None,
        "x",
        {},
        {"key": 1},
        {"key": "k", "name": 5},
        {"key": "k", "kind": []},
    ):
        assert Attachment.load(bad) is None
    assert Attachment.load({"key": "k"}).kind == "file"


# --- a forwarded conversation ---


def item(message_id, upper, sender, msg_type, content):
    return N(
        message_id=message_id,
        upper_message_id=upper,
        sender=N(id=sender),
        msg_type=msg_type,
        body=N(content=json.dumps(content)),
        mentions=None,
    )


def test_a_forwarded_conversation_becomes_numbered_speakers():
    items = [
        item("fwd", None, "app", "merge_forward", {}),
        item("a", "fwd", "ou_1", "text", {"text": "the job failed"}),
        item("b", "fwd", "ou_2", "text", {"text": "since when?"}),
        item(
            "c",
            "fwd",
            "ou_1",
            "post",
            {"content": para(text("since  monday\nmorning"))},
        ),
        item("d", "fwd", "ou_2", "image", {"image_key": "i"}),
        item("e", "other", "ou_3", "text", {"text": "not part of it"}),
    ]
    assert forward_text(items, "fwd").splitlines() == [
        "Person 1: the job failed",
        "Person 2: since when?",
        "Person 1: since monday morning",
        "Person 2: [an attachment]",
    ]


def test_a_long_forward_is_bounded():
    items = [item("fwd", None, "x", "merge_forward", {})] + [
        item(f"m{i}", "fwd", "ou_1", "text", {"text": "y" * 2000}) for i in range(100)
    ]
    lines = forward_text(items, "fwd").splitlines()
    assert len(lines) == 40 and all(len(line) < 520 for line in lines)
