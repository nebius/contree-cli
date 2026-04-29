from __future__ import annotations

import io
from contextvars import copy_context
from unittest.mock import patch

from conftest import ContreeTestClient, FakeResponse

from contree_cli import FORMATTER, SESSION_STORE
from contree_cli.cli.cat import CatArgs, cmd_cat
from contree_cli.output import DefaultFormatter, JSONFormatter
from contree_cli.session import SessionStore


def _run_cmd(
    tc: ContreeTestClient,
    data: bytes = b"",
    *,
    store: SessionStore,
    image: str = "a1b2c3d4-5678-9abc-def0-111111111111",
    path: str = "/etc/hosts",
    images_response: dict | None = None,
    isatty: bool = True,
    formatter=None,
):
    """Run cmd_cat with mocked responses and stdout."""
    if images_response is not None:
        tc.respond_json(images_response)
    tc.fake.responses.append(FakeResponse(body=data))

    FORMATTER.set(formatter or DefaultFormatter())
    store.set_image(image, kind="test")
    SESSION_STORE.set(store)
    ctx = copy_context()

    args = CatArgs(path=path)
    buf = io.BytesIO()
    with patch("contree_cli.cli.cat.sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = isatty
        mock_stdout.buffer = buf
        result = ctx.run(cmd_cat, args)

    return buf, result


class TestCmdCatTTY:
    def test_request_path(self, contree_client, session_store):
        _run_cmd(contree_client, b"hello", store=session_store, path="/etc/hosts")
        paths = contree_client.request_paths
        assert len(paths) == 1
        assert "/v1/inspect/a1b2c3d4-5678-9abc-def0-111111111111/download" in paths[0]
        assert "path=%2Fetc%2Fhosts" in paths[0]

    def test_outputs_content(self, contree_client, session_store):
        buf, result = _run_cmd(
            contree_client, b"file contents here", store=session_store
        )
        assert buf.getvalue() == b"file contents here"
        assert result is None

    def test_tag_resolution(self, contree_client, session_store):
        images_resp = {"images": [{"uuid": "resolved-uuid", "tag": "latest"}]}
        _run_cmd(
            contree_client,
            b"data",
            store=session_store,
            image="tag:latest",
            images_response=images_resp,
        )
        paths = contree_client.request_paths
        assert len(paths) == 2
        assert "tag=latest" in paths[0]
        assert "/v1/inspect/resolved-uuid/download" in paths[1]

    def test_binary_rejected_on_tty(self, contree_client, session_store):
        buf, result = _run_cmd(contree_client, b"\x80\x81\x82\xff", store=session_store)
        assert result == 1
        assert buf.getvalue() == b""

    def test_empty_file(self, contree_client, session_store):
        buf, result = _run_cmd(contree_client, b"", store=session_store)
        assert buf.getvalue() == b""
        assert result is None

    def test_non_default_formatter_warns(self, contree_client, session_store, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.cat"):
            _run_cmd(
                contree_client, b"hello", store=session_store, formatter=JSONFormatter()
            )
        assert any("--format is ignored" in r.message for r in caplog.records)

    def test_default_formatter_no_warning(self, contree_client, session_store, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.cat"):
            _run_cmd(
                contree_client,
                b"hello",
                store=session_store,
                formatter=DefaultFormatter(),
            )
        assert not any("--format is ignored" in r.message for r in caplog.records)


class TestCmdCatPiped:
    """When stdout is not a TTY, cat writes content directly."""

    def test_streams_content(self, contree_client, session_store):
        buf, result = _run_cmd(
            contree_client,
            b"file contents here",
            store=session_store,
            isatty=False,
        )
        assert buf.getvalue() == b"file contents here"
        assert result is None

    def test_binary_allowed(self, contree_client, session_store):
        binary = b"\x80\x81\x82\xff"
        buf, result = _run_cmd(
            contree_client, binary, store=session_store, isatty=False
        )
        assert buf.getvalue() == binary
        assert result is None

    def test_empty_file(self, contree_client, session_store):
        buf, result = _run_cmd(contree_client, b"", store=session_store, isatty=False)
        assert buf.getvalue() == b""
        assert result is None


class TestCmdCatCaching:
    def test_tty_caches_result(self, contree_client, session_store):
        """Second TTY call should not hit the API."""
        _run_cmd(contree_client, b"hello", store=session_store)
        assert contree_client.request_count == 1

        buf2, _ = _run_cmd(contree_client, b"hello", store=session_store)
        assert contree_client.request_count == 1  # no new request (cached)
        assert buf2.getvalue() == b"hello"

    def test_pipe_caches_result(self, contree_client, session_store):
        """Second piped call should not hit the API."""
        _run_cmd(contree_client, b"data", store=session_store, isatty=False)
        assert contree_client.request_count == 1

        buf2, _ = _run_cmd(contree_client, b"data", store=session_store, isatty=False)
        assert contree_client.request_count == 1  # no new request (cached)
        assert buf2.getvalue() == b"data"

    def test_cache_hit_returns_correct_data(self, contree_client, session_store):
        """Cached content should match original bytes exactly."""
        original = b"exact content\nwith newlines\n"
        _run_cmd(contree_client, original, store=session_store)

        buf, result = _run_cmd(contree_client, b"ignored", store=session_store)
        assert buf.getvalue() == original
        assert result is None
