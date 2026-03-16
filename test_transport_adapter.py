"""Tests for distributed.transport_adapter — UDP transport loopback.

Verifies that UdpTransport can publish and subscribe to signals
using the high-level SignalValue interface over localhost loopback.

MqttTransport is tested separately (requires a running broker).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import threading
import time
import unittest
from typing import Union

from distributed.protocol import DTYPE_FLOAT32
from distributed.transport_adapter import UdpTransport

# Test port (offset from the udp_channel tests to avoid conflicts).
TEST_PORT: int = 19430

# Receive timeout.
RECV_TIMEOUT: float = 2.0

# Signal value type.
SignalValue = Union[float, list[float]]


class TestUdpTransportLoopback(unittest.TestCase):
    """Verify UdpTransport publish/subscribe via localhost."""

    def setUp(self) -> None:
        """Create a transport pair: sender → receiver."""
        # Receiver transport: listens on TEST_PORT.
        self._rx: UdpTransport = UdpTransport(listen_port=TEST_PORT)
        # Sender transport: sends to 127.0.0.1:TEST_PORT (no listener).
        self._tx: UdpTransport = UdpTransport(
            targets=[("127.0.0.1", TEST_PORT)],
        )
        self._received: list[tuple[str, SignalValue]] = []
        self._event: threading.Event = threading.Event()

    def tearDown(self) -> None:
        """Shut down both transports."""
        self._rx.stop()
        self._tx.stop()

    def _on_signal(self, name: str, value: SignalValue) -> None:
        """Callback: collect received signals."""
        self._received.append((name, value))
        self._event.set()

    def test_scalar_signal(self) -> None:
        """A scalar float signal round-trips correctly."""
        self._rx.subscribe("test:scalar", self._on_signal)
        self._rx.start()
        self._tx.start()

        self._tx.publish("test:scalar", 0.75, DTYPE_FLOAT32)

        self._event.wait(timeout=RECV_TIMEOUT)
        self.assertEqual(len(self._received), 1)
        name, value = self._received[0]
        self.assertEqual(name, "test:scalar")
        self.assertAlmostEqual(value, 0.75, places=5)

    def test_array_signal(self) -> None:
        """A list[float] signal round-trips correctly."""
        self._rx.subscribe("test:array", self._on_signal)
        self._rx.start()
        self._tx.start()

        bands: list[float] = [0.1, 0.5, 0.9, 0.3]
        self._tx.publish("test:array", bands, DTYPE_FLOAT32)

        self._event.wait(timeout=RECV_TIMEOUT)
        self.assertEqual(len(self._received), 1)
        name, value = self._received[0]
        self.assertEqual(name, "test:array")
        self.assertIsInstance(value, list)
        self.assertEqual(len(value), 4)
        self.assertAlmostEqual(value[0], 0.1, places=5)

    def test_unsubscribed_signal_ignored(self) -> None:
        """Signals not subscribed to are not delivered."""
        self._rx.subscribe("test:wanted", self._on_signal)
        self._rx.start()
        self._tx.start()

        # Send to a different signal name.
        self._tx.publish("test:unwanted", 1.0, DTYPE_FLOAT32)
        time.sleep(0.3)
        self.assertEqual(len(self._received), 0)

    def test_unsubscribe(self) -> None:
        """Unsubscribing stops delivery."""
        self._rx.subscribe("test:unsub", self._on_signal)
        self._rx.start()
        self._tx.start()

        self._tx.publish("test:unsub", 1.0, DTYPE_FLOAT32)
        self._event.wait(timeout=RECV_TIMEOUT)
        self.assertEqual(len(self._received), 1)

        # Unsubscribe and send again.
        self._rx.unsubscribe("test:unsub")
        self._event.clear()
        self._tx.publish("test:unsub", 2.0, DTYPE_FLOAT32)
        time.sleep(0.3)
        self.assertEqual(len(self._received), 1)  # Still 1.

    def test_add_remove_target(self) -> None:
        """Dynamic target management works."""
        self._rx.subscribe("test:dynamic", self._on_signal)
        self._rx.start()
        self._tx.start()

        # No targets initially.
        tx_no_target: UdpTransport = UdpTransport()
        tx_no_target.start()

        tx_no_target.publish("test:dynamic", 1.0, DTYPE_FLOAT32)
        time.sleep(0.3)
        self.assertEqual(len(self._received), 0)

        # Add target and send.
        tx_no_target.add_target("127.0.0.1", TEST_PORT)
        tx_no_target.publish("test:dynamic", 2.0, DTYPE_FLOAT32)
        self._event.wait(timeout=RECV_TIMEOUT)
        self.assertEqual(len(self._received), 1)

        tx_no_target.stop()

    def test_send_only_transport(self) -> None:
        """Transport with listen_port=0 works as send-only."""
        tx_only: UdpTransport = UdpTransport(
            targets=[("127.0.0.1", TEST_PORT)],
        )
        self._rx.subscribe("test:sendonly", self._on_signal)
        self._rx.start()
        tx_only.start()

        tx_only.publish("test:sendonly", 0.42, DTYPE_FLOAT32)
        self._event.wait(timeout=RECV_TIMEOUT)
        self.assertEqual(len(self._received), 1)
        self.assertAlmostEqual(self._received[0][1], 0.42, places=5)

        tx_only.stop()


class TestUdpTransportLifecycle(unittest.TestCase):
    """Verify clean start/stop behavior."""

    def test_stop_without_start(self) -> None:
        """Stopping a never-started transport should not raise."""
        transport: UdpTransport = UdpTransport(listen_port=TEST_PORT + 10)
        transport.stop()

    def test_publish_before_start(self) -> None:
        """Publishing before start should be a silent no-op."""
        transport: UdpTransport = UdpTransport(
            targets=[("127.0.0.1", TEST_PORT + 11)],
        )
        # Should not raise.
        transport.publish("test:noop", 1.0, DTYPE_FLOAT32)


if __name__ == "__main__":
    unittest.main()
