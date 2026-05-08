from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from contree_cli import update_check
from contree_cli.update_check import UpdateChecker


def read_json(path):
    return json.loads(path.read_text())


def seed_state(path, payload, *, mtime: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    if mtime is not None:
        try:
            os.utime(path, (mtime, mtime))
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"os.utime is not supported on this platform: {exc}")


def fake_now(offset: timedelta):
    """Patch update_check.datetime.now to return real-now + offset.

    Use this when tests need to simulate clock advancement on platforms
    where ``os.utime`` may not work (e.g. some Windows configurations).
    """
    pinned = datetime.now(timezone.utc) + offset

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return pinned if tz is not None else pinned.replace(tzinfo=None)

    return patch.object(update_check, "datetime", FakeDatetime)


@pytest.fixture()
def state_path(tmp_path):
    return tmp_path / "version_check.json"


class TestParseVersion:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1.2.3", (1, 2, 3)),
            ("0.0.1", (0, 0, 1)),
            ("0.4.2a1", (0, 4, 2)),
            ("1", (1,)),
            ("", ()),
            ("1.x.3", (1, 3)),
        ],
    )
    def test_cases(self, value, expected):
        assert UpdateChecker.parse_version(value) == expected


class TestEnabled:
    def test_disabled_in_editable_mode(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="editable")
        assert checker.enabled is False

    def test_disabled_when_opt_out_env_set(self, state_path, monkeypatch):
        monkeypatch.setenv("CONTREE_NO_UPDATE_CHECK", "1")
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        assert checker.enabled is False

    def test_enabled_normal(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        assert checker.enabled is True


class TestRefresh:
    def test_skips_in_editable_mode(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="editable")
        with patch.object(checker, "fetch_latest_version") as fetch:
            checker.refresh()
        fetch.assert_not_called()

    def test_skips_when_opt_out_env_set(self, state_path, monkeypatch):
        monkeypatch.setenv("CONTREE_NO_UPDATE_CHECK", "1")
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(checker, "fetch_latest_version") as fetch:
            checker.refresh()
        fetch.assert_not_called()

    def test_fetches_and_writes_when_no_cache(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(checker, "fetch_latest_version", return_value="0.4.1"):
            checker.refresh()
        data = read_json(state_path)
        assert data["latest_version"] == "0.4.1"
        assert data["current_version"] == "0.4.0"
        assert "last_check" in data

    def test_skips_network_within_interval(self, state_path):
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        seed_state(
            state_path,
            {
                "last_check": recent.isoformat(),
                "latest_version": "0.4.0",
                "current_version": "0.4.0",
            },
        )
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(checker, "fetch_latest_version") as fetch:
            checker.refresh()
        fetch.assert_not_called()

    def test_refetches_after_interval_expires(self, state_path):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
        seed_state(
            state_path,
            {
                "last_check": "2026-01-01T00:00:00+00:00",
                "latest_version": "0.4.0",
                "current_version": "0.4.0",
            },
            mtime=old_ts,
        )
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(
            checker, "fetch_latest_version", return_value="0.4.5"
        ) as fetch:
            checker.refresh()
        fetch.assert_called_once()
        assert read_json(state_path)["latest_version"] == "0.4.5"

    def test_swallows_network_failure(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(checker, "fetch_latest_version", return_value=None):
            checker.refresh()
        assert not state_path.exists()

    def test_corrupt_cache_file_is_recoverable_after_interval(self, state_path):
        """Corrupt JSON is silently overwritten once mtime expires."""
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{not json")
        old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
        os.utime(state_path, (old_ts, old_ts))

        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(
            checker, "fetch_latest_version", return_value="0.4.1"
        ) as fetch:
            checker.refresh()
        fetch.assert_called_once()
        assert read_json(state_path)["latest_version"] == "0.4.1"

    def test_refresh_does_not_log_warning(self, state_path, caplog):
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with (
            caplog.at_level(logging.WARNING, logger="contree_cli.update_check"),
            patch.object(checker, "fetch_latest_version", return_value="0.5.0"),
        ):
            checker.refresh()
        assert "available" not in caplog.text


class TestRefreshClockMock:
    """Same logic as TestRefresh, but driven by mocked ``datetime.now``.

    Works on every platform — including Windows configurations where
    ``os.utime`` may silently no-op or raise — because nothing depends
    on the filesystem's mtime resolution or write permissions.
    """

    def test_refetches_after_simulated_interval(self, state_path):
        seed_state(
            state_path,
            {
                "last_check": "2026-01-01T00:00:00+00:00",
                "latest_version": "0.4.0",
                "current_version": "0.4.0",
            },
        )
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with (
            fake_now(timedelta(days=2)),
            patch.object(
                checker, "fetch_latest_version", return_value="0.4.5"
            ) as fetch,
        ):
            checker.refresh()
        fetch.assert_called_once()
        assert read_json(state_path)["latest_version"] == "0.4.5"

    def test_skips_within_simulated_interval(self, state_path):
        seed_state(
            state_path,
            {
                "last_check": "x",
                "latest_version": "0.4.0",
                "current_version": "0.4.0",
            },
        )
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with (
            fake_now(timedelta(hours=1)),
            patch.object(checker, "fetch_latest_version") as fetch,
        ):
            checker.refresh()
        fetch.assert_not_called()


class TestIsStateFresh:
    def test_returns_true_for_freshly_written_file(self, state_path):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{}")
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        assert checker.is_state_fresh() is True

    def test_returns_false_when_missing(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        assert checker.is_state_fresh() is False

    def test_returns_false_when_clock_advanced_past_interval(self, state_path):
        """Cross-platform: simulate 2-day clock advance, no os.utime."""
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{}")
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with fake_now(timedelta(days=2)):
            assert checker.is_state_fresh() is False

    def test_returns_true_when_clock_advanced_within_interval(self, state_path):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{}")
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with fake_now(timedelta(hours=23)):
            assert checker.is_state_fresh() is True


class TestCheck:
    def test_skips_in_editable_mode(self, state_path, caplog):
        seed_state(
            state_path,
            {
                "last_check": datetime.now(timezone.utc).isoformat(),
                "latest_version": "9.9.9",
                "current_version": "editable",
            },
        )
        checker = UpdateChecker(state_path=state_path, current_version="editable")
        with caplog.at_level(logging.WARNING, logger="contree_cli.update_check"):
            checker.check()
        assert "available" not in caplog.text

    def test_skips_when_opt_out_env_set(self, state_path, caplog, monkeypatch):
        monkeypatch.setenv("CONTREE_NO_UPDATE_CHECK", "1")
        seed_state(
            state_path,
            {
                "last_check": datetime.now(timezone.utc).isoformat(),
                "latest_version": "0.5.0",
                "current_version": "0.4.0",
            },
        )
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with caplog.at_level(logging.WARNING, logger="contree_cli.update_check"):
            checker.check()
        assert "available" not in caplog.text

    def test_warns_when_cache_indicates_outdated(self, state_path, caplog):
        seed_state(
            state_path,
            {
                "last_check": datetime.now(timezone.utc).isoformat(),
                "latest_version": "0.5.0",
                "current_version": "0.4.0",
            },
        )
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with caplog.at_level(logging.WARNING, logger="contree_cli.update_check"):
            checker.check()
        assert "0.5.0" in caplog.text
        assert "0.4.0" in caplog.text

    def test_no_warning_when_up_to_date(self, state_path, caplog):
        seed_state(
            state_path,
            {
                "last_check": datetime.now(timezone.utc).isoformat(),
                "latest_version": "0.5.0",
                "current_version": "0.5.0",
            },
        )
        checker = UpdateChecker(state_path=state_path, current_version="0.5.0")
        with caplog.at_level(logging.WARNING, logger="contree_cli.update_check"):
            checker.check()
        assert "available" not in caplog.text

    def test_no_state_file_silently_returns(self, state_path, caplog):
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with caplog.at_level(logging.WARNING, logger="contree_cli.update_check"):
            checker.check()
        assert "available" not in caplog.text

    def test_check_does_not_touch_network(self, state_path):
        seed_state(
            state_path,
            {
                "last_check": datetime.now(timezone.utc).isoformat(),
                "latest_version": "0.5.0",
                "current_version": "0.4.0",
            },
        )
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(checker, "fetch_latest_version") as fetch:
            checker.check()
        fetch.assert_not_called()


class TestFetchLatestVersion:
    @staticmethod
    def fake_response(body: bytes):
        class FakeResponse:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return FakeResponse()

    def test_returns_version_on_success(self, tmp_path):
        checker = UpdateChecker(state_path=tmp_path / "v.json", current_version="0")
        body = json.dumps({"info": {"version": "1.2.3"}}).encode()
        with patch("urllib.request.urlopen", return_value=self.fake_response(body)):
            assert checker.fetch_latest_version() == "1.2.3"

    def test_returns_none_on_exception(self, tmp_path):
        checker = UpdateChecker(state_path=tmp_path / "v.json", current_version="0")
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            assert checker.fetch_latest_version() is None

    def test_returns_none_on_unexpected_payload(self, tmp_path):
        checker = UpdateChecker(state_path=tmp_path / "v.json", current_version="0")
        with patch("urllib.request.urlopen", return_value=self.fake_response(b"[]")):
            assert checker.fetch_latest_version() is None
