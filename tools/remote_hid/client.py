"""glowup-remote-hid client — Mac-side trackpad + keyboard forwarder.

Usage::

    python -m tools.remote_hid.client --host <broker-2 host> --port 8429 \\
        --secret-file ~/.glowup/remote_hid.token

Behavior
--------
On launch, an idle keyboard listener watches for the capture-toggle
hotkey (F13 by default).  First F13 press: open a TCP connection to
the server, send the handshake, then start a pair of suppressed
pynput listeners that consume local trackpad + keyboard events and
forward them over the wire as protocol frames.  Second F13 press:
tear those listeners down and restore local input.

The one-in-flight connection / suppressed-listener pair is the only
stateful thing this client holds; a failed connect simply returns
to idle without poisoning future attempts.

Requires (Mac)
--------------
    pip install pynput pyobjc-framework-Quartz

    macOS Settings -> Privacy & Security:
        - Input Monitoring: allow the terminal / IDE running this
        - Accessibility:    allow the terminal / IDE running this
"""

__version__ = "1.0.0"

import argparse
import logging
import socket
import sys
import threading
from pathlib import Path
from typing import Optional, Tuple

from pynput import keyboard, mouse

from . import protocol, keymap

# Move delta clamp — protocol packs int16 (±32767) so we only clamp
# for defense-in-depth.  Normal trackpad deltas are single digits per
# event; anything beyond this is either a bug or an extreme flick.
_MAX_DELTA: int = 32000

# Scroll-unit normalization.  pynput reports scroll in "lines" which
# for a Mac trackpad are typically ±1 per fine scroll step.  We pass
# through as-is; the server maps straight to REL_WHEEL/REL_HWHEEL.

# Mouse button translation: pynput Button -> protocol button_id.
_BUTTON_IDS = {
    mouse.Button.left: 1,
    mouse.Button.middle: 2,
    mouse.Button.right: 3,
}

logger = logging.getLogger("glowup.remote_hid.client")


class RemoteHIDClient:
    """Owns the socket and the suppressed-listener lifecycle."""

    def __init__(self, host: str, port: int, secret: Optional[str]) -> None:
        """Store config; no network I/O until start_capture() is called."""
        self._host: str = host
        self._port: int = port
        self._secret: Optional[str] = secret
        self._sock: Optional[socket.socket] = None
        self._mouse_listener: Optional[mouse.Listener] = None
        self._kb_listener: Optional[keyboard.Listener] = None
        self._last_pos: Optional[Tuple[int, int]] = None
        # Writes to the socket come from two pynput callback threads
        # (mouse + keyboard).  Guard with a plain Lock.
        self._send_lock: threading.Lock = threading.Lock()
        self._capturing: bool = False

    @property
    def capturing(self) -> bool:
        """True between start_capture() and stop_capture()."""
        return self._capturing

    def start_capture(self) -> bool:
        """Open the socket, handshake, start suppressed listeners.

        Returns False and leaves self idle if the connection fails.
        """
        if self._capturing:
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((self._host, self._port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.sendall(protocol.compute_handshake(self._secret))
        except OSError as exc:
            logger.error("connect failed: %s", exc)
            return False
        self._sock = sock
        self._last_pos = None
        self._mouse_listener = mouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click,
            on_scroll=self._on_scroll,
            suppress=True,
        )
        self._kb_listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            suppress=True,
        )
        self._mouse_listener.start()
        self._kb_listener.start()
        self._capturing = True
        logger.info("capture ON (forwarding to %s:%d)", self._host, self._port)
        return True

    def stop_capture(self) -> None:
        """Tear down listeners and close the socket.  Idempotent."""
        if not self._capturing:
            return
        self._capturing = False
        for listener in (self._mouse_listener, self._kb_listener):
            if listener is not None:
                listener.stop()
        self._mouse_listener = None
        self._kb_listener = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        logger.info("capture OFF")

    # ---- pynput callbacks (run on listener threads) ----

    def _on_move(self, x: float, y: float) -> None:
        """Compute delta from last reported position, send MOVE frame.

        pynput delivers floats on macOS (Retina trackpads report in
        subpixel fractions); we round-and-cast before packing because
        struct.pack('<hh', ...) demands ints.
        """
        ix, iy = int(round(x)), int(round(y))
        if self._last_pos is None:
            # First event after start — seed baseline; no delta sent.
            self._last_pos = (ix, iy)
            return
        dx = max(-_MAX_DELTA, min(_MAX_DELTA, ix - self._last_pos[0]))
        dy = max(-_MAX_DELTA, min(_MAX_DELTA, iy - self._last_pos[1]))
        self._last_pos = (ix, iy)
        if dx or dy:
            self._send(protocol.MsgType.MOVE, protocol.pack_move(dx, dy))

    def _on_click(self, x: float, y: float, button: mouse.Button,
                  pressed: bool) -> None:
        """Translate button + pressed flag and forward."""
        btn_id = _BUTTON_IDS.get(button)
        if btn_id is None:
            return
        self._send(protocol.MsgType.BUTTON,
                   protocol.pack_button(btn_id, pressed))

    def _on_scroll(self, x: float, y: float, dx: float, dy: float) -> None:
        """Forward scroll deltas.  Trackpad scroll deltas can be
        fractional; round-and-cast before packing.
        """
        idx, idy = int(round(dx)), int(round(dy))
        if idx or idy:
            self._send(protocol.MsgType.SCROLL,
                       protocol.pack_scroll(idx, idy))

    def _on_press(self, key) -> None:  # noqa: ANN001 — pynput Key | KeyCode
        """Key-down handler — also catches F13 to toggle OFF."""
        if self._handle_toggle(key):
            return
        ev_key = _nsevent_of(key)
        if ev_key is None:
            return
        evk = keymap.translate(ev_key)
        if evk is None:
            return
        self._send(protocol.MsgType.KEY, protocol.pack_key(evk, True))

    def _on_release(self, key) -> None:  # noqa: ANN001
        """Key-up handler.  Toggle keys also fire release, ignore those."""
        ev_key = _nsevent_of(key)
        if ev_key is None or ev_key == keymap.TOGGLE_KEYCODE:
            return
        evk = keymap.translate(ev_key)
        if evk is None:
            return
        self._send(protocol.MsgType.KEY, protocol.pack_key(evk, False))

    def _handle_toggle(self, key) -> bool:  # noqa: ANN001
        """Return True if the event was the capture-OFF toggle."""
        ev_key = _nsevent_of(key)
        if ev_key == keymap.TOGGLE_KEYCODE:
            # Defer stop_capture to a worker thread — pynput callbacks
            # cannot stop their own listener from inside the callback
            # on macOS without deadlock.
            threading.Thread(target=self.stop_capture, daemon=True).start()
            return True
        return False

    # ---- socket write with soft failure handling ----

    def _send(self, msg_type: protocol.MsgType, payload: bytes) -> None:
        """Send one frame; tear down capture on write error."""
        frame = protocol.encode(msg_type, payload)
        with self._send_lock:
            sock = self._sock
            if sock is None:
                return
            try:
                sock.sendall(frame)
            except OSError as exc:
                logger.error("send failed (%s); dropping capture", exc)
                threading.Thread(target=self.stop_capture,
                                 daemon=True).start()


def _nsevent_of(key) -> Optional[int]:  # noqa: ANN001
    """Extract the macOS virtual keyCode from a pynput key event.

    pynput delivers two shapes: a KeyCode for printable keys (has .vk)
    and a Key enum for named keys (value is KeyCode).  Both expose .vk
    via this unified accessor.
    """
    if hasattr(key, "vk") and key.vk is not None:
        return int(key.vk)
    value = getattr(key, "value", None)
    if value is not None and getattr(value, "vk", None) is not None:
        return int(value.vk)
    return None


def _load_secret(path: Optional[Path]) -> Optional[str]:
    """Read the shared-secret token from a file, stripping whitespace."""
    if path is None:
        return None
    return path.expanduser().read_text().strip() or None


def main() -> int:
    """Run an idle toggle-key watcher that arms the client on demand."""
    parser = argparse.ArgumentParser(description="glowup-remote-hid client")
    parser.add_argument("--host", required=True,
                        help="remote server hostname or IP")
    parser.add_argument("--port", type=int, default=8429)
    parser.add_argument("--secret-file", type=Path, default=None,
                        help="path to file containing the shared secret")
    parser.add_argument("--toggle-key", type=int, default=None,
                        help=("macOS virtual keyCode to toggle capture "
                              "(default: %d = Right Option)"
                              % keymap.TOGGLE_KEYCODE))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    if args.toggle_key is not None:
        # Override the module-level constant so both idle and suppressed
        # listeners see the new toggle — single source of truth.
        keymap.TOGGLE_KEYCODE = args.toggle_key
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    secret = _load_secret(args.secret_file)
    client = RemoteHIDClient(args.host, args.port, secret)
    print(f"glowup-remote-hid client v{__version__}")
    print(f"target {args.host}:{args.port}  auth={'on' if secret else 'off'}")
    print(f"toggle-key = macOS keyCode {keymap.TOGGLE_KEYCODE}")
    print("press the toggle key to arm/release capture; Ctrl-C here to quit")

    def _idle_on_press(key) -> None:  # noqa: ANN001
        """Idle F13-watcher: arms capture when off and F13 is pressed."""
        if client.capturing:
            return
        if _nsevent_of(key) == keymap.TOGGLE_KEYCODE:
            client.start_capture()

    with keyboard.Listener(on_press=_idle_on_press, suppress=False) as idle:
        try:
            idle.join()
        except KeyboardInterrupt:
            pass
        finally:
            client.stop_capture()
    return 0


if __name__ == "__main__":
    sys.exit(main())
