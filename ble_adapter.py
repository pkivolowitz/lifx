"""BLE adapter — bridges BLE sensor MQTT topics to the SignalBus.

The BLE sensor daemon (running on broker-2 Pi) publishes to MQTT topics::

    glowup/ble/{label}/motion       → "1" or "0"
    glowup/ble/{label}/temperature  → float Celsius
    glowup/ble/{label}/humidity     → float percentage
    glowup/ble/{label}/status       → JSON health blob

This adapter subscribes to ``glowup/ble/#``, normalizes the payloads,
and writes signals to the :class:`~media.SignalBus` following the
``{source}:{domain}:{signal}`` convention::

    ble:{label}:motion       → 1.0 / 0.0
    ble:{label}:temperature  → raw Celsius float
    ble:{label}:humidity     → raw percentage float

The ``status`` subtopic is a JSON blob (not a scalar) — it is stored
as a metadata annotation, not a bus signal.

This adapter replaces the side-effect sensor data population that
:class:`~automation.AutomationManager` used to perform.  With all
three sensor transports (BLE, Zigbee, Vivint) following the same
adapter → bus pattern, the AutomationManager is no longer needed.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import time
from typing import Any, Optional

from media import SignalMeta

logger: logging.Logger = logging.getLogger("glowup.ble_adapter")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic prefix for BLE sensor data.
MQTT_PREFIX: str = "glowup/ble"

# Transport identifier for metadata registration.
TRANSPORT: str = "ble"

# MQTT QoS for subscriptions (at-most-once for low-latency sensors).
MQTT_QOS: int = 0

# Subtopics that produce numeric signals.
NUMERIC_SUBTOPICS: dict[str, str] = {
    "motion": "int",         # 1 or 0 → 1.0 / 0.0
    "temperature": "float",  # Celsius
    "humidity": "float",     # Percentage
}

# ---------------------------------------------------------------------------
# Optional dependency
# ---------------------------------------------------------------------------

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
    _PAHO_V2: bool = hasattr(mqtt, "CallbackAPIVersion")
except ImportError:
    _HAS_PAHO = False
    _PAHO_V2 = False
    mqtt = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# BleAdapter
# ---------------------------------------------------------------------------

class BleAdapter:
    """Bridge BLE sensor MQTT topics to the SignalBus.

    Args:
        bus:    The shared :class:`~media.SignalBus`.
        broker: MQTT broker address.
        port:   MQTT broker port.
    """

    def __init__(
        self,
        bus: Any,
        broker: str = "localhost",
        port: int = 1883,
    ) -> None:
        """Initialize the BLE adapter.

        Args:
            bus:    SignalBus instance for signal writes.
            broker: MQTT broker address.
            port:   MQTT broker port.
        """
        self._bus: Any = bus
        self._broker: str = broker
        self._port: int = port
        self._client: Any = None
        self._running: bool = False

        # Status blobs — stored separately since they are JSON, not scalars.
        # Keyed by label → dict.
        self._status: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        """Start the MQTT subscriber for BLE topics."""
        if not _HAS_PAHO:
            logger.warning("paho-mqtt not installed — BLE adapter disabled")
            return

        self._running = True

        if _PAHO_V2:
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"glowup-ble-adapter-{int(time.time())}",
            )
        else:
            self._client = mqtt.Client(
                client_id=f"glowup-ble-adapter-{int(time.time())}",
            )

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect_async(self._broker, self._port)
        self._client.loop_start()

        logger.info("BLE adapter started — subscribing to %s/#", MQTT_PREFIX)

    def stop(self) -> None:
        """Stop the MQTT subscriber."""
        self._running = False
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
        logger.info("BLE adapter stopped")

    def get_status_blob(self, label: str) -> Optional[dict[str, Any]]:
        """Get the last health status JSON for a sensor.

        Args:
            label: Sensor label.

        Returns:
            Status dict, or None if not received.
        """
        return self._status.get(label)

    # --- MQTT callbacks ----------------------------------------------------

    def _on_connect(
        self, client: Any, userdata: Any, flags: Any, rc: int,
        properties: Any = None,
    ) -> None:
        """Subscribe to all BLE topics on connect.

        Args:
            client:     The paho MQTT client.
            userdata:   Unused.
            flags:      Connection flags.
            rc:         Return code.
            properties: MQTT v5 properties (unused).
        """
        if rc != 0:
            logger.warning("BLE adapter MQTT connect failed: rc=%d", rc)
            return
        topic: str = f"{MQTT_PREFIX}/#"
        client.subscribe(topic)
        logger.info("BLE adapter subscribed to %s", topic)

    def _on_message(
        self, client: Any, userdata: Any, msg: Any,
    ) -> None:
        """Parse BLE MQTT message and write to SignalBus.

        Args:
            client:   The paho MQTT client.
            userdata: Unused.
            msg:      The MQTT message.
        """
        try:
            parts: list[str] = msg.topic.split("/")
            # Expected: glowup/ble/{label}/{subtopic}
            if len(parts) != 4:
                return

            label: str = parts[2]
            subtopic: str = parts[3]
            payload: str = msg.payload.decode("utf-8", errors="replace")

            if subtopic == "status":
                # JSON health blob — store separately, not on bus.
                try:
                    self._status[label] = json.loads(payload)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
                return

            if subtopic not in NUMERIC_SUBTOPICS:
                return

            # Parse and write to SignalBus.
            value_type: str = NUMERIC_SUBTOPICS[subtopic]
            if value_type == "int":
                fval: float = float(int(float(payload.strip())))
            else:
                fval = float(payload.strip())

            signal_name: str = f"{label}:{subtopic}"
            # Register with transport metadata on first write.
            if hasattr(self._bus, 'register'):
                self._bus.register(signal_name, SignalMeta(
                    signal_type="scalar",
                    description=f"BLE {label} {subtopic}",
                    source_name=label,
                    transport=TRANSPORT,
                ))
            self._bus.write(signal_name, fval)

        except Exception as exc:
            logger.debug("BLE adapter message error: %s", exc)
