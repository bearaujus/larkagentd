import json

import pytest

from larkagentd.lark.inbound import normalize
from larkagentd.lark.links import links, parse_link

DOC = "https://example.larksuite.com/docx/ExampleDocumentToken"


@pytest.mark.parametrize(
    "host",
    [
        "example.larksuite.com",
        "example.sg.larksuite.com",
        "example.feishu.cn",
        "example.larkoffice.com",
    ],
)
def test_only_known_https_document_urls_are_accepted(host):
    ref = parse_link(f"https://{host}/docx/ExampleDocumentToken?from=share#part")
    assert ref.kind == "docx" and ref.token == "ExampleDocumentToken"
    assert ref.url == f"https://{host}/docx/ExampleDocumentToken"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.larksuite.com/docx/ExampleDocumentToken",
        "https://example.larksuite.com.evil.invalid/docx/ExampleDocumentToken",
        "https://evillarksuite.com/docx/ExampleDocumentToken",
        "https://user:password@example.larksuite.com/docx/ExampleDocumentToken",
        "https://example.larksuite.com:444/docx/ExampleDocumentToken",
        "https://127.0.0.1/docx/ExampleDocumentToken",
        "file:///docx/ExampleDocumentToken",
        "https://example.larksuite.com/docx/../../admin",
        "https://example.larksuite.com/docx/Example%2fDocumentToken",
        "https://example.larksuite.com/docx/short",
        "https://example.larksuite.com/not-a-doc/ExampleDocumentToken",
    ],
)
def test_untrusted_and_malformed_links_are_not_followed(url):
    assert parse_link(url) is None


def test_rich_link_survives_normalizing_and_duplicates_are_listed_once():
    value = normalize(
        "post",
        json.dumps(
            {"content": [[{"tag": "a", "text": "Example handbook", "href": DOC}]]}
        ),
    )
    assert len(links(value.text + " " + DOC + "#section")) == 1


@pytest.mark.parametrize("kind", ["docx", "wiki", "sheets", "base"])
def test_the_kinds_lark_read_can_open_are_found(kind):
    url = f"https://example.larksuite.com/{kind}/ExampleDocumentToken"
    assert [link.kind for link in links("see " + url)] == [kind]


@pytest.mark.parametrize("kind", ["doc", "docs", "mindnotes", "file"])
def test_a_kind_lark_read_cannot_open_is_not_recorded_as_allowed(kind):
    assert links(f"https://example.larksuite.com/{kind}/ExampleDocumentToken") == []


@pytest.mark.parametrize(
    "text",
    [
        # The shape Lark sends for a pasted link followed by words: the normalizer writes
        # "title (url)" and the next element starts right after the bracket.
        f"can you read this lark doc Summary Analysis ({DOC})and summarize it for me",
        f"read [Summary Analysis]({DOC})now",
        f"请看 {DOC}，然后总结",
        f"请看（{DOC}）然后总结",
        f"see <{DOC}>and tell me",
        f"first {DOC}. Then summarize",
        f"请看{DOC}请总结",
        f"{DOC}然后总结",
        f'<a href="{DOC}">Summary Analysis</a>and summarize',
        f"look:{DOC}\nthanks",
    ],
)
def test_a_link_is_found_when_text_follows_it_without_a_space(text):
    assert [link.token for link in links(text)] == ["ExampleDocumentToken"]


def test_a_rich_link_followed_by_words_is_found_from_the_real_message_shape():
    post = {
        "content": [
            [
                {"tag": "text", "text": "can you read this lark doc "},
                {"tag": "a", "text": "Summary Analysis", "href": DOC},
                {"tag": "text", "text": "and summarize it for me"},
            ]
        ]
    }
    text = normalize("post", json.dumps(post)).text
    assert text.endswith("and summarize it for me")
    assert [link.token for link in links(text)] == ["ExampleDocumentToken"]


# --- a link chip followed by words arrives with no separator (seen in a real chat) ---

TOKEN27 = "ExampleToken0123456789abcde"
assert len(TOKEN27) == 27


@pytest.mark.parametrize("kind", ["docx", "wiki", "sheets", "base"])
def test_a_link_glued_to_the_next_word_keeps_its_27_character_token(kind):
    url = f"https://example.larksuite.com/{kind}/{TOKEN27}"
    sent = json.dumps(
        {"text": f"can you read this lark doc {url}and summarize it for me"}
    )
    text = normalize("text", sent).text
    assert text == f"can you read this lark doc {url} and summarize it for me"
    assert [link.token for link in links(text)] == [TOKEN27]


@pytest.mark.parametrize(
    "words", ["and summarize it", "then", "Summarize", "2nd copy", "x_y-z"]
)
def test_whatever_word_follows_the_token_is_split_off(words):
    url = f"https://example.larksuite.com/docx/{TOKEN27}"
    text = normalize("text", json.dumps({"text": url + words})).text
    assert [link.token for link in links(text)] == [TOKEN27]


@pytest.mark.parametrize(
    "sent",
    [
        f"https://example.larksuite.com/docx/{TOKEN27}",  # already exactly 27
        f"https://example.larksuite.com/docx/{TOKEN27} and more",  # already separated
        f"https://example.larksuite.com/docx/{TOKEN27}?from=share and more",
        f"https://example.larksuite.com/docx/{TOKEN27[:20]}",  # a shorter token
        f"https://example.invalid/docx/{TOKEN27}and more",  # not a Lark host
        f"https://example.larksuite.com/other/{TOKEN27}and more",  # not a supported kind
        f"see https://example.com/page/{TOKEN27}and more",
    ],
)
def test_text_that_is_not_a_glued_lark_link_is_left_alone(sent):
    assert normalize("text", json.dumps({"text": sent})).text == sent


def test_a_rich_post_with_a_glued_link_is_separated_too():
    url = f"https://example.larksuite.com/docx/{TOKEN27}"
    post = {"content": [[{"tag": "text", "text": f"read {url}and tell me"}]]}
    text = normalize("post", json.dumps(post)).text
    assert [link.token for link in links(text)] == [TOKEN27]
    assert f"{TOKEN27} and tell me" in text


def test_separating_a_glued_link_is_logged_without_the_text_or_the_token(caplog):
    caplog.set_level("INFO")
    url = f"https://example.larksuite.com/docx/{TOKEN27}"
    normalize("text", json.dumps({"text": f"private words {url}and more"}))
    assert "inbound.link_separated" in caplog.text
    assert TOKEN27 not in caplog.text and "private words" not in caplog.text
    caplog.clear()
    normalize("text", json.dumps({"text": f"{url} and more"}))
    assert "inbound.link_separated" not in caplog.text
