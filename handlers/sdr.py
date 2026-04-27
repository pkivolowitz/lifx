"""SDR (software-defined radio) handlers.

Mixin class for GlowUpRequestHandler.  Provides endpoints for:

- GET  /api/sdr/status     — current SDR service state and recent devices
- POST /api/sdr/frequency  — change the rtl_433 frequency at runtime

The frequency change is implemented by publishing a command to
``glowup/sdr/command`` on the local MQTT broker.  The SDR service
(running on a remote Pi) subscribes to this topic and restarts
rtl_433 on the new frequency.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import logging
import os
import time
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.handlers.sdr")

# Well-known frequency presets (Hz) — used for validation and display.
FREQUENCY_PRESETS: dict[str, int] = {
    "433": 433_920_000,
    "315": 315_000_000,
    "868": 868_000_000,
    "915": 915_000_000,
}

# Valid frequency range for RTL-SDR R820T2 tuner.
MIN_FREQ_HZ: int = 24_000_000      # 24 MHz
MAX_FREQ_HZ: int = 1_766_000_000   # 1.766 GHz


class SdrHandlerMixin:
    """SDR and ADS-B endpoints for the GlowUp REST API."""

    # -- ADS-B endpoints ---------------------------------------------------

    def _handle_get_adsb_aircraft(self) -> None:
        """GET /api/sdr/adsb/aircraft — current aircraft from dump1090.

        Returns the latest aircraft list received via MQTT from the
        ADS-B service.
        """
        adsb_data: dict = getattr(self, "_adsb_aircraft", {})
        self._send_json(200, adsb_data if adsb_data else {
            "aircraft": [],
            "count": 0,
            "timestamp": time.time(),
        })

    # _handle_get_adsb_page and _handle_get_sdr_page were retired
    # 2026-04-27 — the standalone /adsb and /sdr dashboards are gone;
    # the unified /maritime + /air + /traffic page is the new home
    # for aircraft data.  The /api/sdr/adsb/aircraft and
    # /api/sdr/status + /api/sdr/frequency endpoints below are kept
    # (the maritime aircraft layer polls the first; the others are
    # inert when the SDR is ADS-B-only but cheap to leave wired).

    # -- SDR endpoints -----------------------------------------------------

    def _handle_get_sdr_status(self) -> None:
        """GET /api/sdr/status — SDR service state.

        Returns the last-seen SDR devices from the signal bus,
        the current frequency (if known), and recent packet count.
        """
        bus: Any = getattr(self, "signal_bus", None)
        if bus is None:
            self._send_json(503, {"error": "Signal bus unavailable"})
            return

        # Collect SDR signals from the bus — any signal whose label
        # starts with an rtl_433 model slug.  The SDR service publishes
        # status blobs to glowup/sdr/status/{label} which the hub
        # stores in _sdr_status if wired up, but we can also read
        # from the signal bus for basic info.
        sdr_store: dict = getattr(self, "_sdr_status", {})

        result: dict[str, Any] = {
            "devices": sdr_store,
            "device_count": len(sdr_store),
            "timestamp": time.time(),
        }
        self._send_json(200, result)

    def _handle_post_sdr_frequency(self) -> None:
        """POST /api/sdr/frequency — change rtl_433 frequency.

        Request body::

            {"frequency": 315000000}

        Or use a preset name::

            {"frequency": "315"}

        Publishes a command to ``glowup/sdr/command`` on the local
        MQTT broker.  The SDR service picks it up and restarts
        rtl_433 on the new frequency.
        """
        body: Optional[dict] = self._read_json_body()
        if body is None:
            return

        freq_raw: Any = body.get("frequency")
        if freq_raw is None:
            self._send_json(400, {"error": "'frequency' is required"})
            return

        # Resolve preset names.
        freq_hz: int
        if isinstance(freq_raw, str):
            preset: Optional[int] = FREQUENCY_PRESETS.get(freq_raw.strip())
            if preset is not None:
                freq_hz = preset
            else:
                try:
                    freq_hz = int(freq_raw)
                except ValueError:
                    self._send_json(400, {
                        "error": f"Unknown frequency preset: {freq_raw!r}",
                        "presets": FREQUENCY_PRESETS,
                    })
                    return
        elif isinstance(freq_raw, (int, float)):
            freq_hz = int(freq_raw)
        else:
            self._send_json(400, {"error": "'frequency' must be a number or preset name"})
            return

        # Validate range.
        if not (MIN_FREQ_HZ <= freq_hz <= MAX_FREQ_HZ):
            self._send_json(400, {
                "error": (
                    f"Frequency {freq_hz} Hz out of range "
                    f"({MIN_FREQ_HZ}–{MAX_FREQ_HZ})"
                ),
            })
            return

        # Publish command to MQTT.
        mqtt_client: Any = getattr(self, "_mqtt_client", None)
        if mqtt_client is None:
            self._send_json(503, {"error": "MQTT client unavailable"})
            return

        command: str = json.dumps({"frequency": freq_hz})
        try:
            mqtt_client.publish(
                "glowup/sdr/command", command, qos=1, retain=False,
            )
            freq_mhz: float = freq_hz / 1_000_000
            logger.info("SDR frequency change → %d Hz (%.3f MHz)", freq_hz, freq_mhz)
            self._send_json(200, {
                "status": "ok",
                "frequency_hz": freq_hz,
                "frequency_mhz": freq_mhz,
            })
        except Exception as exc:
            logger.error("Failed to publish SDR command: %s", exc)
            self._send_json(500, {"error": f"MQTT publish failed: {exc}"})
