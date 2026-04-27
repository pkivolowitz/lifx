"""aisstream.io → MQTT bridge for the /maritime dashboard.

Subscribes to aisstream.io's free WebSocket feed (filtered by
bounding box), translates each incoming AIS message into the
AIS-catcher JSON shape that :class:`infrastructure.maritime_buffer.
MaritimeBuffer` already understands, and republishes onto an MQTT
topic distinct from the local-RX firehose so the dashboard can
render external-source vessels with a different style.

Why this exists
---------------

The local AIS-catcher receiver sees only what its antenna can
reach (Mobile-Bay-and-immediate-approaches in this deployment).
For wider situational awareness — a vessel inbound from the Gulf
that hasn't crossed the horizon yet, or shipping near Tampa — we
need a second source.  aisstream.io aggregates contributing
stations worldwide and exposes the result over a free WebSocket
keyed by an API key (free non-commercial signup).

Architecture
------------

- Runs anywhere with outbound HTTPS + LAN access to the hub MQTT
  broker.  In the glowup deployment this lives on a Mac host
  (LaunchAgent), keeping the hub Pi free for its many other jobs.
- Reads the API key from ``/etc/glowup/aisstream.conf`` (host-
  managed, NOT in repo).
- Reads the hub broker host/port from ``/etc/glowup/site.json``
  via the existing ``glowup_site`` shim.
- Filters at the source via the WebSocket subscription's
  ``BoundingBoxes`` — minimises bandwidth + irrelevant load.
- Translates aisstream's nested ``MetaData`` / ``Message`` shape
  into the flat AIS-catcher schema (``mmsi``, ``lat``, ``lon``,
  ``speed``, ``course``, ``heading``, ``shipname``, ``callsign``,
  ``shiptype``, ``shiptype_text``, ``destination``, etc.).
- Tags every emitted message with ``"source": "aisstream"`` so the
  buffer / dashboard can distinguish external-source vessels from
  locally-decoded ones.
- Reconnect-on-disconnect with exponential backoff (capped) — the
  WebSocket can drop on transient internet hiccups, and we'd
  rather burn a few seconds backing off than hammer the upstream.

Message-type coverage
---------------------

aisstream.io emits a tagged-union of message types.  We translate:

  - ``PositionReport`` (Class A msg 1/2/3)  → lat/lon/speed/course/
    heading + nav status
  - ``StandardClassBPositionReport`` (msg 18) → same fields, fewer
    nav status options
  - ``ExtendedClassBPositionReport`` (msg 19) → same + shipname/type
  - ``ShipStaticData`` (msg 5)  →  shipname, callsign, type,
    destination, dimensions
  - ``StaticDataReport`` (msg 24)  → Class B static fragments
    (Part A: name; Part B: type/callsign/dimensions)

Anything we don't recognise gets logged at debug and dropped — the
buffer doesn't need every message type to render the map.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import argparse
import asyncio
import json
import logging
import os
import signal
import ssl
import sys
import time
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False

try:
    import websockets
    _HAS_WS: bool = True
except ImportError:
    _HAS_WS = False

# certifi ships an up-to-date Mozilla root CA bundle; we need it to
# build an SSL context that can verify wss://stream.aisstream.io.
# macOS Python.framework installs do NOT ship with the system root
# CAs accessible to ssl.create_default_context(), so without an
# explicit cafile every WebSocket attempt fails with
# CERTIFICATE_VERIFY_FAILED.  certifi is a transitive dep of every
# requests / aiohttp / paho-derived stack so it is always already
# present in the venv.  Soft import: if absent, fall back to the
# system default and surface the certifi-missing case in the error
# rather than fail at import time.
try:
    import certifi
    _HAS_CERTIFI: bool = True
except ImportError:
    _HAS_CERTIFI = False

# Site config — single source of truth for the hub broker host/port.
try:
    from glowup_site import site as _site
except ImportError:
    _site = None


logger: logging.Logger = logging.getLogger("glowup.maritime.aisstream")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# aisstream.io WebSocket endpoint — free non-commercial tier.
_AISSTREAM_URL: str = "wss://stream.aisstream.io/v0/stream"

# MQTT topic for external-source AIS, distinct from the local
# AIS-catcher firehose so subscribers / dashboards can distinguish.
_TOPIC_EXTERNAL: str = "glowup/maritime/ais-external"

# QoS for external messages — same trade-off as the local firehose:
# fire-and-forget, an occasional drop is fine, dashboards are
# eventually consistent.
_PUBLISH_QOS: int = 0

# MQTT keepalive — match the local maritime services.
_MQTT_KEEPALIVE_S: int = 60

# Path to the host-managed secrets file containing the aisstream
# API key.  Format:  AISSTREAM_API_KEY=<key>
# Owner / mode:  root:root  0640 (or whatever the host policy
# requires; we just read it).
_API_KEY_PATH: str = "/etc/glowup/aisstream.conf"

# Reconnect backoff bounds.  Initial 2 s, doubles each consecutive
# failure, capped at 60 s.  Reset to initial on a successful frame.
_BACKOFF_INITIAL_S: float = 2.0
_BACKOFF_MAX_S: float = 60.0

# Default Gulf-of-Mexico bounding box.  Tampa Bay to Brownsville,
# out to the Yucatán channel — captures shipping inbound to all the
# US Gulf ports.  Override via --bbox at the CLI.
_DEFAULT_BBOX: tuple[float, float, float, float] = (
    24.0, -98.0, 31.0, -80.0,
)


# ---------------------------------------------------------------------------
# AIS shiptype → human label.  ITU-R M.1371 "Type of ship and cargo"
# code table.  The local AIS-catcher fills in shiptype_text itself
# from this same table; we replicate it here so external-source
# vessels light up with the right colour on the dashboard's
# type-keyed palette.  Numbers cover the whole 0-99 range; ranges
# share a label per the spec.
# ---------------------------------------------------------------------------

_SHIPTYPE_LABELS: dict[int, str] = {
    0:  "Not available",
    20: "Wing in ground (WIG)",
    21: "Wing in ground (WIG)",
    22: "Wing in ground (WIG)",
    23: "Wing in ground (WIG)",
    24: "Wing in ground (WIG)",
    25: "Wing in ground (WIG)",
    26: "Wing in ground (WIG)",
    27: "Wing in ground (WIG)",
    28: "Wing in ground (WIG)",
    29: "Wing in ground (WIG)",
    30: "Fishing",
    31: "Towing",
    32: "Towing",
    33: "Dredging",
    34: "Diving ops",
    35: "Military ops",
    36: "Sailing",
    37: "Pleasure craft",
    40: "High speed craft",
    41: "High speed craft",
    42: "High speed craft",
    43: "High speed craft",
    44: "High speed craft",
    45: "High speed craft",
    46: "High speed craft",
    47: "High speed craft",
    48: "High speed craft",
    49: "High speed craft",
    50: "Pilot Vessel",
    51: "Search and Rescue",
    52: "Tug",
    53: "Port tender",
    54: "Anti-pollution",
    55: "Law enforcement",
    58: "Medical transport",
    59: "Special craft",
}

# 60-69 Passenger; 70-79 Cargo; 80-89 Tanker; 90-99 Other.  Filled
# below to keep the literal table readable.
for _i in range(60, 70):
    _SHIPTYPE_LABELS[_i] = "Passenger"
for _i in range(70, 80):
    _SHIPTYPE_LABELS[_i] = "Cargo"
for _i in range(80, 90):
    _SHIPTYPE_LABELS[_i] = "Tanker"
for _i in range(90, 100):
    _SHIPTYPE_LABELS[_i] = "Other"


def _shiptype_text(code: Any) -> Optional[str]:
    """Return human label for an AIS shiptype int, or ``None``."""
    if not isinstance(code, int):
        return None
    return _SHIPTYPE_LABELS.get(code)


# ---------------------------------------------------------------------------
# Translation: aisstream JSON → AIS-catcher-shaped JSON.
# ---------------------------------------------------------------------------


def _translate(msg: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Convert one aisstream WebSocket message to AIS-catcher shape.

    aisstream wraps every payload with ``MetaData`` (MMSI, ShipName,
    coarse lat/lon, time) and ``Message`` (a tagged union keyed by
    message-type-name).  We pick the fields MaritimeBuffer's
    ``_MERGED_FIELDS`` set knows about.

    Returns ``None`` for unrecognised message types or malformed
    payloads — caller logs at debug.
    """
    mtype: Any = msg.get("MessageType")
    meta: Any = msg.get("MetaData") or {}
    body: Any = msg.get("Message") or {}
    if not isinstance(mtype, str) or not isinstance(meta, dict):
        return None

    mmsi: Any = meta.get("MMSI")
    if not isinstance(mmsi, int):
        return None

    out: dict[str, Any] = {
        "mmsi":        mmsi,
        "source":      "aisstream",
        "rxuxtime":    time.time(),
    }

    # Names / identifiers that may appear in MetaData even on
    # position-only messages.
    sn: Any = meta.get("ShipName")
    if isinstance(sn, str) and sn.strip():
        out["shipname"] = sn.strip()

    # The actual payload by type.  aisstream nests one inner dict
    # under the type name as the key.
    inner: Any = body.get(mtype) if isinstance(body, dict) else None
    if not isinstance(inner, dict):
        # Fall back to whatever MetaData supplied; some message
        # types (BaseStationReport, etc.) carry no MMSI-bound data
        # we want.  Drop.
        return None

    # Position fields.  aisstream uses TitleCase for nested keys;
    # the lat/lon may also live in MetaData (more precise on Class
    # B Extended).  Prefer inner where present.
    lat: Any = inner.get("Latitude")
    lon: Any = inner.get("Longitude")
    if lat is None:
        lat = meta.get("latitude")
    if lon is None:
        lon = meta.get("longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        # Filter out the AIS-protocol "no-position" sentinels.
        if not (lat == 91.0 and lon == 181.0):
            out["lat"] = float(lat)
            out["lon"] = float(lon)

    # Course / speed / heading.  AIS Class A and B share these
    # field names; aisstream emits TitleCase.  511 / 102.3 are the
    # protocol "unavailable" sentinels — emit them as-is and let
    # the dashboard's clamps handle them.
    cog: Any = inner.get("Cog")
    sog: Any = inner.get("Sog")
    th: Any = inner.get("TrueHeading")
    rot: Any = inner.get("RateOfTurn")
    nav: Any = inner.get("NavigationalStatus")
    if isinstance(cog, (int, float)):
        out["course"] = float(cog)
    if isinstance(sog, (int, float)):
        out["speed"] = float(sog)
    if isinstance(th, (int, float)):
        out["heading"] = int(th)
    if isinstance(rot, (int, float)):
        out["turn"] = rot
    if isinstance(nav, int):
        out["status"] = nav

    # Static-data fields (msg 5 ShipStaticData / msg 24 StaticData).
    name: Any = inner.get("Name")
    if isinstance(name, str) and name.strip():
        out["shipname"] = name.strip()
    cs: Any = inner.get("CallSign")
    if isinstance(cs, str) and cs.strip():
        out["callsign"] = cs.strip()
    st: Any = inner.get("Type") or inner.get("ShipType")
    if isinstance(st, int) and st > 0:
        out["shiptype"] = st
        text: Optional[str] = _shiptype_text(st)
        if text:
            out["shiptype_text"] = text
    dest: Any = inner.get("Destination")
    if isinstance(dest, str) and dest.strip():
        out["destination"] = dest.strip()

    # Dimensions (Class A msg 5 carries A/B/C/D offsets in metres).
    dim: Any = inner.get("Dimension")
    if isinstance(dim, dict):
        for src, dst in (("A", "to_bow"),   ("B", "to_stern"),
                         ("C", "to_port"),  ("D", "to_starboard")):
            v: Any = dim.get(src)
            if isinstance(v, int):
                out[dst] = v

    return out


# ---------------------------------------------------------------------------
# Bridge runtime.
# ---------------------------------------------------------------------------


def _read_api_key(path: str) -> str:
    """Read AISSTREAM_API_KEY from an env-style key=value file.

    Permissive parser: strips comments and blank lines, treats the
    first ``KEY=VALUE`` line whose key is ``AISSTREAM_API_KEY`` as
    the answer.  Raises ``RuntimeError`` (not silent fallback) when
    the file is missing or the key is absent — the bridge is
    useless without a valid key, and a clear failure is the right
    UX per the EnvironmentFile + program-side fail-fast convention.
    """
    if not os.path.exists(path):
        raise RuntimeError(
            f"aisstream config not found at {path}.  Create it with "
            f"a single line: AISSTREAM_API_KEY=<your-key>",
        )
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == "AISSTREAM_API_KEY":
                key: str = v.strip().strip("\"'")
                if not key:
                    raise RuntimeError(
                        f"AISSTREAM_API_KEY is empty in {path}",
                    )
                return key
    raise RuntimeError(
        f"AISSTREAM_API_KEY not found in {path}",
    )


class AISStreamBridge:
    """WebSocket → MQTT bridge for the aisstream.io feed."""

    def __init__(
        self,
        api_key: str,
        broker_host: str,
        broker_port: int,
        bbox: tuple[float, float, float, float],
    ) -> None:
        """See module docstring."""
        if not _HAS_PAHO:
            raise ImportError("paho-mqtt is required")
        if not _HAS_WS:
            raise ImportError(
                "websockets package is required.  Install with: "
                "~/venv/bin/pip install websockets",
            )
        self._api_key: str = api_key
        self._broker_host: str = broker_host
        self._broker_port: int = broker_port
        self._bbox: tuple[float, float, float, float] = bbox
        self._client: "mqtt.Client" = mqtt.Client(
            client_id="glowup-aisstream-bridge",
        )
        self._stopping: bool = False
        # Counters surfaced to systemd / launchd journal periodically
        # for at-a-glance health observation.
        self._n_in: int = 0
        self._n_pub: int = 0
        self._n_drop: int = 0
        self._last_log_ts: float = time.time()

    async def run(self) -> None:
        """Connect MQTT, then loop on the WebSocket forever."""
        try:
            self._client.connect(
                self._broker_host, self._broker_port, _MQTT_KEEPALIVE_S,
            )
        except Exception as exc:
            logger.error(
                "MQTT connect to %s:%d failed: %s",
                self._broker_host, self._broker_port, exc,
            )
            raise
        self._client.loop_start()
        logger.info(
            "MQTT connected to %s:%d", self._broker_host, self._broker_port,
        )

        backoff: float = _BACKOFF_INITIAL_S
        while not self._stopping:
            try:
                await self._stream_once()
                # If the WebSocket exited cleanly without raising,
                # the upstream closed; treat as transient and
                # reconnect after a short delay.
                logger.info(
                    "aisstream stream ended cleanly; reconnecting "
                    "after %.1f s", backoff,
                )
            except (ConnectionError, websockets.exceptions.WebSocketException) as exc:
                logger.warning(
                    "aisstream WebSocket error: %s — reconnecting "
                    "after %.1f s", exc, backoff,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "aisstream loop unexpected error: %s — "
                    "reconnecting after %.1f s", exc, backoff,
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, _BACKOFF_MAX_S)

        self._client.loop_stop()
        self._client.disconnect()

    async def _stream_once(self) -> None:
        """One WebSocket session — connect, subscribe, pump messages."""
        sub: dict[str, Any] = {
            "APIKey":             self._api_key,
            "BoundingBoxes":      [[
                [self._bbox[0], self._bbox[1]],
                [self._bbox[2], self._bbox[3]],
            ]],
            "FilterMessageTypes": [
                "PositionReport",
                "StandardClassBPositionReport",
                "ExtendedClassBPositionReport",
                "ShipStaticData",
                "StaticDataReport",
            ],
        }
        # Build an SSL context pinned to certifi's CA bundle when
        # available — works around macOS Python.framework's lack of
        # system root CAs.  See _HAS_CERTIFI comment at imports.
        ssl_ctx: ssl.SSLContext
        if _HAS_CERTIFI:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        else:
            ssl_ctx = ssl.create_default_context()
        async with websockets.connect(
            _AISSTREAM_URL, ssl=ssl_ctx,
        ) as ws:
            await ws.send(json.dumps(sub))
            logger.info(
                "aisstream subscribed: bbox=%s message_types=%d",
                self._bbox, len(sub["FilterMessageTypes"]),
            )
            async for raw in ws:
                if self._stopping:
                    break
                self._handle_message(raw)
                self._maybe_log_stats()

    def _handle_message(self, raw: Any) -> None:
        """Translate one WebSocket frame and publish to MQTT."""
        self._n_in += 1
        try:
            msg: Any = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.debug("aisstream non-JSON frame dropped: %s", exc)
            self._n_drop += 1
            return
        if not isinstance(msg, dict):
            self._n_drop += 1
            return
        # aisstream sends an authentication-failure error message as
        # a top-level "error" key.  Surface it loudly — silent
        # ignore would burn through reconnect attempts forever.
        if "error" in msg:
            logger.error(
                "aisstream upstream error: %s",
                msg.get("error"),
            )
            self._n_drop += 1
            return
        translated: Optional[dict[str, Any]] = _translate(msg)
        if translated is None:
            self._n_drop += 1
            return
        try:
            payload: str = json.dumps(translated)
        except (TypeError, ValueError) as exc:
            logger.debug("aisstream payload serialise failed: %s", exc)
            self._n_drop += 1
            return
        try:
            self._client.publish(
                _TOPIC_EXTERNAL, payload,
                qos=_PUBLISH_QOS, retain=False,
            )
            self._n_pub += 1
        except Exception as exc:
            logger.debug("aisstream MQTT publish failed: %s", exc)
            self._n_drop += 1

    def _maybe_log_stats(self) -> None:
        """Log throughput stats every 60 s for journal-side health."""
        now: float = time.time()
        if now - self._last_log_ts >= 60.0:
            logger.info(
                "aisstream stats: in=%d pub=%d drop=%d (last %.0f s)",
                self._n_in, self._n_pub, self._n_drop,
                now - self._last_log_ts,
            )
            self._last_log_ts = now

    def stop(self) -> None:
        """Graceful shutdown signal — stops the run-loop."""
        self._stopping = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="glowup-aisstream-bridge",
        description="Bridge aisstream.io AIS feed onto MQTT.",
    )
    p.add_argument(
        "--broker", default=None,
        help="Hub MQTT broker host (default: site.json hub_broker)",
    )
    p.add_argument(
        "--port", type=int, default=None,
        help="Hub MQTT broker port (default: site.json hub_broker_port)",
    )
    p.add_argument(
        "--bbox", default=None,
        help="Bounding box 'lat_min,lon_min,lat_max,lon_max' "
             "(default: 24,-98,31,-80 = Gulf of Mexico)",
    )
    p.add_argument(
        "--api-key-path", default=_API_KEY_PATH,
        help=f"Path to AISSTREAM_API_KEY config (default: {_API_KEY_PATH})",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def _resolve_broker(arg: Optional[str], port: Optional[int]) -> tuple[str, int]:
    """Resolve broker host/port from CLI or site.json.

    glowup_site exposes a ``.get(key, default)`` / ``.require(key)``
    API rather than attributes — the dict-like form makes it explicit
    which keys are looked up and lets us share the missing-key
    error message with the rest of the program.

    Site keys consumed:
      - ``hub_broker``  — required when --broker is not passed
      - ``hub_port``    — optional, default 1883
    """
    host: Optional[str] = arg
    if host is None and _site is not None:
        host = _site.get("hub_broker")
    if not host:
        raise RuntimeError(
            "MQTT broker not specified (no --broker, no "
            "hub_broker in site.json)",
        )
    p: Optional[int] = port
    if p is None and _site is not None:
        p = _site.get("hub_port")
    if not p:
        p = 1883
    return (host, int(p))


def _parse_bbox(s: Optional[str]) -> tuple[float, float, float, float]:
    if s is None:
        return _DEFAULT_BBOX
    parts: list[str] = [x.strip() for x in s.split(",")]
    if len(parts) != 4:
        raise RuntimeError("--bbox must be 'lat_min,lon_min,lat_max,lon_max'")
    return (float(parts[0]), float(parts[1]),
            float(parts[2]), float(parts[3]))


def main(argv: Optional[list[str]] = None) -> int:
    args: argparse.Namespace = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    api_key: str = _read_api_key(args.api_key_path)
    broker_host, broker_port = _resolve_broker(args.broker, args.port)
    bbox: tuple[float, float, float, float] = _parse_bbox(args.bbox)

    bridge: AISStreamBridge = AISStreamBridge(
        api_key=api_key,
        broker_host=broker_host,
        broker_port=broker_port,
        bbox=bbox,
    )

    loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _on_signal(signum: int, frame: Any) -> None:
        logger.info("received signal %d — stopping", signum)
        bridge.stop()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        loop.run_until_complete(bridge.run())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
