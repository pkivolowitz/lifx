"""Base class for out-of-process GlowUp adapters.

Handles the MQTT lifecycle that every isolated adapter needs:
LWT (online/offline), periodic heartbeat, command subscription
with correlation-ID response routing, and signal publishing.

Subclasses override ``run()``, ``get_status_detail()``, and
``handle_command()`` to implement adapter-specific logic.

Usage::

    class MyAdapter(ProcessAdapterBase):
        def get_status_detail(self) -> dict:
            return {"connected": True, "foo": 42}

        def handle_command(self, action, params) -> dict:
            if action == "restart":
                self._restart()
                return {"status": "ok"}
            return {"status": "error", "error": f"unknown action: {action}"}

        def run(self) -> None:
            while not self._stop_event.is_set():
                self.publish_signal("my_sensor:temperature", 22.5)
                self._stop_event.wait(10.0)

    adapter = MyAdapter("my_adapter", broker="<hub-broker>")
    adapter.start()
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.1"

import json
import logging
import os
import resource
import signal
import threading
import time
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.process_base")

# ---------------------------------------------------------------------------
# Optional paho-mqtt import
# ---------------------------------------------------------------------------

try:
    import paho.mqtt.client as mqtt
    _PAHO_V2: bool = hasattr(mqtt, "CallbackAPIVersion")
except ImportError:
    mqtt = None  # type: ignore[assignment]
    _PAHO_V2 = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic templates.
TOPIC_STATUS: str = "glowup/adapter/{id}/status"
TOPIC_HEARTBEAT: str = "glowup/adapter/{id}/heartbeat"
TOPIC_COMMAND: str = "glowup/adapter/{id}/command/+"
TOPIC_RESPONSE: str = "glowup/adapter/{id}/response/{corr}"
TOPIC_SIGNALS: str = "glowup/signals/{name}"

# Heartbeat interval in seconds.
HEARTBEAT_INTERVAL_S: float = 15.0

# QoS levels.
QOS_STATUS: int = 1       # LWT and status — must be delivered.
QOS_HEARTBEAT: int = 0    # Retained, loss tolerable.
QOS_COMMAND: int = 1      # Commands must be delivered.
QOS_SIGNAL: int = 0       # Telemetry — high rate, loss tolerable.


class ProcessAdapterBase:
    """Base class for out-of-process GlowUp adapters.

    Manages the MQTT connection, LWT, heartbeat, command routing,
    and signal publishing.  Subclasses implement the adapter logic.

    Args:
        adapter_id: Unique adapter identifier (e.g., "zigbee").
        broker:     MQTT broker address.
        port:       MQTT broker port.
    """

    def __init__(
        self,
        adapter_id: str,
        broker: str = "localhost",
        port: int = 1883,
    ) -> None:
        """Initialize the adapter base."""
        if mqtt is None:
            raise ImportError("paho-mqtt is required for process adapters")

        self._adapter_id: str = adapter_id
        self._broker: str = broker
        self._port: int = port
        self._stop_event: threading.Event = threading.Event()
        self._start_time: float = time.monotonic()

        # MQTT client with LWT.
        client_id: str = f"glowup-{adapter_id}-{os.getpid()}"
        if _PAHO_V2:
            self._client: mqtt.Client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
            )
        else:
            self._client = mqtt.Client(client_id=client_id)

        # Set LWT — broker publishes "offline" if we disconnect uncleanly.
        status_topic: str = TOPIC_STATUS.format(id=adapter_id)
        self._client.will_set(
            status_topic, payload="offline",
            qos=QOS_STATUS, retain=True,
        )

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        # Heartbeat timer.
        self._heartbeat_timer: Optional[threading.Timer] = None

        logger.info(
            "[%s] ProcessAdapterBase initialized (broker=%s:%d)",
            adapter_id, broker, port,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect to MQTT, start heartbeat, run the adapter.

        Blocks until ``stop()`` is called or SIGTERM is received.
        """
        # Signal handling for graceful shutdown.
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        # Connect to MQTT.
        self._client.connect(self._broker, self._port)
        self._client.loop_start()

        # Publish online status.
        status_topic: str = TOPIC_STATUS.format(id=self._adapter_id)
        self._client.publish(
            status_topic, payload="online",
            qos=QOS_STATUS, retain=True,
        )

        # Start heartbeat.
        self._schedule_heartbeat()

        logger.info("[%s] Adapter started", self._adapter_id)

        # Run adapter logic — blocks until stopped.
        try:
            self.run()
        except Exception as exc:
            logger.error("[%s] Adapter crashed: %s", self._adapter_id, exc)
        finally:
            self._shutdown()

    def stop(self) -> None:
        """Signal the adapter to stop."""
        self._stop_event.set()

    def publish_signal(self, name: str, value: Any) -> None:
        """Publish a signal value to MQTT.

        Signals named ``{device}:_availability`` are published with
        ``retain=True`` so a server subscribing to ``glowup/signals/#``
        after the adapter has already seen the availability edge
        still receives the current state at subscribe time.  Every
        other signal uses ``retain=False`` — retained readings would
        be actively harmful (stale data replayed as ground truth), so
        retention is an opt-in for the specific class of signals
        where "last-known state" is the semantics.

        Args:
            name:  Signal name (e.g., "Office Motion:occupancy").
            value: Signal value (JSON-serializable).
        """
        topic: str = TOPIC_SIGNALS.format(name=name)
        payload: str = json.dumps(value)
        retain: bool = name.endswith(":_availability")
        self._client.publish(topic, payload, qos=QOS_SIGNAL, retain=retain)

    # ------------------------------------------------------------------
    # Override points
    # ------------------------------------------------------------------

    def get_status_detail(self) -> dict[str, Any]:
        """Return adapter-specific status for the heartbeat.

        Override this to include adapter health details.

        Returns:
            Dict with adapter-specific status fields.
        """
        return {}

    def handle_command(
        self, action: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle a command from the server.

        Override this to implement adapter-specific commands.

        Args:
            action: Command action name (e.g., "restart", "send").
            params: Command parameters.

        Returns:
            Response dict with at least ``{"status": "ok"}`` or
            ``{"status": "error", "error": "..."}``.
        """
        if action == "restart":
            logger.info("[%s] Restart requested", self._adapter_id)
            self._stop_event.set()
            return {"status": "ok"}
        if action == "shutdown":
            logger.info("[%s] Shutdown requested", self._adapter_id)
            self._stop_event.set()
            return {"status": "ok"}
        return {"status": "error", "error": f"unknown action: {action}"}

    def run(self) -> None:
        """Main adapter loop — override with adapter logic.

        This method should block until ``self._stop_event`` is set.
        Use ``self._stop_event.wait(interval)`` for periodic work.
        """
        # Default: idle until stopped.
        self._stop_event.wait()

    # ------------------------------------------------------------------
    # MQTT callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client: Any, userdata: Any, *args: Any) -> None:
        """Subscribe to command topic on connect/reconnect."""
        cmd_topic: str = TOPIC_COMMAND.format(id=self._adapter_id)
        client.subscribe(cmd_topic, qos=QOS_COMMAND)
        logger.info(
            "[%s] MQTT connected, subscribed to %s",
            self._adapter_id, cmd_topic,
        )

    def _on_message(
        self, client: Any, userdata: Any, message: Any,
    ) -> None:
        """Route incoming command messages to handle_command()."""
        topic: str = message.topic
        prefix: str = f"glowup/adapter/{self._adapter_id}/command/"

        if not topic.startswith(prefix):
            return

        action: str = topic[len(prefix):]

        try:
            payload: dict[str, Any] = json.loads(message.payload)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "[%s] Invalid command payload on %s",
                self._adapter_id, topic,
            )
            return

        correlation_id: str = payload.get("correlation_id", "")
        params: dict[str, Any] = payload.get("params", {})

        logger.info(
            "[%s] Command: %s (corr=%s)",
            self._adapter_id, action, correlation_id,
        )

        # Dispatch to subclass handler.
        try:
            response: dict[str, Any] = self.handle_command(action, params)
        except Exception as exc:
            logger.error(
                "[%s] Command handler crashed: %s", self._adapter_id, exc,
            )
            response = {"status": "error", "error": str(exc)}

        # Publish response.
        if correlation_id:
            response["correlation_id"] = correlation_id
            resp_topic: str = TOPIC_RESPONSE.format(
                id=self._adapter_id, corr=correlation_id,
            )
            self._client.publish(
                resp_topic,
                json.dumps(response),
                qos=QOS_COMMAND,
            )

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _schedule_heartbeat(self) -> None:
        """Schedule the next heartbeat publication."""
        if self._stop_event.is_set():
            return
        self._heartbeat_timer = threading.Timer(
            HEARTBEAT_INTERVAL_S, self._publish_heartbeat,
        )
        self._heartbeat_timer.daemon = True
        self._heartbeat_timer.start()

    def _publish_heartbeat(self) -> None:
        """Publish a heartbeat message and schedule the next one.

        Wrapped in try/except so a crashing ``get_status_detail()``
        does not kill the timer thread and silence all future heartbeats.
        """
        try:
            uptime_s: float = time.monotonic() - self._start_time
            # Resource usage — maxrss is in kilobytes on Linux,
            # bytes on macOS.  Normalize to megabytes.
            usage: resource.struct_rusage = resource.getrusage(
                resource.RUSAGE_SELF,
            )
            # macOS reports bytes; Linux reports kilobytes.
            rss_mb: float = usage.ru_maxrss / (1024.0 * 1024.0)
            if os.uname().sysname == "Linux":
                # Linux ru_maxrss is in KB, not bytes.
                rss_mb = usage.ru_maxrss / 1024.0

            # get_status_detail() may fail if the adapter crashed or
            # is in an unexpected state.  Degrade gracefully.
            try:
                detail: dict[str, Any] = self.get_status_detail()
            except Exception as exc:
                logger.warning(
                    "[%s] get_status_detail() failed: %s",
                    self._adapter_id, exc,
                )
                detail = {"error": str(exc)}

            heartbeat: dict[str, Any] = {
                "adapter": self._adapter_id,
                "pid": os.getpid(),
                "uptime_s": round(uptime_s, 1),
                "ts": time.time(),
                "state": "running",
                "rss_mb": round(rss_mb, 1),
                "detail": detail,
            }

            topic: str = TOPIC_HEARTBEAT.format(id=self._adapter_id)
            self._client.publish(
                topic,
                json.dumps(heartbeat),
                qos=QOS_HEARTBEAT,
                retain=True,
            )
        except Exception as exc:
            logger.error(
                "[%s] Heartbeat publish failed: %s",
                self._adapter_id, exc,
            )
        finally:
            # Always schedule the next heartbeat, even on failure.
            self._schedule_heartbeat()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _signal_handler(self, sig: int, frame: Any) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        logger.info("[%s] Received signal %d", self._adapter_id, sig)
        self._stop_event.set()

    def _shutdown(self) -> None:
        """Clean shutdown: cancel heartbeat, publish offline, disconnect."""
        if self._heartbeat_timer is not None:
            self._heartbeat_timer.cancel()

        # Publish offline status before disconnecting.
        try:
            status_topic: str = TOPIC_STATUS.format(id=self._adapter_id)
            self._client.publish(
                status_topic, payload="offline",
                qos=QOS_STATUS, retain=True,
            )
        except Exception as exc:
            logger.debug("Offline status publish failed: %s", exc)

        self._client.loop_stop()
        self._client.disconnect()
        logger.info("[%s] Adapter stopped", self._adapter_id)


# ---------------------------------------------------------------------------
# MqttSignalBus — drop-in SignalBus for process-isolated adapters
# ---------------------------------------------------------------------------

# Sentinel for detecting first-write (None could be a valid signal value).
_SENTINEL: object = object()



class MqttSignalBus:
    """Drop-in replacement for :class:`media.SignalBus` in adapter processes.

    Adapters call ``bus.write(name, value)`` to publish signal values.
    In the monolithic server this writes to an in-process dict.  Here
    it publishes to the MQTT topic ``glowup/signals/{name}`` so the
    server (and other subscribers) receive the value.

    A local cache is maintained so adapters that call ``bus.read()``
    (e.g. to read-back their own last-written value) get consistent
    results without a round-trip.

    Args:
        adapter: The :class:`ProcessAdapterBase` whose MQTT client
                 is used for publishing.
    """

    def __init__(self, adapter: ProcessAdapterBase) -> None:
        """Initialize the MQTT-backed signal bus."""
        self._adapter: ProcessAdapterBase = adapter
        self._cache: dict[str, Any] = {}
        self._timestamps: dict[str, float] = {}
        self._lock: threading.Lock = threading.Lock()

    def write(self, name: str, value: Any) -> None:
        """Write a signal value and publish it via MQTT.

        Deduplicates: only publishes to MQTT if the value has
        actually changed since the last write.  This prevents
        high-frequency adapters (e.g. Vivint PubNub) from flooding
        the broker with thousands of identical messages per second.

        Args:
            name:  Signal name (e.g. ``"Office Motion:occupancy"``).
            value: Signal value (JSON-serializable).
        """
        now: float = time.monotonic()
        with self._lock:
            prev: Any = self._cache.get(name, _SENTINEL)
            self._cache[name] = value
            self._timestamps[name] = now
            changed: bool = prev is _SENTINEL or prev != value
        if changed:
            self._adapter.publish_signal(name, value)

    def read(self, name: str, default: Any = 0.0) -> Any:
        """Read the last-written value of a signal from local cache.

        Args:
            name:    Signal name.
            default: Returned if the signal has never been written.

        Returns:
            The signal value, or *default*.
        """
        with self._lock:
            return self._cache.get(name, default)

    def read_timestamp(self, name: str) -> Optional[float]:
        """Read the monotonic timestamp of a signal's last write.

        Args:
            name: Signal name.

        Returns:
            Monotonic timestamp, or ``None`` if never written.
        """
        with self._lock:
            return self._timestamps.get(name)

    def read_with_timestamp(
        self, name: str, default: Any = 0.0,
    ) -> tuple[Any, Optional[float]]:
        """Read a signal value and its write timestamp atomically.

        Args:
            name:    Signal name.
            default: Returned if never written.

        Returns:
            ``(value, timestamp)`` tuple.
        """
        with self._lock:
            return (
                self._cache.get(name, default),
                self._timestamps.get(name),
            )

    def snapshot(self) -> dict[str, Any]:
        """Return a copy of all cached signal values.

        Returns:
            Dict of signal name to value.
        """
        with self._lock:
            return dict(self._cache)

    def register(self, name: str, meta: Any = None) -> None:
        """No-op in process mode — signals are registered server-side.

        Provided for API compatibility with :class:`media.SignalBus`.
        """

    def unregister(self, name: str) -> None:
        """No-op in process mode.

        Provided for API compatibility with :class:`media.SignalBus`.
        """

    def signal_names(self) -> list[str]:
        """Return sorted list of all signal names in local cache.

        Returns:
            Sorted list of signal name strings.
        """
        with self._lock:
            return sorted(self._cache.keys())

    def signals_by_prefix(
        self, prefix: str,
    ) -> dict[str, tuple[Any, Optional[float]]]:
        """Return all signals matching a prefix with timestamps.

        Args:
            prefix: Signal name prefix.

        Returns:
            Dict of name to ``(value, timestamp)`` for matching signals.
        """
        with self._lock:
            return {
                k: (v, self._timestamps.get(k))
                for k, v in self._cache.items()
                if k.startswith(prefix)
            }

    def read_many(
        self, names: list[str], default: Any = 0.0,
    ) -> dict[str, Any]:
        """Read multiple signal values atomically.

        Args:
            names:   List of signal names.
            default: Value for signals not yet written.

        Returns:
            Dict of name to value.
        """
        with self._lock:
            return {n: self._cache.get(n, default) for n in names}
