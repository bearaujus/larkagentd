import json
import os
import subprocess

import pytest

from larkagentd.toolservice import larkcli
from larkagentd.toolservice.larkcli import LarkError, LarkReader, parse_link

DOC = "docxABCDEFGH"
SHEET = "shtABCDEFGH"
BASE = "basABCDEFGH"
WRITE_VERBS = {
    "create",
    "update",
    "delete",
    "set",
    "put",
    "write",
    "remove",
    "clear",
    "move",
    "copy",
    "revert",
    "upload",
    "submit",
}


class FakeRun:
    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        result = self.outputs.pop(0) if self.outputs else {"ok": True, "data": {}}
        if isinstance(result, subprocess.TimeoutExpired):
            raise result
        if isinstance(result, subprocess.CompletedProcess):
            return result
        return subprocess.CompletedProcess(
            argv,
            result.get("returncode", 0),
            json.dumps(result.get("stdout", result)),
            result.get("stderr", ""),
        )


def reader(tmp_path, outputs=None, **kwargs):
    script = tmp_path / "lark-cli"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o700)
    fake = FakeRun(outputs)
    instance = LarkReader(
        (str(script),),
        "REDACTED_TEST_VALUE",
        "REDACTED_TEST_VALUE",
        tmp_path / "home",
        run=fake,
        **kwargs,
    )
    return instance, fake, script


def test_link_parser_recognizes_supported_url_kinds_and_query_coordinates():
    assert parse_link("https://tenant.feishu.cn/docx/docxABCDEFGH") == ("docx", DOC, {})
    assert parse_link("https://tenant.larksuite.com/wiki/wikABCDEFGH")[:2] == (
        "wiki",
        "wikABCDEFGH",
    )
    assert parse_link(
        "https://tenant.feishu.cn/sheets/shtABCDEFGH?sheet=sheet1234"
    ) == ("sheet", SHEET, {"sheet": "sheet1234"})
    assert parse_link("https://tenant.feishu.cn/base/basABCDEFGH?table=tbl123456") == (
        "base",
        BASE,
        {"table": "tbl123456"},
    )
    assert parse_link(DOC) == ("docx", DOC, {})


def test_short_sheet_identifiers_in_links_and_ranges_are_supported():
    assert parse_link("https://example.feishu.cn/sheets/shtABCDEFGH?sheet=0") == (
        "sheet",
        SHEET,
        {"sheet": "0"},
    )
    assert larkcli._range("0!A1:B1") == ("0", "A1:B1")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://example.com/docx/docxABCDEFGH",
        "https://evil.feishu.cn/docx/short",
        "docx/no!",
        "https://feishu.cn/page/abc12345",
    ],
)
def test_invalid_links_have_fixed_bad_link(value):
    with pytest.raises(LarkError, match="^bad_link$"):
        parse_link(value)


def test_doc_reads_markdown_and_redacts_secret_shaped_text(tmp_path):
    shaped = (
        "LTAI" + "x" * 16
    )  # built at run time: no credential-shaped literal in the file
    payload = {"ok": True, "data": {"document": {"content": f"hi {shaped}"}}}
    service, fake, _ = reader(tmp_path, [payload])
    assert service.doc(DOC) == {"content": "hi [REDACTED]", "truncated": False}
    argv, options = fake.calls[0]
    assert argv[1:3] == ["docs", "+fetch"] and "--doc-format" in argv
    assert options["shell"] is False and isinstance(argv, list)


def test_wiki_doc_resolves_node_then_fetches_obj_token(tmp_path):
    service, fake, _ = reader(
        tmp_path,
        [
            {"ok": True, "data": {"obj_token": DOC}},
            {"ok": True, "data": {"document": {"content": "body"}}},
        ],
    )
    assert (
        service.doc("https://example.feishu.cn/wiki/wikABCDEFGH")["content"] == "body"
    )
    assert [call[0][1:3] for call in fake.calls] == [
        ["wiki", "+node-get"],
        ["docs", "+fetch"],
    ]
    assert all(
        "--as" in call[0] and call[0][call[0].index("--as") + 1] == "bot"
        for call in fake.calls
    )


def test_sheet_range_and_sheet_id_are_validated_and_passed_as_values(tmp_path):
    output = {"ok": True, "data": {"sheets": [{"sheet_id": "sheet1234"}]}}
    cells = {"ok": True, "data": {"ranges": [{"cells": [[1, 2]]}]}}
    service, fake, _ = reader(tmp_path, [output, cells])
    assert service.sheet(SHEET, "sheet1234!A1:B2")["rows"] == [{"cells": [[1, 2]]}]
    argv = fake.calls[1][0]
    assert argv[1:3] == ["sheets", "+cells-get"]
    assert argv[argv.index("--range") + 1] == "A1:B2"
    assert argv[argv.index("--sheet-id") + 1] == "sheet1234"


@pytest.mark.parametrize(
    "value",
    ["A1:AE2", "A1:B101", "A1:A0", "A2:A1", "A1:B2; --yes", "'x y'!A1:B2", "A1"],
)
def test_bad_ranges_rejected_before_any_subprocess_call(tmp_path, value):
    service, fake, _ = reader(tmp_path)
    with pytest.raises(LarkError, match="^bad_range$"):
        service.sheet(SHEET, value)
    assert not fake.calls


def test_sheet_falls_back_to_first_workbook_sheet(tmp_path):
    service, fake, _ = reader(
        tmp_path,
        [
            {"ok": True, "data": {"sheets": [{"sheet_id": "sheet1234"}]}},
            {"ok": True, "data": {"ranges": []}},
        ],
    )
    service.sheet(SHEET)
    assert fake.calls[1][0][fake.calls[1][0].index("--range") + 1] == "A1:AD100"


def test_base_reads_table_fields_and_page_of_records(tmp_path):
    service, fake, _ = reader(
        tmp_path,
        [
            {"ok": True, "data": {"fields": [{"field_name": "Name"}]}},
            {
                "ok": True,
                "data": {"items": [{"record_id": "rec123456"}], "has_more": True},
            },
        ],
    )
    result = service.base(
        "https://example.feishu.cn/base/basABCDEFGH?table=tbl123456", page_token="20"
    )
    assert result["records"] == [{"record_id": "rec123456"}]
    assert result["next_page_token"] == "21"
    assert [call[0][1:3] for call in fake.calls] == [
        ["base", "+field-list"],
        ["base", "+record-list"],
    ]


def test_base_without_table_lists_tables(tmp_path):
    service, fake, _ = reader(
        tmp_path,
        [
            {"ok": True, "data": {"tables": [{"table_id": "tbl123456"}]}},
        ],
    )
    assert service.base(BASE)["tables"] == [{"table_id": "tbl123456"}]


@pytest.mark.parametrize(
    ("table", "page", "code"),
    [("../tbl123456", None, "bad_table"), (None, "2/../../x", "bad_page_token")],
)
def test_base_validates_table_and_page_tokens_before_cli(tmp_path, table, page, code):
    service, fake, _ = reader(tmp_path)
    with pytest.raises(LarkError, match=f"^{code}$"):
        service.base(BASE, table=table, page_token=page)
    assert not fake.calls


def test_every_call_is_fixed_allowlisted_read_command_with_no_write_switches(tmp_path):
    service, fake, _ = reader(
        tmp_path,
        [
            {"ok": True, "data": {"document": {"content": "read"}}},
            {"ok": True, "data": {"sheets": [{"sheet_id": "sheet1234"}]}},
            {"ok": True, "data": {"ranges": []}},
            {"ok": True, "data": {"tables": []}},
        ],
    )
    service.doc(DOC)
    service.sheet(SHEET)
    service.base(BASE)
    for argv, options in fake.calls:
        command = tuple(argv[1:3])
        assert command in larkcli.READ_COMMANDS
        assert "api" not in argv and "--yes" not in argv
        assert not ("--as" in argv and argv[argv.index("--as") + 1] == "user")
        assert not any(token in WRITE_VERBS for token in argv[1:])
        assert options["shell"] is False and isinstance(argv, list)


def test_non_allowlisted_subcommands_are_rejected_even_internally(tmp_path):
    service, fake, _ = reader(tmp_path)
    with pytest.raises(LarkError, match="^lark_unavailable$"):
        service._call(("base", "+record-delete"), ["--record-id", "rec123456"])
    assert not fake.calls


def test_credentials_only_in_child_config_and_environment_is_minimal(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", "decoy-database")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "decoy-key")
    monkeypatch.setenv("LARK_USER_TOKEN", "decoy-token")
    service, fake, _ = reader(tmp_path)
    service.doc(DOC)
    argv, options = fake.calls[0]
    assert "REDACTED_TEST_VALUE" not in argv
    assert set(options["env"]) == {"PATH", "HOME"}
    assert options["env"]["HOME"] == os.fspath(tmp_path / "home")
    assert (
        not {"DATABASE_URL", "AWS_SECRET_ACCESS_KEY", "LARK_USER_TOKEN"}
        & options["env"].keys()
    )
    config = json.loads((tmp_path / "home/.lark-cli/config.json").read_text())
    assert config["apps"][0]["appId"] == "REDACTED_TEST_VALUE"
    assert config["apps"][0]["appSecret"] == "REDACTED_TEST_VALUE"
    assert config["apps"][0]["users"] == []


def test_bot_is_explicit_and_no_user_auth_commands_are_allowed(tmp_path):
    service, fake, _ = reader(
        tmp_path, [{"ok": True, "data": {"document": {"content": "ok"}}}]
    )
    service.doc(DOC)
    argv = fake.calls[0][0]
    assert argv[argv.index("--as") + 1] == "bot"
    assert all(command[0] != "auth" for command in larkcli.READ_COMMANDS)


def test_cli_stdout_is_capped_and_timeout_is_fixed(tmp_path):
    assert larkcli.MAX_STDOUT == 2 * 1024 * 1024
    too_big = subprocess.CompletedProcess([], 0, "x" * (2 * 1024 * 1024 + 1), "")
    service, _, _ = reader(tmp_path, [too_big])
    with pytest.raises(LarkError, match="^lark_unavailable$"):
        service.doc(DOC)
    timeout = subprocess.TimeoutExpired("private-command", 1)
    service, fake, _ = reader(tmp_path, [timeout], timeout=7)
    with pytest.raises(LarkError, match="^timeout$"):
        service.doc(DOC)
    assert fake.calls[0][1]["timeout"] == 7


def test_default_runner_kills_process_group_when_child_closes_pipes_and_hangs(
    tmp_path,
):
    script = tmp_path / "slow-lark-cli"
    script.write_text("#!/bin/sh\nexec 1>&- 2>&-\nsleep 10\n", encoding="utf-8")
    script.chmod(0o700)
    service = LarkReader(
        (str(script),),
        "REDACTED_TEST_VALUE",
        "REDACTED_TEST_VALUE",
        tmp_path / "home",
        timeout=0.1,
    )
    with pytest.raises(LarkError, match="^timeout$"):
        service.doc(DOC)


def test_result_budget_trims_data_and_marks_truncation(monkeypatch):
    monkeypatch.setattr(larkcli, "RESULT_CHARS", 160)
    result = larkcli._fit_result({"content": "x" * 500})
    assert result["truncated"] is True
    assert len(json.dumps(result)) <= 160


def test_fixed_cli_error_mapping_does_not_leak_output_or_stderr(tmp_path, caplog):
    for subtype, code in [
        ("permission_denied", "forbidden"),
        ("not_found", "not_found"),
        (
            "invalid_parameters",
            "not_found",
        ),  # a mistyped link: the real CLI reports this, not not_found
        ("invalid_client", "not_configured"),
        ("rate_limit", "rate_limited"),
        ("other", "lark_unavailable"),
    ]:
        payload = json.dumps(
            {
                "ok": False,
                "error": {
                    "type": "authorization",
                    "subtype": subtype,
                    "message": "private title and URL",
                },
            }
        )
        run = FakeRun([subprocess.CompletedProcess([], 3, "", payload)])
        service = LarkReader(
            ("/fake/lark-cli",),
            "REDACTED_TEST_VALUE",
            "REDACTED_TEST_VALUE",
            tmp_path / subtype,
            run=run,
        )
        with pytest.raises(LarkError) as caught:
            service.doc(DOC)
        assert str(caught.value) == code
        assert (
            "private title" not in str(caught.value)
            and "private title" not in caplog.text
        )


def test_credential_shaped_return_text_redacted(tmp_path):
    secret = "password=" + "q" * 16
    service, _, _ = reader(
        tmp_path, [{"ok": True, "data": {"document": {"content": secret}}}]
    )
    assert "q" * 16 not in service.doc(DOC)["content"]


def test_return_result_stays_within_tool_budget(tmp_path):
    service, _, _ = reader(
        tmp_path, [{"ok": True, "data": {"document": {"content": "é" * 40000}}}]
    )
    result = service.doc(DOC)
    assert len(json.dumps(result, ensure_ascii=True)) <= 48_000
    assert result["truncated"] is True
