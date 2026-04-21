"""Wire protocol for glowup-remote-hid — mouse + keyboard events.

Frame format (all little-endian):

    uint16 length   -- bytes to follow, i.e. 1 + len(payload)
    uint8  type     -- one of MsgType
    bytes  payload  -- per-type layout below

Per-type payloads (names match the packers below):

    MOVE    int16 dx, int16 dy                    (relative pointer motion)
    BUTTON  uint8 button_id, uint8 pressed        (1=left, 2=middle, 3=right)
    SCROLL  int16 dx, int16 dy                    (two-finger scroll deltas)
    KEY     uint16 ev_key_code, uint8 pressed     (Linux EV_KEY codes)

Connection setup: the very first bytes a client sends after TCP
connect are the 32-byte HMAC-SHA256 of HANDSHAKE_LABEL computed with
the server's shared secret, OR zero bytes if auth is disabled at the
server.  The server reads exactly HMAC_SIZE bytes (or zero in the
disabled case), verifies with hmac.compare_digest, and either begins
dispatching frames or closes the connection.  Replay-on-LAN is
explicitly out of scope — this transport is for a trusted home net.
"""

__version__ = "1.0.0"

import hmac
import hashlib
import struct
from enum import IntEnum
from typing import Optional, Tuple

# Wire protocol version.  Bump when a frame layout changes so that
# mismatched client/server versions fail loudly at handshake time.
PROTO_VERSION: int = 1

# Frame header: uint16 length, uint8 message type.
_HDR = struct.Struct("<HB")

# Static label bound into the handshake HMAC so that a leaked secret
# can't be reused against a differently-labeled protocol sharing the
# same key file (defense-in-depth; the label is not itself secret).
HANDSHAKE_LABEL: bytes = b"GLOWUP-REMOTE-HID:v1"

# HMAC-SHA256 digest size — fixed because the algorithm is fixed.
HMAC_SIZE: int = 32

# Upper bound on per-frame payload.  Largest legitimate payload is
# 4 bytes (MOVE / SCROLL); cap generously so a malformed length field
# cannot coerce the server into allocating an arbitrary buffer.
MAX_FRAME_PAYLOAD: int = 64


class MsgType(IntEnum):
    """Wire-type tag placed in the 3rd byte of every frame."""

    MOVE = 1
    BUTTON = 2
    SCROLL = 3
    KEY = 4


# Per-type payload codecs.

_MOVE = struct.Struct("<hh")
_BUTTON = struct.Struct("<BB")
_SCROLL = struct.Struct("<hh")
_KEY = struct.Struct("<HB")


def compute_handshake(secret: Optional[str]) -> bytes:
    """Return the HMAC-SHA256 over HANDSHAKE_LABEL, or b'' if no secret.

    An empty handshake means auth is disabled end-to-end; the server
    must accept this ONLY when its own config has no secret.
    """
    if secret is None:
        return b""
    return hmac.new(secret.encode("utf-8"), HANDSHAKE_LABEL, hashlib.sha256).digest()


def verify_handshake(provided: bytes, secret: Optional[str]) -> bool:
    """Constant-time comparison of client-provided handshake vs ours."""
    return hmac.compare_digest(provided, compute_handshake(secret))


def encode(msg_type: MsgType, payload: bytes) -> bytes:
    """Pack one frame.  Raises ValueError for payloads over the cap."""
    if len(payload) > MAX_FRAME_PAYLOAD:
        raise ValueError(
            f"payload too large: {len(payload)} > {MAX_FRAME_PAYLOAD}"
        )
    return _HDR.pack(1 + len(payload), int(msg_type)) + payload


def decode_header(buf: bytes) -> Tuple[int, int]:
    """Parse a 3-byte header.  Returns (payload_length, msg_type).

    Raises ValueError when the length field would indicate an
    oversized payload — the server uses this to drop the connection.
    """
    length, msg_type = _HDR.unpack(buf)
    if length > MAX_FRAME_PAYLOAD + 1 or length < 1:
        raise ValueError(f"bad frame length: {length}")
    return length - 1, msg_type


def pack_move(dx: int, dy: int) -> bytes:
    """Pack a MOVE payload; dx/dy are int16 relative deltas."""
    return _MOVE.pack(dx, dy)


def unpack_move(payload: bytes) -> Tuple[int, int]:
    """Unpack a MOVE payload into (dx, dy)."""
    return _MOVE.unpack(payload)


def pack_button(button_id: int, pressed: bool) -> bytes:
    """Pack a BUTTON payload.  button_id: 1=left, 2=middle, 3=right."""
    return _BUTTON.pack(button_id, 1 if pressed else 0)


def unpack_button(payload: bytes) -> Tuple[int, bool]:
    """Unpack a BUTTON payload into (button_id, pressed)."""
    b, p = _BUTTON.unpack(payload)
    return b, bool(p)


def pack_scroll(dx: int, dy: int) -> bytes:
    """Pack a SCROLL payload; dx/dy are int16 wheel detents."""
    return _SCROLL.pack(dx, dy)


def unpack_scroll(payload: bytes) -> Tuple[int, int]:
    """Unpack a SCROLL payload into (dx, dy)."""
    return _SCROLL.unpack(payload)


def pack_key(ev_key_code: int, pressed: bool) -> bytes:
    """Pack a KEY payload.  ev_key_code is a Linux EV_KEY code."""
    return _KEY.pack(ev_key_code, 1 if pressed else 0)


def unpack_key(payload: bytes) -> Tuple[int, bool]:
    """Unpack a KEY payload into (ev_key_code, pressed)."""
    k, p = _KEY.unpack(payload)
    return k, bool(p)
