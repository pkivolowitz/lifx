"""TimeSourceOperator — periodic wall-clock + sun-event signals.

Publishes current time and daily sun events as bus signals on every
tick.  Night is defined astronomically: ``time:is_night`` is 1.0 from
sunset until the next sunrise for the configured latitude and longitude.
No fixed 22:00-06:00 window — the clock follows the sun.

Signals published on every tick:

- ``time:epoch``        — Unix seconds (float).
- ``time:hour_of_day``  — local hour+fraction, 0.0..24.0.
- ``time:minute``       — local minute, 0.0..59.0.
- ``time:day_of_week``  — local weekday, 0.0 (Monday) .. 6.0 (Sunday).
- ``time:sunrise_hour`` — today's sunrise as a local hour-of-day float.
- ``time:sunset_hour``  — today's sunset as a local hour-of-day float.
- ``time:is_night``     — 1.0 between sunset and sunrise, else 0.0.

Config example::

    {
        "type": "time_source",
        "name": "time",
        "latitude": 30.69,
        "longitude": -88.04,
        "tick_hz": 0.5
    }

Sunrise/sunset are computed once at startup and recomputed whenever
the local date changes (at midnight).  The underlying NOAA algorithm
lives in :mod:`solar` — the same module the schedule system uses, so
``sunset-30m`` in a schedule entry and ``time:is_night`` on the bus
agree to within a minute.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import time as _time
from datetime import date, datetime
from typing import Any, Optional, Tuple

from operators import Operator, TICK_PERIODIC
from param import Param
from solar import sun_times

logger: logging.Logger = logging.getLogger("glowup.operators.time_source")

# Default tick rate — 2-second granularity is plenty for minute logic.
DEFAULT_TICK_HZ: float = 0.5

# Default latitude / longitude — Mobile, Alabama (matches kiosk/data.py).
DEFAULT_LAT: float = 30.69
DEFAULT_LON: float = -88.04

# Polar / error fallback — 6:00 sunrise, 18:00 sunset.
FALLBACK_SUNRISE_HOUR: float = 6.0
FALLBACK_SUNSET_HOUR: float = 18.0


class TimeSourceOperator(Operator):
    """Emit ``time:*`` bus signals every tick, with sun-driven is_night."""

    operator_type: str = "time_source"
    description: str = "Wall-clock + astronomical sunrise/sunset signals"

    input_signals: list[str] = []
    output_signals: list[str] = [
        "time:epoch",
        "time:hour_of_day",
        "time:minute",
        "time:day_of_week",
        "time:sunrise_hour",
        "time:sunset_hour",
        "time:is_night",
    ]

    tick_mode: str = TICK_PERIODIC
    tick_hz: float = DEFAULT_TICK_HZ

    latitude = Param(
        DEFAULT_LAT, min=-90.0, max=90.0,
        description="Observer latitude in degrees (positive = North)",
    )
    longitude = Param(
        DEFAULT_LON, min=-180.0, max=180.0,
        description="Observer longitude in degrees (positive = East)",
    )

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        bus: Any,
    ) -> None:
        super().__init__(name, config, bus)
        # Cached sunrise/sunset for the current local date.
        self._cached_date: Optional[date] = None
        self._sunrise_hour: float = FALLBACK_SUNRISE_HOUR
        self._sunset_hour: float = FALLBACK_SUNSET_HOUR

    def on_start(self) -> None:
        """Log configuration and emit initial time signals."""
        logger.info(
            "TimeSourceOperator started — lat=%.4f lon=%.4f, %.2f Hz",
            self.latitude, self.longitude, self.tick_hz,
        )
        # Emit once immediately so consumers have values before the
        # first tick lands.
        self.on_tick(0.0)

    def on_tick(self, dt: float) -> None:
        """Compute current time values and publish all time signals to the bus."""
        now_epoch: float = _time.time()
        now: datetime = datetime.now()
        hour_f: float = (
            float(now.hour)
            + float(now.minute) / 60.0
            + float(now.second) / 3600.0
        )

        today: date = now.date()
        if today != self._cached_date:
            rise, sset = _compute_sun_hours(
                today, float(self.latitude), float(self.longitude),
            )
            self._sunrise_hour = rise
            self._sunset_hour = sset
            self._cached_date = today
            logger.info(
                "Sun events for %s: sunrise=%.2f sunset=%.2f",
                today.isoformat(), rise, sset,
            )

        self.write("time:epoch", now_epoch)
        self.write("time:hour_of_day", hour_f)
        self.write("time:minute", float(now.minute))
        self.write("time:day_of_week", float(now.weekday()))
        self.write("time:sunrise_hour", self._sunrise_hour)
        self.write("time:sunset_hour", self._sunset_hour)
        self.write(
            "time:is_night",
            _is_night(hour_f, self._sunset_hour, self._sunrise_hour),
        )


def _compute_sun_hours(
    d: date, latitude: float, longitude: float,
) -> Tuple[float, float]:
    """Return (sunrise_hour, sunset_hour) as local hour-of-day floats.

    Delegates to :func:`solar.sun_times` and converts the returned
    timezone-aware datetimes to hour floats.  On polar day/night or
    internal error, returns fallback 6/18 with a warning log.
    """
    try:
        st = sun_times(latitude, longitude, d)
    except (ValueError, ZeroDivisionError) as exc:
        logger.warning(
            "sun_times failed for %s: %s — using 6/18 fallback",
            d.isoformat(), exc,
        )
        return FALLBACK_SUNRISE_HOUR, FALLBACK_SUNSET_HOUR

    rise_hour: float = (
        _dt_to_hour(st.sunrise)
        if st.sunrise is not None else FALLBACK_SUNRISE_HOUR
    )
    set_hour: float = (
        _dt_to_hour(st.sunset)
        if st.sunset is not None else FALLBACK_SUNSET_HOUR
    )
    return rise_hour, set_hour


def _dt_to_hour(dt: datetime) -> float:
    """Convert a datetime to its local hour-of-day float."""
    return (
        float(dt.hour)
        + float(dt.minute) / 60.0
        + float(dt.second) / 3600.0
    )


def _is_night(hour: float, sunset: float, sunrise: float) -> float:
    """Return 1.0 if *hour* is between sunset and sunrise.

    Night always wraps midnight (sunset > sunrise for ordinary civil
    daylight), so this is hour >= sunset OR hour < sunrise.  The
    opposite branch handles the degenerate "sunrise > sunset" case
    (polar-adjacent latitudes) by treating the smaller interval as
    day.
    """
    if sunset > sunrise:
        return 1.0 if (hour >= sunset or hour < sunrise) else 0.0
    return 1.0 if sunrise <= hour < sunset else 0.0
