"""Ernie (.153) sniffer dashboard handlers.

Mixin class for GlowUpRequestHandler.  Serves the /ernie dashboard
page and four REST endpoints that expose live traffic from ernie's
BLE sniffer, rtl_433 TPMS decoder, and thermal sensor.

Endpoints
---------
- GET /ernie                  dashboard HTML
- GET /api/ernie/ble          BLE advertisements catalog (current state per MAC)
- GET /api/ernie/ble/events   BLE event log tail
- GET /api/ernie/tpms         TPMS decodes grouped by (model, id)
- GET /api/ernie/thermal      ernie's latest thermal reading + derived health

All four data endpoints are served from persistent PostgreSQL storage
owned by the loggers in ``infrastructure/`` — ``BleSnifferLogger``,
``TpmsLogger``, and ``ThermalLogger``.  The dashboard therefore
survives server restarts without losing accumulated sensor history,
unlike the earlier in-process dict/ring implementation.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "2.0"

import logging
import os
import time
from typing import Any

logger: logging.Logger = logging.getLogger("glowup.handlers.ernie")

# How many BLE events to return (tail of the event table) per request.
# 200 covers a few minutes of normal activity and keeps the client
# payload small; the full 30-day history lives in PG.
BLE_EVENTS_TAIL: int = 200

# Dashboard-polling windows.  The /ernie dashboard is a live view —
# "what's audible right now" — so the default endpoints return only
# recently-heard sensors.  Full history remains queryable by direct
# SQL (or by setting a longer window when a catalog view is built
# later).  Bound here rather than in the loggers so the logger
# API stays uncoupled from dashboard policy.
BLE_CATALOG_WINDOW_S: float = 600.0    # 10 minutes — BLE devices in range
TPMS_CATALOG_WINDOW_S: float = 7200.0  # 2 hours — vehicles seen today-ish

# Freshness thresholds (seconds) for the derived ``/api/ernie/thermal``
# health block.  Names mirror the sensor that produces each channel.
HEALTH_MOSQUITTO_WINDOW_S: int = 60
HEALTH_BLE_SNIFFER_WINDOW_S: int = 30
HEALTH_PI_THERMAL_WINDOW_S: int = 90

# Ernie's own hostname as reported by ``pi_thermal_sensor.py`` into
# ``ThermalLogger.latest()``.  Keeps a single spelling so a rename
# is one place.
ERNIE_NODE_ID: str = "ernie"


def _parse_window_s(path: str, default: float) -> float:
    """Extract ``?window_s=<seconds>`` from the request path.

    Returns the default when the query string is missing, blank, or
    not parseable as a non-negative float.  ``window_s=0`` is a
    supported request meaning "no window filter, full catalog" and
    is returned verbatim; negative values collapse to default
    because they have no useful meaning here.
    """
    if "?" not in path:
        return default
    query: str = path.split("?", 1)[1]
    for pair in query.split("&"):
        if "=" not in pair:
            continue
        key, raw = pair.split("=", 1)
        if key != "window_s":
            continue
        try:
            val: float = float(raw)
        except (TypeError, ValueError):
            return default
        if val < 0:
            return default
        return val
    return default


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
        """GET /api/ernie/ble — recently-heard BLE devices.

        Default window is :data:`BLE_CATALOG_WINDOW_S` (10 min) so
        the dashboard polls a bounded payload.  Clients that need
        the full historical catalog can pass ``?window_s=0`` (no
        filter) or any larger value.
        """
        window: float = _parse_window_s(
            getattr(self, "path", ""), BLE_CATALOG_WINDOW_S,
        )
        ble_log: Any = getattr(self, "ble_sniffer_logger", None)
        devices: list[dict] = (
            ble_log.catalog(window_s=window if window > 0 else None)
            if ble_log else []
        )
        self._send_json(200, {
            "devices": devices,
            "count": len(devices),
            "window_s": window,
            "timestamp": time.time(),
        })

    def _handle_get_ernie_ble_events(self) -> None:
        """GET /api/ernie/ble/events — tail of the BLE event log."""
        ble_log: Any = getattr(self, "ble_sniffer_logger", None)
        tail: list = ble_log.events_tail(BLE_EVENTS_TAIL) if ble_log else []
        self._send_json(200, {
            "events": tail,
            "count": len(tail),
            "timestamp": time.time(),
        })

    def _handle_get_ernie_tpms(self) -> None:
        """GET /api/ernie/tpms — recently-heard TPMS sensors.

        Default window is :data:`TPMS_CATALOG_WINDOW_S` (2 h),
        sized for the dashboard's "vehicles seen today-ish" use
        case.  ``?window_s=0`` drops the filter and returns the
        full retention-window catalog.
        """
        window: float = _parse_window_s(
            getattr(self, "path", ""), TPMS_CATALOG_WINDOW_S,
        )
        tpms_log: Any = getattr(self, "tpms_logger", None)
        entries: list[dict] = (
            tpms_log.unique_sensors(window_s=window if window > 0 else None)
            if tpms_log else []
        )
        self._send_json(200, {
            "sensors": entries,
            "count": len(entries),
            "window_s": window,
            "timestamp": time.time(),
        })

    def _handle_get_ernie_thermal(self) -> None:
        """GET /api/ernie/thermal — ernie's latest thermal + derived health.

        Service health is inferred from presence of recent traffic:

        - ``mosquitto``: any of BLE/TPMS/thermal within the freshness
          window (the bridge is alive if anything is flowing).
        - ``ble_sniffer``: needs a recent BLE ``last_heard_ts`` — the
          sniffer independently of whether the broker is carrying
          other traffic.
        - ``rtl433``: no cheap "alive" proxy since TPMS bursts are
          sporadic; reports ``None`` rather than guess.
        - ``pi_thermal``: heartbeat every 30 s, so 90 s tolerates a
          3-message gap before declaring down.
        """
        thermal_log: Any = getattr(self, "thermal_logger", None)
        latest: dict[str, Any] = thermal_log.latest() if thermal_log else {}
        reading: dict[str, Any] = latest.get(ERNIE_NODE_ID, {}) or {}
        thermal_ts: float = float(reading.get("timestamp", 0) or 0)

        ble_log: Any = getattr(self, "ble_sniffer_logger", None)
        last_ble: float = ble_log.last_heard_ts() if ble_log else 0.0

        tpms_log: Any = getattr(self, "tpms_logger", None)
        last_tpms: float = tpms_log.last_seen_ts() if tpms_log else 0.0

        now: float = time.time()
        any_recent: float = max(last_ble, last_tpms, thermal_ts)
        self._send_json(200, {
            "reading": reading,
            "health": {
                "mosquitto": (
                    (now - any_recent) < HEALTH_MOSQUITTO_WINDOW_S
                    if any_recent
                    else False
                ),
                "ble_sniffer": (
                    (now - last_ble) < HEALTH_BLE_SNIFFER_WINDOW_S
                    if last_ble
                    else False
                ),
                "rtl433": None,
                "pi_thermal": (
                    (now - thermal_ts) < HEALTH_PI_THERMAL_WINDOW_S
                    if thermal_ts
                    else False
                ),
            },
            "timestamp": now,
        })
