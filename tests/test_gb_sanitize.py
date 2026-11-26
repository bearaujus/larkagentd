"""The Group Brain sanitizer: what it blocks, that it never echoes a match, what it leaves alone, and
that no input can make it slow (it runs in the Gateway on text any member can write)."""

import base64
import time

import pytest

from larkagentd.knowledge.sanitize import SCAN_LIMIT, sanitize, token_count


def shapes():
    """Sensitive shapes, built at run time so no credential-shaped literal sits in this file."""
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    return {
        "email": "mail " + "person" + "@example.com",
        "bearer": "Bearer " + "x1" * 12,
        "password": "password=" + "secretvalue",
        "password_is": "the password is " + "hunter2value",
        "fullwidth_colon": "password\uff1a" + "secretvalue",
        "passwd": "passwd: " + "secretvalue",
        "client_secret": "client_secret = " + "secretvalue",
        "app_secret": "app_secret: " + "secretvalue",
        "authorization": "Authorization: " + "Basic " + "dXNlcjpwYXNz",
        "sk": "sk-" + "x" * 24,
        "aws": key,
        "aliyun": "LTAI" + "x" * 16,
        "github": "ghp_" + "a1" * 20,
        "slack": "xox" + "b-" + "1234567890-abcdef",
        "private_key": "-----BEGIN "
        + "PRIVATE KEY-----\nvalue\n-----END PRIVATE KEY-----",
        "dsn": "postgresql://user:" + "hidden" + "@example.com/db",
        "dsn_driver": "postgresql+psycopg://user:" + "hidden" + "@example.com/db",
        "https_userinfo": "https://user:" + "tokenvalue" + "@example.com/x",
        "jdbc": "jdbc:mysql://example.com/db?user=a&password=" + "hidden",
        "jwt": "eyJ" + "a" * 10 + ".eyJ" + "b" * 10 + "." + "c" * 10,
        "long_token": "Ab1" + "x" * 30,
        "card": "1234567890123456",
        "phone": "+1 (212) 555-0101",
        "zero_width": "pass\u200bword: " + "secretvalue",
        "fullwidth_letters": "ｐａｓｓｗｏｒｄ: " + "secretvalue",
        "base64": "config "
        + base64.b64encode(("password=" + "secretvalue").encode()).decode(),
    }


@pytest.mark.parametrize("name", list(shapes()))
def test_a_sensitive_shape_is_blocked_whole_and_never_echoed(name):
    sample = shapes()[name]
    result = sanitize(sample)
    assert result.blocked and result.text is None and result.reasons
    assert sample not in repr(result) and sample not in " ".join(result.reasons)


@pytest.mark.parametrize(
    "text",
    [
        "Bob fixed it with the GTID_PURGED setting before the restore",
        "see basic configuration and the token management guide",
        "we rotate the access key every quarter and keep no secret in chat",
        "partition dt=2026-01-01 failed in table dwd_orders_di after the 3rd retry",
        "ping om_abcdef1234567890 in the thread about retries",
        "the document token is stored in the vault, not here",
    ],
)
def test_ordinary_engineering_talk_is_kept_as_it_is(text):
    result = sanitize(text)
    assert not result.blocked and result.text == text


def test_text_is_normalized_and_cut_and_what_is_kept_is_what_was_scanned():
    assert sanitize(" \n\t ").reasons == ("empty",)
    assert sanitize(" a  b\n c ").text == "a b c"
    assert sanitize("abcdef", max_chars=3).text == "abc"
    assert sanitize("abcdef", max_chars=3).reasons == ("truncated",)
    assert sanitize("caf\u00e9 \u200bok").text == "caf\u00e9 ok"
    assert sanitize("\u200b\u200b").reasons == ("empty",)
    assert token_count("abcd") == 2


def test_a_secret_after_the_scan_limit_is_never_kept():
    padded = "word " * (SCAN_LIMIT // 5 + 50) + "password=" + "secretvalue"
    result = sanitize(padded, max_chars=SCAN_LIMIT)
    assert result.text is None or "secretvalue" not in result.text


@pytest.mark.parametrize(
    "text",
    [
        "a." * 75_000,
        "a.a" * 50_000 + "@",
        "1 " * 75_000,
        "+" + "1-" * 75_000,
        "password " * 20_000,
        "Bearer " + "a" * 150_000,
        "token " + "a" * 150_000,
        "x" * 150_000,
        "A1" * 75_000,
        "eyJ" + "a" * 150_000 + ".eyJ",
        "AAAA" * 40_000,
        "://" * 50_000,
        "a:" * 75_000 + "@",
    ],
)
def test_no_input_makes_the_sanitizer_slow(text):
    started = time.perf_counter()
    sanitize(text)
    assert time.perf_counter() - started < 1.5
