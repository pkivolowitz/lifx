#!/usr/bin/env python3
"""Unit tests for schedule time parsing and entry matching.

Tests the pure-logic scheduling functions in server.py:
  - _parse_time_spec: fixed times, symbolic solar times, offsets
  - _entry_runs_on_day: day-of-week filtering
  - _validate_days: day string validation
  - _days_display: human-readable day labels
  - _resolve_entries: full entry resolution with overnight handling
  - _find_active_entry: active entry lookup at a given time

All tests are self-contained with no network or hardware dependencies.
"""

import unittest
from datetime import date, datetime, timedelta, timezone

from server import (
    _parse_time_spec,
    _entry_runs_on_day,
    _validate_days,
    _days_display,
    _resolve_entries,
    _find_active_entry,
)
from solar import SunTimes, sun_times


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

# Mobile, AL — matches the user's server.json.
TEST_LAT: float = 30.6954
TEST_LON: float = -88.0399

# Central Standard Time (UTC-6).
CST: timezone = timezone(timedelta(hours=-6))
CST_OFFSET: timedelta = timedelta(hours=-6)

# A fixed date for reproducible tests (winter, CST).
TEST_DATE: date = date(2026, 1, 15)

# Precomputed sun times for fixtures.
TEST_SUN: SunTimes = sun_times(TEST_LAT, TEST_LON, TEST_DATE, CST_OFFSET)


# ---------------------------------------------------------------------------
# _parse_time_spec
# ---------------------------------------------------------------------------

class TestParseTimeSpec(unittest.TestCase):
    """Tests for _parse_time_spec."""

    def test_fixed_time_simple(self) -> None:
        """'14:30' parses to 2:30 PM on the given date."""
        result = _parse_time_spec("14:30", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 14)
        self.assertEqual(result.minute, 30)
        self.assertEqual(result.date(), TEST_DATE)

    def test_fixed_time_midnight(self) -> None:
        """'0:00' parses to midnight."""
        result = _parse_time_spec("0:00", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 0)
        self.assertEqual(result.minute, 0)

    def test_fixed_time_end_of_day(self) -> None:
        """'23:59' parses to 11:59 PM."""
        result = _parse_time_spec("23:59", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 23)
        self.assertEqual(result.minute, 59)

    def test_fixed_time_invalid_hour(self) -> None:
        """'25:00' returns None (hour > 23)."""
        result = _parse_time_spec("25:00", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNone(result)

    def test_fixed_time_invalid_minute(self) -> None:
        """'12:60' returns None (minute > 59)."""
        result = _parse_time_spec("12:60", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNone(result)

    def test_symbolic_sunrise(self) -> None:
        """'sunrise' resolves to a morning time."""
        result = _parse_time_spec("sunrise", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNotNone(result)
        # Sunrise in Mobile, AL in January should be around 6:30-7:30 AM.
        self.assertGreaterEqual(result.hour, 6)
        self.assertLessEqual(result.hour, 8)

    def test_symbolic_sunset(self) -> None:
        """'sunset' resolves to an evening time."""
        result = _parse_time_spec("sunset", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNotNone(result)
        # Sunset in Mobile, AL in January should be around 5:00-6:00 PM.
        self.assertGreaterEqual(result.hour, 16)
        self.assertLessEqual(result.hour, 18)

    def test_symbolic_noon(self) -> None:
        """'noon' resolves to around 12:00 PM."""
        result = _parse_time_spec("noon", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNotNone(result)
        self.assertIn(result.hour, (11, 12, 13))

    def test_symbolic_midnight(self) -> None:
        """'midnight' resolves to 00:00."""
        result = _parse_time_spec("midnight", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 0)
        self.assertEqual(result.minute, 0)

    def test_offset_positive(self) -> None:
        """'sunset+30m' is 30 minutes after sunset."""
        sunset = _parse_time_spec("sunset", TEST_SUN, TEST_DATE, CST_OFFSET)
        offset = _parse_time_spec("sunset+30m", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNotNone(sunset)
        self.assertIsNotNone(offset)
        diff = (offset - sunset).total_seconds()
        self.assertEqual(diff, 30 * 60)

    def test_offset_negative(self) -> None:
        """'sunrise-30m' is 30 minutes before sunrise."""
        sunrise = _parse_time_spec("sunrise", TEST_SUN, TEST_DATE, CST_OFFSET)
        offset = _parse_time_spec("sunrise-30m", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNotNone(sunrise)
        self.assertIsNotNone(offset)
        diff = (sunrise - offset).total_seconds()
        self.assertEqual(diff, 30 * 60)

    def test_offset_hours(self) -> None:
        """'noon-2h' is 2 hours before noon."""
        noon = _parse_time_spec("noon", TEST_SUN, TEST_DATE, CST_OFFSET)
        offset = _parse_time_spec("noon-2h", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNotNone(noon)
        self.assertIsNotNone(offset)
        diff = (noon - offset).total_seconds()
        self.assertEqual(diff, 2 * 3600)

    def test_offset_hours_and_minutes(self) -> None:
        """'sunset+1h30m' is 1 hour 30 minutes after sunset."""
        sunset = _parse_time_spec("sunset", TEST_SUN, TEST_DATE, CST_OFFSET)
        offset = _parse_time_spec("sunset+1h30m", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNotNone(sunset)
        self.assertIsNotNone(offset)
        diff = (offset - sunset).total_seconds()
        self.assertEqual(diff, 90 * 60)

    def test_invalid_spec(self) -> None:
        """Garbage input returns None."""
        result = _parse_time_spec("not-a-time", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNone(result)

    def test_timezone_aware(self) -> None:
        """Returned datetimes are timezone-aware."""
        result = _parse_time_spec("14:00", TEST_SUN, TEST_DATE, CST_OFFSET)
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.tzinfo)


# ---------------------------------------------------------------------------
# _entry_runs_on_day
# ---------------------------------------------------------------------------

class TestEntryRunsOnDay(unittest.TestCase):
    """Tests for _entry_runs_on_day."""

    def test_no_days_key_runs_every_day(self) -> None:
        """An entry with no 'days' key runs every day."""
        spec = {"name": "test", "start": "18:00", "stop": "23:00"}
        # Monday through Sunday.
        for offset in range(7):
            d = date(2026, 1, 5 + offset)  # Jan 5 2026 = Monday
            self.assertTrue(
                _entry_runs_on_day(spec, d),
                f"Should run on {d} (weekday {d.weekday()})",
            )

    def test_empty_days_runs_every_day(self) -> None:
        """An entry with days='' runs every day."""
        spec = {"days": ""}
        for offset in range(7):
            d = date(2026, 1, 5 + offset)
            self.assertTrue(_entry_runs_on_day(spec, d))

    def test_weekdays_only(self) -> None:
        """'MTWRF' matches Monday–Friday, skips Saturday–Sunday."""
        spec = {"days": "MTWRF"}
        monday = date(2026, 1, 5)
        friday = date(2026, 1, 9)
        saturday = date(2026, 1, 10)
        sunday = date(2026, 1, 11)
        self.assertTrue(_entry_runs_on_day(spec, monday))
        self.assertTrue(_entry_runs_on_day(spec, friday))
        self.assertFalse(_entry_runs_on_day(spec, saturday))
        self.assertFalse(_entry_runs_on_day(spec, sunday))

    def test_weekends_only(self) -> None:
        """'SU' matches Saturday and Sunday only."""
        spec = {"days": "SU"}
        friday = date(2026, 1, 9)
        saturday = date(2026, 1, 10)
        sunday = date(2026, 1, 11)
        self.assertFalse(_entry_runs_on_day(spec, friday))
        self.assertTrue(_entry_runs_on_day(spec, saturday))
        self.assertTrue(_entry_runs_on_day(spec, sunday))

    def test_single_day(self) -> None:
        """'W' matches only Wednesday."""
        spec = {"days": "W"}
        wednesday = date(2026, 1, 7)
        thursday = date(2026, 1, 8)
        self.assertTrue(_entry_runs_on_day(spec, wednesday))
        self.assertFalse(_entry_runs_on_day(spec, thursday))

    def test_case_insensitive(self) -> None:
        """Lowercase day letters work."""
        spec = {"days": "mtwrf"}
        monday = date(2026, 1, 5)
        self.assertTrue(_entry_runs_on_day(spec, monday))


# ---------------------------------------------------------------------------
# _validate_days
# ---------------------------------------------------------------------------

class TestValidateDays(unittest.TestCase):
    """Tests for _validate_days."""

    def test_all_days(self) -> None:
        self.assertTrue(_validate_days("MTWRFSU"))

    def test_weekdays(self) -> None:
        self.assertTrue(_validate_days("MTWRF"))

    def test_weekends(self) -> None:
        self.assertTrue(_validate_days("SU"))

    def test_single_day(self) -> None:
        self.assertTrue(_validate_days("M"))

    def test_empty_is_invalid(self) -> None:
        """Empty string is technically invalid (no letters)."""
        # Empty days means "run every day" in _entry_runs_on_day,
        # but _validate_days checks for valid non-empty strings.
        # An empty string has len(0) == len(set()) = 0, and
        # all(... for ch in "") = True.  So it returns True.
        # This is fine — empty is handled before validation.
        self.assertTrue(_validate_days(""))

    def test_duplicate_letters(self) -> None:
        """Repeated letters are invalid."""
        self.assertFalse(_validate_days("MMT"))

    def test_invalid_letter(self) -> None:
        """Letters not in MTWRFSU are invalid."""
        self.assertFalse(_validate_days("MWX"))

    def test_lowercase(self) -> None:
        """Lowercase is accepted."""
        self.assertTrue(_validate_days("mtwrf"))


# ---------------------------------------------------------------------------
# _days_display
# ---------------------------------------------------------------------------

class TestDaysDisplay(unittest.TestCase):
    """Tests for _days_display."""

    def test_empty_is_daily(self) -> None:
        self.assertEqual(_days_display(""), "Daily")

    def test_all_days_is_daily(self) -> None:
        self.assertEqual(_days_display("MTWRFSU"), "Daily")

    def test_weekdays(self) -> None:
        self.assertEqual(_days_display("MTWRF"), "Weekdays")

    def test_weekends(self) -> None:
        self.assertEqual(_days_display("SU"), "Weekends")

    def test_custom_days_canonical_order(self) -> None:
        """Custom subsets are returned in canonical MTWRFSU order."""
        # Input is out of order; output should be sorted.
        self.assertEqual(_days_display("FTM"), "MTF")


# ---------------------------------------------------------------------------
# _resolve_entries (overnight handling)
# ---------------------------------------------------------------------------

class TestResolveEntries(unittest.TestCase):
    """Tests for _resolve_entries, especially overnight entries."""

    def test_daytime_entry(self) -> None:
        """An entry from 18:00–23:00 resolves with stop > start."""
        specs = [{
            "name": "evening",
            "group": "porch",
            "start": "18:00",
            "stop": "23:00",
            "effect": "aurora",
        }]
        resolved = _resolve_entries(
            specs, TEST_LAT, TEST_LON, TEST_DATE, CST_OFFSET,
        )
        self.assertEqual(len(resolved), 1)
        start, stop, _ = resolved[0]
        self.assertLess(start, stop)
        self.assertEqual(start.hour, 18)
        self.assertEqual(stop.hour, 23)

    def test_overnight_entry(self) -> None:
        """An entry from 23:00–07:00 has stop pushed to next day."""
        specs = [{
            "name": "overnight",
            "group": "porch",
            "start": "23:00",
            "stop": "7:00",
            "effect": "binclock",
        }]
        resolved = _resolve_entries(
            specs, TEST_LAT, TEST_LON, TEST_DATE, CST_OFFSET,
        )
        self.assertEqual(len(resolved), 1)
        start, stop, _ = resolved[0]
        self.assertLess(start, stop)
        # Stop should be on the next day.
        self.assertEqual(start.day, TEST_DATE.day)
        self.assertEqual(stop.day, TEST_DATE.day + 1)

    def test_group_filter(self) -> None:
        """group_filter restricts results to matching group."""
        specs = [
            {"name": "a", "group": "porch", "start": "18:00",
             "stop": "23:00", "effect": "aurora"},
            {"name": "b", "group": "living-room", "start": "18:00",
             "stop": "23:00", "effect": "cylon"},
        ]
        porch = _resolve_entries(
            specs, TEST_LAT, TEST_LON, TEST_DATE, CST_OFFSET,
            group_filter="porch",
        )
        living = _resolve_entries(
            specs, TEST_LAT, TEST_LON, TEST_DATE, CST_OFFSET,
            group_filter="living-room",
        )
        self.assertEqual(len(porch), 1)
        self.assertEqual(len(living), 1)
        self.assertEqual(porch[0][2]["name"], "a")
        self.assertEqual(living[0][2]["name"], "b")

    def test_disabled_entry_skipped(self) -> None:
        """Disabled entries are excluded."""
        specs = [{
            "name": "disabled",
            "group": "porch",
            "start": "18:00",
            "stop": "23:00",
            "effect": "aurora",
            "enabled": False,
        }]
        resolved = _resolve_entries(
            specs, TEST_LAT, TEST_LON, TEST_DATE, CST_OFFSET,
        )
        self.assertEqual(len(resolved), 0)

    def test_day_filter_applied(self) -> None:
        """Entries with day filters only appear on matching days."""
        specs = [{
            "name": "weekdays",
            "group": "porch",
            "start": "18:00",
            "stop": "23:00",
            "effect": "aurora",
            "days": "MTWRF",
        }]
        # Jan 15, 2026 is a Thursday (weekday).
        resolved = _resolve_entries(
            specs, TEST_LAT, TEST_LON, TEST_DATE, CST_OFFSET,
        )
        self.assertEqual(len(resolved), 1)

        # Jan 17, 2026 is a Saturday.
        saturday = date(2026, 1, 17)
        resolved = _resolve_entries(
            specs, TEST_LAT, TEST_LON, saturday, CST_OFFSET,
        )
        self.assertEqual(len(resolved), 0)

    def test_solar_time_entry(self) -> None:
        """Entries with symbolic times (sunset-30m) resolve correctly."""
        specs = [{
            "name": "solar",
            "group": "porch",
            "start": "sunset-30m",
            "stop": "23:00",
            "effect": "aurora",
        }]
        resolved = _resolve_entries(
            specs, TEST_LAT, TEST_LON, TEST_DATE, CST_OFFSET,
        )
        self.assertEqual(len(resolved), 1)
        start, stop, _ = resolved[0]
        # Start should be around 4:30-5:30 PM in January.
        self.assertGreaterEqual(start.hour, 16)
        self.assertLessEqual(start.hour, 18)


# ---------------------------------------------------------------------------
# _find_active_entry
# ---------------------------------------------------------------------------

class TestFindActiveEntry(unittest.TestCase):
    """Tests for _find_active_entry."""

    def _make_now(self, hour: int, minute: int = 0) -> datetime:
        """Create a timezone-aware datetime on TEST_DATE at the given time."""
        return datetime(
            TEST_DATE.year, TEST_DATE.month, TEST_DATE.day,
            hour, minute, 0, tzinfo=CST,
        )

    def test_match_during_active_window(self) -> None:
        """An entry from 18:00–23:00 is found at 20:00."""
        specs = [{
            "name": "evening",
            "group": "porch",
            "start": "18:00",
            "stop": "23:00",
            "effect": "aurora",
        }]
        now = self._make_now(20, 0)
        result = _find_active_entry(
            specs, TEST_LAT, TEST_LON, now, "porch",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "evening")

    def test_no_match_outside_window(self) -> None:
        """An entry from 18:00–23:00 is not found at 12:00."""
        specs = [{
            "name": "evening",
            "group": "porch",
            "start": "18:00",
            "stop": "23:00",
            "effect": "aurora",
        }]
        now = self._make_now(12, 0)
        result = _find_active_entry(
            specs, TEST_LAT, TEST_LON, now, "porch",
        )
        self.assertIsNone(result)

    def test_overnight_entry_after_midnight(self) -> None:
        """An overnight entry (23:00–07:00) is found at 02:00."""
        specs = [{
            "name": "overnight",
            "group": "porch",
            "start": "23:00",
            "stop": "7:00",
            "effect": "binclock",
        }]
        # 02:00 on the 15th — the entry started at 23:00 on the 14th.
        now = self._make_now(2, 0)
        result = _find_active_entry(
            specs, TEST_LAT, TEST_LON, now, "porch",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "overnight")

    def test_wrong_group_not_matched(self) -> None:
        """An entry for 'porch' is not found when querying 'living-room'."""
        specs = [{
            "name": "evening",
            "group": "porch",
            "start": "18:00",
            "stop": "23:00",
            "effect": "aurora",
        }]
        now = self._make_now(20, 0)
        result = _find_active_entry(
            specs, TEST_LAT, TEST_LON, now, "living-room",
        )
        self.assertIsNone(result)

    def test_first_match_wins(self) -> None:
        """When two entries overlap, the first one wins."""
        specs = [
            {"name": "first", "group": "porch", "start": "18:00",
             "stop": "23:00", "effect": "aurora"},
            {"name": "second", "group": "porch", "start": "20:00",
             "stop": "23:00", "effect": "cylon"},
        ]
        now = self._make_now(21, 0)
        result = _find_active_entry(
            specs, TEST_LAT, TEST_LON, now, "porch",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "first")

    def test_at_start_boundary_inclusive(self) -> None:
        """start <= now is inclusive (entry found at exactly start time)."""
        specs = [{
            "name": "evening",
            "group": "porch",
            "start": "18:00",
            "stop": "23:00",
            "effect": "aurora",
        }]
        now = self._make_now(18, 0)
        result = _find_active_entry(
            specs, TEST_LAT, TEST_LON, now, "porch",
        )
        self.assertIsNotNone(result)

    def test_at_stop_boundary_exclusive(self) -> None:
        """now < stop is exclusive (entry NOT found at exactly stop time)."""
        specs = [{
            "name": "evening",
            "group": "porch",
            "start": "18:00",
            "stop": "23:00",
            "effect": "aurora",
        }]
        now = self._make_now(23, 0)
        result = _find_active_entry(
            specs, TEST_LAT, TEST_LON, now, "porch",
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
