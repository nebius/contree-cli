from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
from importlib.metadata import PackageNotFoundError
from unittest.mock import MagicMock, patch

import pytest
from conftest import ContreeTestClient, ContreeTestIAMClient, FakeResponse

from contree_cli.client import (
    CLI_USER_AGENT,
    RETRY_DELAYS,
    ApiError,
    BodyFormatter,
    ContreeClient,
    ContreeJWTClient,
    HeaderFormatter,
    PaginatedFetcher,
    cli_version,
    decode_event_chunk,
    iter_sse_events,
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

    def test_retry_on_network_error_then_succeeds(self):
        """A transient gaierror is retried like a 5xx response."""
        import socket

        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=200, body=b'{"ok":true}')

        call_count = {"n": 0}
        real_connect = c._connect

        def flaky_connect():
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise socket.gaierror(8, "nodename nor servname provided")
            return real_connect()

        c._connect = flaky_connect  # type: ignore[method-assign]

        with patch("contree_cli.client.time.sleep") as mock_sleep:
            result = c.request("GET", "/v1/images")

        assert result.body == b'{"ok":true}'
        assert call_count["n"] == 3
        assert mock_sleep.call_count == 2

    def test_retry_exhausted_raises_network_error(self):
        """When retries run out, the last network error propagates."""
        import socket

        c = ContreeTestClient("https://contree.dev", "tok")

        def always_fails():
            raise socket.gaierror(8, "nodename nor servname provided")

        c._connect = always_fails  # type: ignore[method-assign]

        with (
            patch("contree_cli.client.time.sleep"),
            pytest.raises(socket.gaierror),
        ):
            c.request("GET", "/v1/images")

    def test_first_attempt_410_uses_short_delay(self):
        """410 on the very first attempt sleeps `RETRY_DELAYS[0]`, not
        the wraparound `RETRY_DELAYS[-1]` from the old `attempt-1` index."""
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=410, body=b"gone")
        c.respond(status=200, body=b'{"ok":true}')

        with patch("contree_cli.client.time.sleep") as mock_sleep:
            c.request("GET", "/v1/images")
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays[0] == RETRY_DELAYS[0]

    def test_first_attempt_425_uses_short_delay(self):
        c = ContreeTestClient("https://contree.dev", "tok")
        c.respond(status=425, body=b"too early")
        c.respond(status=200, body=b'{"ok":true}')

        with patch("contree_cli.client.time.sleep") as mock_sleep:
            c.request("GET", "/v1/images")
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays[0] == RETRY_DELAYS[0]

    def test_invalid_url_is_not_retried(self):
        """InvalidURL is a permanent caller-side error — should raise immediately."""
        import http.client

        c = ContreeTestClient("https://contree.dev", "tok")

        call_count = {"n": 0}

        def fail_with_invalid_url():
            call_count["n"] += 1
            raise http.client.InvalidURL("control characters in URL")

        c._connect = fail_with_invalid_url  # type: ignore[method-assign]

        with (
            patch("contree_cli.client.time.sleep") as mock_sleep,
            pytest.raises(http.client.InvalidURL),
        ):
            c.request("GET", "/v1/images")
        assert call_count["n"] == 1
        mock_sleep.assert_not_called()


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
        assert sent == b"a" * 1024

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

    def test_request_headers_logged_with_authorization_redacted(self, caplog):
        self._enable_debug(caplog)
        c = ContreeTestClient("https://contree.dev", "secret-token")
        c.respond(status=200, body=b"{}")
        c.request("GET", "/v1/images")
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "headers=" in msgs
        assert "secret-token" not in msgs
        assert "<redacted>" in msgs

    def test_response_headers_logged(self, caplog):
        self._enable_debug(caplog)
        c = ContreeTestClient("https://contree.dev", "tok")
        c.fake.responses.append(
            FakeResponse(
                status=200,
                body=b"{}",
                headers={"Content-Type": "application/json", "X-Trace-Id": "abc"},
            )
        )
        c.request("GET", "/v1/images")
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "X-Trace-Id" in msgs
        assert "abc" in msgs

    def test_error_response_headers_logged(self, caplog):
        self._enable_debug(caplog)
        c = ContreeTestClient("https://contree.dev", "tok")
        c.fake.responses.append(
            FakeResponse(
                status=400,
                body=b"bad",
                headers={"X-Trace-Id": "trace-err"},
            )
        )
        with pytest.raises(ApiError):
            c.request("GET", "/v1/images")
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "trace-err" in msgs


class TestHeaderFormatter:
    def test_redacts_authorization(self):
        out = str(HeaderFormatter({"Authorization": "Bearer secret", "X-Foo": "bar"}))
        assert "secret" not in out
        assert "<redacted>" in out
        assert "bar" in out

    def test_redaction_is_case_insensitive(self):
        out = str(HeaderFormatter({"AUTHORIZATION": "Bearer secret"}))
        assert "secret" not in out
        assert "<redacted>" in out

    def test_accepts_list_of_tuples(self):
        out = str(
            HeaderFormatter(
                [("Authorization", "Bearer secret"), ("X-Trace-Id", "abc")],
            )
        )
        assert "secret" not in out
        assert "abc" in out

    def test_redacts_cookie(self):
        out = str(HeaderFormatter({"Cookie": "session=xyz"}))
        assert "xyz" not in out

    def test_non_sensitive_passes_through(self):
        out = str(HeaderFormatter({"User-Agent": "ua/1.0", "Project": "proj"}))
        assert "ua/1.0" in out
        assert "proj" in out


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


class TestCliVersion:
    """``cli_version()`` must return ``"editable"`` whenever the install is
    not a regular wheel — either the package is missing from metadata, or
    PEP 610 ``direct_url.json`` marks the install as editable. The update
    checker keys off this sentinel to skip PyPI pings during local dev."""

    def make_dist(self, *, version: str, direct_url: str | None) -> MagicMock:
        dist = MagicMock()
        dist.version = version
        dist.read_text.return_value = direct_url
        return dist

    def test_returns_editable_when_package_not_installed(self):
        with patch(
            "contree_cli.client.distribution",
            side_effect=PackageNotFoundError("contree-cli"),
        ):
            assert cli_version() == "editable"

    def test_returns_editable_for_pep610_editable_install(self):
        dist = self.make_dist(
            version="0.5.0",
            direct_url=json.dumps(
                {
                    "url": "file:///path/to/contree-cli",
                    "dir_info": {"editable": True},
                },
            ),
        )
        with patch("contree_cli.client.distribution", return_value=dist):
            assert cli_version() == "editable"

    def test_returns_version_for_regular_install(self):
        dist = self.make_dist(version="0.5.0", direct_url=None)
        with patch("contree_cli.client.distribution", return_value=dist):
            assert cli_version() == "0.5.0"

    def test_returns_version_when_direct_url_lacks_editable_flag(self):
        dist = self.make_dist(
            version="0.5.0",
            direct_url=json.dumps(
                {
                    "url": "https://files.pythonhosted.org/.../contree_cli.whl",
                    "archive_info": {},
                },
            ),
        )
        with patch("contree_cli.client.distribution", return_value=dist):
            assert cli_version() == "0.5.0"

    def test_returns_version_when_direct_url_is_malformed(self):
        dist = self.make_dist(version="0.5.0", direct_url="not json {")
        with patch("contree_cli.client.distribution", return_value=dist):
            assert cli_version() == "0.5.0"


class TestPaginatedFetcherLimit:
    """``limit=`` lives in :class:`PaginatedFetcher` so callers don't repeat
    the page-size math, and a small budget like ``--limit 5`` doesn't pull a
    full 1000-row page just to discard 995."""

    def make_fetcher(
        self,
        client: ContreeTestClient,
        *,
        limit: int | None,
        page_size: int | None = None,
    ) -> PaginatedFetcher:
        return PaginatedFetcher(
            client,
            "/v1/things",
            {},
            lambda body: json.loads(body)["items"],
            limit=limit,
            page_size=page_size,
            concurrency=1,
        )

    def test_small_limit_caps_page_size(self, contree_client):
        f = self.make_fetcher(contree_client, limit=5)
        # +1 so callers can detect "more exists past the limit".
        assert f.page_size == 6
        # Need to cover at most limit+1 records; +1 page of safety pad
        # plus the +1 ceiling makes the math straightforward.
        assert f.max_pages >= 2

    def test_large_limit_uses_default_page_size(self, contree_client):
        f = self.make_fetcher(contree_client, limit=10000)
        assert f.page_size == PaginatedFetcher.DEFAULT_PAGE_SIZE
        assert f.max_pages >= 10000 // PaginatedFetcher.DEFAULT_PAGE_SIZE

    def test_no_limit_uses_default_and_safety_cap(self, contree_client):
        f = self.make_fetcher(contree_client, limit=None)
        assert f.page_size == PaginatedFetcher.DEFAULT_PAGE_SIZE
        assert f.max_pages == PaginatedFetcher.UNLIMITED_MAX_PAGES

    def test_explicit_page_size_still_capped_by_limit(self, contree_client):
        # Caller-supplied page_size is an upper bound; limit can still
        # squash it smaller.
        f = self.make_fetcher(contree_client, limit=3, page_size=100)
        assert f.page_size == 4

    def test_small_limit_uses_capped_page_size_in_request(self, contree_client):
        # `limit=5` must request `limit=6` (capped page size + 1 for the
        # truncation probe), not the default 1000.
        contree_client.respond_json({"items": [{"i": i} for i in range(6)]})
        with self.make_fetcher(contree_client, limit=5) as f:
            pages_iter = iter(f)
            first = next(pages_iter)
            assert len(first) == 6
            # Context manager exit calls stop() automatically; mirrors the
            # real caller which breaks out of the loop after hitting limit.
        with contextlib.suppress(StopIteration):
            next(pages_iter)
        req = contree_client.get_request(0)
        assert "limit=6" in req.path
        assert "limit=1000" not in req.path


# ---------------------------------------------------------------------------
# SSE parser: iter_sse_events
# ---------------------------------------------------------------------------


def _sse(body: str) -> io.BytesIO:
    return io.BytesIO(body.encode("utf-8"))


class TestIterSseEvents:
    def test_single_frame(self):
        body = (
            "id: 1\nevent: stdout\n"
            'data: {"type":"stdout","data":{"value":"hi","encoding":"ascii"}}\n\n'
        )
        events = list(iter_sse_events(_sse(body)))
        assert events == [
            {"type": "stdout", "data": {"value": "hi", "encoding": "ascii"}}
        ]

    def test_multiple_frames(self):
        body = (
            'id: 0\nevent: init\ndata: {"type":"init","data":{}}\n\n'
            'id: 1\nevent: exit\ndata: {"type":"exit","spid":1,"data":{"code":0}}\n\n'
        )
        events = list(iter_sse_events(_sse(body)))
        assert [e["type"] for e in events] == ["init", "exit"]
        assert events[1]["spid"] == 1

    def test_keepalive_and_blank_lines_ignored(self):
        body = (
            ": keepalive\n"
            "\n"
            ": keepalive again\n"
            'id: 5\nevent: stdout\ndata: {"type":"stdout"}\n\n'
        )
        events = list(iter_sse_events(_sse(body)))
        assert events == [{"type": "stdout"}]

    def test_sse_error_frame_surfaces_as_dict(self):
        body = (
            ": stream ended with error, retry since last event id\n\n"
            "event: sse_error\ndata: upstream closed unexpectedly\n\n"
        )
        events = list(iter_sse_events(_sse(body)))
        assert events == [
            {"type": "sse_error", "message": "upstream closed unexpectedly"}
        ]

    def test_sse_error_with_id_carries_id(self):
        body = "id: 42\nevent: sse_error\ndata: boom\n\n"
        events = list(iter_sse_events(_sse(body)))
        assert events == [{"type": "sse_error", "message": "boom", "id": "42"}]

    def test_multiline_data_joined_with_newline(self):
        body = 'event: stdout\ndata: {"type":"stdout",\ndata: "value":"x"}\n\n'
        events = list(iter_sse_events(_sse(body)))
        assert events == [{"type": "stdout", "value": "x"}]

    def test_invalid_json_data_dropped_silently(self):
        body = 'event: stdout\ndata: not-json\n\nevent: exit\ndata: {"type":"exit"}\n\n'
        events = list(iter_sse_events(_sse(body)))
        assert events == [{"type": "exit"}]

    def test_non_dict_json_dropped(self):
        body = 'event: stdout\ndata: [1,2,3]\n\nevent: exit\ndata: {"type":"exit"}\n\n'
        events = list(iter_sse_events(_sse(body)))
        assert events == [{"type": "exit"}]

    def test_frame_at_eof_without_blank_line_still_emits(self):
        body = 'event: stdout\ndata: {"type":"stdout"}\n'
        events = list(iter_sse_events(_sse(body)))
        assert events == [{"type": "stdout"}]

    def test_empty_stream_yields_nothing(self):
        events = list(iter_sse_events(_sse("")))
        assert events == []


# ---------------------------------------------------------------------------
# SSE chunk decoder: decode_event_chunk
# ---------------------------------------------------------------------------


class TestDecodeEventChunk:
    def test_ascii_encoding_default(self):
        assert decode_event_chunk({"value": "hello"}) == b"hello"

    def test_explicit_ascii(self):
        assert decode_event_chunk({"value": "hi", "encoding": "ascii"}) == b"hi"

    def test_base64_encoding(self):
        payload = base64.b64encode(b"\x00\x01\xff").decode("ascii")
        assert (
            decode_event_chunk({"value": payload, "encoding": "base64"})
            == b"\x00\x01\xff"
        )

    def test_base64_invalid_returns_empty(self):
        assert (
            decode_event_chunk({"value": "!!!not-base64!!!", "encoding": "base64"})
            == b""
        )

    def test_missing_value_returns_empty(self):
        assert decode_event_chunk({}) == b""

    def test_empty_value_returns_empty(self):
        assert decode_event_chunk({"value": ""}) == b""

    def test_non_dict_returns_empty(self):
        assert decode_event_chunk("not a dict") == b""
        assert decode_event_chunk(None) == b""
        assert decode_event_chunk(42) == b""

    def test_non_string_value_returns_empty(self):
        assert decode_event_chunk({"value": 123}) == b""
