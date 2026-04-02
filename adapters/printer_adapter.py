"""Brother printer monitor — polls status via the built-in web API.

Detects toner low, paper out, jams, cover open, and other error states
by scraping the CSV maintenance endpoint.  Publishes alerts to MQTT and
writes signals to the :class:`~media.SignalBus`.

Only the Brother HL-5470DW has been tested, but the CSV endpoint is
common across Brother network printers (HL, MFC, DCP series).

Configuration (in server.json)::

    "printer": {
        "host": "10.0.0.59",
        "name": "Brother HL-5470DW",
        "poll_interval_seconds": 86400
    }

Signal output::

    printer:status  — 0.0 (ok) / 1.0 (needs attention)

MQTT output::

    glowup/printer/status   — "ok" / "toner_low" / "no_paper" / "jam" / "error"
    glowup/printer/details  — JSON with page count, drum life, toner status, etc.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import csv
import io
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .adapter_base import PollingAdapterBase
from media import SignalMeta

logger: logging.Logger = logging.getLogger("glowup.printer")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default poll interval: once per day (seconds).
DEFAULT_POLL_INTERVAL: float = 86400.0

# Minimum poll interval to avoid hammering the printer.
MIN_POLL_INTERVAL: float = 60.0

# HTTP timeout for printer requests (seconds).
HTTP_TIMEOUT: float = 10.0

# URL path for the CSV maintenance endpoint (Brother standard).
CSV_ENDPOINT: str = "/etc/mnt_info.csv"

# MQTT topic prefix.
MQTT_TOPIC_PREFIX: str = "glowup/printer"

# Transport identifier for signal metadata.
TRANSPORT: str = "printer"

# MQTT QoS for printer messages.
MQTT_QOS: int = 1

# Brother status page endpoint.
STATUS_ENDPOINT: str = "/general/status.html"

# Status strings that indicate the printer is healthy.
OK_STATUSES: set[str] = {"sleep", "ready", "waiting", "printing", "cooling down"}

# Drum life percentage threshold for warning.
DRUM_WARN_THRESHOLD: float = 15.0


# ---------------------------------------------------------------------------
# PrinterAdapter
# ---------------------------------------------------------------------------

class PrinterAdapter(PollingAdapterBase):
    """Polls a Brother network printer for consumable and error state.

    Args:
        config:      The ``"printer"`` section of server.json.
        bus:         The shared :class:`~media.SignalBus`.
        mqtt_client: Optional paho MQTT client for MQTT publishing.
    """

    def __init__(
        self,
        config: dict[str, Any],
        bus: Any,
        mqtt_client: Any = None,
    ) -> None:
        """Initialize the printer adapter.

        Args:
            config:      Printer config section from server.json.
            bus:         SignalBus instance for signal writes.
            mqtt_client: Optional paho MQTT client for MQTT publishing.
        """
        poll_interval: float = max(
            float(config.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL)),
            MIN_POLL_INTERVAL,
        )
        super().__init__(
            poll_interval=poll_interval,
            thread_name="printer-adapter",
        )
        self._host: str = config.get("host", "")
        self._name: str = config.get("name", "Printer")
        self._bus: Any = bus
        self._mqtt_client: Any = mqtt_client

        # Last known state — preserved across polls.
        self._last_status: str = "unknown"
        self._last_details: dict[str, Any] = {}
        self._last_poll: float = 0.0

    def _check_prerequisites(self) -> bool:
        """Check that a printer host is configured."""
        if not self._host:
            logger.warning(
                "No printer host configured — printer adapter disabled",
            )
            return False
        return True

    # --- Public API --------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return the last known printer state for API responses.

        Returns:
            Dict with status string, detail dict, and poll timestamp.
        """
        return {
            "name": self._name,
            "host": self._host,
            "status": self._last_status,
            "details": dict(self._last_details),
            "last_poll": self._last_poll,
        }

    def force_poll(self) -> dict[str, Any]:
        """Force an immediate poll and return the result.

        Returns:
            The updated status dict.
        """
        self._do_poll()
        return self.get_status()

    # --- Polling -----------------------------------------------------------

    def _do_poll(self) -> None:
        """Execute a single poll cycle."""
        try:
            csv_data: Optional[dict[str, str]] = self._fetch_csv()
            if csv_data is None:
                self._update_state("offline", {"error": "unreachable"})
                return

            device_status: str = self._fetch_device_status()
            alerts: list[str] = []
            details: dict[str, Any] = {
                "device_status": device_status,
                "page_count": int(csv_data.get("Page Counter", "0") or "0"),
                "drum_count": int(csv_data.get("Drum Count", "0") or "0"),
                "toner_replacements": int(
                    csv_data.get("Replace Count(Toner)", "0") or "0"
                ),
            }

            # Drum life.
            drum_pct_str: str = csv_data.get(
                "% of Life Remaining(Drum Unit)", ""
            )
            if drum_pct_str:
                try:
                    drum_pct: float = float(drum_pct_str)
                    details["drum_life_pct"] = drum_pct
                    if drum_pct <= DRUM_WARN_THRESHOLD:
                        alerts.append("drum_low")
                except (ValueError, TypeError):
                    pass

            # Check device status for actionable conditions.
            status_lower: str = device_status.lower().strip()
            if "toner" in status_lower:
                alerts.append("toner_low")
            if "no paper" in status_lower or "out of paper" in status_lower:
                alerts.append("no_paper")
            if "jam" in status_lower:
                alerts.append("jam")
            if "cover" in status_lower and "open" in status_lower:
                alerts.append("cover_open")
            if status_lower and status_lower not in OK_STATUSES and not alerts:
                alerts.append("error")

            details["alerts"] = alerts
            status_str: str = alerts[0] if alerts else "ok"
            self._update_state(status_str, details)

        except Exception as exc:
            logger.warning("Printer poll error: %s", exc)
            self._update_state("error", {"error": str(exc)})

    def _update_state(self, status: str, details: dict[str, Any]) -> None:
        """Update cached state, write signals, publish MQTT.

        Args:
            status:  Status string (ok, toner_low, no_paper, jam, etc.).
            details: Detail dict with page count, drum life, etc.
        """
        prev: str = self._last_status
        self._last_status = status
        self._last_details = details
        self._last_poll = time.time()

        # Signal: 0.0 = ok, 1.0 = needs attention.
        signal_value: float = 0.0 if status == "ok" else 1.0
        signal_name: str = "printer:status"
        if hasattr(self._bus, 'register'):
            self._bus.register(signal_name, SignalMeta(
                signal_type="scalar",
                description=f"{self._name} status",
                source_name="printer",
                transport=TRANSPORT,
            ))
        self._bus.write(signal_name, signal_value)

        # MQTT.
        if self._mqtt_client:
            try:
                self._mqtt_client.publish(
                    f"{MQTT_TOPIC_PREFIX}/status",
                    status, qos=MQTT_QOS,
                )
                self._mqtt_client.publish(
                    f"{MQTT_TOPIC_PREFIX}/details",
                    json.dumps(details), qos=MQTT_QOS,
                )
            except Exception as exc:
                logger.debug("MQTT publish error (printer): %s", exc)

        if prev != status:
            logger.info("Printer %s: %s", self._name, status)

    def _fetch_csv(self) -> Optional[dict[str, str]]:
        """Fetch and parse the CSV maintenance endpoint.

        Returns:
            A dict mapping CSV header names to values, or None on failure.
        """
        url: str = f"http://{self._host}{CSV_ENDPOINT}"
        req: urllib.request.Request = urllib.request.Request(
            url, method="POST",
            data=b"pageid=3",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                text: str = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Printer CSV fetch failed (%s): %s", self._host, exc)
            return None

        # Parse CSV — two rows: header and data.
        reader = csv.reader(io.StringIO(text))
        rows: list[list[str]] = list(reader)
        if len(rows) < 2:
            logger.warning("Printer CSV has fewer than 2 rows")
            return None

        headers: list[str] = rows[0]
        values: list[str] = rows[1]
        return dict(zip(headers, values))

    def _fetch_device_status(self) -> str:
        """Fetch the current device status string from the status page.

        Returns:
            Status string (e.g. "Sleep", "Ready", "Toner Low"), or
            "unknown" on failure.
        """
        url: str = f"http://{self._host}{STATUS_ENDPOINT}"
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
                html: str = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return "unknown"

        # The status is inside: <span class="moni moniOk">STATUS</span>
        # or <span class="moni moniWarn">STATUS</span>
        # Look for the moni span content.
        import re
        match: Optional[re.Match] = re.search(
            r'class="moni\s+moni\w+">(.*?)</span>', html,
        )
        if match:
            return match.group(1).strip()
        return "unknown"

    # --- Hooks -------------------------------------------------------------

    def _on_started(self) -> None:
        """Log printer-specific start message."""
        logger.info(
            "Printer adapter started — %s at %s (poll every %.0fs)",
            self._name, self._host, self._poll_interval,
        )

    def _on_stopped(self) -> None:
        """Log printer-specific stop message."""
        logger.info("Printer adapter stopped")
