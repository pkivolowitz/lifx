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

# Retained tuner-state topic.  The publisher republishes a fresh
# payload on every rtl_433 retune; the broker holds the latest value
# so a freshly-loaded dashboard sees current state immediately.
# Mirrored on the publisher side as ``meters.publisher._TUNER_TOPIC``.
# Payload:  {host, freq_MHz, tuned_at, rotation_MHz: [...], dwell_s}
TUNER_TOPIC: str = "glowup/sub_ghz/tuner"

# How many packets to keep in the ring.  500 at ~5 packets/min works
# out to roughly 1.5 hours of history — enough to scroll back and see
# the morning's garage-door activity, short enough that memory cost
# is trivial (<1 MB).  Tunable via the AirwavesBuffer constructor.
DEFAULT_RING_SIZE: int = 500

# Burst-collapse window for the feed display.  Cheap one-way RF
# sensors (Honeywell 5800 door/window, inFactory weather, most
# 433-MHz consumer remotes) transmit the same payload 3-6 times
# back-to-back at ~140-160 ms intervals to combat RF loss — there
# is no ACK so they spam.  The /airwaves *feed* collapses
# consecutive identical (model, transmitter_id, payload) packets
# within this window into one row tagged with repeat_count, so a
# single physical event reads as one entry instead of six.
# The per-protocol and per-transmitter aggregates still increment
# once per packet — those views answer "how chatty is this device
# physically" and must not lie about the true packet rate.
# 2 s comfortably covers observed Honeywell (709 ms) and inFactory
# (795 ms) burst spans with margin for slower future devices.
_BURST_WINDOW_S: float = 2.0

# Fields that vary per-packet within a single sensor burst and so
# must NOT be part of the burst-equality signature.  ``time`` is the
# rtl_433 timestamp (always different), ``freq``/``rssi``/``snr``/
# ``noise`` are demod-measured per packet, ``mod`` is the modulation
# label (occasionally varies on multi-protocol freqs), ``received_ts``
# is our hub-side arrival stamp.  Everything else is sensor-payload
# proper and goes into the burst signature.
_BURST_SIG_HOUSEKEEPING: frozenset[str] = frozenset({
    "time", "freq", "rssi", "snr", "noise", "mod", "received_ts",
})

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
    "inFactory-TH":     "your inFactory temp/humidity sensor",
    "inFactory":        "your inFactory weather sensor",
}


def friendly_name(model: str) -> str:
    """Return a human description for a rtl_433 model, or the model itself."""
    return FRIENDLY_NAMES.get(model, model)


def _payload_signature(packet: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Stable equality key for burst-collapse, excluding housekeeping.

    Two packets share a signature iff their non-housekeeping fields
    are byte-identical (after ``repr``).  ``repr`` rather than ``str``
    so that the int 0 and the str "0" don't collapse together — a
    sensor that switches from numeric to string representation of the
    same value is reporting different state, not a duplicate.
    """
    return tuple(sorted(
        (k, repr(v)) for k, v in packet.items()
        if k not in _BURST_SIG_HOUSEKEEPING
    ))


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
        # Latest tuner-state payload from the SDR publisher (retained
        # MQTT message).  None until the first packet arrives or the
        # broker delivers the retained value on subscribe.
        self._tuner_state: Optional[dict[str, Any]] = None

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
        """paho callback — (re)subscribe on every connect.

        Re-subscribing on every (re)connect is mandatory: paho's
        client does NOT persist subscriptions across the broker-level
        reconnect handshake, so a one-shot subscription at startup
        would silently deafen after any network blip.  Pinned in
        ``feedback_paho_resubscribe_on_connect.md``.
        """
        if rc == 0:
            client.subscribe(RAW_TOPIC, qos=0)
            client.subscribe(TUNER_TOPIC, qos=1)
            logger.info(
                "airwaves subscribed to %s and %s",
                RAW_TOPIC, TUNER_TOPIC,
            )
        else:
            logger.error("airwaves connect rc=%d", rc)

    def _on_message(
        self,
        client: "mqtt.Client",
        userdata: Any,
        msg: "mqtt.MQTTMessage",
    ) -> None:
        """paho callback — dispatch by topic to ring or tuner-state."""
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

        # Tuner state is a small, retained, separate stream — handle
        # it before the ring/aggregate code which expects rtl_433
        # packet shape.
        if msg.topic == TUNER_TOPIC:
            with self._lock:
                self._tuner_state = packet
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

        sig: tuple[tuple[str, str], ...] = _payload_signature(packet)
        entry: dict[str, Any] = {
            "received_ts":     received_ts,
            "model":           model,
            "friendly":        friendly_name(model),
            "transmitter_id":  transmitter_id,
            "freq_MHz":        packet.get("freq") or packet.get("freq_MHz"),
            "rssi":            packet.get("rssi"),
            "snr":             packet.get("snr"),
            "raw":             packet,
            # Burst-collapse bookkeeping.  ``repeat_count`` is exposed
            # to clients (the feed renders "(×N)" when > 1).
            # ``_payload_sig`` and ``_first_ts`` are private — leading
            # underscore so :meth:`recent` can strip them before the
            # API copy.
            "repeat_count":    1,
            "_payload_sig":    sig,
            "_first_ts":       received_ts,
        }

        with self._lock:
            # Burst collapse: if the most-recent ring entry has the
            # same (model, transmitter_id, payload-sig) AND its
            # *first* packet was within _BURST_WINDOW_S of this one,
            # bump that entry's count and refresh its received_ts
            # rather than append.  Compare against _first_ts (not the
            # rolling received_ts) so a sensor that legitimately
            # transmits steady-state every ~1 s stops collapsing
            # forever after the first match.
            collapsed: bool = False
            if self._ring:
                last: dict[str, Any] = self._ring[-1]
                if (last.get("model") == model
                        and last.get("transmitter_id") == transmitter_id
                        and last.get("_payload_sig") == sig
                        and (received_ts - last.get("_first_ts", 0.0))
                            <= _BURST_WINDOW_S):
                    last["repeat_count"] = last.get("repeat_count", 1) + 1
                    last["received_ts"] = received_ts
                    collapsed = True
            if not collapsed:
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
        # Shallow copies, with leading-underscore bookkeeping fields
        # stripped — those are burst-collapse internals (_payload_sig,
        # _first_ts) the API consumer has no business with.
        return [
            {k: v for k, v in e.items() if not k.startswith("_")}
            for e in tail
        ]

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

    def tuner_state(self) -> Optional[dict[str, Any]]:
        """Return the latest tuner state, or ``None`` if not seen yet.

        Augments the publisher's retained payload with two derived
        fields computed at request time:

        - ``dwell_remaining_s``: seconds left in the current dwell,
          ``max(0, dwell_s - (now - tuned_at))``.  Reads as 0 once the
          slot has expired without a fresh hop event (e.g. SDR host
          hung) — the dashboard treats sustained-zero as "stale".
        - ``next_freq_MHz``: the freq the next hop will land on, or
          ``None`` if the current freq is not in the rotation list
          (defensive — shouldn't happen with a well-formed publisher).

        Returns a shallow copy so the caller cannot mutate the cached
        state under the network thread.
        """
        with self._lock:
            state: Optional[dict[str, Any]] = (
                dict(self._tuner_state) if self._tuner_state else None
            )
        if state is None:
            return None
        try:
            tuned_at: float = float(state.get("tuned_at") or 0.0)
            dwell_s: float = float(state.get("dwell_s") or 0.0)
            elapsed: float = max(0.0, time.time() - tuned_at)
            state["dwell_remaining_s"] = max(0.0, dwell_s - elapsed)
        except (TypeError, ValueError):
            state["dwell_remaining_s"] = 0.0

        rotation: list[Any] = state.get("rotation_MHz") or []
        current: Any = state.get("freq_MHz")
        next_freq: Optional[float] = None
        if isinstance(rotation, list) and current is not None:
            try:
                # Float compare with a tolerance — rtl_433 reports
                # "911.000MHz" exactly, but a configured "911M" parses
                # to 911.0; tolerance shrugs off any future formatting
                # drift (e.g. 911.000000001 from an FP round-trip).
                cur_f: float = float(current)
                idx: int = -1
                for i, f in enumerate(rotation):
                    if abs(float(f) - cur_f) < 0.001:
                        idx = i
                        break
                if idx >= 0 and rotation:
                    next_freq = float(rotation[(idx + 1) % len(rotation)])
            except (TypeError, ValueError):
                next_freq = None
        state["next_freq_MHz"] = next_freq
        return state

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
