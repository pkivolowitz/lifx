"""Solar position calculator — sunrise, sunset, and twilight times.

Computes sunrise, sunset, solar noon, and civil twilight (dawn/dusk)
for any location and date using the NOAA solar position algorithm.
No external dependencies required.

Accuracy is within 1–2 minutes for latitudes between ±72°.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Julian date of the J2000.0 epoch (2000-01-01 12:00 UTC).
J2000: float = 2451545.0

# Days per Julian century.
DAYS_PER_CENTURY: float = 36525.0

# Minutes per degree of Earth rotation (360° in 24 hours = 4 min/degree).
MINUTES_PER_DEGREE: float = 4.0

# Minutes from midnight to noon.
HALF_DAY_MINUTES: float = 720.0

# Solar zenith angles for different events (degrees).
ZENITH_OFFICIAL: float = 90.833   # Standard sunrise/sunset (includes refraction)
ZENITH_CIVIL: float = 96.0       # Civil twilight (sun 6° below horizon)

# Conversion factors.
DEG_TO_RAD: float = math.pi / 180.0
RAD_TO_DEG: float = 180.0 / math.pi

# NOAA solar algorithm coefficients — geometric mean longitude of Sun.
L0_BASE: float = 280.46646
L0_RATE: float = 36000.76983
L0_ACCEL: float = 0.0003032

# NOAA solar algorithm coefficients — geometric mean anomaly of Sun.
M_BASE: float = 357.52911
M_RATE: float = 35999.05029
M_ACCEL: float = 0.0001537

# NOAA solar algorithm coefficients — eccentricity of Earth's orbit.
ECC_BASE: float = 0.016708634
ECC_RATE: float = 0.000042037
ECC_ACCEL: float = 0.0000001267

# NOAA solar algorithm coefficients — equation of center.
CENTER_C1: float = 1.914602
CENTER_C1_RATE: float = 0.004817
CENTER_C1_ACCEL: float = 0.000014
CENTER_C2: float = 0.019993
CENTER_C2_RATE: float = 0.000101
CENTER_C3: float = 0.000289

# Aberration correction for Sun's apparent longitude.
ABERRATION: float = 0.00569
NUTATION_COEFF: float = 0.00478

# Moon's ascending node longitude coefficients.
OMEGA_BASE: float = 125.04
OMEGA_RATE: float = 1934.136

# Mean obliquity of the ecliptic coefficients.
OBLIQ_BASE: float = 23.0
OBLIQ_ARCMIN: float = 26.0
OBLIQ_ARCSEC: float = 21.448
OBLIQ_RATE: float = 46.815
OBLIQ_ACCEL: float = 0.00059
OBLIQ_JERK: float = 0.001813
OBLIQ_CORRECTION: float = 0.00256

# Equation of time coefficients.
EQT_E2_COEFF: float = 1.25


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SunTimes:
    """Solar event times for a single day.

    All times are timezone-aware datetime objects in the local timezone.
    Any field may be ``None`` if the event does not occur on that date
    (e.g., during polar day or polar night).
    """

    dawn: Optional[datetime]
    sunrise: Optional[datetime]
    noon: datetime
    sunset: Optional[datetime]
    dusk: Optional[datetime]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _julian_day(d: date) -> float:
    """Convert a calendar date to Julian Day Number.

    Uses the standard Gregorian calendar algorithm.

    Args:
        d: Calendar date.

    Returns:
        Julian Day Number as a float.
    """
    y: int = d.year
    m: int = d.month
    day: int = d.day

    # For January and February, treat as months 13/14 of the previous year.
    if m <= 2:
        y -= 1
        m += 12

    a: int = y // 100
    b: int = 2 - a + a // 4

    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + day + b - 1524.5


def _julian_century(jd: float) -> float:
    """Convert Julian Day to Julian centuries elapsed since J2000.0.

    Args:
        jd: Julian Day Number.

    Returns:
        Julian centuries as a float.
    """
    return (jd - J2000) / DAYS_PER_CENTURY


def _sun_geometry(jc: float) -> tuple[float, float]:
    """Compute Sun declination and equation of time for a Julian century.

    Implements the NOAA simplified solar position algorithm.

    Args:
        jc: Julian century from J2000.0.

    Returns:
        A tuple of ``(declination_degrees, equation_of_time_minutes)``.
    """
    # Geometric mean longitude of the Sun (degrees), normalized to [0, 360).
    l0: float = (L0_BASE + jc * (L0_RATE + L0_ACCEL * jc)) % 360.0

    # Geometric mean anomaly of the Sun (degrees).
    m_deg: float = M_BASE + jc * (M_RATE - M_ACCEL * jc)
    m_rad: float = m_deg * DEG_TO_RAD

    # Eccentricity of Earth's orbit.
    ecc: float = ECC_BASE - jc * (ECC_RATE + ECC_ACCEL * jc)

    # Sun's equation of center (degrees).
    center: float = (
        math.sin(m_rad) * (CENTER_C1 - jc * (CENTER_C1_RATE + CENTER_C1_ACCEL * jc))
        + math.sin(2.0 * m_rad) * (CENTER_C2 - CENTER_C2_RATE * jc)
        + math.sin(3.0 * m_rad) * CENTER_C3
    )

    # Sun's true longitude (degrees).
    sun_lon: float = l0 + center

    # Longitude of the ascending node of the Moon's orbit (degrees).
    omega: float = OMEGA_BASE - OMEGA_RATE * jc

    # Sun's apparent longitude — corrected for aberration and nutation.
    sun_app_lon: float = (
        sun_lon - ABERRATION - NUTATION_COEFF * math.sin(omega * DEG_TO_RAD)
    )

    # Mean obliquity of the ecliptic (degrees).
    mean_obliq: float = (
        OBLIQ_BASE
        + (OBLIQ_ARCMIN
           + (OBLIQ_ARCSEC
              - jc * (OBLIQ_RATE + jc * (OBLIQ_ACCEL - jc * OBLIQ_JERK))
              ) / 60.0
           ) / 60.0
    )

    # Corrected obliquity (degrees).
    obliq: float = mean_obliq + OBLIQ_CORRECTION * math.cos(omega * DEG_TO_RAD)
    obliq_rad: float = obliq * DEG_TO_RAD

    # Sun's declination (degrees).
    decl: float = (
        math.asin(math.sin(obliq_rad) * math.sin(sun_app_lon * DEG_TO_RAD))
        * RAD_TO_DEG
    )

    # Equation of time (minutes).
    y: float = math.tan(obliq_rad / 2.0) ** 2
    l0_rad: float = l0 * DEG_TO_RAD
    eqt: float = MINUTES_PER_DEGREE * RAD_TO_DEG * (
        y * math.sin(2.0 * l0_rad)
        - 2.0 * ecc * math.sin(m_rad)
        + 4.0 * ecc * y * math.sin(m_rad) * math.cos(2.0 * l0_rad)
        - 0.5 * y * y * math.sin(4.0 * l0_rad)
        - EQT_E2_COEFF * ecc * ecc * math.sin(2.0 * m_rad)
    )

    return decl, eqt


def _hour_angle(lat: float, decl: float, zenith: float) -> Optional[float]:
    """Compute the hour angle for a given solar zenith angle.

    Args:
        lat:    Observer latitude in degrees (positive north).
        decl:   Sun declination in degrees.
        zenith: Solar zenith angle in degrees.

    Returns:
        Hour angle in degrees, or ``None`` if the Sun never reaches
        the specified zenith on this date (polar day/night).
    """
    lat_rad: float = lat * DEG_TO_RAD
    decl_rad: float = decl * DEG_TO_RAD
    cos_ha: float = (
        math.cos(zenith * DEG_TO_RAD) / (math.cos(lat_rad) * math.cos(decl_rad))
        - math.tan(lat_rad) * math.tan(decl_rad)
    )

    # cos_ha outside [-1, 1] means the Sun never reaches this zenith.
    if cos_ha < -1.0 or cos_ha > 1.0:
        return None

    return math.acos(cos_ha) * RAD_TO_DEG


def _minutes_to_datetime(
    minutes: float,
    d: date,
    utc_offset: timedelta,
) -> datetime:
    """Convert minutes-from-midnight (local time) to a timezone-aware datetime.

    Args:
        minutes:    Minutes from midnight in local time.
        d:          Calendar date.
        utc_offset: UTC offset as a timedelta.

    Returns:
        A timezone-aware datetime object.
    """
    tz: timezone = timezone(utc_offset)

    # Handle times that spill past midnight (e.g., > 1440 minutes).
    total_seconds: int = int(minutes * 60)
    extra_days: int = total_seconds // 86400
    remaining: int = total_seconds % 86400

    hours: int = remaining // 3600
    mins: int = (remaining % 3600) // 60
    secs: int = remaining % 60

    base: datetime = datetime(d.year, d.month, d.day, hours, mins, secs, tzinfo=tz)
    if extra_days:
        base += timedelta(days=extra_days)

    return base


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sun_times(
    lat: float,
    lon: float,
    d: date,
    utc_offset: Optional[timedelta] = None,
) -> SunTimes:
    """Compute sunrise, sunset, noon, dawn, and dusk for a location and date.

    Uses the NOAA solar position algorithm.  Accuracy is within 1–2 minutes
    for latitudes between ±72°.

    Args:
        lat:        Observer latitude in degrees (positive north).
        lon:        Observer longitude in degrees (positive east, negative west).
        d:          Calendar date.
        utc_offset: UTC offset as a timedelta.  If ``None``, uses the
                    system's current local timezone offset.

    Returns:
        A :class:`SunTimes` instance with dawn, sunrise, noon, sunset,
        and dusk as timezone-aware datetime objects.  Fields are ``None``
        if the event does not occur on the given date.

    Raises:
        ValueError: If latitude is outside [-90, 90].
    """
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"Latitude must be between -90 and 90, got {lat}")

    if utc_offset is None:
        utc_offset = datetime.now(timezone.utc).astimezone().utcoffset()

    offset_hours: float = utc_offset.total_seconds() / 3600.0

    jd: float = _julian_day(d)
    jc: float = _julian_century(jd)
    decl, eqt = _sun_geometry(jc)

    # Solar noon in minutes from midnight (local time).
    noon_minutes: float = (
        HALF_DAY_MINUTES - MINUTES_PER_DEGREE * lon - eqt + offset_hours * 60.0
    )
    noon_dt: datetime = _minutes_to_datetime(noon_minutes, d, utc_offset)

    # Sunrise/sunset (official zenith, includes atmospheric refraction).
    ha_official: Optional[float] = _hour_angle(lat, decl, ZENITH_OFFICIAL)
    sunrise_dt: Optional[datetime] = None
    sunset_dt: Optional[datetime] = None
    if ha_official is not None:
        rise_minutes: float = noon_minutes - ha_official * MINUTES_PER_DEGREE
        set_minutes: float = noon_minutes + ha_official * MINUTES_PER_DEGREE
        sunrise_dt = _minutes_to_datetime(rise_minutes, d, utc_offset)
        sunset_dt = _minutes_to_datetime(set_minutes, d, utc_offset)

    # Dawn/dusk (civil twilight — sun 6° below horizon).
    ha_civil: Optional[float] = _hour_angle(lat, decl, ZENITH_CIVIL)
    dawn_dt: Optional[datetime] = None
    dusk_dt: Optional[datetime] = None
    if ha_civil is not None:
        dawn_minutes: float = noon_minutes - ha_civil * MINUTES_PER_DEGREE
        dusk_minutes: float = noon_minutes + ha_civil * MINUTES_PER_DEGREE
        dawn_dt = _minutes_to_datetime(dawn_minutes, d, utc_offset)
        dusk_dt = _minutes_to_datetime(dusk_minutes, d, utc_offset)

    return SunTimes(
        dawn=dawn_dt,
        sunrise=sunrise_dt,
        noon=noon_dt,
        sunset=sunset_dt,
        dusk=dusk_dt,
    )
