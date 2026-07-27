from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from collections import deque
from collections.abc import Generator
from pathlib import Path

# Redirect CONTREE_HOME to a throwaway directory BEFORE importing
# contree_cli.config (which reads the variable at import time). Without
# this, tests would write SQLite databases under the user's real
# ~/.config/contree -- which also breaks inside sandboxes that block
# writes to $HOME.
CONTREE_HOME_TMP = Path(tempfile.mkdtemp(prefix="contree-pytest-"))
os.environ["CONTREE_HOME"] = str(CONTREE_HOME_TMP)
atexit.register(shutil.rmtree, CONTREE_HOME_TMP, ignore_errors=True)

# The CONTREE_HOME override above MUST run before any contree_cli import
# touches contree_cli.config, hence the deferred import block below.
import pytest  # noqa: E402
from contree_client import testing  # noqa: E402
from contree_client.profiles import Profile  # noqa: E402
from contree_client.runtime import RequestSpec, ResponseData  # noqa: E402

import contree_cli.arguments  # noqa: E402, F401  populates COMMAND_REGISTRY
import contree_cli.config as config_mod  # noqa: E402
from contree_cli import CLIENT, PROFILE  # noqa: E402
from contree_cli.session import ImageCache, SessionStore  # noqa: E402

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


class ContreeTestClient(testing.ContreeClient):
    """Method-level mock double for CLI handler tests.

    API methods are mocked per operation via ``client.mock("name",
    result_model)`` (see ``contree_client.testing``); calls are
    recorded and available through ``calls_for("name")``.

    Two CLI handlers bypass the typed surface with a hand-built
    ``RequestSpec`` (`ls` text mode and the `session wait` session_key
    filter); ``respond_raw()`` queues buffered responses for those, and
    ``raw_requests`` records the specs for assertions.
    """

    def __init__(
        self,
        url: str = "https://contree.dev",
        token: str = "tok",
        project: str | None = None,
    ) -> None:
        super().__init__(token, base_url=url, project=project)
        self.raw_responses: deque[ResponseData] = deque()
        self.raw_requests: list[RequestSpec] = []

    def respond_raw(
        self,
        *,
        status: int = 200,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.raw_responses.append(
            ResponseData(status=status, headers=headers or {}, body=body)
        )

    def request(self, spec: RequestSpec) -> ResponseData:
        self.raw_requests.append(spec)
        if self.raw_responses:
            return self.raw_responses.popleft()
        raise testing.unmocked(spec)


class ContreeTestIAMClient(ContreeTestClient):
    """Test double configured like an IAM profile (project set)."""

    def __init__(
        self,
        url: str = "https://iam.test",
        token: str = "tok",
        project: str = "aiproject-test",
    ) -> None:
        super().__init__(url, token, project)


def make_file_item(path: str, **overrides: object) -> dict[str, object]:
    """Full FileItem payload exactly as the inspect API returns it.

    The contree-client `FileItem` model requires every field, so test
    fixtures must always send the complete realistic shape. Overrides
    replace individual fields; `is_regular` is derived from the other
    type flags unless overridden explicitly.
    """
    item: dict[str, object] = {
        "path": path,
        "size": 128,
        "owner": "root",
        "group": "root",
        "uid": 0,
        "gid": 0,
        "mode": 0o644,
        "mtime": 1700000000,
        "nlink": 1,
        "is_dir": False,
        "is_regular": True,
        "is_symlink": False,
        "is_socket": False,
        "is_fifo": False,
        "symlink_to": "",
    }
    item.update(overrides)
    if "is_regular" not in overrides:
        item["is_regular"] = not (
            item["is_dir"] or item["is_symlink"] or item["is_socket"] or item["is_fifo"]
        )
    return item


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def contree_client() -> ContreeTestClient:
    tc = ContreeTestClient()
    CLIENT.set(tc)  # type: ignore[arg-type]
    return tc


@pytest.fixture()
def iam_client() -> ContreeTestIAMClient:
    tc = ContreeTestIAMClient()
    CLIENT.set(tc)  # type: ignore[arg-type]
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


@pytest.fixture(autouse=True)
def _isolate_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect CODEX_HOME so a skill install exercising CodexSkill (which
    writes a rules file under it) never touches the real ~/.codex.

    Applies globally, unlike `config_dir`, since `default_codex_home()`
    reads the env var directly rather than a module-level constant --
    any test that reaches it without its own explicit
    `default_codex_home` patch would otherwise fall through to the
    real home. Tests that patch the function themselves are
    unaffected: that override takes precedence over this env var.
    """
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex-home"))


@pytest.fixture()
def profile() -> Generator[Profile]:
    """Set PROFILE context var to a test profile, reset after."""
    p = Profile(name="test", url="http://localhost", token="tok")
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
