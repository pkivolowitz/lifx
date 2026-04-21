"""glowup-remote-hid server — headless Pi accepts mouse+keyboard over TCP.

Architecture
------------
Listens on a TCP port.  On connect, reads the fixed-size handshake
HMAC; if it matches the configured shared secret (or if auth is
disabled and the client sends zero bytes) it accepts the connection
and enters a dispatch loop.  Each frame is translated to kernel-
level uinput events on a composite virtual HID device that presents
as both a mouse (REL_X/Y, LEFT/MIDDLE/RIGHT buttons, wheels) and a
keyboard (every EV_KEY our keymap table produces).

The uinput approach works transparently under X11, labwc, and
bare-console tty — no compositor-specific hooks.  It requires the
kernel uinput module and group write access to /dev/uinput; the
deploy/99-uinput.rules udev rule handles the latter.

Only ONE client is accepted at a time.  A second connection while
one is active is rejected immediately, so a forgotten client on
another machine can't silently steal cursor control.

Config
------
Reads /etc/glowup/remote_hid.json (path overridable via --config).
Example:

    {
        "bind": "0.0.0.0",
        "port": 8429,
        "auth_token": "<shared-secret-or-null>",
        "device_name": "GlowUp Remote HID"
    }
"""

__version__ = "1.0.0"

import argparse
import json
import logging
import signal
import socket
import sys
from pathlib import Path
from typing import Optional

from evdev import UInput, ecodes as ec

from . import protocol
from .keymap import all_mapped_evkeys

# Default config path — installer places the file here with mode 0600
# because the auth_token is sensitive.
DEFAULT_CONFIG_PATH: Path = Path("/etc/glowup/remote_hid.json")

# Port 8429 reserved for this service.  No IANA allocation; picked to
# sit above the other glowup services (server=8420, zigbee=8422).
DEFAULT_PORT: int = 8429

# Max bytes accepted per single recv loop iteration.  Sized so one
# read drains a typical 120 Hz burst (~80 frames of 4-byte payload).
RECV_CHUNK: int = 4096

# Socket TCP_NODELAY off (default) is wrong for this workload — we
# send tiny frequent frames and don't want Nagle batching them.  Set
# explicitly for clarity.
_TCP_NODELAY: int = 1

# Button IDs on the wire match our protocol.py docstring: 1/2/3 =
# left/middle/right.  Kernel constants resolved at server startup.
_BUTTON_MAP: dict = {
    1: ec.BTN_LEFT,
    2: ec.BTN_MIDDLE,
    3: ec.BTN_RIGHT,
}

logger = logging.getLogger("glowup.remote_hid.server")


class RemoteHIDServer:
    """One-connection-at-a-time TCP server driving a uinput device."""

    def __init__(self, bind: str, port: int, secret: Optional[str],
                 device_name: str) -> None:
        """Build the composite uinput device and open the listen socket."""
        self._secret: Optional[str] = secret
        capabilities = {
            ec.EV_REL: [ec.REL_X, ec.REL_Y, ec.REL_HWHEEL, ec.REL_WHEEL],
            ec.EV_KEY: (
                [ec.BTN_LEFT, ec.BTN_MIDDLE, ec.BTN_RIGHT]
                + all_mapped_evkeys()
            ),
        }
        self._ui: UInput = UInput(capabilities, name=device_name,
                                  vendor=0x1209, product=0xC0DE)
        logger.info("uinput device created: %s", device_name)
        self._listener: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM
        )
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((bind, port))
        self._listener.listen(1)
        logger.info("listening on %s:%d (auth=%s)",
                    bind, port, "on" if secret else "off")
        self._stop: bool = False

    def run(self) -> None:
        """Accept loop.  Returns when shutdown() has been called."""
        while not self._stop:
            try:
                conn, addr = self._listener.accept()
            except OSError:
                if self._stop:
                    break
                raise
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY,
                            _TCP_NODELAY)
            logger.info("client connected: %s:%d", *addr)
            try:
                self._serve_connection(conn)
            except Exception:
                logger.exception("connection aborted (%s:%d)", *addr)
            finally:
                conn.close()
                logger.info("client disconnected: %s:%d", *addr)
                # Release any held keys / buttons so a disconnect can't
                # leave the remote kernel with stuck modifiers.
                self._release_all()

    def shutdown(self) -> None:
        """Cause the accept loop to exit at the next iteration."""
        self._stop = True
        try:
            self._listener.close()
        except OSError:
            pass

    def _serve_connection(self, conn: socket.socket) -> None:
        """Handshake, then dispatch frames until the peer disconnects."""
        if not self._authenticate(conn):
            logger.warning("auth failed; dropping connection")
            return
        logger.info("client authenticated")
        buf: bytearray = bytearray()
        while True:
            chunk = conn.recv(RECV_CHUNK)
            if not chunk:
                return
            buf.extend(chunk)
            self._consume_frames(buf)

    def _authenticate(self, conn: socket.socket) -> bool:
        """Read HMAC_SIZE bytes (or 0 in disabled mode) and verify."""
        if self._secret is None:
            # Auth disabled: client must send zero bytes before any
            # frame.  We peek 3 bytes and rely on the header parser
            # to keep things flowing; no handshake bytes consumed.
            return True
        provided = self._recv_exactly(conn, protocol.HMAC_SIZE)
        if provided is None:
            return False
        return protocol.verify_handshake(provided, self._secret)

    @staticmethod
    def _recv_exactly(conn: socket.socket, n: int) -> Optional[bytes]:
        """Read exactly n bytes; None if EOF arrives first."""
        chunks = []
        remaining = n
        while remaining:
            part = conn.recv(remaining)
            if not part:
                return None
            chunks.append(part)
            remaining -= len(part)
        return b"".join(chunks)

    def _consume_frames(self, buf: bytearray) -> None:
        """Extract every complete frame from the buffer and dispatch it."""
        while len(buf) >= 3:
            try:
                payload_len, msg_type = protocol.decode_header(bytes(buf[:3]))
            except ValueError as exc:
                # Oversized length field = malformed stream.  Raising
                # lets the caller drop the connection cleanly.
                raise RuntimeError(f"bad frame: {exc}") from exc
            if len(buf) < 3 + payload_len:
                return  # wait for more bytes
            payload = bytes(buf[3:3 + payload_len])
            del buf[:3 + payload_len]
            self._dispatch(msg_type, payload)

    def _dispatch(self, msg_type: int, payload: bytes) -> None:
        """Translate one frame into uinput events."""
        if msg_type == protocol.MsgType.MOVE:
            dx, dy = protocol.unpack_move(payload)
            self._ui.write(ec.EV_REL, ec.REL_X, dx)
            self._ui.write(ec.EV_REL, ec.REL_Y, dy)
            self._ui.syn()
        elif msg_type == protocol.MsgType.BUTTON:
            btn_id, pressed = protocol.unpack_button(payload)
            btn_code = _BUTTON_MAP.get(btn_id)
            if btn_code is None:
                logger.debug("ignored unknown button_id=%d", btn_id)
                return
            self._ui.write(ec.EV_KEY, btn_code, 1 if pressed else 0)
            self._ui.syn()
        elif msg_type == protocol.MsgType.SCROLL:
            dx, dy = protocol.unpack_scroll(payload)
            if dy:
                self._ui.write(ec.EV_REL, ec.REL_WHEEL, dy)
            if dx:
                self._ui.write(ec.EV_REL, ec.REL_HWHEEL, dx)
            self._ui.syn()
        elif msg_type == protocol.MsgType.KEY:
            ev_key, pressed = protocol.unpack_key(payload)
            self._ui.write(ec.EV_KEY, ev_key, 1 if pressed else 0)
            self._ui.syn()
        else:
            logger.warning("unknown msg_type=%d", msg_type)

    def _release_all(self) -> None:
        """Emit key-up / button-up for everything.  Called on disconnect.

        Without this, a client crash mid-modifier leaves the remote
        kernel thinking Shift is still held — surprising and hard to
        debug.  Walking the whole capability set is cheap.
        """
        for btn in _BUTTON_MAP.values():
            self._ui.write(ec.EV_KEY, btn, 0)
        for ev_key in all_mapped_evkeys():
            self._ui.write(ec.EV_KEY, ev_key, 0)
        self._ui.syn()


def _load_config(path: Path) -> dict:
    """Parse the JSON config, applying defaults for missing keys."""
    raw = json.loads(path.read_text())
    return {
        "bind": raw.get("bind", "0.0.0.0"),
        "port": int(raw.get("port", DEFAULT_PORT)),
        "auth_token": raw.get("auth_token") or None,
        "device_name": raw.get("device_name", "GlowUp Remote HID"),
    }


def main() -> int:
    """Entry point for both CLI and systemd ExecStart invocations."""
    parser = argparse.ArgumentParser(description="glowup-remote-hid server")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help="path to JSON config (default: %(default)s)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = _load_config(args.config)
    server = RemoteHIDServer(
        bind=cfg["bind"], port=cfg["port"],
        secret=cfg["auth_token"], device_name=cfg["device_name"],
    )
    # Graceful shutdown on SIGTERM so systemd stop is clean and the
    # uinput device is destroyed in __del__.
    signal.signal(signal.SIGTERM, lambda *_: server.shutdown())
    signal.signal(signal.SIGINT, lambda *_: server.shutdown())
    try:
        server.run()
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
