"""Shared interval watcher for the thermal sensor family.

Each thermal sensor (Pi / x86 / macOS) imports ``IntervalWatcher`` and
attaches it to its paho MQTT client.  The watcher subscribes to a
single retained-config topic; publishing a new JSON payload there
changes the publish interval for every sensor in the fleet, live,
without a redeploy or restart.

Fleet-wide change::

    mosquitto_pub -h 10.0.0.214 -r \\
        -t glowup/config/thermal_interval_s \\
        -m '{"interval_s": 60}'

The payload is JSON so we can extend it later (per-host overrides,
min/max, ramp rules) without another topic rename.

Design notes:

* Subscribes inside ``on_connect`` — any paho reconnect re-subscribes.
  This is the project rule (``feedback_paho_resubscribe_on_connect``):
  a subscribe at init time goes silent after the first disconnect.
* Validates bounds at ingest so a typo like ``{"interval_s": 0}``
  can't DDoS the broker, and ``{"interval_s": 86400}`` can't turn a
  sensor into a flatline.
* Thread-safe: the MQTT loop thread writes; the sensor main loop
  reads.  A single lock around a float is overkill but obvious.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Optional

logger: logging.Logger = logging.getLogger("glowup.interval_watcher")

INTERVAL_CONFIG_TOPIC: str = "glowup/config/thermal_interval_s"

# Safety bounds — values outside are logged and ignored.
_MIN_INTERVAL_S: float = 5.0
_MAX_INTERVAL_S: float = 3600.0


class IntervalWatcher:
    """Thread-safe holder for the live publish interval.

    Usage::

        watcher = IntervalWatcher(default_interval_s=60.0)
        watcher.attach(mqtt_client)          # before client.connect()
        ...
        sleep_time = watcher.current()       # in the publish loop
    """

    def __init__(
        self,
        default_interval_s: float,
        topic: str = INTERVAL_CONFIG_TOPIC,
    ) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._current: float = float(default_interval_s)
        self._topic: str = topic

    def current(self) -> float:
        """Return the current interval in seconds."""
        with self._lock:
            return self._current

    def attach(self, client: Any) -> None:
        """Hook subscribe + on_message onto a paho client.

        Chains onto any existing ``on_connect`` / ``on_message`` the
        caller has already set, so sensors keep their own callbacks.
        Must be called before ``client.connect`` so the subscribe
        fires on the first CONNACK.
        """
        prior_on_connect: Optional[Callable[..., Any]] = client.on_connect
        prior_on_message: Optional[Callable[..., Any]] = client.on_message
        topic: str = self._topic

        def on_connect(
            c: Any, ud: Any, *args: Any, **kw: Any,
        ) -> None:
            if prior_on_connect is not None:
                prior_on_connect(c, ud, *args, **kw)
            # Re-subscribe on every connect — see module docstring.
            c.subscribe(topic, qos=1)
            logger.info(
                "interval-watcher subscribed to %s (current=%.1fs)",
                topic, self.current(),
            )

        def on_message(c: Any, ud: Any, msg: Any) -> None:
            if msg.topic == topic:
                self._ingest(msg.payload)
            elif prior_on_message is not None:
                prior_on_message(c, ud, msg)

        client.on_connect = on_connect
        client.on_message = on_message

    def _ingest(self, payload: bytes) -> None:
        """Parse a config payload and update the interval if valid."""
        try:
            body: dict[str, Any] = json.loads(payload.decode("utf-8"))
            new_s: float = float(body["interval_s"])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            logger.warning(
                "interval-watcher: bad payload on %s: %s",
                self._topic, exc,
            )
            return
        if not (_MIN_INTERVAL_S <= new_s <= _MAX_INTERVAL_S):
            logger.warning(
                "interval-watcher: %.1fs outside bounds [%.0f, %.0f], ignored",
                new_s, _MIN_INTERVAL_S, _MAX_INTERVAL_S,
            )
            return
        with self._lock:
            old: float = self._current
            self._current = new_s
        if old != new_s:
            logger.info(
                "interval-watcher: %.1fs → %.1fs", old, new_s,
            )
