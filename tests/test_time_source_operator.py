"""Tests for TimeSourceOperator — periodic time + sun signals.

Test data uses a fixed Mobile, AL latitude/longitude (the LAT / LON
constants below).  The operator under test is generic — it accepts
any valid coordinate, with fallback to ``site.latitude`` /
``site.longitude`` when the operator's per-instance config doesn't
provide one.  Tests pass explicit coords so they don't depend on the
machine's site.json.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

import unittest
from datetime import date
from unittest.mock import patch

from media import SignalBus
from operators.time_source import (
    TimeSourceOperator,
    _compute_sun_hours,
    _is_night,
)


# Mobile, AL — the test ranges in TimeSourceOperatorTests are anchored
# to this latitude (sunrise < 8.0 local etc.).  Operator code itself
# carries no hardcoded location; this constant is test data only.
LAT: float = 30.69
LON: float = -88.04


class IsNightTests(unittest.TestCase):
    """Sunrise-to-sunset is day; else night."""

    def test_day_hour_is_day(self):
        # sunset 18.5, sunrise 6.25 → noon is day.
        self.assertEqual(_is_night(12.0, 18.5, 6.25), 0.0)

    def test_sunset_hour_is_night(self):
        self.assertEqual(_is_night(18.5, 18.5, 6.25), 1.0)
        self.assertEqual(_is_night(23.0, 18.5, 6.25), 1.0)

    def test_before_sunrise_is_night(self):
        self.assertEqual(_is_night(5.0, 18.5, 6.25), 1.0)

    def test_sunrise_hour_is_day(self):
        self.assertEqual(_is_night(6.25, 18.5, 6.25), 0.0)


class ComputeSunHoursTests(unittest.TestCase):
    """_compute_sun_hours delegates to solar.sun_times — sanity check
    against Mobile, AL on the equinox."""

    def test_sunrise_before_sunset(self):
        rise, sset = _compute_sun_hours(date(2026, 3, 20), LAT, LON)
        self.assertGreater(sset, rise)

    def test_mobile_equinox_sunrise_in_morning(self):
        rise, _ = _compute_sun_hours(date(2026, 3, 20), LAT, LON)
        # Equinox sunrise is near 06:00 ±1 hour regardless of DST offset.
        self.assertGreaterEqual(rise, 5.0)
        self.assertLess(rise, 8.0)

    def test_mobile_equinox_sunset_in_evening(self):
        _, sset = _compute_sun_hours(date(2026, 3, 20), LAT, LON)
        self.assertGreater(sset, 17.0)
        self.assertLess(sset, 20.0)


class TimeSourceOperatorTests(unittest.TestCase):

    # Explicit per-instance coords for every operator construction —
    # otherwise the operator falls back to site.latitude / longitude
    # and the test depends on whatever's in /etc/glowup/site.json on
    # the host, which CI / fresh dev machines may not have.
    _MOBILE: dict = {"latitude": LAT, "longitude": LON}

    def test_publishes_all_signals(self):
        bus = SignalBus()
        op = TimeSourceOperator("time", self._MOBILE, bus)
        op.on_tick(0.0)
        for sig in [
            "time:epoch", "time:hour_of_day", "time:minute",
            "time:day_of_week", "time:sunrise_hour", "time:sunset_hour",
            "time:is_night",
        ]:
            self.assertIsNotNone(bus.read(sig, None),
                                 f"signal {sig} should be present")
        self.assertIn(bus.read("time:is_night"), (0.0, 1.0))

    def test_sun_signals_in_reasonable_range(self):
        """At Mobile, AL, sunrise is always < 8.0 local and sunset > 16.0."""
        bus = SignalBus()
        op = TimeSourceOperator("time", self._MOBILE, bus)
        op.on_tick(0.0)
        rise = bus.read("time:sunrise_hour")
        sset = bus.read("time:sunset_hour")
        self.assertGreaterEqual(rise, 4.0)
        self.assertLess(rise, 9.0)
        self.assertGreaterEqual(sset, 16.0)
        self.assertLess(sset, 21.0)
        self.assertGreater(sset, rise)

    def test_custom_location_survives(self):
        """A different lat/lon still produces valid sun hours."""
        bus = SignalBus()
        # Seattle, WA.
        op = TimeSourceOperator(
            "time",
            {"latitude": 47.61, "longitude": -122.33},
            bus,
        )
        op.on_tick(0.0)
        rise = bus.read("time:sunrise_hour")
        sset = bus.read("time:sunset_hour")
        self.assertGreater(sset, rise)


if __name__ == "__main__":
    unittest.main()
