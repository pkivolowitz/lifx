"""In-memory ring buffer for the /airwaves dashboard.

Subscribes to ``glowup/sub_ghz/raw`` (the rtl_433 firehose published
by :mod:`meters.publisher`) and keeps the last N decoded packets in
RAM.  Backs the live RF activity feed; explicitly **not persisted**.
The whole point of this module is "what's on the airwaves around the
house right now" — a curiosity surface, not a measurement record.

The durable measurement path is :mod:`infrastructure.meter_logger`,
which subscribes to a different (filtered) topic.

Threading: paho's ``loop_start()`` runs the network in a background
thread; ``on_message`` callbacks land there.  All buffer mutations go
through a single :class:`threading.Lock`.  Reads are short — copy the
deque under the lock and return — so HTTP handlers never block the
network thread for more than a few microseconds.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import collections
import json
import logging
import threading
import time
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False


logger: logging.Logger = logging.getLogger("glowup.airwaves")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic the publisher fires every decoded rtl_433 packet onto.
# Mirrored on the publisher side as ``meters.publisher._RAW_TOPIC``.
RAW_TOPIC: str = "glowup/sub_ghz/raw"

# How many packets to keep in the ring.  500 at ~5 packets/min works
# out to roughly 1.5 hours of history — enough to scroll back and see
# the morning's garage-door activity, short enough that memory cost
# is trivial (<1 MB).  Tunable via the AirwavesBuffer constructor.
DEFAULT_RING_SIZE: int = 500

# paho keepalive (seconds).  Same value the meter logger uses.
_MQTT_KEEPALIVE_S: int = 60

# Friendly per-protocol annotations.  rtl_433 model strings on the
# left, plain-English description on the right.  Used by the
# /airwaves dashboard so the operator sees "window blind remote"
# rather than "Markisol id=0 button=2".  Add rows here as new things
# show up in the live feed — the lookup is purely cosmetic and
# missing entries fall back to the rtl_433 model string verbatim.
FRIENDLY_NAMES: dict[str, str] = {
    # Utility meters
    "SCM":              "ITRON SCM electric meter",
    "SCMplus":          "ITRON SCM+ gas / water meter",
    "ERT-SCM":          "ITRON SCM electric meter (legacy)",
    "ERT-SCM+":         "ITRON SCM+ gas / water meter (legacy)",
    "IDM":              "ITRON IDM interval meter",
    "ERT-IDM":          "ITRON IDM interval meter (legacy)",
    "NetIDM":           "ITRON NetIDM interval meter",
    "ERT-NetIDM":       "ITRON NetIDM interval meter (legacy)",
    "Neptune-R900":     "Neptune R900 water meter",
    # Garage doors / gates / fans / blinds — the entertainment
    "Markisol":         "window blind remote",
    "Regency-Remote":   "ceiling fan remote",
    "Honeywell-ActivLink":  "doorbell button",
    "Genie-GIT-1":      "Genie garage door remote",
    "Chamberlain":      "Chamberlain garage door remote",
    "Linear":           "Linear gate / garage remote",
    "TPMS":             "tire-pressure sensor",
    # Weather stations
    "Acurite-606TX":    "Acurite weather station",
    "Acurite-Tower":    "Acurite tower sensor",
    "AmbientWeather":   "Ambient Weather sensor",
    "LaCrosse-TX":      "LaCrosse weather sensor",
    "OS":               "Oregon Scientific weather sensor",
    "Fineoffset":       "Fine Offset weather sensor",
}


def friendly_name(model: str) -> str:
    """Return a human description for a rtl_433 model, or the model itself."""
    return FRIENDLY_NAMES.get(model, model)


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------


class AirwavesBuffer:
    """In-memory ring of decoded rtl_433 packets.

    Subscribes to :data:`RAW_TOPIC` over MQTT and appends every
    JSON-shaped message to a bounded :class:`collections.deque`.
    Older entries fall off the back automatically.  Read accessors
    return shallow copies under a lock; the network thread is never
    blocked on reader work.

    Args:
        ring_size:  Maximum entries kept.  Older drops off the tail.
    """

    def __init__(self, ring_size: int = DEFAULT_RING_SIZE) -> None:
        """See class docstring."""
        self._ring: collections.deque[dict[str, Any]] = (
            collections.deque(maxlen=ring_size)
        )
        self._lock: threading.Lock = threading.Lock()
        self._client: Optional["mqtt.Client"] = None
        self._started: bool = False
        # Per-transmitter aggregates.  Keyed by (model, transmitter_id);
        # value is {first_seen, last_seen, count}.  Bounded indirectly
        # — entries persist for the lifetime of the process but the
        # cardinality of distinct sub-GHz transmitters in a residential
        # neighbourhood is small (dozens, not millions).
        self._by_transmitter: dict[tuple[str, str], dict[str, Any]] = {}
        # Per-protocol counts.  Same lifetime caveat.
        self._by_protocol: dict[str, dict[str, Any]] = {}

    # ---- MQTT lifecycle ----------------------------------------------------

    def start_subscriber(
        self,
        broker_host: str = "127.0.0.1",
        broker_port: int = 1883,
    ) -> None:
        """Connect a paho client and subscribe to the raw firehose."""
        if not _HAS_PAHO:
            logger.warning(
                "paho-mqtt not installed — airwaves buffer disabled",
            )
            return
        if self._started:
            logger.debug("airwaves subscriber already running")
            return

        client: "mqtt.Client" = mqtt.Client(client_id="glowup-airwaves")
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

        try:
            client.connect(broker_host, broker_port, _MQTT_KEEPALIVE_S)
        except Exception as exc:
            logger.error(
                "airwaves subscriber connect to %s:%d failed: %s",
                broker_host, broker_port, exc,
            )
            return
        client.loop_start()
        self._client = client
        self._started = True
        logger.info(
            "airwaves subscriber started — %s:%d topic=%s ring=%d",
            broker_host, broker_port, RAW_TOPIC, self._ring.maxlen,
        )

    def close(self) -> None:
        """Stop the subscriber and release the client."""
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as exc:
                logger.warning("airwaves shutdown error: %s", exc)
            self._client = None
            self._started = False

    # ---- paho callbacks ----------------------------------------------------

    def _on_connect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        flags: dict[str, Any],
        rc: int,
    ) -> None:
        """paho callback — (re)subscribe on every connect."""
        if rc == 0:
            client.subscribe(RAW_TOPIC, qos=0)
            logger.info("airwaves subscribed to %s", RAW_TOPIC)
        else:
            logger.error("airwaves connect rc=%d", rc)

    def _on_message(
        self,
        client: "mqtt.Client",
        userdata: Any,
        msg: "mqtt.MQTTMessage",
    ) -> None:
        """paho callback — append a decoded packet to the ring."""
        try:
            packet: Any = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.debug(
                "airwaves message on %s is not JSON: %s", msg.topic, exc,
            )
            return
        if not isinstance(packet, dict):
            logger.debug(
                "airwaves message is not a dict: %s",
                type(packet).__name__,
            )
            return

        model: str = str(packet.get("model") or "unknown")
        # rtl_433 emits the transmitter id under several keys depending
        # on the protocol.  Probe the common ones.  When nothing is
        # present, group anonymous packets under the model name so the
        # by-transmitter view aggregates them sensibly (e.g. every
        # Markisol click rolls up under one row).
        transmitter_id: str = ""
        for key in ("id", "EndpointID", "endpoint_id", "address",
                    "device", "channel"):
            v: Any = packet.get(key)
            if v is not None:
                transmitter_id = str(v)
                break
        if not transmitter_id:
            transmitter_id = f"anon-{model}"

        received_ts: float = float(
            packet.get("received_ts") or time.time(),
        )

        entry: dict[str, Any] = {
            "received_ts":     received_ts,
            "model":           model,
            "friendly":        friendly_name(model),
            "transmitter_id":  transmitter_id,
            "freq_MHz":        packet.get("freq") or packet.get("freq_MHz"),
            "rssi":            packet.get("rssi"),
            "snr":             packet.get("snr"),
            "raw":             packet,
        }

        with self._lock:
            self._ring.append(entry)
            # Per-protocol aggregate.
            p: dict[str, Any] = self._by_protocol.setdefault(
                model,
                {"count": 0, "first_seen": received_ts,
                 "last_seen": received_ts, "friendly": friendly_name(model)},
            )
            p["count"] += 1
            p["last_seen"] = received_ts
            # Per-transmitter aggregate.
            tkey: tuple[str, str] = (model, transmitter_id)
            t: dict[str, Any] = self._by_transmitter.setdefault(
                tkey,
                {"count": 0, "first_seen": received_ts,
                 "last_seen": received_ts,
                 "model": model, "transmitter_id": transmitter_id,
                 "friendly": friendly_name(model)},
            )
            t["count"] += 1
            t["last_seen"] = received_ts

    def _on_disconnect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        rc: int,
    ) -> None:
        """paho callback — log unexpected disconnects."""
        if rc != 0:
            logger.warning("airwaves subscriber disconnect rc=%d", rc)

    # ---- Read accessors ----------------------------------------------------

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most-recent ``limit`` packets, newest first.

        Returns shallow copies so callers may freely mutate without
        affecting the ring.
        """
        if limit <= 0:
            return []
        with self._lock:
            # ``deque`` is left-old / right-new; reverse for newest-first.
            n: int = min(limit, len(self._ring))
            tail: list[dict[str, Any]] = list(self._ring)[-n:]
        tail.reverse()
        # Shallow copies so a caller mutating the returned list cannot
        # smear the ring's internal entries.
        return [dict(e) for e in tail]

    def by_protocol(self) -> list[dict[str, Any]]:
        """Return per-protocol aggregates, newest-active first."""
        with self._lock:
            rows: list[dict[str, Any]] = [
                {"model": model, **dict(v)}
                for model, v in self._by_protocol.items()
            ]
        rows.sort(key=lambda r: r["last_seen"], reverse=True)
        return rows

    def by_transmitter(self, limit: int = 30) -> list[dict[str, Any]]:
        """Return per-transmitter aggregates, most-active first.

        ``limit`` caps the result.  Sort key is count desc, last_seen
        desc — the chattiest transmitters lead.
        """
        with self._lock:
            rows: list[dict[str, Any]] = [
                dict(v) for v in self._by_transmitter.values()
            ]
        rows.sort(key=lambda r: (r["count"], r["last_seen"]), reverse=True)
        return rows[:max(0, limit)]

    def stats(self) -> dict[str, Any]:
        """Return small summary metrics for the dashboard header."""
        with self._lock:
            ring_len: int = len(self._ring)
            protocols: int = len(self._by_protocol)
            transmitters: int = len(self._by_transmitter)
            newest: Optional[float] = (
                self._ring[-1]["received_ts"] if self._ring else None
            )
        return {
            "ring_size": self._ring.maxlen,
            "ring_len": ring_len,
            "protocols": protocols,
            "transmitters": transmitters,
            "last_packet_ts": newest,
        }
