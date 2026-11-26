"""Bounded, read-only GitLab API access for one configured host."""

import json
import re
import time
from urllib.parse import quote, urlsplit

import httpx

from ..core.fields import SECRET

MAX_BODY = 2_000_000
MAX_RESULT = 48_000
MAX_TEXT = 30_000
MAX_DIFF_FILE = 8_000
MAX_DIFF_TOTAL = 24_000
MAX_REQUESTS = 4
DEADLINE = 60


class GitlabError(Exception):
    """An operation failed with a stable, non-sensitive code."""


class GitlabReader:
    def __init__(self, url, token, client=None, clock=time.monotonic):
        parsed = urlsplit(url) if isinstance(url, str) else None
        try:
            valid_origin = (
                parsed is not None
                and parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and (parsed.port is None or 1 <= parsed.port <= 65535)
            )
        except ValueError:
            valid_origin = False
        if not valid_origin:
            raise ValueError("url must be a plain https origin")
        self.base = f"{url.rstrip('/')}/api/v4"
        self._token = token
        self.clock = clock
        self.client = client or httpx.Client(
            headers={"PRIVATE-TOKEN": token},
            timeout=httpx.Timeout(20, connect=5),
            trust_env=False,
            follow_redirects=False,
        )
        if client is not None:
            self.client.headers["PRIVATE-TOKEN"] = token
            self.client.follow_redirects = False

    @staticmethod
    def _bad(name):
        raise GitlabError(f"bad_{name}")

    @classmethod
    def _validate(cls, name, value):
        if name == "project":
            valid = (
                isinstance(value, str)
                and len(value) <= 255
                and re.fullmatch(
                    r"(?:[0-9]+|[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)", value
                )
                and ".." not in value
                and not value.startswith("/")
                and not value.endswith("/")
            )
        elif name == "iid":
            valid = type(value) is int and 0 < value < 10_000_000
        elif name in ("ref", "base", "head"):
            valid = (
                isinstance(value, str)
                and 1 <= len(value) <= 255
                and not value.startswith("-")
                and ".." not in value
                and re.fullmatch(r"[A-Za-z0-9._/@+~^-]+", value)
            )
        elif name == "path":
            valid = (
                isinstance(value, str)
                and len(value) <= 500
                and not value.startswith("/")
                and not any(ord(c) < 32 or ord(c) == 127 for c in value)
                and all(part != ".." for part in value.split("/"))
            )
        elif name == "query":
            valid = (
                isinstance(value, str)
                and 2 <= len(value) <= 200
                and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            )
        elif name == "page":
            valid = type(value) is int and 1 <= value <= 100
        elif name == "offset":
            valid = type(value) is int and value >= 0
        elif name == "state":
            valid = value in ("opened", "merged", "closed", "all")
        elif name == "part":
            valid = value in ("info", "changes", "commits", "discussions")
        elif name == "scope":
            valid = value in (
                "projects",
                "merge_requests",
                "issues",
                "blobs",
                "commits",
            )
        else:
            valid = False
        if not valid:
            cls._bad(name)
        return value

    def _request(self, endpoint, params, started, count, raw=False):
        if count[0] >= MAX_REQUESTS or self.clock() - started > DEADLINE:
            raise GitlabError("gitlab_unavailable")
        count[0] += 1
        try:
            with self.client.stream(
                "GET",
                f"{self.base}/{endpoint}",
                params=params or {},
                timeout=20,
                follow_redirects=False,
            ) as response:
                status = response.status_code
                if status in (401, 403):
                    raise GitlabError("unauthorized" if status == 401 else "forbidden")
                if status == 404:
                    raise GitlabError("not_found")
                if status == 429:
                    raise GitlabError("rate_limited")
                if status != 200:
                    raise GitlabError("gitlab_unavailable")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY:
                        raise GitlabError("bad_response")
                    chunks.append(chunk)
                body = b"".join(chunks)
        except GitlabError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            raise GitlabError("gitlab_unavailable") from None
        if self.clock() - started > DEADLINE:
            raise GitlabError("gitlab_unavailable")
        if raw:
            return body
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise GitlabError("bad_response") from None

    def _call(self, operation):
        return operation(self.clock(), [0])

    @staticmethod
    def _project(project):
        return quote(GitlabReader._validate("project", project), safe="")

    def _text(self, value, limit=MAX_TEXT):
        if not isinstance(value, str):
            return "", False
        value = value.replace(self._token, "[REDACTED]") if self._token else value
        value = SECRET.sub("[REDACTED]", value)
        return value[:limit], len(value) > limit

    def _sanitize(self, value):
        if isinstance(value, str):
            value = value.replace(self._token, "[REDACTED]") if self._token else value
            return SECRET.sub("[REDACTED]", value)
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if isinstance(value, dict):
            return {key: self._sanitize(item) for key, item in value.items()}
        return value

    def _author(self, item):
        author = item.get("author") or {}
        name = author.get("name") if isinstance(author, dict) else None
        return self._text(name, 200)[0] if isinstance(name, str) else None

    def _fit(self, result):
        result = self._sanitize(result)
        if len(json.dumps(result, ensure_ascii=True)) <= MAX_RESULT:
            return result
        result = {
            k: v
            for k, v in result.items()
            if k not in ("diffs", "commits", "items", "discussions", "files", "tree")
        }
        result["truncated"] = True
        while len(json.dumps(result, ensure_ascii=True)) > MAX_RESULT:
            strings = [
                (len(v), k) for k, v in result.items() if isinstance(v, str) and v
            ]
            if not strings:
                break
            _, key = max(strings)
            result[key] = result[key][: len(result[key]) // 2]
        return result

    def mr(self, project, iid=None, part="info", state="opened", page=1):
        project = self._project(project)
        self._validate("page", page)
        if iid is None:
            self._validate("state", state)

            def run(start, count):
                rows = self._request(
                    f"projects/{project}/merge_requests",
                    {
                        "state": state,
                        "order_by": "updated_at",
                        "sort": "desc",
                        "per_page": 20,
                        "page": page,
                    },
                    start,
                    count,
                )
                return self._fit(
                    {
                        "items": [
                            self._mr_summary(x)
                            for x in rows[:20]
                            if isinstance(x, dict)
                        ]
                    }
                )

            return self._call(run)
        self._validate("iid", iid)
        self._validate("part", part)

        def run(start, count):
            root = f"projects/{project}/merge_requests/{iid}"
            if part == "info":
                x = self._request(root, {}, start, count)
                description, cut = self._text(x.get("description"))
                pipeline = x.get("head_pipeline") or {}
                return self._fit(
                    {
                        "iid": iid,
                        "title": self._text(x.get("title"), 500)[0],
                        "author": self._author(x),
                        "state": x.get("state"),
                        "source_branch": self._text(x.get("source_branch"), 255)[0],
                        "target_branch": self._text(x.get("target_branch"), 255)[0],
                        "description": description,
                        "labels": [
                            self._text(v, 200)[0]
                            for v in x.get("labels", [])
                            if isinstance(v, str)
                        ][:30],
                        "pipeline_status": pipeline.get("status"),
                        "changes_count": x.get("changes_count"),
                        "truncated": cut,
                    }
                )
            endpoint = {
                "changes": "changes",
                "commits": "commits",
                "discussions": "discussions",
            }[part]
            rows = self._request(f"{root}/{endpoint}", {"per_page": 20}, start, count)
            if part == "changes":
                return self._fit(
                    self._diffs(
                        rows.get("changes", []) if isinstance(rows, dict) else rows
                    )
                )
            if part == "commits":
                return self._fit(
                    {
                        "commits": [
                            self._commit(x) for x in rows[:20] if isinstance(x, dict)
                        ]
                    }
                )
            discussions = [
                self._discussion(x)
                for x in rows[:20]
                if isinstance(x, dict) and isinstance(x.get("notes"), list)
            ]
            return self._fit({"discussions": [d for d in discussions if d is not None]})

        return self._call(run)

    def _mr_summary(self, x):
        return {
            "iid": x.get("iid"),
            "title": self._text(x.get("title"), 500)[0],
            "author": self._author(x),
            "state": x.get("state"),
            "source_branch": self._text(x.get("source_branch"), 255)[0],
            "target_branch": self._text(x.get("target_branch"), 255)[0],
            "created_at": x.get("created_at"),
            "updated_at": x.get("updated_at"),
            "url": self._text(x.get("web_url"), 1000)[0],
        }

    def _commit(self, x):
        return {
            "sha": self._text(x.get("short_id") or x.get("id"), 7)[0],
            "title": self._text(x.get("title"), 500)[0],
            "author": self._text(x.get("author_name"), 200)[0],
            "date": self._text(x.get("created_at") or x.get("committed_date"), 50)[0],
        }

    def _diffs(self, rows):
        diffs, used, truncated = [], 0, False
        for x in rows[:100] if isinstance(rows, list) else []:
            if not isinstance(x, dict):
                continue
            limit = max(0, min(MAX_DIFF_FILE, MAX_DIFF_TOTAL - used))
            diff, cut = self._text(x.get("diff"), limit)
            used += len(diff)
            collapsed = bool(x.get("collapsed") or x.get("too_large"))
            truncated |= cut or collapsed
            diffs.append(
                {
                    "old_path": self._text(x.get("old_path"), 500)[0],
                    "new_path": self._text(x.get("new_path"), 500)[0],
                    "new_file": bool(x.get("new_file")),
                    "renamed_file": bool(x.get("renamed_file")),
                    "deleted_file": bool(x.get("deleted_file")),
                    "diff": diff,
                    "truncated": cut or collapsed,
                    "note": "diff_too_large_or_collapsed" if collapsed else None,
                }
            )
        return {"diffs": diffs, "truncated": truncated}

    def _discussion(self, x):
        notes = [n for n in x["notes"] if isinstance(n, dict) and not n.get("system")]
        if not notes:
            return None
        note = notes[0]
        body, cut = self._text(note.get("body"), 2_000)
        pos = note.get("position") or x.get("position") or {}
        return {
            "author": self._author(note),
            "body": body,
            "resolved": bool(x.get("resolved")),
            "position": {
                k: pos[k]
                for k in ("new_path", "old_path", "new_line", "old_line")
                if k in pos
            },
            "truncated": cut,
        }

    def compare(self, project, base, head):
        project = self._project(project)
        base = self._validate("base", base)
        head = self._validate("head", head)

        def run(start, count):
            x = self._request(
                f"projects/{project}/repository/compare",
                {"from": base, "to": head},
                start,
                count,
            )
            commits = [
                self._commit(c)
                for c in x.get("commits", [])[:20]
                if isinstance(c, dict)
            ]
            return self._fit({"commits": commits, **self._diffs(x.get("diffs", []))})

        return self._call(run)

    def _tree(self, rows, offset):
        entries = [
            {
                "name": self._text(x.get("name"), 255)[0],
                "type": x.get("type"),
                "path": self._text(x.get("path"), 500)[0],
            }
            for x in rows[:100]
            if isinstance(x, dict) and x.get("type") in ("tree", "blob")
        ]
        return self._fit(
            {"tree": entries, "next_offset": offset + 100 if len(rows) == 100 else None}
        )

    def path(self, project, path="", ref=None, offset=0):
        project = self._project(project)
        path = self._validate("path", path)
        self._validate("offset", offset)
        if ref is not None:
            ref = self._validate("ref", ref)

        def run(start, count):
            prefix = f"projects/{project}/repository"
            if not path:
                rows = self._request(
                    f"{prefix}/tree",
                    {"per_page": 100, "page": offset // 100 + 1},
                    start,
                    count,
                )
                return self._tree(rows, offset)
            try:
                rows = self._request(
                    f"{prefix}/tree",
                    {"path": path, "per_page": 100, "page": offset // 100 + 1},
                    start,
                    count,
                )
            except GitlabError as error:
                if str(error) != "not_found":
                    raise
                rows = None
            if (
                rows is not None
                and isinstance(rows, list)
                and rows
                and all(
                    isinstance(x, dict)
                    and x.get("path", "").startswith(path.rstrip("/") + "/")
                    for x in rows
                )
            ):
                return self._tree(rows, offset)
            raw = self._request(
                f"{prefix}/files/{quote(path, safe='')}/raw",
                {"ref": ref} if ref else {},
                start,
                count,
                raw=True,
            )
            if b"\x00" in raw or any(b < 9 or 13 < b < 32 for b in raw[:4096]):
                return {
                    "path": path,
                    "binary": True,
                    "content": "",
                    "truncated": False,
                    "next_offset": None,
                }
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError:
                return {
                    "path": path,
                    "binary": True,
                    "content": "",
                    "truncated": False,
                    "next_offset": None,
                }
            text, cut = self._text(decoded[offset:], MAX_TEXT)
            return self._fit(
                {
                    "path": path,
                    "binary": False,
                    "content": text,
                    "truncated": cut,
                    "next_offset": offset + len(text) if cut else None,
                }
            )

        return self._call(run)

    def search(self, query, scope="projects", project=None, page=1):
        query = self._validate("query", query)
        self._validate("scope", scope)
        self._validate("page", page)
        if scope in ("blobs", "commits") and project is None:
            self._bad("project")
        p = self._project(project) if project is not None else None

        def run(start, count):
            endpoint = f"projects/{p}/search" if p is not None else "search"
            rows = self._request(
                endpoint,
                {"scope": scope, "search": query, "per_page": 20, "page": page},
                start,
                count,
            )
            hits = []
            for x in rows[:20] if isinstance(rows, list) else []:
                if not isinstance(x, dict):
                    continue
                hit = {
                    k: x[k]
                    for k in (
                        "iid",
                        "path",
                        "ref",
                        "state",
                        "created_at",
                        "updated_at",
                    )
                    if isinstance(x.get(k), (str, int))
                }
                for key in (
                    "title",
                    "name",
                    "description",
                    "message",
                    "data",
                    "filename",
                ):
                    if key in x:
                        hit[key], cut = self._text(x[key], 500)
                        if cut:
                            hit["truncated"] = True
                if isinstance(x.get("author"), dict):
                    hit["author"] = self._author(x)
                hits.append(hit)
            return self._fit({"items": hits})

        return self._call(run)
