"""GlowUp Zigbee service — runs on broker-2, owns Zigbee end-to-end.

Single-file service replacing the hub-side ZigbeeAdapter, watchdog
machinery, PowerLogger subscription chain, and cross-host MQTT
subscription that produced the zombie-reconnect loop documented
across seven commits of flip-flop fixes.

Design principles:

- **broker-2 owns the Zigbee radio, so broker-2 owns Zigbee data.**
  The service runs ON broker-2, subscribes to the LOCAL Z2M mosquitto
  (``localhost:1883``), and is the authoritative source for Zigbee
  device state and history.

- **Cross-host subscribes are fragile.  Cross-host publishes are not.**
  The service pushes real-time signals to hub mosquitto at
  ``10.0.0.214:1883`` using the existing ``glowup/signals/{device}:{prop}``
  schema.  Publishers notice failed publishes immediately — no
  watchdog, no rebuild-client, no half-open zombie loop.

- **HTTP for pull, MQTT publish for push.**
  Dashboards and command clients hit the HTTP API on port 8422.
  SOE operators on hub keep subscribing to their local signal bus
  exactly as before — they see no change.

- **One file, no base classes, no framework magic.**
  Paho client, psycopg2, stdlib http.server.  No framework.

Endpoints::

    GET  /health                         → {"status":"ok", ...}
    GET  /devices                        → [{name, online, power_w, state, ...}, ...]
    GET  /devices/{name}                 → full current state dict
    GET  /devices/{name}/history?hours=24&resolution=60
                                         → [{ts, watts}, ...]
    GET  /summary?days=7&rate=0.13       → {total_kwh, cost_usd, per_device:{...}}
    POST /devices/{name}/state           body {"state":"ON"|"OFF"}

Ports:

- HTTP:  :8422
- Z2M MQTT: localhost:1883  (Z2M publishes zigbee2mqtt/#)
- Hub MQTT: 10.0.0.214:1883  (service publishes glowup/signals/*)

Config is via env vars (systemd unit sets them), not a config file —
the service is so small that config-as-env is appropriate.

See ``glowup-zigbee-service.service`` for the systemd unit.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "1.1"

import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

try:
    import paho.mqtt.client as mqtt
except ImportError as exc:
    raise SystemExit(
        "paho-mqtt is required: pip install paho-mqtt"
    ) from exc

try:
    import psycopg2
except ImportError as exc:
    raise SystemExit(
        "psycopg2 is required: pip install psycopg2-binary"
    ) from exc

# ---------------------------------------------------------------------------
# Configuration — env-driven so systemd unit owns the deployment parameters.
# ---------------------------------------------------------------------------

# HTTP listener.
HTTP_BIND: str = os.environ.get("GLZ_HTTP_BIND", "0.0.0.0")
HTTP_PORT: int = int(os.environ.get("GLZ_HTTP_PORT", "8422"))

# Local Z2M MQTT (the broker on THIS host).
Z2M_BROKER: str = os.environ.get("GLZ_Z2M_BROKER", "localhost")
Z2M_PORT: int = int(os.environ.get("GLZ_Z2M_PORT", "1883"))
Z2M_PREFIX: str = os.environ.get("GLZ_Z2M_PREFIX", "zigbee2mqtt")

# Hub MQTT (where glowup signal subscribers live).  Leave empty to
# disable signal publishing and run as a local-only service.
HUB_BROKER: str = os.environ.get("GLZ_HUB_BROKER", "10.0.0.214")
HUB_PORT: int = int(os.environ.get("GLZ_HUB_PORT", "1883"))
HUB_SIGNAL_PREFIX: str = os.environ.get("GLZ_HUB_SIGNAL_PREFIX", "glowup/signals")

# PostgreSQL DSN for history storage.
DB_DSN: str = os.environ.get(
    "GLZ_DB_DSN", "postgresql://glowup:changeme@10.0.0.111:5432/glowup",
)

# History retention and prune cadence (matches PowerLogger / ThermalLogger).
_RETENTION_SECONDS: float = 7 * 24 * 3600
_PRUNE_EVERY: int = 100

# Default electricity rate ($/kWh) for summary rollups.  Configurable
# per request via ?rate= query parameter.
DEFAULT_RATE_USD_PER_KWH: float = float(
    os.environ.get("GLZ_RATE_USD_PER_KWH", "0.13"),
)

# How often to commit pending sqlite rows.
DB_FLUSH_INTERVAL_SEC: float = 5.0

# Command publish QoS.  1 = at-least-once (Z2M commands should arrive).
CMD_QOS: int = 1

# Signal publish QoS.  0 = fire-and-forget (matches existing signal bus).
SIGNAL_QOS: int = 0

# Command state-change wait timeout — how long POST /devices/{name}/state
# blocks waiting for the echoed new state before returning.
CMD_ECHO_TIMEOUT_SEC: float = 5.0

# Device-type taxonomy lives in a sibling module so clients of this
# service (hub dashboard, hub scheduler, voice coordinator) can import
# the constants and inference function without pulling paho/sqlite.
# Re-exported below so existing callers importing from service.py keep
# working.
#
# Dual-mode import: in-repo service.py runs as a package member
# (``zigbee_service.service``); deployed to broker-2 it runs as a bare
# script from /opt/glowup-zigbee/ with device_types.py beside it.  The
# fallback handles the flat-layout case without requiring a packaging
# change on broker-2.
try:
    from zigbee_service.device_types import (  # noqa: F401 (re-export)
        KNOWN_TYPES,
        TYPE_BUTTON,
        TYPE_CONTACT,
        TYPE_MOTION,
        TYPE_PLUG,
        TYPE_SOIL,
        TYPE_UNKNOWN,
        infer_device_type,
    )
except ImportError:
    from device_types import (  # type: ignore[no-redef]  # noqa: F401
        KNOWN_TYPES,
        TYPE_BUTTON,
        TYPE_CONTACT,
        TYPE_MOTION,
        TYPE_PLUG,
        TYPE_SOIL,
        TYPE_UNKNOWN,
        infer_device_type,
    )

logger: logging.Logger = logging.getLogger("glowup.zigbee_service")


# ---------------------------------------------------------------------------
# History database — PostgreSQL, per-device time series.
# ---------------------------------------------------------------------------

_PG_DDL: str = """
CREATE TABLE IF NOT EXISTS zigbee_readings (
    id BIGSERIAL PRIMARY KEY,
    ts DOUBLE PRECISION NOT NULL,
    device TEXT NOT NULL,
    power_w REAL,
    state SMALLINT,
    voltage REAL,
    current_a REAL,
    energy_kwh REAL
);
CREATE INDEX IF NOT EXISTS idx_zigbee_readings_device_ts
    ON zigbee_readings(device, ts);
"""


class HistoryDB:
    """Thread-safe PostgreSQL wrapper for Zigbee device history.

    All access is serialised by ``self._lock`` so psycopg2's connection
    (not thread-safe by itself) is safe from concurrent callers.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn: str = dsn
        self._lock: threading.Lock = threading.Lock()
        self._write_count: int = 0
        self._conn = psycopg2.connect(dsn, connect_timeout=10)
        self._conn.autocommit = True
        with self._conn.cursor() as cur:
            cur.execute(_PG_DDL)

    def append(
        self,
        device: str,
        power_w: Optional[float],
        state: Optional[int],
        voltage: Optional[float],
        current_a: Optional[float],
        energy_kwh: Optional[float],
    ) -> None:
        """Append a single reading row; prune every _PRUNE_EVERY writes."""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO zigbee_readings"
                    " (ts, device, power_w, state, voltage, current_a, energy_kwh)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (time.time(), device, power_w, state, voltage, current_a,
                     energy_kwh),
                )
                self._write_count += 1
                if self._write_count % _PRUNE_EVERY == 0:
                    cur.execute(
                        "DELETE FROM zigbee_readings WHERE ts < %s",
                        (time.time() - _RETENTION_SECONDS,),
                    )

    def history(
        self, device: str, hours: float, resolution_sec: int,
    ) -> list[dict[str, Any]]:
        """Return power_w samples for *device* over the last *hours*.

        Downsamples to *resolution_sec* buckets using AVG().
        """
        cutoff: float = time.time() - hours * 3600.0
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT floor(ts / %s)::bigint * %s AS bucket,"
                    "       AVG(power_w), AVG(voltage), SUM(energy_kwh)"
                    " FROM zigbee_readings"
                    " WHERE device = %s AND ts >= %s AND power_w IS NOT NULL"
                    " GROUP BY bucket ORDER BY bucket",
                    (resolution_sec, resolution_sec, device, cutoff),
                )
                rows = cur.fetchall()
        return [
            {
                "ts": int(bucket),
                "watts": round(float(p), 2) if p is not None else None,
                "voltage": round(float(v), 1) if v is not None else None,
                "energy_kwh_delta": round(float(e), 4) if e is not None else None,
            }
            for bucket, p, v, e in rows
        ]

    def summary(
        self, days: int,
    ) -> dict[str, dict[str, float]]:
        """Return {device: {avg_w, peak_w, kwh}} over the last *days*.

        kwh is an integration estimate (trapezoid) rather than the raw
        energy_kwh delta, since some plugs don't report energy and we
        want coverage.
        """
        cutoff: float = time.time() - days * 86400.0
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT device, AVG(power_w), MAX(power_w), COUNT(*)"
                    " FROM zigbee_readings"
                    " WHERE ts >= %s AND power_w IS NOT NULL"
                    " GROUP BY device",
                    (cutoff,),
                )
                rows = cur.fetchall()
        out: dict[str, dict[str, float]] = {}
        secs_window: float = max(1.0, days * 86400.0)
        for device, avg_w, peak_w, count in rows:
            if avg_w is None:
                continue
            kwh: float = (float(avg_w) * secs_window) / 3600.0 / 1000.0
            out[device] = {
                "avg_w": round(float(avg_w), 2),
                "peak_w": round(float(peak_w or 0.0), 2),
                "samples": int(count),
                "kwh": round(kwh, 3),
            }
        return out

    def close(self) -> None:
        """Close the PostgreSQL connection."""
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# Current state — in-memory snapshot of every Zigbee device.
# ---------------------------------------------------------------------------

@dataclass
class DeviceState:
    """Current state of one Zigbee device."""
    name: str
    online: bool = True
    last_seen: float = 0.0
    power_w: Optional[float] = None
    state: Optional[str] = None  # "ON"/"OFF" for plugs
    voltage: Optional[float] = None
    current_a: Optional[float] = None
    energy_kwh: Optional[float] = None
    # Device classification; see infer_device_type.  Defaults to
    # TYPE_UNKNOWN until the first payload with a distinguishing
    # fingerprint arrives — the registry re-runs inference after every
    # update, so classification is sticky once raw accumulates.
    type: str = TYPE_UNKNOWN
    # All raw properties from the last Z2M message — for devices we
    # don't specially understand (soil sensors, etc.).
    raw: dict[str, Any] = None  # type: ignore

    def __post_init__(self) -> None:
        if self.raw is None:
            self.raw = {}

    def to_dict(self) -> dict[str, Any]:
        """Serialize current state to a JSON-safe dict."""
        return {
            "name": self.name,
            "type": self.type,
            "online": self.online,
            "last_seen": self.last_seen,
            "age_sec": round(time.time() - self.last_seen, 1)
                       if self.last_seen else None,
            "power_w": self.power_w,
            "state": self.state,
            "voltage": self.voltage,
            "current_a": self.current_a,
            "energy_kwh": self.energy_kwh,
            "raw": self.raw,
        }


class StateRegistry:
    """Thread-safe in-memory device state registry."""

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._devices: dict[str, DeviceState] = {}
        # Per-device Events used to wake command waiters.
        self._state_events: dict[str, threading.Event] = {}

    def update(self, name: str, payload: dict[str, Any]) -> DeviceState:
        """Merge a Z2M message into current state for *name*.

        Returns the updated DeviceState so callers can publish signals.
        """
        with self._lock:
            dev: DeviceState = self._devices.setdefault(
                name, DeviceState(name=name),
            )
            dev.last_seen = time.time()
            # Pull known numeric properties if present.
            if "power" in payload:
                try:
                    dev.power_w = float(payload["power"])
                except (TypeError, ValueError):
                    pass
            if "voltage" in payload:
                try:
                    dev.voltage = float(payload["voltage"])
                except (TypeError, ValueError):
                    pass
            if "current" in payload:
                try:
                    dev.current_a = float(payload["current"])
                except (TypeError, ValueError):
                    pass
            if "energy" in payload:
                try:
                    dev.energy_kwh = float(payload["energy"])
                except (TypeError, ValueError):
                    pass
            if "state" in payload:
                raw_state: Any = payload["state"]
                if isinstance(raw_state, str):
                    dev.state = raw_state.upper()
            # Keep a merged snapshot of the raw payload for opaque
            # devices (soil sensors, etc.).
            dev.raw.update(payload)
            # Re-run type inference against the accumulated raw.  Cheap
            # (dict membership checks) and sticky — once a soil sensor
            # has ever reported soil_moisture it stays classified as
            # soil even if a later heartbeat only carries linkquality.
            dev.type = infer_device_type(dev.raw)
            # Fire any waiting Event so command handlers can unblock.
            evt: Optional[threading.Event] = self._state_events.get(name)
            if evt is not None:
                evt.set()
            return dev

    def set_availability(self, name: str, online: bool) -> None:
        """Update the online/offline status of a device."""
        with self._lock:
            dev: DeviceState = self._devices.setdefault(
                name, DeviceState(name=name),
            )
            dev.online = online
            dev.last_seen = time.time()

    def get(self, name: str) -> Optional[DeviceState]:
        """Return the current state of a device by name, or None."""
        with self._lock:
            return self._devices.get(name)

    def snapshot(self) -> list[DeviceState]:
        """Return a list of all tracked device states."""
        with self._lock:
            return list(self._devices.values())

    def wait_for_state_change(
        self, name: str, timeout: float,
    ) -> bool:
        """Block until *name* publishes a new message or timeout."""
        with self._lock:
            evt: threading.Event = self._state_events.setdefault(
                name, threading.Event(),
            )
            evt.clear()
        return evt.wait(timeout)


# ---------------------------------------------------------------------------
# MQTT wiring — Z2M subscriber on localhost + hub signal publisher.
# ---------------------------------------------------------------------------

# Property suffixes we publish as individual signals on the hub bus.
# Using the existing convention <device>:<prop>.
_SIGNAL_PROPS: tuple[str, ...] = (
    "power", "voltage", "current", "energy",
    "humidity", "temperature", "soil_moisture",
    "battery", "linkquality",
)

# state→numeric map for the signal bus convention (1.0 on, 0.0 off).
_STATE_NUMERIC: dict[str, float] = {"ON": 1.0, "OFF": 0.0}


class Z2MClient:
    """Local paho client subscribed to ``zigbee2mqtt/#`` on localhost."""

    def __init__(
        self,
        registry: StateRegistry,
        history: HistoryDB,
        hub_publisher: "HubPublisher",
    ) -> None:
        self._registry: StateRegistry = registry
        self._history: HistoryDB = history
        self._hub: HubPublisher = hub_publisher
        self._client: mqtt.Client = mqtt.Client(
            client_id=f"glowup-zigbee-service-z2m-{os.getpid()}",
            clean_session=True,
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def start(self) -> None:
        """Connect to the local Z2M MQTT broker and start the event loop."""
        logger.info(
            "Z2M client connecting to %s:%d (%s/#)",
            Z2M_BROKER, Z2M_PORT, Z2M_PREFIX,
        )
        self._client.connect(Z2M_BROKER, Z2M_PORT, keepalive=30)
        self._client.loop_start()

    def stop(self) -> None:
        """Stop the MQTT event loop and disconnect."""
        self._client.loop_stop()
        self._client.disconnect()

    def publish(self, topic: str, payload: str) -> None:
        """Publish to the LOCAL Z2M broker — used for sending commands."""
        self._client.publish(topic, payload, qos=CMD_QOS)

    def _on_connect(
        self, client: mqtt.Client, userdata: Any, flags: Any, rc: int,
        *args: Any,
    ) -> None:
        if rc != 0:
            logger.error("Z2M connect failed rc=%s", rc)
            return
        client.subscribe(f"{Z2M_PREFIX}/#", qos=0)
        logger.info("Z2M subscribed to %s/#", Z2M_PREFIX)

    def _on_message(
        self, client: mqtt.Client, userdata: Any, msg: Any,
    ) -> None:
        topic: str = msg.topic
        # Strip the leading prefix.
        if not topic.startswith(Z2M_PREFIX + "/"):
            return
        remainder: str = topic[len(Z2M_PREFIX) + 1:]

        # Ignore bridge/* internal topics — not device data.
        if remainder.startswith("bridge/"):
            return

        # Availability topic: zigbee2mqtt/<device>/availability
        if remainder.endswith("/availability"):
            device: str = remainder[: -len("/availability")]
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
                online: bool = (
                    payload.get("state") == "online"
                    if isinstance(payload, dict)
                    else str(payload).strip().lower() == "online"
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            self._registry.set_availability(device, online)
            self._hub.publish_availability(device, online)
            return

        # Anything else with a slash is a subtopic we don't handle yet
        # (config, get, set, etc.).  Skip.
        if "/" in remainder:
            return

        # Base topic: zigbee2mqtt/<device> — device state payload.
        device = remainder
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug("skip malformed payload for %s: %s", device, exc)
            return
        if not isinstance(payload, dict):
            return

        dev: DeviceState = self._registry.update(device, payload)

        # Persist a history row if we got any metering value.
        if any(k in payload for k in ("power", "voltage", "current", "energy", "state")):
            state_num: Optional[int] = None
            if dev.state in _STATE_NUMERIC:
                state_num = int(_STATE_NUMERIC[dev.state])
            self._history.append(
                device=device,
                power_w=dev.power_w,
                state=state_num,
                voltage=dev.voltage,
                current_a=dev.current_a,
                energy_kwh=dev.energy_kwh,
            )

        # Publish real-time signals to the hub bus — one topic per
        # known property so existing operators see the same schema
        # as before.
        self._hub.publish_properties(device, payload)


class HubPublisher:
    """Cross-host publisher to hub mosquitto for the glowup signal bus."""

    def __init__(self) -> None:
        self._enabled: bool = bool(HUB_BROKER)
        self._client: Optional[mqtt.Client] = None
        if self._enabled:
            self._client = mqtt.Client(
                client_id=f"glowup-zigbee-service-hub-{os.getpid()}",
                clean_session=True,
            )
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect

    def start(self) -> None:
        """Connect to the hub MQTT broker with async reconnect."""
        if not self._enabled or self._client is None:
            logger.info("Hub publisher disabled (no GLZ_HUB_BROKER)")
            return
        logger.info("Hub publisher connecting to %s:%d", HUB_BROKER, HUB_PORT)
        # Wire paho's own logger so we see protocol-level detail.
        self._client.enable_logger(
            logging.getLogger("glowup.zigbee_service.paho_hub"),
        )
        # connect_async + loop_start gives us automatic reconnects on
        # publish failures with no code on our side.
        self._client.connect_async(HUB_BROKER, HUB_PORT, keepalive=30)
        self._client.loop_start()

    def stop(self) -> None:
        """Stop the hub publisher event loop and disconnect."""
        if self._client is None:
            return
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(
        self, client: mqtt.Client, userdata: Any, flags: Any, rc: int,
        *args: Any,
    ) -> None:
        if rc != 0:
            logger.warning("Hub publisher connect failed rc=%s", rc)
            return
        logger.info("Hub publisher connected to %s:%d", HUB_BROKER, HUB_PORT)

    def _on_disconnect(
        self, client: mqtt.Client, userdata: Any, rc: int, *args: Any,
    ) -> None:
        level: int = logging.INFO if rc == 0 else logging.WARNING
        logger.log(level, "Hub publisher disconnected rc=%s", rc)
        # paho auto-reconnects when loop_start is active.

    def publish_properties(self, device: str, payload: dict[str, Any]) -> None:
        """Emit <device>:<prop> signals for every known numeric property."""
        if not self._enabled or self._client is None:
            logger.debug(
                "publish skipped — enabled=%s client=%s",
                self._enabled, self._client,
            )
            return
        published: list[str] = []
        for prop in _SIGNAL_PROPS:
            if prop not in payload:
                continue
            val: Any = payload[prop]
            try:
                fval: float = float(val)
            except (TypeError, ValueError):
                continue
            topic: str = f"{HUB_SIGNAL_PREFIX}/{device}:{prop}"
            info = self._client.publish(topic, f"{fval}", qos=SIGNAL_QOS)
            published.append(f"{prop}={fval} rc={info.rc}")
        # state: ON/OFF → 1.0/0.0
        raw_state: Any = payload.get("state")
        if isinstance(raw_state, str) and raw_state.upper() in _STATE_NUMERIC:
            fval = _STATE_NUMERIC[raw_state.upper()]
            topic = f"{HUB_SIGNAL_PREFIX}/{device}:state"
            info = self._client.publish(topic, f"{fval}", qos=SIGNAL_QOS)
            published.append(f"state={fval} rc={info.rc}")
        if published:
            logger.info("pub %s → %s", device, ", ".join(published))

    def publish_availability(self, device: str, online: bool) -> None:
        """Publish a device's availability as a numeric signal to the hub."""
        if not self._enabled or self._client is None:
            return
        topic: str = f"{HUB_SIGNAL_PREFIX}/{device}:_availability"
        self._client.publish(
            topic, "1.0" if online else "0.0", qos=SIGNAL_QOS,
        )


# ---------------------------------------------------------------------------
# HTTP API — tiny stdlib handler, JSON responses.
# ---------------------------------------------------------------------------

class ZigbeeHTTPHandler(BaseHTTPRequestHandler):
    """BaseHTTPRequestHandler subclass servicing the Zigbee API."""

    # Injected on the server instance in ``serve()``.
    registry: StateRegistry  # type: ignore
    history: HistoryDB  # type: ignore
    z2m: Z2MClient  # type: ignore

    def log_message(self, fmt: str, *args: Any) -> None:
        """Route HTTP request logging through the application logger."""
        logger.debug("http %s", fmt % args)

    def _send_json(self, status: int, body: Any) -> None:
        data: bytes = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        """Handle GET requests for health, devices, history, and summary."""
        parsed = urlparse(self.path)
        path: str = parsed.path.rstrip("/") or "/"
        qs: dict[str, list[str]] = parse_qs(parsed.query)

        if path == "/health":
            self._send_json(200, {
                "status": "ok",
                "version": __version__,
                "devices_tracked": len(self.registry.snapshot()),
                "ts": time.time(),
            })
            return

        if path == "/devices":
            # Optional ?type= filter — unknown type names yield an empty
            # list rather than 400 so dashboards tolerate rolling
            # upgrades (hub built against a newer taxonomy than service).
            type_filter: Optional[str] = None
            if "type" in qs and qs["type"]:
                type_filter = qs["type"][0]
            devs: list[dict[str, Any]] = [
                d.to_dict() for d in self.registry.snapshot()
                if type_filter is None or d.type == type_filter
            ]
            devs.sort(key=lambda d: d["name"])
            self._send_json(200, {"devices": devs})
            return

        if path.startswith("/devices/"):
            parts: list[str] = path[len("/devices/"):].split("/", 1)
            name: str = parts[0]
            rest: str = parts[1] if len(parts) > 1 else ""

            if rest == "":
                dev = self.registry.get(name)
                if dev is None:
                    self._send_json(404, {"error": f"unknown device: {name}"})
                    return
                self._send_json(200, dev.to_dict())
                return

            if rest == "history":
                hours: float = float(qs.get("hours", ["24"])[0])
                resolution: int = int(qs.get("resolution", ["60"])[0])
                readings = self.history.history(name, hours, resolution)
                self._send_json(200, {
                    "device": name,
                    "hours": hours,
                    "resolution": resolution,
                    "readings": readings,
                })
                return

            self._send_json(404, {"error": f"unknown sub-path: {rest}"})
            return

        if path == "/summary":
            days: int = int(qs.get("days", ["7"])[0])
            rate: float = float(qs.get("rate", [str(DEFAULT_RATE_USD_PER_KWH)])[0])
            per_device: dict[str, dict[str, float]] = self.history.summary(days)
            total_kwh: float = sum(d["kwh"] for d in per_device.values())
            self._send_json(200, {
                "days": days,
                "rate_usd_per_kwh": rate,
                "total_kwh": round(total_kwh, 3),
                "total_cost_usd": round(total_kwh * rate, 2),
                "per_device": {
                    name: {
                        **data,
                        "cost_usd": round(data["kwh"] * rate, 2),
                    }
                    for name, data in per_device.items()
                },
            })
            return

        self._send_json(404, {"error": f"unknown path: {path}"})

    def do_POST(self) -> None:
        """Handle POST requests for device state commands (ON/OFF)."""
        parsed = urlparse(self.path)
        path: str = parsed.path.rstrip("/")

        if path.startswith("/devices/") and path.endswith("/state"):
            name: str = path[len("/devices/"): -len("/state")]
            content_len: int = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(content_len) or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON body"})
                return
            desired: Any = body.get("state")
            if not isinstance(desired, str) or desired.upper() not in ("ON", "OFF"):
                self._send_json(400, {
                    "error": "body must be {\"state\": \"ON\"|\"OFF\"}",
                })
                return
            desired = desired.upper()

            # Publish the command to Z2M on localhost.  Z2M handles the
            # radio-level send and publishes the new state back when
            # the device acknowledges.
            cmd_topic: str = f"{Z2M_PREFIX}/{name}/set"
            cmd_payload: str = json.dumps({"state": desired})
            self.z2m.publish(cmd_topic, cmd_payload)
            logger.info("cmd → %s: %s", name, cmd_payload)

            # Wait for the device to echo the new state, up to timeout.
            got: bool = self.registry.wait_for_state_change(
                name, timeout=CMD_ECHO_TIMEOUT_SEC,
            )
            dev = self.registry.get(name)
            if not got or dev is None:
                self._send_json(504, {
                    "device": name,
                    "desired": desired,
                    "echoed": False,
                    "error": "timed out waiting for device to acknowledge",
                })
                return
            self._send_json(200, {
                "device": name,
                "desired": desired,
                "echoed": True,
                "current_state": dev.state,
                "power_w": dev.power_w,
            })
            return

        self._send_json(404, {"error": f"unknown path: {path}"})


# ---------------------------------------------------------------------------
# Wiring — main entry.
# ---------------------------------------------------------------------------

def serve() -> None:
    """Spin everything up and block forever serving HTTP."""
    logging.basicConfig(
        level=os.environ.get("GLZ_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    history = HistoryDB(DB_DSN)
    registry = StateRegistry()
    hub_publisher = HubPublisher()
    hub_publisher.start()

    z2m = Z2MClient(registry, history, hub_publisher)
    z2m.start()

    server = ThreadingHTTPServer((HTTP_BIND, HTTP_PORT), ZigbeeHTTPHandler)
    # Attach dependencies to the handler class — instances look them up.
    ZigbeeHTTPHandler.registry = registry  # type: ignore
    ZigbeeHTTPHandler.history = history  # type: ignore
    ZigbeeHTTPHandler.z2m = z2m  # type: ignore

    def _shutdown(signum: int, frame: Any) -> None:
        logger.info("Shutting down (signal %d)", signum)
        try:
            z2m.stop()
            hub_publisher.stop()
            history.close()
        finally:
            server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info(
        "glowup-zigbee-service v%s listening on %s:%d",
        __version__, HTTP_BIND, HTTP_PORT,
    )
    server.serve_forever()


if __name__ == "__main__":
    serve()
