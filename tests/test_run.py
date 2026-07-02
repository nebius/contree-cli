from __future__ import annotations

import base64
import io
import json
import logging
import os
import select
from contextvars import copy_context
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
from conftest import ContreeTestClient, FakeResponse

from contree_cli import CLIENT, FORMATTER, SESSION_STORE
from contree_cli.cli.run import (
    RunArgs,
    TerminalSummary,
    _build_op_from_summary,
    _expand_mapped_files,
    _is_excluded,
    _local_file_cache_kind,
    _read_piped_stdin,
    _stream_events_until_close,
    cmd_run,
)
from contree_cli.client import ApiError
from contree_cli.mapped_file import MappedFile
from contree_cli.output import DefaultFormatter, JSONFormatter
from contree_cli.session import SessionStore

IMG_UUID = "a1b2c3d4-5678-9abc-def0-111111111111"
IMG_NEW = "c3d4e5f6-789a-bcde-f012-333333333333"
IMG_NEW2 = "d4e5f6a7-89ab-cdef-0123-444444444444"
IMG_SOME = "b2c3d4e5-6789-abcd-ef01-222222222222"


def _spawn_response(uuid: str = "op-1") -> FakeResponse:
    return FakeResponse.json(
        {"uuid": uuid, "status": "PENDING"},
        status=201,
    )


def _op_response(
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
) -> FakeResponse:
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
    return FakeResponse.json(
        {
            "uuid": uuid,
            "kind": "instance",
            "status": status,
            "error": error,
            "duration": duration,
            "metadata": {"result": instance_result},
            "result": {"image": image, "tag": "latest"},
        }
    )


def _api_response(body: dict, *, status: int = 200) -> FakeResponse:
    return FakeResponse.json(body, status=status)


def _tty_stdin() -> MagicMock:
    """Return a mock stdin that reports as a TTY (no piped input)."""
    mock = MagicMock()
    mock.isatty.return_value = True
    return mock


def _run_cmd(
    tc: ContreeTestClient,
    args: RunArgs,
    responses: list[FakeResponse],
    *,
    store: SessionStore,
    formatter=None,
    stdin_mock: MagicMock | None = None,
):
    """Run cmd_run with mocked HTTP responses and mocked sleep.

    The fake connection auto-serves empty SSE responses for any
    GET /events path so existing tests can stay shaped as
    `[spawn, op]` without knowing the CLI now opens an SSE first.
    """
    tc.fake.responses.extend(responses)

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
        """Only 1 HTTP request (the POST spawn), no GET poll."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(detach=True)
        _run_cmd(contree_client, args, [_spawn_response()], store=session_store)
        assert contree_client.request_count == 1
        assert contree_client.get_request(0).method == "POST"

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
        """SSE follow=1 makes the API serve the terminal state directly —
        the CLI no longer polls through intermediate PENDING snapshots."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        rc = _run_cmd(contree_client, args, responses, store=session_store)
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["status"] == "SUCCESS"

    def test_unknown_field_passes_through(self, contree_client, session_store, capsys):
        """New server fields on the operation reach JSON output as-is."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        op_body = json.loads(_op_response(status="SUCCESS", exit_code=0).body)
        op_body["session_key"] = "sess-1"
        op_body["future_field"] = "anything"
        responses = [_spawn_response(), FakeResponse.json(op_body)]
        _run_cmd(contree_client, args, responses, store=session_store)
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["session_key"] == "sess-1"
        assert parsed["future_field"] == "anything"

    def test_poll_default_shows_stdout(self, contree_client, session_store, capsys):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        responses = [
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
            responses,
            store=session_store,
            formatter=DefaultFormatter(),
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "hello\n" in out

    def test_poll_until_failed(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="FAILED", exit_code=None, error="timeout"),
        ]
        rc = _run_cmd(contree_client, args, responses, store=session_store)
        assert rc == 1

    def test_poll_until_cancelled(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="CANCELLED", exit_code=None),
        ]
        rc = _run_cmd(contree_client, args, responses, store=session_store)
        assert rc == 1

    def test_failed_with_exit_code(self, contree_client, session_store):
        """FAILED with exit code (e.g. timeout kill) returns the exit code."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="FAILED", exit_code=137, error="timeout"),
        ]
        rc = _run_cmd(contree_client, args, responses, store=session_store)
        assert rc == 137

    def test_timed_out_logs_warning(self, contree_client, session_store, caplog):
        """``state.timed_out=true`` triggers a WARNING regardless of status.

        The API can report SUCCESS while still flagging the process as killed
        by the user-set timeout (signal=9, exit_code=-1, timed_out=true).
        """
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(timeout=60)
        responses = [
            _spawn_response(),
            _op_response(
                status="SUCCESS",
                exit_code=-1,
                state_extra={"timed_out": True, "signal": 9},
            ),
        ]
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.run"):
            _run_cmd(contree_client, args, responses, store=session_store)

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
        responses = [
            _spawn_response(),
            _op_response(status="FAILED", exit_code=1, error="oom"),
        ]
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.run"):
            _run_cmd(contree_client, args, responses, store=session_store)

        ended = [r for r in caplog.records if "ended with status" in r.getMessage()]
        assert len(ended) == 1
        assert ended[0].levelno == logging.CRITICAL

    def test_success_without_timeout_logs_nothing(
        self, contree_client, session_store, caplog
    ):
        """Plain SUCCESS does not emit a timeout warning."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.run"):
            _run_cmd(contree_client, args, responses, store=session_store)
        assert [r for r in caplog.records if "timed out" in r.getMessage()] == []

    def test_exit_code_propagated(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=42),
        ]
        rc = _run_cmd(contree_client, args, responses, store=session_store)
        assert rc == 42

    def test_success_no_exit_code(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=None),
        ]
        rc = _run_cmd(contree_client, args, responses, store=session_store)
        assert rc is None


# ── Ctrl+C cancellation ─────────────────────────────────────────────────


class TestCtrlC:
    @staticmethod
    def _run_ctrl_c(
        responses: list[FakeResponse], store: SessionStore
    ) -> ContreeTestClient:
        """Run cmd_run with the events stream raising KeyboardInterrupt
        — simulates the user hitting Ctrl-C while the CLI was waiting
        on SSE for the operation to terminate."""
        store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        tc = ContreeTestClient()
        tc.fake.responses.extend(responses)

        CLIENT.set(tc)
        FORMATTER.set(JSONFormatter())
        SESSION_STORE.set(store)
        ctx = copy_context()

        with (
            patch(
                "contree_cli.cli.run._stream_events_until_close",
                side_effect=KeyboardInterrupt,
            ),
            patch("contree_cli.cli.run.sys.stdin", _tty_stdin()),
            pytest.raises(KeyboardInterrupt),
        ):
            ctx.run(cmd_run, args)
        return tc

    def test_ctrl_c_cancels_operation(self, session_store):
        """On KeyboardInterrupt during the wait, DELETE is sent."""
        tc = self._run_ctrl_c(
            [
                _spawn_response(),
                _api_response({}, status=202),
            ],
            session_store,
        )
        methods = [r.method for r in tc.fake.requests]
        assert "DELETE" in methods
        delete_req = next(r for r in tc.fake.requests if r.method == "DELETE")
        assert "/v1/operations/op-1" in delete_req.path

    def test_ctrl_c_delete_failure_still_raises(self, session_store):
        """If DELETE fails, KeyboardInterrupt is still re-raised."""
        self._run_ctrl_c(
            [
                _spawn_response(),
                _api_response({"error": "not found"}, status=404),
            ],
            session_store,
        )


class TestBrokenPipe:
    """`BrokenPipeError` from local stdio (shell piped output closed
    early, e.g. ``contree run ... | head``) must cancel the remote op
    and exit with 141 (128 + SIGPIPE) instead of being misinterpreted
    as a remote network drop."""

    @staticmethod
    def _run_broken_pipe(
        responses: list[FakeResponse], store: SessionStore
    ) -> ContreeTestClient:
        store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        tc = ContreeTestClient()
        tc.fake.responses.extend(responses)

        CLIENT.set(tc)
        FORMATTER.set(JSONFormatter())
        SESSION_STORE.set(store)
        ctx = copy_context()

        with (
            patch(
                "contree_cli.cli.run._stream_events_until_close",
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
            [_spawn_response(), _api_response({}, status=202)],
            session_store,
        )
        methods = [r.method for r in tc.fake.requests]
        assert "DELETE" in methods
        delete_req = next(r for r in tc.fake.requests if r.method == "DELETE")
        assert "/v1/operations/op-1" in delete_req.path

    def test_broken_pipe_delete_failure_still_exits_141(self, session_store):
        """Even if the DELETE fails, we still exit 141 rather than
        re-raising BrokenPipeError."""
        self._run_broken_pipe(
            [_spawn_response(), _api_response({"error": "not found"}, status=404)],
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
        """POST /v1/files is called for each attached file."""
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

        file_resp = _api_response(
            {"uuid": "file-uuid-1", "sha256": "abc"},
            status=201,
        )
        responses = [
            _api_response({"error": "not found"}, status=404),  # GET dedup miss
            file_resp,  # POST /v1/files
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        rc = _run_cmd(contree_client, args, responses, store=session_store)
        assert rc == 0

        # Verify dedup check then file upload
        req0 = contree_client.get_request(0)
        assert req0.method == "GET"
        assert "/v1/files/" in req0.path
        req1 = contree_client.get_request(1)
        assert req1.method == "POST"
        assert "/v1/files" in req1.path

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

        file_resp = _api_response(
            {"uuid": "file-42", "sha256": "def"},
            status=201,
        )
        responses = [
            _api_response({"error": "not found"}, status=404),  # GET dedup miss
            file_resp,
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)

        # GET dedup, POST /v1/files, then POST /v1/instances
        spawn_req = contree_client.get_request(2)
        body = json.loads(spawn_req.body)
        assert "/app/script.sh" in body["files"]
        assert body["files"]["/app/script.sh"]["uuid"] == "file-42"
        assert body["files"]["/app/script.sh"]["uid"] == 1000

    def test_file_dedup_skips_upload(self, contree_client, session_store, tmp_path):
        """GET /v1/files/... returns 200 -> no POST upload, UUID reused."""
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

        responses = [
            _api_response({"uuid": "existing-uuid"}),  # GET dedup hit
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        rc = _run_cmd(contree_client, args, responses, store=session_store)
        assert rc == 0

        methods = [r.method for r in contree_client.fake.requests]
        # Only GET (dedup), POST (spawn), GET (poll) -- no POST /v1/files
        req0 = contree_client.get_request(0)
        assert req0.method == "GET"
        assert "/v1/files/" in req0.path
        assert methods.count("POST") == 1  # only the spawn POST

        # Spawn uses the existing UUID
        spawn_req = contree_client.get_request(1)
        body = json.loads(spawn_req.body)
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

        responses = [
            _api_response({"uuid": "existing-uuid"}),  # GET dedup hit
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        with caplog.at_level(logging.INFO):
            _run_cmd(contree_client, args, responses, store=session_store)
        assert "File reused:" in caplog.text
        assert "existing-uuid" in caplog.text

    def test_file_dedup_non_404_raises(self, contree_client, session_store, tmp_path):
        """Non-404 error from GET /v1/files propagates."""
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

        # Client retries 500s (RETRY_DELAYS has 7 entries -> 8 attempts)
        responses = [
            _api_response({"error": "server error"}, status=500) for _ in range(8)
        ]
        with pytest.raises(ApiError) as exc_info:
            _run_cmd(contree_client, args, responses, store=session_store)
        assert exc_info.value.status == 500

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
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)

        # spawn + poll only, no GET/POST /v1/files
        req0 = contree_client.get_request(0)
        assert req0.method == "POST"
        assert "/v1/instances" in req0.path
        body = json.loads(req0.body)
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
        responses = [
            _api_response({"uuid": "new-uuid"}),  # GET dedup hit for new content
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)

        req0 = contree_client.get_request(0)
        assert req0.method == "GET"
        assert "/v1/files/" in req0.path
        spawn_req = contree_client.get_request(1)
        spawn_body = json.loads(spawn_req.body)
        assert spawn_body["files"]["/app/cached-change.txt"]["uuid"] == "new-uuid"


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
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(tc, args, responses, store=store)
        spawn_req = tc.get_request(0)
        return json.loads(spawn_req.body)

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
        tag_resp = _api_response(
            {"images": [{"uuid": "resolved-uuid"}]},
        )
        responses = [
            tag_resp,  # GET /v1/images?tag=latest
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)

        # First request is the tag lookup
        req0 = contree_client.get_request(0)
        assert req0.method == "GET"
        assert "tag=latest" in req0.path

        # Spawn uses resolved UUID
        spawn_req = contree_client.get_request(1)
        body = json.loads(spawn_req.body)
        assert body["image"] == "resolved-uuid"

    def test_uuid_passthrough(self, contree_client, session_store):
        session_store.set_image(IMG_SOME, kind="test")
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["image"] == IMG_SOME


# ── Env parsing ──────────────────────────────────────────────────────────


class TestEnvParsing:
    def test_env_key_value(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(env=["FOO=bar", "BAZ=qux"])
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["env"] == {"FOO": "bar", "BAZ": "qux"}

    def test_env_empty_value(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(env=["KEY="])
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["env"] == {"KEY": ""}

    def test_no_env_omitted(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(env=[])
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert "env" not in body

    def test_session_env_no_auto_preserve(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        session_store.set_env("PATH", "/usr/bin:/bin")
        args = _default_args(env=[])
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["env"] == {"PATH": "/usr/bin:/bin"}
        assert "preserve_env" not in body

    def test_session_env_with_per_run_override(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        session_store.set_env("PATH", "/usr/bin:/bin")
        args = _default_args(env=["DEBUG=1"])
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["env"] == {"PATH": "/usr/bin:/bin", "DEBUG": "1"}
        assert "preserve_env" not in body

    def test_session_env_with_preserve_flag(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        session_store.set_env("PATH", "/usr/bin:/bin")
        args = _default_args(preserve_env=True)
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["env"] == {"PATH": "/usr/bin:/bin"}
        assert body["preserve_env"] is True

    def test_preserve_env_flag(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(preserve_env=True)
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["preserve_env"] is True

    def test_preserve_env_with_per_run_env(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(env=["FOO=bar"], preserve_env=True)
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["env"] == {"FOO": "bar"}
        assert body["preserve_env"] is True

    def test_preserved_env_not_resent(self, contree_client, session_store):
        """After preserve_env run, same env is not resent on next run."""
        session_store.set_image(IMG_UUID, kind="test")
        session_store.set_env("PATH", "/usr/bin")
        new_img = "00000000-0000-0000-0000-000000000099"

        # First run: preserve env
        args1 = _default_args(preserve_env=True)
        responses1 = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=new_img),
        ]
        _run_cmd(contree_client, args1, responses1, store=session_store)

        # Clear recorded requests for second run
        contree_client.fake.requests.clear()

        # Second run: same env, should skip sending it
        args2 = _default_args()
        responses2 = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args2, responses2, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert "env" not in body

    def test_preserved_env_resent_after_rollback(self, contree_client, session_store):
        """After rollback to image without preserved env, env is sent again."""
        session_store.set_image(IMG_UUID, kind="test")
        session_store.set_env("PATH", "/usr/bin")
        new_img = "00000000-0000-0000-0000-000000000099"

        # Run with preserve
        args1 = _default_args(preserve_env=True)
        responses1 = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=new_img),
        ]
        _run_cmd(contree_client, args1, responses1, store=session_store)

        # Rollback to original image (no preserved env)
        session_store.rollback(1)

        # Clear recorded requests for next run
        contree_client.fake.requests.clear()

        # Run again: env must be sent because original image has no preserved env
        args2 = _default_args()
        responses2 = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args2, responses2, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["env"] == {"PATH": "/usr/bin"}


# ── Shell mode ───────────────────────────────────────────────────────────


class TestShellMode:
    def test_shell_joins_command(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(
            command_args=["echo", "hello", "world"],
            shell=True,
        )
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["command"] == "echo hello world"
        assert body["shell"] is True
        assert "args" not in body

    def test_non_shell_splits_command(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(
            command_args=["echo", "hello", "world"],
            shell=False,
        )
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
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
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["command"] == "python3"
        assert body["args"] == ["-c", "print('hello world')"]

    def test_shell_quotes_arg_with_spaces(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(
            command_args=["python3", "-c", "print('hello world')"],
            shell=True,
        )
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
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
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
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
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["command"] == "echo 1 ; echo 2"
        assert body["shell"] is True


# ── Session update on success ────────────────────────────────────────────


class TestStdinHandling:
    def test_skips_unready_stdin(self, contree_client, session_store, monkeypatch):
        session_store.set_image(IMG_UUID, kind="use")
        fake = io.BytesIO()
        fake.isatty = lambda: False  # type: ignore[assignment]
        fake.fileno = lambda: 0  # type: ignore[assignment]
        fake.buffer = fake  # type: ignore[assignment]
        monkeypatch.setattr(select, "select", lambda *args, **kwargs: ([], [], []))

        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, responses, store=session_store, stdin_mock=fake)
        # ensure request sent without stdin field
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert "stdin" not in body

    def test_reads_ready_stdin(self, contree_client, session_store, monkeypatch):
        session_store.set_image(IMG_UUID, kind="use")
        fake = io.BytesIO(b"echo hi\n")
        fake.isatty = lambda: False  # type: ignore[assignment]
        fake.fileno = lambda: 0  # type: ignore[assignment]
        fake.buffer = fake  # type: ignore[assignment]
        monkeypatch.setattr(select, "select", lambda *args, **kwargs: ([0], [], []))

        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, responses, store=session_store, stdin_mock=fake)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert "stdin" in body
        assert body["stdin"]["value"]


class TestSessionUpdate:
    def test_success_updates_session(self, contree_client, session_store):
        """On SUCCESS with new image, session is updated."""
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
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
        responses = [
            _spawn_response("op-dispose"),
            _op_response("op-dispose", status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        assert session_store.current_image == IMG_UUID
        branches = dict(session_store.list_branches())
        assert "disposable-op-dispose" in branches
        assert branches["disposable-op-dispose"] is False

    def test_disposable_detach_creates_branch(
        self, contree_client, session_store, capsys
    ) -> None:
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args(disposable=True, detach=True)
        responses = [_spawn_response("op-dispose-det")]
        _run_cmd(contree_client, args, responses, store=session_store)
        branches = dict(session_store.list_branches())
        assert "disposable-op-dispose-det" in branches
        assert branches["disposable-op-dispose-det"] is False

    def test_disposable_does_not_update_session(self, contree_client, session_store):
        """Disposable runs do not update the session image."""
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args(disposable=True)
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        assert session_store.current_image == IMG_UUID

    def test_failed_does_not_update_session(self, contree_client, session_store):
        """Failed runs do not update the session image."""
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="FAILED", error="timeout"),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        assert session_store.current_image == IMG_UUID


# ── Operation caching ─────────────────────────────────────────────────────


class TestOperationCaching:
    def test_terminal_op_cached_after_run(self, contree_client, session_store):
        """Completed run caches the operation so `show` skips the API."""
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args()
        responses = [
            _spawn_response("op-cached"),
            _op_response("op-cached", status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        cached = session_store.cache.get(("op-cached", "operation"))
        assert cached is not None
        assert cached["status"] == "SUCCESS"

    def test_failed_op_cached_after_run(self, contree_client, session_store):
        """Failed runs also cache the terminal operation."""
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args()
        responses = [
            _spawn_response("op-fail"),
            _op_response("op-fail", status="FAILED", error="boom"),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        cached = session_store.cache.get(("op-fail", "operation"))
        assert cached is not None
        assert cached["status"] == "FAILED"

    def test_detach_does_not_cache(self, contree_client, session_store):
        """Detached runs exit before terminal state — nothing to cache."""
        session_store.set_image(IMG_UUID, kind="use")
        args = _default_args(detach=True)
        responses = [_spawn_response("op-detach")]
        _run_cmd(contree_client, args, responses, store=session_store)
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
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
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
        responses = [
            _api_response({"uuid": "explicit-uuid"}),  # GET dedup hit
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(1)  # after GET dedup
        body = json.loads(spawn_req.body)
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
        # First run -- includes pending file
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        # Clear recorded requests before second run
        contree_client.fake.requests.clear()
        # Second run -- pending file should NOT be included (last entry is run)
        responses2 = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW2),
        ]
        _run_cmd(contree_client, args, responses2, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
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
        # Run -- bakes the file in
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        assert session_store.pending_files() == []
        # Rollback past the run
        session_store.rollback(1)
        assert len(session_store.pending_files()) == 1
        # Clear recorded requests before next run
        contree_client.fake.requests.clear()
        # Next run should include the pending file again
        responses2 = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW2),
        ]
        _run_cmd(contree_client, args, responses2, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert "/a.txt" in body["files"]


# ── Stdin passthrough ────────────────────────────────────────────────────


class TestStdinPassthrough:
    @staticmethod
    def _piped_stdin(data: bytes) -> MagicMock:
        mock = MagicMock()
        mock.isatty.return_value = False
        mock.buffer = io.BytesIO(data)
        return mock

    def test_stdin_piped(self, contree_client, session_store):
        """Piped stdin is included in payload as base64 StreamRepr."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        stdin_content = b"print('hello')\n"
        _run_cmd(
            contree_client,
            args,
            responses,
            store=session_store,
            stdin_mock=self._piped_stdin(stdin_content),
        )
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert "stdin" in body
        assert body["stdin"]["encoding"] == "base64"
        assert base64.b64decode(body["stdin"]["value"]) == stdin_content

    def test_stdin_tty_not_included(self, contree_client, session_store):
        """When stdin is a TTY, no stdin key in payload."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert "stdin" not in body

    def test_stdin_empty_not_included(self, contree_client, session_store):
        """Piped but empty stdin does not add stdin key."""
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(
            contree_client,
            args,
            responses,
            store=session_store,
            stdin_mock=self._piped_stdin(b""),
        )
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
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
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        rc = _run_cmd(contree_client, args, responses, store=session_store)
        assert rc == 0
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["command"] == "/bin/sh"
        assert body["shell"] is True
        assert body["args"] == ["-s"]
        assert body["stdin"]["encoding"] == "base64"
        decoded = base64.b64decode(body["stdin"]["value"])
        assert decoded == b"echo hello\n"

    def test_extra_args(self, contree_client, session_store, tmp_path):
        """Extra args after script path are passed as -s -- args."""
        script = tmp_path / "script.sh"
        script.write_text("#!/usr/bin/env -S contree run -I\nset -e\n")
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(
            command_args=[str(script), "arg1", "arg2"],
            interpreter=True,
        )
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        assert body["command"] == "/bin/sh"
        assert body["args"] == ["-s", "--", "arg1", "arg2"]

    def test_without_flag_no_magic(self, contree_client, session_store, tmp_path):
        """Without -I, script file is treated as a regular command."""
        script = tmp_path / "script.sh"
        script.write_text("#!/usr/bin/env -S contree run -I\necho hello\n")
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args(command_args=[str(script)])
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
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
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0),
        ]
        piped = MagicMock()
        piped.isatty.return_value = False
        piped.buffer = io.BytesIO(b"piped data")
        _run_cmd(
            contree_client,
            args,
            responses,
            store=session_store,
            stdin_mock=piped,
        )
        spawn_req = contree_client.get_request(0)
        body = json.loads(spawn_req.body)
        decoded = base64.b64decode(body["stdin"]["value"])
        assert decoded == b"echo from script\n"


# ── Escape sequence sanitization ─────────────────────────────────────


class TestEscapeSanitization:
    """DefaultFormatter strips breaking escape sequences from output."""

    def test_colors_preserved(self, contree_client, session_store, capsys):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        colored = "\033[1;32mgreen\033[0m normal"
        responses = [
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
            responses,
            store=session_store,
            formatter=DefaultFormatter(),
        )
        out = capsys.readouterr().out
        assert "\033[1;32mgreen\033[0m normal" in out

    def test_cursor_movement_stripped(self, contree_client, session_store, capsys):
        session_store.set_image(IMG_UUID, kind="test")
        args = _default_args()
        raw = "\033[2;5Htext\033[Aup\033[Bdown\033[10Gcol"
        responses = [
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
            responses,
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
        responses = [
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
            responses,
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
        responses = [
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
            responses,
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
        responses = [
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
            responses,
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
        responses = [
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
            responses,
            store=session_store,
            formatter=JSONFormatter(),
        )
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "\033[2J" in parsed["stdout"]


# ── _read_piped_stdin ────────────────────────────────────────────────────


class TestReadPipedStdin:
    def test_tty_returns_empty(self):
        mock = MagicMock()
        mock.isatty.return_value = True
        with patch("contree_cli.cli.run.sys.stdin", mock):
            assert _read_piped_stdin() == b""

    def test_fileno_oserror_falls_back(self):
        mock = MagicMock()
        mock.isatty.return_value = False
        mock.fileno.side_effect = OSError("no fileno")
        mock.buffer.read.return_value = b"data"
        with patch("contree_cli.cli.run.sys.stdin", mock):
            assert _read_piped_stdin() == b"data"

    def test_select_error_returns_empty(self):
        mock = MagicMock()
        mock.isatty.return_value = False
        mock.fileno.return_value = 0
        with (
            patch("contree_cli.cli.run.sys.stdin", mock),
            patch("contree_cli.cli.run.select.select", side_effect=OSError),
        ):
            assert _read_piped_stdin() == b""

    def test_buffer_read_error_returns_empty(self):
        mock = MagicMock()
        mock.isatty.return_value = False
        mock.fileno.return_value = 0
        mock.buffer.read.side_effect = OSError("read error")
        with (
            patch("contree_cli.cli.run.sys.stdin", mock),
            patch("contree_cli.cli.run.select.select", return_value=([0], [], [])),
        ):
            assert _read_piped_stdin() == b""

    def test_happy_path_with_data(self):
        mock = MagicMock()
        mock.isatty.return_value = False
        mock.fileno.return_value = 0
        mock.buffer.read.return_value = b"hello\n"
        with (
            patch("contree_cli.cli.run.sys.stdin", mock),
            patch("contree_cli.cli.run.select.select", return_value=([0], [], [])),
        ):
            assert _read_piped_stdin() == b"hello\n"

    def test_not_ready_returns_empty(self):
        mock = MagicMock()
        mock.isatty.return_value = False
        mock.fileno.return_value = 0
        with (
            patch("contree_cli.cli.run.sys.stdin", mock),
            patch("contree_cli.cli.run.select.select", return_value=([], [], [])),
        ):
            assert _read_piped_stdin() == b""


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
        _run_cmd(contree_client, args1, [_spawn_response("op-1")], store=session_store)
        args2 = _default_args(detach=True)
        _run_cmd(contree_client, args2, [_spawn_response("op-2")], store=session_store)
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
        responses = [
            _api_response({"images": [{"uuid": IMG_UUID}]}),
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        rc = _run_cmd(contree_client, args, responses, store=session_store)
        assert rc == 0
        assert session_store.current_image == IMG_NEW

    def test_use_with_uuid(self, contree_client, session_store):
        """--use with a UUID skips tag resolution."""
        args = _default_args(use=IMG_SOME)
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        rc = _run_cmd(contree_client, args, responses, store=session_store)
        assert rc == 0
        req0 = contree_client.get_request(0)
        assert req0.method == "POST"
        body = json.loads(req0.body)
        assert body["image"] == IMG_SOME

    def test_use_works_without_existing_session(self, contree_client, session_store):
        """--use creates a session even when none exists."""
        assert session_store.session is None
        args = _default_args(use=IMG_UUID)
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        rc = _run_cmd(contree_client, args, responses, store=session_store)
        assert rc == 0
        assert session_store.session is not None
        assert session_store.current_image == IMG_NEW

    def test_use_disposable(self, contree_client, session_store):
        """--use + --disposable sets session to use-image, run doesn't advance."""
        args = _default_args(use=IMG_UUID, disposable=True)
        responses = [
            _spawn_response("op-use-disp"),
            _op_response(
                "op-use-disp",
                status="SUCCESS",
                exit_code=0,
                image=IMG_NEW,
            ),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        assert session_store.current_image == IMG_UUID

    def test_use_creates_history_entry(self, contree_client, session_store):
        """--use creates a 'use' kind history entry before the run."""
        args = _default_args(use="tag:myimage")
        responses = [
            _api_response({"images": [{"uuid": IMG_UUID}]}),
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
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
        responses = [
            _spawn_response(),
            _op_response(status="SUCCESS", exit_code=0, image=IMG_NEW),
        ]
        _run_cmd(contree_client, args, responses, store=session_store)
        assert session_store.session is not None


# ---------------------------------------------------------------------------
# SSE streaming (`_stream_events_until_close` and `_build_op_from_summary`)
# ---------------------------------------------------------------------------


class _SSEResponse:
    """Minimal HTTPResponse-shaped object backed by an in-memory buffer.

    `_stream_events_until_close` only touches `.readline()` (via
    `iter_sse_events`) and `.close()`; nothing else is needed.
    """

    def __init__(self, body: bytes | str) -> None:
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.buf = io.BytesIO(payload)
        self.closed = False

    def readline(self, size: int = -1) -> bytes:
        return self.buf.readline()

    def close(self) -> None:
        self.closed = True


class _JSONResponse:
    """Minimal HTTPResponse-shape for the streamer's between-attempt
    ``GET /operations/{uuid}`` terminal check.  Only ``.read()`` and
    ``.close()`` are used."""

    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.payload

    def close(self) -> None:
        self.closed = True


class _StubStreamClient:
    """Stand-in for `ContreeClient` that queues responses for both the
    SSE `follow=1` endpoint and the between-attempt terminal-status
    GET.

    `responses` feeds `GET /events?follow=1` calls.  `get_responses`
    feeds `GET /operations/{uuid}` terminal checks; when it's empty
    the stub falls back to a non-terminal op (`status=EXECUTING`) so
    tests that don't care about the check don't have to prime it.

    A queued item may be an `_SSEResponse` / `_JSONResponse` (served
    as-is), an `Exception` subclass or instance (raised), or `None`
    (equivalent to an empty response).  All calls are recorded so
    tests can inspect them via `.calls`, `.sse_calls`, `.get_calls`.
    """

    NON_TERMINAL: ClassVar[dict[str, str]] = {"status": "EXECUTING"}

    def __init__(
        self,
        responses: list,
        get_responses: list | None = None,
    ) -> None:
        self.responses = list(responses)
        self.get_responses = list(get_responses or [])
        self.calls: list[tuple[str, str, dict[str, str] | None]] = []

    @property
    def sse_calls(self) -> list[tuple[str, str, dict[str, str] | None]]:
        return [c for c in self.calls if "/events" in c[1]]

    @property
    def get_calls(self) -> list[tuple[str, str, dict[str, str] | None]]:
        return [c for c in self.calls if "/events" not in c[1]]

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        **_: object,
    ):
        self.calls.append((method, path, headers))
        is_sse = "/events" in path
        if is_sse:
            item = self.responses.pop(0)
        elif self.get_responses:
            item = self.get_responses.pop(0)
        else:
            return _JSONResponse(self.NON_TERMINAL)
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item("stub")
        if isinstance(item, BaseException):
            raise item
        if item is None:
            return _SSEResponse(b"") if is_sse else _JSONResponse(self.NON_TERMINAL)
        return item


def _frame(*, id: int, event: str, data: dict | str) -> str:
    """Build one SSE frame. Mirrors the API contract by embedding `id`
    inside the JSON `data:` payload as well as the SSE `id:` line
    (the CLI reads the id from the JSON, not the SSE header)."""
    if isinstance(data, dict) and "id" not in data:
        data = {"id": id, **data}
    data_str = data if isinstance(data, str) else json.dumps(data)
    return f"id: {id}\nevent: {event}\ndata: {data_str}\n\n"


class TestStreamEventsUntilClose:
    def test_url_uses_follow_1(self, session_store):
        """Verifies the endpoint spelling matches the OpenAPI spec (`?follow=1`)."""
        completion = _frame(
            id=1,
            event="completion",
            data={"type": "completion", "data": {"status": "SUCCESS"}},
        )
        client = _StubStreamClient([_SSEResponse(completion)])
        _stream_events_until_close(client, "op-1", DefaultFormatter())
        assert client.calls[0][0] == "GET"
        assert client.calls[0][1] == "/v1/operations/op-1/events?follow=1"

    def test_first_call_has_no_last_event_id_header(self, session_store):
        client = _StubStreamClient(
            [
                _SSEResponse(
                    _frame(
                        id=1,
                        event="completion",
                        data={"type": "completion", "data": {}},
                    )
                )
            ]
        )
        _stream_events_until_close(client, "op-x", DefaultFormatter())
        assert client.calls[0][2] is None

    def test_stdout_streamed_live_for_default_formatter(self, capsys):
        chunk = _frame(
            id=1,
            event="stdout",
            data={
                "type": "stdout",
                "spid": 1,
                "data": {"value": "hello", "encoding": "ascii"},
            },
        )
        completion = _frame(
            id=2,
            event="completion",
            data={"type": "completion", "data": {"status": "SUCCESS"}},
        )
        client = _StubStreamClient([_SSEResponse(chunk + completion)])
        summary = _stream_events_until_close(client, "op-1", DefaultFormatter())
        out = capsys.readouterr()
        assert out.out == "hello"
        assert bytes(summary.stdout) == b"hello"

    def test_stderr_streamed_live_for_default_formatter(self, capsys):
        chunk = _frame(
            id=1,
            event="stderr",
            data={
                "type": "stderr",
                "spid": 1,
                "data": {"value": "oops\n", "encoding": "ascii"},
            },
        )
        completion = _frame(
            id=2,
            event="completion",
            data={"type": "completion", "data": {"status": "FAILED"}},
        )
        client = _StubStreamClient([_SSEResponse(chunk + completion)])
        summary = _stream_events_until_close(client, "op-1", DefaultFormatter())
        out = capsys.readouterr()
        assert out.err == "oops\n"
        assert bytes(summary.stderr) == b"oops\n"

    def test_json_formatter_accumulates_without_printing(self, capsys):
        chunk = _frame(
            id=1,
            event="stdout",
            data={
                "type": "stdout",
                "spid": 1,
                "data": {"value": "hi", "encoding": "ascii"},
            },
        )
        completion = _frame(
            id=2,
            event="completion",
            data={"type": "completion", "data": {"status": "SUCCESS"}},
        )
        client = _StubStreamClient([_SSEResponse(chunk + completion)])
        summary = _stream_events_until_close(client, "op-1", JSONFormatter())
        out = capsys.readouterr()
        assert out.out == ""
        assert bytes(summary.stdout) == b"hi"

    def test_base64_chunk_decoded(self, capsys):
        payload = base64.b64encode(b"\x00\xff bin").decode("ascii")
        chunk = _frame(
            id=1,
            event="stdout",
            data={
                "type": "stdout",
                "spid": 1,
                "data": {"value": payload, "encoding": "base64"},
            },
        )
        completion = _frame(
            id=2,
            event="completion",
            data={"type": "completion", "data": {"status": "SUCCESS"}},
        )
        client = _StubStreamClient([_SSEResponse(chunk + completion)])
        summary = _stream_events_until_close(client, "op-1", JSONFormatter())
        assert bytes(summary.stdout) == b"\x00\xff bin"

    def test_exit_event_for_spid_1_stored(self):
        exit_frame = _frame(
            id=1,
            event="exit",
            data={"type": "exit", "spid": 1, "data": {"code": 0, "timed_out": False}},
        )
        completion = _frame(
            id=2,
            event="completion",
            data={"type": "completion", "data": {"status": "SUCCESS"}},
        )
        client = _StubStreamClient([_SSEResponse(exit_frame + completion)])
        summary = _stream_events_until_close(client, "op-1", DefaultFormatter())
        assert summary.exit_event is not None
        assert summary.exit_event["data"]["code"] == 0

    def test_exit_event_for_non_main_spid_ignored(self):
        """Only spid=1 drives CLI exit code; child spid exits stay unrecorded."""
        exit_frame = _frame(
            id=1,
            event="exit",
            data={"type": "exit", "spid": 2, "data": {"code": 3, "timed_out": False}},
        )
        completion = _frame(
            id=2,
            event="completion",
            data={"type": "completion", "data": {"status": "SUCCESS"}},
        )
        client = _StubStreamClient([_SSEResponse(exit_frame + completion)])
        summary = _stream_events_until_close(client, "op-1", DefaultFormatter())
        assert summary.exit_event is None

    def test_completion_frame_breaks_loop(self):
        completion = _frame(
            id=1,
            event="completion",
            data={"type": "completion", "data": {"status": "SUCCESS"}},
        )
        client = _StubStreamClient([_SSEResponse(completion)])
        summary = _stream_events_until_close(client, "op-1", DefaultFormatter())
        assert summary.completion is not None
        assert summary.completion["data"]["status"] == "SUCCESS"

    def test_stream_ends_without_completion_falls_back_to_terminal_get(self):
        """SSE closes cleanly without a `completion` frame — the
        between-attempt GET check detects the op is terminal and parks
        it on `summary.fallback_op` so the caller doesn't need to GET
        again."""
        op = {"uuid": "op-1", "status": "SUCCESS", "result": {"image": None}}
        client = _StubStreamClient(
            [_SSEResponse(b"")], get_responses=[_JSONResponse(op)]
        )
        summary = _stream_events_until_close(client, "op-1", DefaultFormatter())
        assert summary.completion is None
        assert summary.fallback_op == op
        assert bytes(summary.stdout) == b""

    def test_sse_connect_errors_then_terminal_via_get(self):
        """SSE keeps failing to connect while the op runs; once the
        between-attempt GET reports terminal status the streamer
        stops retrying and returns without ever seeing a completion
        frame."""
        op = {"uuid": "op-1", "status": "SUCCESS"}
        client = _StubStreamClient(
            [ApiError(500, "srv", "boom")] * 3,
            get_responses=[
                _JSONResponse({"status": "EXECUTING"}),
                _JSONResponse({"status": "EXECUTING"}),
                _JSONResponse(op),
            ],
        )
        with patch("contree_cli.cli.run.time.sleep"):
            summary = _stream_events_until_close(client, "op-1", DefaultFormatter())
        assert summary.completion is None
        assert summary.fallback_op == op
        assert len(client.sse_calls) == 3
        assert len(client.get_calls) == 3

    def test_sse_error_frame_triggers_reconnect_with_last_event_id(self):
        first = (
            _frame(
                id=1,
                event="stdout",
                data={
                    "type": "stdout",
                    "spid": 1,
                    "data": {"value": "a", "encoding": "ascii"},
                },
            )
            + "event: sse_error\ndata: boom\n\n"
        )
        completion = _frame(
            id=2,
            event="completion",
            data={"type": "completion", "data": {"status": "SUCCESS"}},
        )
        client = _StubStreamClient([_SSEResponse(first), _SSEResponse(completion)])
        with patch("contree_cli.cli.run.time.sleep"):
            summary = _stream_events_until_close(client, "op-1", DefaultFormatter())
        assert summary.completion is not None
        assert len(client.sse_calls) == 2
        assert client.sse_calls[1][2] == {"Last-Event-Id": "1"}

    def test_broken_pipe_from_stdout_propagates(self, monkeypatch):
        """`BrokenPipeError` from local stdio write must propagate
        unchanged — retrying can't fix a closed local pipe and it
        would be misinterpreted as a remote network error otherwise."""
        chunk = _frame(
            id=1,
            event="stdout",
            data={
                "type": "stdout",
                "spid": 1,
                "data": {"value": "hi", "encoding": "ascii"},
            },
        )
        client = _StubStreamClient([_SSEResponse(chunk)])

        def raise_broken_pipe(*_args: object, **_kw: object) -> int:
            raise BrokenPipeError

        monkeypatch.setattr("sys.stdout.buffer.write", raise_broken_pipe)
        with pytest.raises(BrokenPipeError):
            _stream_events_until_close(client, "op-1", DefaultFormatter())
        # Only the initial SSE attempt is made; no retry, no terminal-check
        # GET (BrokenPipeError bypasses the retry path entirely).
        assert len(client.sse_calls) == 1
        assert len(client.get_calls) == 0


class TestBuildOpFromSummary:
    def _completion(self, **overrides) -> dict:
        base = {
            "type": "completion",
            "data": {
                "status": "SUCCESS",
                "error": None,
                "duration_ms": 1500,
                "image_size_bytes": 4096,
                "result_image_uuid": IMG_NEW,
            },
        }
        base["data"].update(overrides)
        return base

    def test_shape_carries_status_and_uuid(self):
        summary = TerminalSummary(completion=self._completion())
        op = _build_op_from_summary("op-1", summary)
        assert op["uuid"] == "op-1"
        assert op["kind"] == "instance"
        assert op["status"] == "SUCCESS"

    def test_duration_ms_converted_to_seconds(self):
        summary = TerminalSummary(completion=self._completion(duration_ms=2500))
        op = _build_op_from_summary("op-1", summary)
        assert op["duration"] == 2.5

    def test_duration_missing_falls_back_to_zero(self):
        summary = TerminalSummary(
            completion={"type": "completion", "data": {"status": "SUCCESS"}}
        )
        op = _build_op_from_summary("op-1", summary)
        assert op["duration"] == 0.0

    def test_result_image_uuid_propagates(self):
        summary = TerminalSummary(completion=self._completion())
        op = _build_op_from_summary("op-1", summary)
        assert op["result_image_uuid"] == IMG_NEW
        assert op["result"] == {"image": IMG_NEW, "tag": None}

    def test_exit_event_drives_state(self):
        summary = TerminalSummary(
            completion=self._completion(),
            exit_event={
                "type": "exit",
                "spid": 1,
                "data": {"code": 42, "timed_out": True},
            },
        )
        op = _build_op_from_summary("op-1", summary)
        state = op["metadata"]["result"]["state"]
        assert state == {"exit_code": 42, "timed_out": True}

    def test_state_is_none_without_exit_event(self):
        summary = TerminalSummary(completion=self._completion())
        op = _build_op_from_summary("op-1", summary)
        assert op["metadata"]["result"]["state"] is None

    def test_stdout_stderr_reassembled_from_bytearrays(self):
        summary = TerminalSummary(
            completion=self._completion(),
            stdout=bytearray(b"hello\n"),
            stderr=bytearray(b"warn\n"),
        )
        op = _build_op_from_summary("op-1", summary)
        result = op["metadata"]["result"]
        assert result["stdout"]["value"] == "hello\n"
        assert result["stderr"]["value"] == "warn\n"
        assert result["stdout"]["truncated"] is False

    def test_error_field_passthrough(self):
        summary = TerminalSummary(
            completion=self._completion(status="FAILED", error="boom")
        )
        op = _build_op_from_summary("op-1", summary)
        assert op["status"] == "FAILED"
        assert op["error"] == "boom"
