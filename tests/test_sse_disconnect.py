"""Tests for SSE client disconnect detection via select().

Verifies that the SSE color stream handler exits promptly when the
client closes its end of the connection — no timeout guessing.

Uses real sockets to prove the mechanism works end-to-end.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import select
import socket
import threading
import time
import unittest


# ---------------------------------------------------------------------------
# Core mechanism test — does select() detect FIN on a closed socket?
# ---------------------------------------------------------------------------

class TestSelectDetectsFIN(unittest.TestCase):
    """Prove that select() on a TCP socket returns readable when the
    peer sends FIN (closes their end).  This is the mechanism the SSE
    handler relies on instead of timeouts."""

    def test_select_detects_client_close(self) -> None:
        """Server-side select() sees readability immediately after
        the client closes its socket."""
        server_sock: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM,
        )
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", 0))
        port: int = server_sock.getsockname()[1]
        server_sock.listen(1)

        # Client connects.
        client: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM,
        )
        client.connect(("127.0.0.1", port))
        conn, _ = server_sock.accept()

        try:
            # Before close: not readable (no data, no FIN).
            readable, _, _ = select.select([conn], [], [], 0)
            self.assertEqual(len(readable), 0,
                             "Socket should NOT be readable before client close")

            # Client closes its end → sends FIN.
            client.close()

            # Brief pause for FIN to propagate through loopback.
            time.sleep(0.05)

            # After close: readable (FIN arrived).
            readable, _, _ = select.select([conn], [], [], 0)
            self.assertEqual(len(readable), 1,
                             "Socket MUST be readable after client close (FIN)")
        finally:
            conn.close()
            server_sock.close()

    def test_select_not_triggered_by_active_client(self) -> None:
        """A connected, silent client does NOT trigger readability."""
        server_sock: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM,
        )
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", 0))
        port: int = server_sock.getsockname()[1]
        server_sock.listen(1)

        client: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM,
        )
        client.connect(("127.0.0.1", port))
        conn, _ = server_sock.accept()

        try:
            # Wait a bit, then check — should NOT be readable.
            time.sleep(0.1)
            readable, _, _ = select.select([conn], [], [], 0)
            self.assertEqual(len(readable), 0,
                             "Active client should not trigger readability")
        finally:
            client.close()
            conn.close()
            server_sock.close()


# ---------------------------------------------------------------------------
# Simulate SSE loop exit on disconnect
# ---------------------------------------------------------------------------

class TestSSELoopExitsOnDisconnect(unittest.TestCase):
    """Simulate the SSE write loop and verify it exits when the
    client disconnects — no timeout, no guessing."""

    def test_loop_exits_within_one_poll_cycle(self) -> None:
        """SSE loop detects disconnect and exits within one poll interval."""
        # Polling interval matching production SSE_POLL_INTERVAL.
        POLL_INTERVAL: float = 0.25

        server_sock: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM,
        )
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", 0))
        port: int = server_sock.getsockname()[1]
        server_sock.listen(1)

        client: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM,
        )
        client.connect(("127.0.0.1", port))
        conn, _ = server_sock.accept()

        loop_iterations: list[int] = [0]
        loop_exited: threading.Event = threading.Event()

        def sse_loop() -> None:
            """Mimics the production SSE loop in device.py."""
            while True:
                readable, _, _ = select.select([conn], [], [], 0)
                if readable:
                    break
                loop_iterations[0] += 1
                time.sleep(POLL_INTERVAL)
            loop_exited.set()

        t: threading.Thread = threading.Thread(target=sse_loop, daemon=True)
        t.start()

        # Let loop run a few iterations.
        time.sleep(0.6)
        self.assertGreater(loop_iterations[0], 0, "Loop should have iterated")
        self.assertFalse(loop_exited.is_set(), "Loop should still be running")

        # Client disconnects.
        client.close()

        # Loop should exit within one poll interval + margin.
        exited: bool = loop_exited.wait(timeout=POLL_INTERVAL + 0.5)
        self.assertTrue(exited, "SSE loop did not exit after client disconnect")

        conn.close()
        server_sock.close()

    def test_loop_survives_active_client(self) -> None:
        """SSE loop keeps running while client is connected."""
        POLL_INTERVAL: float = 0.05

        server_sock: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM,
        )
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", 0))
        port: int = server_sock.getsockname()[1]
        server_sock.listen(1)

        client: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM,
        )
        client.connect(("127.0.0.1", port))
        conn, _ = server_sock.accept()

        loop_exited: threading.Event = threading.Event()
        stop: threading.Event = threading.Event()

        def sse_loop() -> None:
            while not stop.is_set():
                readable, _, _ = select.select([conn], [], [], 0)
                if readable:
                    loop_exited.set()
                    return
                time.sleep(POLL_INTERVAL)

        t: threading.Thread = threading.Thread(target=sse_loop, daemon=True)
        t.start()

        # Run for 0.5s — loop should NOT exit.
        time.sleep(0.5)
        self.assertFalse(loop_exited.is_set(),
                         "Loop must NOT exit while client is connected")

        stop.set()
        client.close()
        conn.close()
        server_sock.close()


if __name__ == "__main__":
    unittest.main()
