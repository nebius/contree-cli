from __future__ import annotations

import base64
import json
from contextvars import copy_context

from conftest import ContreeTestClient
from contree_client.models import OperationResponse, decode_stream

from contree_cli import FORMATTER, SESSION_STORE
from contree_cli.cli.show import ShowArgs, cmd_show
from contree_cli.output import (
    CSVFormatter,
    DefaultFormatter,
    JSONFormatter,
    TableFormatter,
)


def _run_cmd(tc: ContreeTestClient, op, *, formatter=None, store):
    tc.mock("get_operation_status", OperationResponse.from_dict(op))

    FORMATTER.set(formatter or CSVFormatter())
    SESSION_STORE.set(store)
    ctx = copy_context()

    args = ShowArgs(uuid=op["uuid"])
    ctx.run(cmd_show, args)


def _make_op(
    *,
    uuid="op-abc",
    status="SUCCESS",
    kind="instance",
    duration=5.0,
    error=None,
    image="img-1",
    tag="latest",
    stdout=None,
    stderr=None,
    exit_code=None,
):
    if kind == "instance":
        metadata = {
            "command": "echo hi",
            "image": "img-base",
            "shell": True,
            "result": None,
        }
    else:
        metadata = {
            "registry": {"url": "docker://docker.io/busybox:latest"},
            "tag": "busybox:latest",
        }
    if stdout is not None or stderr is not None or exit_code is not None:
        state = {}
        if exit_code is not None:
            state["exit_code"] = exit_code
        metadata["result"] = {
            "stdout": stdout,
            "stderr": stderr,
            "state": state or None,
        }
    return {
        "uuid": uuid,
        "kind": kind,
        "status": status,
        "error": error,
        "duration": duration,
        "metadata": metadata,
        "result": {"image": image, "tag": tag},
    }


def _b64_stream(text, *, truncated=False):
    return {
        "value": base64.b64encode(text.encode()).decode(),
        "encoding": "base64",
        "truncated": truncated,
    }


def _ascii_stream(text, *, truncated=False):
    return {
        "value": text,
        "encoding": "ascii",
        "truncated": truncated,
    }


class TestDecodeStream:
    def test_base64(self):
        stream = _b64_stream("hello world")
        assert decode_stream(stream) == "hello world"

    def test_ascii(self):
        stream = _ascii_stream("plain text")
        assert decode_stream(stream) == "plain text"

    def test_none(self):
        assert decode_stream(None) == ""

    def test_empty_value(self):
        assert decode_stream({"value": "", "encoding": "ascii"}) == ""

    def test_missing_value(self):
        assert decode_stream({"encoding": "base64"}) == ""


class TestCmdShow:
    def test_shows_operation(self, contree_client, capsys, session_store):
        _run_cmd(contree_client, _make_op(), store=session_store)
        out = capsys.readouterr().out
        assert "op-abc" in out
        assert "SUCCESS" in out
        assert "instance" in out

    def test_request_uuid(self, contree_client, session_store):
        _run_cmd(contree_client, _make_op(), store=session_store)
        calls = contree_client.calls_for("get_operation_status")
        assert len(calls) == 1
        assert calls[0].args == ("op-abc",)

    def test_null_duration(self, contree_client, capsys, session_store):
        _run_cmd(contree_client, _make_op(duration=None), store=session_store)
        out = capsys.readouterr().out
        assert "op-abc" in out

    def test_error_displayed(self, contree_client, capsys, session_store):
        _run_cmd(
            contree_client,
            _make_op(status="FAILED", error="timeout"),
            store=session_store,
        )
        out = capsys.readouterr().out
        assert "timeout" in out

    def test_null_result(self, contree_client, capsys, session_store):
        op = _make_op()
        op["result"] = None
        _run_cmd(contree_client, op, store=session_store)
        out = capsys.readouterr().out
        assert "op-abc" in out

    def test_null_result_image_and_tag(self, contree_client, capsys, session_store):
        op = _make_op(image=None, tag=None)
        _run_cmd(contree_client, op, store=session_store)
        out = capsys.readouterr().out
        assert "op-abc" in out

    def test_json_output(self, contree_client, capsys, session_store):
        _run_cmd(
            contree_client, _make_op(), formatter=JSONFormatter(), store=session_store
        )
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["uuid"] == "op-abc"
        assert parsed["duration"] == 5.0

    def test_unknown_field_passes_through(self, contree_client, capsys, session_store):
        """Schema fields the handler does not hardcode reach the row.

        The payload round-trips through the typed OperationResponse
        model, so only fields the API schema declares survive; the
        handler itself must not need a code change for them.
        """
        op = _make_op()
        op["image_uuid"] = "img-src"
        op["consumed_cpu"] = 0.25
        _run_cmd(contree_client, op, formatter=JSONFormatter(), store=session_store)
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["image_uuid"] == "img-src"
        assert parsed["consumed_cpu"] == 0.25

    def test_table_output(self, contree_client, capsys, session_store):
        fmt = TableFormatter()
        _run_cmd(contree_client, _make_op(), formatter=fmt, store=session_store)
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 2
        assert "UUID" in lines[0]

    def test_image_import_kind(self, contree_client, capsys, session_store):
        _run_cmd(contree_client, _make_op(kind="image_import"), store=session_store)
        out = capsys.readouterr().out
        assert "image_import" in out

    def test_exit_code(self, contree_client, capsys, session_store):
        _run_cmd(contree_client, _make_op(exit_code=0), store=session_store)
        out = capsys.readouterr().out
        assert "0" in out

    def test_history_entry_id_resolution(self, contree_client, session_store, capsys):
        # Create history entry with operation UUID
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid="op-run")

        op = _make_op(uuid="op-run")
        contree_client.mock("get_operation_status", OperationResponse.from_dict(op))
        SESSION_STORE.set(session_store)
        FORMATTER.set(CSVFormatter())
        ctx = copy_context()

        args = ShowArgs(uuid="@2")
        ctx.run(cmd_show, args)

        out = capsys.readouterr().out
        assert "op-run" in out
        calls = contree_client.calls_for("get_operation_status")
        assert calls[0].args == ("op-run",)


class TestShowStdout:
    def test_stdout_base64(self, contree_client, capsys, session_store):
        _run_cmd(
            contree_client,
            _make_op(stdout=_b64_stream("hello\n")),
            formatter=DefaultFormatter(),
            store=session_store,
        )
        out = capsys.readouterr().out
        assert "hello\n" in out

    def test_stdout_ascii(self, contree_client, capsys, session_store):
        _run_cmd(
            contree_client,
            _make_op(stdout=_ascii_stream("world\n")),
            formatter=DefaultFormatter(),
            store=session_store,
        )
        out = capsys.readouterr().out
        assert "world\n" in out

    def test_stdout_no_trailing_newline(self, contree_client, capsys, session_store):
        _run_cmd(
            contree_client,
            _make_op(stdout=_ascii_stream("no newline")),
            formatter=DefaultFormatter(),
            store=session_store,
        )
        out = capsys.readouterr().out
        assert out.endswith("no newline\n")

    def test_no_stdout(self, contree_client, capsys, session_store):
        _run_cmd(
            contree_client,
            _make_op(),
            formatter=DefaultFormatter(),
            store=session_store,
        )
        captured = capsys.readouterr()
        # metadata only, no extra stdout content beyond key-value lines
        for line in captured.out.splitlines():
            assert "  " in line  # all lines are key-value pairs


class TestShowStderr:
    def test_stderr_base64(self, contree_client, capsys, session_store):
        _run_cmd(
            contree_client,
            _make_op(stderr=_b64_stream("error msg\n")),
            formatter=DefaultFormatter(),
            store=session_store,
        )
        err = capsys.readouterr().err
        assert "error msg\n" in err

    def test_stderr_no_trailing_newline(self, contree_client, capsys, session_store):
        _run_cmd(
            contree_client,
            _make_op(stderr=_ascii_stream("warn")),
            formatter=DefaultFormatter(),
            store=session_store,
        )
        err = capsys.readouterr().err
        assert err.endswith("warn\n")

    def test_both_stdout_and_stderr(self, contree_client, capsys, session_store):
        _run_cmd(
            contree_client,
            _make_op(
                stdout=_ascii_stream("out"),
                stderr=_ascii_stream("err"),
            ),
            formatter=DefaultFormatter(),
            store=session_store,
        )
        captured = capsys.readouterr()
        assert "out" in captured.out
        assert "err" in captured.err


class TestShowCaching:
    def test_terminal_op_cached(self, contree_client, session_store):
        """SUCCESS op is cached; second call skips API."""
        op = _make_op(status="SUCCESS")
        _run_cmd(contree_client, op, store=session_store)
        assert len(contree_client.calls_for("get_operation_status")) == 1

        _run_cmd(contree_client, op, store=session_store)
        # no new request (cached)
        assert len(contree_client.calls_for("get_operation_status")) == 1

    def test_failed_op_cached(self, contree_client, session_store):
        """FAILED op is also terminal and should be cached."""
        op = _make_op(status="FAILED", error="boom")
        _run_cmd(contree_client, op, store=session_store)
        assert len(contree_client.calls_for("get_operation_status")) == 1

        _run_cmd(contree_client, op, store=session_store)
        # no new request (cached)
        assert len(contree_client.calls_for("get_operation_status")) == 1

    def test_non_terminal_op_not_cached(self, contree_client, session_store):
        """EXECUTING op should not be cached; second call hits API."""
        op = _make_op(status="EXECUTING")
        _run_cmd(contree_client, op, store=session_store)
        assert len(contree_client.calls_for("get_operation_status")) == 1

        _run_cmd(contree_client, op, store=session_store)
        # new request (not cached)
        assert len(contree_client.calls_for("get_operation_status")) == 2


class TestStatusVerbatim:
    def test_success_with_nonzero_exit_keeps_status(
        self, contree_client, capsys, session_store
    ):
        """SUCCESS + exit_code!=0 reports SUCCESS; exit_code is its own column.

        Operation status is an orchestration verdict (did the API run the
        job?). What the sandbox process did with its exit code is a
        separate concern and lives in the `exit_code` column.
        """
        op = _make_op(status="SUCCESS", exit_code=1)
        _run_cmd(contree_client, op, store=session_store)
        out = capsys.readouterr().out
        assert "SUCCESS" in out
        assert "FAILED" not in out
        assert ",1," in out  # exit_code column

    def test_success_with_exit_code_zero_shows_success(
        self, contree_client, capsys, session_store
    ):
        op = _make_op(status="SUCCESS", exit_code=0)
        _run_cmd(contree_client, op, store=session_store)
        out = capsys.readouterr().out
        assert "SUCCESS" in out

    def test_success_with_no_exit_code_shows_success(
        self, contree_client, capsys, session_store
    ):
        op = _make_op(status="SUCCESS", exit_code=None)
        _run_cmd(contree_client, op, store=session_store)
        out = capsys.readouterr().out
        assert "SUCCESS" in out


class TestShowRaw:
    def test_raw_prints_server_payload_as_jsonl(
        self, contree_client, capsys, session_store
    ):
        # --raw skips formatter routing and derived columns and emits
        # JSONL: one operation per line, the server payload as the
        # typed OperationResponse model round-trips it, so multi-UUID
        # `op show --raw` streams cleanly into `jq -c` / `awk`.
        op = _make_op(status="SUCCESS", exit_code=1)
        op["consumed_memory"] = 4096
        contree_client.mock("get_operation_status", OperationResponse.from_dict(op))
        FORMATTER.set(JSONFormatter())
        SESSION_STORE.set(session_store)
        ctx = copy_context()
        ctx.run(cmd_show, ShowArgs(uuid="op-abc", raw=True))
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        # No derived columns (no `exit_code` flattening, no `image`/`tag`
        # promotion); every schema field of the payload is kept.
        assert parsed["status"] == "SUCCESS"
        assert parsed["consumed_memory"] == 4096
        assert "metadata" in parsed  # full nested structure preserved
        assert parsed["metadata"]["result"]["state"]["exit_code"] == 1
        # JSONL contract: exactly one line of compact JSON per op.
        assert "\n" not in lines[0]
