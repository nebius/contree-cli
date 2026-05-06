from __future__ import annotations

import base64
import http.client
import io
import json
import logging
import platform
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError, version
from typing import IO, cast
from urllib.parse import urlencode, urlsplit

from contree_cli.config import AuthType, ConfigProfile

log = logging.getLogger(__name__)

RETRY_DELAYS = (1, 2, 4, 5, 10, 10, 10)


def _cli_version() -> str:
    try:
        return version("contree-cli")
    except PackageNotFoundError:
        return "editable"


CLI_USER_AGENT = (
    f"contree-cli/{_cli_version()} "
    f"Python/{'.'.join(map(str, sys.version_info))} "
    f"{platform.platform()} "
)


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
        attempts = len(RETRY_DELAYS) + 1

        log.debug(
            "%s %s body=%s",
            method,
            full_path,
            BodyFormatter(body, content_type=merged.get("Content-Type", "")),
        )

        for attempt in range(attempts):
            if last_error is not None:
                delay = RETRY_DELAYS[attempt - 1]
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

            if 200 <= resp.status < 300:
                log.debug(
                    "%s %s -> %d %s",
                    method,
                    full_path,
                    resp.status,
                    resp.reason,
                )
                if log.isEnabledFor(logging.DEBUG):
                    return self.log_and_buffer(method, full_path, resp)
                return resp

            resp_body = resp.read().decode("utf-8", errors="replace")
            log.debug(
                "%s %s -> %d %s (%dB)",
                method,
                full_path,
                resp.status,
                resp.reason,
                len(resp_body),
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
