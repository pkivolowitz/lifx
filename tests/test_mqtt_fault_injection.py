"""Fault-injection tests for MqttAdapterBase.

These tests are the flight instructor putting the student in a stall.
A minimal ``FaultBroker`` speaks just enough MQTT wire protocol to
satisfy paho, then deliberately breaks: drops connections, hangs,
goes silent, refuses connections.  The adapter under test is real
code (not mocked) running against this evil broker.

Each test verifies the adapter's *observable behavior* under failure:
does it detect the fault, log it, reconnect, and resume?

The MQTT wire protocol is simple enough to implement the minimum
viable subset in raw sockets:

    CONNECT  → CONNACK
    SUBSCRIBE → SUBACK
    PINGREQ  → PINGRESP
    PUBLISH  (broker → client)

Everything else is ignored or triggers a controlled fault.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.

__version__: str = "1.0"

import logging
import os
import selectors
import socket
import struct
import threading
import time
import unittest
from typing import Any, Optional
from unittest.mock import patch

from adapters.adapter_base import (
    MqttAdapterBase,
    WATCHDOG_SILENCE_THRESHOLD,
    WATCHDOG_POLL_INTERVAL,
    MQTT_KEEPALIVE,
    _HAS_PAHO,
)

logger: logging.Logger = logging.getLogger("glowup.test.fault")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Shortened thresholds for testing — we don't want 120s waits.
TEST_SILENCE_THRESHOLD: float = 4.0
TEST_WATCHDOG_POLL: float = 1.0
TEST_KEEPALIVE: int = 5

# MQTT packet type nibbles (upper 4 bits of byte 0).
MQTT_CONNECT: int = 0x10
MQTT_CONNACK: int = 0x20
MQTT_PUBLISH: int = 0x30
MQTT_SUBSCRIBE: int = 0x80
MQTT_SUBACK: int = 0x90
MQTT_PINGREQ: int = 0xC0
MQTT_PINGRESP: int = 0xD0
MQTT_DISCONNECT: int = 0xE0

# How long to wait for adapter to react to faults.
SETTLE_TIME: float = 3.0

# Maximum time to wait for reconnection after a fault.
# Paho's internal reconnect has backoff — give it room.
RECONNECT_WAIT: float = 20.0


# ---------------------------------------------------------------------------
# FaultBroker — evil MQTT broker for fault injection
# ---------------------------------------------------------------------------

class FaultBroker:
    """Minimal MQTT broker that can be told to misbehave.

    Runs a TCP server on localhost with a random port.  Speaks just
    enough MQTT to satisfy paho's connect/subscribe handshake, then
    executes fault scenarios on command.

    Thread-safe: the broker runs in its own thread.  Control methods
    (``inject_*``) are called from the test thread.

    Attributes:
        port:            The TCP port assigned by the OS.
        connections:     Count of accepted connections (including reconnects).
        messages_sent:   Count of PUBLISH packets sent to clients.
    """

    def __init__(self) -> None:
        """Create the broker socket and bind to a random port."""
        self._server: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM,
        )
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self._server.settimeout(1.0)
        self.port: int = self._server.getsockname()[1]

        self._thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._client_sock: Optional[socket.socket] = None
        self._lock: threading.Lock = threading.Lock()

        # Fault injection flags.
        self._drop_after_subscribe: bool = False
        self._hang_after_subscribe: bool = False
        self._stop_publishing: bool = False
        self._reject_connections: bool = False
        self._respond_to_pings: bool = True

        # Observability.
        self.connections: int = 0
        self.messages_sent: int = 0

        # Publish loop control.
        self._publish_topic: str = "test/sensor"
        self._publish_payload: bytes = b'{"value": 42}'
        self._publish_interval: float = 0.5

    def start(self) -> None:
        """Start the broker thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._serve_loop,
            daemon=True,
            name="fault-broker",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the broker and close all sockets."""
        self._running = False
        with self._lock:
            if self._client_sock:
                try:
                    self._client_sock.close()
                except Exception:
                    pass
                self._client_sock = None
        try:
            self._server.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=5.0)

    # --- Fault injection controls -----------------------------------------

    def inject_drop(self) -> None:
        """Drop the current client connection immediately."""
        with self._lock:
            if self._client_sock:
                try:
                    self._client_sock.close()
                except Exception:
                    pass
                self._client_sock = None

    def inject_silence(self) -> None:
        """Stop publishing messages but keep the connection alive."""
        self._stop_publishing = True

    def inject_hang(self) -> None:
        """Stop responding to everything (pings, reads) but keep socket open."""
        self._respond_to_pings = False
        self._stop_publishing = True

    def resume_publishing(self) -> None:
        """Resume normal publishing after a silence injection."""
        self._stop_publishing = False

    def set_reject_connections(self, reject: bool) -> None:
        """Reject new connections immediately after accept."""
        self._reject_connections = reject

    # --- Wire protocol ----------------------------------------------------

    def _read_packet(self, sock: socket.socket) -> Optional[tuple[int, bytes]]:
        """Read one MQTT packet.  Returns (packet_type, payload) or None."""
        try:
            header = sock.recv(1)
            if not header:
                return None
            packet_type: int = header[0] & 0xF0

            # Decode remaining length (variable-length encoding).
            multiplier: int = 1
            remaining: int = 0
            for _ in range(4):
                b = sock.recv(1)
                if not b:
                    return None
                remaining += (b[0] & 0x7F) * multiplier
                if (b[0] & 0x80) == 0:
                    break
                multiplier *= 128

            payload: bytes = b""
            while len(payload) < remaining:
                chunk = sock.recv(remaining - len(payload))
                if not chunk:
                    return None
                payload += chunk

            return (packet_type, payload)
        except (socket.timeout, OSError):
            return None

    def _send_connack(self, sock: socket.socket, rc: int = 0) -> None:
        """Send a CONNACK packet."""
        # Fixed header: type 0x20, remaining length 2.
        # Variable header: session present = 0, return code.
        sock.sendall(bytes([MQTT_CONNACK, 2, 0, rc]))

    def _send_suback(self, sock: socket.socket, packet_id: int) -> None:
        """Send a SUBACK for the given packet ID."""
        # Fixed header: type 0x90, remaining length 3.
        # Variable header: packet ID (2 bytes) + granted QoS 0.
        sock.sendall(bytes([
            MQTT_SUBACK, 3,
            (packet_id >> 8) & 0xFF, packet_id & 0xFF,
            0,
        ]))

    def _send_pingresp(self, sock: socket.socket) -> None:
        """Send a PINGRESP."""
        sock.sendall(bytes([MQTT_PINGRESP, 0]))

    def _send_publish(self, sock: socket.socket,
                      topic: str, payload: bytes) -> None:
        """Send a PUBLISH (QoS 0) packet."""
        topic_bytes: bytes = topic.encode("utf-8")
        # Variable header: topic length (2 bytes) + topic.
        var_header: bytes = struct.pack(
            "!H", len(topic_bytes),
        ) + topic_bytes
        remaining: bytes = var_header + payload
        # Encode remaining length.
        length_bytes: bytes = self._encode_remaining_length(len(remaining))
        sock.sendall(bytes([MQTT_PUBLISH]) + length_bytes + remaining)

    @staticmethod
    def _encode_remaining_length(length: int) -> bytes:
        """Encode MQTT variable-length integer."""
        result: bytearray = bytearray()
        while True:
            byte: int = length % 128
            length = length // 128
            if length > 0:
                byte |= 0x80
            result.append(byte)
            if length == 0:
                break
        return bytes(result)

    # --- Main serve loop --------------------------------------------------

    def _serve_loop(self) -> None:
        """Accept connections and handle them until stopped.

        Each client is handled in its own daemon thread so the accept
        loop is never blocked — critical for testing reconnect.
        """
        while self._running:
            try:
                client, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            self.connections += 1

            if self._reject_connections:
                client.close()
                continue

            # Close previous client if still open.
            with self._lock:
                if self._client_sock:
                    try:
                        self._client_sock.close()
                    except Exception:
                        pass
                self._client_sock = client

            client.settimeout(0.5)
            t = threading.Thread(
                target=self._handle_client,
                args=(client,),
                daemon=True,
                name="fault-broker-client",
            )
            t.start()

    def _handle_client(self, sock: socket.socket) -> None:
        """Handle one MQTT client connection."""
        # Wait for CONNECT.
        packet = self._read_packet(sock)
        if packet is None or packet[0] != MQTT_CONNECT:
            sock.close()
            return

        self._send_connack(sock)

        # Now handle packets and optionally publish.
        subscribed: bool = False
        last_publish: float = 0.0

        while self._running:
            # Check for incoming packets (non-blocking-ish).
            packet = self._read_packet(sock)
            if packet is not None:
                ptype: int = packet[0]
                if ptype == MQTT_SUBSCRIBE:
                    # Extract packet ID from first 2 bytes of payload.
                    pid: int = struct.unpack("!H", packet[1][:2])[0]

                    if self._drop_after_subscribe:
                        self._send_suback(sock, pid)
                        time.sleep(0.1)
                        sock.close()
                        return

                    if self._hang_after_subscribe:
                        self._send_suback(sock, pid)
                        # Just sit here doing nothing until stopped.
                        while self._running:
                            time.sleep(0.5)
                        return

                    self._send_suback(sock, pid)
                    subscribed = True

                elif ptype == MQTT_PINGREQ:
                    if self._respond_to_pings:
                        try:
                            self._send_pingresp(sock)
                        except OSError:
                            return

                elif ptype == MQTT_DISCONNECT:
                    sock.close()
                    return

            # Publish messages if subscribed and not silenced.
            if (subscribed and not self._stop_publishing
                    and time.monotonic() - last_publish
                    >= self._publish_interval):
                try:
                    self._send_publish(
                        sock, self._publish_topic, self._publish_payload,
                    )
                    self.messages_sent += 1
                    last_publish = time.monotonic()
                except OSError:
                    return

            # Check if socket was closed externally (inject_drop)
            # or replaced by a new connection.
            with self._lock:
                if self._client_sock is not sock:
                    return


# ---------------------------------------------------------------------------
# Test adapter — real MqttAdapterBase, no mocks
# ---------------------------------------------------------------------------

class FaultTestAdapter(MqttAdapterBase):
    """Concrete adapter for fault injection tests.

    Records messages and state transitions for assertions.
    """

    def __init__(self, broker: str, port: int) -> None:
        """Initialize with the fault broker's address."""
        super().__init__(
            broker=broker,
            port=port,
            subscribe_prefix="test",
            client_id_prefix="fault-test",
        )
        self.messages: list[tuple[str, bytes]] = []
        self.message_lock: threading.Lock = threading.Lock()

    def _handle_message(self, topic: str, payload: bytes) -> None:
        """Record received messages thread-safely."""
        with self.message_lock:
            self.messages.append((topic, payload))

    def message_count(self) -> int:
        """Return current message count."""
        with self.message_lock:
            return len(self.messages)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

@unittest.skipUnless(_HAS_PAHO, "paho-mqtt not installed")
class TestBrokerDropsConnection(unittest.TestCase):
    """Broker accepts, subscribes, then drops the TCP connection."""

    def test_adapter_detects_drop_and_reconnects(self) -> None:
        """Adapter detects the drop via on_disconnect and reconnects."""
        broker = FaultBroker()
        broker.start()
        try:
            adapter = FaultTestAdapter("127.0.0.1", broker.port)
            with patch("adapters.adapter_base.MQTT_KEEPALIVE", TEST_KEEPALIVE):
                adapter.start()

            # Wait for messages to flow.
            deadline: float = time.monotonic() + 5.0
            while adapter.message_count() < 3 and time.monotonic() < deadline:
                time.sleep(0.2)
            self.assertGreaterEqual(adapter.message_count(), 3)
            self.assertTrue(adapter._connected)

            # Drop the connection.
            initial_connections: int = broker.connections
            broker.inject_drop()

            # Wait for adapter to detect disconnect and reconnect.
            deadline = time.monotonic() + RECONNECT_WAIT
            while broker.connections <= initial_connections and time.monotonic() < deadline:
                time.sleep(0.3)

            self.assertGreater(
                broker.connections, initial_connections,
                "Adapter did not reconnect after broker dropped connection",
            )

            # Messages should resume after reconnect.
            count_after_reconnect: int = adapter.message_count()
            time.sleep(2.0)
            self.assertGreater(
                adapter.message_count(), count_after_reconnect,
                "Messages did not resume after reconnect",
            )
        finally:
            adapter.stop()
            broker.stop()


@unittest.skipUnless(_HAS_PAHO, "paho-mqtt not installed")
class TestBrokerGoesCompletelyDead(unittest.TestCase):
    """Broker process dies — port becomes unreachable."""

    def test_adapter_survives_broker_death(self) -> None:
        """Adapter stays running and reconnects when broker returns."""
        broker = FaultBroker()
        broker.start()
        try:
            adapter = FaultTestAdapter("127.0.0.1", broker.port)
            with patch("adapters.adapter_base.MQTT_KEEPALIVE", TEST_KEEPALIVE):
                adapter.start()

            # Wait for messages.
            deadline: float = time.monotonic() + 5.0
            while adapter.message_count() < 2 and time.monotonic() < deadline:
                time.sleep(0.2)
            self.assertGreaterEqual(adapter.message_count(), 2)

            saved_port: int = broker.port
            broker.stop()
            time.sleep(SETTLE_TIME)

            # Adapter should still be running (not crashed).
            self.assertTrue(adapter._running)

            # Bring broker back on the same port.
            broker2 = FaultBroker.__new__(FaultBroker)
            broker2.__init__()
            # Rebind to same port.
            broker2._server.close()
            broker2._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            broker2._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            broker2._server.bind(("127.0.0.1", saved_port))
            broker2._server.listen(4)
            broker2._server.settimeout(1.0)
            broker2.port = saved_port
            broker2.start()

            try:
                # Wait for reconnect and message resumption.
                count_before: int = adapter.message_count()
                deadline = time.monotonic() + RECONNECT_WAIT
                while (adapter.message_count() <= count_before
                       and time.monotonic() < deadline):
                    time.sleep(0.3)
                self.assertGreater(
                    adapter.message_count(), count_before,
                    "Messages did not resume after broker returned",
                )
            finally:
                broker2.stop()
        finally:
            adapter.stop()


@unittest.skipUnless(_HAS_PAHO, "paho-mqtt not installed")
class TestBrokerHangsHalfOpen(unittest.TestCase):
    """Broker stops sending data but keeps socket open — half-open socket.

    This is the exact failure mode that went undetected for 24+ hours
    in production.  The watchdog must detect the silence and force a
    reconnect.
    """

    def test_watchdog_detects_silence_and_reconnects(self) -> None:
        """Watchdog fires after silence threshold, adapter reconnects."""
        broker = FaultBroker()
        broker.start()
        try:
            adapter = FaultTestAdapter("127.0.0.1", broker.port)
            with (
                patch("adapters.adapter_base.MQTT_KEEPALIVE", TEST_KEEPALIVE),
                patch("adapters.adapter_base.WATCHDOG_SILENCE_THRESHOLD",
                      TEST_SILENCE_THRESHOLD),
                patch("adapters.adapter_base.WATCHDOG_POLL_INTERVAL",
                      TEST_WATCHDOG_POLL),
            ):
                adapter.start()

                # Wait for messages to flow.
                deadline: float = time.monotonic() + 5.0
                while adapter.message_count() < 3 and time.monotonic() < deadline:
                    time.sleep(0.2)
                self.assertGreaterEqual(adapter.message_count(), 3)

                # Inject silence — broker stops publishing but keeps
                # socket open and responds to pings.
                broker.inject_silence()
                initial_connections: int = broker.connections

                # Wait for watchdog to detect silence and force reconnect.
                with self.assertLogs("glowup.adapter_base", level="WARNING") as cm:
                    deadline = time.monotonic() + TEST_SILENCE_THRESHOLD + 8.0
                    while (broker.connections <= initial_connections
                           and time.monotonic() < deadline):
                        time.sleep(0.5)

                self.assertTrue(
                    any("forcing reconnect" in line for line in cm.output),
                    "Watchdog did not log forced reconnect",
                )
        finally:
            adapter.stop()
            broker.stop()


@unittest.skipUnless(_HAS_PAHO, "paho-mqtt not installed")
class TestBrokerRejectsConnection(unittest.TestCase):
    """Broker is running but refuses MQTT connections."""

    def test_adapter_survives_rejection(self) -> None:
        """Adapter does not crash when broker sends bad CONNACK."""
        broker = FaultBroker()
        broker.set_reject_connections(True)
        broker.start()
        try:
            adapter = FaultTestAdapter("127.0.0.1", broker.port)
            with patch("adapters.adapter_base.MQTT_KEEPALIVE", TEST_KEEPALIVE):
                adapter.start()

            # Give it a few seconds — adapter should stay running
            # despite repeated rejections.
            time.sleep(3.0)
            self.assertTrue(adapter._running)
            self.assertFalse(adapter._connected)

            # Now accept connections and verify recovery.
            broker.set_reject_connections(False)
            deadline: float = time.monotonic() + RECONNECT_WAIT
            while adapter.message_count() < 1 and time.monotonic() < deadline:
                time.sleep(0.3)
            self.assertGreater(
                adapter.message_count(), 0,
                "Adapter did not recover after broker started accepting",
            )
        finally:
            adapter.stop()
            broker.stop()


@unittest.skipUnless(_HAS_PAHO, "paho-mqtt not installed")
class TestMessagesSurviveTransientDrop(unittest.TestCase):
    """Messages resume after a brief connection interruption."""

    def test_message_flow_resumes(self) -> None:
        """Drop connection, let it reconnect, verify messages resume."""
        broker = FaultBroker()
        broker._publish_interval = 0.3
        broker.start()
        try:
            adapter = FaultTestAdapter("127.0.0.1", broker.port)
            with patch("adapters.adapter_base.MQTT_KEEPALIVE", TEST_KEEPALIVE):
                adapter.start()

            # Accumulate some messages.
            deadline: float = time.monotonic() + 5.0
            while adapter.message_count() < 5 and time.monotonic() < deadline:
                time.sleep(0.2)
            pre_drop: int = adapter.message_count()
            self.assertGreaterEqual(pre_drop, 5)

            # Drop and wait for reconnect.
            broker.inject_drop()
            time.sleep(3.0)

            # Messages should be flowing again.
            post_reconnect: int = adapter.message_count()
            time.sleep(2.0)
            final: int = adapter.message_count()
            self.assertGreater(
                final, post_reconnect,
                "Message flow did not resume after transient drop",
            )
        finally:
            adapter.stop()
            broker.stop()


@unittest.skipUnless(_HAS_PAHO, "paho-mqtt not installed")
class TestSilenceThenResume(unittest.TestCase):
    """Broker goes silent, then resumes publishing before watchdog fires."""

    def test_no_false_alarm_on_brief_silence(self) -> None:
        """Brief silence shorter than threshold does not trigger watchdog."""
        broker = FaultBroker()
        broker._publish_interval = 0.3
        broker.start()
        try:
            adapter = FaultTestAdapter("127.0.0.1", broker.port)
            with (
                patch("adapters.adapter_base.MQTT_KEEPALIVE", TEST_KEEPALIVE),
                patch("adapters.adapter_base.WATCHDOG_SILENCE_THRESHOLD",
                      TEST_SILENCE_THRESHOLD),
                patch("adapters.adapter_base.WATCHDOG_POLL_INTERVAL",
                      TEST_WATCHDOG_POLL),
            ):
                adapter.start()

                # Wait for messages.
                deadline: float = time.monotonic() + 5.0
                while adapter.message_count() < 3 and time.monotonic() < deadline:
                    time.sleep(0.2)

                initial_connections: int = broker.connections

                # Go silent for LESS than the threshold.
                broker.inject_silence()
                time.sleep(TEST_SILENCE_THRESHOLD * 0.4)

                # Resume before watchdog fires.
                broker.resume_publishing()
                time.sleep(SETTLE_TIME)

                # Should NOT have reconnected.
                self.assertEqual(
                    broker.connections, initial_connections,
                    "Watchdog falsely triggered on brief silence",
                )
        finally:
            adapter.stop()
            broker.stop()


if __name__ == "__main__":
    unittest.main()
