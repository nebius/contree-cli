from __future__ import annotations

import json
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import ContreeTestClient
from contree_client.exceptions import ContreeAPIError, NotFoundError
from contree_client.models import File, FileResponse, FilesListResponse

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
from contree_cli.output import JSONFormatter
from contree_cli.session import SessionStore


def file_payload(uuid: str, *, sha256: str = "0" * 64, size: int = 1) -> dict:
    """Full ``File`` payload as returned by GET /v1/files/{sha256}."""
    return {
        "uuid": uuid,
        "sha256": sha256,
        "size": size,
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-01T00:00:00Z",
    }


def make_file(uuid: str, *, sha256: str = "0" * 64, size: int = 1) -> File:
    return File.from_dict(file_payload(uuid, sha256=sha256, size=size))


def mock_download(tc: ContreeTestClient, content: bytes | None) -> None:
    """Mock the image file download stream; None means 404 (no file)."""
    if content is None:
        tc.mock(
            "inspect_image_download_stream",
            error=NotFoundError(404, "not found"),
        )
    else:
        tc.mock("inspect_image_download_stream", [content])


def mock_dedup_miss(tc: ContreeTestClient) -> None:
    tc.mock("get_file", error=NotFoundError(404, "not found"))


def mock_dedup_hit(tc: ContreeTestClient, uuid: str) -> None:
    tc.mock("get_file", make_file(uuid))


def mock_upload(tc: ContreeTestClient, uuid: str) -> None:
    tc.mock("upload_file", FileResponse(uuid=uuid, sha256="0" * 64, size=1))


def _run_file_edit(
    tc: ContreeTestClient,
    args: FileEditArgs,
    *,
    store: SessionStore,
    editor_content: bytes | None = None,
) -> int | None:
    """Drive ``cmd_file_edit`` with a mocked editor.

    The editor mock writes ``editor_content`` directly to the file
    inside the temp dir created by ``cmd_file_edit``. This sidesteps
    shell-string parsing, which has subtle differences on Windows
    when paths contain backslashes.
    """
    import tempfile

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
    files: list[dict],
    *,
    store: SessionStore,
) -> int | None:
    tc.mock("list_files", FilesListResponse.from_dict({"files": files}))
    CLIENT.set(tc)
    SESSION_STORE.set(store)
    FORMATTER.set(JSONFormatter())
    ctx = copy_context()
    return ctx.run(cmd_file_ls, args)


class TestFileLs:
    def test_lists_with_source(self, contree_client, session_store, capsys):
        session_store.cache[("", "local_file:a")] = {
            "uuid": "file-1",
            "local_path": "/host/app.py",
        }
        session_store.cache[("", "local_file:https://example.com/pkg.tgz")] = {
            "uuid": "file-3",
            "url": "https://example.com/pkg.tgz",
        }
        files = [
            file_payload("file-1", sha256="a" * 64, size=10),
            file_payload("file-2", sha256="d" * 64, size=20),
            file_payload("file-3", sha256="e" * 64, size=30),
        ]
        rc = _run_file_ls(
            contree_client,
            FileListArgs(limit=10),
            files,
            store=session_store,
        )
        assert rc is None
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert rows[0]["source"] == "/host/app.py"
        assert rows[1]["source"] == ""
        assert rows[2]["source"] == "https://example.com/pkg.tgz"

    def test_quiet_emits_three_columns(self, contree_client, session_store, capsys):
        session_store.cache[("", "local_file:a")] = {
            "uuid": "file-1",
            "local_path": "/host/app.py",
        }
        files = [file_payload("file-1", sha256="a" * 64, size=10)]
        _run_file_ls(
            contree_client,
            FileListArgs(limit=10, quiet=True),
            files,
            store=session_store,
        )
        row = json.loads(capsys.readouterr().out.strip())
        assert set(row) == {"uuid", "sha256", "source"}
        assert row["source"] == "/host/app.py"


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
        mock_download(contree_client, b"original content")
        mock_dedup_miss(contree_client)
        mock_upload(contree_client, "file-uuid-1")
        rc = _run_file_edit(
            contree_client,
            args,
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
        mock_download(contree_client, None)
        mock_dedup_miss(contree_client)
        mock_upload(contree_client, "file-uuid-2")
        rc = _run_file_edit(
            contree_client,
            args,
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
        contree_client.mock(
            "inspect_image_download_stream",
            error=ContreeAPIError(403, "forbidden"),
        )
        with pytest.raises(ContreeAPIError) as exc_info:
            _run_file_edit(contree_client, args, store=session_store)
        assert exc_info.value.status == 403


class TestFileEditNoChanges:
    def test_no_changes_skips_upload(self, contree_client, session_store: SessionStore):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = FileEditArgs(path="/etc/config.ini")
        mock_download(contree_client, b"same content")
        # editor_content=None means editor doesn't modify the file
        rc = _run_file_edit(
            contree_client,
            args,
            store=session_store,
            editor_content=None,
        )
        assert rc is None
        # No API calls beyond the download
        assert len(contree_client.calls) == 1
        # No pending files
        assert session_store.pending_files() == []


class TestFileEditDedup:
    def test_dedup_hit_skips_upload(self, contree_client, session_store: SessionStore):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = FileEditArgs(path="/etc/config.ini")
        mock_download(contree_client, b"original")
        mock_dedup_hit(contree_client, "existing-uuid")
        rc = _run_file_edit(
            contree_client,
            args,
            store=session_store,
            editor_content=b"modified",
        )
        assert rc is None
        files = session_store.pending_files()
        assert files[0].file_uuid == "existing-uuid"
        # Only 2 API calls: download + dedup check (no upload)
        assert len(contree_client.calls) == 2
        assert contree_client.calls_for("upload_file") == []


class TestFileEditEditorFailure:
    def test_editor_nonzero_exit(self, contree_client, session_store: SessionStore):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = FileEditArgs(path="/etc/config.ini")
        mock_download(contree_client, b"content")

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
        mock_download(contree_client, b"original")
        mock_dedup_miss(contree_client)
        mock_upload(contree_client, "file-uuid-e")

        called_with: list[str] = []

        def fake_editor(cmd: str, *, shell: bool = True) -> int:
            called_with.append(cmd)
            import shlex

            parts = shlex.split(cmd)
            Path(parts[1]).write_bytes(b"modified")
            return 0

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
        mock_download(contree_client, b"original")
        mock_dedup_miss(contree_client)
        mock_upload(contree_client, "file-uuid")
        _run_file_edit(
            contree_client,
            args,
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
    *,
    store: SessionStore,
) -> int | None:
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
        mock_dedup_miss(contree_client)
        mock_upload(contree_client, "file-uuid-cp")
        rc = _run_file_cp(contree_client, args, store=session_store)
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
        mock_dedup_hit(contree_client, "existing-uuid")
        rc = _run_file_cp(contree_client, args, store=session_store)
        assert rc is None
        files = session_store.pending_files()
        assert files[0].file_uuid == "existing-uuid"
        # Only 1 API call: dedup check (no upload)
        assert len(contree_client.calls) == 1
        assert contree_client.calls_for("upload_file") == []

    def test_cp_missing_file_returns_error(
        self,
        contree_client,
        session_store: SessionStore,
    ):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = FileCpArgs(src="/nonexistent/file.py", dest="/app/file.py")
        rc = _run_file_cp(contree_client, args, store=session_store)
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
        mock_dedup_miss(contree_client)
        mock_upload(contree_client, "file-uuid")
        _run_file_cp(contree_client, args, store=session_store)
        s = session_store.session
        assert s is not None
        assert s.last_kind == "file"
        assert s.last_title == "Change file /opt/data.txt"
