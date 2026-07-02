from __future__ import annotations

import base64
import binascii
import collections
import contextlib
import http.client
import io
import json
import logging
import platform
import socket
import sys
import threading
import time
import zlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from importlib.metadata import PackageNotFoundError, distribution
from typing import IO, Any, cast
from urllib.parse import urlencode, urlsplit

from contree_cli.config import AuthType, ConfigProfile

log = logging.getLogger(__name__)

RETRY_DELAYS = (1, 2, 4, 5, 10, 10, 10)

# Socket-level / connection-level errors that warrant a retry. DNS hiccups
# (gaierror), refused/reset connections, and broken HTTP framing are all
# transient — the server may come back. TimeoutError already retried below.
RETRYABLE_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    socket.gaierror,
    ConnectionError,
    http.client.HTTPException,
    OSError,
)


def cli_version() -> str:
    try:
        dist = distribution("contree-cli")
    except PackageNotFoundError:
        return "editable"
    raw = dist.read_text("direct_url.json")
    if raw:
        try:
            if json.loads(raw).get("dir_info", {}).get("editable"):
                return "editable"
        except ValueError:
            pass
    return dist.version


CLI_USER_AGENT = (
    f"contree-cli/{cli_version()} "
    f"Python/{'.'.join(map(str, sys.version_info))} "
    f"{platform.platform()} "
)


class HeaderFormatter:
    """Lazy redactor for HTTP headers, formats only on emit."""

    SENSITIVE_HEADERS = frozenset(
        {
            "authorization",
            "proxy-authorization",
            "cookie",
            "set-cookie",
            "x-api-key",
            "x-auth-token",
        }
    )

    def __init__(
        self,
        headers: dict[str, str] | list[tuple[str, str]],
    ) -> None:
        self.headers = headers

    def __str__(self) -> str:
        items: Iterable[tuple[str, str]] = (
            self.headers.items() if isinstance(self.headers, dict) else self.headers
        )
        redacted = {
            k: "<redacted>" if k.lower() in self.SENSITIVE_HEADERS else v
            for k, v in items
        }
        return repr(redacted)


class BodyFormatter:
    """Lazy %s-arg for logging HTTP bodies — formats only on emit."""

    def __init__(
        self,
        body: bytes | str | IO[bytes] | None,
        content_type: str = "",
        binary_max_size: int = 4096,
    ) -> None:
        self.body = body
        self.binary_max_size = binary_max_size
        self.content_type = content_type

    def __str__(self) -> str:
        match self.body:
            case None:
                return self.format_none()
            case bytes() | bytearray() as data:
                return self.dispatch_bytes(bytes(data))
            case str() as text:
                return self.dispatch_bytes(text.encode("utf-8", errors="replace"))
            case _:
                return self.format_stream()

    def format_none(self) -> str:
        return "<none>"

    def format_stream(self) -> str:
        return "<stream>"

    def dispatch_bytes(self, data: bytes) -> str:
        if not data:
            return "<empty>"
        if self.content_type and (
            "json" in self.content_type or "text" in self.content_type
        ):
            return self.format_json(data)
        if self.content_type:
            return self.format_bytes(data)
        return self.format_json(data)

    def format_json(self, data: bytes) -> str:
        truncated = data[: self.binary_max_size]
        try:
            text = truncated.decode("utf-8")
        except UnicodeDecodeError:
            return self.format_bytes(data)
        if len(data) > self.binary_max_size:
            return f"{text}... <truncated, {len(data)}B total>"
        return text

    def format_bytes(self, data: bytes) -> str:
        if self.content_type:
            return f"<binary {len(data)}B Content-Type={self.content_type!r}>"
        return f"<binary {len(data)}B>"


class GzipResponse:
    """Wrap an HTTPResponse, inflating `Content-Encoding: gzip` via
    `zlib.decompressobj(wbits=31)` so Z_SYNC_FLUSH'd SSE frames decode
    incrementally without buffering whole responses."""

    def __init__(self, resp: http.client.HTTPResponse) -> None:
        self.resp = resp
        self.decomp = zlib.decompressobj(wbits=31)
        self.buf = bytearray()
        self.eof = False

    def pump(self, want: int = -1) -> None:
        while not self.eof and (want < 0 or len(self.buf) < want):
            # read1() returns whatever's in the socket buffer after one
            # underlying read; plain read(n) blocks for n bytes, which
            # stalls SSE streaming because tiny frames never fill it.
            chunk = self.resp.read1(8192)
            if not chunk:
                tail = self.decomp.flush()
                if tail:
                    self.buf.extend(tail)
                self.eof = True
                return
            decoded = self.decomp.decompress(chunk)
            if decoded:
                self.buf.extend(decoded)

    def read(self, amt: int | None = None) -> bytes:
        if amt is None or amt < 0:
            self.pump()
            out = bytes(self.buf)
            self.buf.clear()
            return out
        self.pump(amt)
        out = bytes(self.buf[:amt])
        del self.buf[:amt]
        return out

    def readline(self, limit: int = -1) -> bytes:
        while True:
            nl = self.buf.find(b"\n")
            if nl >= 0:
                end = nl + 1
                if 0 <= limit < end:
                    end = limit
                out = bytes(self.buf[:end])
                del self.buf[:end]
                return out
            if self.eof:
                out = bytes(self.buf)
                self.buf.clear()
                return out
            self.pump(want=len(self.buf) + 1)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.resp.close()

    def getheader(self, name: str, default: str | None = None) -> str | None:
        # Hide Content-Encoding so downstream readers don't try a
        # second decompression; Content-Length is bogus post-inflate.
        lname = name.lower()
        if lname in ("content-encoding", "content-length"):
            return default
        return self.resp.getheader(name, default)

    def getheaders(self) -> list[tuple[str, str]]:
        return [
            (k, v)
            for k, v in self.resp.getheaders()
            if k.lower() not in ("content-encoding", "content-length")
        ]

    @property
    def status(self) -> int:
        return self.resp.status

    @property
    def reason(self) -> str:
        return self.resp.reason


class BufferedResponse:
    """Replay an HTTPResponse from buffered bytes (for debug body logging)."""

    def __init__(
        self,
        status: int,
        reason: str,
        headers: list[tuple[str, str]],
        data: bytes,
    ) -> None:
        self.status = status
        self.reason = reason
        self.headers = headers
        self.buf = io.BytesIO(data)

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            return self.buf.read()
        return self.buf.read(amt)

    def getheader(self, name: str, default: str | None = None) -> str | None:
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return default

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self.headers)


class ApiError(Exception):
    """Raised when the contree API returns a non-2xx status."""

    def __init__(self, status: int, reason: str, body: str) -> None:
        self.status = status
        self.reason = reason
        self.body = body

    def __str__(self) -> str:
        return f"API {self.status} {self.reason}: {self.body}"


class ContreeClient(ABC):
    """Abstract HTTP client for the contree REST API (stdlib only)."""

    def __init__(
        self, url: str, token: str | None, timeout: float | None = None
    ) -> None:
        parts = urlsplit(url)
        self._scheme = parts.scheme or "https"
        self._host = parts.hostname or "localhost"
        self._port = parts.port
        self._prefix = (parts.path or "").rstrip("/")
        self._token = token
        self._timeout = timeout

    @abstractmethod
    def _build_headers(self) -> dict[str, str]:
        """Return authentication headers for the request."""

    def _connect(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            # Stdlib http.client is the only option — project has
            # zero external dependencies. SSL context uses defaults.
            # nosemgrep: httpsconnection-detected
            return http.client.HTTPSConnection(
                self._host,
                self._port,
                timeout=self._timeout,
            )
        return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | IO[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ) -> http.client.HTTPResponse:
        merged: dict[str, str] = {
            **self._build_headers(),
            "User-Agent": CLI_USER_AGENT,
            "Accept-Encoding": "gzip",
        }
        if body is not None:
            merged.setdefault("Content-Type", "application/json")
        if headers:
            merged.update(headers)

        full_path = self._prefix + path
        last_error: ApiError | None = None
        last_network_error: BaseException | None = None
        attempts = len(RETRY_DELAYS) + 1

        log.debug(
            "%s %s headers=%s body=%s",
            method,
            full_path,
            HeaderFormatter(merged),
            BodyFormatter(body, content_type=merged.get("Content-Type", "")),
        )

        for attempt in range(attempts):
            if last_error is not None or last_network_error is not None:
                delay = RETRY_DELAYS[attempt - 1]
                if last_network_error is not None:
                    log.warning(
                        "Network error (%s), retrying in %ds…",
                        type(last_network_error).__name__,
                        delay,
                    )
                else:
                    assert last_error is not None
                    log.warning(
                        "Server error %d, retrying in %ds…",
                        last_error.status,
                        delay,
                    )
                time.sleep(delay)

            if attempt > 0 and hasattr(body, "seek"):
                stream = cast(IO[bytes], body)
                if not stream.seekable():
                    raise ApiError(
                        0,
                        "RetryNotSeekable",
                        "Cannot retry: streaming body is not seekable",
                    )
                stream.seek(0)
            try:
                conn = self._connect()
                conn.request(method, full_path, body, merged)
                resp = conn.getresponse()
            except TimeoutError as exc:
                raise TimeoutError(f"Request timed out: {method} {full_path}") from exc
            except http.client.InvalidURL:
                # Malformed URL is a permanent caller-side error — retrying
                # would just spin through the back-off ladder for nothing.
                raise
            except RETRYABLE_NETWORK_ERRORS as exc:
                last_network_error = exc
                last_error = None
                continue

            # Successful round-trip clears the network-error trail so the
            # final raise below doesn't pick up stale failure context.
            last_network_error = None

            if (resp.getheader("Content-Encoding", "") or "").lower() == "gzip":
                resp = cast(http.client.HTTPResponse, GzipResponse(resp))

            if 200 <= resp.status < 300:
                log.debug(
                    "%s %s -> %d %s headers=%s",
                    method,
                    full_path,
                    resp.status,
                    resp.reason,
                    HeaderFormatter(list(resp.getheaders())),
                )
                if log.isEnabledFor(logging.DEBUG):
                    return self.log_and_buffer(method, full_path, resp)
                return resp

            if resp.status in (410, 425):
                # Retry-After hint: sleep the next-attempt delay, capped
                # at the last RETRY_DELAYS entry so `attempt=0` uses 1s
                # instead of the -1 index wrapping to the tail (10s).
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                last_error = ApiError(resp.status, resp.reason, resp.read().decode())
                continue

            resp_headers = list(resp.getheaders())
            resp_body = resp.read().decode("utf-8", errors="replace")
            log.debug(
                "%s %s -> %d %s (%dB) headers=%s",
                method,
                full_path,
                resp.status,
                resp.reason,
                len(resp_body),
                HeaderFormatter(resp_headers),
            )
            log.debug(
                "%s %s response body: %s",
                method,
                full_path,
                BodyFormatter(
                    resp_body,
                    content_type=resp.getheader("Content-Type", "") or "",
                ),
            )
            error = ApiError(resp.status, resp.reason, resp_body)

            if 500 <= resp.status < 600:
                last_error = error
                continue

            raise error

        if last_network_error is not None:
            raise last_network_error
        assert last_error is not None
        raise last_error

    def log_and_buffer(
        self,
        method: str,
        full_path: str,
        resp: http.client.HTTPResponse,
    ) -> http.client.HTTPResponse:
        """Read & log a textual response body; pass binary streams through."""
        content_type = resp.getheader("Content-Type", "") or ""
        # event-stream is line-streamed and unbounded — buffering it would block.
        is_event_stream = "event-stream" in content_type
        textual = not content_type or "json" in content_type or "text" in content_type
        if is_event_stream or not textual:
            log.debug(
                "%s %s response body: <stream Content-Type=%r>",
                method,
                full_path,
                content_type,
            )
            return resp
        data = resp.read()
        log.debug(
            "%s %s response body: %s",
            method,
            full_path,
            BodyFormatter(data, content_type=content_type),
        )
        return cast(
            http.client.HTTPResponse,
            BufferedResponse(
                status=resp.status,
                reason=resp.reason,
                headers=list(resp.getheaders()),
                data=data,
            ),
        )

    # -- convenience methods --------------------------------------------------

    def get(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> http.client.HTTPResponse:
        if params:
            path = f"{path}?{urlencode(params)}"
        return self.request("GET", path)

    def post_json(
        self,
        path: str,
        payload: dict[str, object],
    ) -> http.client.HTTPResponse:
        return self.request(
            "POST",
            path,
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

    def patch_json(
        self,
        path: str,
        payload: dict[str, object],
    ) -> http.client.HTTPResponse:
        return self.request(
            "PATCH",
            path,
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

    def delete(self, path: str) -> http.client.HTTPResponse:
        return self.request("DELETE", path)


class ContreeJWTClient(ContreeClient):
    """Client using JWT bearer-token authentication."""

    @classmethod
    def from_profile(
        cls,
        profile: ConfigProfile,
        timeout: float | None = None,
    ) -> ContreeJWTClient:
        if not profile.url:
            raise ValueError(
                f"No URL configured for JWT profile {profile.name!r}."
                " Run `contree auth` or pass --url."
            )
        return cls(profile.url, profile.token, timeout=timeout)

    def _build_headers(self) -> dict[str, str]:
        if self._token is None:
            raise ApiError(
                0,
                "Unauthorized",
                "No token configured. Run `contree auth` first.",
            )
        return {"Authorization": f"Bearer {self._token}"}


class ContreeIAMClient(ContreeClient):
    """Client using IAM authentication with a project header."""

    DEFAULT_URL = "https://api.tokenfactory.nebius.com/sandboxes"

    def __init__(
        self,
        url: str,
        token: str | None,
        project: str | None,
        timeout: float | None = None,
    ) -> None:
        super().__init__(url, token, timeout=timeout)
        self._project = project

    @classmethod
    def from_profile(
        cls,
        profile: ConfigProfile,
        timeout: float | None = None,
    ) -> ContreeIAMClient:
        return cls(
            profile.url or cls.DEFAULT_URL,
            profile.token,
            profile.project,
            timeout=timeout,
        )

    def _build_headers(self) -> dict[str, str]:
        if self._token is None:
            raise ApiError(
                0,
                "Unauthorized",
                "No token configured. Run `contree auth` first.",
            )
        if self._project is None:
            raise ApiError(
                0,
                "No project",
                "No project configured. Run `contree auth` first.",
            )
        return {
            "Authorization": f"Bearer {self._token}",
            "Project": self._project,
        }


def client_from_profile(
    profile: ConfigProfile,
    timeout: float | None = None,
) -> ContreeClient:
    """Create the appropriate client for a profile's auth type."""
    if profile.auth_type == AuthType.IAM:
        return ContreeIAMClient.from_profile(profile, timeout=timeout)
    return ContreeJWTClient.from_profile(profile, timeout=timeout)


_UUID_RE_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _is_uuid(ref: str) -> bool:
    """Return True if *ref* looks like a UUID."""
    import re

    return re.match(_UUID_RE_PATTERN, ref, re.ASCII) is not None


def _resolve_tag(client: ContreeClient, tag: str) -> str:
    resp = client.get("/v1/images", params={"tag": tag})
    data = json.loads(resp.read())
    images = data.get("images", [])
    if not images:
        raise ApiError(404, "Not Found", f"No image with tag '{tag}'")
    return str(images[0]["uuid"])


def resolve_image(client: ContreeClient, ref: str) -> str:
    """Resolve an image reference to a UUID.

    Accepts a raw UUID, ``tag:NAME``, or a bare tag name.  For bare
    references that are not valid UUIDs the function tries to resolve
    them as tag names.
    """
    if ref.startswith("tag:"):
        return _resolve_tag(client, ref[4:])
    if _is_uuid(ref):
        return ref
    return _resolve_tag(client, ref)


CHUNK_SIZE = 256 * 1024  # 256 KiB


def stream_response(
    resp: http.client.HTTPResponse,
) -> Iterator[bytes]:
    """Yield chunks from *resp*."""
    while True:
        chunk = resp.read(CHUNK_SIZE)
        if not chunk:
            break
        yield chunk


def iter_sse_events(resp: http.client.HTTPResponse) -> Iterator[dict[str, object]]:
    """Yield one dict per SSE frame.

    Normal frames carry a JSON object in ``data:`` — that object is
    yielded as-is.  Server-pushed error frames use ``event: sse_error``
    with a plain-text ``data:`` body (the exception message) — those
    surface as ``{"type": "sse_error", "message": <text>, "id": ...}``
    so callers can log + decide whether to reconnect with
    ``Last-Event-Id`` set to the last good id.
    """
    data_lines: list[str] = []
    event_name: str | None = None
    event_id: str | None = None

    def emit() -> Iterator[dict[str, object]]:
        nonlocal event_name, event_id
        if not data_lines:
            return
        body = "\n".join(data_lines)
        if event_name == "sse_error":
            payload: dict[str, object] = {"type": "sse_error", "message": body}
            if event_id is not None:
                payload["id"] = event_id
            yield payload
        else:
            with suppress(json.JSONDecodeError):
                decoded = json.loads(body)
                if isinstance(decoded, dict):
                    yield decoded
        data_lines.clear()
        event_name = None
        event_id = None

    while True:
        line_bytes = resp.readline()
        if not line_bytes:
            yield from emit()
            return
        line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
        match line:
            case "":
                yield from emit()
            case _ if line.startswith(":"):
                pass  # SSE comment / keepalive
            case _ if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
            case _ if line.startswith("event:"):
                event_name = line[6:].lstrip(" ").strip() or None
            case _ if line.startswith("id:"):
                event_id = line[3:].lstrip(" ").strip() or None


def decode_event_chunk(data: object) -> bytes:
    """Decode `{value, encoding}` event payload to raw bytes (ascii or base64)."""
    if not isinstance(data, dict):
        return b""
    value = data.get("value", "")
    if not isinstance(value, str) or not value:
        return b""
    encoding = data.get("encoding", "ascii")
    if encoding == "base64":
        with suppress(binascii.Error, ValueError):
            return base64.b64decode(value)
        return b""
    return value.encode("utf-8", errors="replace")


def decode_stream(stream: dict[str, object] | None) -> str:
    """Decode an API StreamRepr object to a string."""
    if not stream:
        return ""
    value = stream.get("value", "")
    if not isinstance(value, str) or not value:
        return ""
    encoding = stream.get("encoding", "ascii")
    if encoding == "base64":
        return base64.b64decode(value).decode(
            "utf-8",
            errors="replace",
        )
    return value


class PaginatedFetcher:
    """Iterate paginated API endpoint pages concurrently.

    Issues GET ``path`` requests with ``offset``/``limit`` query params,
    pulling pages in parallel via :class:`ThreadPool.imap` (results
    delivered in offset order). Stops on the first empty or short
    (``< page_size``) page or after ``max_pages`` requests. May
    over-fetch by up to ``concurrency - 1`` pages past the actual end
    of data; the trade-off buys roughly ``concurrency``-fold latency
    reduction on multi-page listings.

    Callers that hit their own record limit mid-iteration should call
    :meth:`stop` to short-circuit pending workers — fetches that have
    not yet issued their HTTP request will skip it and return an empty
    page, ending iteration.

    :attr:`exhausted` is ``True`` after iteration finishes only if the
    helper saw the end of data (short/empty page) — including via the
    ``stop`` signal. It stays ``False`` if it stopped because
    ``max_pages`` was reached without seeing the end.
    """

    DEFAULT_PAGE_SIZE = 1000
    UNLIMITED_MAX_PAGES = 1000  # 1M-record safety cap when limit=None.

    def __init__(
        self,
        client: ContreeClient,
        path: str,
        params: dict[str, str],
        extract: Callable[[bytes], list[dict[str, Any]]],
        *,
        limit: int | None,
        page_size: int | None = None,
        concurrency: int = 8,
    ) -> None:
        """Configure a paginated fetch.

        ``limit`` is the caller's record budget (``--limit``, ``--show-max``).
        ``None`` means "fetch everything up to ``UNLIMITED_MAX_PAGES * page_size``
        records". When set, ``page_size`` is capped at ``limit + 1`` so a
        small budget like ``--limit 5`` doesn't pull a 1000-row page just
        to discard 995, and ``max_pages`` is sized to cover ``limit + 1``
        records (the extra record lets callers detect "more available"
        and warn). ``page_size`` defaults to :attr:`DEFAULT_PAGE_SIZE`.
        """
        self.client = client
        self.path = path
        self.params = params
        self.extract = extract
        self.concurrency = concurrency
        self.exhausted = False
        self._stop = threading.Event()

        default_page_size = page_size or self.DEFAULT_PAGE_SIZE
        if limit is None:
            self.page_size = default_page_size
            self.max_pages = self.UNLIMITED_MAX_PAGES
        else:
            # Fetch one extra record so callers can detect "more results
            # exist past --limit" and emit a warning.
            self.page_size = min(default_page_size, limit + 1)
            self.max_pages = (limit + self.page_size) // self.page_size + 1

    def stop(self) -> None:
        """Signal that the caller has seen enough; skip pending fetches."""
        self._stop.set()

    def __enter__(self) -> PaginatedFetcher:
        return self

    def __exit__(self, *_: object) -> None:
        # Setting the stop event short-circuits any worker that hasn't
        # started yet and prevents the iterator's refill from enqueueing
        # more fetches. Callers wrap iteration in `with PaginatedFetcher(...)`
        # so they don't have to remember an explicit `stop()` after
        # breaking out of a paged loop.
        self.stop()

    def _fetch(self, offset: int) -> list[dict[str, Any]]:
        if self._stop.is_set():
            return []
        page_params = {
            **self.params,
            "offset": str(offset),
            "limit": str(self.page_size),
        }
        resp = self.client.get(self.path, params=page_params)
        return self.extract(resp.read())

    def __iter__(self) -> Iterator[list[dict[str, Any]]]:
        offsets = iter(i * self.page_size for i in range(self.max_pages))
        pending: collections.deque[Future[list[dict[str, Any]]]] = collections.deque()
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            # Prime the pool with up to `concurrency` in-flight fetches.
            for _ in range(self.concurrency):
                with contextlib.suppress(StopIteration):
                    pending.append(pool.submit(self._fetch, next(offsets)))

            while pending:
                page = pending.popleft().result()
                if not page:
                    self.exhausted = True
                    self._stop.set()
                    continue
                if len(page) < self.page_size:
                    # Mark exhausted before yielding so callers that break
                    # out of the loop still see the correct end-of-data flag.
                    self.exhausted = True
                    self._stop.set()
                yield page
                # Refill so in-flight count stays at `concurrency`.
                if not self._stop.is_set():
                    with contextlib.suppress(StopIteration):
                        pending.append(pool.submit(self._fetch, next(offsets)))
