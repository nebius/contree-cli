from __future__ import annotations

import json
from contextvars import copy_context

import pytest
from conftest import ContreeTestClient
from contree_client.models import OperationSummary

from contree_cli import FORMATTER
from contree_cli.cli.operation import (
    STATUS_CHOICES,
)
from contree_cli.cli.operation import (
    ListArgs as PsArgs,
)
from contree_cli.cli.operation import (
    cmd_list as cmd_ps,
)
from contree_cli.output import CSVFormatter, JSONFormatter, TableFormatter
from contree_cli.types import parse_interval


def _run_cmd(tc: ContreeTestClient, operations, *, formatter=None, **kwargs):
    tc.mock(
        "iter_operations",
        [OperationSummary.from_dict(op) for op in operations],
    )

    FORMATTER.set(formatter or CSVFormatter())
    ctx = copy_context()

    if "since" in kwargs and isinstance(kwargs["since"], str):
        kwargs["since"] = parse_interval(kwargs["since"])
    if "until" in kwargs and isinstance(kwargs["until"], str):
        kwargs["until"] = parse_interval(kwargs["until"])

    args = PsArgs(**kwargs)
    ctx.run(cmd_ps, args)


def _list_calls(tc: ContreeTestClient):
    return tc.calls_for("iter_operations")


def _make_op(i, *, status="EXECUTING", kind="instance", duration=1.5):
    return {
        "uuid": f"op-{i}",
        "kind": kind,
        "status": status,
        "error": None,
        "duration": duration,
        "created_at": "2025-06-01T00:00:00Z",
    }


class TestCmdPs:
    def test_lists_operations(self, contree_client, capsys):
        ops = [_make_op(0), _make_op(1)]
        _run_cmd(contree_client, ops)
        out = capsys.readouterr().out
        assert "op-0" in out
        assert "op-1" in out
        assert "EXECUTING" in out

    def test_quiet_prints_uuids_only(self, contree_client, capsys):
        ops = [_make_op(0), _make_op(1)]
        _run_cmd(contree_client, ops, quiet=True)
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines == ["op-0", "op-1"]

    def test_empty_list(self, contree_client, capsys):
        _run_cmd(contree_client, [])
        assert capsys.readouterr().out == ""

    def test_null_duration(self, contree_client, capsys):
        op = _make_op(0, duration=None)
        _run_cmd(contree_client, [op])
        out = capsys.readouterr().out
        assert "op-0" in out

    def test_error_field(self, contree_client, capsys):
        op = _make_op(0, status="FAILED")
        op["error"] = "OOM killed"
        _run_cmd(contree_client, [op], all=True)
        out = capsys.readouterr().out
        assert "OOM killed" in out

    def test_json_output(self, contree_client, capsys):
        ops = [_make_op(0)]
        _run_cmd(contree_client, ops, formatter=JSONFormatter())
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["uuid"] == "op-0"
        assert parsed["duration"] == 1.5
        assert parsed["status"] == "EXECUTING"

    def test_table_output(self, contree_client, capsys):
        ops = [_make_op(0), _make_op(1)]
        fmt = TableFormatter()
        _run_cmd(contree_client, ops, formatter=fmt)
        fmt.flush()
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 3
        assert "UUID" in lines[0]


class TestPsDynamicFields:
    """List rows carry every scalar field of the OperationSummary schema."""

    def test_unknown_top_level_field_appears_in_row(self, contree_client, capsys):
        """Schema fields the handler does not hardcode show up in the row."""
        op = _make_op(0)
        op["consumed_cpu"] = 0.0042
        op["result_image_uuid"] = "img-result"
        _run_cmd(contree_client, [op], formatter=JSONFormatter())
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["consumed_cpu"] == 0.0042
        assert parsed["result_image_uuid"] == "img-result"

    def test_nested_dict_field_skipped(self, contree_client, capsys):
        """Nested structures (metadata, result) are filtered out of the row."""
        op = _make_op(0)
        op["metadata"] = {"big": "object"}
        op["result"] = {"image": "img-1"}
        _run_cmd(contree_client, [op], formatter=JSONFormatter())
        parsed = json.loads(capsys.readouterr().out)
        assert "metadata" not in parsed
        assert "result" not in parsed
        assert parsed["uuid"] == "op-0"

    def test_nested_list_field_skipped(self, contree_client, capsys):
        op = _make_op(0)
        op["tags"] = ["a", "b", "c"]
        _run_cmd(contree_client, [op], formatter=JSONFormatter())
        parsed = json.loads(capsys.readouterr().out)
        assert "tags" not in parsed

    def test_created_at_passes_through_iso(self, contree_client, capsys):
        """``created_at`` reaches the row as the ISO instant the API sent."""
        op = _make_op(0)
        op["created_at"] = "2025-06-01T01:00:00Z"
        _run_cmd(contree_client, [op], formatter=JSONFormatter())
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["created_at"].startswith("2025-06-01T01:00:00")

    def test_error_is_always_last_column(self, contree_client, capsys):
        """``error`` is pinned to the trailing position regardless of API order."""
        # Build an op where `error` is *not* the last key in insertion order.
        op = {
            "uuid": "op-0",
            "error": "boom",
            "status": "FAILED",
            "kind": "instance",
            "duration": 1.0,
            "created_at": "2025-06-01T00:00:00Z",
        }
        _run_cmd(contree_client, [op], formatter=JSONFormatter())
        parsed = json.loads(capsys.readouterr().out)
        keys = list(parsed.keys())
        assert keys[-1] == "error"
        assert parsed["error"] == "boom"

    def test_error_last_even_when_added_field_present(self, contree_client, capsys):
        """A schema field the handler does not hardcode appears before ``error``."""
        op = _make_op(0, status="FAILED")
        op["error"] = "oom"
        op["image_size"] = 2048  # schema field placed after `error` in the response
        _run_cmd(contree_client, [op], formatter=JSONFormatter())
        parsed = json.loads(capsys.readouterr().out)
        keys = list(parsed.keys())
        assert keys[-1] == "error"
        assert "image_size" in keys
        assert keys.index("image_size") < keys.index("error")


class TestPsParams:
    def test_status_param(self, contree_client):
        _run_cmd(contree_client, [], status="FAILED")
        assert _list_calls(contree_client)[0].kwargs["status"] == "FAILED"

    def test_kind_param(self, contree_client):
        _run_cmd(contree_client, [], kind="instance")
        assert _list_calls(contree_client)[0].kwargs["kind"] == "instance"

    def test_since_param(self, contree_client):
        """The parsed datetime goes to the client as-is; the library
        owns the wire formatting (format_time_param)."""
        from datetime import datetime

        _run_cmd(contree_client, [], since="1h")
        since = _list_calls(contree_client)[0].kwargs["since"]
        assert isinstance(since, datetime)

    def test_until_param(self, contree_client):
        from datetime import datetime

        _run_cmd(contree_client, [], until="2025-01-01")
        until = _list_calls(contree_client)[0].kwargs["until"]
        assert isinstance(until, datetime)

    def test_no_filters_no_extra_params(self, contree_client):
        _run_cmd(contree_client, [])
        kwargs = _list_calls(contree_client)[0].kwargs
        assert kwargs["kind"] is None
        assert kwargs["since"] is None
        assert kwargs["until"] is None


class TestPsPagination:
    """Offset pagination itself is the library's job (iter_operations);
    the CLI contract is a single iterator pass with the record budget
    forwarded as limit."""

    def test_single_iterator_pass(self, contree_client):
        ops = [_make_op(i) for i in range(5)]
        _run_cmd(contree_client, ops)
        assert len(_list_calls(contree_client)) == 1

    def test_limit_forwarded_with_probe(self, contree_client):
        """--show-max N asks the iterator for N+1 records so truncation
        is detectable."""
        _run_cmd(contree_client, [], show_max=7)
        assert _list_calls(contree_client)[0].kwargs["limit"] == 8

    def test_no_show_max_means_unbounded(self, contree_client):
        _run_cmd(contree_client, [], show_max=None)
        assert _list_calls(contree_client)[0].kwargs["limit"] is None

    def test_long_stream_fully_emitted(self, contree_client, capsys):
        ops = [_make_op(i) for i in range(25)]
        _run_cmd(contree_client, ops, show_max=None)
        out = capsys.readouterr().out
        assert "op-0" in out
        assert "op-24" in out

    def test_progress_logged_per_page(self, contree_client, caplog):
        """Every consumed page reports progress, so `ps -a` over a big
        history does not look hung while the next page loads."""
        import logging

        ops = [_make_op(i) for i in range(2500)]
        with caplog.at_level(logging.INFO, logger="contree_cli.cli.operation"):
            _run_cmd(contree_client, ops, show_max=None, all=True)
        progress = [
            r.getMessage() for r in caplog.records if "loading more" in r.getMessage()
        ]
        assert progress == [
            "Fetched 1000 operations, loading more...",
            "Fetched 2000 operations, loading more...",
        ]

    def test_no_progress_log_for_short_stream(self, contree_client, caplog):
        import logging

        ops = [_make_op(i) for i in range(5)]
        with caplog.at_level(logging.INFO, logger="contree_cli.cli.operation"):
            _run_cmd(contree_client, ops, show_max=None, all=True)
        assert "loading more" not in caplog.text


class TestPsActiveFilter:
    def test_default_sends_executing_status_to_server(self, contree_client, capsys):
        """Default ps sends status=EXECUTING to the server for filtering."""
        ops = [_make_op(0, status="EXECUTING")]
        _run_cmd(contree_client, ops)
        assert _list_calls(contree_client)[0].kwargs["status"] == "EXECUTING"
        out = capsys.readouterr().out
        assert "op-0" in out

    def test_all_flag_shows_everything(self, contree_client, capsys):
        ops = [
            _make_op(0, status="EXECUTING"),
            _make_op(1, status="SUCCESS"),
            _make_op(2, status="FAILED"),
        ]
        _run_cmd(contree_client, ops, all=True)
        out = capsys.readouterr().out
        assert "op-0" in out
        assert "op-1" in out
        assert "op-2" in out

    def test_explicit_status_overrides_active_filter(self, contree_client, capsys):
        ops = [_make_op(0, status="FAILED")]
        _run_cmd(contree_client, ops, status="FAILED")
        out = capsys.readouterr().out
        assert "op-0" in out

    def test_default_quiet_sends_status_filter(self, contree_client, capsys):
        """Quiet mode sends status filter, prints all returned UUIDs."""
        ops = [_make_op(0, status="EXECUTING")]
        _run_cmd(contree_client, ops, quiet=True)
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines == ["op-0"]
        assert _list_calls(contree_client)[0].kwargs["status"] == "EXECUTING"

    def test_all_flag_no_status_param(self, contree_client):
        """--all flag does not send a status filter to the server."""
        ops = [_make_op(0)]
        _run_cmd(contree_client, ops, all=True)
        assert _list_calls(contree_client)[0].kwargs["status"] is None

    def test_all_with_explicit_status(self, contree_client, capsys):
        """--all combined with --status sends the status filter."""
        ops = [_make_op(0, status="FAILED")]
        _run_cmd(contree_client, ops, all=True, status="FAILED")
        assert _list_calls(contree_client)[0].kwargs["status"] == "FAILED"
        assert "op-0" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "short,full",
        list(STATUS_CHOICES.items()),
    )
    def test_status_shortcut_expansion(
        self,
        contree_client,
        short,
        full,
    ):
        """Single-letter status shortcuts are expanded."""
        _run_cmd(contree_client, [], status=short)
        assert _list_calls(contree_client)[0].kwargs["status"] == full


class TestPsShowMax:
    def test_show_max_truncates_output(self, contree_client, capsys):
        """show_max caps emitted ops mid-page."""
        ops = [_make_op(i) for i in range(5)]
        _run_cmd(
            contree_client,
            ops,
            show_max=3,
            all=True,
        )
        out = capsys.readouterr().out
        assert "op-0" in out
        assert "op-1" in out
        assert "op-2" in out
        assert "op-3" not in out

    def test_show_max_logs_warning(self, contree_client, caplog):
        ops = [_make_op(i) for i in range(5)]
        _run_cmd(
            contree_client,
            ops,
            show_max=3,
            all=True,
        )
        assert "Output truncated at --show-max=3" in caplog.text

    def test_show_max_none_shows_all(self, contree_client, capsys):
        ops = [_make_op(i) for i in range(5)]
        _run_cmd(contree_client, ops, show_max=None, all=True)
        out = capsys.readouterr().out
        for i in range(5):
            assert f"op-{i}" in out

    def test_show_max_larger_than_ops(self, contree_client, capsys):
        ops = [_make_op(i) for i in range(3)]
        _run_cmd(contree_client, ops, show_max=100, all=True)
        out = capsys.readouterr().out
        for i in range(3):
            assert f"op-{i}" in out

    def test_show_max_no_warning_when_under_limit(
        self,
        contree_client,
        caplog,
    ):
        ops = [_make_op(i) for i in range(3)]
        _run_cmd(contree_client, ops, show_max=100, all=True)
        assert "Output truncated" not in caplog.text

    def test_show_max_stops_pagination(self, contree_client):
        """show_max stops mid-page without fetching further pages."""
        ops = [_make_op(i) for i in range(10)]
        _run_cmd(
            contree_client,
            ops,
            show_max=3,
            all=True,
        )
        assert len(_list_calls(contree_client)) == 1

    def test_show_max_truncates_stream(self, contree_client, capsys):
        ops = [_make_op(i) for i in range(12)]
        _run_cmd(
            contree_client,
            ops,
            show_max=10,
            all=True,
        )
        out = capsys.readouterr().out
        assert "op-9" in out
        assert "op-10" not in out

    def test_show_max_one_shows_one(self, contree_client, capsys):
        """show_max=1 emits exactly one op (no off-by-one)."""
        ops = [_make_op(0), _make_op(1)]
        _run_cmd(
            contree_client,
            ops,
            show_max=1,
            all=True,
        )
        out = capsys.readouterr().out
        assert "op-0" in out
        assert "op-1" not in out

    def test_show_max_no_warning_when_stream_fits(self, contree_client, caplog):
        """Exactly show_max ops in the stream means nothing was cut."""
        ops = [_make_op(i) for i in range(3)]
        _run_cmd(
            contree_client,
            ops,
            show_max=3,
            all=True,
        )
        assert "Output truncated" not in caplog.text

    def test_show_max_warning_after_table_flush(self, contree_client, caplog, capsys):
        """TableFormatter buffer is flushed before the warning is logged."""
        import logging

        ops = [_make_op(i) for i in range(5)]
        contree_client.mock(
            "iter_operations",
            [OperationSummary.from_dict(op) for op in ops],
        )

        FORMATTER.set(TableFormatter())
        ctx = copy_context()
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.operation"):
            ctx.run(cmd_ps, PsArgs(show_max=3, all=True))

        out = capsys.readouterr().out
        assert "op-0" in out
        assert "op-2" in out


class TestPsCreatedAtFormats:
    """Verify created_at parsing with various ISO 8601 formats from the API."""

    def _make_op_with_ts(self, uuid, ts):
        return {
            "uuid": uuid,
            "kind": "instance",
            "status": "EXECUTING",
            "error": None,
            "duration": 1.0,
            "created_at": ts,
        }

    def test_fractional_seconds_microseconds(self, contree_client, capsys):
        ops = [self._make_op_with_ts("ts-1", "2026-02-25T16:16:28.984413Z")]
        _run_cmd(contree_client, ops)
        assert "ts-1" in capsys.readouterr().out

    def test_fractional_seconds_milliseconds(self, contree_client, capsys):
        ops = [self._make_op_with_ts("ts-2", "2025-03-15T10:00:00.123Z")]
        _run_cmd(contree_client, ops)
        assert "ts-2" in capsys.readouterr().out

    def test_explicit_utc_offset(self, contree_client, capsys):
        ops = [self._make_op_with_ts("ts-3", "2026-02-25T16:16:28.984413+00:00")]
        _run_cmd(contree_client, ops)
        assert "ts-3" in capsys.readouterr().out

    def test_whole_seconds_z_suffix(self, contree_client, capsys):
        ops = [self._make_op_with_ts("ts-4", "2025-06-01T00:00:00Z")]
        _run_cmd(contree_client, ops)
        assert "ts-4" in capsys.readouterr().out

    def test_mixed_formats_in_single_page(self, contree_client, capsys):
        ops = [
            self._make_op_with_ts("mix-1", "2025-01-01T00:00:00Z"),
            self._make_op_with_ts("mix-2", "2026-02-16T21:25:30.265927Z"),
            self._make_op_with_ts("mix-3", "2025-07-04T12:00:00.500+00:00"),
        ]
        _run_cmd(contree_client, ops)
        out = capsys.readouterr().out
        assert "mix-1" in out
        assert "mix-2" in out
        assert "mix-3" in out
