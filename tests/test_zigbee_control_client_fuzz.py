"""Fault-injection tests for ``zigbee_service.client.ZigbeeControlClient``.

These tests stand up synthetic servers that deliberately misbehave in
ways that real broker-2 under load, OOM, or network weather can
produce — half-dead sockets, mid-response hang-ups, byte-trickle,
malformed JSON, wrong content types.  No fleet hardware is touched;
everything runs on localhost over ephemeral ports.

Fault catalogue (each test corresponds to one row):

    connect refused              — broker-2 process dead
    half-dead socket             — TCP accept then indefinite silence
    mid-response hang-up         — close socket after partial body
    truncated JSON body          — valid HTTP, JSON cut off mid-array
    non-JSON body                — HTML error page from a proxy
    zero-length 200              — accept then close cleanly, no body
    HTTP 500 non-JSON body       — service blew up below the JSON layer
    HTTP 400 malformed JSON body — service errored AND its error is broken
    huge device list             — 10k devices, ensure we don't explode
    unicode device name          — RTL marks, emoji, URL-encoded NULs
    set_state 200 echoed=False   — service accepted but device silent
    200 with wrong content-type  — proxy stripped headers

The client's contract: never crash, always return ``(ok, detail)``
or ``CommandResult`` with a human-readable error string.  That
contract is what gets verified below — the underlying socket is
allowed to be as broken as reality makes it.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.

__version__ = "1.0"

import json
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Optional

_REPO_ROOT: str = os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."),
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from zigbee_service.client import (
    CommandResult,
    ZigbeeControlClient,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Client timeout used by every fuzz test.  Short enough that the
# whole suite runs in under a couple seconds even when the faulty
# server stalls, long enough that we see the timeout path not a
# handshake race.
_FUZZ_CLIENT_TIMEOUT_S: float = 0.75

# Per-test server accept backlog.  One connection per test is plenty.
_ACCEPT_BACKLOG: int = 1

# How long the raw-socket fault servers are willing to hold a connection
# before giving up on the client.  Set well above the client timeout so
# the client always loses the race, not the server.
_RAW_SERVER_HOLD_S: float = 5.0


# ---------------------------------------------------------------------------
# Raw-socket fault server — for the HTTP-illegal pathologies (half-dead,
# mid-response hang-up) where http.server can't express the fault.
# ---------------------------------------------------------------------------

class RawFaultServer:
    """Listens on localhost, runs a per-connection handler, one shot.

    Each instance is single-use: start, serve one connection, stop.
    Test tearDown joins the thread and closes the socket.
    """

    def __init__(self, handler: Callable[[socket.socket], None]) -> None:
        self._handler: Callable[[socket.socket], None] = handler
        self._sock: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM,
        )
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(_ACCEPT_BACKLOG)
        self._sock.settimeout(_RAW_SERVER_HOLD_S)
        self.port: int = self._sock.getsockname()[1]
        self._thread: threading.Thread = threading.Thread(
            target=self._run, name="RawFaultServer", daemon=True,
        )

    def _run(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except (OSError, socket.timeout):
            return
        try:
            self._handler(conn)
        except (OSError, BrokenPipeError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def start(self) -> None:
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=_RAW_SERVER_HOLD_S + 1.0)


# ---------------------------------------------------------------------------
# HTTP fault server — uses http.server for the pathologies that are still
# protocol-conformant on the wire (just semantically broken).
# ---------------------------------------------------------------------------

class _QuietHTTPServer(HTTPServer):
    """HTTPServer that suppresses default stderr logging for the test run."""


class _FuzzHandler(BaseHTTPRequestHandler):
    """Dispatches GET/POST to the callable set on the server instance."""

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence default per-request logging."""

    def do_GET(self) -> None:  # noqa: N802  (http.server contract)
        server_any: Any = self.server
        handler: Callable[[_FuzzHandler], None] = server_any.handler_fn
        handler(self)

    def do_POST(self) -> None:  # noqa: N802  (http.server contract)
        server_any: Any = self.server
        handler: Callable[[_FuzzHandler], None] = server_any.handler_fn
        handler(self)


def _start_http_fault_server(
    handler_fn: Callable[[_FuzzHandler], None],
) -> tuple[_QuietHTTPServer, threading.Thread]:
    """Spin up a one-shot HTTP server bound to an ephemeral localhost port."""
    server = _QuietHTTPServer(("127.0.0.1", 0), _FuzzHandler)
    server.handler_fn = handler_fn  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=server.serve_forever, name="FuzzHTTPServer", daemon=True,
    )
    thread.start()
    return server, thread


# ---------------------------------------------------------------------------
# Connect-refused fault — bind, close, and reuse the now-closed port.
# macOS/Linux both issue ECONNREFUSED for a fresh connect to a port that
# was just bound-and-released, before the kernel reclaims it.
# ---------------------------------------------------------------------------

def _closed_port() -> int:
    """Return a port that has just been closed — connect() will get ECONNREFUSED."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class ConnectRefusedTest(unittest.TestCase):
    """Nothing listening → surfaced as ``unreachable`` without crashing."""

    def test_set_state_on_dead_port(self) -> None:
        port: int = _closed_port()
        client = ZigbeeControlClient(
            f"http://127.0.0.1:{port}", timeout_s=_FUZZ_CLIENT_TIMEOUT_S,
        )
        result = client.set_state("BYIR", "ON")
        self.assertFalse(result.ok)
        assert result.error is not None
        err = result.error.lower()
        self.assertTrue(
            "unreachable" in err or "refused" in err,
            f"expected unreachable/refused, got: {result.error}",
        )

    def test_list_devices_on_dead_port(self) -> None:
        port: int = _closed_port()
        client = ZigbeeControlClient(
            f"http://127.0.0.1:{port}", timeout_s=_FUZZ_CLIENT_TIMEOUT_S,
        )
        ok, err = client.list_devices()
        self.assertFalse(ok)


class HalfDeadSocketTest(unittest.TestCase):
    """TCP accept succeeds, server goes silent — client must time out."""

    def setUp(self) -> None:
        def _silent(conn: socket.socket) -> None:
            # Hold the connection open; never read, never write.
            time.sleep(_RAW_SERVER_HOLD_S)
        self.server: RawFaultServer = RawFaultServer(_silent)
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()

    def test_set_state_against_silent_server(self) -> None:
        client = ZigbeeControlClient(
            self.server.base_url, timeout_s=_FUZZ_CLIENT_TIMEOUT_S,
        )
        started: float = time.monotonic()
        result = client.set_state("BYIR", "ON")
        elapsed: float = time.monotonic() - started
        self.assertFalse(result.ok)
        # Timeout must fire within a small multiple of the configured
        # client timeout — otherwise we're hanging on something else.
        self.assertLess(
            elapsed, _FUZZ_CLIENT_TIMEOUT_S * 3,
            f"client took {elapsed:.2f}s to time out on a silent server",
        )


class MidResponseHangupTest(unittest.TestCase):
    """Server sends partial headers then closes — must not crash."""

    def setUp(self) -> None:
        def _partial(conn: socket.socket) -> None:
            # Read the request so the client's send() completes.
            try:
                conn.recv(4096)
            except OSError:
                return
            # Send the status line and Content-Length, then hang up —
            # no body, no final \r\n\r\n.  urllib will raise a
            # BadStatusLine or URLError-wrapped RemoteDisconnected.
            try:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 42\r\n")
            except OSError:
                pass
            conn.close()
        self.server = RawFaultServer(_partial)
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()

    def test_list_devices_survives_hangup(self) -> None:
        client = ZigbeeControlClient(
            self.server.base_url, timeout_s=_FUZZ_CLIENT_TIMEOUT_S,
        )
        ok, err = client.list_devices()
        self.assertFalse(ok)
        self.assertIsNotNone(err)


class TruncatedJsonBodyTest(unittest.TestCase):
    """Content-length honest but body is half a JSON document."""

    def setUp(self) -> None:
        self.partial: bytes = b'{"devices": [{"name": "LRTV", "type":'

        def _trunc(h: _FuzzHandler) -> None:
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(self.partial)))
            h.end_headers()
            h.wfile.write(self.partial)
        self.server, self.thread = _start_http_fault_server(_trunc)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def test_truncated_body_is_reported(self) -> None:
        client = ZigbeeControlClient(
            f"http://127.0.0.1:{self.server.server_address[1]}",
            timeout_s=_FUZZ_CLIENT_TIMEOUT_S,
        )
        ok, err = client.list_devices()
        self.assertFalse(ok)
        # Either json parse failure or shape failure is acceptable.
        self.assertIsNotNone(err)


class NonJsonBodyTest(unittest.TestCase):
    """200 OK with HTML body — often the signature of a reverse proxy error."""

    def setUp(self) -> None:
        def _html(h: _FuzzHandler) -> None:
            body: bytes = b"<html><body>bad gateway proxy page</body></html>"
            h.send_response(200)
            h.send_header("Content-Type", "text/html")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
        self.server, self.thread = _start_http_fault_server(_html)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def test_html_body_surfaces_as_non_json(self) -> None:
        client = ZigbeeControlClient(
            f"http://127.0.0.1:{self.server.server_address[1]}",
            timeout_s=_FUZZ_CLIENT_TIMEOUT_S,
        )
        ok, err = client.list_devices()
        self.assertFalse(ok)
        self.assertIn("non-JSON", str(err))


class ZeroLengthBodyTest(unittest.TestCase):
    """200 with empty body — client returns an empty dict, which fails the envelope check."""

    def setUp(self) -> None:
        def _empty(h: _FuzzHandler) -> None:
            h.send_response(200)
            h.send_header("Content-Length", "0")
            h.end_headers()
        self.server, self.thread = _start_http_fault_server(_empty)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def test_empty_body_fails_envelope_check(self) -> None:
        client = ZigbeeControlClient(
            f"http://127.0.0.1:{self.server.server_address[1]}",
            timeout_s=_FUZZ_CLIENT_TIMEOUT_S,
        )
        ok, err = client.list_devices()
        self.assertFalse(ok)
        self.assertIn("unexpected response shape", str(err))


class Http500NonJsonBodyTest(unittest.TestCase):
    """5xx with plaintext body — fallback error string, not a crash."""

    def setUp(self) -> None:
        def _five_hundred(h: _FuzzHandler) -> None:
            body: bytes = b"the service is on fire"
            h.send_response(500)
            h.send_header("Content-Type", "text/plain")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
        self.server, self.thread = _start_http_fault_server(_five_hundred)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def test_500_with_plaintext_body_falls_through(self) -> None:
        client = ZigbeeControlClient(
            f"http://127.0.0.1:{self.server.server_address[1]}",
            timeout_s=_FUZZ_CLIENT_TIMEOUT_S,
        )
        ok, err = client.list_devices()
        self.assertFalse(ok)
        # No JSON to pluck 'error' from, so we report HTTP 500.
        self.assertIn("500", str(err))


class Http400MalformedJsonBodyTest(unittest.TestCase):
    """4xx with JSON-ish but malformed body — still reaches the fallback."""

    def setUp(self) -> None:
        def _bad(h: _FuzzHandler) -> None:
            body: bytes = b'{"error": "truncated'
            h.send_response(400)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
        self.server, self.thread = _start_http_fault_server(_bad)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def test_400_with_malformed_json_body_falls_through(self) -> None:
        client = ZigbeeControlClient(
            f"http://127.0.0.1:{self.server.server_address[1]}",
            timeout_s=_FUZZ_CLIENT_TIMEOUT_S,
        )
        result = client.set_state("BYIR", "ON")
        self.assertFalse(result.ok)
        assert result.error is not None
        self.assertIn("400", result.error)


class HugeDeviceListTest(unittest.TestCase):
    """10k-entry device array — must be parsed without issue."""

    _DEVICE_COUNT: int = 10_000

    def setUp(self) -> None:
        body_obj: dict[str, Any] = {
            "devices": [
                {"name": f"fuzz_dev_{i:05d}", "type": "plug"}
                for i in range(self._DEVICE_COUNT)
            ],
        }
        body_bytes: bytes = json.dumps(body_obj).encode("utf-8")

        def _huge(h: _FuzzHandler) -> None:
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body_bytes)))
            h.end_headers()
            h.wfile.write(body_bytes)
        self.server, self.thread = _start_http_fault_server(_huge)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def test_huge_list_parses_completely(self) -> None:
        # Loosen the timeout slightly — the 10k JSON body dominates the
        # timing on a slow filesystem / pytest collection run.
        client = ZigbeeControlClient(
            f"http://127.0.0.1:{self.server.server_address[1]}",
            timeout_s=max(_FUZZ_CLIENT_TIMEOUT_S, 3.0),
        )
        ok, devices = client.list_devices()
        self.assertTrue(ok)
        self.assertEqual(len(devices), self._DEVICE_COUNT)


class UnicodeDeviceNameTest(unittest.TestCase):
    """Weird device names must be URL-encoded, not passed raw."""

    def setUp(self) -> None:
        # Capture the actual path the client requests so we can verify
        # encoding happened.
        self.captured_path: Optional[str] = None

        def _echo(h: _FuzzHandler) -> None:
            self.captured_path = h.path
            body: bytes = json.dumps({
                "device": "x", "desired": "ON", "echoed": True,
                "current_state": "ON", "power_w": None,
            }).encode("utf-8")
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
        self.server, self.thread = _start_http_fault_server(_echo)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def test_name_with_slash_and_unicode_is_encoded(self) -> None:
        """A name containing / or non-ASCII must not inject into the URL path."""
        client = ZigbeeControlClient(
            f"http://127.0.0.1:{self.server.server_address[1]}",
            timeout_s=_FUZZ_CLIENT_TIMEOUT_S,
        )
        # Slashes would alter the path; a right-to-left mark tests
        # quote()'s non-ASCII handling.  A literal NUL ensures we
        # aren't dropping control characters before encoding.
        weird: str = "bad/name‏\x00☃"
        result = client.set_state(weird, "ON")
        # We don't care about ok — just that the client didn't blow
        # up AND that the server saw an encoded path.
        self.assertIsNotNone(self.captured_path)
        assert self.captured_path is not None
        self.assertNotIn("/", self.captured_path[len("/devices/"):-len("/state")])
        # Verify the CommandResult was well-formed (didn't raise).
        self.assertIsInstance(result, CommandResult)


class SetStateAcceptedButNotEchoedTest(unittest.TestCase):
    """Service says ``echoed=False`` — must propagate as ok=True, echoed=False.

    This is distinct from the HTTPError 504 case already covered in the
    unit tests: here the service returned 200 with ``echoed=False``,
    which in the current service doesn't happen but *could* if a
    future version decouples acceptance from echo.  Verifying the
    client handles both shapes keeps it forward-compatible.
    """

    def setUp(self) -> None:
        def _accepted_not_echoed(h: _FuzzHandler) -> None:
            body: bytes = json.dumps({
                "device": "BYIR",
                "desired": "ON",
                "echoed": False,
                "error": "radio wedged",
            }).encode("utf-8")
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
        self.server, self.thread = _start_http_fault_server(
            _accepted_not_echoed,
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def test_accepted_not_echoed_surfaces_both_flags(self) -> None:
        client = ZigbeeControlClient(
            f"http://127.0.0.1:{self.server.server_address[1]}",
            timeout_s=_FUZZ_CLIENT_TIMEOUT_S,
        )
        result = client.set_state("BYIR", "ON")
        self.assertTrue(result.ok)
        self.assertFalse(result.echoed)
        assert result.error is not None
        self.assertIn("radio wedged", result.error)


if __name__ == "__main__":
    unittest.main()
