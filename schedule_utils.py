"""Shared schedule resolution utilities.

Time parsing, day-of-week filtering, schedule resolution, and active
entry lookup used by both ``server.py`` and ``scheduler.py``.
Extracted to break circular imports (server ↔ automation ↔ trigger).

All functions are pure — no side effects, no global state, no imports
from server.py or scheduler.py.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from solar import SunTimes, sun_times

logger: logging.Logger = logging.getLogger("glowup.schedule_utils")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Regex for symbolic time specifications.
# Matches: "sunrise", "sunset+30m", "noon-1h30m", "midnight+2h", etc.
SYMBOLIC_RE: re.Pattern[str] = re.compile(
    r"^(sunrise|sunset|dawn|dusk|noon|midnight)"
    r"(?:([+-])"
    r"(?:(\d+)h)?"
    r"(?:(\d+)m)?"
    r")?$"
)

# Regex for fixed HH:MM time specifications.
FIXED_TIME_RE: re.Pattern[str] = re.compile(r"^(\d{1,2}):(\d{2})$")

# Valid hours/minutes range.
MAX_HOUR: int = 23
MAX_MINUTE: int = 59

# Day-of-week letter to weekday index (Monday=0 .. Sunday=6).
# Matches Python's date.weekday() convention.
DAY_LETTER_TO_WEEKDAY: dict[str, int] = {
    "M": 0, "T": 1, "W": 2, "R": 3, "F": 4, "S": 5, "U": 6,
}

# All valid day letters (for validation).
VALID_DAY_LETTERS: str = "MTWRFSU"


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

def parse_time_spec(
    spec: str,
    sun: SunTimes,
    d: date,
    utc_offset: timedelta,
) -> Optional[datetime]:
    """Parse a time specification into a timezone-aware datetime.

    Supports three formats:

    - **Fixed times**: ``"14:30"``, ``"06:00"``
    - **Symbolic times**: ``"sunrise"``, ``"sunset"``, ``"dawn"``,
      ``"dusk"``, ``"noon"``, ``"midnight"``
    - **Symbolic with offsets**: ``"sunset+30m"``, ``"sunrise-1h"``,
      ``"dawn+1h30m"``

    Args:
        spec:       The time specification string.
        sun:        Precomputed solar event times for date *d*.
        d:          Calendar date for resolving the time.
        utc_offset: Local UTC offset as a timedelta.

    Returns:
        A timezone-aware datetime, or ``None`` if the symbolic sun event
        does not occur on this date (polar day/night).
    """
    tz: timezone = timezone(utc_offset)

    # Try fixed time first (e.g., "14:30").
    match = FIXED_TIME_RE.match(spec)
    if match:
        hours: int = int(match.group(1))
        mins: int = int(match.group(2))
        if hours > MAX_HOUR or mins > MAX_MINUTE:
            logger.error("Invalid fixed time: %s", spec)
            return None
        return datetime(d.year, d.month, d.day, hours, mins, 0, tzinfo=tz)

    # Try symbolic time (e.g., "sunset+30m").
    match = SYMBOLIC_RE.match(spec)
    if not match:
        logger.error("Invalid time specification: %r", spec)
        return None

    symbol: str = match.group(1)
    sign: Optional[str] = match.group(2)
    offset_hours: int = int(match.group(3) or 0)
    offset_mins: int = int(match.group(4) or 0)

    # Resolve symbolic name to a datetime.
    base_time: Optional[datetime] = None
    if symbol == "midnight":
        base_time = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)
    elif symbol == "noon":
        base_time = sun.noon
    elif symbol == "sunrise":
        base_time = sun.sunrise
    elif symbol == "sunset":
        base_time = sun.sunset
    elif symbol == "dawn":
        base_time = sun.dawn
    elif symbol == "dusk":
        base_time = sun.dusk

    if base_time is None:
        logger.warning(
            "Sun event '%s' does not occur on %s (polar day/night?)",
            symbol, d,
        )
        return None

    # Apply offset.
    if sign:
        delta: timedelta = timedelta(hours=offset_hours, minutes=offset_mins)
        if sign == "-":
            delta = -delta
        base_time = base_time + delta

    return base_time


# ---------------------------------------------------------------------------
# Day-of-week filtering
# ---------------------------------------------------------------------------

def entry_runs_on_day(spec: dict[str, Any], d: date) -> bool:
    """Check whether a schedule entry runs on a given calendar date.

    If the ``days`` key is absent or empty, the entry runs every day.
    Otherwise it must be a string of day letters from ``MTWRFSU``
    (Monday through Sunday, academic convention).

    Args:
        spec: Schedule entry dict (may contain a ``days`` key).
        d:    Calendar date to check.

    Returns:
        ``True`` if the entry should run on date *d*.
    """
    days_str: str = spec.get("days", "")
    if not days_str:
        return True
    weekday: int = d.weekday()
    for letter, idx in DAY_LETTER_TO_WEEKDAY.items():
        if idx == weekday:
            return letter in days_str.upper()
    return False


def validate_days(days_str: str) -> bool:
    """Validate a day-of-week string.

    Args:
        days_str: String of day letters (e.g. ``"MTWRF"``).

    Returns:
        ``True`` if all characters are valid day letters with no repeats.
    """
    upper: str = days_str.upper()
    return (
        all(ch in VALID_DAY_LETTERS for ch in upper)
        and len(upper) == len(set(upper))
    )


def days_display(days_str: str) -> str:
    """Format a days string for human display.

    Args:
        days_str: Day letter string (e.g. ``"MTWRF"``).

    Returns:
        A display string like ``"Weekdays"``, ``"Weekends"``, ``"Daily"``,
        or the sorted letter string.
    """
    if not days_str:
        return "Daily"
    upper: str = days_str.upper()
    canonical: str = "".join(ch for ch in VALID_DAY_LETTERS if ch in upper)
    if canonical == VALID_DAY_LETTERS:
        return "Daily"
    if canonical == "MTWRF":
        return "Weekdays"
    if canonical == "SU":
        return "Weekends"
    return canonical


# ---------------------------------------------------------------------------
# Schedule resolution
# ---------------------------------------------------------------------------

def resolve_entries(
    specs: list[dict[str, Any]],
    lat: float,
    lon: float,
    d: date,
    utc_offset: timedelta,
    group_filter: Optional[str] = None,
) -> list[tuple[datetime, datetime, dict[str, Any]]]:
    """Resolve schedule entries for a specific date.

    Each entry in *specs* has ``start``, ``stop``, ``effect``, ``group``,
    and optional ``params`` keys.  Symbolic times are resolved against
    sun positions for date *d*.

    Args:
        specs:        List of raw schedule entry dicts from the config file.
        lat:          Observer latitude in degrees.
        lon:          Observer longitude in degrees.
        d:            Calendar date for sun time resolution.
        utc_offset:   Local UTC offset.
        group_filter: If set, only include entries matching this group name.

    Returns:
        A list of ``(start_datetime, stop_datetime, spec_dict)`` tuples.
        Entries where start or stop could not be resolved are omitted.
    """
    sun: SunTimes = sun_times(lat, lon, d, utc_offset)
    resolved: list[tuple[datetime, datetime, dict[str, Any]]] = []

    for spec in specs:
        if group_filter is not None and spec.get("group") != group_filter:
            continue
        if not spec.get("enabled", True):
            continue
        if not entry_runs_on_day(spec, d):
            continue

        start: Optional[datetime] = parse_time_spec(
            spec["start"], sun, d, utc_offset,
        )
        stop: Optional[datetime] = parse_time_spec(
            spec["stop"], sun, d, utc_offset,
        )

        if start is None or stop is None:
            logger.warning(
                "Skipping entry '%s': could not resolve times",
                spec.get("name", "?"),
            )
            continue

        # Overnight entries: stop before start → add a day.
        if stop < start:
            stop += timedelta(days=1)

        resolved.append((start, stop, spec))

    return resolved


def find_active_entry(
    specs: list[dict[str, Any]],
    lat: float,
    lon: float,
    now: datetime,
    group_name: str,
) -> Optional[dict[str, Any]]:
    """Find the first schedule entry active for a group at time *now*.

    Checks resolved schedules for both today and yesterday to correctly
    handle overnight entries that started yesterday and extend past midnight.

    When multiple entries overlap for the same group, config file order
    determines priority — the first matching entry wins.

    Args:
        specs:      Raw schedule entry dicts (order = priority).
        lat:        Observer latitude in degrees.
        lon:        Observer longitude in degrees.
        now:        Current timezone-aware datetime.
        group_name: Only consider entries targeting this group.

    Returns:
        The matching spec dict, or ``None`` if no entry is active.
    """
    utc_offset: timedelta = now.utcoffset()
    today: date = now.date()
    yesterday: date = today - timedelta(days=1)

    today_resolved = resolve_entries(
        specs, lat, lon, today, utc_offset, group_filter=group_name,
    )
    yesterday_resolved = resolve_entries(
        specs, lat, lon, yesterday, utc_offset, group_filter=group_name,
    )

    for start, stop, spec in today_resolved:
        if start <= now < stop:
            return spec

    for start, stop, spec in yesterday_resolved:
        if start <= now < stop:
            return spec

    return None
