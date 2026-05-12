from __future__ import annotations

import io
import json
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import ContreeTestClient

from contree_cli import CLIENT, FORMATTER, SESSION_STORE
from contree_cli.cli.file import (
    FileCpArgs,
    FileEditArgs,
    FileListArgs,
    _file_sha256,
    cmd_file_cp,
    cmd_file_edit,
    cmd_file_ls,
)
from contree_cli.client import ApiError
from contree_cli.output import JSONFormatter
from contree_cli.session import SessionStore


class StreamResponse:
    """Response that supports chunked reading for stream_response()."""

    def __init__(self, data: bytes, *, status: int = 200):
        self.status = status
        self.reason = "OK" if status < 300 else "Error"
        self._buf = io.BytesIO(data)

    def read(self, amt: int | None = None) -> bytes:
        return self._buf.read(amt)

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return default

    def getheaders(self) -> list[tuple[str, str]]:
        return []


def _api_response(body: bytes | dict, *, status: int = 200) -> StreamResponse:
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    return StreamResponse(data, status=status)


def _run_file_edit(
    tc: ContreeTestClient,
    args: FileEditArgs,
    responses: list[StreamResponse],
    *,
    store: SessionStore,
    editor_content: bytes | None = None,
) -> tuple[int | None]:
    """Drive ``cmd_file_edit`` with mocked editor + HTTP responses.

    The editor mock writes ``editor_content`` directly to the file
    inside the temp dir created by ``cmd_file_edit``. This sidesteps
    shell-string parsing, which has subtle differences on Windows
    when paths contain backslashes.
    """
    import tempfile

    tc.fake.responses.extend(responses)

    if not args.editor:
        args = replace(args, editor="fake-editor")

    SESSION_STORE.set(store)
    ctx = copy_context()

    captured_dir: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def capturing_mkdtemp(*a, **kw):
        d = real_mkdtemp(*a, **kw)
        captured_dir.append(d)
        return d

    def fake_editor(cmd: str, *, shell: bool = True) -> int:
        if editor_content is not None and captured_dir:
            for f in Path(captured_dir[-1]).iterdir():
                f.write_bytes(editor_content)
        return 0

    with (
        patch(
            "contree_cli.cli.file.tempfile.mkdtemp",
            side_effect=capturing_mkdtemp,
        ),
        patch("contree_cli.cli.file.subprocess.call", side_effect=fake_editor),
    ):
        rc = ctx.run(cmd_file_edit, args)
    return rc


def _run_file_ls(
    tc: ContreeTestClient,
    args: FileListArgs,
    responses: list[StreamResponse],
    *,
    store: SessionStore,
) -> int | None:
    tc.fake.responses.extend(responses)
    CLIENT.set(tc)
    SESSION_STORE.set(store)
    FORMATTER.set(JSONFormatter())
    ctx = copy_context()
    return ctx.run(cmd_file_ls, args)


class TestFileLs:
    def test_lists_with_local_path(self, contree_client, session_store, capsys):
        session_store.cache[("", "local_file:a")] = {
            "uuid": "file-1",
            "local_path": "/host/app.py",
        }
        responses = [
            _api_response(
                {
                    "files": [
                        {"uuid": "file-1", "sha256": "abc", "size": 10},
                        {"uuid": "file-2", "sha256": "def", "size": 20},
                    ]
                }
            ),
        ]
        rc = _run_file_ls(
            contree_client,
            FileListArgs(limit=10),
            responses,
            store=session_store,
        )
        assert rc is None
        out = capsys.readouterr().out.splitlines()
        rows = [json.loads(line) for line in out]
        assert rows[0]["uuid"] == "file-1"
        assert rows[0]["local_path"] == "/host/app.py"
        assert rows[1]["uuid"] == "file-2"
        assert rows[1]["local_path"] == ""

    def test_quiet_emits_three_columns(self, contree_client, session_store, capsys):
        session_store.cache[("", "local_file:a")] = {
            "uuid": "file-1",
            "local_path": "/host/app.py",
        }
        responses = [
            _api_response(
                {
                    "files": [
                        {
                            "uuid": "file-1",
                            "sha256": "abc",
                            "size": 10,
                            "created_at": "2026-05-01T00:00:00Z",
                        },
                    ]
                }
            ),
        ]
        _run_file_ls(
            contree_client,
            FileListArgs(limit=10, quiet=True),
            responses,
            store=session_store,
        )
        out = capsys.readouterr().out.strip()
        row = json.loads(out)
        assert set(row) == {"uuid", "sha256", "local_path"}
        assert row["uuid"] == "file-1"
        assert row["sha256"] == "abc"
        assert row["local_path"] == "/host/app.py"


class TestFileSha256:
    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty"
        f.write_bytes(b"")
        h = _file_sha256(f)
        assert len(h) == 64

    def test_known_content(self, tmp_path: Path):
        f = tmp_path / "data"
        f.write_bytes(b"hello")
        h = _file_sha256(f)
        assert h == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


class TestFileEditDownload:
    def test_downloads_existing_file(self, contree_client, session_store: SessionStore):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = FileEditArgs(path="/etc/config.ini")
        download_resp = _api_response(b"original content")
        dedup_miss = _api_response({"error": "not found"}, status=404)
        upload_resp = _api_response({"uuid": "file-uuid-1"}, status=201)
        responses = [download_resp, dedup_miss, upload_resp]
        rc = _run_file_edit(
            contree_client,
            args,
            responses,
            store=session_store,
            editor_content=b"modified content",
        )
        assert rc is None
        # Should have pending file (history tip is kind=file, not run)
        files = session_store.pending_files()
        assert len(files) == 1
        assert files[0].instance_path == "/etc/config.ini"
        assert files[0].file_uuid == "file-uuid-1"

    def test_creates_empty_on_404(self, contree_client, session_store: SessionStore):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = FileEditArgs(path="/new/file.txt")
        not_found = _api_response({"error": "not found"}, status=404)
        dedup_miss = _api_response({"error": "not found"}, status=404)
        upload_resp = _api_response({"uuid": "file-uuid-2"}, status=201)
        responses = [not_found, dedup_miss, upload_resp]
        rc = _run_file_edit(
            contree_client,
            args,
            responses,
            store=session_store,
            editor_content=b"new file content",
        )
        assert rc is None
        files = session_store.pending_files()
        assert len(files) == 1
        assert files[0].file_uuid == "file-uuid-2"

    def test_non_404_error_propagates(
        self, contree_client, session_store: SessionStore
    ):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = FileEditArgs(path="/etc/config.ini")
        # Client retries 500s (RETRY_DELAYS has 7 entries -> 8 attempts total)
        responses = [
            _api_response({"error": "server error"}, status=500) for _ in range(8)
        ]
        with (
            patch("time.sleep"),
            pytest.raises(ApiError) as exc_info,
        ):
            _run_file_edit(contree_client, args, responses, store=session_store)
        assert exc_info.value.status == 500


class TestFileEditNoChanges:
    def test_no_changes_skips_upload(self, contree_client, session_store: SessionStore):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = FileEditArgs(path="/etc/config.ini")
        download_resp = _api_response(b"same content")
        responses = [download_resp]
        # editor_content=None means editor doesn't modify the file
        rc = _run_file_edit(
            contree_client,
            args,
            responses,
            store=session_store,
            editor_content=None,
        )
        assert rc is None
        # No HTTP calls beyond the download
        assert contree_client.request_count == 1
        # No pending files
        assert session_store.pending_files() == []


class TestFileEditDedup:
    def test_dedup_hit_skips_upload(self, contree_client, session_store: SessionStore):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = FileEditArgs(path="/etc/config.ini")
        download_resp = _api_response(b"original")
        dedup_hit = _api_response({"uuid": "existing-uuid"})
        responses = [download_resp, dedup_hit]
        rc = _run_file_edit(
            contree_client,
            args,
            responses,
            store=session_store,
            editor_content=b"modified",
        )
        assert rc is None
        files = session_store.pending_files()
        assert files[0].file_uuid == "existing-uuid"
        # Only 2 HTTP calls: download + dedup check (no POST upload)
        assert contree_client.request_count == 2


class TestFileEditEditorFailure:
    def test_editor_nonzero_exit(self, contree_client, session_store: SessionStore):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = FileEditArgs(path="/etc/config.ini")
        download_resp = _api_response(b"content")

        tc = contree_client
        tc.fake.responses.append(download_resp)

        SESSION_STORE.set(session_store)
        ctx = copy_context()

        with patch("contree_cli.cli.file.subprocess.call", return_value=1):
            rc = ctx.run(cmd_file_edit, replace(args, editor="fake-editor"))
        assert rc == 1
        assert session_store.pending_files() == []


class TestFileEditEditorFlag:
    def test_editor_flag_overrides_env(
        self,
        contree_client,
        session_store: SessionStore,
    ):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = FileEditArgs(path="/etc/config.ini", editor="nvim")
        download_resp = _api_response(b"original")
        dedup_miss = _api_response({"error": "not found"}, status=404)
        upload_resp = _api_response({"uuid": "file-uuid-e"}, status=201)

        called_with: list[str] = []

        def fake_editor(cmd: str, *, shell: bool = True) -> int:
            called_with.append(cmd)
            import shlex

            parts = shlex.split(cmd)
            Path(parts[1]).write_bytes(b"modified")
            return 0

        tc = contree_client
        tc.fake.responses.extend([download_resp, dedup_miss, upload_resp])
        SESSION_STORE.set(session_store)
        ctx = copy_context()

        with patch("contree_cli.cli.file.subprocess.call", side_effect=fake_editor):
            rc = ctx.run(cmd_file_edit, args)
        assert rc is None
        assert called_with[0].startswith("nvim ")


class TestFileEditHistoryEntry:
    def test_history_entry_created(self, contree_client, session_store: SessionStore):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = FileEditArgs(path="/etc/config.ini")
        download_resp = _api_response(b"original")
        dedup_miss = _api_response({"error": "not found"}, status=404)
        upload_resp = _api_response({"uuid": "file-uuid"}, status=201)
        responses = [download_resp, dedup_miss, upload_resp]
        _run_file_edit(
            contree_client,
            args,
            responses,
            store=session_store,
            editor_content=b"modified",
        )
        s = session_store.session
        assert s is not None
        assert s.last_kind == "file"
        assert s.last_title == "Change file /etc/config.ini"


# --- file cp tests ---


def _run_file_cp(
    tc: ContreeTestClient,
    args: FileCpArgs,
    responses: list[StreamResponse],
    *,
    store: SessionStore,
) -> int | None:
    tc.fake.responses.extend(responses)

    SESSION_STORE.set(store)
    ctx = copy_context()

    rc = ctx.run(cmd_file_cp, args)
    return rc


class TestFileCp:
    def test_cp_uploads_and_records(
        self,
        contree_client,
        tmp_path: Path,
        session_store: SessionStore,
    ):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        src = tmp_path / "app.py"
        src.write_bytes(b"print('hello')")
        args = FileCpArgs(src=str(src), dest="/app/app.py")
        dedup_miss = _api_response({"error": "not found"}, status=404)
        upload_resp = _api_response({"uuid": "file-uuid-cp"}, status=201)
        rc = _run_file_cp(
            contree_client,
            args,
            [dedup_miss, upload_resp],
            store=session_store,
        )
        assert rc is None
        files = session_store.pending_files()
        assert len(files) == 1
        assert files[0].instance_path == "/app/app.py"
        assert files[0].file_uuid == "file-uuid-cp"

    def test_cp_dedup_hit(
        self,
        contree_client,
        tmp_path: Path,
        session_store: SessionStore,
    ):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        src = tmp_path / "app.py"
        src.write_bytes(b"print('hello')")
        args = FileCpArgs(src=str(src), dest="/app/app.py")
        dedup_hit = _api_response({"uuid": "existing-uuid"})
        rc = _run_file_cp(
            contree_client,
            args,
            [dedup_hit],
            store=session_store,
        )
        assert rc is None
        files = session_store.pending_files()
        assert files[0].file_uuid == "existing-uuid"
        # Only 1 HTTP call: dedup check (no POST upload)
        assert contree_client.request_count == 1

    def test_cp_missing_file_returns_error(
        self,
        contree_client,
        session_store: SessionStore,
    ):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = FileCpArgs(src="/nonexistent/file.py", dest="/app/file.py")
        rc = _run_file_cp(contree_client, args, [], store=session_store)
        assert rc == 1
        assert session_store.pending_files() == []

    def test_cp_history_entry_title(
        self,
        contree_client,
        tmp_path: Path,
        session_store: SessionStore,
    ):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        src = tmp_path / "data.txt"
        src.write_bytes(b"data")
        args = FileCpArgs(src=str(src), dest="/opt/data.txt")
        dedup_miss = _api_response({"error": "not found"}, status=404)
        upload_resp = _api_response({"uuid": "file-uuid"}, status=201)
        _run_file_cp(
            contree_client,
            args,
            [dedup_miss, upload_resp],
            store=session_store,
        )
        s = session_store.session
        assert s is not None
        assert s.last_kind == "file"
        assert s.last_title == "Change file /opt/data.txt"
