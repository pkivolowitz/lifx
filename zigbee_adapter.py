"""Zigbee adapter — normalizes Zigbee2MQTT payloads to SignalBus signals.

Subscribes to ``zigbee2mqtt/#`` MQTT topics, parses the JSON payloads
published by Zigbee2MQTT for each device, and writes normalized signals
to the :class:`~media.SignalBus` following the ``{source}:{domain}:{signal}``
convention.

This adapter handles Zigbee devices paired to the SONOFF dongle via Z2M:
motion sensors, contact sensors, temperature/humidity sensors, etc.  It
does NOT handle locks — those stay on Vivint (see ``vivint_adapter.py``).

Signal normalization:
    - Boolean values (occupancy, contact): ``True`` → ``1.0``, ``False`` → ``0.0``
    - Battery: 0-100 integer → 0.0-1.0 normalized float
    - Environmental (temperature, humidity, illuminance): raw float, natural range
    - Lock state: ``"LOCK"`` → ``1.0``, ``"UNLOCK"`` → ``0.0`` (future-proof)

The adapter also publishes to MQTT topic ``glowup/zigbee/{name}/{property}``
for remote subscribers that aren't on the local SignalBus.

Requires ``paho-mqtt`` (already a project dependency for MQTT bridge).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import json
import logging
from typing import Any, Optional

from adapter_base import MqttAdapterBase
from media import SignalMeta

logger: logging.Logger = logging.getLogger("glowup.zigbee")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default Zigbee2MQTT topic prefix.
DEFAULT_Z2M_PREFIX: str = "zigbee2mqtt"

# Default GlowUp output topic prefix.
DEFAULT_GLOWUP_PREFIX: str = "glowup/zigbee"

# Transport identifier for metadata registration.
TRANSPORT: str = "zigbee"

# Z2M bridge subtopic — internal coordination, skip these.
BRIDGE_SUBTOPIC: str = "bridge"

# MQTT QoS for normalized messages (at-most-once for low-latency sensors).
MQTT_QOS: int = 0

# Battery normalization divisor.
BATTERY_SCALE: float = 100.0

# Lock state string mapping (future-proof if locks ever move off Vivint).
LOCK_STATE_MAP: dict[str, float] = {
    "LOCK": 1.0,
    "UNLOCK": 0.0,
}

# Boolean property names — these get normalized to 1.0/0.0.
BOOLEAN_PROPERTIES: frozenset[str] = frozenset({
    "occupancy", "contact", "water_leak", "vibration",
    "tamper", "battery_low",
})


# ---------------------------------------------------------------------------
# ZigbeeAdapter
# ---------------------------------------------------------------------------

class ZigbeeAdapter(MqttAdapterBase):
    """Normalize Zigbee2MQTT payloads into SignalBus signals and MQTT topics.

    Args:
        config:      The ``"zigbee"`` section of server.json.
        bus:         The shared :class:`~media.SignalBus`.
        broker:      MQTT broker address.
        port:        MQTT broker port.
    """

    def __init__(
        self,
        config: dict[str, Any],
        bus: Any,
        broker: str = "localhost",
        port: int = 1883,
    ) -> None:
        """Initialize the Zigbee adapter.

        Args:
            config: Zigbee config section from server.json.
            bus:    SignalBus instance for signal writes.
            broker: MQTT broker address.
            port:   MQTT broker port.
        """
        z2m_prefix: str = config.get("z2m_prefix", DEFAULT_Z2M_PREFIX)
        super().__init__(
            broker=broker,
            port=port,
            subscribe_prefix=z2m_prefix,
            client_id_prefix="glowup-zigbee",
        )
        self._bus: Any = bus
        self._glowup_prefix: str = config.get(
            "topic_prefix", DEFAULT_GLOWUP_PREFIX,
        )
        # Optional power logger — set by server.py after construction.
        self._power_logger: Any = None

    # --- Message handling --------------------------------------------------

    def _handle_message(self, topic: str, payload: bytes) -> None:
        """Parse and normalize a single Z2M message.

        Args:
            topic:   The MQTT topic string.
            payload: The raw message payload.
        """
        # Parse topic: zigbee2mqtt/{friendly_name}
        parts: list[str] = topic.split("/")
        if len(parts) < 2:
            return

        # Skip bridge internal messages.
        if parts[1] == BRIDGE_SUBTOPIC:
            return

        friendly_name: str = parts[1]

        # Skip subtopics like zigbee2mqtt/{name}/set or /get.
        if len(parts) > 2:
            return

        # Parse JSON payload.
        try:
            data: dict[str, Any] = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if not isinstance(data, dict):
            return

        # Normalize and publish each property.
        for key, raw_value in data.items():
            normalized: Optional[float] = self._normalize_value(key, raw_value)
            if normalized is None:
                continue

            # Write to SignalBus (transport-free naming).
            signal_name: str = f"{friendly_name}:{key}"
            if hasattr(self._bus, 'register'):
                self._bus.register(signal_name, SignalMeta(
                    signal_type="scalar",
                    description=f"Zigbee {friendly_name} {key}",
                    source_name=friendly_name,
                    transport=TRANSPORT,
                ))
            self._bus.write(signal_name, normalized)

            # Log power readings if a power logger is attached.
            if self._power_logger is not None:
                self._power_logger.record(friendly_name, key, normalized)

            # Publish to GlowUp MQTT topic for remote subscribers.
            if self._client:
                out_topic: str = (
                    f"{self._glowup_prefix}/{friendly_name}/{key}"
                )
                try:
                    self._client.publish(
                        out_topic,
                        f"{normalized}",
                        qos=MQTT_QOS,
                    )
                except Exception:
                    pass  # Best-effort.

    def _normalize_value(
        self,
        key: str,
        raw_value: Any,
    ) -> Optional[float]:
        """Normalize a Z2M property value to a float signal.

        Args:
            key:       Property name (e.g., ``"occupancy"``, ``"battery"``).
            raw_value: Raw value from Z2M JSON payload.

        Returns:
            Normalized float, or ``None`` if the value can't be normalized.
        """
        # Lock state string mapping.
        if key == "lock_state" and isinstance(raw_value, str):
            return LOCK_STATE_MAP.get(raw_value)

        # Boolean properties → 1.0 / 0.0.
        if key in BOOLEAN_PROPERTIES:
            if isinstance(raw_value, bool):
                return 1.0 if raw_value else 0.0
            # Some Z2M devices send 0/1 integers for booleans.
            if isinstance(raw_value, (int, float)):
                return 1.0 if raw_value else 0.0
            return None

        # Battery: 0-100 integer → 0.0-1.0.
        if key == "battery":
            try:
                return float(raw_value) / BATTERY_SCALE
            except (ValueError, TypeError):
                return None

        # Numeric properties: temperature, humidity, illuminance, etc.
        if isinstance(raw_value, (int, float)):
            try:
                return float(raw_value)
            except (ValueError, TypeError, OverflowError):
                return None

        # Non-numeric, non-boolean values (strings, objects) — skip.
        return None

    # --- Introspection -----------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return adapter status for API responses.

        Returns:
            Dict with connection state and config.
        """
        return {
            "running": self._running,
            "z2m_prefix": self._subscribe_prefix,
            "glowup_prefix": self._glowup_prefix,
        }

    # --- Hooks -------------------------------------------------------------

    def _on_started(self) -> None:
        """Log Zigbee-specific start message."""
        logger.info(
            "Zigbee adapter started — subscribing to %s/#",
            self._subscribe_prefix,
        )

    def _on_stopped(self) -> None:
        """Log Zigbee-specific stop message."""
        logger.info("Zigbee adapter stopped")
