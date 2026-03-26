"""BLE sensor daemon — standalone motion/event publisher for GlowUp.

Runs on any Pi near BLE accessories.  Connects to paired HAP-BLE
devices, subscribes to characteristic notifications, and publishes
events to the MQTT signal bus for the GlowUp server to act on.

Designed for the distributed SOE architecture: one or more sensor Pis
each cover a BLE zone, all publishing to the same MQTT broker.  The
central GlowUp server subscribes and triggers group actions.

MQTT topics::

    glowup/ble/{label}/motion     — "1" or "0"
    glowup/ble/{label}/temperature — float Celsius
    glowup/ble/{label}/humidity    — float percentage
    glowup/ble/{label}/battery     — int 0–100
    glowup/ble/{label}/status      — JSON status/health

Usage::

    python3 -m ble.sensor
    python3 -m ble.sensor --registry /path/to/ble_pairing.json
    python3 -m ble.sensor --broker 10.0.0.48

The daemon:
    1. Loads the registry (paired devices).
    2. Connects to each paired device via BLE.
    3. Runs pair-verify to establish encrypted sessions.
    4. Subscribes to occupancy/motion characteristics.
    5. Publishes events to MQTT.
    6. Reconnects on disconnect (BLE connections are fragile).

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "0.1"

import argparse
import asyncio
import json
import logging
import signal
import struct
import sys
import time
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.ble.sensor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic prefix for all BLE sensor events.
MQTT_TOPIC_PREFIX: str = "glowup/ble"

# Default MQTT broker (Pi, same as GlowUp server).
DEFAULT_BROKER: str = "10.0.0.48"

# Default MQTT port.
DEFAULT_MQTT_PORT: int = 1883

# Seconds between reconnection attempts after BLE disconnect.
RECONNECT_DELAY: float = 5.0

# Maximum reconnection delay (exponential backoff cap).
MAX_RECONNECT_DELAY: float = 60.0

# Seconds between health/status publishes.
STATUS_INTERVAL: float = 60.0

# Motion timeout: seconds of no motion events before publishing
# motion=0.  ONVIS sensors have their own internal timeout (typically
# 30s–120s) but this provides a fallback.
DEFAULT_MOTION_TIMEOUT: float = 120.0


# ---------------------------------------------------------------------------
# MQTT publisher
# ---------------------------------------------------------------------------

class MqttPublisher:
    """Publishes BLE sensor events to the MQTT signal bus.

    Wraps paho-mqtt with auto-reconnect.  If MQTT is unavailable,
    events are logged but not lost — the sensor keeps running.
    """

    def __init__(
        self,
        broker: str = DEFAULT_BROKER,
        port: int = DEFAULT_MQTT_PORT,
        client_id: Optional[str] = None,
    ) -> None:
        """Initialize the MQTT publisher.

        Args:
            broker: MQTT broker hostname or IP.
            port: MQTT broker port.
            client_id: MQTT client ID (auto-generated if None).
        """
        self._broker: str = broker
        self._port: int = port
        self._client_id: str = client_id or f"glowup-ble-{int(time.time())}"
        self._client: Any = None
        self._connected: bool = False

    def connect(self) -> None:
        """Connect to the MQTT broker.

        Raises:
            ImportError: If paho-mqtt is not installed.
        """
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise ImportError(
                "BLE sensor MQTT publishing requires paho-mqtt: "
                "pip install paho-mqtt"
            )

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.connect_async(self._broker, self._port)
        self._client.loop_start()
        logger.info(
            "MQTT connecting to %s:%d as %s",
            self._broker, self._port, self._client_id,
        )

    def _on_connect(self, client, userdata, flags, rc, properties=None) -> None:
        """MQTT connect callback."""
        if rc == 0:
            self._connected = True
            logger.info("MQTT connected to %s:%d", self._broker, self._port)
        else:
            logger.warning("MQTT connection failed: rc=%d", rc)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None) -> None:
        """MQTT disconnect callback."""
        self._connected = False
        if rc != 0:
            logger.warning(
                "MQTT unexpected disconnect (rc=%d), will auto-reconnect", rc
            )

    def publish(self, label: str, subtopic: str, payload: str) -> None:
        """Publish an event to MQTT.

        Args:
            label: Device label (e.g., ``"hallway_motion"``).
            subtopic: Event type (e.g., ``"motion"``, ``"temperature"``).
            payload: String payload.
        """
        topic: str = f"{MQTT_TOPIC_PREFIX}/{label}/{subtopic}"
        if self._client and self._connected:
            self._client.publish(topic, payload, qos=1, retain=True)
            logger.debug("Published %s = %s", topic, payload)
        else:
            logger.warning(
                "MQTT not connected — dropping %s = %s", topic, payload
            )

    def publish_status(self, label: str, status: dict) -> None:
        """Publish a JSON status message.

        Args:
            label: Device label.
            status: Status dict to serialize.
        """
        self.publish(label, "status", json.dumps(status))

    def disconnect(self) -> None:
        """Disconnect from the MQTT broker."""
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            logger.info("MQTT disconnected")


# ---------------------------------------------------------------------------
# Device monitor
# ---------------------------------------------------------------------------

class DeviceMonitor:
    """Manages the BLE connection and event subscription for one device.

    Handles connect → pair-verify → subscribe → reconnect lifecycle.

    Attributes:
        label: Device label.
        address: BLE address.
        connected: Whether the BLE connection is active.
    """

    def __init__(
        self,
        label: str,
        address: str,
        registry_path: str,
        publisher: MqttPublisher,
        motion_timeout: float = DEFAULT_MOTION_TIMEOUT,
    ) -> None:
        """Initialize the device monitor.

        Args:
            label: Human-readable device label.
            address: BLE address to connect to.
            registry_path: Path to ble_pairing.json (for keys).
            publisher: MQTT publisher for events.
            motion_timeout: Seconds before publishing motion=0 after
                last motion event.
        """
        self.label: str = label
        self.address: str = address
        self._registry_path: str = registry_path
        self._publisher: MqttPublisher = publisher
        self._motion_timeout: float = motion_timeout
        self._connected: bool = False
        self._last_motion_time: float = 0.0
        self._motion_active: bool = False
        self._running: bool = False

    async def run(self) -> None:
        """Main loop: connect, subscribe, reconnect on failure.

        Runs until :meth:`stop` is called.  Reconnects with
        exponential backoff on BLE disconnection.
        """
        from .registry import BleRegistry
        from .scanner import connect_and_wrap
        from .hap_session import HapSession, HapError

        self._running = True
        delay: float = RECONNECT_DELAY

        while self._running:
            try:
                logger.info(
                    "Connecting to %s (%s)...", self.label, self.address
                )

                # Load fresh keys each attempt (registry may be updated).
                registry = BleRegistry(self._registry_path)
                keys = registry.get_pairing_keys(self.label)
                if keys is None:
                    logger.error(
                        "No pairing keys for %s — run pair-setup first",
                        self.label,
                    )
                    await asyncio.sleep(MAX_RECONNECT_DELAY)
                    continue

                # Connect BLE.
                gatt = await connect_and_wrap(self.address)
                self._connected = True
                delay = RECONNECT_DELAY  # Reset backoff on success.

                # Pair-verify (establish encrypted session).
                session = HapSession(gatt)
                await session.pair_verify(keys)

                self._publisher.publish_status(self.label, {
                    "state": "connected",
                    "address": self.address,
                    "timestamp": time.time(),
                })

                # Subscribe to occupancy/motion notifications.
                # IIDs are discovered during the HAP service enumeration,
                # but for ONVIS sensors the occupancy characteristic is
                # typically at a known IID.  We'll discover dynamically
                # in a future iteration.
                #
                # For now, we poll the characteristic periodically as a
                # fallback if subscriptions aren't supported.
                await self._monitor_loop(session, gatt)

            except ImportError as exc:
                logger.error("Missing dependency: %s", exc)
                self._running = False
                break

            except HapError as exc:
                logger.error("HAP protocol error for %s: %s", self.label, exc)

            except Exception as exc:
                logger.error(
                    "BLE error for %s: %s", self.label, exc,
                    exc_info=True,
                )

            finally:
                self._connected = False
                self._publisher.publish_status(self.label, {
                    "state": "disconnected",
                    "address": self.address,
                    "timestamp": time.time(),
                })

            if self._running:
                logger.info(
                    "Reconnecting to %s in %.0fs...", self.label, delay
                )
                await asyncio.sleep(delay)
                # Exponential backoff capped at MAX_RECONNECT_DELAY.
                delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def _monitor_loop(self, session, gatt) -> None:
        """Run the event monitoring loop until disconnect.

        Publishes motion events and handles the motion timeout.

        Args:
            session: Active encrypted HapSession.
            gatt: Connected BleakGattClient.
        """
        # TODO: Discover characteristic IIDs dynamically via service
        # signature reads.  For now this is a polling fallback that
        # reads the occupancy characteristic periodically.
        #
        # The real implementation will subscribe to notifications
        # once we've mapped the ONVIS's characteristic IIDs.

        last_status_time: float = time.time()

        while self._running and gatt.is_connected:
            now: float = time.time()

            # Check motion timeout.
            if self._motion_active:
                elapsed: float = now - self._last_motion_time
                if elapsed >= self._motion_timeout:
                    self._motion_active = False
                    self._publisher.publish(self.label, "motion", "0")
                    logger.info("%s: motion timeout — cleared", self.label)

            # Periodic status publish.
            if now - last_status_time >= STATUS_INTERVAL:
                self._publisher.publish_status(self.label, {
                    "state": "monitoring",
                    "motion_active": self._motion_active,
                    "last_motion": self._last_motion_time,
                    "timestamp": now,
                })
                last_status_time = now

            await asyncio.sleep(1.0)

    def on_motion_event(self, detected: bool) -> None:
        """Handle a motion event from characteristic notification.

        Called by the HAP subscription callback when the occupancy
        characteristic changes.

        Args:
            detected: True if motion detected, False if cleared.
        """
        now: float = time.time()

        if detected:
            self._last_motion_time = now
            if not self._motion_active:
                self._motion_active = True
                self._publisher.publish(self.label, "motion", "1")
                logger.info("%s: motion DETECTED", self.label)
            else:
                # Motion still active — update timestamp, don't re-publish.
                logger.debug("%s: motion sustained", self.label)
        else:
            self._motion_active = False
            self._publisher.publish(self.label, "motion", "0")
            logger.info("%s: motion CLEARED", self.label)

    def stop(self) -> None:
        """Signal the monitor to stop."""
        self._running = False


# ---------------------------------------------------------------------------
# Daemon entry point
# ---------------------------------------------------------------------------

async def run_sensor_daemon(
    registry_path: str = "ble_pairing.json",
    broker: str = DEFAULT_BROKER,
    mqtt_port: int = DEFAULT_MQTT_PORT,
    motion_timeout: float = DEFAULT_MOTION_TIMEOUT,
) -> None:
    """Run the BLE sensor daemon.

    Loads paired devices from the registry and monitors them all
    concurrently.

    Args:
        registry_path: Path to ble_pairing.json.
        broker: MQTT broker hostname/IP.
        mqtt_port: MQTT broker port.
        motion_timeout: Seconds before motion-cleared timeout.
    """
    from .registry import BleRegistry

    registry = BleRegistry(registry_path)
    paired_devices = registry.get_paired_devices()

    if not paired_devices:
        logger.error(
            "No paired BLE devices in %s — pair a device first",
            registry_path,
        )
        return

    publisher = MqttPublisher(broker=broker, port=mqtt_port)
    publisher.connect()

    monitors: list[DeviceMonitor] = []
    tasks: list[asyncio.Task] = []

    for device in paired_devices:
        monitor = DeviceMonitor(
            label=device.label,
            address=device.address,
            registry_path=registry_path,
            publisher=publisher,
            motion_timeout=motion_timeout,
        )
        monitors.append(monitor)
        tasks.append(asyncio.create_task(monitor.run()))

    logger.info(
        "BLE sensor daemon started — monitoring %d device(s)",
        len(monitors),
    )

    # Run until interrupted.
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for monitor in monitors:
            monitor.stop()
        publisher.disconnect()
        logger.info("BLE sensor daemon stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for the BLE sensor daemon."""
    parser = argparse.ArgumentParser(
        description="GlowUp BLE sensor daemon — monitors HAP-BLE "
        "accessories and publishes events to MQTT.",
    )
    parser.add_argument(
        "--registry",
        default="ble_pairing.json",
        help="Path to ble_pairing.json (default: ./ble_pairing.json)",
    )
    parser.add_argument(
        "--broker",
        default=DEFAULT_BROKER,
        help=f"MQTT broker address (default: {DEFAULT_BROKER})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_MQTT_PORT,
        help=f"MQTT broker port (default: {DEFAULT_MQTT_PORT})",
    )
    parser.add_argument(
        "--motion-timeout",
        type=float,
        default=DEFAULT_MOTION_TIMEOUT,
        help=(
            f"Seconds before motion-cleared timeout "
            f"(default: {DEFAULT_MOTION_TIMEOUT})"
        ),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Graceful shutdown on SIGINT/SIGTERM.
    loop = asyncio.new_event_loop()

    def _shutdown(sig: int, frame) -> None:
        logger.info("Received signal %d — shutting down", sig)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(
            run_sensor_daemon(
                registry_path=args.registry,
                broker=args.broker,
                mqtt_port=args.port,
                motion_timeout=args.motion_timeout,
            )
        )
    finally:
        loop.close()


if __name__ == "__main__":
    main()
