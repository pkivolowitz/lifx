"""Weather and air-quality data sources with automatic failover.

Two independent providers back the voice coordinator's weather /
forecast handlers:

- :class:`NWSSource` — National Weather Service, ``api.weather.gov``.
  Authoritative US source.  Two-hop resolution: ``/points/{lat,lon}``
  returns per-grid URLs for forecast and for the observation-station
  list; the first station's latest observation feeds current
  conditions.  Lazy-caches the points lookup for
  :data:`_NWS_POINTS_TTL_S` seconds — the grid assignment is stable
  for a given lat/lon and revalidating per-query is wasteful.
  NWS requires a ``User-Agent`` header that identifies the caller.

- :class:`OpenMeteoSource` — ``api.open-meteo.com``, no key required,
  WMO weather codes translated to plain English.  Used as the
  fallback when NWS is unreachable.

:class:`WeatherClient` wraps a primary + fallback pair.  If the
primary raises :class:`WeatherSourceError`, the client invokes an
optional ``on_fallback`` callback (so the caller can tell the user
"retrying" instead of leaving them wondering whether the system
froze) and then tries the fallback.  If both fail, the exception
from the fallback is re-raised.

Air quality (PM2.5, ozone, UV, pollen by species) has no NWS
equivalent; :class:`OpenMeteoAirQuality` is single-sourced against
Open-Meteo's air-quality API.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger: logging.Logger = logging.getLogger("glowup.voice.weather")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# NWS requires a distinguishable User-Agent — opaque UAs are rate-limited
# or blocked outright.  Form is a free-text identifier; contact info is
# conventional so NWS can reach out before blocking misbehaving clients.
# The contact half is sourced from site.json so the public repo carries
# no operator email; the application name half stays generic in source.
# If site.contact_email is unset the UA degrades to name-only — NWS may
# rate-limit but will still answer; we log a warning at module import
# so the operator sees the missing key.
from glowup_site import site as _site
_NWS_APP_NAME: str = "glowup-voice"
_NWS_CONTACT: str = _site.get("contact_email") or ""
if _NWS_CONTACT:
    _NWS_USER_AGENT: str = f"({_NWS_APP_NAME}, {_NWS_CONTACT})"
else:
    _NWS_USER_AGENT = f"({_NWS_APP_NAME})"
    logger.warning(
        "site.contact_email not set — NWS User-Agent is name-only; "
        "set 'contact_email' in site.json to avoid possible NWS rate-limiting"
    )

# Per-request HTTP timeout for both sources.  Kept aggressive so that a
# primary failure cascades to the fallback inside a single voice
# interaction budget (~10s total).
_HTTP_TIMEOUT_S: float = 5.0

# Points-endpoint cache lifetime.  The lat/lon → (forecast URL, station
# list URL) mapping changes only when NWS redraws its grid; a day is
# comfortably within any realistic update cadence.
_NWS_POINTS_TTL_S: int = 24 * 60 * 60

# WMO weather interpretation codes → plain English.  Shared by the
# Open-Meteo current and forecast endpoints.  Not defined inside the
# class so tests can import it directly.
_WMO_CODES: dict[int, str] = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy",
    3: "overcast", 45: "foggy", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snow", 73: "moderate snow", 75: "heavy snow",
    77: "snow grains", 80: "slight rain showers",
    81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CurrentConditions:
    """Snapshot of current outdoor weather.

    Any field may be None when the source returns a null value (NWS
    observations regularly reject readings under QC; Open-Meteo is
    less prone to this).  Callers must tolerate missing fields.
    """
    temp_f: Optional[float]
    apparent_f: Optional[float]  # Heat index / wind chill / feels-like.
    humidity_pct: Optional[float]
    wind_mph: Optional[float]
    condition: str  # Plain-English description.
    source: str = "unknown"


@dataclass
class ForecastPeriod:
    """One period in a multi-period forecast.

    NWS periods alternate day/night with human-readable ``name``
    ("Today", "Tonight", "Thursday", "Thursday Night", ...).
    Open-Meteo is re-shaped to match this structure so downstream
    formatting does not branch on source.
    """
    name: str
    is_daytime: bool
    temperature_f: Optional[float]
    condition: str
    precip_probability_pct: Optional[float]
    wind_mph_desc: str  # Free-form ("10 to 15 mph"); may be empty.


@dataclass
class AirQuality:
    """Air-quality snapshot — particulate, ozone, UV, pollen."""
    pm2_5: Optional[float]
    pm10: Optional[float]
    ozone: Optional[float]
    us_aqi: Optional[float]
    uv_index: Optional[float]
    # Species name → grains/m^3 (Open-Meteo units).  Missing species
    # simply absent from the dict rather than None-valued, so len()
    # reflects how many the provider actually reported.
    pollen: dict[str, float] = field(default_factory=dict)
    source: str = "Open-Meteo"


# ---------------------------------------------------------------------------
# Exceptions and helpers
# ---------------------------------------------------------------------------

class WeatherSourceError(Exception):
    """Raised by any source on timeout, HTTP error, or parse failure.

    Callers that want failover should catch this specifically so
    unrelated programming errors (e.g. an :class:`AttributeError` from
    a refactor) are not silently swallowed.
    """


def _val(props: dict[str, Any], key: str) -> Optional[float]:
    """Extract an NWS GeoJSON ``{unitCode, value}`` pair's numeric value.

    NWS wraps every measurement in that shape; null values are legal
    and common under QC.  Returns None when the key is absent, the
    wrapper is missing, or the value is non-numeric.
    """
    entry = props.get(key) or {}
    v = entry.get("value") if isinstance(entry, dict) else None
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _c_to_f(c: Optional[float]) -> Optional[float]:
    """Celsius → Fahrenheit, preserving None."""
    return None if c is None else c * 9.0 / 5.0 + 32.0


def _kmh_to_mph(kmh: Optional[float]) -> Optional[float]:
    """km/h → mph, preserving None."""
    return None if kmh is None else kmh * 0.621371


def _to_float(x: Any) -> Optional[float]:
    """Best-effort float coercion; returns None on failure."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class WeatherSource:
    """Abstract weather source interface.

    Concrete implementations fetch current conditions and daily/nightly
    forecast periods.  Any failure must raise
    :class:`WeatherSourceError` so :class:`WeatherClient` can fail over.
    """

    name: str = "base"

    def current(self) -> CurrentConditions:
        """Return current outdoor conditions or raise ``WeatherSourceError``."""
        raise NotImplementedError

    def forecast(self) -> list[ForecastPeriod]:
        """Return list of forecast periods or raise ``WeatherSourceError``."""
        raise NotImplementedError


class NWSSource(WeatherSource):
    """National Weather Service source.

    Two-hop resolution:
      - ``/points/{lat},{lon}`` → grid metadata including forecast URL
        and observation-stations URL (cached :data:`_NWS_POINTS_TTL_S`).
      - The first station in the stations list is queried for
        ``/observations/latest`` to produce current conditions.

    Station assignment rarely changes; cached per-instance.
    """

    name: str = "NWS"

    def __init__(
        self,
        lat: float,
        lon: float,
        timeout_s: float = _HTTP_TIMEOUT_S,
        user_agent: str = _NWS_USER_AGENT,
    ) -> None:
        """Initialize with coordinates and optional timeout / UA override."""
        self._lat: float = lat
        self._lon: float = lon
        self._timeout: float = timeout_s
        self._ua: str = user_agent
        self._points_data: Optional[dict[str, Any]] = None
        self._points_ts: float = 0.0
        self._station_obs_url: Optional[str] = None

    def _get(self, url: str) -> dict[str, Any]:
        """Perform an authenticated GET against ``api.weather.gov``."""
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self._ua,
                "Accept": "application/geo+json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            raise WeatherSourceError(
                f"NWS {url}: {type(exc).__name__}: {exc}"
            ) from exc

    def _points(self) -> dict[str, Any]:
        """Return cached points data, refreshing if stale."""
        now: float = time.time()
        if (
            self._points_data is not None
            and (now - self._points_ts) < _NWS_POINTS_TTL_S
        ):
            return self._points_data
        url: str = (
            f"https://api.weather.gov/points/"
            f"{self._lat:.4f},{self._lon:.4f}"
        )
        data: dict[str, Any] = self._get(url)
        self._points_data = data
        self._points_ts = now
        return data

    def _nearest_station_obs_url(self) -> str:
        """Resolve and cache the nearest station's observations URL."""
        if self._station_obs_url:
            return self._station_obs_url
        props: dict[str, Any] = self._points().get("properties", {})
        stations_url: Optional[str] = props.get("observationStations")
        if not stations_url:
            raise WeatherSourceError(
                "NWS points response missing observationStations URL"
            )
        stations: dict[str, Any] = self._get(stations_url)
        feats: list[dict[str, Any]] = stations.get("features", [])
        if not feats:
            raise WeatherSourceError("NWS stations list empty for this grid")
        station_id: Optional[str] = (
            feats[0].get("properties", {}).get("stationIdentifier")
        )
        if not station_id:
            raise WeatherSourceError("NWS first station missing identifier")
        self._station_obs_url = (
            f"https://api.weather.gov/stations/"
            f"{station_id}/observations/latest"
        )
        return self._station_obs_url

    def current(self) -> CurrentConditions:
        """Current observation from the nearest NWS station."""
        obs: dict[str, Any] = self._get(self._nearest_station_obs_url())
        props: dict[str, Any] = obs.get("properties", {})

        temp_c: Optional[float] = _val(props, "temperature")
        humidity: Optional[float] = _val(props, "relativeHumidity")
        wind_kmh: Optional[float] = _val(props, "windSpeed")

        # NWS only populates one of heatIndex / windChill at a time.
        # If neither is present, fall back to raw temperature so the
        # "apparent" field is never more misleading than absent.
        apparent_c: Optional[float] = _val(props, "heatIndex")
        if apparent_c is None:
            apparent_c = _val(props, "windChill")
        if apparent_c is None:
            apparent_c = temp_c

        condition: str = (
            props.get("textDescription") or "unknown conditions"
        )

        return CurrentConditions(
            temp_f=_c_to_f(temp_c),
            apparent_f=_c_to_f(apparent_c),
            humidity_pct=humidity,
            wind_mph=_kmh_to_mph(wind_kmh),
            condition=condition,
            source=self.name,
        )

    def forecast(self) -> list[ForecastPeriod]:
        """Multi-period forecast from the NWS grid's forecast URL."""
        forecast_url: Optional[str] = (
            self._points().get("properties", {}).get("forecast")
        )
        if not forecast_url:
            raise WeatherSourceError(
                "NWS points response missing forecast URL"
            )
        data: dict[str, Any] = self._get(forecast_url)
        periods: list[dict[str, Any]] = (
            data.get("properties", {}).get("periods", [])
        )
        out: list[ForecastPeriod] = []
        for p in periods:
            precip_entry = p.get("probabilityOfPrecipitation") or {}
            out.append(ForecastPeriod(
                name=p.get("name", ""),
                is_daytime=bool(p.get("isDaytime", True)),
                temperature_f=_to_float(p.get("temperature")),
                condition=p.get("shortForecast", "unknown"),
                precip_probability_pct=_to_float(precip_entry.get("value")),
                wind_mph_desc=p.get("windSpeed", "") or "",
            ))
        return out


class OpenMeteoSource(WeatherSource):
    """Open-Meteo fallback source.

    Unlike NWS, Open-Meteo returns a single JSON document per request
    with a stable schema, so there is no points/station indirection.
    Forecast is reshaped into day/night ``ForecastPeriod`` pairs so the
    downstream selector is source-agnostic.
    """

    name: str = "Open-Meteo"

    _CURRENT_URL: str = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude={lat:.4f}&longitude={lon:.4f}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
        "weather_code,wind_speed_10m"
        "&temperature_unit=fahrenheit&wind_speed_unit=mph"
    )
    _FORECAST_URL: str = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude={lat:.4f}&longitude={lon:.4f}"
        "&daily=temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max,weather_code"
        "&temperature_unit=fahrenheit&wind_speed_unit=mph"
        "&timezone=auto&forecast_days=3"
    )

    # Human labels for the first few daily indices.  Day N for N>2 is
    # left as the ISO date — Open-Meteo's ``daily.time`` entries.
    _DAY_LABELS: tuple[str, ...] = ("Today", "Tomorrow", "Day after tomorrow")

    def __init__(
        self,
        lat: float,
        lon: float,
        timeout_s: float = _HTTP_TIMEOUT_S,
    ) -> None:
        """Initialize with coordinates and optional timeout override."""
        self._lat: float = lat
        self._lon: float = lon
        self._timeout: float = timeout_s

    def _get(self, url: str) -> dict[str, Any]:
        """Perform a GET against Open-Meteo; raise on any failure."""
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            raise WeatherSourceError(
                f"Open-Meteo {url}: {type(exc).__name__}: {exc}"
            ) from exc

    def current(self) -> CurrentConditions:
        """Current conditions from Open-Meteo forecast endpoint."""
        data: dict[str, Any] = self._get(
            self._CURRENT_URL.format(lat=self._lat, lon=self._lon)
        )
        cur: dict[str, Any] = data.get("current", {})
        code: int = int(cur.get("weather_code", 0) or 0)
        return CurrentConditions(
            temp_f=_to_float(cur.get("temperature_2m")),
            apparent_f=_to_float(cur.get("apparent_temperature")),
            humidity_pct=_to_float(cur.get("relative_humidity_2m")),
            wind_mph=_to_float(cur.get("wind_speed_10m")),
            condition=_WMO_CODES.get(code, "unknown conditions"),
            source=self.name,
        )

    def forecast(self) -> list[ForecastPeriod]:
        """Daily forecast reshaped to day+night period pairs."""
        data: dict[str, Any] = self._get(
            self._FORECAST_URL.format(lat=self._lat, lon=self._lon)
        )
        daily: dict[str, Any] = data.get("daily", {})
        dates: list[str] = daily.get("time", []) or []
        highs: list[Any] = daily.get("temperature_2m_max", []) or []
        lows: list[Any] = daily.get("temperature_2m_min", []) or []
        precip: list[Any] = (
            daily.get("precipitation_probability_max", []) or []
        )
        codes: list[Any] = daily.get("weather_code", []) or []

        def _at(lst: list[Any], i: int) -> Any:
            """Safe list indexing with a None default."""
            return lst[i] if i < len(lst) else None

        out: list[ForecastPeriod] = []
        for i, iso_date in enumerate(dates):
            label: str = (
                self._DAY_LABELS[i] if i < len(self._DAY_LABELS) else iso_date
            )
            code_i: int = int(_at(codes, i) or 0)
            condition: str = _WMO_CODES.get(code_i, "unknown conditions")
            precip_pct: Optional[float] = _to_float(_at(precip, i))

            out.append(ForecastPeriod(
                name=label,
                is_daytime=True,
                temperature_f=_to_float(_at(highs, i)),
                condition=condition,
                precip_probability_pct=precip_pct,
                wind_mph_desc="",
            ))
            out.append(ForecastPeriod(
                name=f"{label} night",
                is_daytime=False,
                temperature_f=_to_float(_at(lows, i)),
                condition=condition,
                precip_probability_pct=precip_pct,
                wind_mph_desc="",
            ))
        return out


class OpenMeteoAirQuality:
    """Air-quality source (Open-Meteo, single-sourced).

    Returns PM2.5/PM10, ozone, US AQI, UV index, and species-specific
    pollen grain counts.  No fallback provider exists — NWS does not
    publish air-quality data.
    """

    name: str = "Open-Meteo AQ"

    _POLLEN_SPECIES: tuple[str, ...] = (
        "alder_pollen", "birch_pollen", "grass_pollen",
        "mugwort_pollen", "olive_pollen", "ragweed_pollen",
    )

    _URL: str = (
        "https://air-quality-api.open-meteo.com/v1/air-quality"
        "?latitude={lat:.4f}&longitude={lon:.4f}"
        "&current=pm2_5,pm10,ozone,us_aqi,uv_index,"
        "alder_pollen,birch_pollen,grass_pollen,"
        "mugwort_pollen,olive_pollen,ragweed_pollen"
    )

    def __init__(
        self,
        lat: float,
        lon: float,
        timeout_s: float = _HTTP_TIMEOUT_S,
    ) -> None:
        """Initialize with coordinates and optional timeout override."""
        self._lat: float = lat
        self._lon: float = lon
        self._timeout: float = timeout_s

    def _get(self, url: str) -> dict[str, Any]:
        """Perform a GET against Open-Meteo air-quality; raise on failure."""
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            raise WeatherSourceError(
                f"Open-Meteo AQ {url}: {type(exc).__name__}: {exc}"
            ) from exc

    def current(self) -> AirQuality:
        """Return the current air-quality snapshot."""
        data: dict[str, Any] = self._get(
            self._URL.format(lat=self._lat, lon=self._lon)
        )
        cur: dict[str, Any] = data.get("current", {})

        pollen: dict[str, float] = {}
        for species in self._POLLEN_SPECIES:
            v: Optional[float] = _to_float(cur.get(species))
            if v is not None:
                pollen[species] = v

        return AirQuality(
            pm2_5=_to_float(cur.get("pm2_5")),
            pm10=_to_float(cur.get("pm10")),
            ozone=_to_float(cur.get("ozone")),
            us_aqi=_to_float(cur.get("us_aqi")),
            uv_index=_to_float(cur.get("uv_index")),
            pollen=pollen,
            source=self.name,
        )


# ---------------------------------------------------------------------------
# Failover client
# ---------------------------------------------------------------------------

class WeatherClient:
    """Primary source with automatic fallback.

    When the primary source raises :class:`WeatherSourceError`, the
    client invokes the optional ``on_fallback`` callback (so the
    voice handler can emit a "retrying" notice before the user thinks
    the system has hung) and re-tries against the fallback source.

    If the fallback also fails, its exception propagates — the caller
    sees exactly which source's error to log.
    """

    def __init__(
        self,
        primary: WeatherSource,
        fallback: WeatherSource,
        on_fallback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Initialize with two sources and an optional retry-notice hook."""
        self._primary: WeatherSource = primary
        self._fallback: WeatherSource = fallback
        self._on_fallback: Optional[Callable[[str], None]] = on_fallback

    def _notify(self, reason: str) -> None:
        """Best-effort invoke ``on_fallback``; never let it crash the call."""
        if self._on_fallback is None:
            return
        try:
            self._on_fallback(reason)
        except Exception as exc:
            logger.debug("Fallback notify callback raised: %s", exc)

    def current(self) -> CurrentConditions:
        """Current conditions, primary then fallback on failure."""
        try:
            return self._primary.current()
        except WeatherSourceError as exc:
            logger.warning(
                "Current-conditions primary (%s) failed: %s — "
                "falling back to %s",
                self._primary.name, exc, self._fallback.name,
            )
            self._notify(
                f"{self._primary.name} is not responding. "
                "Retrying with backup."
            )
            return self._fallback.current()

    def forecast(self) -> list[ForecastPeriod]:
        """Forecast periods, primary then fallback on failure."""
        try:
            return self._primary.forecast()
        except WeatherSourceError as exc:
            logger.warning(
                "Forecast primary (%s) failed: %s — falling back to %s",
                self._primary.name, exc, self._fallback.name,
            )
            self._notify(
                f"{self._primary.name} is not responding. "
                "Retrying with backup."
            )
            return self._fallback.forecast()
