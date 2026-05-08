from __future__ import annotations

import json
import logging
import time
from unittest.mock import patch

import pytest

from contree_cli.update_check import UpdateChecker


def read_json(path):
    return json.loads(path.read_text())


def seed_state(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


@pytest.fixture()
def state_path(tmp_path):
    return tmp_path / "version_check.json"


HOUR = 3600.0
DAY = 86400.0


class TestParseVersion:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1.2.3", ((1, 1), (2, 1), (3, 1))),
            ("0.0.1", ((0, 1), (0, 1), (1, 1))),
            ("0.4.2a1", ((0, 1), (4, 1), (2, 0))),
            ("1", ((1, 1),)),
            ("", ()),
            ("1.x.3", ((1, 1), (3, 1))),
            ("v1.2.3", ((1, 1), (2, 1), (3, 1))),
            ("1.0.0-rc.1", ((1, 1), (0, 1), (0, 0), (1, 1))),
        ],
    )
    def test_cases(self, value, expected):
        checker = UpdateChecker(state_path="/dev/null", current_version="0")
        assert checker.parse_version(value) == expected

    def test_pre_release_sorts_before_release(self):
        checker = UpdateChecker(state_path="/dev/null", current_version="0")
        assert checker.parse_version("0.4.2a1") < checker.parse_version("0.4.2")

    def test_higher_release_sorts_after_pre_release(self):
        checker = UpdateChecker(state_path="/dev/null", current_version="0")
        assert checker.parse_version("0.4.2") < checker.parse_version("0.4.21")

    def test_rc_sorts_before_release(self):
        checker = UpdateChecker(state_path="/dev/null", current_version="0")
        assert checker.parse_version("1.0.0-rc.1") < checker.parse_version("1.0.0")


class TestEnabled:
    def test_disabled_in_editable_mode(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="editable")
        assert checker.enabled is False

    def test_disabled_when_opt_out_env_set(self, state_path, monkeypatch):
        monkeypatch.setenv("CONTREE_NO_UPDATE_CHECK", "1")
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        assert checker.enabled is False

    def test_disabled_when_opt_out_env_set_to_empty(self, state_path, monkeypatch):
        """Presence (any value, even empty) opts out, per documented contract."""
        monkeypatch.setenv("CONTREE_NO_UPDATE_CHECK", "")
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

    def test_returns_false_for_non_numeric_last_check(self):
        """Legacy ISO strings or garbage values are treated as stale."""
        checker = UpdateChecker(state_path="/dev/null", current_version="0")
        assert checker.is_cache_fresh({"last_check": "2026-05-08T00:00:00"}) is False
        assert checker.is_cache_fresh({"last_check": None}) is False

    def test_returns_true_for_recent_last_check(self):
        checker = UpdateChecker(state_path="/dev/null", current_version="0")
        assert checker.is_cache_fresh({"last_check": time.time() - HOUR}) is True

    def test_returns_false_for_old_last_check(self):
        checker = UpdateChecker(state_path="/dev/null", current_version="0")
        assert checker.is_cache_fresh({"last_check": time.time() - 2 * DAY}) is False


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
        assert isinstance(data["last_check"], (int, float))

    def test_skips_network_within_interval(self, state_path):
        seed_state(
            state_path,
            {
                "last_check": time.time() - HOUR,
                "latest_version": "0.5.0",
                "current_version": "0.4.0",
            },
        )
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(checker, "fetch_latest_version") as fetch:
            checker.refresh()
        fetch.assert_not_called()
        assert checker.latest_version == "0.5.0"

    def test_refetches_after_interval_expires(self, state_path):
        seed_state(
            state_path,
            {
                "last_check": time.time() - 2 * DAY,
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

    def test_network_failure_keeps_cached_value(self, state_path):
        seed_state(
            state_path,
            {
                "last_check": time.time() - 2 * DAY,
                "latest_version": "0.4.0",
                "current_version": "0.4.0",
            },
        )
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        with patch.object(checker, "fetch_latest_version", return_value=None):
            checker.refresh()
        assert checker.latest_version == "0.4.0"
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

    def test_legacy_iso_last_check_is_discarded(self, state_path):
        """An old cache file written before the epoch migration is treated
        as missing entirely — read_state rejects the whole record."""
        seed_state(
            state_path,
            {
                "last_check": "2026-05-08T12:00:00+00:00",
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
        # Discarded state -> nothing carried over -> only freshly fetched
        # value remains.
        assert read_json(state_path)["latest_version"] == "0.4.5"
        assert checker.latest_version == "0.4.5"

    def test_wrong_typed_latest_version_is_discarded(self, state_path):
        seed_state(
            state_path,
            {
                "last_check": time.time() - 2 * DAY,
                "latest_version": 42,  # not a string
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
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        assert checker.is_latest() is True

    def test_returns_true_when_current_is_newer(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="0.6.0")
        checker.latest_version = "0.5.0"
        assert checker.is_latest() is True

    def test_returns_true_when_current_is_pre_release_of_same(self, state_path):
        """Pre-release of the same release is "older", so warns to upgrade."""
        checker = UpdateChecker(state_path=state_path, current_version="0.5.0a1")
        checker.latest_version = "0.5.0"
        assert checker.is_latest() is False

    def test_returns_true_when_latest_is_pre_release_of_same(self, state_path):
        """If pypi only knows a pre-release, an installed stable is fine."""
        checker = UpdateChecker(state_path=state_path, current_version="0.5.0")
        checker.latest_version = "0.5.0a1"
        assert checker.is_latest() is True

    def test_does_not_touch_filesystem(self, state_path):
        checker = UpdateChecker(state_path=state_path, current_version="0.4.0")
        checker.latest_version = "0.5.0"
        with patch.object(UpdateChecker, "read_state") as read:
            checker.is_latest()
        read.assert_not_called()

    def test_does_not_log(self, state_path, caplog):
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
