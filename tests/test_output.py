from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from contree_cli.output import (
    FORMATTERS,
    CSVFormatter,
    DefaultFormatter,
    JSONFormatter,
    JSONPrettyFormatter,
    PlainFormatter,
    TableFormatter,
    TSVFormatter,
    _fit_columns,
    _format_value,
    _json_default,
    _truncate,
)

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class TestCSVFormatter:
    def test_header_and_row(self, capsys):
        CSVFormatter()(name="alice", age=30)
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "name,age"
        assert lines[1] == "alice,30"

    def test_header_written_once(self, capsys):
        fmt = CSVFormatter()
        fmt(name="alice")
        fmt(name="bob")
        lines = capsys.readouterr().out.splitlines()
        assert lines.count("name") == 1

    def test_quoting(self, capsys):
        CSVFormatter()(val="has,comma")
        lines = capsys.readouterr().out.splitlines()
        assert '"has,comma"' in lines[1]


class TestTSVFormatter:
    def test_header_and_row(self, capsys):
        TSVFormatter()(name="alice", age=30)
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "name\tage"
        assert lines[1] == "alice\t30"

    def test_header_written_once(self, capsys):
        fmt = TSVFormatter()
        fmt(name="alice")
        fmt(name="bob")
        lines = capsys.readouterr().out.splitlines()
        assert lines.count("name") == 1

    def test_tab_in_value(self, capsys):
        TSVFormatter()(val="has\ttab")
        lines = capsys.readouterr().out.splitlines()
        # excel-tab dialect should quote it
        assert '"has\ttab"' in lines[1]


class TestJSONFormatter:
    def test_compact_json(self, capsys):
        JSONFormatter()(name="alice", count=3)
        line = capsys.readouterr().out
        assert json.loads(line) == {"name": "alice", "count": 3}
        assert "\n" not in line.rstrip("\n")

    def test_valid_json(self, capsys):
        JSONFormatter()(x=1)
        assert json.loads(capsys.readouterr().out) == {"x": 1}


class TestJSONPrettyFormatter:
    def test_single_item_is_list(self, capsys):
        fmt = JSONPrettyFormatter()
        fmt(a=1, b=2)
        fmt.flush()
        parsed = json.loads(capsys.readouterr().out)
        assert parsed == [{"a": 1, "b": 2}]

    def test_multiple_items(self, capsys):
        fmt = JSONPrettyFormatter()
        fmt(x=1)
        fmt(x=2)
        fmt.flush()
        parsed = json.loads(capsys.readouterr().out)
        assert parsed == [{"x": 1}, {"x": 2}]

    def test_indented(self, capsys):
        fmt = JSONPrettyFormatter()
        fmt(key="val")
        fmt.flush()
        assert "  " in capsys.readouterr().out

    def test_flush_empty(self, capsys):
        fmt = JSONPrettyFormatter()
        fmt.flush()
        assert capsys.readouterr().out == ""

    def test_flush_clears_buffer(self, capsys):
        fmt = JSONPrettyFormatter()
        fmt(k=1)
        fmt.flush()
        capsys.readouterr()
        fmt.flush()
        assert capsys.readouterr().out == ""


class TestTableFormatter:
    def test_single_row(self, capsys):
        fmt = TableFormatter()
        fmt(name="alice", age=30)
        fmt.flush()
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 2
        assert "NAME" in lines[0]
        assert "AGE" in lines[0]
        assert "alice" in lines[1]
        assert "30" in lines[1]

    def test_column_alignment(self, capsys):
        fmt = TableFormatter()
        fmt(name="ab", val="x")
        fmt(name="abcdef", val="y")
        fmt.flush()
        lines = capsys.readouterr().out.splitlines()
        # header and rows have consistent column widths
        assert len(lines) == 3
        # all lines should have the same length (padded)
        assert len(lines[0]) == len(lines[1]) == len(lines[2])

    def test_flush_empty(self, capsys):
        fmt = TableFormatter()
        fmt.flush()
        assert capsys.readouterr().out == ""

    def test_flush_clears_buffer(self, capsys):
        fmt = TableFormatter()
        fmt(x=1)
        fmt.flush()
        capsys.readouterr()
        fmt.flush()
        assert capsys.readouterr().out == ""

    def test_multiple_batches(self, capsys):
        fmt = TableFormatter()
        fmt(k="a")
        fmt.flush()
        first = capsys.readouterr().out
        fmt(k="b")
        fmt.flush()
        second = capsys.readouterr().out
        assert "a" in first
        assert "b" in second


class TestFormatValue:
    def test_datetime(self):
        dt = datetime(2025, 1, 15, 10, 30, 45, tzinfo=timezone.utc)
        assert _format_value(dt) == "2025-01-15 10:30:45"

    def test_timedelta_seconds(self):
        assert _format_value(timedelta(seconds=42)) == "42s"

    def test_timedelta_minutes(self):
        assert _format_value(timedelta(minutes=5, seconds=3)) == "5m3s"

    def test_timedelta_hours(self):
        assert _format_value(timedelta(hours=2, minutes=15)) == "2h15m"

    def test_float(self):
        assert _format_value(3.14159) == "3.14"

    def test_bool(self):
        assert _format_value(True) == "true"
        assert _format_value(False) == "false"

    def test_none(self):
        assert _format_value(None) == ""

    def test_string_passthrough(self):
        assert _format_value("hello") == "hello"

    def test_int_passthrough(self):
        assert _format_value(42) == "42"


class TestJsonDefault:
    def test_datetime_iso(self):
        dt = datetime(2025, 1, 15, 10, 30, 45, tzinfo=timezone.utc)
        assert _json_default(dt) == "2025-01-15T10:30:45+00:00"

    def test_timedelta_total_seconds(self):
        assert _json_default(timedelta(minutes=5)) == 300.0

    def test_json_with_datetime(self, capsys):
        dt = datetime(2025, 6, 1, tzinfo=timezone.utc)
        JSONFormatter()(ts=dt)
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ts"] == "2025-06-01T00:00:00+00:00"

    def test_json_with_timedelta(self, capsys):
        JSONFormatter()(dur=timedelta(hours=1))
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["dur"] == 3600.0


class TestDefaultFormatter:
    def test_is_table_formatter(self):
        assert issubclass(DefaultFormatter, TableFormatter)

    def test_isinstance_check(self):
        fmt = DefaultFormatter()
        assert isinstance(fmt, TableFormatter)
        assert isinstance(fmt, DefaultFormatter)

    def test_works_as_table(self, capsys):
        fmt = DefaultFormatter()
        fmt(name="alice", age=30)
        fmt.flush()
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 2
        assert "NAME" in lines[0]
        assert "alice" in lines[1]


class TestFormattersRegistry:
    def test_all_keys(self):
        expected = {"csv", "tsv", "json", "json-pretty", "plain", "table", "default"}
        try:
            import tomllib  # noqa: F401

            expected.add("toml")
        except ImportError:
            pass
        assert set(FORMATTERS) == expected

    def test_csv(self):
        assert FORMATTERS["csv"] is CSVFormatter

    def test_tsv(self):
        assert FORMATTERS["tsv"] is TSVFormatter

    def test_json(self):
        assert FORMATTERS["json"] is JSONFormatter

    def test_json_pretty(self):
        assert FORMATTERS["json-pretty"] is JSONPrettyFormatter

    def test_table(self):
        assert FORMATTERS["table"] is TableFormatter

    def test_plain(self):
        assert FORMATTERS["plain"] is PlainFormatter

    def test_default(self):
        assert FORMATTERS["default"] is DefaultFormatter


class TestPlainFormatter:
    def test_single_row(self, capsys):
        fmt = PlainFormatter()
        fmt(name="alice", age=30)
        out = capsys.readouterr().out
        assert "name: alice" in out
        assert "age:  30" in out

    def test_multiple_rows_separated(self, capsys):
        fmt = PlainFormatter()
        fmt(x="a")
        fmt(x="b")
        out = capsys.readouterr().out
        assert out.count("---") == 1

    def test_multiline_indented(self, capsys):
        fmt = PlainFormatter()
        fmt(key="line1\nline2\nline3")
        lines = capsys.readouterr().out.splitlines()
        assert "line1" in lines[0]
        # continuation lines indented to align with first line's value
        assert lines[1].startswith(" ")
        assert "line2" in lines[1]
        assert lines[2].startswith(" ")
        assert "line3" in lines[2]

    def test_no_separator_for_first_row(self, capsys):
        fmt = PlainFormatter()
        fmt(a="1")
        out = capsys.readouterr().out
        assert "---" not in out


class TestTruncate:
    def test_no_truncation_when_fits(self):
        assert _truncate("hello", 10) == "hello"

    def test_exact_fit(self):
        assert _truncate("hello", 5) == "hello"

    def test_truncates_with_ellipsis(self):
        assert _truncate("hello world", 6) == "hello\u2026"

    def test_width_2(self):
        assert _truncate("hello", 2) == "h\u2026"

    def test_width_1(self):
        # No room for ellipsis, just first char.
        assert _truncate("hello", 1) == "h"


class TestFitColumns:
    def test_no_shrink_when_fits(self):
        widths = {"a": 5, "b": 5}
        result, truncated = _fit_columns(widths, ["a", "b"], 10)
        assert result == {"a": 5, "b": 5}
        assert not truncated

    def test_shrinks_wide_column(self):
        widths = {"a": 3, "b": 50}
        result, truncated = _fit_columns(widths, ["a", "b"], 20)
        assert result["a"] == 3  # narrow column keeps its width
        assert result["b"] == 17  # gets the remainder
        assert truncated

    def test_fair_share_for_all_wide(self):
        widths = {"a": 50, "b": 50}
        result, truncated = _fit_columns(widths, ["a", "b"], 20)
        assert result["a"] == 10
        assert result["b"] == 10
        assert truncated

    def test_respects_minimum(self):
        widths = {"a": 50, "b": 50}
        result, truncated = _fit_columns(widths, ["a", "b"], 4)
        assert result["a"] >= 3
        assert result["b"] >= 3
        assert truncated


class TestTableTruncation:
    """Table formatter truncation with mocked TTY and terminal size."""

    def _flush_with_term_width(self, fmt, width, capsys):
        """Flush formatter pretending stdout is a TTY with given width."""
        with (
            patch("contree_cli.output.STDOUT_IS_A_TTY", True),
            patch(
                "contree_cli.output.shutil.get_terminal_size",
                return_value=os.terminal_size((width, 24)),
            ),
        ):
            fmt.flush()
        return capsys.readouterr()

    def test_truncates_wide_content(self, capsys):
        fmt = TableFormatter()
        fmt(name="alice", description="a" * 100)
        out = self._flush_with_term_width(fmt, 30, capsys)
        lines = out.out.splitlines()
        for line in lines:
            assert len(_strip_ansi(line)) <= 30

    def test_no_truncation_when_fits(self, capsys):
        fmt = TableFormatter()
        fmt(a="x", b="y")
        out = self._flush_with_term_width(fmt, 200, capsys)
        assert "\u2026" not in _strip_ansi(out.out)

    def test_warning_on_truncation(self, capsys, caplog):
        fmt = TableFormatter()
        fmt(col="a" * 100)
        self._flush_with_term_width(fmt, 20, capsys)
        assert any("--format json" in r.message for r in caplog.records)

    def test_no_warning_when_fits(self, capsys, caplog):
        fmt = TableFormatter()
        fmt(col="short")
        self._flush_with_term_width(fmt, 200, capsys)
        assert not any("truncated" in r.message for r in caplog.records)

    def test_multiline_with_truncation(self, capsys):
        fmt = TableFormatter()
        fmt(name="alice", info="short\n" + "x" * 100)
        out = self._flush_with_term_width(fmt, 30, capsys)
        lines = out.out.splitlines()
        for line in lines:
            assert len(_strip_ansi(line)) <= 30

    def test_no_truncation_when_not_tty(self, capsys):
        """capsys is not a TTY, so no truncation or colors."""
        fmt = TableFormatter()
        long_val = "a" * 200
        fmt(col=long_val)
        with patch("contree_cli.output.STDOUT_IS_A_TTY", False):
            fmt.flush()
        out = capsys.readouterr().out
        assert long_val in out
        assert "\033[" not in out

    def test_content_on_tty(self, capsys):
        fmt = TableFormatter()
        fmt(name="alice", age=30)
        out = self._flush_with_term_width(fmt, 200, capsys)
        plain = _strip_ansi(out.out)
        assert "NAME" in plain
        assert "AGE" in plain
        assert "alice" in plain
        assert "30" in plain

    def test_no_colors_when_not_tty(self, capsys):
        fmt = TableFormatter()
        fmt(name="alice")
        with patch("contree_cli.output.STDOUT_IS_A_TTY", False):
            fmt.flush()
        raw = capsys.readouterr().out
        assert "\033[" not in raw


try:
    import tomllib

    from contree_cli.output import TOMLFormatter

    class TestTOMLFormatter:
        def test_single_row(self, capsys):
            fmt = TOMLFormatter()
            fmt(name="alice", age=30)
            fmt.flush()
            raw = capsys.readouterr().out
            parsed = tomllib.loads(raw)
            assert parsed == {"results": [{"name": "alice", "age": 30}]}

        def test_multiple_rows(self, capsys):
            fmt = TOMLFormatter()
            fmt(x="a")
            fmt(x="b")
            fmt.flush()
            parsed = tomllib.loads(capsys.readouterr().out)
            assert len(parsed["results"]) == 2
            assert parsed["results"][0]["x"] == "a"
            assert parsed["results"][1]["x"] == "b"

        def test_escapes_special_chars(self, capsys):
            fmt = TOMLFormatter()
            fmt(val='has "quotes" and\nnewline')
            fmt.flush()
            raw = capsys.readouterr().out
            parsed = tomllib.loads(raw)
            assert parsed["results"][0]["val"] == 'has "quotes" and\nnewline'

        def test_flush_empty(self, capsys):
            fmt = TOMLFormatter()
            fmt.flush()
            assert capsys.readouterr().out == ""

        def test_flush_clears_buffer(self, capsys):
            fmt = TOMLFormatter()
            fmt(k="a")
            fmt.flush()
            capsys.readouterr()
            fmt.flush()
            assert capsys.readouterr().out == ""

        def test_registered_in_formatters(self):
            assert FORMATTERS["toml"] is TOMLFormatter

except ImportError:
    pass
