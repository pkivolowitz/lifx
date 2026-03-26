"""BLE trigger subscriber — acts on motion events from BLE sensors.

Subscribes to MQTT topics published by the BLE sensor daemon and
triggers GlowUp group actions (power on/off, brightness changes).

Configuration in ``server.json``::

    "ble_triggers": {
        "onvis_motion": {
            "group": "group:living_room",
            "on_motion": {
                "brightness": 70
            },
            "watchdog_minutes": 30,
            "publish_temperature": true,
            "publish_humidity": true
        }
    }

The ``watchdog_minutes`` timer turns lights off if no motion event
(neither ``1`` nor ``0``) arrives within the configured period.
This covers BLE disconnects and sensor failures — if the sensor
goes silent, the lights eventually turn off.

Sensor data (temperature, humidity) is stored and available via
REST endpoints regardless of the trigger configuration.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import struct
import threading
import time
from typing import Any, Callable, Optional

logger: logging.Logger = logging.getLogger("glowup.ble_trigger")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic prefix matching ble/sensor.py.
MQTT_PREFIX: str = "glowup/ble"

# Default watchdog timeout (minutes).
DEFAULT_WATCHDOG_MINUTES: float = 30.0

# Default brightness on motion (percent, 0–100).
DEFAULT_BRIGHTNESS: int = 70

# Minimum seconds between repeated "lights on" commands to avoid
# hammering the bulbs while motion is sustained.
DEBOUNCE_SECONDS: float = 2.0


# ---------------------------------------------------------------------------
# Sensor data store
# ---------------------------------------------------------------------------

class BleSensorData:
    """Thread-safe store for the latest BLE sensor readings.

    Available to REST endpoints for querying current values.
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}

    def update(self, label: str, key: str, value: Any) -> None:
        """Update a sensor value."""
        with self._lock:
            if label not in self._data:
                self._data[label] = {}
            self._data[label][key] = value
            self._data[label]["last_update"] = time.time()

    def get(self, label: str) -> dict[str, Any]:
        """Get all values for a sensor."""
        with self._lock:
            return dict(self._data.get(label, {}))

    def get_all(self) -> dict[str, dict[str, Any]]:
        """Get all sensor data."""
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}


# Singleton sensor data store.
sensor_data: BleSensorData = BleSensorData()


# ---------------------------------------------------------------------------
# BLE Trigger Manager
# ---------------------------------------------------------------------------

class BleTriggerManager:
    """Subscribes to BLE sensor MQTT topics and triggers group actions.

    Runs as a background thread alongside the GlowUp HTTP server.

    Args:
        config: The ``ble_triggers`` section from server.json.
        device_manager: The server's DeviceManager instance.
        broker: MQTT broker address.
        port: MQTT broker port.
    """

    def __init__(
        self,
        config: dict[str, Any],
        device_manager: Any,
        broker: str = "10.0.0.48",
        port: int = 1883,
    ) -> None:
        self._config: dict[str, Any] = config
        self._dm: Any = device_manager
        self._broker: str = broker
        self._port: int = port
        self._client: Any = None
        self._running: bool = False

        # Per-label state.
        self._motion_active: dict[str, bool] = {}
        self._last_motion_time: dict[str, float] = {}
        self._last_lights_on_time: dict[str, float] = {}
        self._watchdog_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the MQTT subscriber and watchdog thread."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.warning(
                "BLE triggers require paho-mqtt — triggers disabled"
            )
            return

        if not self._config:
            logger.info("No ble_triggers configured — skipping")
            return

        self._running = True

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"glowup-ble-trigger-{int(time.time())}",
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect_async(self._broker, self._port)
        self._client.loop_start()

        # Watchdog thread checks for stale motion.
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="ble-watchdog"
        )
        self._watchdog_thread.start()

        logger.info(
            "BLE trigger manager started — %d trigger(s)",
            len(self._config),
        )

    def stop(self) -> None:
        """Stop the subscriber and watchdog."""
        self._running = False
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """Subscribe to all BLE sensor topics on connect."""
        if rc != 0:
            logger.warning("BLE trigger MQTT connect failed: rc=%d", rc)
            return

        # Subscribe to motion, temperature, humidity for each configured label.
        for label in self._config:
            for subtopic in ("motion", "temperature", "humidity", "status"):
                topic: str = f"{MQTT_PREFIX}/{label}/{subtopic}"
                client.subscribe(topic)
                logger.debug("Subscribed to %s", topic)

        logger.info("BLE trigger MQTT connected and subscribed")

    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages from BLE sensors."""
        try:
            # Parse topic: glowup/ble/{label}/{subtopic}
            parts: list[str] = msg.topic.split("/")
            if len(parts) != 4:
                return
            label: str = parts[2]
            subtopic: str = parts[3]
            payload: str = msg.payload.decode("utf-8", errors="replace")

            if label not in self._config:
                return

            trigger_cfg: dict[str, Any] = self._config[label]

            if subtopic == "motion":
                self._handle_motion(label, payload, trigger_cfg)
            elif subtopic == "temperature":
                sensor_data.update(label, "temperature", float(payload))
            elif subtopic == "humidity":
                sensor_data.update(label, "humidity", float(payload))
            elif subtopic == "status":
                sensor_data.update(label, "status", json.loads(payload))

        except Exception as exc:
            logger.error(
                "BLE trigger message error: %s", exc, exc_info=True
            )

    def _handle_motion(
        self, label: str, payload: str, cfg: dict[str, Any]
    ) -> None:
        """Process a motion event."""
        now: float = time.time()
        self._last_motion_time[label] = now
        sensor_data.update(label, "motion", int(payload))

        detected: bool = payload.strip() == "1"

        if detected:
            self._motion_active[label] = True

            # Debounce: don't hammer the bulbs on every poll cycle.
            last_on: float = self._last_lights_on_time.get(label, 0)
            if now - last_on < DEBOUNCE_SECONDS:
                return

            self._last_lights_on_time[label] = now

            # Turn on lights.
            group: str = cfg.get("group", "")
            on_motion: dict = cfg.get("on_motion", {})
            brightness: int = on_motion.get("brightness", DEFAULT_BRIGHTNESS)

            if group and self._dm:
                try:
                    # Power on + set brightness.
                    self._dm.set_power(group, True)
                    self._dm.set_brightness(group, brightness)
                    logger.info(
                        "Motion %s → %s ON @ %d%%",
                        label, group, brightness,
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to trigger %s: %s", group, exc
                    )
        else:
            # Motion cleared by sensor.
            self._motion_active[label] = False
            logger.info("Motion %s cleared (sensor reported 0)", label)
            # Don't turn off immediately — let the watchdog handle it.
            # The sensor's internal timeout already provides a delay.

    def _watchdog_loop(self) -> None:
        """Background thread: turn off lights when motion goes stale.

        Checks every 60 seconds whether any trigger's last motion
        event exceeds its watchdog_minutes timeout.
        """
        WATCHDOG_CHECK_INTERVAL: float = 60.0

        while self._running:
            time.sleep(WATCHDOG_CHECK_INTERVAL)

            now: float = time.time()
            for label, cfg in self._config.items():
                timeout_min: float = cfg.get(
                    "watchdog_minutes", DEFAULT_WATCHDOG_MINUTES
                )
                timeout_sec: float = timeout_min * 60.0
                last: float = self._last_motion_time.get(label, 0)

                if last == 0:
                    # Never received motion — skip.
                    continue

                elapsed: float = now - last
                if elapsed >= timeout_sec and self._motion_active.get(label):
                    # Watchdog fired — turn off lights.
                    group: str = cfg.get("group", "")
                    if group and self._dm:
                        try:
                            self._dm.set_power(group, False)
                            self._motion_active[label] = False
                            logger.info(
                                "Watchdog %s: no motion for %.0f min → "
                                "%s OFF",
                                label, elapsed / 60, group,
                            )
                        except Exception as exc:
                            logger.error(
                                "Watchdog power-off %s failed: %s",
                                group, exc,
                            )
