from __future__ import annotations

import io
import urllib.error
from unittest.mock import patch

import pytest

import contree_cli.docker.url_fetch as url_fetch
from contree_cli.docker.url_fetch import (
    HashingReader,
    fetch_and_upload,
    is_url,
    url_basename,
    url_cache_key,
    validators_match,
)


class FakeStream:
    """Minimal http.client.HTTPResponse-like object."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self._buf = io.BytesIO(body)
        self.headers = headers or {}
        self.status = 200
        self.closed = False

    def read(self, amt: int | None = None) -> bytes:
        return self._buf.read(amt) if amt is not None else self._buf.read()

    def close(self) -> None:
        self.closed = True


class TestSmallHelpers:
    def test_is_url(self):
        assert is_url("https://x")
        assert is_url("http://x")
        assert not is_url("./x")
        assert not is_url("ftp://x")

    def test_url_basename(self):
        assert url_basename("https://example.com/foo/bar.tar.gz") == "bar.tar.gz"
        assert url_basename("https://example.com/") == "downloaded"

    def test_url_cache_key_uses_url_as_identity(self):
        assert url_cache_key("https://x/y") == "local_file:https://x/y"

    def test_validators_match_etag(self):
        meta = {"etag": '"abc"', "last_modified": "", "content_md5": ""}
        assert validators_match(meta, {"etag": '"abc"'}) is True
        assert validators_match(meta, {"etag": '"def"'}) is False

    def test_validators_match_last_modified(self):
        meta = {"etag": "", "last_modified": "Mon, 01 Jan 2024 00:00:00 GMT"}
        assert validators_match(
            meta, {"last-modified": "Mon, 01 Jan 2024 00:00:00 GMT"}
        )

    def test_validators_match_returns_false_when_no_validators(self):
        assert validators_match({}, {}) is False


class TestHashingReader:
    def test_hashes_and_counts(self):
        src = FakeStream(b"hello world")
        r = HashingReader(src)
        chunks = [r.read(5), r.read(5), r.read(5)]
        assert b"".join(chunks) == b"hello world"
        assert r.bytes_read == 11
        import hashlib

        assert r.hasher.hexdigest() == hashlib.sha256(b"hello world").hexdigest()

    def test_has_no_seek_attribute(self):
        """ContreeClient.request relies on absent .seek to skip retry-rewind."""
        r = HashingReader(FakeStream(b"x"))
        assert not hasattr(r, "seek")


class TestFetchAndUpload:
    URL = "https://example.com/pkg.tgz"

    def test_first_fetch_uploads_and_caches(self, contree_client, session_store):
        body = b"abcdef"
        head_headers = {"etag": '"first"'}
        get_headers = {"etag": '"first"', "content-length": str(len(body))}

        contree_client.respond_json({"uuid": "remote-1", "sha256": "x"})

        with (
            patch.object(url_fetch, "http_head", return_value=head_headers),
            patch.object(
                url_fetch,
                "http_get_stream",
                return_value=(200, get_headers, FakeStream(body)),
            ),
        ):
            result = fetch_and_upload(self.URL, contree_client, session_store)

        assert result.file_uuid == "remote-1"
        assert result.size == len(body)

        cached = session_store.cache.get(("", url_cache_key(self.URL)))
        assert isinstance(cached, dict)
        assert cached["uuid"] == "remote-1"
        assert cached["url"] == self.URL
        assert cached["etag"] == '"first"'

        upload_req = contree_client.get_request(0)
        assert upload_req.method == "POST"
        assert "/v1/files" in upload_req.path

    def test_head_validators_skip_download(self, contree_client, session_store):
        cache_key = url_cache_key(self.URL)
        session_store.cache[("", cache_key)] = {
            "uuid": "remote-cached",
            "sha256": "cached-sha",
            "url": self.URL,
            "etag": '"v1"',
            "size": 42,
        }
        with (
            patch.object(url_fetch, "http_head", return_value={"etag": '"v1"'}),
            patch.object(url_fetch, "http_get_stream") as get_mock,
        ):
            result = fetch_and_upload(self.URL, contree_client, session_store)

        assert result.file_uuid == "remote-cached"
        assert result.sha256 == "cached-sha"
        get_mock.assert_not_called()
        assert contree_client.request_count == 0

    def test_get_304_skips_upload(self, contree_client, session_store):
        cache_key = url_cache_key(self.URL)
        session_store.cache[("", cache_key)] = {
            "uuid": "remote-cached",
            "sha256": "cached-sha",
            "url": self.URL,
            "etag": '"v1"',
            "size": 42,
        }
        # HEAD reports no usable validators (e.g. stripped by intermediary).
        with (
            patch.object(url_fetch, "http_head", return_value={}),
            patch.object(
                url_fetch,
                "http_get_stream",
                return_value=(304, {"etag": '"v1"'}, None),
            ),
        ):
            result = fetch_and_upload(self.URL, contree_client, session_store)

        assert result.file_uuid == "remote-cached"
        assert contree_client.request_count == 0


class TestHttpHelpers:
    def test_http_head_returns_empty_on_http_error(self):
        err = urllib.error.HTTPError(
            url="https://x",
            code=405,
            msg="Method Not Allowed",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b""),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            assert url_fetch.http_head("https://x") == {}

    def test_http_get_stream_translates_304_to_status_only(self):
        class FakeHeaders:
            def items(self):
                return [("ETag", '"v1"')]

        err = urllib.error.HTTPError(
            url="https://x",
            code=304,
            msg="Not Modified",
            hdrs=FakeHeaders(),  # type: ignore[arg-type]
            fp=io.BytesIO(b""),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            status, headers, source = url_fetch.http_get_stream("https://x", headers={})
        assert status == 304
        assert headers == {"etag": '"v1"'}
        assert source is None

    def test_http_get_stream_raises_on_non_2xx(self):
        class FakeHeaders:
            def items(self):
                return []

        err = urllib.error.HTTPError(
            url="https://x",
            code=500,
            msg="Internal Server Error",
            hdrs=FakeHeaders(),  # type: ignore[arg-type]
            fp=io.BytesIO(b"oops"),
        )
        with (
            patch("urllib.request.urlopen", side_effect=err),
            pytest.raises(RuntimeError, match="500"),
        ):
            url_fetch.http_get_stream("https://x", headers={})
