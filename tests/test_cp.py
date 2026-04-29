from __future__ import annotations

import io
import logging
from contextlib import ExitStack
from contextvars import copy_context
from unittest.mock import patch

from conftest import ContreeTestClient

from contree_cli import FORMATTER, SESSION_STORE
from contree_cli.cli.cp import CpArgs, cmd_cp, fmt_duration, fmt_size
from contree_cli.client import CHUNK_SIZE
from contree_cli.output import DefaultFormatter, JSONFormatter
from contree_cli.session import SessionStore


class StreamResponse:
    """Response that supports chunked reading via BytesIO."""

    def __init__(
        self,
        data: bytes,
        *,
        status: int = 200,
        content_length: bool = True,
    ):
        self.status = status
        self.reason = "OK" if status < 300 else "Error"
        self._buf = io.BytesIO(data)
        self._length = len(data) if content_length else None

    def read(self, amt: int | None = None) -> bytes:
        return self._buf.read(amt)

    def getheader(self, name: str, default: str | None = None) -> str | None:
        if name == "Content-Length" and self._length is not None:
            return str(self._length)
        return default


def _run_cmd(
    tc: ContreeTestClient,
    data: bytes = b"",
    *,
    store: SessionStore,
    image: str = "a1b2c3d4-5678-9abc-def0-111111111111",
    path: str = "/etc/hosts",
    dest: str = "/tmp/out",
    images_response: dict | None = None,
    formatter=None,
    content_length: bool = True,
    time_values: list[float] | None = None,
):
    """Run cmd_cp with mocked responses."""
    if images_response is not None:
        tc.respond_json(images_response)
    tc.fake.responses.append(
        StreamResponse(data, content_length=content_length),
    )

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
    def test_request_path(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "out"
        _run_cmd(
            contree_client,
            b"hello",
            store=session_store,
            path="/etc/hosts",
            dest=str(dest),
        )
        paths = contree_client.request_paths
        assert len(paths) == 1
        assert "/v1/inspect/a1b2c3d4-5678-9abc-def0-111111111111/download" in paths[0]
        assert "path=%2Fetc%2Fhosts" in paths[0]

    def test_writes_file(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "output.bin"
        result = _run_cmd(
            contree_client, b"file contents here", store=session_store, dest=str(dest)
        )
        assert result is None
        assert dest.read_bytes() == b"file contents here"

    def test_tag_resolution(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "out"
        images_resp = {"images": [{"uuid": "resolved-uuid", "tag": "latest"}]}
        _run_cmd(
            contree_client,
            b"data",
            store=session_store,
            image="tag:latest",
            images_response=images_resp,
            dest=str(dest),
        )
        paths = contree_client.request_paths
        assert len(paths) == 2
        assert "tag=latest" in paths[0]
        assert "/v1/inspect/resolved-uuid/download" in paths[1]

    def test_empty_file(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "empty"
        result = _run_cmd(contree_client, b"", store=session_store, dest=str(dest))
        assert result is None
        assert dest.read_bytes() == b""

    def test_non_default_formatter_warns(
        self, contree_client, session_store, tmp_path, caplog
    ):
        dest = tmp_path / "out"
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.cp"):
            _run_cmd(
                contree_client,
                b"hello",
                store=session_store,
                formatter=JSONFormatter(),
                dest=str(dest),
            )
        assert any("--format is ignored" in r.message for r in caplog.records)

    def test_overwrites_existing(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "existing.txt"
        dest.write_bytes(b"old content")
        result = _run_cmd(
            contree_client, b"new content", store=session_store, dest=str(dest)
        )
        assert result is None
        assert dest.read_bytes() == b"new content"

    def test_progress_log_with_content_length(
        self, contree_client, session_store, tmp_path, caplog
    ):
        """After 5s elapsed, a progress line with %, ETA, and speed."""
        dest = tmp_path / "out"
        data = b"A" * CHUNK_SIZE * 2

        # monotonic: start, after-chunk-1 (6s), after-chunk-2, final
        time_values = [0.0, 6.0, 6.1, 6.1]

        with caplog.at_level(logging.INFO, logger="contree_cli.cli.cp"):
            _run_cmd(
                contree_client,
                data,
                store=session_store,
                dest=str(dest),
                time_values=time_values,
            )

        progress = [r for r in caplog.records if "downloaded" in r.message]
        assert len(progress) == 1
        msg = progress[0].message
        assert "50%" in msg
        assert "ETA" in msg
        assert "/s" in msg

    def test_progress_log_without_content_length(
        self, contree_client, session_store, tmp_path, caplog
    ):
        """Without Content-Length, progress shows size and speed but no ETA."""
        dest = tmp_path / "out"
        data = b"B" * CHUNK_SIZE * 2

        time_values = [0.0, 6.0, 6.1, 6.1]

        with caplog.at_level(logging.INFO, logger="contree_cli.cli.cp"):
            _run_cmd(
                contree_client,
                data,
                store=session_store,
                dest=str(dest),
                time_values=time_values,
                content_length=False,
            )

        progress = [r for r in caplog.records if "downloaded" in r.message]
        assert len(progress) == 1
        msg = progress[0].message
        assert "/s" in msg
        assert "ETA" not in msg

    def test_final_log_shows_total(
        self, contree_client, session_store, tmp_path, caplog
    ):
        """The final summary log includes total size."""
        dest = tmp_path / "out"
        with caplog.at_level(logging.INFO, logger="contree_cli.cli.cp"):
            _run_cmd(
                contree_client, b"hello world", store=session_store, dest=str(dest)
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

    def test_fmt_duration_seconds(self):
        assert fmt_duration(45) == "45s"

    def test_fmt_duration_minutes(self):
        assert fmt_duration(125) == "2m05s"

    def test_fmt_duration_hours(self):
        assert fmt_duration(3723) == "1h02m03s"
