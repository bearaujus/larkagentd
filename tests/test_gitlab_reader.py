import json
from urllib.parse import unquote

import httpx
import pytest

from larkagentd.toolservice.gitlab import GitlabError, GitlabReader

TOKEN = "REDACTED_TEST_VALUE"
URL = "https://git.example.com"


class API:
    def __init__(self, response=None):
        self.requests = []
        self.response = response

    def __call__(self, request):
        self.requests.append(request)
        if self.response:
            return self.response(request)
        path = request.url.path
        if path.endswith("/merge_requests/3"):
            return httpx.Response(
                200,
                json={
                    "iid": 3,
                    "title": "title",
                    "description": "safe",
                    "author": {"name": "A", "email": "bad"},
                    "labels": [],
                    "state": "opened",
                    "source_branch": "b",
                    "target_branch": "main",
                    "changes_count": "1",
                    "head_pipeline": {"status": "success"},
                },
            )
        if path.endswith("/repository/tree"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])


def make(api=None, clock=None):
    api = api or API()
    client = httpx.Client(transport=httpx.MockTransport(api))
    return GitlabReader(
        URL, TOKEN, client=client, clock=clock or __import__("time").monotonic
    ), api


def test_every_request_uses_get_and_fixed_host_and_token_header():
    reader, api = make()
    reader.mr("group/project", 3)
    assert api.requests
    assert all(r.method == "GET" for r in api.requests)
    assert all(
        r.url.scheme == "https" and r.url.host == "git.example.com"
        for r in api.requests
    )
    assert all("/api/v4/" in r.url.path for r in api.requests)
    assert all(r.headers["PRIVATE-TOKEN"] == TOKEN for r in api.requests)
    assert all(TOKEN not in str(r.url) for r in api.requests)


@pytest.mark.parametrize(
    "url",
    [
        "http://git.example.com",
        "https://git.example.com/path",
        "https://u:p@git.example.com",
        "https://git.example.com?q=x",
        "https://git.example.com/#x",
        "https://git.example.com:invalid",
        "https://git.example.com:70000",
    ],
)
def test_url_must_be_plain_https_origin(url):
    with pytest.raises(ValueError):
        GitlabReader(url, TOKEN)


@pytest.mark.parametrize(
    "method,args,code",
    [
        ("mr", {"project": "group/../project"}, "bad_project"),
        ("mr", {"project": "group/project", "iid": 0}, "bad_iid"),
        ("path", {"project": "group/project", "path": "../x"}, "bad_path"),
        ("path", {"project": "group/project", "ref": r"main\\branch"}, "bad_ref"),
        (
            "compare",
            {"project": "group/project", "base": "a b", "head": "main"},
            "bad_base",
        ),
        ("search", {"query": "x"}, "bad_query"),
        ("search", {"query": "ab", "scope": "blobs"}, "bad_project"),
        ("search", {"query": "ab", "page": 101}, "bad_page"),
    ],
)
def test_inputs_are_validated(method, args, code):
    reader, api = make()
    with pytest.raises(GitlabError) as caught:
        getattr(reader, method)(**args)
    assert str(caught.value) == code
    assert not api.requests


def test_readers_project_diff_commit_and_discussion_shapes():
    def respond(request):
        if request.url.path.endswith("/changes"):
            return httpx.Response(
                200,
                json={
                    "changes": [
                        {
                            "old_path": "a",
                            "new_path": "b",
                            "diff": "safe",
                            "renamed_file": True,
                        }
                    ]
                },
            )
        if request.url.path.endswith("/commits"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "123456789",
                        "title": "commit",
                        "author_name": "A",
                        "committed_date": "today",
                        "author_email": "discard",
                    }
                ],
            )
        if request.url.path.endswith("/discussions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "resolved": True,
                        "notes": [
                            {
                                "body": "comment",
                                "author": {"name": "A", "email": "discard"},
                                "position": {"new_path": "a", "new_line": 4},
                            }
                        ],
                    }
                ],
            )
        return httpx.Response(200, json=[])

    reader, _ = make(API(respond))
    changes = reader.mr("group/project", 3, part="changes")
    commits = reader.mr("group/project", 3, part="commits")
    discussions = reader.mr("group/project", 3, part="discussions")
    assert changes["diffs"][0]["renamed_file"] is True
    assert commits["commits"][0] == {
        "sha": "1234567",
        "title": "commit",
        "author": "A",
        "date": "today",
    }
    assert discussions["discussions"][0]["position"] == {"new_path": "a", "new_line": 4}


def test_path_tree_and_compare_output_shapes():
    def respond(request):
        if request.url.path.endswith("/compare"):
            return httpx.Response(
                200,
                json={
                    "commits": [{"id": "123456789", "title": "c", "author_name": "A"}],
                    "diffs": [],
                },
            )
        return httpx.Response(
            200, json=[{"name": "child", "type": "blob", "path": "folder/child"}]
        )

    reader, _ = make(API(respond))
    tree = reader.path("group/project", "folder", offset=100)
    compare = reader.compare("group/project", "main", "next")
    assert tree["tree"] == [{"name": "child", "type": "blob", "path": "folder/child"}]
    assert tree["next_offset"] is None
    assert compare["commits"][0]["sha"] == "1234567"


def test_project_is_encoded_as_single_path_segment():
    reader, api = make()
    reader.mr("group/sub/project", 3)
    assert "%2F" in api.requests[0].url.raw_path.decode()
    assert unquote(api.requests[0].url.path).endswith(
        "/projects/group/sub/project/merge_requests/3"
    )


def test_token_never_appears_in_error_log_or_result(caplog):
    def fail(request):
        return httpx.Response(500, text=f"upstream {TOKEN}")

    reader, _ = make(API(fail))
    with pytest.raises(GitlabError) as caught:
        reader.search("abc")
    assert str(caught.value) == "gitlab_unavailable"
    assert TOKEN not in str(caught.value) + caplog.text + json.dumps(
        {"error": str(caught.value)}
    )


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (429, "rate_limited"),
        (500, "gitlab_unavailable"),
        (302, "gitlab_unavailable"),
    ],
)
def test_http_failures_have_fixed_codes(status, code):
    reader, _ = make(API(lambda request: httpx.Response(status, text=TOKEN)))
    with pytest.raises(GitlabError) as caught:
        reader.search("abc")
    assert str(caught.value) == code


def test_bad_json_and_oversized_body_are_rejected():
    reader, _ = make(API(lambda request: httpx.Response(200, content=b"not json")))
    with pytest.raises(GitlabError, match="^bad_response$"):
        reader.search("abc")
    reader, _ = make(
        API(lambda request: httpx.Response(200, content=b"[" + b" " * 2_000_001 + b"]"))
    )
    with pytest.raises(GitlabError, match="^bad_response$"):
        reader.search("abc")


def test_timeout_and_transport_errors_are_fixed():
    reader, _ = make(
        API(lambda request: (_ for _ in ()).throw(httpx.ConnectTimeout("secret")))
    )
    with pytest.raises(GitlabError, match="^gitlab_unavailable$"):
        reader.search("abc")


def test_request_and_call_budgets_are_enforced():
    clock = type("Clock", (), {"now": 0.0, "__call__": lambda self: self.now})()

    def api(request):
        clock.now += 61
        return httpx.Response(200, json=[])

    reader, _ = make(API(api), clock)
    with pytest.raises(GitlabError, match="^gitlab_unavailable$"):
        reader.search("abc")
    # A listing can issue at most the configured request cap; each invocation below is one request.
    reader, api = make()
    reader.search("abc")
    assert len(api.requests) <= 4


def test_per_call_request_count_and_request_timeout_caps():
    reader, api = make()

    def five(started, count):
        return [reader._request("search", {}, started, count) for _ in range(5)]

    with pytest.raises(GitlabError, match="^gitlab_unavailable$"):
        reader._call(five)
    assert len(api.requests) == 4
    timeout = api.requests[0].extensions["timeout"]
    assert timeout["read"] == 20


def test_text_redaction_and_output_budget():
    secret = "glpat-" + "abcdefghijklmno"
    reader, _ = make(
        API(
            lambda request: httpx.Response(
                200,
                json=[
                    {
                        "title": secret,
                        "author": {"name": TOKEN, "email": "hidden"},
                        "web_url": "https://private.invalid",
                        "id": 1,
                    }
                    for _ in range(100)
                ],
            )
        )
    )
    result = reader.search("abc")
    encoded = json.dumps(result)
    assert secret not in encoded
    assert TOKEN not in encoded
    assert "hidden" not in encoded and "private.invalid" not in encoded
    assert len(encoded) <= 48_000


def test_diff_text_is_redacted_and_large_diff_marked():
    secret = "glpat-" + "abcdefghijklmno"
    payload = [
        {
            "old_path": "a",
            "new_path": "a",
            "diff": secret + "x" * 20_000,
            "too_large": True,
        }
    ]
    reader, _ = make(API(lambda request: httpx.Response(200, json=payload)))
    result = reader.mr("group/project", 3, part="changes")
    encoded = json.dumps(result)
    assert secret not in encoded
    assert result["diffs"][0]["truncated"] is True
    assert result["diffs"][0]["note"] == "diff_too_large_or_collapsed"
    assert len(encoded) <= 48_000


def test_large_result_is_trimmed_to_json_budget():
    payload = {
        "commits": [],
        "diffs": [
            {"old_path": "a" * 500, "new_path": "b" * 500, "diff": "x" * 8_000}
            for _ in range(100)
        ],
    }
    reader, _ = make(API(lambda request: httpx.Response(200, json=payload)))
    result = reader.compare("group/project", "main", "next")
    assert result["truncated"] is True
    assert len(json.dumps(result)) <= 48_000


def test_file_content_is_bounded_and_binary_is_flagged():
    api = API(
        lambda request: (
            httpx.Response(200, json=[])
            if request.url.path.endswith("/tree")
            else httpx.Response(200, content=b"a" * 31_000)
        )
    )
    reader, _ = make(api)
    result = reader.path("group/project", "file.txt", ref="main")
    assert result["truncated"] is True
    assert len(result["content"]) <= 30_000
    reader, _ = make(
        API(
            lambda request: (
                httpx.Response(200, json=[])
                if request.url.path.endswith("/tree")
                else httpx.Response(200, content=b"\x00\x01")
            )
        )
    )
    result = reader.path("group/project", "image.bin")
    assert result["binary"] is True and result["content"] == ""
