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

__version__ = "1.1"

import json
import logging
import threading
from typing import Any, Optional

from adapter_base import MqttAdapterBase
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
# BleAdapter
# ---------------------------------------------------------------------------

class BleAdapter(MqttAdapterBase):
    """Bridge BLE sensor MQTT topics to the SignalBus.

    Args:
        bus:    The shared :class:`~media.SignalBus`.
        broker: MQTT broker address.
        port:   MQTT broker port.
        config: Optional config dict with ``topic_prefix`` key.
    """

    def __init__(
        self,
        bus: Any,
        broker: str = "localhost",
        port: int = 1883,
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        """Initialize the BLE adapter.

        Args:
            bus:    SignalBus instance for signal writes.
            broker: MQTT broker address.
            port:   MQTT broker port.
            config: Optional config dict with ``topic_prefix`` key.
        """
        topic_prefix: str = (config or {}).get("topic_prefix", MQTT_PREFIX)
        super().__init__(
            broker=broker,
            port=port,
            subscribe_prefix=topic_prefix,
            client_id_prefix="glowup-ble-adapter",
        )
        self._bus: Any = bus

        # Status blobs — stored separately since they are JSON, not scalars.
        # Keyed by label → dict.  Protected by _status_lock for thread safety
        # (MQTT callbacks arrive on paho's internal thread).
        self._status: dict[str, dict[str, Any]] = {}
        self._status_lock: threading.Lock = threading.Lock()

    def get_status_blob(self, label: str) -> Optional[dict[str, Any]]:
        """Get the last health status JSON for a sensor.

        Args:
            label: Sensor label.

        Returns:
            Status dict, or None if not received.
        """
        with self._status_lock:
            return self._status.get(label)

    # --- Message handling --------------------------------------------------

    def _handle_message(self, topic: str, payload: bytes) -> None:
        """Parse BLE MQTT message and write to SignalBus.

        Args:
            topic:   The MQTT topic string.
            payload: The raw message payload.
        """
        parts: list[str] = topic.split("/")
        # Expected: glowup/ble/{label}/{subtopic}
        if len(parts) != 4:
            return

        label: str = parts[2]
        subtopic: str = parts[3]
        payload_str: str = payload.decode("utf-8", errors="replace")

        if subtopic == "status":
            # JSON health blob — store separately, not on bus.
            try:
                blob: dict = json.loads(payload_str)
                with self._status_lock:
                    self._status[label] = blob
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            return

        if subtopic not in NUMERIC_SUBTOPICS:
            return

        # Parse and write to SignalBus.
        value_type: str = NUMERIC_SUBTOPICS[subtopic]
        if value_type == "int":
            fval: float = float(int(float(payload_str.strip())))
        else:
            fval = float(payload_str.strip())

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

    # --- Hooks -------------------------------------------------------------

    def _on_started(self) -> None:
        """Log BLE-specific start message."""
        logger.info(
            "BLE adapter started — broker %s:%d, subscribing to %s/#",
            self._broker, self._port, self._subscribe_prefix,
        )

    def _on_stopped(self) -> None:
        """Log BLE-specific stop message."""
        logger.info("BLE adapter stopped")
