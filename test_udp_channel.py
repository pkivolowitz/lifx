"""Tests for distributed.udp_channel — UDP sender/receiver loopback.

Uses localhost loopback to verify that frames sent by UdpSender
are received and decoded correctly by UdpReceiver.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import threading
import time
import unittest

from distributed.protocol import (
    DTYPE_FLOAT32, DTYPE_INT16_PCM,
    MSG_SIGNAL_DATA, MSG_HEARTBEAT,
    SignalFrame,
    pack_float32_array, unpack_float32_array,
)
from distributed.udp_channel import UdpSender, UdpReceiver, _is_multicast

# Test port — chosen to avoid conflicts with real services.
TEST_PORT: int = 19420

# Maximum time to wait for a frame callback (seconds).
RECV_TIMEOUT: float = 2.0


class TestIsMulticast(unittest.TestCase):
    """Verify multicast address range detection."""

    def test_multicast_addresses(self) -> None:
        """Known multicast addresses should return True."""
        self.assertTrue(_is_multicast("224.0.0.1"))
        self.assertTrue(_is_multicast("239.255.255.255"))
        self.assertTrue(_is_multicast("239.0.42.1"))

    def test_unicast_addresses(self) -> None:
        """Regular unicast addresses should return False."""
        self.assertFalse(_is_multicast("10.0.0.48"))
        self.assertFalse(_is_multicast("192.168.1.1"))
        self.assertFalse(_is_multicast("127.0.0.1"))

    def test_invalid_address(self) -> None:
        """Malformed addresses should return False (not raise)."""
        self.assertFalse(_is_multicast("not_an_ip"))
        self.assertFalse(_is_multicast(""))


class TestUdpLoopback(unittest.TestCase):
    """Loopback tests: sender → receiver on localhost."""

    def setUp(self) -> None:
        """Create sender and receiver."""
        self._receiver: UdpReceiver = UdpReceiver(
            port=TEST_PORT, bind_ip="127.0.0.1",
        )
        self._sender: UdpSender = UdpSender(
            targets=[("127.0.0.1", TEST_PORT)],
        )
        self._received: list[SignalFrame] = []
        self._event: threading.Event = threading.Event()

    def tearDown(self) -> None:
        """Shut down sender and receiver."""
        self._receiver.stop()
        self._sender.close()

    def _on_frame(self, frame: SignalFrame,
                  addr: tuple[str, int]) -> None:
        """Callback: collect received frames."""
        self._received.append(frame)
        self._event.set()

    def test_single_frame(self) -> None:
        """A single frame makes the round trip via loopback."""
        self._receiver.add_callback(self._on_frame)
        self._receiver.start()

        payload: bytes = pack_float32_array([0.1, 0.2, 0.3, 0.4])
        sent: int = self._sender.send(
            "test:audio:bands", payload, DTYPE_FLOAT32,
        )
        self.assertEqual(sent, 1)

        self._event.wait(timeout=RECV_TIMEOUT)
        self.assertEqual(len(self._received), 1)

        frame: SignalFrame = self._received[0]
        self.assertEqual(frame.name, "test:audio:bands")
        self.assertEqual(frame.dtype, DTYPE_FLOAT32)
        values: list[float] = unpack_float32_array(frame.payload)
        self.assertEqual(len(values), 4)
        self.assertAlmostEqual(values[0], 0.1, places=5)

    def test_multiple_frames_ordered(self) -> None:
        """Multiple frames arrive with monotonic sequence numbers."""
        received_seqs: list[int] = []
        barrier: threading.Event = threading.Event()

        def collector(frame: SignalFrame,
                      addr: tuple[str, int]) -> None:
            received_seqs.append(frame.sequence)
            if len(received_seqs) >= 5:
                barrier.set()

        self._receiver.add_callback(collector)
        self._receiver.start()

        for _ in range(5):
            self._sender.send("test:seq", b"x", DTYPE_FLOAT32)
            time.sleep(0.01)  # Small gap to avoid burst drops.

        barrier.wait(timeout=RECV_TIMEOUT)
        self.assertEqual(len(received_seqs), 5)
        # Sequences should be monotonically increasing.
        for i in range(1, len(received_seqs)):
            self.assertGreater(received_seqs[i], received_seqs[i - 1])

    def test_heartbeat_message_type(self) -> None:
        """Heartbeat messages are received with correct msg_type."""
        self._receiver.add_callback(self._on_frame)
        self._receiver.start()

        self._sender.send(
            "node:health", b"", DTYPE_FLOAT32, MSG_HEARTBEAT,
        )

        self._event.wait(timeout=RECV_TIMEOUT)
        self.assertEqual(len(self._received), 1)
        self.assertEqual(self._received[0].msg_type, MSG_HEARTBEAT)

    def test_add_remove_target(self) -> None:
        """Adding and removing targets works correctly."""
        self._sender.add_target("10.0.0.99", 9999)
        # Should now have 2 targets.
        self._receiver.add_callback(self._on_frame)
        self._receiver.start()

        # Send should attempt both targets (one will fail — 10.0.0.99
        # doesn't exist, but UDP send doesn't fail on unreachable).
        sent: int = self._sender.send("test:target", b"x", DTYPE_FLOAT32)
        self.assertEqual(sent, 2)

        self._sender.remove_target("10.0.0.99", 9999)
        sent = self._sender.send("test:target", b"y", DTYPE_FLOAT32)
        self.assertEqual(sent, 1)

    def test_remove_callback(self) -> None:
        """Removing a callback stops delivery to it."""
        self._receiver.add_callback(self._on_frame)
        self._receiver.start()

        # First send — callback active.
        self._sender.send("test:cb", b"x", DTYPE_FLOAT32)
        self._event.wait(timeout=RECV_TIMEOUT)
        self.assertEqual(len(self._received), 1)

        # Remove callback and send again.
        self._receiver.remove_callback(self._on_frame)
        self._event.clear()
        self._sender.send("test:cb", b"y", DTYPE_FLOAT32)
        time.sleep(0.2)  # Give it time to arrive (it shouldn't).
        self.assertEqual(len(self._received), 1)  # Still 1.

    def test_pcm_payload(self) -> None:
        """Realistic PCM audio payload survives the round trip."""
        self._receiver.add_callback(self._on_frame)
        self._receiver.start()

        # 3200 bytes = 1600 samples of 16-bit PCM.
        payload: bytes = b"\x00\x80" * 1600
        self._sender.send("mic:audio:raw", payload, DTYPE_INT16_PCM)

        self._event.wait(timeout=RECV_TIMEOUT)
        self.assertEqual(len(self._received), 1)
        self.assertEqual(len(self._received[0].payload), 3200)
        self.assertEqual(self._received[0].dtype, DTYPE_INT16_PCM)


class TestUdpReceiverStop(unittest.TestCase):
    """Verify clean shutdown."""

    def test_stop_without_start(self) -> None:
        """Stopping a never-started receiver should not raise."""
        receiver: UdpReceiver = UdpReceiver(port=TEST_PORT + 1)
        receiver.stop()  # Should be a no-op.

    def test_double_start(self) -> None:
        """Starting twice should be idempotent."""
        receiver: UdpReceiver = UdpReceiver(
            port=TEST_PORT + 2, bind_ip="127.0.0.1",
        )
        try:
            receiver.start()
            receiver.start()  # Second start is a no-op.
        finally:
            receiver.stop()


if __name__ == "__main__":
    unittest.main()
