"""Background data poller — fetches API data on intervals.

Runs a single background thread that polls all GlowUp and
external API endpoints.  Tile renderers read from the shared
``state`` dict which is updated atomically.

Thread-safe: the poller writes complete dicts; readers get a
consistent snapshot via the property accessors.
"""

__version__: str = "1.0"

import json
import logging
import threading
import time
from typing import Any, Optional

import requests

logger: logging.Logger = logging.getLogger("glowup.kiosk.data")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Mobile, AL coordinates for weather APIs.
LAT: float = 30.69
LON: float = -88.04

# Poll intervals in seconds.
POLL_FAST: float = 10.0      # health, locks, security
POLL_MEDIUM: float = 30.0    # devices, cameras
POLL_SLOW: float = 300.0     # weather, AQI, soil, moon
POLL_ALERTS: float = 120.0   # NWS alerts

# Open-Meteo weather URL.
WEATHER_URL: str = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,relative_humidity_2m,weather_code,"
    "wind_speed_10m"
    "&temperature_unit=fahrenheit&wind_speed_unit=mph"
)

# Open-Meteo AQI URL.
AQI_URL: str = (
    "https://air-quality-api.open-meteo.com/v1/air-quality"
    "?latitude={lat}&longitude={lon}"
    "&current=us_aqi,pm10,pm2_5"
)

# NWS alerts URL (Mobile, AL county zone).
NWS_URL: str = (
    "https://api.weather.gov/alerts/active"
    "?point={lat},{lon}&status=actual&severity=Extreme,Severe,Moderate"
)


class DataPoller:
    """Background data poller for all kiosk data sources.

    Args:
        api_base: GlowUp server URL (e.g., ``http://10.0.0.214:8420``).
    """

    def __init__(self, api_base: str = "http://10.0.0.214:8420") -> None:
        """Initialize the poller."""
        self._api: str = api_base.rstrip("/")
        self._stop: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Shared state — each key is updated atomically.
        self._lock: threading.Lock = threading.Lock()
        self._state: dict[str, Any] = {}

        # Last poll timestamps — track when each source was last polled.
        self._last_poll: dict[str, float] = {}

    # -- Public API ---------------------------------------------------------

    def start(self) -> None:
        """Start the background polling thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="kiosk-data",
        )
        self._thread.start()
        logger.info("Data poller started: %s", self._api)

    def stop(self) -> None:
        """Stop the polling thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value from the shared state.

        Args:
            key:     State key (e.g., "health", "weather").
            default: Default if key not present.

        Returns:
            The current value, or default.
        """
        with self._lock:
            return self._state.get(key, default)

    # -- Internal -----------------------------------------------------------

    def _set(self, key: str, value: Any) -> None:
        """Atomically update a state value."""
        with self._lock:
            self._state[key] = value

    def _due(self, source: str, interval: float) -> bool:
        """Check if a source is due for polling."""
        now: float = time.monotonic()
        last: float = self._last_poll.get(source, 0.0)
        if now - last >= interval:
            self._last_poll[source] = now
            return True
        return False

    def _fetch_json(self, url: str, timeout: float = 10.0) -> Optional[dict]:
        """Fetch JSON from a URL, returning None on failure."""
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("Fetch failed %s: %s", url[:60], exc)
            return None

    def _api_get(self, path: str) -> Optional[dict]:
        """Fetch JSON from the GlowUp API."""
        return self._fetch_json(f"{self._api}{path}")

    def _run(self) -> None:
        """Main poll loop — runs until stopped."""
        # Initial poll of everything.
        self._poll_bundled()
        self._poll_weather()
        self._poll_aqi()
        self._poll_alerts()

        while not self._stop.is_set():
            # Bundled GlowUp data — single request for all local tiles.
            if self._due("bundled", POLL_FAST):
                self._poll_bundled()

            # External APIs — separate, slower intervals.
            if self._due("weather", POLL_SLOW):
                self._poll_weather()
            if self._due("aqi", POLL_SLOW):
                self._poll_aqi()
            if self._due("alerts", POLL_ALERTS):
                self._poll_alerts()

            self._stop.wait(1.0)

    def _poll_bundled(self) -> None:
        """Poll /api/home/all — single request for all GlowUp tile data."""
        data = self._api_get("/api/home/all")
        if data is not None:
            for key in ("locks", "security", "health", "cameras",
                        "printer", "soil"):
                if key in data:
                    self._set(key, data[key])

    def _poll_weather(self) -> None:
        """Poll Open-Meteo weather."""
        url: str = WEATHER_URL.format(lat=LAT, lon=LON)
        data = self._fetch_json(url)
        if data is not None:
            self._set("weather", data)

    def _poll_aqi(self) -> None:
        """Poll Open-Meteo AQI."""
        url: str = AQI_URL.format(lat=LAT, lon=LON)
        data = self._fetch_json(url)
        if data is not None:
            self._set("aqi", data)

    def _poll_alerts(self) -> None:
        """Poll NWS severe weather alerts."""
        url: str = NWS_URL.format(lat=LAT, lon=LON)
        data = self._fetch_json(url, timeout=15.0)
        if data is not None:
            features = data.get("features", [])
            self._set("alerts", features)
