from __future__ import annotations

import gzip
import io
from contextvars import copy_context
from unittest.mock import patch

from conftest import ContreeTestClient
from contree_client.exceptions import NotFoundError

from contree_cli import FORMATTER, SESSION_STORE
from contree_cli.cli.export import ExportArgs, cmd_export
from contree_cli.output import DefaultFormatter, JSONFormatter
from contree_cli.session import SessionStore

IMG_UUID = "a1b2c3d4-5678-9abc-def0-111111111111"


def gzip_chunk(data: bytes) -> bytes:
    return gzip.compress(data)


def _run_cmd(
    tc: ContreeTestClient,
    chunks: list[bytes] | object | None = None,
    *,
    store: SessionStore,
    image: str = IMG_UUID,
    path: str | None = "/etc",
    output: str = "",
    decompress: bool = False,
    formatter=None,
    stdout=None,
):
    """Run cmd_export with mocked archive responses."""
    if isinstance(chunks, BaseException):
        tc.mock("inspect_image_archive", error=chunks)
    else:
        tc.mock("inspect_image_archive", chunks or [])

    FORMATTER.set(formatter or DefaultFormatter())
    store.set_image(image, kind="test")
    SESSION_STORE.set(store)
    ctx = copy_context()

    ns_path = path if path is not None else None
    args = ExportArgs(
        path=ns_path if ns_path is not None else "/",
        output=output,
        decompress=decompress,
    )
    if stdout is None:
        stdout = _piped_stdout()
    with patch("contree_cli.cli.export.sys.stdout", stdout):
        return ctx.run(cmd_export, args)


def _piped_stdout(isatty: bool = False):
    from unittest.mock import MagicMock

    mock = MagicMock()
    mock.isatty.return_value = isatty
    mock.buffer = io.BytesIO()
    return mock


class TestCmdExport:
    def test_streams_to_file(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "etc.tar.gz"
        payload = gzip_chunk(b"tar-bytes")
        rc = _run_cmd(
            contree_client,
            [payload],
            store=session_store,
            path="/etc",
            output=str(dest),
        )
        assert rc is None
        assert dest.read_bytes() == payload
        calls = contree_client.calls_for("inspect_image_archive")
        assert len(calls) == 1
        assert calls[0].args == (IMG_UUID, "/etc")
        assert calls[0].kwargs["compressed"] is True

    def test_default_path_is_root(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "rootfs.tar.gz"
        _run_cmd(
            contree_client,
            [gzip_chunk(b"x")],
            store=session_store,
            path="/",
            output=str(dest),
        )
        calls = contree_client.calls_for("inspect_image_archive")
        assert calls[0].args == (IMG_UUID, "/")

    def test_relative_path_resolves_against_cwd(
        self, contree_client, session_store, tmp_path
    ):
        session_store.set_image(IMG_UUID, kind="test")
        session_store.set_cwd("/srv")
        dest = tmp_path / "out.tar.gz"
        _run_cmd(
            contree_client,
            [gzip_chunk(b"x")],
            store=session_store,
            path="app",
            output=str(dest),
        )
        calls = contree_client.calls_for("inspect_image_archive")
        assert calls[0].args == (IMG_UUID, "/srv/app")

    def test_stdout_pipe(self, contree_client, session_store):
        payload = gzip_chunk(b"tar-bytes")
        stdout = _piped_stdout()
        rc = _run_cmd(
            contree_client,
            [payload],
            store=session_store,
            stdout=stdout,
        )
        assert rc is None
        assert stdout.buffer.getvalue() == payload

    def test_tty_stdout_refused_with_hint(self, contree_client, session_store, caplog):
        rc = _run_cmd(
            contree_client,
            [gzip_chunk(b"x")],
            store=session_store,
            stdout=_piped_stdout(isatty=True),
        )
        assert rc == 1
        assert "binary stream" in caplog.text
        assert "-F rootfs.tar.gz" in caplog.text
        assert "tar -tzf -" in caplog.text
        # Refused before any API call.
        assert contree_client.calls_for("inspect_image_archive") == []

    def test_default_output_is_the_served_stream(
        self, contree_client, session_store, tmp_path
    ):
        """The default output is byte-identical to what the server
        serves with compressed=True; the CLI never re-encodes it."""
        dest = tmp_path / "out.tar.gz"
        _run_cmd(
            contree_client,
            [b"served-", b"bytes"],
            store=session_store,
            output=str(dest),
        )
        assert dest.read_bytes() == b"served-bytes"

    def test_decompress_flag(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "out"
        rc = _run_cmd(
            contree_client,
            [b"tar-", b"bytes"],
            store=session_store,
            output=str(dest),
            decompress=True,
        )
        assert rc is None
        assert dest.read_bytes() == b"tar-bytes"
        calls = contree_client.calls_for("inspect_image_archive")
        assert calls[0].kwargs["compressed"] is False

    def test_tar_extension_implies_decompress(
        self, contree_client, session_store, tmp_path
    ):
        dest = tmp_path / "out.tar"
        _run_cmd(
            contree_client,
            [b"tar-bytes"],
            store=session_store,
            output=str(dest),
        )
        assert dest.read_bytes() == b"tar-bytes"
        calls = contree_client.calls_for("inspect_image_archive")
        assert calls[0].kwargs["compressed"] is False

    def test_tar_gz_extension_stays_gzip(self, contree_client, session_store, tmp_path):
        dest = tmp_path / "out.tar.gz"
        payload = gzip_chunk(b"x")
        _run_cmd(contree_client, [payload], store=session_store, output=str(dest))
        calls = contree_client.calls_for("inspect_image_archive")
        assert calls[0].kwargs["compressed"] is True
        assert dest.read_bytes() == payload

    def test_tag_image_resolves(self, contree_client, session_store, tmp_path):
        contree_client.mock("inspect_find_image_by_tag", IMG_UUID)
        dest = tmp_path / "out.tar.gz"
        _run_cmd(
            contree_client,
            [gzip_chunk(b"x")],
            store=session_store,
            image="tag:alpine:latest",
            output=str(dest),
        )
        resolve_calls = contree_client.calls_for("inspect_find_image_by_tag")
        assert resolve_calls[0].args == ("alpine:latest",)
        calls = contree_client.calls_for("inspect_image_archive")
        assert calls[0].args == (IMG_UUID, "/etc")

    def test_missing_path_fails(self, contree_client, session_store, tmp_path, caplog):
        dest = tmp_path / "out.tar.gz"
        rc = _run_cmd(
            contree_client,
            NotFoundError(404, "path not found"),
            store=session_store,
            path="/nope",
            output=str(dest),
        )
        assert rc == 1
        assert "not found in image" in caplog.text
        # No partial file is left behind.
        assert not dest.exists()

    def test_missing_path_preserves_existing_output(
        self, contree_client, session_store, tmp_path, caplog
    ):
        dest = tmp_path / "out.tar.gz"
        dest.write_bytes(b"previous export contents")
        rc = _run_cmd(
            contree_client,
            NotFoundError(404, "path not found"),
            store=session_store,
            path="/nope",
            output=str(dest),
        )
        assert rc == 1
        # The failed export must not clobber a file that already existed.
        assert dest.read_bytes() == b"previous export contents"
        # No leftover temp file in the destination directory.
        assert not list(tmp_path.glob(f".{dest.name}.*"))

    def test_format_warning(self, contree_client, session_store, tmp_path, caplog):
        dest = tmp_path / "out.tar.gz"
        _run_cmd(
            contree_client,
            [gzip_chunk(b"x")],
            store=session_store,
            output=str(dest),
            formatter=JSONFormatter(),
        )
        assert "--format is ignored" in caplog.text
