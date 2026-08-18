from __future__ import annotations

import json
from contextvars import copy_context
from unittest.mock import patch

from conftest import ContreeTestClient, make_grep_match, make_grep_result
from contree_client.models import GrepResult

from contree_cli import FORMATTER, SESSION_STORE
from contree_cli.cli.grep import GrepArgs, cmd_grep, colorize, highlight_match
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
    before_context: int | None = None,
    after_context: int | None = None,
    context: int | None = None,
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
        path=(path,) if path is not None else (),
        glob=glob,
        max_count=max_count,
        max_total=max_total,
        case=case,
        raw=raw,
        before_context=before_context,
        after_context=after_context,
        context=context,
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
        assert calls[0].kwargs["path"] == ["/app"]

    def test_default_path_falls_back_to_root(self, contree_client, session_store):
        _run_cmd(
            contree_client,
            [make_grep_match()],
            store=session_store,
            path=None,
        )
        calls = contree_client.calls_for("inspect_image_grep")
        assert calls[0].kwargs["path"] == ["/"]

    def test_explicit_path_resolved(self, contree_client, session_store):
        _run_cmd(contree_client, [make_grep_match()], store=session_store, path="/etc")
        calls = contree_client.calls_for("inspect_image_grep")
        assert calls[0].kwargs["path"] == ["/etc"]

    def test_multiple_paths_each_resolved(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        FORMATTER.set(CSVFormatter())
        SESSION_STORE.set(session_store)
        contree_client.mock(
            "inspect_image_grep",
            GrepResult.from_dict(make_grep_result(matches=[])),
        )
        args = GrepArgs(pattern="localhost", path=("/etc", "app.log"))
        session_store.set_cwd("/var/log")
        ctx = copy_context()
        ctx.run(cmd_grep, args)
        calls = contree_client.calls_for("inspect_image_grep")
        # "/etc" is absolute; "app.log" is relative to the session cwd.
        assert calls[0].kwargs["path"] == ["/etc", "/var/log/app.log"]

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

    def test_before_after_forwarded_independently(self, contree_client, session_store):
        _run_cmd(
            contree_client,
            [],
            store=session_store,
            before_context=1,
            after_context=3,
        )
        kwargs = contree_client.calls_for("inspect_image_grep")[0].kwargs
        assert kwargs["before"] == 1
        assert kwargs["after"] == 3

    def test_context_sets_both_before_and_after(self, contree_client, session_store):
        _run_cmd(contree_client, [], store=session_store, context=2)
        kwargs = contree_client.calls_for("inspect_image_grep")[0].kwargs
        assert kwargs["before"] == 2
        assert kwargs["after"] == 2

    def test_before_after_override_context(self, contree_client, session_store):
        _run_cmd(
            contree_client,
            [],
            store=session_store,
            context=5,
            before_context=1,
        )
        kwargs = contree_client.calls_for("inspect_image_grep")[0].kwargs
        assert kwargs["before"] == 1  # explicit -B wins over -C
        assert kwargs["after"] == 5  # -A not given, falls back to -C

    def test_no_context_flags_forwards_none(self, contree_client, session_store):
        _run_cmd(contree_client, [], store=session_store)
        kwargs = contree_client.calls_for("inspect_image_grep")[0].kwargs
        assert kwargs["before"] is None
        assert kwargs["after"] is None

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

    def test_context_lines_use_dash_separator(
        self, contree_client, session_store, capsys
    ):
        matches = [
            make_grep_match(path="/var/log/app.log", line_number=5, type="match"),
            make_grep_match(path="/var/log/app.log", line_number=6, type="context"),
        ]
        with patch("contree_cli.cli.grep.STDOUT_IS_A_TTY", False):
            _run_cmd(
                contree_client,
                matches,
                store=session_store,
                formatter=DefaultFormatter(),
                context=1,
            )
        lines = capsys.readouterr().out.splitlines()
        assert lines == [
            "/var/log/app.log:5:127.0.0.1 localhost",
            "/var/log/app.log-6-127.0.0.1 localhost",
        ]

    def test_group_separator_between_noncontiguous_groups(
        self, contree_client, session_store, capsys
    ):
        matches = [
            make_grep_match(path="/a", line_number=5, type="match"),
            make_grep_match(path="/a", line_number=6, type="context"),
            make_grep_match(path="/a", line_number=40, type="context"),
            make_grep_match(path="/a", line_number=41, type="match"),
        ]
        with patch("contree_cli.cli.grep.STDOUT_IS_A_TTY", False):
            _run_cmd(
                contree_client,
                matches,
                store=session_store,
                formatter=DefaultFormatter(),
                context=1,
            )
        lines = capsys.readouterr().out.splitlines()
        assert lines == [
            "/a:5:127.0.0.1 localhost",
            "/a-6-127.0.0.1 localhost",
            "--",
            "/a-40-127.0.0.1 localhost",
            "/a:41:127.0.0.1 localhost",
        ]

    def test_group_separator_on_path_change_even_if_lines_contiguous(
        self, contree_client, session_store, capsys
    ):
        matches = [
            make_grep_match(path="/a", line_number=10, type="match"),
            make_grep_match(path="/b", line_number=11, type="match"),
        ]
        with patch("contree_cli.cli.grep.STDOUT_IS_A_TTY", False):
            _run_cmd(
                contree_client,
                matches,
                store=session_store,
                formatter=DefaultFormatter(),
                context=1,
            )
        lines = capsys.readouterr().out.splitlines()
        assert lines == [
            "/a:10:127.0.0.1 localhost",
            "--",
            "/b:11:127.0.0.1 localhost",
        ]

    def test_no_group_separator_without_context_flags(
        self, contree_client, session_store, capsys
    ):
        """Without -A/-B/-C, gaps between plain matches print no `--`,
        matching real grep's own behavior."""
        matches = [
            make_grep_match(path="/a", line_number=5, type="match"),
            make_grep_match(path="/a", line_number=99, type="match"),
        ]
        with patch("contree_cli.cli.grep.STDOUT_IS_A_TTY", False):
            _run_cmd(
                contree_client,
                matches,
                store=session_store,
                formatter=DefaultFormatter(),
            )
        lines = capsys.readouterr().out.splitlines()
        assert "--" not in lines

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

    def test_line_text_crlf_stripped_in_normal_row(
        self, contree_client, session_store, capsys
    ):
        _run_cmd(
            contree_client,
            [make_grep_match(line_text="127.0.0.1 localhost\r\n")],
            store=session_store,
            formatter=JSONFormatter(),
        )
        parsed = json.loads(capsys.readouterr().out.strip())
        assert parsed["line_text"] == "127.0.0.1 localhost"

    def test_line_text_crlf_stripped_in_default_formatter(
        self, contree_client, session_store, capsys
    ):
        with patch("contree_cli.cli.grep.STDOUT_IS_A_TTY", False):
            _run_cmd(
                contree_client,
                [make_grep_match(line_text="127.0.0.1 localhost\r\n")],
                store=session_store,
                formatter=DefaultFormatter(),
            )
        out = capsys.readouterr().out
        assert out == "/etc/hosts:1:127.0.0.1 localhost\n"

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

    def test_exit_code_2_when_truncated_before_any_match(
        self, contree_client, session_store
    ):
        """Truncated with zero matches is inconclusive (the deadline/
        max_total was hit before finding anything) -- distinct from a
        completed search confirming there's nothing to find."""
        exit_code = _run_cmd(contree_client, [], store=session_store, truncated=True)
        assert exit_code == 2

    def test_exit_code_none_when_truncated_but_matches_found(
        self, contree_client, session_store
    ):
        exit_code = _run_cmd(
            contree_client,
            [make_grep_match()],
            store=session_store,
            truncated=True,
        )
        assert exit_code is None

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
    """highlight_match() always highlights -- unconditionally, no
    IS_A_TTY check of its own. The caller (write_grep_lines) is the
    one that decides whether to invoke it based on STDOUT_IS_A_TTY;
    see TestCmdGrep's DefaultFormatter tests for that side."""

    def test_wraps_matched_span(self):
        result = highlight_match(
            "127.0.0.1 localhost",
            [{"text": "localhost", "start": 10, "end": 19}],
        )
        assert result == f"127.0.0.1 {colorize(Colors.BOLD_RED, 'localhost')}"

    def test_multiple_submatches(self):
        result = highlight_match(
            "foo bar foo",
            [
                {"text": "foo", "start": 0, "end": 3},
                {"text": "foo", "start": 8, "end": 11},
            ],
        )
        assert result == (
            f"{colorize(Colors.BOLD_RED, 'foo')} bar {colorize(Colors.BOLD_RED, 'foo')}"
        )

    def test_no_submatches_returns_unchanged(self):
        assert highlight_match("plain text", []) == "plain text"

    def test_multibyte_utf8_offsets(self):
        # "café " is 5 chars but 6 bytes (é is 2 bytes in UTF-8); the
        # match's byte offsets must still land on "bar", not be thrown
        # off by the multi-byte character preceding it.
        result = highlight_match("café bar", [{"text": "bar", "start": 6, "end": 9}])
        assert result == f"café {colorize(Colors.BOLD_RED, 'bar')}"

    def test_out_of_range_submatch_skipped(self):
        result = highlight_match("short", [{"text": "x", "start": 50, "end": 51}])
        assert result == "short"

    def test_replacement_character_skips_highlighting(self):
        """Lossily-decoded lines (invalid UTF-8 in the source file) carry
        U+FFFD, whose re-encoded byte length wouldn't match the
        original offsets -- highlighting must be skipped rather than
        risk slicing at the wrong point."""
        line = "before � after"
        result = highlight_match(line, [{"text": "after", "start": 11, "end": 16}])
        assert result == line
