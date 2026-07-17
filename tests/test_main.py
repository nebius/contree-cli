"""Entry-point wiring: resource lifecycle around command dispatch.

The transport backend is auto-detected and session-based backends
(requests/httpx/urllib3) pool connections, so main() must drive the
client through its context manager. These tests run main() end to end
with a spy client to pin that contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from contree_client.profiles import Profile

from contree_cli.__main__ import main
from contree_cli.config import Config


class DummyChecker:
    """UpdateChecker stand-in: no PyPI traffic, always up to date."""

    def refresh(self) -> None:
        pass

    def is_latest(self) -> bool:
        return True


class SpyClient:
    """Records context-manager transitions instead of doing HTTP."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self) -> SpyClient:
        self.events.append("enter")
        return self

    def __exit__(self, *exc: object) -> None:
        self.events.append("exit")


def write_profile(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "auth.ini"
    cfg = Config(cfg_path)
    cfg["default"] = Profile(
        name="default",
        url="https://contree.dev",
        token="tok",
    )
    return cfg_path


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    cfg_path: Path,
    events: list[str],
    command: str,
) -> pytest.ExceptionInfo[SystemExit]:
    monkeypatch.setattr("contree_cli.__main__.UpdateChecker", DummyChecker)
    monkeypatch.setattr(
        "contree_cli.__main__.client_from_profile",
        lambda profile, timeout=300.0: SpyClient(events),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["contree", "--config", str(cfg_path), command],
    )
    with pytest.raises(SystemExit) as exc_info:
        main()
    return exc_info


class TestClientLifecycle:
    def test_client_entered_and_exited(self, tmp_path, monkeypatch, capsys):
        """main() drives the client through __enter__/__exit__ so
        session-based transports release pooled connections."""
        cfg_path = write_profile(tmp_path)
        events: list[str] = []

        # `session` without an active session prints an error and
        # exits 1 without touching the API: the lifecycle is observed
        # without mocking any API method.
        run_main(monkeypatch, cfg_path, events, "session")

        assert events == ["enter", "exit"]

    def test_local_command_creates_no_client(self, tmp_path, monkeypatch, capsys):
        cfg_path = write_profile(tmp_path)
        events: list[str] = []

        run_main(monkeypatch, cfg_path, events, "agent")

        assert events == []
