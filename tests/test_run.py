from __future__ import annotations

import base64
import io
import json
import logging
import os
import queue
from contextvars import copy_context
from unittest.mock import MagicMock, patch

import pytest
from conftest import ContreeTestClient
from contree_client.exceptions import (
    ContreeAPIError,
    NotFoundError,
    SSEStreamError,
)
from contree_client.models import (
    ClosableStreamRepr,
    File,
    FileResponse,
    InstanceSpawnResponse,
    OperationEvent,
    OperationResponse,
    StreamRepr,
)

from contree_cli import CLIENT, FORMATTER, SESSION_STORE
from contree_cli.cli.run import (
    RunArgs,
    StdinForwarder,
    TerminalSummary,
    _expand_mapped_files,
    _is_excluded,
    _local_file_cache_kind,
    build_op_from_summary,
    cmd_run,
    stream_events_until_close,
)
from contree_cli.mapped_file import MappedFile
from contree_cli.output import DefaultFormatter, JSONFormatter
from contree_cli.session import SessionStore

IMG_UUID = "a1b2c3d4-5678-9abc-def0-111111111111"
IMG_NEW = "c3d4e5f6-789a-bcde-f012-333333333333"
IMG_NEW2 = "d4e5f6a7-89ab-cdef-0123-444444444444"
IMG_SOME = "b2c3d4e5-6789-abcd-ef01-222222222222"

# A prepared mock outcome: (operation name, result model or exception).
MockSpec = tuple[str, object]


def _spawn_response(uuid: str = "op-1") -> MockSpec:
    return ("spawn_instance", InstanceSpawnResponse.from_dict({"uuid": uuid}))


def op_body(
    uuid: str = "op-1",
    status: str = "SUCCESS",
    *,
    exit_code: int | None = 0,
    stdout: dict | None = None,
    stderr: dict | None = None,
    duration: float | None = 2.0,
    error: str | None = None,
    image: str = IMG_NEW,
    state_extra: dict | None = None,
) -> dict:
    """Full GET /v1/operations/{uuid} payload as the API returns it."""
    state: dict = {}
    if exit_code is not None:
        state["exit_code"] = exit_code
    if state_extra:
        state.update(state_extra)
    instance_result: dict | None = None
    if stdout is not None or stderr is not None or state:
        instance_result = {
            "stdout": stdout,
            "stderr": stderr,
            "state": state or None,
        }
    # OperationInstanceMetadata requires `command` and `image` on the
    # wire; the API always echoes the spawn parameters back here.
    return {
        "uuid": uuid,
        "kind": "instance",
        "status": status,
        "error": error,
        "created_at": "2026-01-01T00:00:00+00:00",
        "duration": duration,
        "metadata": {
            "command": "echo",
            "image": IMG_UUID,
            "shell": False,
            "result": instance_result,
        },
        "result": {"image": image, "tag": "latest"},
    }


def _op_response(uuid: str = "op-1", status: str = "SUCCESS", **kwargs) -> MockSpec:
    return (
        "get_operation_status",
        OperationResponse.from_dict(op_body(uuid, status, **kwargs)),
    )


def tag_lookup(uuid: str) -> MockSpec:
    """GET /v1/images?tag=... resolution result."""
    return ("inspect_find_image_by_tag", uuid)


def file_info_response(uuid: str) -> MockSpec:
    """GET /v1/files/{sha256} dedup hit: the full File record."""
    return (
        "get_file",
        File.from_dict(
            {
                "uuid": uuid,
                "sha256": "0" * 64,
                "size": 7,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ),
    )


def file_missing_response() -> MockSpec:
    """GET /v1/files/{sha256} dedup miss: 404."""
    return ("get_file", NotFoundError(404, "not found"))


def file_upload_response(uuid: str, sha256: str = "0" * 64) -> MockSpec:
    """POST /v1/files success: uuid + sha256 + size."""
    return ("upload_file", FileResponse(uuid=uuid, sha256=sha256, size=7))


def apply_mocks(tc: ContreeTestClient, mocks: list[MockSpec]) -> None:
    """Queue mock outcomes; auto-mock the SSE stream as empty when the
    test doesn't care about it (the CLI always opens the event stream
    before falling back to the terminal GET)."""
    for name, value in mocks:
        if isinstance(value, BaseException):
            tc.mock(name, error=value)
        else:
            tc.mock(name, value)
    if all(name != "iter_operation_events" for name, _ in mocks):
        tc.mock("iter_operation_events", [])


def call_ops(tc: ContreeTestClient) -> list[str]:
    """Operation names of every recorded API call, in order."""
    return [c.operation for c in tc.calls]


def spawn_payload(tc: ContreeTestClient, index: int = 0) -> dict:
    """Reassemble the spawn_instance call into the wire-payload shape
    the old tests asserted against: positional (command, image) merged
    with the kwargs, FileSpec/StreamRepr models rendered as dicts."""
    call = tc.calls_for("spawn_instance")[index]
    payload: dict = {"command": call.args[0], "image": call.args[1], **call.kwargs}
    files = payload.get("files")
    if isinstance(files, dict):
        payload["files"] = {
            path: spec.to_dict() if hasattr(spec, "to_dict") else spec
            for path, spec in files.items()
        }
    stdin = payload.get("stdin")
    if stdin is not None and hasattr(stdin, "to_dict"):
        payload["stdin"] = stdin.to_dict()
    return payload


def _tty_stdin() -> MagicMock:
    """Return a mock stdin that reports as a TTY (no piped input)."""
    mock = MagicMock()
    mock.isatty.return_value = True
    return mock


def _run_cmd(
    tc: ContreeTestClient,
    args: RunArgs,
    mocks: list[MockSpec],
    *,
    store: SessionStore,
    formatter=None,
    stdin_mock: MagicMock | None = None,
):
    """Run cmd_run with prepared method-level mocks and mocked sleep.

    Tests that invoke cmd_run twice on the same client must queue all
    mocks in the first call (outcome queues are FIFO with a sticky
    tail) and pass [] on the second.
    """
    apply_mocks(tc, mocks)

    FORMATTER.set(formatter or JSONFormatter())
    SESSION_STORE.set(store)
    ctx = copy_context()

    with (
        patch("contree_cli.cli.run.time.sleep"),
        patch("contree_cli.cli.run.sys.stdin", stdin_mock or _tty_stdin()),
    ):
        rc = ctx.run(cmd_run, args)
    return rc


def _default_args(**overrides) -> RunArgs:
    defaults: dict = {
        "command_args": ["echo", "hello"],
    }
    defaults.update(overrides)
    return RunArgs(**defaults)


# ── Detach mode ──────────────────────────────────────────────────────────


class TestDetach:
    def test_detach_exits_after_spawn(self, contree_client, session_store, capsys):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(detach=True)
        rc = _run_cmd(contree_client, args, [_spawn_response()], store=session_store)
        assert rc is None
        out = capsys.readouterr().out
        assert "op-1" in out

    def test_detach_no_poll_request(self, contree_client, session_store):
        """Only the spawn call is made, no status poll."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(detach=True)
        _run_cmd(contree_client, args, [_spawn_response()], store=session_store)
        assert call_ops(contree_client) == ["spawn_instance"]

    def test_detach_shows_status(self, contree_client, session_store, capsys):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(detach=True)
        _run_cmd(
            contree_client,
            args,
            [_spawn_response()],
            store=session_store,
            formatter=JSONFormatter(),
        )
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["uuid"] == "op-1"
        assert parsed["status"] == "PENDING"


# ── Poll loop ────────────────────────────────────────────────────────────


class TestPollLoop:
    def test_poll_until_success(self, contree_client, session_store, capsys):
        """The event stream closes without a completion frame; the
        terminal-check GET serves the final state directly."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        rc = _run_cmd(contree_client, args, mocks, store=session_store)
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["status"] == "SUCCESS"

    def test_unknown_field_filtered_by_typed_client(
        self, contree_client, session_store, capsys
    ):
        """Typed response models keep only spec fields: unknown server
        fields are dropped by contree-client instead of passing through
        to JSON output, while known fields still land as-is."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        body = op_body(status="SUCCESS", exit_code=0)
        body["future_field"] = "anything"
        mocks: list[MockSpec] = [
            _spawn_response(),
            ("get_operation_status", OperationResponse.from_dict(body)),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        parsed = json.loads(capsys.readouterr().out)
        assert "future_field" not in parsed
        assert parsed["status"] == "SUCCESS"
        assert parsed["uuid"] == "op-1"

    def test_poll_default_shows_stdout(self, contree_client, session_store, capsys):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(
                status="SUCCESS",
                exit_code=0,
                stdout={"value": "hello\n", "encoding": "ascii"},
            ),
        ]
        rc = _run_cmd(
            contree_client,
            args,
            mocks,
            store=session_store,
            formatter=DefaultFormatter(),
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "hello\n" in out

    def test_poll_until_failed(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="FAILED", exit_code=None, error="timeout"),
        ]
        rc = _run_cmd(contree_client, args, mocks, store=session_store)
        assert rc == 1

    def test_poll_until_cancelled(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="CANCELLED", exit_code=None),
        ]
        rc = _run_cmd(contree_client, args, mocks, store=session_store)
        assert rc == 1

    def test_failed_with_exit_code(self, contree_client, session_store):
        """FAILED with exit code (e.g. timeout kill) returns the exit code."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="FAILED", exit_code=137, error="timeout"),
        ]
        rc = _run_cmd(contree_client, args, mocks, store=session_store)
        assert rc == 137

    def test_timed_out_logs_warning(self, contree_client, session_store, caplog):
        """``state.timed_out=true`` triggers a WARNING regardless of status.

        The API can report SUCCESS while still flagging the process as killed
        by the user-set timeout (signal=9, exit_code=-1, timed_out=true).
        """
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(timeout=60)
        mocks = [
            _spawn_response(),
            _op_response(
                status="SUCCESS",
                exit_code=-1,
                state_extra={"timed_out": True, "signal": 9},
            ),
        ]
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.run"):
            _run_cmd(contree_client, args, mocks, store=session_store)

        records = [r for r in caplog.records if "timed out" in r.getMessage()]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "60s" in records[0].getMessage()

    def test_failed_without_timeout_still_fatal(
        self, contree_client, session_store, caplog
    ):
        """A non-timeout FAILED keeps emitting at FATAL severity."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="FAILED", exit_code=1, error="oom"),
        ]
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.run"):
            _run_cmd(contree_client, args, mocks, store=session_store)

        ended = [r for r in caplog.records if "ended with status" in r.getMessage()]
        assert len(ended) == 1
        assert ended[0].levelno == logging.CRITICAL

    def test_success_without_timeout_logs_nothing(
        self, contree_client, session_store, caplog
    ):
        """Plain SUCCESS does not emit a timeout warning."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.run"):
            _run_cmd(contree_client, args, mocks, store=session_store)
        assert [r for r in caplog.records if "timed out" in r.getMessage()] == []

    def test_exit_code_propagated(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=42),
        ]
        rc = _run_cmd(contree_client, args, mocks, store=session_store)
        assert rc == 42

    def test_success_no_exit_code(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=None),
        ]
        rc = _run_cmd(contree_client, args, mocks, store=session_store)
        assert rc is None


# ── Ctrl+C cancellation ─────────────────────────────────────────────────


class TestCtrlC:
    @staticmethod
    def _run_ctrl_c(mocks: list[MockSpec], store: SessionStore) -> ContreeTestClient:
        """Run cmd_run with `iter_operation_events` raising KeyboardInterrupt,
        simulating the user hitting Ctrl-C while the CLI was waiting on
        SSE for the operation to terminate. Expects KeyboardInterrupt to
        ultimately propagate out of cmd_run (a second interruption, or a
        failure to deliver the signal)."""
        store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        tc = ContreeTestClient()
        apply_mocks(tc, mocks)

        CLIENT.set(tc)
        FORMATTER.set(JSONFormatter())
        SESSION_STORE.set(store)
        ctx = copy_context()

        with (
            patch("contree_cli.cli.run.sys.stdin", _tty_stdin()),
            pytest.raises(KeyboardInterrupt),
        ):
            ctx.run(cmd_run, args)
        return tc

    def test_ctrl_c_signals_main_process_first(self, session_store):
        """First Ctrl-C sends SIGINT to spid=1 and resumes streaming
        instead of tearing down the whole operation; once the operation
        then finishes cleanly, cmd_run returns normally without ever
        cancelling."""
        store = session_store
        store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        tc = ContreeTestClient()
        apply_mocks(
            tc,
            [
                _spawn_response(),
                ("iter_operation_events", KeyboardInterrupt()),
                ("operation_subprocess_kill", None),
                ("iter_operation_events", []),
                _op_response(status="SUCCESS"),
            ],
        )

        CLIENT.set(tc)
        FORMATTER.set(JSONFormatter())
        SESSION_STORE.set(store)
        ctx = copy_context()

        with patch("contree_cli.cli.run.sys.stdin", _tty_stdin()):
            ctx.run(cmd_run, args)

        kills = tc.calls_for("operation_subprocess_kill")
        assert len(kills) == 1
        assert kills[0].args == ("op-1",)
        assert kills[0].kwargs == {"spid": 1, "signal": "INT"}
        assert tc.calls_for("cancel_operation") == []

    def test_second_ctrl_c_cancels_operation(self, session_store):
        """A second Ctrl-C while waiting after the SIGINT force-cancels,
        same as the old single-shot behavior."""
        tc = self._run_ctrl_c(
            [
                _spawn_response(),
                ("iter_operation_events", KeyboardInterrupt()),
                ("operation_subprocess_kill", None),
                ("iter_operation_events", KeyboardInterrupt()),
                ("cancel_operation", None),
            ],
            session_store,
        )
        cancels = tc.calls_for("cancel_operation")
        assert len(cancels) == 1
        assert cancels[0].args == ("op-1",)

    def test_ctrl_c_delete_failure_still_raises(self, session_store):
        """If the cancel fails, KeyboardInterrupt is still re-raised."""
        self._run_ctrl_c(
            [
                _spawn_response(),
                ("iter_operation_events", KeyboardInterrupt()),
                ("operation_subprocess_kill", None),
                ("iter_operation_events", KeyboardInterrupt()),
                ("cancel_operation", NotFoundError(404, "not found")),
            ],
            session_store,
        )

    def test_sigint_send_failure_escalates_immediately(self, session_store):
        """If sending SIGINT itself fails, escalate to hard-cancel right
        away instead of waiting for a second Ctrl-C."""
        tc = self._run_ctrl_c(
            [
                _spawn_response(),
                ("iter_operation_events", KeyboardInterrupt()),
                ("operation_subprocess_kill", NotFoundError(404, "not found")),
                ("cancel_operation", None),
            ],
            session_store,
        )
        assert len(tc.calls_for("operation_subprocess_kill")) == 1
        cancels = tc.calls_for("cancel_operation")
        assert len(cancels) == 1
        assert cancels[0].args == ("op-1",)


class TestBrokenPipe:
    """`BrokenPipeError` from local stdio (shell piped output closed
    early, e.g. ``contree run ... | head``) must cancel the remote op
    and exit with 141 (128 + SIGPIPE) instead of being misinterpreted
    as a remote network drop."""

    @staticmethod
    def _run_broken_pipe(
        mocks: list[MockSpec], store: SessionStore
    ) -> ContreeTestClient:
        store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        tc = ContreeTestClient()
        apply_mocks(tc, mocks)

        CLIENT.set(tc)
        FORMATTER.set(JSONFormatter())
        SESSION_STORE.set(store)
        ctx = copy_context()

        with (
            patch(
                "contree_cli.cli.run.stream_events_until_close",
                side_effect=BrokenPipeError,
            ),
            patch("contree_cli.cli.run.sys.stdin", _tty_stdin()),
            # `cmd_run` reopens stdout to /dev/null on BrokenPipeError;
            # short-circuit that so pytest keeps its capture intact.
            patch("contree_cli.cli.run.os.dup2"),
            patch(
                "contree_cli.cli.run.os.open",
                return_value=os.open(os.devnull, os.O_RDONLY),
            ),
            patch("contree_cli.cli.run.os.close"),
            pytest.raises(SystemExit) as exc_info,
        ):
            ctx.run(cmd_run, args)
        assert exc_info.value.code == 141
        return tc

    def test_broken_pipe_cancels_operation(self, session_store):
        tc = self._run_broken_pipe(
            [_spawn_response(), ("cancel_operation", None)],
            session_store,
        )
        cancels = tc.calls_for("cancel_operation")
        assert len(cancels) == 1
        assert cancels[0].args == ("op-1",)

    def test_broken_pipe_delete_failure_still_exits_141(self, session_store):
        """Even if the cancel fails, we still exit 141 rather than
        re-raising BrokenPipeError."""
        self._run_broken_pipe(
            [
                _spawn_response(),
                ("cancel_operation", NotFoundError(404, "not found")),
            ],
            session_store,
        )


# ── File upload ──────────────────────────────────────────────────────────


class TestDirectoryAttachments:
    def test_expand_directory_respects_default_excludes(self, tmp_path):
        root = tmp_path / "src"
        root.mkdir()
        (root / "main.py").write_text("print('ok')\n")
        (root / ".env").write_text("SECRET=1\n")
        git_dir = root / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("[core]\n")
        pycache = root / "__pycache__"
        pycache.mkdir()
        (pycache / "x.pyc").write_bytes(b"x")

        mf = MappedFile.parse(f"{root}:/app")
        expanded = _expand_mapped_files([mf], [])

        instance_paths = {m.instance_path for m in expanded}
        assert "/app/main.py" in instance_paths
        assert "/app/.env" not in instance_paths
        assert "/app/.git/config" not in instance_paths
        assert "/app/__pycache__/x.pyc" not in instance_paths

    def test_expand_directory_custom_excludes(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "a.txt").write_text("a")
        (root / "skip.log").write_text("log")

        mf = MappedFile.parse(f"{root}:/app")
        expanded = _expand_mapped_files([mf], ["*.log"])

        instance_paths = {m.instance_path for m in expanded}
        assert "/app/a.txt" in instance_paths
        assert "/app/skip.log" not in instance_paths


class TestFileUpload:
    def test_file_upload(self, contree_client, session_store, tmp_path):
        """upload_file is called for each attached file after dedup miss."""
        session_store.set_image(IMG_UUID, kind="test")
        host_file = tmp_path / "data.txt"
        host_file.write_text("content")
        mf = MappedFile(
            host_path=str(host_file),
            instance_path="/app/data.txt",
            uid=0,
            gid=0,
            mode=0o644,
        )
        args = _default_args(file=[mf])

        mocks = [
            file_missing_response(),  # GET dedup miss
            file_upload_response("file-uuid-1"),  # POST /v1/files
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        rc = _run_cmd(contree_client, args, mocks, store=session_store)
        assert rc == 0

        # Verify dedup check then file upload, before the spawn
        assert call_ops(contree_client)[:3] == [
            "get_file",
            "upload_file",
            "spawn_instance",
        ]

    def test_file_uuid_in_spawn_payload(self, contree_client, session_store, tmp_path):
        """Uploaded file UUID appears in the spawn payload."""
        session_store.set_image(IMG_UUID, kind="test")
        host_file = tmp_path / "script.sh"
        host_file.write_text("#!/bin/sh")
        mf = MappedFile(
            host_path=str(host_file),
            instance_path="/app/script.sh",
            uid=1000,
            gid=1000,
            mode=0o755,
        )
        args = _default_args(file=[mf])

        mocks = [
            file_missing_response(),  # GET dedup miss
            file_upload_response("file-42"),
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)

        body = spawn_payload(contree_client)
        assert "/app/script.sh" in body["files"]
        assert body["files"]["/app/script.sh"]["uuid"] == "file-42"
        assert body["files"]["/app/script.sh"]["uid"] == 1000

    def test_file_dedup_skips_upload(self, contree_client, session_store, tmp_path):
        """get_file returns the record -> no upload, UUID reused."""
        session_store.set_image(IMG_UUID, kind="test")
        host_file = tmp_path / "data.txt"
        host_file.write_text("content")
        mf = MappedFile(
            host_path=str(host_file),
            instance_path="/app/data.txt",
            uid=0,
            gid=0,
            mode=0o644,
        )
        args = _default_args(file=[mf])

        mocks = [
            file_info_response("existing-uuid"),  # GET dedup hit
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        rc = _run_cmd(contree_client, args, mocks, store=session_store)
        assert rc == 0

        assert call_ops(contree_client)[:2] == ["get_file", "spawn_instance"]
        assert contree_client.calls_for("upload_file") == []

        # Spawn uses the existing UUID
        body = spawn_payload(contree_client)
        assert body["files"]["/app/data.txt"]["uuid"] == "existing-uuid"

    def test_file_dedup_logs_reuse(
        self, contree_client, session_store, tmp_path, caplog
    ):
        """Reuse is logged when file already exists on server."""
        session_store.set_image(IMG_UUID, kind="test")
        host_file = tmp_path / "data.txt"
        host_file.write_text("content")
        mf = MappedFile(
            host_path=str(host_file),
            instance_path="/app/data.txt",
            uid=0,
            gid=0,
            mode=0o644,
        )
        args = _default_args(file=[mf])

        mocks = [
            file_info_response("existing-uuid"),  # GET dedup hit
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        with caplog.at_level(logging.INFO):
            _run_cmd(contree_client, args, mocks, store=session_store)
        assert "File reused:" in caplog.text
        assert "existing-uuid" in caplog.text

    def test_file_dedup_non_404_raises(self, contree_client, session_store, tmp_path):
        """Non-404 error from get_file propagates."""
        session_store.set_image(IMG_UUID, kind="test")
        host_file = tmp_path / "data.txt"
        host_file.write_text("content")
        mf = MappedFile(
            host_path=str(host_file),
            instance_path="/app/data.txt",
            uid=0,
            gid=0,
            mode=0o644,
        )
        args = _default_args(file=[mf])

        mocks: list[MockSpec] = [("get_file", ContreeAPIError(403, "forbidden"))]
        with pytest.raises(ContreeAPIError) as exc_info:
            _run_cmd(contree_client, args, mocks, store=session_store)
        assert exc_info.value.status == 403

    def test_local_file_cache_skips_api_file_lookup(
        self, contree_client, session_store, tmp_path
    ):
        session_store.set_image(IMG_UUID, kind="test")
        host_file = tmp_path / "cached.txt"
        host_file.write_text("content")
        mf = MappedFile(
            host_path=str(host_file),
            instance_path="/app/cached.txt",
            uid=0,
            gid=0,
            mode=0o644,
        )
        cache_kind = _local_file_cache_kind(str(host_file))
        import time

        session_store.cache[("", cache_kind)] = {
            "uuid": "cached-uuid",
            "uploaded_at": time.time(),
        }

        args = _default_args(file=[mf])
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)

        # spawn + poll only, no get_file/upload_file
        assert call_ops(contree_client)[0] == "spawn_instance"
        assert contree_client.calls_for("get_file") == []
        assert contree_client.calls_for("upload_file") == []
        body = spawn_payload(contree_client)
        assert body["files"]["/app/cached.txt"]["uuid"] == "cached-uuid"

    def test_local_file_cache_invalidated_when_file_changes(
        self, contree_client, session_store, tmp_path
    ):
        session_store.set_image(IMG_UUID, kind="test")
        host_file = tmp_path / "cached-change.txt"
        host_file.write_text("v1")
        mf = MappedFile(
            host_path=str(host_file),
            instance_path="/app/cached-change.txt",
            uid=0,
            gid=0,
            mode=0o644,
        )

        old_kind = _local_file_cache_kind(str(host_file))
        session_store.cache[("", old_kind)] = "old-uuid"

        st = host_file.stat()
        host_file.write_text("v2")
        os.utime(host_file, ns=(st.st_atime_ns, st.st_mtime_ns + 1))

        args = _default_args(file=[mf])
        mocks = [
            file_info_response("new-uuid"),  # GET dedup hit for new content
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)

        assert call_ops(contree_client)[0] == "get_file"
        body = spawn_payload(contree_client)
        assert body["files"]["/app/cached-change.txt"]["uuid"] == "new-uuid"


class TestParallelUpload:
    def test_upload_files_parallel_aggregates(
        self, contree_client, session_store, tmp_path, monkeypatch
    ):
        """upload_files dispatches via ThreadPool and collects all uuids."""
        from contree_cli.cli import run as run_mod

        files = []
        for i in range(5):
            p = tmp_path / f"f{i}.txt"
            p.write_text(f"content-{i}")
            files.append(
                MappedFile(
                    host_path=str(p),
                    instance_path=f"/app/f{i}.txt",
                    uid=0,
                    gid=0,
                    mode=0o644,
                )
            )

        def fake_remote(client, mf):
            return mf, f"uuid-for-{os.path.basename(mf.host_path)}"

        monkeypatch.setattr(run_mod, "upload_one_remote", fake_remote)

        result = run_mod.upload_files(contree_client, files, session_store)

        assert {mf.host_path for mf in files} == set(result.keys())
        for mf in files:
            assert result[mf.host_path] == (
                f"uuid-for-{os.path.basename(mf.host_path)}"
            )

    def test_upload_files_skips_cached(
        self, contree_client, session_store, tmp_path, monkeypatch
    ):
        """Files already in the local cache must not hit the upload pool."""
        from contree_cli.cli import run as run_mod

        p = tmp_path / "cached.txt"
        p.write_text("data")
        mf = MappedFile(
            host_path=str(p),
            instance_path="/app/cached.txt",
            uid=0,
            gid=0,
            mode=0o644,
        )
        run_mod.record_local_uuid(mf, "cached-uuid", session_store)

        called = []

        def fake_remote(client, mfx):
            called.append(mfx.host_path)
            return mfx, "should-not-be-used"

        monkeypatch.setattr(run_mod, "upload_one_remote", fake_remote)

        result = run_mod.upload_files(contree_client, [mf], session_store)
        assert result == {mf.host_path: "cached-uuid"}
        assert called == []


# ── Spawn payload ────────────────────────────────────────────────────────


class TestSpawnPayload:
    def _get_payload(
        self, tc: ContreeTestClient, args: RunArgs, store: SessionStore
    ) -> dict:
        store.set_image(IMG_UUID, kind="test")
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(tc, args, mocks, store=store)
        return spawn_payload(tc)

    def test_basic_fields(self, contree_client, session_store):
        args = _default_args()
        payload = self._get_payload(contree_client, args, session_store)
        assert payload["image"] == IMG_UUID
        assert payload["command"] == "echo"
        assert payload["args"] == ["hello"]
        assert payload["shell"] is False
        assert payload["disposable"] is False
        assert payload["hostname"] == "linuxkit"

    def test_timeout_included(self, contree_client, session_store):
        args = _default_args(timeout=60)
        payload = self._get_payload(contree_client, args, session_store)
        assert payload["timeout"] == 60

    def test_cwd_included(self, contree_client, session_store):
        args = _default_args(cwd="/app")
        payload = self._get_payload(contree_client, args, session_store)
        assert payload["cwd"] == "/app"

    def test_cwd_empty_omitted(self, contree_client, session_store):
        args = _default_args(cwd="")
        payload = self._get_payload(contree_client, args, session_store)
        assert "cwd" not in payload

    def test_cwd_defaults_to_session_cwd(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        session_store.set_cwd("/work")
        args = _default_args(cwd="")
        payload = self._get_payload(contree_client, args, session_store)
        assert payload["cwd"] == "/work"

    def test_truncate_field(self, contree_client, session_store):
        args = _default_args(truncate=1024)
        payload = self._get_payload(contree_client, args, session_store)
        assert payload["truncate_output_at"] == 1024

    def test_single_command_no_args(self, contree_client, session_store):
        args = _default_args(command_args=["ls"])
        payload = self._get_payload(contree_client, args, session_store)
        assert payload["command"] == "ls"
        assert "args" not in payload

    def test_empty_command(self, contree_client, session_store):
        args = _default_args(command_args=[])
        payload = self._get_payload(contree_client, args, session_store)
        assert payload["command"] == ""
        assert "args" not in payload


# ── Tag resolution ───────────────────────────────────────────────────────


class TestTagResolution:
    def test_tag_resolved_before_spawn(self, contree_client, session_store):
        session_store.set_image("tag:latest", kind="test")
        args = _default_args()
        mocks = [
            tag_lookup("resolved-uuid"),  # GET /v1/images?tag=latest
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)

        # First call is the tag lookup
        assert call_ops(contree_client)[0] == "inspect_find_image_by_tag"
        lookup = contree_client.calls_for("inspect_find_image_by_tag")[0]
        assert lookup.args == ("latest",)

        # Spawn uses resolved UUID
        body = spawn_payload(contree_client)
        assert body["image"] == "resolved-uuid"

    def test_uuid_passthrough(self, contree_client, session_store):
        session_store.set_image(IMG_SOME, kind="test")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        assert contree_client.calls_for("inspect_find_image_by_tag") == []
        body = spawn_payload(contree_client)
        assert body["image"] == IMG_SOME


# ── Env parsing ──────────────────────────────────────────────────────────


class TestEnvParsing:
    def test_env_key_value(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(env=["FOO=bar", "BAZ=qux"])
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["env"] == {"FOO": "bar", "BAZ": "qux"}

    def test_env_empty_value(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(env=["KEY="])
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["env"] == {"KEY": ""}

    def test_no_env_omitted(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(env=[])
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert "env" not in body

    def test_session_env_no_auto_preserve(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        session_store.set_env("PATH", "/usr/bin:/bin")
        args = _default_args(env=[])
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["env"] == {"PATH": "/usr/bin:/bin"}
        assert "preserve_env" not in body

    def test_session_env_with_per_run_override(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        session_store.set_env("PATH", "/usr/bin:/bin")
        args = _default_args(env=["DEBUG=1"])
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["env"] == {"PATH": "/usr/bin:/bin", "DEBUG": "1"}
        assert "preserve_env" not in body

    def test_session_env_with_preserve_flag(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        session_store.set_env("PATH", "/usr/bin:/bin")
        args = _default_args(preserve_env=True)
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["env"] == {"PATH": "/usr/bin:/bin"}
        assert body["preserve_env"] is True

    def test_preserve_env_flag(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(preserve_env=True)
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["preserve_env"] is True

    def test_preserve_env_with_per_run_env(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(env=["FOO=bar"], preserve_env=True)
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["env"] == {"FOO": "bar"}
        assert body["preserve_env"] is True

    def test_preserved_env_not_resent(self, contree_client, session_store):
        """After preserve_env run, same env is not resent on next run."""
        session_store.set_image(IMG_UUID, kind="test")
        session_store.set_env("PATH", "/usr/bin")
        new_img = "00000000-0000-0000-0000-000000000099"

        # First run: preserve env. Mocks for both runs are queued up
        # front (outcome queues pop in FIFO order across invocations).
        args1 = _default_args(preserve_env=True)
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=new_img),
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args1, mocks, store=session_store)

        # Second run: same env, should skip sending it
        args2 = _default_args()
        _run_cmd(contree_client, args2, [], store=session_store)
        body = spawn_payload(contree_client, index=1)
        assert "env" not in body

    def test_preserved_env_resent_after_rollback(self, contree_client, session_store):
        """After rollback to image without preserved env, env is sent again."""
        session_store.set_image(IMG_UUID, kind="test")
        session_store.set_env("PATH", "/usr/bin")
        new_img = "00000000-0000-0000-0000-000000000099"

        # Run with preserve (mocks for both runs queued up front)
        args1 = _default_args(preserve_env=True)
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=new_img),
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args1, mocks, store=session_store)

        # Rollback to original image (no preserved env)
        session_store.rollback(1)

        # Run again: env must be sent because original image has no preserved env
        args2 = _default_args()
        _run_cmd(contree_client, args2, [], store=session_store)
        body = spawn_payload(contree_client, index=1)
        assert body["env"] == {"PATH": "/usr/bin"}


# ── Shell mode ───────────────────────────────────────────────────────────


class TestShellMode:
    def test_shell_joins_command(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(
            command_args=["echo", "hello", "world"],
            shell=True,
        )
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["command"] == "echo hello world"
        assert body["shell"] is True
        assert "args" not in body

    def test_non_shell_splits_command(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(
            command_args=["echo", "hello", "world"],
            shell=False,
        )
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["command"] == "echo"
        assert body["args"] == ["hello", "world"]
        assert body["shell"] is False

    def test_non_shell_passes_args_raw(self, contree_client, session_store):
        """Non-shell mode: command + args go to direct exec, no shell quoting.

        The API exec's argv directly, so adding shell quotes would put
        literal quote characters into the program's argv.
        """
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(
            command_args=["python3", "-c", "print('hello world')"],
            shell=False,
        )
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["command"] == "python3"
        assert body["args"] == ["-c", "print('hello world')"]

    def test_shell_quotes_arg_with_spaces(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(
            command_args=["python3", "-c", "print('hello world')"],
            shell=True,
        )
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        # shlex.join must round-trip back through shlex.split to the
        # original argv when the remote shell parses the command.
        import shlex

        assert shlex.split(body["command"]) == [
            "python3",
            "-c",
            "print('hello world')",
        ]

    def test_shell_does_not_overquote_simple_tokens(
        self, contree_client, session_store
    ):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(
            command_args=["ls", "-la", "/etc"],
            shell=True,
        )
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["command"] == "ls -la /etc"

    def test_shell_passes_single_expression_verbatim(
        self, contree_client, session_store
    ):
        """Single arg is treated as a pre-formed shell expression.

        `contree run -s -- 'echo 1 ; echo 2'` produces command_args with one
        element. Wrapping it via shlex.join would quote the whole string and
        sh -c would try to exec the literal as a command name.
        """
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(
            command_args=["echo 1 ; echo 2"],
            shell=True,
        )
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["command"] == "echo 1 ; echo 2"
        assert body["shell"] is True


# ── Session update on success ────────────────────────────────────────────


class TestStdinHandling:
    def test_skips_unready_stdin(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="use")
        read_fd, write_fd = os.pipe()
        os.close(write_fd)  # immediate EOF, nothing at all

        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        fake = os.fdopen(read_fd, "rb", buffering=0)
        _run_cmd(contree_client, args, mocks, store=session_store, stdin_mock=fake)
        body = spawn_payload(contree_client)
        assert "stdin" not in body

    def test_reads_ready_stdin(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="use")
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"echo hi\n")
        os.close(write_fd)

        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        fake = os.fdopen(read_fd, "rb", buffering=0)
        _run_cmd(contree_client, args, mocks, store=session_store, stdin_mock=fake)
        body = spawn_payload(contree_client)
        assert "stdin" in body
        assert body["stdin"]["value"]


class TestSessionUpdate:
    def test_success_updates_session(self, contree_client, session_store):
        """On SUCCESS with new image, session is updated."""
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        assert session_store.current_image == IMG_NEW
        s = session_store.session
        assert s is not None
        assert s.last_kind == "run"

    def test_disposable_creates_branch_no_image_update(
        self, contree_client, session_store
    ):
        """Disposable runs create disposable branch without changing image."""
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args(disposable=True)
        mocks = [
            _spawn_response("op-dispose"),
            _op_response("op-dispose", status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        assert session_store.current_image == IMG_UUID
        branches = dict(session_store.list_branches())
        assert "disposable-op-dispose" in branches
        assert branches["disposable-op-dispose"] is False

    def test_disposable_detach_creates_branch(
        self, contree_client, session_store, capsys
    ) -> None:
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args(disposable=True, detach=True)
        _run_cmd(
            contree_client,
            args,
            [_spawn_response("op-dispose-det")],
            store=session_store,
        )
        branches = dict(session_store.list_branches())
        assert "disposable-op-dispose-det" in branches
        assert branches["disposable-op-dispose-det"] is False

    def test_disposable_does_not_update_session(self, contree_client, session_store):
        """Disposable runs do not update the session image."""
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args(disposable=True)
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        assert session_store.current_image == IMG_UUID

    def test_failed_does_not_update_session(self, contree_client, session_store):
        """Failed runs do not update the session image."""
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="FAILED", exit_code=None, error="timeout"),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        assert session_store.current_image == IMG_UUID


# ── Operation caching ─────────────────────────────────────────────────────


class TestOperationCaching:
    def test_terminal_op_cached_after_run(self, contree_client, session_store):
        """Completed run caches the operation so `show` skips the API."""
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args()
        mocks = [
            _spawn_response("op-cached"),
            _op_response("op-cached", status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        cached = session_store.cache.get(("op-cached", "operation"))
        assert cached is not None
        assert cached["status"] == "SUCCESS"

    def test_failed_op_cached_after_run(self, contree_client, session_store):
        """Failed runs also cache the terminal operation."""
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args()
        mocks = [
            _spawn_response("op-fail"),
            _op_response("op-fail", status="FAILED", exit_code=None, error="boom"),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        cached = session_store.cache.get(("op-fail", "operation"))
        assert cached is not None
        assert cached["status"] == "FAILED"

    def test_detach_does_not_cache(self, contree_client, session_store):
        """Detached runs exit before terminal state, nothing to cache."""
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args(detach=True)
        _run_cmd(
            contree_client, args, [_spawn_response("op-detach")], store=session_store
        )
        assert session_store.cache.get(("op-detach", "operation")) is None


# ── Pending file inclusion ────────────────────────────────────────────────


class TestPendingFileInclusion:
    def test_pending_files_in_spawn_payload(self, contree_client, session_store):
        """Pending files from file edit are included in the spawn payload."""
        session_store.set_image(IMG_UUID, kind="use")
        hid = session_store.set_image(
            IMG_UUID,
            kind="file",
            title="Change file /app/config.ini",
        )
        session_store.add_pending_file(hid, "/app/config.ini", "pf-uuid-1")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert "/app/config.ini" in body["files"]
        assert body["files"]["/app/config.ini"]["uuid"] == "pf-uuid-1"

    def test_explicit_file_overrides_pending(
        self, contree_client, session_store, tmp_path
    ):
        """Explicit --file takes priority over pending file with same path."""
        session_store.set_image(IMG_UUID, kind="use")
        hid = session_store.set_image(
            IMG_UUID,
            kind="file",
            title="Change file /app/data.txt",
        )
        session_store.add_pending_file(hid, "/app/data.txt", "pending-uuid")
        # Create explicit file for the same path
        host_file = tmp_path / "data.txt"
        host_file.write_text("content")
        mf = MappedFile(
            host_path=str(host_file),
            instance_path="/app/data.txt",
            uid=0,
            gid=0,
            mode=0o644,
        )
        args = _default_args(file=[mf])
        mocks = [
            file_info_response("explicit-uuid"),  # GET dedup hit
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        # Explicit file should win
        assert body["files"]["/app/data.txt"]["uuid"] == "explicit-uuid"

    def test_not_included_after_run(self, contree_client, session_store):
        """After a successful run, pending files are no longer included."""
        session_store.set_image(IMG_UUID, kind="use")
        hid = session_store.set_image(
            IMG_UUID,
            kind="file",
            title="Change file /a.txt",
        )
        session_store.add_pending_file(hid, "/a.txt", "pf-uuid")
        # First run -- includes pending file. Mocks for both runs are
        # queued up front (FIFO outcome queues, sticky tail).
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW2),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        # Second run -- pending file should NOT be included (last entry is run)
        _run_cmd(contree_client, args, [], store=session_store)
        body = spawn_payload(contree_client, index=1)
        assert "files" not in body

    def test_reappears_after_rollback(self, contree_client, session_store):
        """After rollback past a run, pending files are included again."""
        session_store.set_image(IMG_UUID, kind="use")
        hid = session_store.set_image(
            IMG_UUID,
            kind="file",
            title="Change file /a.txt",
        )
        session_store.add_pending_file(hid, "/a.txt", "pf-uuid")
        # Run -- bakes the file in (mocks for both runs queued up front)
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW2),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        assert session_store.pending_files() == []
        # Rollback past the run
        session_store.rollback(1)
        assert len(session_store.pending_files()) == 1
        # Next run should include the pending file again
        _run_cmd(contree_client, args, [], store=session_store)
        body = spawn_payload(contree_client, index=1)
        assert "/a.txt" in body["files"]


# --- Stdin forwarder ---


class TestStdinForwarder:
    def test_ambiguous_failure_is_not_retried(self):
        """A 504 on the non-idempotent stdin POST is ambiguous (may or
        may not have been delivered) -- retrying it can duplicate data,
        so a single failed attempt must stop forwarding, not retry."""
        tc = ContreeTestClient()
        tc.respond_raw(status=504, body=b'{"error": "timeout"}')
        q: queue.Queue[ClosableStreamRepr] = queue.Queue()
        q.put(ClosableStreamRepr(value="aGk=", encoding="base64", close=False))
        forwarder = StdinForwarder(tc, "op-1", q)
        forwarder.run()
        assert forwarder.error is not None
        assert len(tc.raw_requests) == 1

    def test_success_does_not_touch_raw_client(self):
        tc = ContreeTestClient()
        tc.respond_raw(status=200)
        q: queue.Queue[ClosableStreamRepr] = queue.Queue()
        q.put(ClosableStreamRepr(value="", encoding="ascii", close=True))
        forwarder = StdinForwarder(tc, "op-1", q)
        forwarder.run()
        assert forwarder.error is None
        assert len(tc.raw_requests) == 1


# --- Stdin passthrough ---


class TestStdinPassthrough:
    @staticmethod
    def _piped_stdin(data: bytes):
        read_fd, write_fd = os.pipe()
        if data:
            os.write(write_fd, data)
        os.close(write_fd)
        return os.fdopen(read_fd, "rb", buffering=0)

    def test_stdin_piped(self, contree_client, session_store):
        """Piped stdin is included in payload as base64 StreamRepr."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        stdin_content = b"print('hello')\n"
        _run_cmd(
            contree_client,
            args,
            mocks,
            store=session_store,
            stdin_mock=self._piped_stdin(stdin_content),
        )
        body = spawn_payload(contree_client)
        assert "stdin" in body
        # Printable payloads travel verbatim; the encoding rule lives
        # in StreamRepr.from_bytes.
        assert body["stdin"]["encoding"] == "ascii"
        assert StreamRepr.from_dict(body["stdin"]).as_bytes() == stdin_content

    def test_stdin_tty_not_included(self, contree_client, session_store):
        """When stdin is a TTY, no stdin key in payload."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert "stdin" not in body

    def test_stdin_empty_not_included(self, contree_client, session_store):
        """Piped but empty stdin does not add stdin key."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(
            contree_client,
            args,
            mocks,
            store=session_store,
            stdin_mock=self._piped_stdin(b""),
        )
        body = spawn_payload(contree_client)
        assert "stdin" not in body


# ── Interpreter mode (-I) ─────────────────────────────────────────────────


class TestInterpreterMode:
    def test_script_sent_as_stdin(self, contree_client, session_store, tmp_path):
        """With -I, script file is read, shebang stripped, body sent as stdin."""
        script = tmp_path / "script.sh"
        script.write_text("#!/usr/bin/env -S contree run -I\necho hello\n")
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(
            command_args=[str(script)],
            interpreter=True,
        )
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        rc = _run_cmd(contree_client, args, mocks, store=session_store)
        assert rc == 0
        body = spawn_payload(contree_client)
        assert body["command"] == "/bin/sh"
        assert body["shell"] is True
        assert body["args"] == ["-s"]
        assert StreamRepr.from_dict(body["stdin"]).as_bytes() == b"echo hello\n"

    def test_extra_args(self, contree_client, session_store, tmp_path):
        """Extra args after script path are passed as -s -- args."""
        script = tmp_path / "script.sh"
        script.write_text("#!/usr/bin/env -S contree run -I\nset -e\n")
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(
            command_args=[str(script), "arg1", "arg2"],
            interpreter=True,
        )
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["command"] == "/bin/sh"
        assert body["args"] == ["-s", "--", "arg1", "arg2"]

    def test_without_flag_no_magic(self, contree_client, session_store, tmp_path):
        """Without -I, script file is treated as a regular command."""
        script = tmp_path / "script.sh"
        script.write_text("#!/usr/bin/env -S contree run -I\necho hello\n")
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(command_args=[str(script)])
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        body = spawn_payload(contree_client)
        assert body["command"] == str(script)
        assert "stdin" not in body

    def test_skips_piped_stdin(self, contree_client, session_store, tmp_path):
        """When -I sets stdin from file, piped stdin is ignored."""
        script = tmp_path / "script.sh"
        script.write_text("#!/usr/bin/env -S contree run -I\necho from script\n")
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(
            command_args=[str(script)],
            interpreter=True,
        )
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        piped = MagicMock()
        piped.isatty.return_value = False
        piped.buffer = io.BytesIO(b"piped data")
        _run_cmd(
            contree_client,
            args,
            mocks,
            store=session_store,
            stdin_mock=piped,
        )
        body = spawn_payload(contree_client)
        stdin = StreamRepr.from_dict(body["stdin"])
        assert stdin.as_bytes() == b"echo from script\n"


# ── Escape sequence sanitization ─────────────────────────────────────


class TestEscapeSanitization:
    """DefaultFormatter strips breaking escape sequences from output."""

    def test_colors_preserved(self, contree_client, session_store, capsys):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        colored = "\033[1;32mgreen\033[0m normal"
        mocks = [
            _spawn_response(),
            _op_response(
                status="SUCCESS",
                exit_code=0,
                stdout={"value": colored, "encoding": "ascii"},
            ),
        ]
        _run_cmd(
            contree_client,
            args,
            mocks,
            store=session_store,
            formatter=DefaultFormatter(),
        )
        out = capsys.readouterr().out
        assert "\033[1;32mgreen\033[0m normal" in out

    def test_cursor_movement_stripped(self, contree_client, session_store, capsys):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        raw = "\033[2;5Htext\033[Aup\033[Bdown\033[10Gcol"
        mocks = [
            _spawn_response(),
            _op_response(
                status="SUCCESS",
                exit_code=0,
                stdout={"value": raw, "encoding": "ascii"},
            ),
        ]
        _run_cmd(
            contree_client,
            args,
            mocks,
            store=session_store,
            formatter=DefaultFormatter(),
        )
        out = capsys.readouterr().out
        assert "text" in out
        assert "\033[2;5H" not in out
        assert "\033[A" not in out

    def test_alternate_screen_stripped(self, contree_client, session_store, capsys):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        raw = "\033[?1049hhtop output\033[?1049l"
        mocks = [
            _spawn_response(),
            _op_response(
                status="SUCCESS",
                exit_code=0,
                stdout={"value": raw, "encoding": "ascii"},
            ),
        ]
        _run_cmd(
            contree_client,
            args,
            mocks,
            store=session_store,
            formatter=DefaultFormatter(),
        )
        out = capsys.readouterr().out
        assert "htop output" in out
        assert "\033[?1049h" not in out
        assert "\033[?1049l" not in out

    def test_clear_screen_stripped(self, contree_client, session_store, capsys):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        raw = "\033[2Jcontent\033[K"
        mocks = [
            _spawn_response(),
            _op_response(
                status="SUCCESS",
                exit_code=0,
                stdout={"value": raw, "encoding": "ascii"},
            ),
        ]
        _run_cmd(
            contree_client,
            args,
            mocks,
            store=session_store,
            formatter=DefaultFormatter(),
        )
        out = capsys.readouterr().out
        assert "content" in out
        assert "\033[2J" not in out
        assert "\033[K" not in out

    def test_stderr_also_sanitized(self, contree_client, session_store, capsys):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        raw = "\033[?25lerror msg\033[?25h"
        mocks = [
            _spawn_response(),
            _op_response(
                status="SUCCESS",
                exit_code=0,
                stderr={"value": raw, "encoding": "ascii"},
            ),
        ]
        _run_cmd(
            contree_client,
            args,
            mocks,
            store=session_store,
            formatter=DefaultFormatter(),
        )
        err = capsys.readouterr().err
        assert "error msg" in err
        assert "\033[?25l" not in err

    def test_json_formatter_not_sanitized(self, contree_client, session_store, capsys):
        """Non-default formatters get raw output (for structured data)."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        raw = "\033[2Jcontent"
        mocks = [
            _spawn_response(),
            _op_response(
                status="SUCCESS",
                exit_code=0,
                stdout={"value": raw, "encoding": "ascii"},
            ),
        ]
        _run_cmd(
            contree_client,
            args,
            mocks,
            store=session_store,
            formatter=JSONFormatter(),
        )
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "\033[2J" in parsed["stdout"]


# ── _is_excluded ─────────────────────────────────────────────────────────


class TestIsExcluded:
    def test_matches_full_path_pattern(self):
        assert _is_excluded("test.pyc", ("*.pyc",)) is True

    def test_matches_part_pattern(self):
        assert _is_excluded("src/__pycache__/x.py", ("__pycache__",)) is True

    def test_no_match(self):
        assert _is_excluded("src/main.py", ("*.pyc", "__pycache__")) is False

    def test_hidden_file(self):
        assert _is_excluded(".git", (".*",)) is True

    def test_nested_hidden(self):
        assert _is_excluded("src/.env", (".*",)) is True


# ── _expand_mapped_files extended ────────────────────────────────────────


class TestExpandMappedFilesExtended:
    def test_nonexistent_path_raises(self, tmp_path):
        # Construct MappedFile directly to bypass parse()'s os.stat call
        mf = MappedFile(
            host_path=str(tmp_path / "nope"),
            instance_path="/app",
            uid=0,
            gid=0,
            mode=0o644,
        )
        with pytest.raises(ValueError, match="neither file nor directory"):
            _expand_mapped_files([mf], [])

    def test_file_passthrough(self, tmp_path):
        f = tmp_path / "single.txt"
        f.write_text("content")
        mf = MappedFile.parse(f"{f}:/app/single.txt")
        result = _expand_mapped_files([mf], [])
        assert len(result) == 1
        assert result[0].host_path == str(f)

    def test_skips_non_files_in_dir(self, tmp_path):
        """Symlinks or special files are skipped."""
        root = tmp_path / "d"
        root.mkdir()
        (root / "real.txt").write_text("ok")
        mf = MappedFile.parse(f"{root}:/app")
        result = _expand_mapped_files([mf], [])
        paths = {m.instance_path for m in result}
        assert "/app/real.txt" in paths


# ── RunArgs.from_args ────────────────────────────────────────────────────


class TestRunArgsFromArgs:
    def test_file_excludes_flattening(self):
        import argparse

        ns = argparse.Namespace(
            command_args=["echo", "hi"],
            timeout=30,
            env=[],
            hostname="linuxkit",
            disposable=False,
            interpreter=False,
            shell=False,
            file=[],
            file_excludes=[["*.log", "*.tmp"], ["*.bak"]],
            truncate=65536,
            detach=False,
            preserve_env=False,
            cwd="",
            use="",
            stdin_open=False,
        )
        args = RunArgs.from_args(ns)
        assert args.file_excludes == ["*.log", "*.tmp", "*.bak"]

    def test_strips_leading_double_dash(self):
        import argparse

        ns = argparse.Namespace(
            command_args=["--", "echo", "hi"],
            timeout=30,
            env=[],
            hostname="linuxkit",
            disposable=False,
            interpreter=False,
            shell=False,
            file=[],
            file_excludes=[],
            truncate=65536,
            detach=False,
            preserve_env=False,
            cwd="",
            use="",
            stdin_open=False,
        )
        args = RunArgs.from_args(ns)
        assert args.command_args == ["echo", "hi"]


# ── Detach pending ops cache ─────────────────────────────────────────────


class TestDetachPendingOps:
    def test_detach_creates_pending_ops_cache(self, contree_client, session_store):
        """Detach mode adds op to pending cache."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(detach=True)
        _run_cmd(contree_client, args, [_spawn_response("op-det")], store=session_store)
        pending_key = ("", f"ops:{session_store.session_key}")
        cached = session_store.cache.get(pending_key)
        assert isinstance(cached, list)
        assert len(cached) == 1
        assert cached[0]["op"] == "op-det"
        assert cached[0]["disposable"] is False

    def test_detach_disposable_cache(self, contree_client, session_store):
        """Detach + disposable creates disposable branch and cache entry."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(detach=True, disposable=True)
        _run_cmd(
            contree_client, args, [_spawn_response("op-ddisp")], store=session_store
        )
        pending_key = ("", f"ops:{session_store.session_key}")
        cached = session_store.cache.get(pending_key)
        assert isinstance(cached, list)
        assert cached[0]["disposable"] is True
        branches = dict(session_store.list_branches())
        assert "disposable-op-ddisp" in branches

    def test_detach_appends_to_existing_cache(self, contree_client, session_store):
        """Multiple detach runs append to cache."""
        session_store.set_image(IMG_UUID, kind="test")
        args1 = _default_args(detach=True)
        # Queue both spawn outcomes up front (FIFO with sticky tail)
        _run_cmd(
            contree_client,
            args1,
            [_spawn_response("op-1"), _spawn_response("op-2")],
            store=session_store,
        )
        args2 = _default_args(detach=True)
        _run_cmd(contree_client, args2, [], store=session_store)
        pending_key = ("", f"ops:{session_store.session_key}")
        cached = session_store.cache.get(pending_key)
        assert isinstance(cached, list)
        assert len(cached) == 2
        ops = {c["op"] for c in cached}
        assert ops == {"op-1", "op-2"}

    def test_detach_creates_detached_branch(self, contree_client, session_store):
        """Non-disposable detach creates detached branch."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(detach=True, disposable=False)
        _run_cmd(
            contree_client, args, [_spawn_response("op-detbr")], store=session_store
        )
        branches = dict(session_store.list_branches())
        assert "detached-op-detbr" in branches


# ── --use flag ──────────────────────────────────────────────────────────


class TestUseFlag:
    def test_use_resolves_tag_and_runs(self, contree_client, session_store):
        """--use resolves a tag, sets session image, then runs."""
        args = _default_args(use="tag:ubuntu:latest")
        mocks = [
            tag_lookup(IMG_UUID),
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        rc = _run_cmd(contree_client, args, mocks, store=session_store)
        assert rc == 0
        assert session_store.current_image == IMG_NEW

    def test_use_with_uuid(self, contree_client, session_store):
        """--use with a UUID skips tag resolution."""
        args = _default_args(use=IMG_SOME)
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        rc = _run_cmd(contree_client, args, mocks, store=session_store)
        assert rc == 0
        assert contree_client.calls_for("inspect_find_image_by_tag") == []
        body = spawn_payload(contree_client)
        assert body["image"] == IMG_SOME

    def test_use_works_without_existing_session(self, contree_client, session_store):
        """--use creates a session even when none exists."""
        assert session_store.session is None
        args = _default_args(use=IMG_UUID)
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        rc = _run_cmd(contree_client, args, mocks, store=session_store)
        assert rc == 0
        assert session_store.session is not None
        assert session_store.current_image == IMG_NEW

    def test_use_disposable(self, contree_client, session_store):
        """--use + --disposable sets session to use-image, run doesn't advance."""
        args = _default_args(use=IMG_UUID, disposable=True)
        mocks = [
            _spawn_response("op-use-disp"),
            _op_response(
                "op-use-disp",
                status="SUCCESS",
                exit_code=0,
                image=IMG_NEW,
            ),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        assert session_store.current_image == IMG_UUID

    def test_use_creates_history_entry(self, contree_client, session_store):
        """--use creates a 'use' kind history entry before the run."""
        args = _default_args(use="tag:myimage")
        mocks = [
            tag_lookup(IMG_UUID),
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        s = session_store.session
        assert s is not None
        assert s.last_kind == "run"

    def test_use_without_command_still_sets_image(
        self,
        contree_client,
        session_store,
    ):
        """--use with no command args still switches session image."""
        args = RunArgs(use=IMG_UUID)
        mocks = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, mocks, store=session_store)
        assert session_store.session is not None


# ---------------------------------------------------------------------------
# SSE streaming (`stream_events_until_close` and `build_op_from_summary`)
# ---------------------------------------------------------------------------


EVENT_TS = "2026-01-01T00:00:00.000000+00:00"


def full_resources() -> dict:
    """The complete per-process resource usage block every real `exit`
    event carries (all counters are required by the API schema)."""
    counters = (
        "user_time_us",
        "sys_time_us",
        "max_rss_kb",
        "shared_memory",
        "unshared_memory",
        "swaps",
        "minor_faults",
        "major_faults",
        "voluntary_ctx_switches",
        "involuntary_ctx_switches",
        "block_input_ops",
        "block_output_ops",
        "ipc_msgs_sent",
        "ipc_msgs_received",
        "signals_received",
    )
    return dict.fromkeys(counters, 0)


def exit_data(code: int = 0, *, timed_out: bool = False, signal: int = -1) -> dict:
    """Full `exit` event payload as the API emits it."""
    return {
        "pid": 4242,
        "code": code,
        "signal": signal,
        "timed_out": timed_out,
        "duration_ms": 1500,
        "resources": full_resources(),
    }


def completion_data(status: str = "SUCCESS", **overrides) -> dict:
    """Full `completion` event payload as the API emits it."""
    data: dict = {
        "status": status,
        "duration_ms": 1500,
        "result_image_uuid": IMG_NEW,
        "error": None,
        "image_size_bytes": 4096,
    }
    data.update(overrides)
    return data


def make_event(
    event_type: str,
    data: dict,
    *,
    event_id: int = 1,
    spid: int | None = None,
) -> OperationEvent:
    """Build a typed OperationEvent the way the SSE decoder does:
    payloads matching the schema become models, sparse payloads stay
    raw dicts."""
    payload: dict = {"id": event_id, "ts": EVENT_TS, "type": event_type, "data": data}
    if spid is not None:
        payload["spid"] = spid
    return OperationEvent.from_dict(payload)


def stream_event(
    kind: str,
    value: str,
    *,
    event_id: int = 1,
    encoding: str = "ascii",
) -> OperationEvent:
    """A stdout/stderr chunk event for the main process (spid=1)."""
    return make_event(
        kind,
        {"value": value, "encoding": encoding},
        event_id=event_id,
        spid=1,
    )


def completion_event(
    status: str = "SUCCESS", *, event_id: int = 2, **overrides
) -> OperationEvent:
    return make_event(
        "completion", completion_data(status, **overrides), event_id=event_id
    )


def executing_response(uuid: str = "op-1") -> OperationResponse:
    """A non-terminal GET /v1/operations/{uuid} snapshot."""
    return OperationResponse.from_dict({"uuid": uuid, "status": "EXECUTING"})


class TestStreamEventsUntilClose:
    def test_stream_opened_with_follow(self):
        """The streamer subscribes with follow=True so the server keeps
        the stream open for a running operation."""
        tc = ContreeTestClient()
        tc.mock("iter_operation_events", [completion_event()])
        stream_events_until_close(tc, "op-1", DefaultFormatter())
        call = tc.calls_for("iter_operation_events")[0]
        assert call.args == ("op-1",)
        assert call.kwargs["follow"] is True

    def test_first_call_has_no_last_event_id(self):
        """The first subscribe passes last_event_id=None (nothing to
        resume from)."""
        tc = ContreeTestClient()
        tc.mock("iter_operation_events", [completion_event()])
        stream_events_until_close(tc, "op-x", DefaultFormatter())
        call = tc.calls_for("iter_operation_events")[0]
        assert call.kwargs["last_event_id"] is None

    def test_stdout_streamed_live_for_default_formatter(self, capsys):
        tc = ContreeTestClient()
        tc.mock(
            "iter_operation_events",
            [stream_event("stdout", "hello", event_id=1), completion_event()],
        )
        summary = stream_events_until_close(tc, "op-1", DefaultFormatter())
        out = capsys.readouterr()
        assert out.out == "hello"
        assert bytes(summary.stdout) == b"hello"

    def test_stderr_streamed_live_for_default_formatter(self, capsys):
        tc = ContreeTestClient()
        tc.mock(
            "iter_operation_events",
            [
                stream_event("stderr", "oops\n", event_id=1),
                completion_event("FAILED", error="boom", result_image_uuid=None),
            ],
        )
        summary = stream_events_until_close(tc, "op-1", DefaultFormatter())
        out = capsys.readouterr()
        assert out.err == "oops\n"
        assert bytes(summary.stderr) == b"oops\n"

    def test_json_formatter_accumulates_without_printing(self, capsys):
        tc = ContreeTestClient()
        tc.mock(
            "iter_operation_events",
            [stream_event("stdout", "hi", event_id=1), completion_event()],
        )
        summary = stream_events_until_close(tc, "op-1", JSONFormatter())
        out = capsys.readouterr()
        assert out.out == ""
        assert bytes(summary.stdout) == b"hi"

    def test_base64_chunk_decoded(self, capsys):
        payload = base64.b64encode(b"\x00\xff bin").decode("ascii")
        tc = ContreeTestClient()
        tc.mock(
            "iter_operation_events",
            [
                stream_event("stdout", payload, event_id=1, encoding="base64"),
                completion_event(),
            ],
        )
        summary = stream_events_until_close(tc, "op-1", JSONFormatter())
        assert bytes(summary.stdout) == b"\x00\xff bin"

    def test_exit_event_for_spid_1_stored(self):
        tc = ContreeTestClient()
        tc.mock(
            "iter_operation_events",
            [
                make_event("exit", exit_data(0), event_id=1, spid=1),
                completion_event(),
            ],
        )
        summary = stream_events_until_close(tc, "op-1", DefaultFormatter())
        assert summary.exit_event is not None
        assert summary.exit_event.data.code == 0

    def test_exit_event_for_non_main_spid_ignored(self):
        """Only spid=1 drives CLI exit code; child spid exits stay unrecorded."""
        tc = ContreeTestClient()
        tc.mock(
            "iter_operation_events",
            [
                make_event("exit", exit_data(3), event_id=1, spid=2),
                completion_event(),
            ],
        )
        summary = stream_events_until_close(tc, "op-1", DefaultFormatter())
        assert summary.exit_event is None

    def test_completion_event_breaks_loop(self):
        tc = ContreeTestClient()
        tc.mock("iter_operation_events", [completion_event(event_id=1)])
        summary = stream_events_until_close(tc, "op-1", DefaultFormatter())
        assert summary.completion is not None
        assert summary.completion.data.status == "SUCCESS"
        assert len(tc.calls_for("iter_operation_events")) == 1

    def test_stream_ends_without_completion_falls_back_to_terminal_get(self):
        """SSE closes cleanly without a `completion` event: the
        between-attempt GET check detects the op is terminal and parks
        it on `summary.fallback_op` so the caller doesn't need to GET
        again."""
        tc = ContreeTestClient()
        tc.mock("iter_operation_events", [])
        op = OperationResponse.from_dict(
            {"uuid": "op-1", "status": "SUCCESS", "result": {"image": None}}
        )
        tc.mock("get_operation_status", op)
        summary = stream_events_until_close(tc, "op-1", DefaultFormatter())
        assert summary.completion is None
        assert summary.fallback_op == op.to_dict()
        assert bytes(summary.stdout) == b""

    def test_sse_connect_errors_then_terminal_via_get(self):
        """SSE keeps failing to connect while the op runs; once the
        between-attempt GET reports terminal status the streamer
        stops retrying and returns without ever seeing a completion
        event."""
        tc = ContreeTestClient()
        tc.mock("iter_operation_events", error=ContreeAPIError(502, "boom"))
        tc.mock("get_operation_status", executing_response())
        tc.mock("get_operation_status", executing_response())
        tc.mock(
            "get_operation_status",
            OperationResponse.from_dict({"uuid": "op-1", "status": "SUCCESS"}),
        )
        # Reconnect sleeps happen inside the library's
        # follow_operation_events loop.
        with patch("contree_client.base.time.sleep"):
            summary = stream_events_until_close(tc, "op-1", DefaultFormatter())
        assert summary.completion is None
        assert summary.fallback_op == {"uuid": "op-1", "status": "SUCCESS"}
        assert len(tc.calls_for("iter_operation_events")) == 3
        # Three terminal probes inside the library loop plus one final
        # fetch of the terminal payload for fallback_op.
        assert len(tc.calls_for("get_operation_status")) == 4

    def test_sse_error_triggers_reconnect_with_last_event_id(self):
        """A mid-stream server error (SSEStreamError after some events)
        makes the streamer reconnect, resuming from the last received
        event id."""
        tc = ContreeTestClient()
        tc.mock(
            "iter_operation_events",
            [stream_event("stdout", "a", event_id=1)],
            error=SSEStreamError("boom", last_event_id=1),
        )
        tc.mock("iter_operation_events", [completion_event(event_id=2)])
        # The op is still running when the terminal check fires between
        # the two attempts.
        tc.mock("get_operation_status", executing_response())
        summary = stream_events_until_close(tc, "op-1", DefaultFormatter())
        assert summary.completion is not None
        calls = tc.calls_for("iter_operation_events")
        assert len(calls) == 2
        assert calls[0].kwargs["last_event_id"] is None
        assert calls[1].kwargs["last_event_id"] == 1

    def test_retry_after_honored_on_api_error(self):
        """A 425/410-style ContreeAPIError with retry_after sleeps for
        exactly that delay before reconnecting."""
        tc = ContreeTestClient()
        tc.mock(
            "iter_operation_events",
            error=ContreeAPIError(425, "too early", retry_after=7),
        )
        tc.mock("iter_operation_events", [completion_event()])
        tc.mock("get_operation_status", executing_response())
        with patch("contree_cli.cli.run.time.sleep") as sleep_mock:
            summary = stream_events_until_close(tc, "op-1", DefaultFormatter())
        assert summary.completion is not None
        assert sleep_mock.call_args_list[0].args == (7,)

    def test_broken_pipe_from_stdout_propagates(self, monkeypatch):
        """`BrokenPipeError` from local stdio write must propagate
        unchanged: retrying can't fix a closed local pipe and it
        would be misinterpreted as a remote network error otherwise."""
        tc = ContreeTestClient()
        tc.mock(
            "iter_operation_events",
            [stream_event("stdout", "hi", event_id=1)],
        )

        def raise_broken_pipe(*args: object, **kw: object) -> int:
            raise BrokenPipeError

        monkeypatch.setattr("sys.stdout.buffer.write", raise_broken_pipe)
        with pytest.raises(BrokenPipeError):
            stream_events_until_close(tc, "op-1", DefaultFormatter())
        # Only the initial SSE attempt is made; no retry, no terminal-check
        # GET (BrokenPipeError bypasses the retry path entirely).
        assert len(tc.calls_for("iter_operation_events")) == 1
        assert tc.calls_for("get_operation_status") == []


class TestBuildOpFromSummary:
    def _completion(self, **overrides) -> OperationEvent:
        return make_event("completion", completion_data(**overrides))

    def test_shape_carries_status_and_uuid(self):
        summary = TerminalSummary(completion=self._completion())
        op = build_op_from_summary("op-1", summary)
        assert op["uuid"] == "op-1"
        assert op["kind"] == "instance"
        assert op["status"] == "SUCCESS"

    def test_duration_ms_converted_to_seconds(self):
        summary = TerminalSummary(completion=self._completion(duration_ms=2500))
        op = build_op_from_summary("op-1", summary)
        assert op["duration"] == 2.5

    def test_duration_missing_falls_back_to_zero(self):
        """A completion payload that doesn't match the schema is kept
        as a raw dict by the decoder; missing duration_ms then falls
        back to zero."""
        summary = TerminalSummary(
            completion=make_event("completion", {"status": "SUCCESS"})
        )
        op = build_op_from_summary("op-1", summary)
        assert op["duration"] == 0.0

    def test_result_image_uuid_propagates(self):
        summary = TerminalSummary(completion=self._completion())
        op = build_op_from_summary("op-1", summary)
        assert op["result_image_uuid"] == IMG_NEW
        assert op["result"] == {"image": IMG_NEW, "tag": None}

    def test_exit_event_drives_state(self):
        summary = TerminalSummary(
            completion=self._completion(),
            exit_event=make_event(
                "exit",
                exit_data(42, timed_out=True),
                event_id=2,
                spid=1,
            ),
        )
        op = build_op_from_summary("op-1", summary)
        state = op["metadata"]["result"]["state"]
        assert state == {"exit_code": 42, "timed_out": True}

    def test_state_is_none_without_exit_event(self):
        summary = TerminalSummary(completion=self._completion())
        op = build_op_from_summary("op-1", summary)
        assert op["metadata"]["result"]["state"] is None

    def test_stdout_stderr_reassembled_from_bytearrays(self):
        summary = TerminalSummary(
            completion=self._completion(),
            stdout=bytearray(b"hello\n"),
            stderr=bytearray(b"warn\n"),
        )
        op = build_op_from_summary("op-1", summary)
        result = op["metadata"]["result"]
        assert result["stdout"]["value"] == "hello\n"
        assert result["stderr"]["value"] == "warn\n"
        assert result["stdout"]["truncated"] is False

    def test_error_field_passthrough(self):
        summary = TerminalSummary(
            completion=self._completion(
                status="FAILED", error="boom", result_image_uuid=None
            )
        )
        op = build_op_from_summary("op-1", summary)
        assert op["status"] == "FAILED"
        assert op["error"] == "boom"
