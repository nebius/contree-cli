from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from contree_cli import update_check
from contree_cli.update_check import UpdateChecker


def read_json(path):
    return json.loads(path.read_text())


def seed_state(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def fake_now(offset: timedelta):
    """Patch update_check.datetime.now to return real-now + offset."""
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
            ("0.4.2a1", (0, 4, 21)),
            ("1", (1,)),
            ("", ()),
            ("1.x.3", (1, 3)),
            ("v1.2.3", (1, 2, 3)),
            ("1.0.0-rc.1", (1, 0, 0, 1)),
        ],
    )
    def test_cases(self, value, expected):
        checker = UpdateChecker(state_path="/dev/null", current_version="0")
        assert checker.parse_version(value) == expected


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


class TestIsCacheFresh:
    def test_returns_false_for_empty_state(self):
        checker = UpdateChecker(state_path="/dev/null", current_version="0")
        assert checker.is_cache_fresh({}) is False

    def test_returns_false_for_missing_last_check(self):
        checker = UpdateChecker(state_path="/dev/null", current_version="0")
        assert checker.is_cache_fresh({"latest_version": "1.0.0"}) is False

    def test_returns_false_for_unparseable_last_check(self):
        checker = UpdateChecker(state_path="/dev/null", current_version="0")
        assert checker.is_cache_fresh({"last_check": "not-a-timestamp"}) is False

    def test_returns_true_for_recent_last_check(self):
        checker = UpdateChecker(state_path="/dev/null", current_version="0")
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert checker.is_cache_fresh({"last_check": recent}) is True

    def test_returns_false_for_old_last_check(self):
        checker = UpdateChecker(state_path="/dev/null", current_version="0")
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        assert checker.is_cache_fresh({"last_check": old}) is False


class TestRefresh:
    def test_skips_in_editable_mode(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="editable")
        with patch.object(checker, "fetch_latest_version") as fetch:
            checker.refresh()
        fetch.assert_not_called()
        assert checker.latest_version is None

    def test_skips_when_opt_out_env_set(self, state_path, monkeypatch):
        monkeypatch.setenv("CONTREE_NO_UPDATE_CHECK", "1")
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(checker, "fetch_latest_version") as fetch:
            checker.refresh()
        fetch.assert_not_called()
        assert checker.latest_version is None

    def test_fetches_and_writes_when_no_cache(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(checker, "fetch_latest_version", return_value="0.4.1"):
            checker.refresh()
        assert checker.latest_version == "0.4.1"
        data = read_json(state_path)
        assert data["latest_version"] == "0.4.1"
        assert data["current_version"] == "0.4.0"
        assert "last_check" in data

    def test_skips_network_within_interval(self, state_path):
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        seed_state(
            state_path,
            {
                "last_check": recent,
                "latest_version": "0.5.0",
                "current_version": "0.4.0",
            },
        )
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(checker, "fetch_latest_version") as fetch:
            checker.refresh()
        fetch.assert_not_called()
        # Cached value loaded into self.
        assert checker.latest_version == "0.5.0"

    def test_refetches_after_interval_expires(self, state_path):
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        seed_state(
            state_path,
            {
                "last_check": old,
                "latest_version": "0.4.0",
                "current_version": "0.4.0",
            },
        )
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(
            checker, "fetch_latest_version", return_value="0.4.5"
        ) as fetch:
            checker.refresh()
        fetch.assert_called_once()
        assert checker.latest_version == "0.4.5"
        assert read_json(state_path)["latest_version"] == "0.4.5"

    def test_refetches_via_clock_mock(self, state_path):
        """Cross-platform verification using mocked clock."""
        seed_state(
            state_path,
            {
                "last_check": datetime.now(timezone.utc).isoformat(),
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
        assert checker.latest_version == "0.4.5"

    def test_network_failure_keeps_cached_value(self, state_path):
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        seed_state(
            state_path,
            {
                "last_check": old,
                "latest_version": "0.4.0",
                "current_version": "0.4.0",
            },
        )
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(checker, "fetch_latest_version", return_value=None):
            checker.refresh()
        assert checker.latest_version == "0.4.0"
        # State file untouched (no rewrite).
        assert read_json(state_path)["latest_version"] == "0.4.0"

    def test_network_failure_with_no_cache_leaves_latest_none(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(checker, "fetch_latest_version", return_value=None):
            checker.refresh()
        assert checker.latest_version is None
        assert not state_path.exists()

    def test_corrupt_cache_is_overwritten(self, state_path):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{not json")
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(
            checker, "fetch_latest_version", return_value="0.4.1"
        ) as fetch:
            checker.refresh()
        fetch.assert_called_once()
        assert checker.latest_version == "0.4.1"
        assert read_json(state_path)["latest_version"] == "0.4.1"

    def test_refresh_does_not_log_warning(self, state_path, caplog):
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with (
            caplog.at_level(logging.WARNING, logger="contree_cli.update_check"),
            patch.object(checker, "fetch_latest_version", return_value="0.5.0"),
        ):
            checker.refresh()
        assert "available" not in caplog.text


class TestIsLatest:
    def test_returns_true_in_editable_mode(self, state_path):
        """Editable installs are always considered up to date."""
        checker = UpdateChecker(state_path=state_path, current_version="editable")
        checker.latest_version = "9.9.9"
        assert checker.is_latest() is True

    def test_returns_true_when_opt_out_env_set(self, state_path, monkeypatch):
        monkeypatch.setenv("CONTREE_NO_UPDATE_CHECK", "1")
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        checker.latest_version = "9.9.9"
        assert checker.is_latest() is True

    def test_returns_false_when_outdated(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        checker.latest_version = "0.5.0"
        assert checker.is_latest() is False

    def test_returns_true_when_up_to_date(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="0.5.0")
        checker.latest_version = "0.5.0"
        assert checker.is_latest() is True

    def test_returns_true_when_latest_unknown(self, state_path):
        """Unknown latest defaults to "up to date" so callers don't warn."""
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        assert checker.is_latest() is True

    def test_returns_true_when_current_is_newer(self, state_path):
        """Pre-release/dev install ahead of pypi is "latest"."""
        checker = UpdateChecker(state_path=state_path, current_version="0.6.0")
        checker.latest_version = "0.5.0"
        assert checker.is_latest() is True

    def test_does_not_touch_filesystem(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        checker.latest_version = "0.5.0"
        with patch.object(UpdateChecker, "read_state") as read:
            checker.is_latest()
        read.assert_not_called()

    def test_does_not_log(self, state_path, caplog):
        """is_latest() is a pure predicate; logging is the caller's job."""
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        checker.latest_version = "0.5.0"
        with caplog.at_level(logging.WARNING, logger="contree_cli.update_check"):
            assert checker.is_latest() is False
        assert caplog.records == []


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
