"""MQTT bridge for the GlowUp REST API server.

Connects the GlowUp server to an MQTT broker, enabling
publish/subscribe control and monitoring of LIFX devices.  Command
messages received on subscribed topics are dispatched to the
:class:`DeviceManager`; device state changes are published as
retained messages.

Requires the ``paho-mqtt`` package::

    pip install paho-mqtt

The bridge is optional — it starts only when an ``"mqtt"`` section
is present in the server configuration file.  If ``paho-mqtt`` is
not installed, the server runs normally without MQTT support.

Topic layout (default prefix ``glowup``)::

    Published (retained):
        {prefix}/status                          "online" / "offline" (LWT)
        {prefix}/devices                         JSON device list
        {prefix}/device/{device_id}/state        JSON effect status

    Published (not retained, optional):
        {prefix}/device/{device_id}/colors       JSON zone HSBK array

    Subscribed (commands):
        {prefix}/device/{device_id}/command/play     {"effect":"name","params":{}}
        {prefix}/device/{device_id}/command/stop     (any payload)
        {prefix}/device/{device_id}/command/resume   (any payload)
        {prefix}/device/{device_id}/command/power    {"on": true}
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "1.0"

import json
import logging
import threading
import time as time_mod
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt
    PAHO_AVAILABLE: bool = True
except ImportError:
    mqtt = None  # type: ignore[assignment]
    PAHO_AVAILABLE = False

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default MQTT broker hostname (assumes broker is on the same machine).
DEFAULT_BROKER: str = "localhost"

# Default MQTT broker port (standard unencrypted).
DEFAULT_PORT: int = 1883

# Default MQTT broker TLS port.
DEFAULT_TLS_PORT: int = 8883

# Default topic prefix for all GlowUp MQTT messages.
DEFAULT_TOPIC_PREFIX: str = "glowup"

# Default interval for publishing live zone colors (seconds).
DEFAULT_COLOR_INTERVAL: float = 1.0

# How often the state publisher polls DeviceManager for changes (seconds).
STATE_POLL_INTERVAL: float = 2.0

# Minimum reconnect delay after unexpected disconnect (seconds).
RECONNECT_MIN_DELAY: int = 1

# Maximum reconnect delay (exponential backoff cap, seconds).
RECONNECT_MAX_DELAY: int = 60

# Client ID prefix — combined with a timestamp for uniqueness.
CLIENT_ID_PREFIX: str = "glowup-server"

# MQTT QoS levels.
QOS_AT_LEAST_ONCE: int = 1
QOS_AT_MOST_ONCE: int = 0

# Topic segments.
TOPIC_STATUS: str = "status"
TOPIC_DEVICES: str = "devices"
TOPIC_DEVICE: str = "device"
TOPIC_STATE: str = "state"
TOPIC_COLORS: str = "colors"
TOPIC_COMMAND: str = "command"

# Command action names (must match the final segment of command topics).
ACTION_PLAY: str = "play"
ACTION_STOP: str = "stop"
ACTION_RESUME: str = "resume"
ACTION_POWER: str = "power"

# Availability payloads.
PAYLOAD_ONLINE: str = "online"
PAYLOAD_OFFLINE: str = "offline"


class MqttBridge:
    """Bridge between the GlowUp DeviceManager and an MQTT broker.

    Subscribes to command topics and dispatches them to the
    DeviceManager.  Publishes device state changes as retained
    messages and optionally publishes live zone colors.

    The bridge is entirely optional — the server works identically
    without it.  It is started only when the configuration file
    contains an ``"mqtt"`` section with at least a ``"broker"`` key.

    Attributes:
        broker:          Hostname or IP of the MQTT broker.
        port:            Broker TCP port.
        topic_prefix:    Root prefix for all published/subscribed topics.
        publish_colors:  Whether to publish live zone color data.
        color_interval:  Seconds between color publishes.
    """

    def __init__(
        self,
        device_manager: Any,
        config: dict[str, Any],
        scheduler: Optional[Any] = None,
    ) -> None:
        """Initialize the MQTT bridge.

        Args:
            device_manager: The shared :class:`DeviceManager` instance.
            config:         The full server configuration dict (must
                            contain an ``"mqtt"`` sub-dict).
            scheduler:      Optional :class:`SchedulerThread` reference
                            (used to look up active schedule entries for
                            the phone-override mechanism).
        """
        mqtt_cfg: dict[str, Any] = config.get("mqtt", {})

        self._dm: Any = device_manager
        self._scheduler: Optional[Any] = scheduler
        self._config: dict[str, Any] = config

        # Connection settings.
        self.broker: str = mqtt_cfg.get("broker", DEFAULT_BROKER)
        self.port: int = mqtt_cfg.get("port", DEFAULT_PORT)
        self.topic_prefix: str = mqtt_cfg.get(
            "topic_prefix", DEFAULT_TOPIC_PREFIX,
        )
        self._username: Optional[str] = mqtt_cfg.get("username")
        self._password: Optional[str] = mqtt_cfg.get("password")
        self._tls: bool = mqtt_cfg.get("tls", False)

        # Publishing options.
        self.publish_colors: bool = mqtt_cfg.get("publish_colors", False)
        self.color_interval: float = mqtt_cfg.get(
            "color_interval", DEFAULT_COLOR_INTERVAL,
        )

        # Internal state.
        self._stop_event: threading.Event = threading.Event()
        self._client: Optional[mqtt.Client] = None  # type: ignore[union-attr]
        self._state_thread: Optional[threading.Thread] = None
        self._color_thread: Optional[threading.Thread] = None

        # Change-detection caches (JSON strings of last-published data).
        # Protected by _cache_lock since publisher threads and
        # _on_connect (which clears caches) can race.
        self._cache_lock: threading.Lock = threading.Lock()
        self._last_states: dict[str, str] = {}
        self._last_devices: Optional[str] = None

    # -- Topic helpers ------------------------------------------------------

    def _topic(self, *segments: str) -> str:
        """Build a full topic string from segments.

        Args:
            *segments: Topic path segments appended after the prefix.

        Returns:
            The fully-qualified MQTT topic string.
        """
        return "/".join([self.topic_prefix, *segments])

    # -- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Connect to the broker and start background publisher threads.

        If the connection fails, the error is logged but the server
        continues running — MQTT is best-effort.
        """
        if not PAHO_AVAILABLE:
            logger.error(
                "paho-mqtt is not installed — MQTT bridge disabled.  "
                "Install with: pip install paho-mqtt"
            )
            return

        client_id: str = (
            f"{CLIENT_ID_PREFIX}-{int(time_mod.time())}"
        )
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )

        if self._username:
            self._client.username_pw_set(self._username, self._password)

        if self._tls:
            self._client.tls_set()

        # Last Will and Testament — broker publishes "offline" if we
        # disconnect unexpectedly.
        self._client.will_set(
            self._topic(TOPIC_STATUS),
            payload=PAYLOAD_OFFLINE,
            qos=QOS_AT_LEAST_ONCE,
            retain=True,
        )

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        self._client.reconnect_delay_set(
            RECONNECT_MIN_DELAY, RECONNECT_MAX_DELAY,
        )

        try:
            self._client.connect(self.broker, self.port)
        except Exception as exc:
            logger.error("MQTT connect to %s:%d failed: %s",
                         self.broker, self.port, exc)
            return

        # paho's network loop runs in its own daemon thread.
        self._client.loop_start()

        # State publisher thread — polls DeviceManager for changes.
        self._state_thread = threading.Thread(
            target=self._state_publisher_loop,
            name="mqtt-state",
            daemon=True,
        )
        self._state_thread.start()

        # Optional color publisher thread.
        if self.publish_colors:
            self._color_thread = threading.Thread(
                target=self._color_publisher_loop,
                name="mqtt-colors",
                daemon=True,
            )
            self._color_thread.start()

        logger.info(
            "MQTT bridge started — broker %s:%d, prefix '%s'",
            self.broker, self.port, self.topic_prefix,
        )

    def stop(self) -> None:
        """Disconnect from the broker and stop publisher threads."""
        self._stop_event.set()

        # Join publisher threads BEFORE stopping the paho loop so
        # in-flight publishes can complete rather than racing with
        # loop_stop() tearing down the network layer.
        if self._state_thread is not None:
            self._state_thread.join(timeout=5.0)
        if self._color_thread is not None:
            self._color_thread.join(timeout=5.0)

        if self._client is not None:
            # Publish offline status before disconnecting.
            try:
                self._client.publish(
                    self._topic(TOPIC_STATUS),
                    PAYLOAD_OFFLINE,
                    qos=QOS_AT_LEAST_ONCE,
                    retain=True,
                )
            except Exception:
                pass
            self._client.loop_stop()
            self._client.disconnect()

        logger.info("MQTT bridge stopped.")

    # -- paho callbacks -----------------------------------------------------

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        """Handle successful broker connection.

        Subscribes to all command topics and publishes the online
        availability message.  Re-subscribes on every connect so
        subscriptions survive broker restarts.
        """
        if reason_code == 0:
            logger.info("MQTT connected to %s:%d", self.broker, self.port)

            # Subscribe to: {prefix}/device/+/command/+
            # The + wildcard matches any single topic level, so this
            # captures all device IDs and all action names.
            sub_topic: str = self._topic(
                TOPIC_DEVICE, "+", TOPIC_COMMAND, "+",
            )
            client.subscribe(sub_topic, qos=QOS_AT_LEAST_ONCE)
            logger.info("MQTT subscribed to %s", sub_topic)

            # Publish online availability (retained).
            client.publish(
                self._topic(TOPIC_STATUS),
                PAYLOAD_ONLINE,
                qos=QOS_AT_LEAST_ONCE,
                retain=True,
            )

            # Force a full state publish on reconnect by clearing caches.
            with self._cache_lock:
                self._last_states.clear()
                self._last_devices = None
        else:
            logger.error(
                "MQTT connection refused: reason_code=%s", reason_code,
            )

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        """Log unexpected disconnections (paho reconnects automatically)."""
        if reason_code != 0:
            logger.warning(
                "MQTT disconnected unexpectedly (rc=%s), reconnecting...",
                reason_code,
            )

    def _on_message(
        self,
        client: Any,
        userdata: Any,
        msg: Any,
    ) -> None:
        """Dispatch incoming command messages to the DeviceManager."""
        try:
            self._dispatch_command(msg.topic, msg.payload)
        except Exception:
            logger.exception(
                "Error handling MQTT message on %s", msg.topic,
            )

    # -- Command dispatch ---------------------------------------------------

    def _dispatch_command(self, topic: str, payload: bytes) -> None:
        """Parse a command topic and route to the appropriate action.

        Expected topic format::

            {prefix}/device/{device_id}/command/{action}

        Args:
            topic:   The full MQTT topic string.
            payload: The raw message payload (may be empty).
        """
        parts: list[str] = topic.split("/")

        # Locate the "command" segment to extract device_id and action.
        try:
            cmd_idx: int = parts.index(TOPIC_COMMAND)
        except ValueError:
            logger.warning("MQTT: no '%s' segment in topic %s",
                           TOPIC_COMMAND, topic)
            return

        if cmd_idx < 1 or cmd_idx + 1 >= len(parts):
            logger.warning("MQTT: malformed command topic %s", topic)
            return

        device_id: str = parts[cmd_idx - 1]
        action: str = parts[cmd_idx + 1]

        # Parse JSON payload (may be empty for stop/resume).
        body: dict[str, Any] = {}
        if payload:
            try:
                body = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Some commands (stop, resume) don't need a body.
                pass

        # Validate that the device exists.
        em: Any = self._dm.get_emitter(device_id)
        if em is None:
            logger.warning("MQTT: unknown device '%s'", device_id)
            return

        if action == ACTION_PLAY:
            self._handle_play(device_id, body)
        elif action == ACTION_STOP:
            self._handle_stop(device_id)
        elif action == ACTION_RESUME:
            self._handle_resume(device_id)
        elif action == ACTION_POWER:
            self._handle_power(device_id, body)
        else:
            logger.warning(
                "MQTT: unknown action '%s' for device '%s'",
                action, device_id,
            )

    def _handle_play(
        self, device_id: str, body: dict[str, Any],
    ) -> None:
        """Start an effect on a device.

        Args:
            device_id: Device IP or group identifier.
            body:      Parsed JSON with ``effect`` and optional ``params``.
        """
        effect: Optional[str] = body.get("effect")
        if not effect or not isinstance(effect, str):
            logger.warning(
                "MQTT: play command for %s missing 'effect' field",
                device_id,
            )
            return

        params: dict[str, Any] = body.get("params", {})

        # Mark phone override so the scheduler backs off.
        # Pass None as the entry name — the override persists until
        # the user explicitly publishes a resume command.
        self._dm.mark_override(device_id, None)

        try:
            result: dict[str, Any] = self._dm.play(
                device_id, effect, params,
            )
            logger.info(
                "MQTT: play '%s' on %s → %s",
                effect, device_id, result.get("status", "ok"),
            )
        except (KeyError, ValueError) as exc:
            logger.error("MQTT: play failed for %s: %s", device_id, exc)

    def _handle_stop(self, device_id: str) -> None:
        """Stop the current effect on a device.

        Args:
            device_id: Device IP or group identifier.
        """
        if not self._dm.is_overridden(device_id):
            self._dm.mark_override(device_id, None)

        try:
            result: dict[str, Any] = self._dm.stop(device_id)
            logger.info(
                "MQTT: stop %s → %s",
                device_id, result.get("status", "ok"),
            )
        except KeyError as exc:
            logger.error("MQTT: stop failed for %s: %s", device_id, exc)

    def _handle_resume(self, device_id: str) -> None:
        """Clear the phone override and let the scheduler resume.

        Args:
            device_id: Device IP or group identifier.
        """
        self._dm.clear_override(device_id)
        logger.info(
            "MQTT: resume %s (override cleared)", device_id,
        )

    def _handle_power(
        self, device_id: str, body: dict[str, Any],
    ) -> None:
        """Turn a device on or off.

        Args:
            device_id: Device IP or group identifier.
            body:      Parsed JSON with ``on`` boolean.
        """
        on: bool = body.get("on", True)

        if not on:
            self._dm.mark_override(device_id, None)

        try:
            result: dict[str, Any] = self._dm.set_power(device_id, on)
            logger.info(
                "MQTT: power %s %s → %s",
                device_id,
                "on" if on else "off",
                result.get("status", "ok"),
            )
        except KeyError as exc:
            logger.error(
                "MQTT: power failed for %s: %s", device_id, exc,
            )

    # -- State publishing ---------------------------------------------------

    def _state_publisher_loop(self) -> None:
        """Poll DeviceManager and publish state changes as retained messages.

        Runs in a daemon thread.  Publishes only when state actually
        changes (compared by JSON string equality) to avoid flooding
        the broker with identical messages.
        """
        while not self._stop_event.is_set():
            try:
                self._publish_device_list()
                self._publish_device_states()
            except Exception:
                logger.exception("Error in MQTT state publisher")
            self._stop_event.wait(STATE_POLL_INTERVAL)

    def _publish_device_list(self) -> None:
        """Publish the device list if it has changed since last publish."""
        if self._client is None:
            return

        devices: list[dict[str, Any]] = self._dm.devices_as_list()
        payload: str = json.dumps(devices, separators=(",", ":"))

        with self._cache_lock:
            if payload != self._last_devices:
                self._last_devices = payload
                self._client.publish(
                    self._topic(TOPIC_DEVICES),
                    payload,
                    qos=QOS_AT_LEAST_ONCE,
                    retain=True,
                )

    def _publish_device_states(self) -> None:
        """Publish per-device state for any device whose state changed."""
        if self._client is None:
            return

        devices: list[dict[str, Any]] = self._dm.devices_as_list()
        for dev in devices:
            device_id: str = dev.get("ip", "")
            if not device_id:
                continue

            try:
                status: dict[str, Any] = self._dm.get_status(device_id)
            except KeyError:
                continue

            payload: str = json.dumps(status, separators=(",", ":"))

            with self._cache_lock:
                if self._last_states.get(device_id) != payload:
                    self._last_states[device_id] = payload
                    self._client.publish(
                        self._topic(TOPIC_DEVICE, device_id, TOPIC_STATE),
                        payload,
                        qos=QOS_AT_LEAST_ONCE,
                        retain=True,
                    )

    # -- Color publishing ---------------------------------------------------

    def _color_publisher_loop(self) -> None:
        """Publish live zone colors at the configured interval.

        Runs in a daemon thread.  Colors are published as non-retained
        QoS 0 messages to keep broker overhead low — they are ephemeral
        snapshots, not durable state.
        """
        while not self._stop_event.is_set():
            try:
                self._publish_colors()
            except Exception:
                logger.exception("Error in MQTT color publisher")
            self._stop_event.wait(self.color_interval)

    def _publish_colors(self) -> None:
        """Publish current zone colors for all devices."""
        if self._client is None:
            return

        devices: list[dict[str, Any]] = self._dm.devices_as_list()
        for dev in devices:
            device_id: str = dev.get("ip", "")
            if not device_id:
                continue

            colors: Optional[list[dict[str, Any]]] = (
                self._dm.get_colors(device_id)
            )
            if colors is not None:
                self._client.publish(
                    self._topic(
                        TOPIC_DEVICE, device_id, TOPIC_COLORS,
                    ),
                    json.dumps(colors, separators=(",", ":")),
                    qos=QOS_AT_MOST_ONCE,
                )
