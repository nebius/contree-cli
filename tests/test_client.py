from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest
from conftest import ContreeTestClient, ContreeTestIAMClient, FakeResponse

from contree_cli.client import (
    CLI_USER_AGENT,
    RETRY_DELAYS,
    ApiError,
    BodyFormatter,
    ContreeClient,
    ContreeJWTClient,
    resolve_image,
)

# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


class TestUrlParsing:
    def test_https_default(self):
        c = ContreeJWTClient("https://contree.dev", "tok")
        assert c._scheme == "https"
        assert c._host == "contree.dev"
        assert c._port is None
        assert c._prefix == ""

    def test_http_scheme(self):
        c = ContreeJWTClient("http://localhost:8080", "tok")
        assert c._scheme == "http"
        assert c._host == "localhost"
        assert c._port == 8080

    def test_path_prefix_stripped(self):
        c = ContreeJWTClient("https://contree.dev/api/", "tok")
        assert c._prefix == "/api"

    def test_bare_host_defaults_https(self):
        c = ContreeJWTClient("https://example.com", "tok")
        assert c._scheme == "https"


# ---------------------------------------------------------------------------
# Connection type
# ---------------------------------------------------------------------------


class TestConnect:
    def test_https_creates_https_connection(self):
        c = ContreeJWTClient("https://contree.dev", "tok")
        conn = c._connect()
        import http.client

        assert isinstance(conn, http.client.HTTPSConnection)

    def test_http_creates_http_connection(self):
        c = ContreeJWTClient("http://localhost", "tok")
        conn = c._connect()
        import http.client

        assert isinstance(conn, http.client.HTTPConnection)


# ---------------------------------------------------------------------------
# ApiError
# ---------------------------------------------------------------------------


class TestApiError:
    def test_str(self):
        e = ApiError(404, "Not Found", '{"error":"gone"}')
        assert str(e) == 'API 404 Not Found: {"error":"gone"}'

    def test_attributes(self):
        e = ApiError(500, "Internal Server Error", "oops")
        assert e.status == 500
        assert e.reason == "Internal Server Error"
        assert e.body == "oops"


# ---------------------------------------------------------------------------
# request()
# ---------------------------------------------------------------------------


class TestRequest:
    def test_sets_user_agent(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=200, body=b"{}")
        c.request("GET", "/v1/images")
        headers = c.get_request(-1).headers
        assert headers["User-Agent"] == CLI_USER_AGENT

    def test_prepends_prefix(self):
        c = ContreeTestClient("https://contree.dev/api", "tok")
        c.respond(status=200, body=b"{}")
        c.request("GET", "/v1/images")
        path = c.get_request(-1).path
        assert path == "/api/v1/images"

    def test_raises_on_non_2xx(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=403, body=b"nope")
        with pytest.raises(ApiError) as exc_info:
            c.request("GET", "/v1/images")
        assert exc_info.value.status == 403
        assert exc_info.value.body == "nope"

    def test_returns_response_on_success(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=200, body=b'{"ok":true}')
        result = c.request("GET", "/v1/images")
        assert isinstance(result, FakeResponse)
        assert result.body == b'{"ok":true}'


# ---------------------------------------------------------------------------
# Retry on 5xx
# ---------------------------------------------------------------------------


class TestRetry:
    def test_retries_on_5xx_then_succeeds(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=502, body=b"down")
        c.respond(status=200, body=b'{"ok":true}')

        with patch("contree_cli.client.time.sleep") as mock_sleep:
            result = c.request("GET", "/v1/images")

        assert isinstance(result, FakeResponse)
        assert result.body == b'{"ok":true}'
        mock_sleep.assert_called_once_with(RETRY_DELAYS[0])

    def test_exhausts_retries_then_raises(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        total = len(RETRY_DELAYS) + 1
        for _ in range(total):
            c.respond(status=500, body=b"err")

        with (
            patch("contree_cli.client.time.sleep") as mock_sleep,
            pytest.raises(ApiError) as exc_info,
        ):
            c.request("GET", "/v1/images")

        assert exc_info.value.status == 500
        assert mock_sleep.call_count == len(RETRY_DELAYS)

    def test_no_retry_on_4xx(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=404, body=b"nope")

        with (
            patch("contree_cli.client.time.sleep") as mock_sleep,
            pytest.raises(ApiError) as exc_info,
        ):
            c.request("GET", "/v1/images")

        assert exc_info.value.status == 404
        mock_sleep.assert_not_called()

    def test_retry_recovers_midway(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        for _ in range(3):
            c.respond(status=503, body=b"down")
        c.respond(status=200, body=b'{"ok":true}')

        with patch("contree_cli.client.time.sleep") as mock_sleep:
            result = c.request("GET", "/v1/images")

        assert isinstance(result, FakeResponse)
        assert result.body == b'{"ok":true}'
        assert mock_sleep.call_count == 3
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays == list(RETRY_DELAYS[:3])


# ---------------------------------------------------------------------------
# Convenience methods
# ---------------------------------------------------------------------------


class TestGet:
    def test_without_params(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=200, body=b"{}")
        c.get("/v1/images")
        path = c.get_request(-1).path
        assert path == "/v1/images"

    def test_with_params(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=200, body=b"{}")
        c.get("/v1/images", params={"prefix": "ubuntu"})
        path = c.get_request(-1).path
        assert path == "/v1/images?prefix=ubuntu"


class TestPostJson:
    def test_sends_json_body(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=201, body=b"{}")
        c.post_json("/v1/instances", {"image": "ubuntu"})
        req = c.get_request(-1)
        assert req.method == "POST"
        body = json.loads(req.body)
        assert body == {"image": "ubuntu"}
        assert req.headers["Content-Type"] == "application/json"


class TestPatchJson:
    def test_sends_patch(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=200, body=b"{}")
        c.patch_json("/v1/images/abc/tag", {"tag": "latest"})
        req = c.get_request(-1)
        assert req.method == "PATCH"
        body = json.loads(req.body)
        assert body == {"tag": "latest"}


class TestDelete:
    def test_sends_delete(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=200, body=b"{}")
        c.delete("/v1/operations/abc")
        req = c.get_request(-1)
        assert req.method == "DELETE"
        assert req.path == "/v1/operations/abc"


# ---------------------------------------------------------------------------
# resolve_image()
# ---------------------------------------------------------------------------


class TestResolveImage:
    def test_uuid_passthrough(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        uuid = "a1b2c3d4-5678-9abc-def0-111111111111"
        assert resolve_image(c, uuid) == uuid

    def test_tag_resolution(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        body = json.dumps({"images": [{"uuid": "resolved-uuid", "tag": "latest"}]})
        c.respond(status=200, body=body.encode())
        result = resolve_image(c, "tag:latest")
        assert result == "resolved-uuid"

    def test_tag_not_found(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        body = json.dumps({"images": []})
        c.respond(status=200, body=body.encode())
        with pytest.raises(ApiError) as exc_info:
            resolve_image(c, "tag:nonexistent")
        assert exc_info.value.status == 404

    def test_tag_queries_images_endpoint(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        body = json.dumps({"images": [{"uuid": "u1", "tag": "mytag"}]})
        c.respond(status=200, body=body.encode())
        resolve_image(c, "tag:mytag")
        path = c.get_request(-1).path
        assert "/v1/images" in path
        assert "tag=mytag" in path

    def test_bare_tag_resolves(self):
        """Non-UUID bare ref is resolved as a tag name."""
        c = ContreeTestClient("https://contree.dev", "tok")
        body = json.dumps({"images": [{"uuid": "u2", "tag": "common/py"}]})
        c.respond(status=200, body=body.encode())
        result = resolve_image(c, "common/py")
        assert result == "u2"

    def test_bare_tag_not_found(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        body = json.dumps({"images": []})
        c.respond(status=200, body=body.encode())
        with pytest.raises(ApiError) as exc_info:
            resolve_image(c, "no-such-tag")
        assert exc_info.value.status == 404


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class ContreeTestClientABC:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            ContreeClient("https://example.com", "tok")  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Streaming body
# ---------------------------------------------------------------------------


class TestStreamingBody:
    def test_passes_file_object_to_connection(self):
        import io

        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=201, body=b'{"uuid":"u1"}')
        stream = io.BytesIO(b"a" * 1024)
        c.request(
            "POST",
            "/v1/files",
            body=stream,
            headers={"Content-Type": "application/octet-stream"},
        )
        sent = c.get_request(-1).body
        assert sent is stream

    def test_retry_seeks_back_to_start(self):
        import io

        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=503, body=b"down")
        c.respond(status=201, body=b'{"uuid":"u1"}')

        seeks: list[int] = []

        class TrackingStream(io.BytesIO):
            def seek(self, pos, whence=0):  # type: ignore[override]
                seeks.append(pos)
                return super().seek(pos, whence)

            def seekable(self) -> bool:  # type: ignore[override]
                return True

        stream = TrackingStream(b"payload")

        with patch("contree_cli.client.time.sleep"):
            c.request("POST", "/v1/files", body=stream)

        assert seeks == [0]

    def test_retry_unseekable_stream_raises(self):
        import io

        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=500, body=b"down")

        class Unseekable(io.BytesIO):
            def seekable(self) -> bool:  # type: ignore[override]
                return False

        with patch("contree_cli.client.time.sleep"), pytest.raises(ApiError) as ei:
            c.request("POST", "/v1/files", body=Unseekable(b"x"))

        assert ei.value.reason == "RetryNotSeekable"


# ---------------------------------------------------------------------------
# IAM client
# ---------------------------------------------------------------------------


class TestDebugLogging:
    def _enable_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="contree_cli.client")

    def test_logs_request_body_when_debug(self, caplog):
        self._enable_debug(caplog)
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=201, body=b'{"uuid":"x"}')
        c.post_json("/v1/instances", {"image": "ubuntu", "command": "uname -a"})
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert '"image": "ubuntu"' in msgs
        assert '"command": "uname -a"' in msgs

    def test_logs_error_response_body_when_debug(self, caplog):
        self._enable_debug(caplog)
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=400, body=b'{"error":"bad payload"}')
        with pytest.raises(ApiError):
            c.post_json("/v1/instances", {"image": "ubuntu"})
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert '"error":"bad payload"' in msgs

    def test_no_body_logs_when_not_debug(self, caplog):
        caplog.set_level(logging.INFO, logger="contree_cli.client")
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=200, body=b'{"a":1}')
        c.post_json("/v1/instances", {"x": 1})
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "request body" not in msgs

    def test_octet_stream_request_body_not_dumped(self, caplog):
        self._enable_debug(caplog)
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=201, body=b"{}")
        binary = bytes(range(256))
        c.request(
            "POST",
            "/v1/files",
            body=binary,
            headers={"Content-Type": "application/octet-stream"},
        )
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "<binary" in msgs
        assert "Content-Type='application/octet-stream'" in msgs


class TestBodyFormatter:
    def test_none(self):
        assert str(BodyFormatter(None)) == "<none>"

    def test_empty(self):
        assert str(BodyFormatter(b"")) == "<empty>"

    def test_text_body(self):
        assert str(BodyFormatter(b'{"hi":1}')) == '{"hi":1}'

    def test_truncation(self):
        body = b"a" * 5000
        out = str(BodyFormatter(body, binary_max_size=100))
        assert out.startswith("a" * 100)
        assert "5000B total" in out

    def test_binary_content_type(self):
        out = str(
            BodyFormatter(
                b"\xff\xfe\x00",
                content_type="application/octet-stream",
            )
        )
        assert "<binary 3B" in out
        assert "application/octet-stream" in out

    def test_undecodable_bytes(self):
        out = str(BodyFormatter(b"\xff\xfe\x00"))
        assert out == "<binary 3B>"

    def test_lazy_str_only_called_at_format(self):
        """BodyFormatter must defer its work until __str__ is invoked."""
        calls: list[int] = []

        class Counting(BodyFormatter):
            def __str__(self) -> str:
                calls.append(1)
                return super().__str__()

        # Logging at a level above DEBUG must not call __str__.
        logger = logging.getLogger("contree_cli.client")
        prev = logger.level
        logger.setLevel(logging.WARNING)
        try:
            logger.debug("body=%s", Counting(b"hello"))
        finally:
            logger.setLevel(prev)
        assert calls == []


class TestContreeIAMClient:
    def test_injects_project_header(self):
        c = ContreeTestIAMClient("https://example.com", "tok", "aiproject-test")
        c.respond(status=200, body=b"{}")
        c.request("GET", "/v1/images")
        headers = c.get_request(-1).headers
        assert headers["Project"] == "aiproject-test"
        assert headers["Authorization"] == "Bearer tok"

    def test_raises_without_project(self):
        c = ContreeTestIAMClient("https://example.com", "tok", None)
        with pytest.raises(ApiError, match="No project"):
            c.request("GET", "/v1/images")

    def test_raises_without_token(self):
        c = ContreeTestIAMClient("https://example.com", None, "aiproject-x")
        with pytest.raises(ApiError, match="No token"):
            c.request("GET", "/v1/images")
