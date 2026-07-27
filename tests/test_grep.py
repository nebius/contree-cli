from __future__ import annotations

import json
from contextvars import copy_context
from unittest.mock import patch

from conftest import ContreeTestClient, make_grep_match, make_grep_result
from contree_client.models import GrepResult

from contree_cli import FORMATTER, SESSION_STORE
from contree_cli.cli.grep import GrepArgs, cmd_grep, highlight_match
from contree_cli.output import (
    CSVFormatter,
    DefaultFormatter,
    JSONFormatter,
    TableFormatter,
)
from contree_cli.session import SessionStore
from contree_cli.types import Colors

IMG_UUID = "a1b2c3d4-5678-9abc-def0-111111111111"


def _run_cmd(
    tc: ContreeTestClient,
    matches: list[dict[str, object]] | None = None,
    *,
    store: SessionStore,
    image: str = IMG_UUID,
    path: str | None = "/etc",
    pattern: str = "localhost",
    glob: str | None = None,
    max_count: int | None = None,
    max_total: int | None = None,
    case: str | None = None,
    raw: bool = False,
    formatter=None,
    truncated: bool = False,
) -> int | None:
    """Run cmd_grep with a mocked inspect_image_grep response."""
    tc.mock(
        "inspect_image_grep",
        GrepResult.from_dict(
            make_grep_result(
                path=path or "/etc",
                matches=matches or [],
                truncated=truncated,
            )
        ),
    )
    FORMATTER.set(formatter or CSVFormatter())
    store.set_image(image, kind="test")
    SESSION_STORE.set(store)
    ctx = copy_context()

    args = GrepArgs(
        pattern=pattern,
        path=path,
        glob=glob,
        max_count=max_count,
        max_total=max_total,
        case=case,
        raw=raw,
    )
    return ctx.run(cmd_grep, args)


class TestCmdGrep:
    def test_default_path_uses_cwd(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        session_store.set_cwd("/app")
        _run_cmd(
            contree_client,
            [make_grep_match()],
            store=session_store,
            path=None,
        )
        calls = contree_client.calls_for("inspect_image_grep")
        assert calls[0].kwargs["path"] == "/app"

    def test_default_path_falls_back_to_root(self, contree_client, session_store):
        _run_cmd(
            contree_client,
            [make_grep_match()],
            store=session_store,
            path=None,
        )
        calls = contree_client.calls_for("inspect_image_grep")
        assert calls[0].kwargs["path"] == "/"

    def test_explicit_path_resolved(self, contree_client, session_store):
        _run_cmd(contree_client, [make_grep_match()], store=session_store, path="/etc")
        calls = contree_client.calls_for("inspect_image_grep")
        assert calls[0].kwargs["path"] == "/etc"

    def test_pattern_forwarded(self, contree_client, session_store):
        _run_cmd(contree_client, [], store=session_store, pattern="foo.*bar")
        calls = contree_client.calls_for("inspect_image_grep")
        assert calls[0].args == (IMG_UUID, "foo.*bar")

    def test_glob_max_count_max_total_case_forwarded(
        self, contree_client, session_store
    ):
        _run_cmd(
            contree_client,
            [],
            store=session_store,
            glob="*.py",
            max_count=3,
            max_total=100,
            case="insensitive",
        )
        kwargs = contree_client.calls_for("inspect_image_grep")[0].kwargs
        assert kwargs["glob"] == "*.py"
        assert kwargs["max_count"] == 3
        assert kwargs["max_total"] == 100
        assert kwargs["case"] == "insensitive"

    def test_one_row_per_match_csv(self, contree_client, session_store, capsys):
        matches = [make_grep_match(path="/a"), make_grep_match(path="/b")]
        _run_cmd(contree_client, matches, store=session_store, formatter=CSVFormatter())
        out = capsys.readouterr().out
        assert "/a" in out
        assert "/b" in out
        assert len(out.strip().splitlines()) == 3  # header + 2 rows

    def test_one_row_per_match_json(self, contree_client, session_store, capsys):
        matches = [make_grep_match(path="/a"), make_grep_match(path="/b")]
        _run_cmd(
            contree_client, matches, store=session_store, formatter=JSONFormatter()
        )
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["path"] == "/a"
        assert json.loads(lines[1])["path"] == "/b"

    def test_one_row_per_match_table(self, contree_client, session_store, capsys):
        matches = [make_grep_match(path="/a"), make_grep_match(path="/b")]
        fmt = TableFormatter()
        _run_cmd(contree_client, matches, store=session_store, formatter=fmt)
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 3  # header + 2 rows
        assert "PATH" in lines[0]

    def test_default_formatter_prints_grep_style_lines(
        self, contree_client, session_store, capsys
    ):
        matches = [
            make_grep_match(path="/etc/hosts", line_number=3),
            make_grep_match(path="/etc/passwd", line_number=7),
        ]
        # Pinned regardless of the environment's FORCE_COLOR -- this test
        # asserts the plain-text shape; coloring is covered separately by
        # TestHighlightMatch.
        with patch("contree_cli.cli.grep.STDOUT_IS_A_TTY", False):
            _run_cmd(
                contree_client,
                matches,
                store=session_store,
                formatter=DefaultFormatter(),
            )
        lines = capsys.readouterr().out.splitlines()
        assert lines == [
            "/etc/hosts:3:127.0.0.1 localhost",
            "/etc/passwd:7:127.0.0.1 localhost",
        ]

    def test_default_formatter_empty(self, contree_client, session_store, capsys):
        with patch("contree_cli.cli.grep.STDOUT_IS_A_TTY", False):
            _run_cmd(
                contree_client, [], store=session_store, formatter=DefaultFormatter()
            )
        assert capsys.readouterr().out == ""

    def test_submatches_dropped_from_normal_json_row(
        self, contree_client, session_store, capsys
    ):
        _run_cmd(
            contree_client,
            [make_grep_match()],
            store=session_store,
            formatter=JSONFormatter(),
        )
        parsed = json.loads(capsys.readouterr().out.strip())
        assert "submatches" not in parsed

    def test_line_text_trailing_newline_stripped_in_normal_row(
        self, contree_client, session_store, capsys
    ):
        _run_cmd(
            contree_client,
            [make_grep_match(line_text="127.0.0.1 localhost\n")],
            store=session_store,
            formatter=JSONFormatter(),
        )
        parsed = json.loads(capsys.readouterr().out.strip())
        assert parsed["line_text"] == "127.0.0.1 localhost"

    def test_raw_preserves_submatches_patterns_truncated(
        self, contree_client, session_store, capsys
    ):
        _run_cmd(
            contree_client,
            [make_grep_match()],
            store=session_store,
            raw=True,
            truncated=True,
        )
        parsed = json.loads(capsys.readouterr().out.strip())
        assert parsed["truncated"] is True
        assert "patterns" in parsed
        assert "submatches" in parsed["matches"][0]
        # --raw stays byte-exact -- the trailing newline is not stripped.
        assert parsed["matches"][0]["line_text"].endswith("\n")

    def test_exit_code_1_when_no_matches(self, contree_client, session_store):
        exit_code = _run_cmd(contree_client, [], store=session_store)
        assert exit_code == 1

    def test_exit_code_none_when_matches_found(self, contree_client, session_store):
        exit_code = _run_cmd(contree_client, [make_grep_match()], store=session_store)
        assert exit_code is None

    def test_raw_exit_code_1_when_no_matches(self, contree_client, session_store):
        exit_code = _run_cmd(contree_client, [], store=session_store, raw=True)
        assert exit_code == 1

    def test_truncated_logs_warning(self, contree_client, session_store, caplog):
        _run_cmd(
            contree_client,
            [make_grep_match()],
            store=session_store,
            truncated=True,
        )
        assert any("truncat" in rec.message.lower() for rec in caplog.records)

    def test_not_truncated_no_warning(self, contree_client, session_store, caplog):
        _run_cmd(
            contree_client,
            [make_grep_match()],
            store=session_store,
            truncated=False,
        )
        assert not any("truncat" in rec.message.lower() for rec in caplog.records)


class TestHighlightMatch:
    def test_wraps_matched_span(self):
        with patch("contree_cli.types.IS_A_TTY", True):
            result = highlight_match(
                "127.0.0.1 localhost",
                [{"text": "localhost", "start": 10, "end": 19}],
            )
            assert result == f"127.0.0.1 {Colors.BOLD_RED('localhost')}"

    def test_no_tty_returns_plain_text(self):
        with patch("contree_cli.types.IS_A_TTY", False):
            result = highlight_match(
                "127.0.0.1 localhost",
                [{"text": "localhost", "start": 10, "end": 19}],
            )
        assert result == "127.0.0.1 localhost"

    def test_multiple_submatches(self):
        with patch("contree_cli.types.IS_A_TTY", True):
            result = highlight_match(
                "foo bar foo",
                [
                    {"text": "foo", "start": 0, "end": 3},
                    {"text": "foo", "start": 8, "end": 11},
                ],
            )
            assert result == f"{Colors.BOLD_RED('foo')} bar {Colors.BOLD_RED('foo')}"

    def test_no_submatches_returns_unchanged(self):
        with patch("contree_cli.types.IS_A_TTY", True):
            assert highlight_match("plain text", []) == "plain text"

    def test_multibyte_utf8_offsets(self):
        # "café " is 5 chars but 6 bytes (é is 2 bytes in UTF-8); the
        # match's byte offsets must still land on "bar", not be thrown
        # off by the multi-byte character preceding it.
        with patch("contree_cli.types.IS_A_TTY", True):
            result = highlight_match(
                "café bar", [{"text": "bar", "start": 6, "end": 9}]
            )
            assert result == f"café {Colors.BOLD_RED('bar')}"

    def test_out_of_range_submatch_skipped(self):
        with patch("contree_cli.types.IS_A_TTY", True):
            result = highlight_match("short", [{"text": "x", "start": 50, "end": 51}])
        assert result == "short"
