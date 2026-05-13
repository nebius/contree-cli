"""Fetch a URL straight into ``POST /v1/files`` for ``ADD`` directives.

The body is streamed from the source socket to the contree API socket
without ever touching local disk. Before the download starts we issue a
``HEAD`` against the source URL: if our cached ``etag`` /
``last-modified`` / ``content-md5`` still match the upstream headers we
return the cached remote ``file_uuid`` without re-downloading or
re-uploading. When the upstream has no usable validators we issue the
``GET``, hash the body as it flies through, and persist whatever
validators the server did return so the next build can short-circuit.

HTTP transport is ``urllib.request`` which transparently follows
redirects and handles HTTPS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import IO, cast

from contree_cli.client import ApiError, ContreeClient
from contree_cli.session import SessionStore

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT_DEFAULT = 300
USER_AGENT = "contree-cli url-fetch"


@dataclass(frozen=True)
class FetchedUrl:
    url: str
    file_uuid: str
    sha256: str
    size: int
    cache_state: str = "fetched"  # "head", "get-304", or "fetched"


def fetch_and_upload(
    url: str,
    client: ContreeClient,
    store: SessionStore,
    *,
    timeout: int = DOWNLOAD_TIMEOUT_DEFAULT,
) -> FetchedUrl:
    """Resolve ``url`` to a remote ``file_uuid``, skipping work whenever
    cached validators match the upstream.

    The returned ``cache_state`` tells the caller how the result was
    obtained (``"head"``, ``"get-304"``, ``"fetched"``); use it to emit
    a ``CACHED:`` log line at the call site.
    """
    cache_key = url_cache_key(url)
    meta = read_metadata(store, cache_key)

    head_headers = http_head(url, timeout=timeout)
    if meta and head_headers and validators_match(meta, head_headers):
        bump_fetched_at(store, cache_key, meta)
        return cached_result(url, meta, cache_state="head")

    cond = conditional_headers(meta) if meta else {}
    status, response_headers, source = http_get_stream(
        url, headers=cond, timeout=timeout
    )
    if status == 304:
        if not meta:
            raise RuntimeError(
                f"server returned 304 for {url!r} but no cached metadata exists"
            )
        bump_fetched_at(store, cache_key, meta)
        return cached_result(url, meta, cache_state="get-304")

    assert source is not None
    try:
        reader = HashingReader(source)
        upload_headers: dict[str, str] = {"Content-Type": "application/octet-stream"}
        content_length = parse_content_length(response_headers)
        if content_length > 0:
            upload_headers["Content-Length"] = str(content_length)

        resp = client.request(
            "POST",
            "/v1/files",
            body=cast(IO[bytes], reader),
            headers=upload_headers,
        )
        data = json.loads(resp.read())
    finally:
        close = getattr(source, "close", None)
        if callable(close):
            close()

    file_uuid = str(data["uuid"])
    sha = reader.hasher.hexdigest()
    size = reader.bytes_read

    write_metadata(
        store,
        cache_key,
        {
            "uuid": file_uuid,
            "url": url,
            "sha256": sha,
            "size": size,
            "etag": response_headers.get("etag", ""),
            "last_modified": response_headers.get("last-modified", ""),
            "content_md5": response_headers.get("content-md5", ""),
            "fetched_at": time.time(),
        },
    )
    logger.info(
        "URL piped %s -> %s (%d bytes, sha %s)",
        url,
        file_uuid,
        size,
        sha[:12],
    )
    return FetchedUrl(url=url, file_uuid=file_uuid, sha256=sha, size=size)


def url_cache_key(url: str) -> str:
    """The URL is its own identity in the local_file cache."""
    return f"local_file:{url}"


def read_metadata(store: SessionStore, cache_key: str) -> dict[str, object] | None:
    value = store.cache.get(("", cache_key))
    if isinstance(value, dict):
        return value
    return None


def write_metadata(
    store: SessionStore,
    cache_key: str,
    meta: dict[str, object],
) -> None:
    store.cache[("", cache_key)] = meta


def bump_fetched_at(
    store: SessionStore,
    cache_key: str,
    meta: dict[str, object],
) -> None:
    refreshed = dict(meta)
    refreshed["fetched_at"] = time.time()
    write_metadata(store, cache_key, refreshed)


def cached_result(url: str, meta: dict[str, object], *, cache_state: str) -> FetchedUrl:
    size_raw = meta.get("size", -1)
    size = size_raw if isinstance(size_raw, int) else -1
    return FetchedUrl(
        url=url,
        file_uuid=str(meta["uuid"]),
        sha256=str(meta["sha256"]),
        size=size,
        cache_state=cache_state,
    )


def validators_match(meta: dict[str, object], headers: dict[str, str]) -> bool:
    """Return True if any cached validator still matches the upstream headers."""
    etag_cached = meta.get("etag")
    etag_upstream = headers.get("etag")
    if isinstance(etag_cached, str) and etag_cached and etag_cached == etag_upstream:
        return True
    lm_cached = meta.get("last_modified")
    lm_upstream = headers.get("last-modified")
    if isinstance(lm_cached, str) and lm_cached and lm_cached == lm_upstream:
        return True
    md5_cached = meta.get("content_md5")
    md5_upstream = headers.get("content-md5")
    return bool(
        isinstance(md5_cached, str) and md5_cached and md5_cached == md5_upstream
    )


def conditional_headers(meta: dict[str, object]) -> dict[str, str]:
    headers: dict[str, str] = {}
    etag = meta.get("etag")
    if isinstance(etag, str) and etag:
        headers["If-None-Match"] = etag
    last_modified = meta.get("last_modified")
    if isinstance(last_modified, str) and last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def parse_content_length(headers: dict[str, str]) -> int:
    raw = headers.get("content-length")
    if not raw:
        return -1
    try:
        return int(raw)
    except ValueError:
        return -1


class HashingReader:
    """Read-only adapter that hashes bytes as they flow through.

    Intentionally has no ``seek`` attribute so ``ContreeClient.request``
    does not attempt to rewind the upstream HTTP body on retry.
    """

    __slots__ = ("bytes_read", "hasher", "source")

    def __init__(self, source: object) -> None:
        self.source = source
        self.hasher = hashlib.sha256()
        self.bytes_read = 0

    def read(self, amt: int | None = None) -> bytes:
        chunk = self.source.read() if amt is None else self.source.read(amt)  # type: ignore[attr-defined]
        if chunk:
            self.hasher.update(chunk)
            self.bytes_read += len(chunk)
        return chunk  # type: ignore[no-any-return]


def http_head(url: str, *, timeout: int = DOWNLOAD_TIMEOUT_DEFAULT) -> dict[str, str]:
    """Best-effort ``HEAD`` request. Returns empty headers on any failure.

    ``urllib.request`` transparently follows 3xx redirects, so the returned
    headers always describe the final resource.
    """
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        logger.debug("HEAD %s returned %d, skipping validator probe", url, exc.code)
        return {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.debug("HEAD %s failed (%s), skipping validator probe", url, exc)
        return {}


def http_get_stream(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int = DOWNLOAD_TIMEOUT_DEFAULT,
) -> tuple[int, dict[str, str], object | None]:
    """Issue a GET; return ``(status, headers, body_stream | None)``.

    A ``304 Not Modified`` response yields ``(304, headers, None)`` and
    has already been drained. For any other non-2xx the function raises
    ``RuntimeError``. The caller owns the returned stream and must close it.
    Redirects are handled by ``urllib.request``.
    """
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "*/*", **headers},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response_headers = {k.lower(): v for k, v in exc.headers.items()}
        if exc.code == 304:
            exc.close()
            return 304, response_headers, None
        body = exc.read(2048).decode("utf-8", errors="replace")
        exc.close()
        raise RuntimeError(
            f"GET {url!r} returned {exc.code} {exc.reason}: {body!r}"
        ) from exc

    response_headers = {k.lower(): v for k, v in resp.headers.items()}
    return resp.status, response_headers, resp


def is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def url_basename(url: str, fallback: str = "downloaded") -> str:
    parsed = urllib.parse.urlsplit(url)
    name = parsed.path.rsplit("/", 1)[-1]
    return name or fallback


__all__ = [
    "ApiError",
    "FetchedUrl",
    "fetch_and_upload",
    "is_url",
    "url_basename",
]
