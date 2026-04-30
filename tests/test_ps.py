from __future__ import annotations

import json
from contextvars import copy_context

import pytest
from conftest import ContreeTestClient

from contree_cli import FORMATTER
from contree_cli.cli.ps import PAGE_SIZE, STATUS_CHOICES, PsArgs, cmd_ps
from contree_cli.output import CSVFormatter, JSONFormatter, TableFormatter
from contree_cli.types import parse_interval


def _run_cmd(tc: ContreeTestClient, operations, *, formatter=None, **kwargs):
    return _run_cmd_pages(tc, [operations], formatter=formatter, **kwargs)


def _run_cmd_pages(tc: ContreeTestClient, pages, *, formatter=None, **kwargs):
    for page in pages:
        tc.respond_json(page)

    FORMATTER.set(formatter or CSVFormatter())
    ctx = copy_context()

    if "since" in kwargs and isinstance(kwargs["since"], str):
        kwargs["since"] = parse_interval(kwargs["since"])
    if "until" in kwargs and isinstance(kwargs["until"], str):
        kwargs["until"] = parse_interval(kwargs["until"])

    args = PsArgs(**kwargs)
    ctx.run(cmd_ps, args)


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


class TestPsParams:
    def test_status_param(self, contree_client):
        _run_cmd(contree_client, [], status="FAILED")
        assert "status=FAILED" in contree_client.request_paths[0]

    def test_kind_param(self, contree_client):
        _run_cmd(contree_client, [], kind="instance")
        assert "kind=instance" in contree_client.request_paths[0]

    def test_since_param(self, contree_client):
        _run_cmd(contree_client, [], since="1h")
        path = contree_client.request_paths[0]
        assert "since=" in path

    def test_until_param(self, contree_client):
        _run_cmd(contree_client, [], until="2025-01-01")
        path = contree_client.request_paths[0]
        assert "until=" in path

    def test_no_filters_no_extra_params(self, contree_client):
        _run_cmd(contree_client, [])
        path = contree_client.request_paths[0]
        assert "kind" not in path


class TestPsPagination:
    def test_single_page(self, contree_client):
        ops = [_make_op(i) for i in range(5)]
        _run_cmd(contree_client, ops)
        assert contree_client.request_count == 1

    def test_multi_page(self, contree_client, capsys):
        page1 = [_make_op(i) for i in range(PAGE_SIZE)]
        page2 = [_make_op(i) for i in range(PAGE_SIZE, PAGE_SIZE + 3)]
        _run_cmd_pages(contree_client, [page1, page2])
        assert contree_client.request_count == 2
        out = capsys.readouterr().out
        assert f"op-{PAGE_SIZE + 2}" in out

    def test_offset_increments(self, contree_client):
        page1 = [_make_op(i) for i in range(PAGE_SIZE)]
        page2 = []
        _run_cmd_pages(contree_client, [page1, page2])
        paths = contree_client.request_paths
        assert "offset=0" in paths[0]
        assert f"offset={PAGE_SIZE}" in paths[1]


class TestPsActiveFilter:
    def test_default_sends_executing_status_to_server(self, contree_client, capsys):
        """Default ps sends status=EXECUTING to the server for filtering."""
        ops = [_make_op(0, status="EXECUTING")]
        _run_cmd(contree_client, ops)
        assert "status=EXECUTING" in contree_client.request_paths[0]
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
        assert "status=EXECUTING" in contree_client.request_paths[0]

    def test_all_flag_no_status_param(self, contree_client):
        """--all flag does not send a status filter to the server."""
        ops = [_make_op(0)]
        _run_cmd(contree_client, ops, all=True)
        assert "status=" not in contree_client.request_paths[0]

    def test_all_with_explicit_status(self, contree_client, capsys):
        """--all combined with --status sends the status filter."""
        ops = [_make_op(0, status="FAILED")]
        _run_cmd(contree_client, ops, all=True, status="FAILED")
        assert "status=FAILED" in contree_client.request_paths[0]
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
        assert f"status={full}" in contree_client.request_paths[0]


class TestPsShowMax:
    def test_show_max_truncates_output(self, contree_client, capsys):
        ops = [_make_op(i) for i in range(5)]
        _run_cmd(contree_client, ops, show_max=3, all=True)
        out = capsys.readouterr().out
        assert "op-0" in out
        assert "op-1" in out
        assert "op-2" not in out

    def test_show_max_logs_warning(self, contree_client, caplog):
        ops = [_make_op(i) for i in range(5)]
        _run_cmd(contree_client, ops, show_max=3, all=True)
        assert "show_max limit of 3" in caplog.text

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
        assert "show_max" not in caplog.text

    def test_show_max_stops_pagination(self, contree_client, capsys):
        """show_max stops iteration mid-page, no extra page fetch."""
        ops = [_make_op(i) for i in range(10)]
        _run_cmd(contree_client, ops, show_max=3, all=True)
        assert contree_client.request_count == 1

    def test_show_max_across_pages(self, contree_client, capsys):
        """show_max truncates across page boundaries."""
        page1 = [_make_op(i) for i in range(PAGE_SIZE)]
        page2 = [_make_op(i) for i in range(PAGE_SIZE, PAGE_SIZE + 5)]
        _run_cmd_pages(
            contree_client,
            [page1, page2],
            show_max=PAGE_SIZE + 2,
            all=True,
        )
        out = capsys.readouterr().out
        assert f"op-{PAGE_SIZE}" in out
        assert f"op-{PAGE_SIZE + 2}" not in out

    def test_show_max_one_shows_nothing(self, contree_client, capsys):
        """show_max=1 yields 0 ops (counter starts at 1, 1>=1 is true)."""
        ops = [_make_op(0)]
        _run_cmd(contree_client, ops, show_max=1, all=True)
        out = capsys.readouterr().out
        assert "op-0" not in out


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
