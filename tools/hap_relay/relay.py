"""HAP relay — fake HomeKit switch that relays state to MQTT.

Creates a virtual HomeKit switch accessory. When Apple Home
automations flip this switch (e.g., "SMS2 detects motion → turn
on GlowUp Motion Relay"), the state change is published to MQTT
so GlowUp can act on it.

The switch auto-resets to OFF after a configurable delay so the
automation can re-trigger on the next motion event.

Usage::

    python -m tools.hap_relay.relay --broker 10.0.0.214

Requires: HAP-python, paho-mqtt.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import argparse
import json
import logging
import signal
import threading
import time
from typing import Any, Optional

_missing: list[str] = []
try:
    from pyhap.accessory import Accessory, Bridge
    from pyhap.accessory_driver import AccessoryDriver
    from pyhap import loader as service_loader
except ImportError:
    _missing.append("HAP-python")
try:
    import paho.mqtt.client as mqtt
except ImportError:
    _missing.append("paho-mqtt")
if _missing:
    import sys
    sys.exit(
        f"tools.hap_relay.relay: missing packages: {', '.join(_missing)}  "
        f"— pip install {' '.join(_missing)}"
    )

logger: logging.Logger = logging.getLogger("glowup.hap_relay")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic where motion events are published.
MQTT_TOPIC: str = "glowup/hap_relay/{name}"

# Auto-reset delay — switch flips back to OFF after this many seconds
# so the next motion event can trigger it again.
AUTO_RESET_SECONDS: float = 5.0

# HAP accessory persist file — stores pairing keys across restarts.
PERSIST_FILE: str = "/etc/glowup/hap_relay.state"


# ---------------------------------------------------------------------------
# Relay Switch Accessory
# ---------------------------------------------------------------------------

class RelaySwitch(Accessory):
    """Virtual HomeKit switch that publishes state changes to MQTT.

    When Apple Home turns this switch ON (via automation), the
    event is published to MQTT and the switch auto-resets to OFF.

    Args:
        driver:       HAP AccessoryDriver.
        display_name: Name shown in Apple Home.
        mqtt_client:  Connected paho MQTT client.
        mqtt_topic:   MQTT topic for publishing events.
        reset_delay:  Seconds before auto-reset to OFF.
    """

    category = 8  # HAP category: Switch.

    def __init__(
        self,
        driver: AccessoryDriver,
        display_name: str,
        *,
        mqtt_client: mqtt.Client,
        mqtt_topic: str,
        reset_delay: float = AUTO_RESET_SECONDS,
    ) -> None:
        """Initialize the relay switch."""
        super().__init__(driver, display_name)
        self._mqtt: mqtt.Client = mqtt_client
        self._topic: str = mqtt_topic
        self._reset_delay: float = reset_delay
        self._reset_timer: Optional[threading.Timer] = None

        # Add the Switch service.
        switch_svc = self.add_preload_service("Switch")
        self._on_char = switch_svc.configure_char(
            "On", setter_callback=self._on_set,
        )

    def _on_set(self, value: bool) -> None:
        """Called when Apple Home sets the switch state.

        Args:
            value: True = ON (motion detected), False = OFF.
        """
        # Cancel any pending reset.
        if self._reset_timer is not None:
            self._reset_timer.cancel()
            self._reset_timer = None

        timestamp: float = time.time()
        state: str = "on" if value else "off"

        payload: str = json.dumps({
            "source": "hap_relay",
            "name": self.display_name,
            "state": state,
            "timestamp": timestamp,
        })

        self._mqtt.publish(self._topic, payload, qos=1)
        logger.info(
            "[%s] → MQTT %s: %s",
            self.display_name, self._topic, state,
        )

        # Auto-reset to OFF so the automation can re-trigger.
        if value:
            self._reset_timer = threading.Timer(
                self._reset_delay, self._auto_reset,
            )
            self._reset_timer.daemon = True
            self._reset_timer.start()

    def _auto_reset(self) -> None:
        """Reset the switch to OFF and notify HomeKit.

        Uses client_update_value so Apple Home sees the switch
        flip back to OFF — without this, the automation won't
        re-trigger because HomeKit thinks the switch is already ON.
        """
        self._on_char.client_update_value(False)
        logger.info("[%s] Auto-reset to OFF (notified HomeKit)", self.display_name)

    def stop(self) -> None:
        """Clean up on shutdown."""
        if self._reset_timer is not None:
            self._reset_timer.cancel()
        super().stop()


# ---------------------------------------------------------------------------
# MQTT setup
# ---------------------------------------------------------------------------

def _connect_mqtt(broker: str, port: int) -> mqtt.Client:
    """Create and connect an MQTT client.

    Args:
        broker: MQTT broker address.
        port:   MQTT broker port.

    Returns:
        Connected paho MQTT client.
    """
    client_id: str = f"hap-relay-{int(time.time())}"
    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
    else:
        client = mqtt.Client(client_id=client_id)

    client.connect(broker, port)
    client.loop_start()
    logger.info("MQTT connected: %s:%d", broker, port)
    return client


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse args and run the HAP relay."""
    parser = argparse.ArgumentParser(
        description="GlowUp HAP Relay — fake HomeKit switch → MQTT",
    )
    parser.add_argument(
        "--broker", type=str, default="localhost",
        help="MQTT broker address",
    )
    parser.add_argument(
        "--mqtt-port", type=int, default=1883,
        help="MQTT broker port",
    )
    parser.add_argument(
        "--name", type=str, default="GlowUp Motion Relay",
        help="HomeKit accessory name (shown in Apple Home)",
    )
    parser.add_argument(
        "--port", type=int, default=51826,
        help="HAP server port",
    )
    parser.add_argument(
        "--persist", type=str, default=PERSIST_FILE,
        help="Path to persist pairing state",
    )
    parser.add_argument(
        "--reset-delay", type=float, default=AUTO_RESET_SECONDS,
        help="Seconds before auto-reset to OFF",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # MQTT.
    mqtt_client: mqtt.Client = _connect_mqtt(args.broker, args.mqtt_port)

    # Build topic from sanitized name.
    safe_name: str = args.name.lower().replace(" ", "_")
    topic: str = MQTT_TOPIC.format(name=safe_name)

    # HAP driver.
    driver = AccessoryDriver(
        port=args.port,
        persist_file=args.persist,
    )

    # Create the relay switch.
    relay = RelaySwitch(
        driver,
        args.name,
        mqtt_client=mqtt_client,
        mqtt_topic=topic,
        reset_delay=args.reset_delay,
    )
    driver.add_accessory(relay)

    # Signal handling.
    def shutdown(sig: int, frame: Any) -> None:
        """Stop the HAP driver and MQTT client on signal."""
        logger.info("Shutting down (signal %d)", sig)
        driver.stop()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()

    signal.signal(signal.SIGTERM, shutdown)

    logger.info(
        "HAP Relay '%s' starting on port %d → MQTT %s",
        args.name, args.port, topic,
    )
    logger.info(
        "Add to Apple Home using the setup code shown below.",
    )

    # Blocks until stopped.
    driver.start()


if __name__ == "__main__":
    main()
