"""In-memory current-state cache for buoy observations.

Subscribes to ``glowup/maritime/buoy/+`` (the topic
``maritime/buoy_scraper.py`` publishes onto every poll cycle) and
keeps the most recent observation per station, plus the station's
operator-supplied display metadata (``name``, ``lat``, ``lon``).

Backs:

- ``GET /api/buoys/current`` on the hub — the /maritime map's
  buoy layer renders one marker per station and pops up the
  latest reading on click.

History (the chart cards on /buoys/<station>) is served by
:class:`infrastructure.buoy_logger.BuoyLogger`'s postgres-backed
query surface, NOT this buffer — the buffer is current-state only.
Two separate consumers of the same MQTT topic.

Threading: paho's network thread invokes ``_on_message``; reads and
writes go through one lock.  Reads return shallow copies so HTTP
handlers don't block the network thread for more than microseconds.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

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


logger: logging.Logger = logging.getLogger("glowup.buoys")


# ─── Constants ────────────────────────────────────────────────────────

# MQTT topic the scraper publishes onto.  ``+`` matches the per-
# station suffix.  Same wildcard pattern as
# ``glowup/hardware/thermal/+`` and friends — every fleet sensor in
# this tree uses the same shape.
BUOY_TOPIC_PATTERN: str = "glowup/maritime/buoy/+"

# paho keepalive seconds — match the rest of the maritime subscribers.
_MQTT_KEEPALIVE_S: int = 60


# ─── Buffer ───────────────────────────────────────────────────────────

class BuoyBuffer:
    """Per-station latest-observation cache."""

    def __init__(self) -> None:
        """Construct an empty buffer.  No I/O until start_subscriber."""
        self._stations: dict[str, dict[str, Any]] = {}
        self._lock: threading.Lock = threading.Lock()
        self._client: Optional["mqtt.Client"] = None
        self._started: bool = False
        self._msg_count: int = 0
        self._last_msg_ts: Optional[float] = None

    # -- MQTT lifecycle ------------------------------------------------------

    def start_subscriber(
        self,
        broker_host: str = "127.0.0.1",
        broker_port: int = 1883,
    ) -> None:
        """Connect a paho client and subscribe to the buoy firehose."""
        if not _HAS_PAHO:
            logger.warning("paho-mqtt not installed — buoy buffer disabled")
            return
        if self._started:
            logger.debug("buoy subscriber already running")
            return

        client: "mqtt.Client" = mqtt.Client(client_id="glowup-buoy-buffer")
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

        try:
            client.connect(broker_host, broker_port, _MQTT_KEEPALIVE_S)
        except Exception as exc:
            logger.error(
                "buoy buffer connect to %s:%d failed: %s",
                broker_host, broker_port, exc,
            )
            return
        client.loop_start()
        self._client = client
        self._started = True
        logger.info(
            "buoy buffer started — %s:%d topic=%s",
            broker_host, broker_port, BUOY_TOPIC_PATTERN,
        )

    def close(self) -> None:
        """Stop the subscriber and release the client."""
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as exc:
                logger.warning("buoy buffer shutdown error: %s", exc)
            self._client = None
            self._started = False

    # -- paho callbacks ------------------------------------------------------

    def _on_connect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        flags: dict[str, Any],
        rc: int,
    ) -> None:
        """(Re)subscribe on every connect — paho doesn't persist subs."""
        if rc == 0:
            client.subscribe(BUOY_TOPIC_PATTERN, qos=1)
            logger.info("buoy buffer subscribed to %s", BUOY_TOPIC_PATTERN)
        else:
            logger.error("buoy buffer connect rc=%d", rc)

    def _on_message(
        self,
        client: "mqtt.Client",
        userdata: Any,
        msg: "mqtt.MQTTMessage",
    ) -> None:
        """paho callback — accept a buoy observation."""
        try:
            packet: Any = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.debug("buoy message is not JSON: %s", exc)
            return
        if not isinstance(packet, dict):
            return
        sid: Any = packet.get("station_id")
        if not isinstance(sid, str) or not sid:
            return
        now: float = time.time()
        with self._lock:
            self._msg_count += 1
            self._last_msg_ts = now
            # Carry forward operator-supplied metadata on every
            # message.  Each scraper publish includes name / lat /
            # lon so the buffer doesn't need to be told the station
            # set out-of-band.
            self._stations[sid] = {
                **packet,
                "received_ts": now,
            }

    def _on_disconnect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        rc: int,
    ) -> None:
        """paho callback — log unexpected disconnects."""
        if rc != 0:
            logger.warning("buoy buffer disconnect rc=%d", rc)

    # -- Read accessors ------------------------------------------------------

    def stations(self) -> list[dict[str, Any]]:
        """Return one shallow dict per known station, current obs.

        Each entry includes the station's lat/lon if the operator
        configured one (drives the map marker).  Stations heard
        without lat/lon are still included — the dashboard renders
        them as a list-only entry without a map marker.
        """
        with self._lock:
            return [dict(v) for v in self._stations.values()]

    def station(self, station_id: str) -> Optional[dict[str, Any]]:
        """Return one station's current state, or None if unknown."""
        with self._lock:
            v: Optional[dict[str, Any]] = self._stations.get(station_id)
            return None if v is None else dict(v)

    def stats(self) -> dict[str, Any]:
        """Return small summary metrics for the dashboard header."""
        with self._lock:
            return {
                "n_stations":  len(self._stations),
                "msg_count":   self._msg_count,
                "last_msg_ts": self._last_msg_ts,
            }
