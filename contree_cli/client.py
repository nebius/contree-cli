from __future__ import annotations

import base64
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
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError, version
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
        return version("contree-cli")
    except PackageNotFoundError:
        return "editable"


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
            except RETRYABLE_NETWORK_ERRORS as exc:
                last_network_error = exc
                last_error = None
                continue

            # Successful round-trip clears the network-error trail so the
            # final raise below doesn't pick up stale failure context.
            last_network_error = None

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
        textual = not content_type or "json" in content_type or "text" in content_type
        if not textual:
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

    def __init__(
        self,
        client: ContreeClient,
        path: str,
        params: dict[str, str],
        extract: Callable[[bytes], list[dict[str, Any]]],
        *,
        page_size: int,
        max_pages: int,
        concurrency: int = 8,
    ) -> None:
        self.client = client
        self.path = path
        self.params = params
        self.extract = extract
        self.page_size = page_size
        self.max_pages = max_pages
        self.concurrency = concurrency
        self.exhausted = False
        self._stop = threading.Event()

    def stop(self) -> None:
        """Signal that the caller has seen enough; skip pending fetches."""
        self._stop.set()

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
