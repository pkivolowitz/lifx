"""HDHomeRun adapter — polls tuner status and channel lineup.

Subscribes to the local HDHomeRun device via its HTTP API and writes
tuner status, signal diagnostics, and channel information to the
:class:`~media.SignalBus` as scalar signals.

The HDHomeRun exposes a simple HTTP API on port 5004:
  - ``/lineup.json``        — available channels
  - ``/lineup_status.json`` — tuner/scan status
  - ``/status.json``        — device info and tuner states

This adapter also provides methods for the dashboard to query
current tuner state and channel lineup directly.

Requires ``urllib`` (stdlib) — no additional dependencies.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import time
import urllib.request
from typing import Any, Optional

from adapter_base import PollingAdapterBase

logger: logging.Logger = logging.getLogger("glowup.hdhr")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default HDHomeRun HTTP API port.
HDHR_PORT: int = 5004

# Default poll interval (seconds) — tuner status doesn't change fast.
DEFAULT_POLL_INTERVAL: float = 10.0

# HTTP request timeout (seconds).
HTTP_TIMEOUT: float = 5.0

# Channel lineup refresh interval (seconds) — lineup rarely changes.
LINEUP_REFRESH_INTERVAL: float = 3600.0

# Signal quality thresholds for dashboard color coding.
SIGNAL_GOOD: int = 75
SIGNAL_FAIR: int = 50

# Transport identifier for SignalBus metadata.
TRANSPORT: str = "hdhr"


# ---------------------------------------------------------------------------
# HDHomeRunAdapter
# ---------------------------------------------------------------------------

class HDHomeRunAdapter(PollingAdapterBase):
    """Poll HDHomeRun tuner status and channel lineup.

    Args:
        config: The ``"hdhr"`` section of server.json.
        bus:    The shared :class:`~media.SignalBus`.
    """

    def __init__(
        self,
        config: dict[str, Any],
        bus: Any,
    ) -> None:
        """Initialize the HDHomeRun adapter.

        Args:
            config: HDHomeRun config section from server.json.
            bus:    SignalBus instance for signal writes.
        """
        self._host: str = config.get("host", "hdhomerun.local")
        self._port: int = config.get("port", HDHR_PORT)
        poll_interval: float = config.get(
            "poll_interval", DEFAULT_POLL_INTERVAL,
        )
        super().__init__(
            poll_interval=poll_interval,
            thread_name="hdhr-adapter",
        )
        self._bus: Any = bus
        self._base_url: str = f"http://{self._host}:{self._port}"

        # Cached state — thread-safe reads via the lock in PollingAdapterBase.
        self._tuner_status: list[dict[str, Any]] = []
        self._device_info: dict[str, Any] = {}
        self._lineup: list[dict[str, Any]] = []
        self._lineup_last_refresh: float = 0.0

    # --- Internal helpers --------------------------------------------------

    def _fetch_json(self, path: str) -> Optional[Any]:
        """Fetch JSON from the HDHomeRun HTTP API.

        Args:
            path: URL path (e.g., ``/status.json``).

        Returns:
            Parsed JSON, or None on error.
        """
        url: str = f"{self._base_url}{path}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("HDHomeRun fetch %s failed: %s", path, exc)
            return None

    # --- Polling -----------------------------------------------------------

    def _do_poll(self) -> None:
        """Execute one poll cycle — fetch tuner status and optionally lineup."""
        # Tuner/device status — every poll.
        status = self._fetch_json("/status.json")
        if status:
            self._device_info = status
            tuners = status.get("tuners", [])
            if isinstance(tuners, list):
                self._tuner_status = tuners
            # Write signal quality to SignalBus for each active tuner.
            for i, tuner in enumerate(self._tuner_status):
                if tuner.get("VctNumber"):
                    sig_quality = tuner.get("SignalQualityPercent", 0)
                    self._bus.write(
                        f"hdhr:tuner{i}:signal_quality",
                        float(sig_quality),
                    )
                    sym_quality = tuner.get("SymbolQualityPercent", 0)
                    self._bus.write(
                        f"hdhr:tuner{i}:symbol_quality",
                        float(sym_quality),
                    )

        # Channel lineup — refresh hourly.
        now: float = time.time()
        if now - self._lineup_last_refresh > LINEUP_REFRESH_INTERVAL:
            lineup = self._fetch_json("/lineup.json")
            if lineup and isinstance(lineup, list):
                self._lineup = lineup
                self._lineup_last_refresh = now
                logger.info(
                    "HDHomeRun lineup refreshed: %d channels", len(lineup),
                )

    # --- Public API for dashboard ------------------------------------------

    def get_tuner_status(self) -> list[dict[str, Any]]:
        """Return current tuner status for all tuners.

        Returns:
            List of tuner dicts with channel, signal, frequency info.
        """
        result: list[dict[str, Any]] = []
        for i, tuner in enumerate(self._tuner_status):
            entry: dict[str, Any] = {
                "tuner": i,
                "channel": tuner.get("VctName", ""),
                "channel_number": tuner.get("VctNumber", ""),
                "frequency": tuner.get("Frequency", 0),
                "signal_strength": tuner.get("SignalStrengthPercent", 0),
                "signal_quality": tuner.get("SignalQualityPercent", 0),
                "symbol_quality": tuner.get("SymbolQualityPercent", 0),
                "active": bool(tuner.get("VctNumber")),
            }
            result.append(entry)
        return result

    def get_device_info(self) -> dict[str, Any]:
        """Return HDHomeRun device information.

        Returns:
            Dict with model, firmware, device ID, tuner count.
        """
        return {
            "model": self._device_info.get("ModelNumber", ""),
            "firmware": self._device_info.get("FirmwareVersion", ""),
            "device_id": self._device_info.get("DeviceID", ""),
            "tuner_count": len(self._tuner_status),
            "base_url": self._base_url,
        }

    def get_lineup(self) -> list[dict[str, Any]]:
        """Return the channel lineup.

        Returns:
            List of dicts with GuideName, GuideNumber, URL.
        """
        return [
            {
                "number": ch.get("GuideNumber", ""),
                "name": ch.get("GuideName", ""),
                "hd": ch.get("HD", 0) == 1,
            }
            for ch in self._lineup
        ]

    def get_stream_url(self, channel: str) -> str:
        """Return the HTTP stream URL for a channel.

        Args:
            channel: Virtual channel number (e.g., ``"5.1"``).

        Returns:
            HTTP URL for MPEG-TS stream.
        """
        return f"{self._base_url}/auto/v{channel}"

    # --- Status and hooks --------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return adapter status for API responses.

        Returns:
            Dict with connection state and config.
        """
        return {
            "running": self._running,
            "host": self._host,
            "port": self._port,
            "tuners": len(self._tuner_status),
            "channels": len(self._lineup),
        }

    def _on_started(self) -> None:
        """Log HDHomeRun start."""
        logger.info(
            "HDHomeRun adapter started — polling %s", self._base_url,
        )

    def _on_stopped(self) -> None:
        """Log HDHomeRun stop."""
        logger.info("HDHomeRun adapter stopped")
