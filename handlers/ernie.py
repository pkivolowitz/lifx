"""Ernie (.153) sniffer dashboard handlers.

Mixin class for GlowUpRequestHandler. Serves the /ernie dashboard page
and three REST endpoints that expose live traffic from ernie's BLE
sniffer, SDR decoder, and thermal sensor.

Endpoints
---------
- GET /ernie                  dashboard HTML
- GET /api/ernie/ble          BLE advertisements seen in the last window
- GET /api/ernie/tpms         TPMS decodes grouped by (model, id)
- GET /api/ernie/thermal      ernie's latest thermal reading + services

Data is populated by three MQTT subscribers wired in server.py:

    glowup/ble/adv/<mac>              -> _ernie_ble[mac]
    glowup/tpms/events                -> _ernie_tpms[(model, id)]
    glowup/hardware/thermal/ernie     -> _ernie_thermal

The dashboard polls each API every 2 s (configurable client-side).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import logging
import os
import time
from typing import Any

logger: logging.Logger = logging.getLogger("glowup.handlers.ernie")

# How many BLE events to return (tail of the ring buffer) per request.
# 200 covers a few minutes of normal activity; full ring depth lives
# on the server side.
BLE_EVENTS_TAIL: int = 200


class ErnieHandlerMixin:
    """Ernie sniffer dashboard + APIs."""

    def _handle_get_ernie_page(self) -> None:
        """GET /ernie — serve the sniffer dashboard page."""
        page_path: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "ernie.html",
        )
        try:
            with open(page_path, "r") as f:
                html: str = f.read()
            body: bytes = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._send_json(404, {"error": "Ernie dashboard not found"})

    def _handle_get_ernie_ble(self) -> None:
        """GET /api/ernie/ble — current BLE device catalog.

        Returns every MAC the sniffer currently knows, including those
        marked ``gone`` (recently departed). The v2 sniffer handles
        staleness server-side via absence sweep + retained gone marker.
        """
        store: dict = getattr(self, "_ernie_ble_seen", {})
        devices: list[dict] = list(store.values())
        # Sort gone-items to the bottom; within each group sort by
        # most-recent activity (last_heard_ts) descending.
        def _key(r: dict) -> tuple:
            gone: int = 1 if r.get("gone") else 0
            ts: float = r.get("last_heard_ts") or r.get("ts") or 0
            return (gone, -ts)
        devices.sort(key=_key)
        self._send_json(200, {
            "devices": devices,
            "count": len(devices),
            "timestamp": time.time(),
        })

    def _handle_get_ernie_ble_events(self) -> None:
        """GET /api/ernie/ble/events — tail of the BLE event log."""
        ring: list = getattr(self, "_ernie_ble_events", [])
        # Return the tail, newest last — matches a "log file" mental
        # model for the dashboard's event stream panel.
        tail: list = ring[-BLE_EVENTS_TAIL:]
        self._send_json(200, {
            "events": tail,
            "count": len(tail),
            "timestamp": time.time(),
        })

    def _handle_get_ernie_tpms(self) -> None:
        """GET /api/ernie/tpms — unique TPMS sensors seen.

        One entry per (model, id) tuple — that pair is the fingerprint.
        The service never forgets a sensor within the process lifetime;
        seeing the same ``id`` twice means the same tire passed twice.
        """
        store: dict = getattr(self, "_ernie_tpms", {})
        entries: list[dict] = list(store.values())
        entries.sort(key=lambda r: r.get("last_seen", 0), reverse=True)
        self._send_json(200, {
            "sensors": entries,
            "count": len(entries),
            "timestamp": time.time(),
        })

    def _handle_get_ernie_thermal(self) -> None:
        """GET /api/ernie/thermal — latest thermal + derived health.

        Service health is inferred from presence of recent traffic:
        ble-sniffer is considered "up" if any BLE advert has arrived
        in the last 30 s; rtl433 is "up" if pi-thermal heartbeats are
        arriving (same box, same local broker) AND the rtl433 service
        can't be queried directly from here. We fall back to
        last-thermal-timestamp age as the proxy for ernie-alive.
        """
        reading: dict = getattr(self, "_ernie_thermal", {}) or {}
        now: float = time.time()
        ble_store: dict = getattr(self, "_ernie_ble_seen", {})
        last_ble: float = 0.0
        if ble_store:
            last_ble = max(
                (r.get("last_heard_ts") or r.get("ts") or 0)
                for r in ble_store.values()
            )
        tpms_store: dict = getattr(self, "_ernie_tpms", {})
        last_tpms: float = 0.0
        if tpms_store:
            last_tpms = max(
                r.get("last_seen", 0) for r in tpms_store.values()
            )
        thermal_ts: float = reading.get("_received_at", 0)
        self._send_json(200, {
            "reading": reading,
            "health": {
                # mosquitto is "up" if ANY traffic is flowing at all;
                # if we're receiving any of these, the bridge is alive.
                "mosquitto": (
                    (now - max(last_ble, last_tpms, thermal_ts)) < 60
                ),
                # ble-sniffer independent of mosquitto: needs recent BLE.
                "ble_sniffer": (now - last_ble) < 30 if last_ble else False,
                # rtl433: no cheap "alive" proxy since traffic is
                # sporadic. Reports "unknown" (null) rather than guess.
                "rtl433": None,
                # pi-thermal: heartbeats every 30 s, so 90 s is a
                # 3-miss tolerance.
                "pi_thermal": (
                    (now - thermal_ts) < 90 if thermal_ts else False
                ),
            },
            "timestamp": now,
        })
