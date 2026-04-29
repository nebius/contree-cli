from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pytest

from contree_cli.types import FLAGS, parse_datetime, parse_interval

REF = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


class TestParseInterval:
    @pytest.mark.parametrize(
        "value, expect_kind",
        [
            ("60", "interval"),
            ("+60", "interval"),
            ("-60", "interval"),
            ("60s", "interval"),
            ("1m", "interval"),
            ("1h", "interval"),
            ("1d", "interval"),
            ("1M", "interval"),
            ("1y", "interval"),
            ("1d1h10m", "interval"),
            ("1y6M", "interval"),
            ("2026-03-03", "date"),
            ("2026/03/03", "date"),
            ("2026 03 03", "date"),
            ("2026.03.03", "date"),
            ("2026-03-03T12:34:56", "datetime"),
            ("2026-03-03 12.34.56", "datetime"),
            ("2026-03-03 12-34-56", "datetime"),
            ("2026-03-03T12/34/56", "datetime"),
            ("2026 03 03 12 34 56", "datetime"),
            ("2026/03/03T12-34:56", "datetime"),
            ("2026.03.03 12/34-56", "datetime"),
            ("2026-03-03T12.34-56", "datetime"),
            ("2026/03/03 12-34.56", "datetime"),
            ("2026 03 03 12-34.56", "datetime"),
            ("12:01:31", "time"),
            ("12:01", "time"),
            ("2024-01", "date"),
            ("2024--31", "date"),
        ],
    )
    def test_valid_intervals_and_dates(self, value: str, expect_kind: str) -> None:
        dt = parse_interval(value, now=REF)
        assert dt is not None
        if expect_kind == "interval":
            assert dt <= REF
        else:
            assert dt >= datetime(1970, 1, 1, tzinfo=timezone.utc)

    # -- None / empty input --

    @pytest.mark.parametrize("value", [None, ""])
    def test_none_and_empty_return_none(self, value: str | None) -> None:
        assert parse_interval(value) is None

    # -- Interval arithmetic --

    @pytest.mark.parametrize(
        "value, expected_seconds",
        [
            ("0", 0),
            ("0s", 0),
            ("1s", 1),
            ("90s", 90),
            ("1m", 60),
            ("5m", 300),
            ("1h", 3600),
            ("2h", 7200),
            ("1d", 86400),
            ("1M", 30 * 86400),
            ("1y", 365 * 86400),
            ("120", 120),
        ],
    )
    def test_interval_single_unit_arithmetic(
        self, value: str, expected_seconds: int
    ) -> None:
        dt = parse_interval(value, now=REF)
        assert dt == REF - timedelta(seconds=expected_seconds)

    @pytest.mark.parametrize(
        "value, expected_seconds",
        [
            ("1h30m", 3600 + 1800),
            ("2d12h", 2 * 86400 + 12 * 3600),
            ("1d1h1m1s", 86400 + 3600 + 60 + 1),
            ("1y1M1d", 365 * 86400 + 30 * 86400 + 86400),
            ("3h15m30s", 3 * 3600 + 15 * 60 + 30),
        ],
    )
    def test_interval_combined_units(self, value: str, expected_seconds: int) -> None:
        dt = parse_interval(value, now=REF)
        assert dt == REF - timedelta(seconds=expected_seconds)

    def test_zero_interval_returns_now(self) -> None:
        dt = parse_interval("0", now=REF)
        assert dt == REF

    def test_sign_is_stripped_from_interval(self) -> None:
        """Both +N and -N produce the same result (sign is stripped)."""
        dt_plus = parse_interval("+300", now=REF)
        dt_minus = parse_interval("-300", now=REF)
        assert dt_plus == dt_minus == REF - timedelta(seconds=300)

    def test_now_defaults_to_utcnow(self) -> None:
        """Without explicit now, intervals are relative to current time."""
        before = datetime.now(timezone.utc)
        dt = parse_interval("60")
        after = datetime.now(timezone.utc)
        assert dt is not None
        assert before - timedelta(seconds=60) <= dt <= after - timedelta(seconds=60)

    # -- Absolute date parsing --

    @pytest.mark.parametrize(
        "value, year, month, day, hour, minute, second",
        [
            ("2025-06-15", 2025, 6, 15, 0, 0, 0),
            ("2025-06-15T10:30:45", 2025, 6, 15, 10, 30, 45),
            ("2025-01-01T00:00:00", 2025, 1, 1, 0, 0, 0),
            ("2025-12-31T23:59:59", 2025, 12, 31, 23, 59, 59),
            ("2025", 2025, 1, 1, 0, 0, 0),
            ("2025-06", 2025, 6, 1, 0, 0, 0),
            ("2025-06-15T10:30", 2025, 6, 15, 10, 30, 0),
        ],
    )
    def test_absolute_date_exact_values(
        self,
        value: str,
        year: int,
        month: int,
        day: int,
        hour: int,
        minute: int,
        second: int,
    ) -> None:
        dt = parse_interval(value, now=REF)
        assert dt == datetime(
            year, month, day, hour, minute, second, tzinfo=timezone.utc
        )

    def test_absolute_date_ignores_now(self) -> None:
        """Absolute dates don't depend on the now parameter."""
        early = datetime(2000, 1, 1, tzinfo=timezone.utc)
        late = datetime(2030, 1, 1, tzinfo=timezone.utc)
        expected = datetime(2025, 6, 15, tzinfo=timezone.utc)
        assert parse_interval("2025-06-15", now=early) == expected
        assert parse_interval("2025-06-15", now=late) == expected

    def test_date_with_z_suffix(self) -> None:
        dt = parse_interval("2025-06-15T10:30:45Z")
        assert dt == datetime(2025, 6, 15, 10, 30, 45, tzinfo=timezone.utc)

    def test_year_only(self) -> None:
        dt = parse_interval("2025")
        assert dt == datetime(2025, 1, 1, tzinfo=timezone.utc)

    # -- Time-only parsing --

    def test_time_only_hms(self) -> None:
        dt = parse_interval("08:30:15", now=REF)
        assert dt == datetime(2025, 6, 15, 8, 30, 15, tzinfo=timezone.utc)

    def test_time_only_hm(self) -> None:
        dt = parse_interval("14:00", now=REF)
        assert dt == datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

    def test_time_only_midnight(self) -> None:
        dt = parse_interval("00:00:00", now=REF)
        assert dt == datetime(2025, 6, 15, 0, 0, 0, tzinfo=timezone.utc)

    def test_time_only_end_of_day(self) -> None:
        dt = parse_interval("23:59:59", now=REF)
        assert dt == datetime(2025, 6, 15, 23, 59, 59, tzinfo=timezone.utc)

    def test_time_only_uses_now_date(self) -> None:
        """Time-only values use the date from the now parameter."""
        ref = datetime(2020, 3, 25, 9, 0, 0, tzinfo=timezone.utc)
        dt = parse_interval("15:30", now=ref)
        assert dt == datetime(2020, 3, 25, 15, 30, 0, tzinfo=timezone.utc)

    # -- Error cases --

    def test_invalid_date_month_raises(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            parse_interval("2025-13-01")

    def test_invalid_date_day_raises(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            parse_interval("2025-02-30")

    def test_invalid_time_hour_raises(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            parse_interval("25:00")

    def test_invalid_time_minute_raises(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            parse_interval("12:61")

    def test_invalid_unit_raises(self) -> None:
        """The case _ branch is unreachable via normal input because the regex
        constrains units to [smhdMy]. Patch findall to force the branch."""
        from unittest.mock import patch

        with patch("contree_cli.types._INTERVAL_RE") as mock_re:
            mock_re.findall.return_value = [("1", "x")]
            with pytest.raises(
                argparse.ArgumentTypeError, match="Invalid interval unit"
            ):
                parse_interval("1x", now=REF)


class TestParseDatetime:
    def test_iso_basic(self) -> None:
        dt = parse_datetime("2025-06-15T10:30:00+00:00")
        assert dt == datetime(2025, 6, 15, 10, 30, 0, tzinfo=timezone.utc)

    def test_z_suffix(self) -> None:
        dt = parse_datetime("2025-06-15T10:30:00Z")
        assert dt == datetime(2025, 6, 15, 10, 30, 0, tzinfo=timezone.utc)

    def test_offset_converted_to_utc(self) -> None:
        dt = parse_datetime("2025-06-15T12:00:00+02:00")
        assert dt == datetime(2025, 6, 15, 10, 0, 0, tzinfo=timezone.utc)

    def test_naive_datetime(self) -> None:
        # fromisoformat on 3.11+ treats naive as local; just verify no crash
        dt = parse_datetime("2025-06-15T10:30:00")
        assert dt.tzinfo is not None


class TestFlags:
    def test_no_duplicate_flag_names(self) -> None:
        """Each FLAGS key must be unique (enforced by dict, but explicit)."""
        assert len(FLAGS) == len(set(FLAGS.keys()))

    def test_all_values_are_tuples(self) -> None:
        for key, val in FLAGS.items():
            assert isinstance(val, tuple), f"FLAGS[{key!r}] is not a tuple"
            assert len(val) >= 1, f"FLAGS[{key!r}] is empty"

    def test_long_flags_start_with_double_dash(self) -> None:
        for key, flags in FLAGS.items():
            for flag in flags:
                if not flag.startswith("-"):
                    continue
                if flag.startswith("--"):
                    assert len(flag) > 3, f"FLAGS[{key!r}] long flag too short: {flag}"
                else:
                    assert len(flag) == 2, (
                        f"FLAGS[{key!r}] short flag wrong length: {flag}"
                    )

    def test_no_duplicate_flags_in_registry(self) -> None:
        """Each CLI flag string must appear in only one FLAGS entry."""
        seen: dict[str, str] = {}
        for name, flags in FLAGS.items():
            for flag in flags:
                if flag in seen:
                    raise AssertionError(
                        f"Flag {flag!r} is in both"
                        f" FLAGS[{seen[flag]!r}] and"
                        f" FLAGS[{name!r}]"
                    )
                seen[flag] = name
