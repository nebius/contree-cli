from __future__ import annotations

import logging
from contextlib import ExitStack
from contextvars import copy_context
from unittest.mock import patch

import pytest
from conftest import ContreeTestClient
from contree_client.exceptions import NotFoundError
from contree_client.runtime import CHUNK_SIZE

from contree_cli import FORMATTER, SESSION_STORE
from contree_cli.cli.cp import CpArgs, cmd_cp, fmt_size
from contree_cli.output import DefaultFormatter, JSONFormatter
from contree_cli.session import SessionStore


def _run_cmd(
    tc: ContreeTestClient,
    chunks: list[bytes] | BaseException | None = None,
    *,
    store: SessionStore,
    image: str = "a1b2c3d4-5678-9abc-def0-111111111111",
    path: str = "/etc/hosts",
    dest: str = "/tmp/out",
    images_response: dict | None = None,
    formatter=None,
    time_values: list[float] | None = None,
):
    """Run cmd_cp with mocked responses."""
    if images_response is not None:
        tc.mock("inspect_find_image_by_tag", images_response["images"][0]["uuid"])
    if isinstance(chunks, BaseException):
        tc.mock("inspect_image_download_stream", error=chunks)
    else:
        tc.mock("inspect_image_download_stream", chunks or [])

    FORMATTER.set(formatter or DefaultFormatter())
    store.set_image(image, kind="test")
    SESSION_STORE.set(store)
    ctx = copy_context()

    args = CpArgs(path=path, dest=dest)
    with ExitStack() as stack:
        if time_values is not None:
            stack.enter_context(
                patch(
                    "contree_cli.cli.cp.time.monotonic",
                    side_effect=time_values,
                ),
            )
        result = ctx.run(cmd_cp, args)

    return result


class TestCmdCp:
    def test_request_args(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "out"
        _run_cmd(
            contree_client,
            [b"hello"],
            store=session_store,
            path="/etc/hosts",
            dest=str(dest),
        )
        calls = contree_client.calls_for("inspect_image_download_stream")
        assert len(calls) == 1
        assert calls[0].args == (
            "a1b2c3d4-5678-9abc-def0-111111111111",
            "/etc/hosts",
        )

    def test_writes_file(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "output.bin"
        result = _run_cmd(
            contree_client,
            [b"file contents here"],
            store=session_store,
            dest=str(dest),
        )
        assert result is None
        assert dest.read_bytes() == b"file contents here"

    def test_dest_directory_appends_basename(
        self, contree_client, session_store, tmp_path
    ):
        result = _run_cmd(
            contree_client,
            [b"content"],
            store=session_store,
            path="/etc/os-release",
            dest=str(tmp_path),
        )
        assert result is None
        assert (tmp_path / "os-release").read_bytes() == b"content"

    def test_tag_resolution(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "out"
        images_resp = {"images": [{"uuid": "resolved-uuid", "tag": "latest"}]}
        _run_cmd(
            contree_client,
            [b"data"],
            store=session_store,
            image="tag:latest",
            images_response=images_resp,
            dest=str(dest),
        )
        resolve_calls = contree_client.calls_for("inspect_find_image_by_tag")
        assert len(resolve_calls) == 1
        assert resolve_calls[0].args == ("latest",)
        stream_calls = contree_client.calls_for("inspect_image_download_stream")
        assert len(stream_calls) == 1
        assert stream_calls[0].args[0] == "resolved-uuid"

    def test_empty_file(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "empty"
        result = _run_cmd(contree_client, [], store=session_store, dest=str(dest))
        assert result is None
        assert dest.read_bytes() == b""

    def test_non_default_formatter_warns(
        self, contree_client, session_store, tmp_path, caplog
    ):
        dest = tmp_path / "out"
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.cp"):
            _run_cmd(
                contree_client,
                [b"hello"],
                store=session_store,
                formatter=JSONFormatter(),
                dest=str(dest),
            )
        assert any("--format is ignored" in r.message for r in caplog.records)

    def test_overwrites_existing(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "existing.txt"
        dest.write_bytes(b"old content")
        result = _run_cmd(
            contree_client, [b"new content"], store=session_store, dest=str(dest)
        )
        assert result is None
        assert dest.read_bytes() == b"new content"

    def test_missing_path_leaves_no_partial_file(
        self, contree_client, session_store, tmp_path
    ):
        dest = tmp_path / "out.bin"
        with pytest.raises(NotFoundError):
            _run_cmd(
                contree_client,
                NotFoundError(404, "path not found"),
                store=session_store,
                path="/nope",
                dest=str(dest),
            )
        # No partial file is left behind.
        assert not dest.exists()

    def test_missing_path_preserves_existing_output(
        self, contree_client, session_store, tmp_path
    ):
        dest = tmp_path / "out.bin"
        dest.write_bytes(b"previous copy contents")
        with pytest.raises(NotFoundError):
            _run_cmd(
                contree_client,
                NotFoundError(404, "path not found"),
                store=session_store,
                path="/nope",
                dest=str(dest),
            )
        # A failed copy must not clobber a file that already existed.
        assert dest.read_bytes() == b"previous copy contents"
        # No leftover temp file in the destination directory.
        assert not list(tmp_path.glob(f".{dest.name}.*"))

    def test_progress_log(self, contree_client, session_store, tmp_path, caplog):
        """After 5s elapsed, a progress line with volume and speed.

        The streaming download API exposes no response headers, so the
        total size is unknown and progress carries no percent or ETA.
        """
        dest = tmp_path / "out"
        chunks = [b"A" * CHUNK_SIZE, b"A" * CHUNK_SIZE]

        # monotonic: start, after-chunk-1 (6s), after-chunk-2, final
        time_values = [0.0, 6.0, 6.1, 6.1]

        with caplog.at_level(logging.INFO, logger="contree_cli.cli.cp"):
            _run_cmd(
                contree_client,
                chunks,
                store=session_store,
                dest=str(dest),
                time_values=time_values,
            )

        progress = [r for r in caplog.records if "downloaded" in r.message]
        assert len(progress) == 1
        msg = progress[0].message
        assert "/s" in msg
        assert "ETA" not in msg
        assert "%" not in msg

    def test_final_log_shows_total(
        self, contree_client, session_store, tmp_path, caplog
    ):
        """The final summary log includes total size."""
        dest = tmp_path / "out"
        with caplog.at_level(logging.INFO, logger="contree_cli.cli.cp"):
            _run_cmd(
                contree_client, [b"hello world"], store=session_store, dest=str(dest)
            )

        written = [r for r in caplog.records if "Written" in r.message]
        assert len(written) == 1
        assert "11.0 B" in written[0].message


class TestFormatHelpers:
    def test_fmt_size_bytes(self):
        assert fmt_size(500) == "500.0 B"

    def test_fmt_size_kib(self):
        assert fmt_size(2048) == "2.0 KiB"

    def test_fmt_size_mib(self):
        assert fmt_size(5 * 1024 * 1024) == "5.0 MiB"

    def test_fmt_size_gib(self):
        assert fmt_size(3 * 1024**3) == "3.0 GiB"

    def test_fmt_size_tib(self):
        assert fmt_size(2 * 1024**4) == "2.0 TiB"
