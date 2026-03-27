#!/usr/bin/env python3
"""Unit tests for solar time calculations.

Verifies that sun_times() produces reasonable sunrise/sunset/dawn/dusk
values for known locations and dates.  Tests:
  - Test location (mid-latitude) — year-round sun events
  - New York City — well-known reference point
  - Tromso, Norway — polar night / midnight sun edge cases
  - Equator — minimal seasonal variation
  - SunTimes fields are timezone-aware datetimes
  - Latitude validation (rejects out-of-range values)

No network or hardware dependencies — pure math.
"""

import unittest
from datetime import date, timedelta, timezone

from solar import sun_times, SunTimes


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Madison, WI (test location).
MADISON_LAT: float = 43.0731
MADISON_LON: float = -89.4012
CST_OFFSET: timedelta = timedelta(hours=-6)

# New York City.
NYC_LAT: float = 40.7128
NYC_LON: float = -74.0060
EST_OFFSET: timedelta = timedelta(hours=-5)

# Tromso, Norway (above the Arctic Circle).
TROMSO_LAT: float = 69.6496
TROMSO_LON: float = 18.9560
CET_OFFSET: timedelta = timedelta(hours=1)

# Equator (Quito, Ecuador).
QUITO_LAT: float = -0.1807
QUITO_LON: float = -78.4678
ECT_OFFSET: timedelta = timedelta(hours=-5)


# ---------------------------------------------------------------------------
# Mid-latitude tests (Madison, WI)
# ---------------------------------------------------------------------------

class TestMidLatitude(unittest.TestCase):
    """Sun times for a mid-latitude US location."""

    def test_winter_solstice(self) -> None:
        """Dec 21: shortest day of the year in Northern Hemisphere."""
        d = date(2026, 12, 21)
        st = sun_times(MADISON_LAT, MADISON_LON, d, CST_OFFSET)

        # All events should be present at this latitude.
        self.assertIsNotNone(st.dawn)
        self.assertIsNotNone(st.sunrise)
        self.assertIsNotNone(st.noon)
        self.assertIsNotNone(st.sunset)
        self.assertIsNotNone(st.dusk)

        # Sunrise around 7:00–8:00 AM CST.
        self.assertGreaterEqual(st.sunrise.hour, 6)
        self.assertLessEqual(st.sunrise.hour, 8)

        # Sunset around 4:15–5:00 PM CST.
        self.assertGreaterEqual(st.sunset.hour, 16)
        self.assertLessEqual(st.sunset.hour, 18)

    def test_summer_solstice(self) -> None:
        """Jun 21: longest day of the year in Northern Hemisphere."""
        d = date(2026, 6, 21)
        st = sun_times(MADISON_LAT, MADISON_LON, d, CST_OFFSET)

        self.assertIsNotNone(st.sunrise)
        self.assertIsNotNone(st.sunset)

        # Sunrise around 4:15–5:15 AM CST.
        self.assertGreaterEqual(st.sunrise.hour, 4)
        self.assertLessEqual(st.sunrise.hour, 7)

        # Sunset around 7:30–9:00 PM CST.
        self.assertGreaterEqual(st.sunset.hour, 18)
        self.assertLessEqual(st.sunset.hour, 21)

    def test_event_ordering(self) -> None:
        """Dawn < sunrise < noon < sunset < dusk."""
        d = date(2026, 3, 20)  # Equinox.
        st = sun_times(MADISON_LAT, MADISON_LON, d, CST_OFFSET)

        self.assertLess(st.dawn, st.sunrise)
        self.assertLess(st.sunrise, st.noon)
        self.assertLess(st.noon, st.sunset)
        self.assertLess(st.sunset, st.dusk)

    def test_noon_around_midday(self) -> None:
        """Solar noon should be roughly 12:00–13:30 local time."""
        d = date(2026, 6, 1)
        st = sun_times(MADISON_LAT, MADISON_LON, d, CST_OFFSET)

        self.assertIsNotNone(st.noon)
        self.assertIn(st.noon.hour, (11, 12, 13))


# ---------------------------------------------------------------------------
# NYC reference point
# ---------------------------------------------------------------------------

class TestNewYorkCity(unittest.TestCase):
    """Sun times for NYC — well-known reference values."""

    def test_equinox(self) -> None:
        """Mar 20: roughly equal day and night."""
        d = date(2026, 3, 20)
        st = sun_times(NYC_LAT, NYC_LON, d, EST_OFFSET)

        self.assertIsNotNone(st.sunrise)
        self.assertIsNotNone(st.sunset)

        # Day length should be close to 12 hours (±30 min).
        day_length = (st.sunset - st.sunrise).total_seconds() / 3600
        self.assertGreater(day_length, 11.5)
        self.assertLess(day_length, 12.5)


# ---------------------------------------------------------------------------
# Polar edge cases (Tromso, Norway)
# ---------------------------------------------------------------------------

class TestTromsoNorway(unittest.TestCase):
    """Sun times for Tromso — polar night and midnight sun."""

    def test_polar_night(self) -> None:
        """Dec 21 in Tromso: sun never rises (polar night)."""
        d = date(2026, 12, 21)
        st = sun_times(TROMSO_LAT, TROMSO_LON, d, CET_OFFSET)

        # Sunrise and sunset should be None during polar night.
        self.assertIsNone(st.sunrise)
        self.assertIsNone(st.sunset)

        # Noon should always be present.
        self.assertIsNotNone(st.noon)

    def test_midnight_sun(self) -> None:
        """Jun 21 in Tromso: sun never sets (midnight sun)."""
        d = date(2026, 6, 21)
        st = sun_times(TROMSO_LAT, TROMSO_LON, d, CET_OFFSET)

        # Sunrise and sunset should be None during midnight sun.
        self.assertIsNone(st.sunrise)
        self.assertIsNone(st.sunset)

        # Noon is always present.
        self.assertIsNotNone(st.noon)


# ---------------------------------------------------------------------------
# Equator (minimal seasonal variation)
# ---------------------------------------------------------------------------

class TestEquator(unittest.TestCase):
    """Sun times at the equator — roughly 12-hour days year-round."""

    def test_day_length_stable(self) -> None:
        """Day length should be close to 12 hours in both June and Dec."""
        for month in (6, 12):
            d = date(2026, month, 21)
            st = sun_times(QUITO_LAT, QUITO_LON, d, ECT_OFFSET)

            self.assertIsNotNone(st.sunrise)
            self.assertIsNotNone(st.sunset)

            day_length = (st.sunset - st.sunrise).total_seconds() / 3600
            self.assertGreater(
                day_length, 11.5,
                f"Day too short in month {month}: {day_length:.1f}h",
            )
            self.assertLess(
                day_length, 12.5,
                f"Day too long in month {month}: {day_length:.1f}h",
            )


# ---------------------------------------------------------------------------
# General properties
# ---------------------------------------------------------------------------

class TestSunTimesProperties(unittest.TestCase):
    """General properties that should hold for any location/date."""

    def test_timezone_aware(self) -> None:
        """All returned datetimes should be timezone-aware."""
        d = date(2026, 6, 1)
        st = sun_times(MADISON_LAT, MADISON_LON, d, CST_OFFSET)

        for field_name in ("dawn", "sunrise", "noon", "sunset", "dusk"):
            value = getattr(st, field_name)
            if value is not None:
                self.assertIsNotNone(
                    value.tzinfo,
                    f"{field_name} is not timezone-aware",
                )

    def test_noon_always_present(self) -> None:
        """Solar noon is always present, even during polar night."""
        d = date(2026, 12, 21)
        st = sun_times(TROMSO_LAT, TROMSO_LON, d, CET_OFFSET)
        self.assertIsNotNone(st.noon)

    def test_invalid_latitude_raises(self) -> None:
        """Latitude outside [-90, 90] should raise ValueError."""
        d = date(2026, 6, 1)
        with self.assertRaises(ValueError):
            sun_times(91.0, 0.0, d, CST_OFFSET)
        with self.assertRaises(ValueError):
            sun_times(-91.0, 0.0, d, CST_OFFSET)


if __name__ == "__main__":
    unittest.main()
