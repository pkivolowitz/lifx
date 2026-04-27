"""In-memory per-vessel state for the /maritime dashboard.

Subscribes to ``glowup/maritime/ais`` (the AIS-catcher firehose) and
maintains a per-MMSI dictionary of vessel state plus a bounded
breadcrumb of recent positions for each vessel.  Backs the live map
on the /maritime page; explicitly **not persisted**.

AIS messages arrive in two main flavours:

- **Position reports** (types 1/2/3 Class A, 18/19 Class B) carry
  lat/lon/speed/course/heading.  These update the vessel's track.
- **Static reports** (type 5 Class A, 24 Class B) carry shipname,
  callsign, ship type, destination, dimensions.  Broadcast every
  ~6 min so a vessel may report position dozens of times before
  its name is heard for the first time.

We merge: any field present in an incoming packet overwrites the
state's value, but missing fields keep their last-known value, so a
vessel's name persists across subsequent position-only updates.

Threading: paho's network thread invokes ``_on_message``; all reads
and writes go through a single lock.  Reads return shallow copies
under the lock so HTTP handlers never block the network thread for
more than a few microseconds.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.1"

import collections
import json
import logging
import math
import threading
import time
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False

from infrastructure.maritime_mid import (
    iso2_to_emoji as _mid_iso2_to_emoji,
    lookup as _mid_lookup,
)


logger: logging.Logger = logging.getLogger("glowup.maritime")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic the AIS-catcher service publishes JSON-Full per AIS
# message onto.  Mirrored on the producer side as the TOPIC argument
# to AIS-catcher's -Q (see maritime/glowup-maritime.service).
AIS_TOPIC: str = "glowup/maritime/ais"

# Second topic for external-source AIS feeds (currently aisstream.io
# via maritime/aisstream_bridge.py running on a non-hub host).
# Translator on the bridge side emits AIS-catcher-shaped JSON with
# an extra ``"source": "aisstream"`` field so the dashboard can
# render external vessels with a distinct style.  Subscribing here
# lets one MaritimeBuffer mix local-RX and internet-fed traffic on
# a single map without the buffer caring which is which.
AIS_TOPIC_EXTERNAL: str = "glowup/maritime/ais-external"

# Maximum breadcrumb length per vessel.  At ~1 position report every
# 2-10 s for an active Class A vessel, 60 points is roughly 2-10
# minutes of history — enough for a visible track on the map without
# unbounded memory growth.  Vessels below this count keep all points.
DEFAULT_TRACK_LEN: int = 60

# How long since last_seen before a vessel is considered "stale" by
# the API.  AIS Class A position reports fire every 2-10 s when
# under way, every 3 min when anchored; Class B is sparser.  10
# minutes is a generous upper bound — anything older than that has
# probably moved out of range.
DEFAULT_STALE_AFTER_S: float = 600.0

# paho keepalive (seconds).  Match the airwaves subscriber.
_MQTT_KEEPALIVE_S: int = 60

# Earth's mean radius in nautical miles.  Maritime convention is
# nautical miles (1 nmi = 1.852 km), so distances in this buffer
# are reported in nmi to match what the rest of the maritime
# ecosystem (chartplotters, AIS displays, vessel ETAs) uses.  The
# /maritime dashboard renders the same unit.
_EARTH_RADIUS_NMI: float = 3440.065

# External-source markers carried in the AIS packet's ``"source"``
# field.  Local AIS-catcher messages carry no ``source`` field at
# all (treated as "local-RX" by default).  Anything other than
# these markers is treated as external for the seen_local /
# seen_external flag accounting.
_EXTERNAL_SOURCE_MARKERS: frozenset[str] = frozenset({"aisstream"})

# AIS message fields we promote onto the per-vessel state on every
# incoming message.  Any field present in the packet overwrites the
# corresponding state value; missing fields keep the prior value
# (the merge contract — see module docstring).
_MERGED_FIELDS: tuple[str, ...] = (
    "shipname", "callsign", "shiptype", "shiptype_text",
    "destination", "eta", "status", "status_text",
    "to_bow", "to_stern", "to_port", "to_starboard",
    "speed", "course", "heading", "turn", "accuracy",
    "epfd", "epfd_text",
    # Origin tag emitted by external-source bridges (e.g.
    # maritime/aisstream_bridge.py adds "source": "aisstream").
    # Local AIS-catcher messages don't carry this field, which the
    # dashboard treats as "local-RX" by default.  Last-source-wins
    # so a vessel we initially saw via aisstream and later picked
    # up locally promotes from external to local.
    "source",
)


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------


class MaritimeBuffer:
    """Per-vessel rolling state for the /maritime dashboard.

    Args:
        track_len:        Max breadcrumb length per vessel.
        stale_after_s:    Default cutoff for the ``stale`` flag in
                          ``vessels()`` results.
        reference:        Optional dict ``{lat, lon, postal_code,
                          country}`` defining a fixed reference point
                          (typically the operator's home port or
                          downtown).  When set, every vessel's
                          ``distance_nmi`` is computed at materialise
                          time so the dashboard can sort/filter by
                          distance without each browser re-doing the
                          haversine.  The ``postal_code`` and
                          ``country`` fields are display labels only;
                          ``lat`` and ``lon`` do the math, no runtime
                          geocoding.  Sourced from
                          ``site.maritime_reference``; absent / None
                          leaves ``distance_nmi`` set to None.
    """

    def __init__(
        self,
        track_len: int = DEFAULT_TRACK_LEN,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        reference: Optional[dict[str, Any]] = None,
    ) -> None:
        """See class docstring."""
        self._track_len: int = track_len
        self._stale_after_s: float = stale_after_s
        self._reference: Optional[dict[str, Any]] = (
            self._validate_reference(reference)
        )
        self._vessels: dict[int, dict[str, Any]] = {}
        self._lock: threading.Lock = threading.Lock()
        self._client: Optional["mqtt.Client"] = None
        self._started: bool = False
        # Lifetime totals — for the dashboard header.
        self._msg_count: int = 0
        self._first_msg_ts: Optional[float] = None
        self._last_msg_ts: Optional[float] = None

    @staticmethod
    def _validate_reference(
        reference: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """Coerce a site.maritime_reference dict into a usable form.

        A valid reference must carry numeric ``lat`` / ``lon`` in
        WGS-84 ranges; anything else is rejected at construction
        rather than crashing per-vessel later.  Missing optional
        labels (``postal_code``, ``country``) are tolerated — they
        only affect display.  Returning None disables distance
        computation entirely.
        """
        if not isinstance(reference, dict):
            return None
        lat: Any = reference.get("lat")
        lon: Any = reference.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            logger.warning(
                "maritime_reference rejected — lat/lon must be numeric, "
                "got lat=%r lon=%r",
                lat, lon,
            )
            return None
        if not (-90.0 <= float(lat) <= 90.0) or not (-180.0 <= float(lon) <= 180.0):
            logger.warning(
                "maritime_reference rejected — lat/lon out of range "
                "(lat=%r lon=%r)",
                lat, lon,
            )
            return None
        return {
            "lat":         float(lat),
            "lon":         float(lon),
            "postal_code": reference.get("postal_code"),
            "country":     reference.get("country"),
        }

    @property
    def reference(self) -> Optional[dict[str, Any]]:
        """Return the configured reference point dict, or None."""
        return self._reference

    # ---- MQTT lifecycle ----------------------------------------------------

    def start_subscriber(
        self,
        broker_host: str = "127.0.0.1",
        broker_port: int = 1883,
    ) -> None:
        """Connect a paho client and subscribe to the AIS firehose."""
        if not _HAS_PAHO:
            logger.warning(
                "paho-mqtt not installed — maritime buffer disabled",
            )
            return
        if self._started:
            logger.debug("maritime subscriber already running")
            return

        client: "mqtt.Client" = mqtt.Client(client_id="glowup-maritime")
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

        try:
            client.connect(broker_host, broker_port, _MQTT_KEEPALIVE_S)
        except Exception as exc:
            logger.error(
                "maritime subscriber connect to %s:%d failed: %s",
                broker_host, broker_port, exc,
            )
            return
        client.loop_start()
        self._client = client
        self._started = True
        logger.info(
            "maritime subscriber started — %s:%d topic=%s",
            broker_host, broker_port, AIS_TOPIC,
        )

    def close(self) -> None:
        """Stop the subscriber and release the client."""
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as exc:
                logger.warning("maritime shutdown error: %s", exc)
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
            client.subscribe(AIS_TOPIC, qos=0)
            client.subscribe(AIS_TOPIC_EXTERNAL, qos=0)
            logger.info(
                "maritime subscribed to %s and %s",
                AIS_TOPIC, AIS_TOPIC_EXTERNAL,
            )
        else:
            logger.error("maritime connect rc=%d", rc)

    def _on_message(
        self,
        client: "mqtt.Client",
        userdata: Any,
        msg: "mqtt.MQTTMessage",
    ) -> None:
        """paho callback — merge a decoded AIS packet into vessel state."""
        try:
            packet: Any = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.debug("maritime message is not JSON: %s", exc)
            return
        if not isinstance(packet, dict):
            return
        mmsi: Any = packet.get("mmsi")
        if not isinstance(mmsi, int):
            # Some AIS broadcasts (base station reports, etc.) carry
            # no MMSI we can index on; ignore.  Counted into stats
            # only if we did the merge below.
            return

        now: float = time.time()
        lat: Any = packet.get("lat")
        lon: Any = packet.get("lon")
        has_position: bool = (
            isinstance(lat, (int, float))
            and isinstance(lon, (int, float))
            and -90.0 <= float(lat) <= 90.0
            and -180.0 <= float(lon) <= 180.0
            # AIS position-unavailable sentinels.
            and not (lat == 91.0 and lon == 181.0)
        )

        with self._lock:
            self._msg_count += 1
            if self._first_msg_ts is None:
                self._first_msg_ts = now
            self._last_msg_ts = now

            v: dict[str, Any] = self._vessels.setdefault(
                mmsi,
                {
                    "mmsi":             mmsi,
                    "first_seen":       now,
                    "last_seen":        now,
                    "msg_count":        0,
                    "track":            collections.deque(
                                            maxlen=self._track_len,
                                        ),
                    "last_position_ts": None,
                    "last_signal_power": None,
                    "last_channel":     None,
                    # Sticky per-source flags — once set true, stay
                    # true while the vessel is in the buffer.  Answers
                    # the more useful "did our antenna ever hear this"
                    # question rather than "what was the most recent
                    # decode".  ``source`` (last-source-wins via
                    # _MERGED_FIELDS below) still tracks the most
                    # recent source for the popup's "Source" row.
                    "seen_local":       False,
                    "seen_external":    False,
                },
            )
            v["last_seen"] = now
            v["msg_count"] = v["msg_count"] + 1

            # Sticky source-presence accounting.  Local AIS-catcher
            # publishes carry no ``source`` field; external bridges
            # (currently only aisstream, see _EXTERNAL_SOURCE_MARKERS)
            # tag their messages.  Set the matching flag true on
            # every message; never clear — once heard, always heard
            # for the lifetime of this buffer entry.
            packet_source: Any = packet.get("source")
            if (
                isinstance(packet_source, str)
                and packet_source in _EXTERNAL_SOURCE_MARKERS
            ):
                v["seen_external"] = True
            else:
                v["seen_local"] = True

            # Promote merge fields when present and non-null.
            for key in _MERGED_FIELDS:
                if key in packet and packet[key] is not None:
                    v[key] = packet[key]

            # Per-vessel running signal-power stats.  We keep
            # min / max / sum / count rather than a full sample buffer
            # because the buffer would unbounded-grow per vessel; the
            # streaming stats give us mean (sum/count) on read and are
            # an O(1) update per decode.  Useful for antenna-quality
            # experiments — ground-plane / antenna swaps can be
            # compared by per-vessel mean on a same-MMSI basis without
            # the small-sample composition artifacts that bite when
            # using last-decode signal power alone.
            sp: Any = packet.get("signalpower")
            if isinstance(sp, (int, float)):
                sp_f: float = float(sp)
                v["last_signal_power"] = sp_f
                cur_min: Any = v.get("signal_power_min")
                cur_max: Any = v.get("signal_power_max")
                if cur_min is None or sp_f < cur_min:
                    v["signal_power_min"] = sp_f
                if cur_max is None or sp_f > cur_max:
                    v["signal_power_max"] = sp_f
                v["signal_power_sum"] = (
                    float(v.get("signal_power_sum") or 0.0) + sp_f
                )
                v["signal_power_count"] = (
                    int(v.get("signal_power_count") or 0) + 1
                )
            ch: Any = packet.get("channel")
            if isinstance(ch, str):
                v["last_channel"] = ch

            # Append to track only on a position-bearing message.
            if has_position:
                lat_f: float = float(lat)
                lon_f: float = float(lon)
                v["lat"] = lat_f
                v["lon"] = lon_f
                v["last_position_ts"] = now
                v["track"].append(
                    {"ts": now, "lat": lat_f, "lon": lon_f},
                )

    def _on_disconnect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        rc: int,
    ) -> None:
        """paho callback — log unexpected disconnects."""
        if rc != 0:
            logger.warning("maritime subscriber disconnect rc=%d", rc)

    # ---- Read accessors ----------------------------------------------------

    @staticmethod
    def _haversine_nmi(
        lat1: float, lon1: float, lat2: float, lon2: float,
    ) -> float:
        """Great-circle distance in nautical miles.

        WGS-84 is treated as a sphere — sub-percent error is fine
        for at-a-glance dashboard distances.  Accurate ETA / nav
        computations would use Vincenty or similar; we are not
        doing nav, we are sorting a table.
        """
        p1: float = math.radians(lat1)
        p2: float = math.radians(lat2)
        dp: float = math.radians(lat2 - lat1)
        dl: float = math.radians(lon2 - lon1)
        a: float = (
            math.sin(dp / 2) ** 2
            + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        )
        return 2.0 * _EARTH_RADIUS_NMI * math.asin(math.sqrt(a))

    def _materialise(
        self,
        v: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        """Inner helper — copy + derive read-only fields for the API.

        Computes signal_power_mean from the running sum/count, decode
        rate per minute from msg_count and the seen window, and the
        stale flag from last_seen against ``self._stale_after_s``.
        Caller already holds ``self._lock``.
        """
        copy: dict[str, Any] = {
            k: v[k] for k in v if k != "track"
        }
        copy["track"] = list(v["track"])
        # Streaming-stat derivations.
        cnt: int = int(copy.get("signal_power_count") or 0)
        if cnt > 0:
            copy["signal_power_mean"] = (
                float(copy.get("signal_power_sum") or 0.0) / cnt
            )
        else:
            copy["signal_power_mean"] = None
        first_seen: float = float(copy.get("first_seen") or now)
        last_seen: float = float(copy.get("last_seen") or now)
        elapsed_s: float = max(1.0, last_seen - first_seen)
        copy["decode_rate_per_min"] = (
            (copy.get("msg_count") or 0) / (elapsed_s / 60.0)
        )
        copy["seen_for_s"] = elapsed_s
        copy["stale"] = (now - last_seen) > self._stale_after_s
        # MID → flag enrichment.  ITU-R M.585: first 3 digits of an
        # MMSI identify country of registration.  Pure static lookup,
        # no network cost.  Unknown MIDs fall through to (None, None)
        # — the dashboard popup just omits the flag row.
        mmsi: Any = copy.get("mmsi")
        if isinstance(mmsi, int):
            iso2, country_name = _mid_lookup(mmsi)
            copy["flag_iso"] = iso2
            copy["flag_country"] = country_name
            copy["flag_emoji"] = _mid_iso2_to_emoji(iso2)
        # Distance enrichment — only when a reference is configured
        # AND the vessel has a current position.  Computed server-side
        # so 3000-vessel browsers don't each re-haversine on every
        # poll, and so server-side sort/range filters are stable.
        copy["distance_nmi"] = None
        if self._reference is not None:
            lat: Any = copy.get("lat")
            lon: Any = copy.get("lon")
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                copy["distance_nmi"] = self._haversine_nmi(
                    self._reference["lat"], self._reference["lon"],
                    float(lat), float(lon),
                )
        return copy

    def vessels(
        self,
        with_position_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Return one shallow dict per vessel, current state.

        The ``track`` deque is converted to a plain list — no caller
        can mutate the live state.  Set ``with_position_only=False``
        to include vessels we have heard from but never received a
        valid lat/lon for.

        Each entry includes derived fields the producer side can't
        compute streamingly: ``signal_power_mean``,
        ``decode_rate_per_min``, ``seen_for_s``, and ``stale``.
        """
        now: float = time.time()
        with self._lock:
            out: list[dict[str, Any]] = []
            for v in self._vessels.values():
                if with_position_only and v.get("lat") is None:
                    continue
                out.append(self._materialise(v, now))
        # Newest-active first.  Lets the dashboard render most-
        # recently-heard vessels on top of the marker layer.
        out.sort(key=lambda r: r.get("last_seen") or 0.0, reverse=True)
        return out

    def vessel(self, mmsi: int) -> Optional[dict[str, Any]]:
        """Return one vessel's full state, or ``None`` if unknown."""
        now: float = time.time()
        with self._lock:
            v: Optional[dict[str, Any]] = self._vessels.get(mmsi)
            if v is None:
                return None
            return self._materialise(v, now)

    def stats(self) -> dict[str, Any]:
        """Return small summary metrics for the dashboard header."""
        with self._lock:
            n_total: int = len(self._vessels)
            n_with_pos: int = sum(
                1 for v in self._vessels.values()
                if v.get("lat") is not None
            )
            return {
                "n_vessels":       n_total,
                "n_with_position": n_with_pos,
                "msg_count":       self._msg_count,
                "first_msg_ts":    self._first_msg_ts,
                "last_msg_ts":     self._last_msg_ts,
                "stale_after_s":   self._stale_after_s,
            }
