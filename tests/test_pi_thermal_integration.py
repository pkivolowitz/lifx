#!/usr/bin/env python3
"""Network integration tests for contrib.sensors.pi_thermal_sensor.

Exercises the daemon against a real MQTT broker reachable on the LAN.
Default target is broker-2 (``10.0.0.123:1883``) since that host is
guaranteed to have an open listener per the GlowUp fleet rebuild
procedure.  Override with the ``GLOWUP_TEST_BROKER`` environment
variable, e.g. ``GLOWUP_TEST_BROKER=10.0.0.214:1883``.

If no broker is reachable within the connection timeout these tests
are skipped, so a headless CI run without fleet access still passes.

Coverage:

- End-to-end publish + subscribe round-trip for a ``ThermalReading``
- Retained thermal topic — a late subscriber immediately receives the
  last reading with no prior state
- Retained capability announcement with correct fields
- Status topic transitions from "online" at connect to "offline" on
  explicit clean shutdown
- Graceful disconnect triggers paho's DISCONNECT packet, not the LWT —
  that case would only fire on a crash, which we can't reliably force
  from userspace in a unit test

Each test uses a unique ``node_id`` so parallel fleet participants
aren't clobbered, and it cleans up retained state on teardown by
publishing empty payloads with ``retain=True``.

Run::

    python3 -m unittest tests.test_pi_thermal_integration -v
    GLOWUP_TEST_BROKER=10.0.0.214:1883 python3 -m pytest \\
        tests/test_pi_thermal_integration.py -v

Requires paho-mqtt.  Without paho the tests skip with a clear reason.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import os
import queue
import socket
import threading
import time
import unittest
from typing import Any, Optional
from unittest.mock import patch

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False

from contrib.sensors import pi_thermal_sensor as pts
from contrib.sensors.pi_thermal_sensor import PiThermalSensor, ThermalReading


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default broker — hub .214 is the canonical GlowUp MQTT broker per
# reference_project_state.md.  Broker-2 (.123) is a dedicated Z2M
# bridge broker and must NOT be used for unrelated telemetry traffic.
_DEFAULT_TEST_BROKER: str = "10.0.0.214:1883"

# IMPORTANT: test topics use a dedicated prefix (glowup/test/thermal/)
# that the production ThermalLogger does NOT subscribe to.  The logger
# subscribes to glowup/hardware/thermal/+ and persists everything it
# receives to SQLite with 7-day retention.  If tests published on the
# production topic, the logger would ingest itest-* payloads and they
# would appear as ghost hosts in the /thermal dashboard until manually
# purged.  This happened on 2026-04-11 and left two stale itest-*
# rows in thermal.db that had to be deleted by hand.
_TEST_TOPIC_PREFIX: str = "glowup/test/thermal/"

# How long to wait for a subscribe message in a test (seconds).
_MESSAGE_WAIT_S: float = 3.0

# TCP connect timeout when reachability-probing the broker.
_CONNECT_PROBE_TIMEOUT_S: float = 1.5

# paho connect keepalive for test clients.
_TEST_KEEPALIVE_S: int = 30


# ---------------------------------------------------------------------------
# Reachability probe
# ---------------------------------------------------------------------------

def _parse_broker(spec: str) -> tuple[str, int]:
    """Parse a ``host:port`` spec.

    Args:
        spec: The raw environment variable value.

    Returns:
        (host, port) tuple.

    Raises:
        ValueError: If the spec is malformed.
    """
    host_str, port_str = spec.rsplit(":", 1)
    return host_str, int(port_str)


def _broker_reachable(host: str, port: int) -> bool:
    """TCP-probe the broker; True if a connect succeeds within the timeout."""
    try:
        with socket.create_connection(
            (host, port), timeout=_CONNECT_PROBE_TIMEOUT_S,
        ):
            return True
    except (OSError, socket.timeout):
        return False


_BROKER_SPEC: str = os.environ.get("GLOWUP_TEST_BROKER", _DEFAULT_TEST_BROKER)
try:
    _TEST_HOST, _TEST_PORT = _parse_broker(_BROKER_SPEC)
except ValueError:
    _TEST_HOST = "127.0.0.1"
    _TEST_PORT = 1883

_BROKER_OK: bool = _HAS_PAHO and _broker_reachable(_TEST_HOST, _TEST_PORT)


# ---------------------------------------------------------------------------
# Test helper — a subscribing client that buffers messages
# ---------------------------------------------------------------------------

class _Subscriber:
    """Minimal paho subscriber that drops received messages into a queue.

    Used by integration tests that need to verify publications without
    racing against the main sensor's publish loop.
    """

    def __init__(self, host: str, port: int, topics: list[str]) -> None:
        """Configure the subscriber and connect."""
        self._host: str = host
        self._port: int = port
        self._topics: list[str] = topics
        self._queue: "queue.Queue[tuple[str, bytes]]" = queue.Queue()
        self._client: mqtt.Client = mqtt.Client(
            client_id=f"integration-sub-{os.getpid()}-{id(self)}",
        )
        self._client.on_message = self._on_message
        self._client.on_connect = self._on_connect
        self._ready: threading.Event = threading.Event()

    def _on_connect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        flags: dict[str, Any],
        rc: int,
    ) -> None:
        """Subscribe to all topics once connected."""
        for topic in self._topics:
            client.subscribe(topic, qos=1)
        self._ready.set()

    def _on_message(
        self,
        client: "mqtt.Client",
        userdata: Any,
        msg: "mqtt.MQTTMessage",
    ) -> None:
        """Push every inbound message into the buffer queue."""
        self._queue.put((msg.topic, msg.payload))

    def start(self) -> None:
        """Connect and spin up the paho network loop."""
        self._client.connect(self._host, self._port, _TEST_KEEPALIVE_S)
        self._client.loop_start()
        if not self._ready.wait(timeout=_MESSAGE_WAIT_S):
            raise RuntimeError("subscriber did not connect in time")

    def wait_for(
        self,
        topic: str,
        timeout: float = _MESSAGE_WAIT_S,
    ) -> Optional[bytes]:
        """Block until a message on ``topic`` arrives or timeout expires.

        Non-matching messages are re-queued so parallel waits work.

        Returns:
            Payload bytes, or ``None`` on timeout.
        """
        deadline: float = time.monotonic() + timeout
        other: list[tuple[str, bytes]] = []
        try:
            while time.monotonic() < deadline:
                try:
                    item: tuple[str, bytes] = self._queue.get(
                        timeout=max(0.05, deadline - time.monotonic()),
                    )
                except queue.Empty:
                    break
                if item[0] == topic:
                    return item[1]
                other.append(item)
            return None
        finally:
            for saved in other:
                self._queue.put(saved)

    def stop(self) -> None:
        """Disconnect and stop the paho loop."""
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:
            # Ignore shutdown races — test teardown should not fail
            # because an already-disconnected client can't re-disconnect.
            pass


# ---------------------------------------------------------------------------
# Retained-state cleanup helper
# ---------------------------------------------------------------------------

def _clear_retained(
    host: str,
    port: int,
    topics: list[str],
) -> None:
    """Publish an empty retained message on every topic to clear state.

    Args:
        host:   Broker host.
        port:   Broker port.
        topics: Topics to clear.
    """
    cleaner: mqtt.Client = mqtt.Client(client_id=f"integration-cleaner-{os.getpid()}")
    cleaner.connect(host, port, _TEST_KEEPALIVE_S)
    cleaner.loop_start()
    for topic in topics:
        info: mqtt.MQTTMessageInfo = cleaner.publish(
            topic, payload=b"", qos=1, retain=True,
        )
        info.wait_for_publish(timeout=_MESSAGE_WAIT_S)
    cleaner.loop_stop()
    cleaner.disconnect()


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

@unittest.skipUnless(
    _HAS_PAHO,
    "paho-mqtt not installed — skipping integration tests",
)
@unittest.skipUnless(
    _BROKER_OK,
    f"test broker {_BROKER_SPEC} is not reachable — skipping integration tests",
)
class PiThermalIntegrationTest(unittest.TestCase):
    """End-to-end tests against a real MQTT broker."""

    @classmethod
    def setUpClass(cls) -> None:
        """Record the broker + per-class topic list for cleanup."""
        cls._host: str = _TEST_HOST
        cls._port: int = _TEST_PORT
        cls._used_topics: list[str] = []

    @classmethod
    def tearDownClass(cls) -> None:
        """Clean every retained topic the tests touched."""
        if cls._used_topics:
            _clear_retained(cls._host, cls._port, cls._used_topics)

    def setUp(self) -> None:
        """Pick a unique node_id for this test and derive its topics."""
        self._node_id: str = (
            f"itest-{os.getpid()}-{int(time.monotonic() * 1000) % 100000}"
        )
        self._thermal_topic: str = (
            f"{_TEST_TOPIC_PREFIX}{self._node_id}"
        )
        self._status_topic: str = (
            f"glowup/node/{self._node_id}/status"
        )
        self._capability_topic: str = (
            f"glowup/node/{self._node_id}/capability"
        )
        type(self)._used_topics.extend([
            self._thermal_topic,
            self._status_topic,
            self._capability_topic,
        ])

    def _make_sensor(self, interval_s: float = 0.25) -> PiThermalSensor:
        """Build a sensor pointed at the integration broker.

        Overrides the sensor's ``_thermal_topic`` to use the test-only
        prefix so the production ThermalLogger (subscribed to
        ``glowup/hardware/thermal/+``) never sees test payloads.
        """
        sensor: PiThermalSensor = PiThermalSensor(
            broker_host=self._host,
            broker_port=self._port,
            interval_s=interval_s,
            node_id=self._node_id,
            hostname=f"{self._node_id}.test",
            fan_declared_present=True,
            pi_model="Raspberry Pi 5 Model B Rev 1.0 (integration)",
            platform="pi5",
        )
        # Redirect publishes to the test-only topic tree.
        sensor._thermal_topic = self._thermal_topic
        return sensor

    def _patch_readers(
        self,
        *,
        cpu_temp: Optional[float] = 55.5,
        fan_rpm: Optional[int] = 2500,
        fan_pwm: Optional[int] = 1,
        throttled: Optional[str] = "0x0",
        volts: Optional[float] = 0.87,
    ) -> Any:
        """Return a context manager that stubs every sysfs/vcgencmd reader."""
        from contextlib import ExitStack

        def _enter() -> ExitStack:
            stack: ExitStack = ExitStack()
            stack.enter_context(
                patch.object(pts, "_read_cpu_temp_c", return_value=cpu_temp),
            )
            stack.enter_context(
                patch.object(pts, "_read_fan_rpm", return_value=fan_rpm),
            )
            stack.enter_context(
                patch.object(pts, "_read_fan_pwm_step", return_value=fan_pwm),
            )
            stack.enter_context(
                patch.object(
                    pts, "_read_loadavg",
                    return_value=(0.11, 0.22, 0.33),
                ),
            )
            stack.enter_context(
                patch.object(pts, "_read_uptime_s", return_value=1234.5),
            )
            stack.enter_context(
                patch.object(
                    pts, "_read_throttled_flags", return_value=throttled,
                ),
            )
            stack.enter_context(
                patch.object(pts, "_read_core_volts", return_value=volts),
            )
            return stack
        return _enter()

    # ---- actual tests ---------------------------------------------------

    def test_publish_subscribe_round_trip(self) -> None:
        """Sensor publishes a ThermalReading; a fresh subscriber receives it."""
        subscriber: _Subscriber = _Subscriber(
            self._host, self._port, [self._thermal_topic],
        )
        subscriber.start()

        sensor: PiThermalSensor = self._make_sensor()
        with self._patch_readers(cpu_temp=55.5, fan_rpm=2500):
            sensor.connect()
            try:
                # Publish one sample explicitly rather than running the
                # background loop — makes the test deterministic.
                reading: ThermalReading = sensor._sample()
                info: mqtt.MQTTMessageInfo = sensor._client.publish(
                    sensor._thermal_topic,
                    reading.to_json(),
                    qos=1,
                    retain=True,
                )
                info.wait_for_publish(timeout=_MESSAGE_WAIT_S)

                payload: Optional[bytes] = subscriber.wait_for(
                    self._thermal_topic,
                )
                self.assertIsNotNone(
                    payload,
                    "no message received within timeout",
                )
                data: dict[str, Any] = json.loads(payload.decode())
                self.assertEqual(data["node_id"], self._node_id)
                self.assertEqual(data["platform"], "pi5")
                self.assertEqual(data["cpu_temp_c"], 55.5)
                self.assertEqual(data["fan_rpm"], 2500)
                self.assertTrue(data["fan_declared_present"])
                self.assertEqual(data["extra"]["throttled_flags"], "0x0")
            finally:
                sensor.stop()
                subscriber.stop()

    def test_retain_delivers_last_reading_to_late_subscriber(self) -> None:
        """Late subscriber joins AFTER publish and still gets the retained value."""
        sensor: PiThermalSensor = self._make_sensor()
        with self._patch_readers(cpu_temp=42.0, fan_rpm=1800):
            sensor.connect()
            try:
                reading: ThermalReading = sensor._sample()
                info: mqtt.MQTTMessageInfo = sensor._client.publish(
                    sensor._thermal_topic,
                    reading.to_json(),
                    qos=1,
                    retain=True,
                )
                info.wait_for_publish(timeout=_MESSAGE_WAIT_S)
            finally:
                sensor.stop()

        # Now — after the sensor is gone — subscribe fresh.  A retained
        # message must be delivered even though nobody is publishing.
        late: _Subscriber = _Subscriber(
            self._host, self._port, [self._thermal_topic],
        )
        late.start()
        try:
            payload: Optional[bytes] = late.wait_for(self._thermal_topic)
            self.assertIsNotNone(
                payload,
                "retained message not delivered to late subscriber",
            )
            data: dict[str, Any] = json.loads(payload.decode())
            self.assertEqual(data["cpu_temp_c"], 42.0)
            self.assertEqual(data["fan_rpm"], 1800)
        finally:
            late.stop()

    def test_capability_announcement_published_on_connect(self) -> None:
        """connect() publishes a retained NodeCapability payload."""
        subscriber: _Subscriber = _Subscriber(
            self._host, self._port, [self._capability_topic],
        )
        subscriber.start()

        sensor: PiThermalSensor = self._make_sensor()
        with self._patch_readers():
            sensor.connect()
            try:
                payload: Optional[bytes] = subscriber.wait_for(
                    self._capability_topic,
                )
                self.assertIsNotNone(
                    payload,
                    "capability announcement not received",
                )
                cap: dict[str, Any] = json.loads(payload.decode())
                self.assertEqual(cap["node_id"], self._node_id)
                self.assertEqual(cap["roles"], ["sensor"])
                self.assertEqual(cap["resources"]["platform"], "pi5")
                self.assertIn("thermal", cap["resources"]["hardware"])
            finally:
                sensor.stop()
                subscriber.stop()

    def test_status_transitions_online_then_offline(self) -> None:
        """Status is 'online' at connect and 'offline' after clean stop()."""
        subscriber: _Subscriber = _Subscriber(
            self._host, self._port, [self._status_topic],
        )
        subscriber.start()

        sensor: PiThermalSensor = self._make_sensor()
        with self._patch_readers():
            sensor.connect()

            online_payload: Optional[bytes] = subscriber.wait_for(
                self._status_topic,
            )
            self.assertIsNotNone(
                online_payload,
                "online status not received",
            )
            self.assertEqual(online_payload.decode(), "online")

            sensor.stop()

            offline_payload: Optional[bytes] = subscriber.wait_for(
                self._status_topic,
            )
            self.assertIsNotNone(
                offline_payload,
                "offline status not received",
            )
            self.assertEqual(offline_payload.decode(), "offline")

        subscriber.stop()


if __name__ == "__main__":
    unittest.main()
