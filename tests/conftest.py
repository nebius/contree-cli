from __future__ import annotations

import http.client
import json
import os
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

import contree_cli.config as config_mod
from contree_cli import CLIENT, PROFILE
from contree_cli.client import ContreeClient, ContreeIAMClient
from contree_cli.config import ConfigProfile
from contree_cli.session import ImageCache, SessionStore

for var in (
    "CONTREE_TOKEN",
    "CONTREE_URL",
    "CONTREE_PROJECT",
    "CONTREE_PROFILE",
    "CONTREE_SESSION",
    "CONTREE_SESSION_DB",
    "NEBIUS_API_KEY",
    "NEBIUS_AI_PROJECT",
):
    os.environ.pop(var, None)


@dataclass
class FakeResponse:
    """Minimal HTTPResponse-compatible object for tests."""

    status: int = 200
    reason: str = ""
    body: bytes = b"{}"
    headers: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.reason:
            self.reason = "OK" if self.status < 300 else "Error"
        if self.headers is None:
            self.headers = {}

    def read(self, amt: int | None = None) -> bytes:
        return self.body

    def getheader(self, name: str, default: str | None = None) -> str | None:
        assert self.headers is not None
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def getheaders(self) -> list[tuple[str, str]]:
        assert self.headers is not None
        return list(self.headers.items())

    @staticmethod
    def json(body: object, *, status: int = 200) -> FakeResponse:
        return FakeResponse(
            status=status,
            body=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )


@dataclass
class RecordedRequest:
    """A single HTTP request captured by FakeConnection."""

    method: str
    path: str
    body: bytes | None
    headers: dict[str, str]


class FakeConnection:
    """Drop-in replacement for http.client.HTTPConnection in tests."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.responses: list[FakeResponse] = []

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.requests.append(RecordedRequest(method, path, body, headers or {}))

    def getresponse(self) -> FakeResponse:
        return self.responses.pop(0)


class ContreeTestClient(ContreeClient):
    """Test client with FakeConnection and Bearer auth."""

    def __init__(self, url: str = "https://contree.dev", token: str = "tok") -> None:
        super().__init__(url, token)
        self.fake = FakeConnection()

    def _build_headers(self) -> dict[str, str]:
        return {"Authorization": "Hello"}

    def _connect(self) -> http.client.HTTPConnection:
        return self.fake  # type: ignore[return-value]

    # -- response helpers --

    def respond(self, *, status: int = 200, body: bytes = b"{}") -> None:
        self.fake.responses.append(FakeResponse(status=status, body=body))

    def respond_json(self, body: object, *, status: int = 200) -> None:
        self.fake.responses.append(FakeResponse.json(body, status=status))

    # -- request introspection --

    @property
    def request_count(self) -> int:
        return len(self.fake.requests)

    @property
    def request_paths(self) -> list[str]:
        return [r.path for r in self.fake.requests]

    def get_request(self, index: int = -1) -> RecordedRequest:
        return self.fake.requests[index]


class ContreeTestIAMClient(ContreeIAMClient):
    """IAM client that uses FakeConnection instead of real HTTP."""

    def __init__(
        self,
        url: str = "https://iam.test",
        token: str = "tok",
        project: str = "aiproject-test",
    ) -> None:
        super().__init__(url, token, project)
        self.fake = FakeConnection()

    def _connect(self) -> http.client.HTTPConnection:
        return self.fake  # type: ignore[return-value]

    def respond(self, *, status: int = 200, body: bytes = b"{}") -> None:
        self.fake.responses.append(FakeResponse(status=status, body=body))

    def respond_json(self, body: object, *, status: int = 200) -> None:
        self.fake.responses.append(FakeResponse.json(body, status=status))

    @property
    def request_count(self) -> int:
        return len(self.fake.requests)

    @property
    def request_paths(self) -> list[str]:
        return [r.path for r in self.fake.requests]

    def get_request(self, index: int = -1) -> RecordedRequest:
        return self.fake.requests[index]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def contree_client() -> ContreeTestClient:
    tc = ContreeTestClient()
    CLIENT.set(tc)
    return tc


@pytest.fixture()
def iam_client() -> ContreeTestIAMClient:
    tc = ContreeTestIAMClient()
    CLIENT.set(tc)
    return tc


@pytest.fixture()
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect CONTREE_HOME / CONFIG_DIR / CONFIG_FILE to a temp directory."""
    home = tmp_path / ".contree"
    cfg_dir = home / "contree"
    cfg_file = cfg_dir / "auth.ini"
    monkeypatch.setattr(config_mod, "CONTREE_HOME", home)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", cfg_file)
    return cfg_dir


@pytest.fixture()
def profile() -> Generator[ConfigProfile]:
    """Set PROFILE context var to a test profile, reset after."""
    p = ConfigProfile(name="test", url="http://localhost", token="tok")
    token = PROFILE.set(p)
    yield p  # type: ignore[misc]
    PROFILE.reset(token)


@pytest.fixture()
def session_store(tmp_path: Path) -> Generator[SessionStore]:
    """A fresh SessionStore backed by a temp DB, pre-keyed as 'test'."""
    store = SessionStore(tmp_path / "test.db", "test")
    yield store  # type: ignore[misc]
    store.close()


@pytest.fixture()
def image_cache(session_store: SessionStore) -> ImageCache:
    """ImageCache from the session_store fixture."""
    return session_store.cache
