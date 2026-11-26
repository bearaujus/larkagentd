import pytest
from gb_fakes import FakeReader

from larkagentd.knowledge.lark_sources import CliGroupSource, SourceError, verified_link
from larkagentd.toolservice.larkcli import LarkError


@pytest.mark.parametrize(
    "url",
    [
        None,
        42,
        "http://applink.feishu.cn/x",
        "https://applink.feishu.cn.evil.example/x",
        "https://user@applink.feishu.cn/x",
        "https://applink.feishu.cn:444/x",
        "https://applink.feishu.cn/invite/x",
        "https://applink.feishu.cn/share/x",
        "https://applink.feishu.cn/client/message/link/open?chat_id=oc_wrong12345",
        "https://applink.feishu.cn/client/chat/open?openChatId=oc_wrong12345",
        "https://applink.feishu.cn/client/web_url/open?url=https%3A%2F%2Fexample.com",
        "https://applink.feishu.cn/client/message/link/open/../../web_url/open",
        "https://applink.feishu.cn/",
    ],
)
def test_untrusted_links_rejected(url):
    assert verified_link(url, chat_id="oc_example1234") is None


def test_verified_link_kept_verbatim():
    link = "https://applink.feishu.cn/client/message/link/open?chat_id=oc_example1234"
    assert verified_link(link, chat_id="oc_example1234") == link


def test_parses_raw_messages_and_counts_bad_records():
    raw = {
        "items": [
            {
                "id": "om_example0001",
                "chat_id": "oc_example1234",
                "create_time": "1700000000000",
                "sender": {"open_id": "ou_example1234", "type": "user"},
                "msg_type": "post",
                "content": '{"zh_cn":{"title":"heading","content":[[{"tag":"text","text":"body"}]]}}',
                "message_app_link": "https://applink.feishu.cn/client/message/link/open?token=example",
            },
            {
                "id": "om_example0002",
                "create_time": 1700000000,
                "content": '{"text":"second"}',
                "msg_type": "text",
            },
            {"id": "bad", "create_time": "bad", "content": "bad"},
            {
                "id": "om_example0003",
                "create_time": "1700000000",
                "content": "gone",
                "deleted": True,
            },
        ],
        "has_more": True,
        "page_token": "next_1",
    }
    source = CliGroupSource(FakeReader(raw))
    page = source.list_messages("oc_example1234")
    assert len(page.messages) == 2
    assert page.messages[0].created_at.timestamp() == 1700000000
    assert page.messages[0].text == "heading body"
    assert page.messages[1].sender_id is None
    assert source.skipped == 2
    assert page.next_page_token == "next_1" and not page.complete


def test_lark_errors_map_without_leaking():
    source = CliGroupSource(FakeReader(error=LarkError("forbidden")))
    with pytest.raises(SourceError) as caught:
        source.list_messages("oc_example1234")
    assert caught.value.code == "forbidden"
    assert str(caught.value) == "forbidden"
