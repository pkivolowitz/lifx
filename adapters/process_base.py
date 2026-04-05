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

    adapter = MyAdapter("my_adapter", broker="10.0.0.214")
    adapter.start()
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import logging
import os
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

        Args:
            name:  Signal name (e.g., "Office Motion:occupancy").
            value: Signal value (JSON-serializable).
        """
        topic: str = TOPIC_SIGNALS.format(name=name)
        payload: str = json.dumps(value)
        self._client.publish(topic, payload, qos=QOS_SIGNAL)

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
        """Publish a heartbeat message and schedule the next one."""
        uptime_s: float = time.monotonic() - self._start_time
        heartbeat: dict[str, Any] = {
            "adapter": self._adapter_id,
            "pid": os.getpid(),
            "uptime_s": round(uptime_s, 1),
            "ts": time.time(),
            "state": "running",
            "detail": self.get_status_detail(),
        }

        topic: str = TOPIC_HEARTBEAT.format(id=self._adapter_id)
        self._client.publish(
            topic,
            json.dumps(heartbeat),
            qos=QOS_HEARTBEAT,
            retain=True,
        )

        # Schedule next heartbeat.
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
        except Exception:
            pass

        self._client.loop_stop()
        self._client.disconnect()
        logger.info("[%s] Adapter stopped", self._adapter_id)
