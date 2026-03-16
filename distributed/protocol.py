"""UDP binary wire protocol for high-rate signal transport.

Defines the frame format for sending signal data over UDP between
compute nodes.  The protocol is designed for minimal overhead on
audio-sized payloads (~3 KB) while remaining self-describing.

Frame layout (little-endian)::

    Offset  Size   Field        Description
    ──────  ────   ─────        ───────────
     0       4     magic        "GWUP" (0x47575550)
     4       2     version      Protocol version (1)
     6       2     msg_type     0=signal_data, 1=heartbeat, 2=assignment
     8       4     sequence     Monotonic sequence number (wraps at 2^32)
    12       2     name_len     Length of signal name (UTF-8 bytes)
    14       4     payload_len  Length of payload bytes
    18       1     dtype        Data type: 0=float32, 1=float64, 2=int16, 3=rgb24, 4=json
    19       N     name         Signal name (UTF-8, N = name_len)
    19+N     M     payload      Raw payload bytes (M = payload_len)

Total header overhead: 19 bytes + name length.  For a signal named
``"mic:audio:pcm_raw"`` (18 chars) with a 3200-byte PCM chunk, the
overhead is 37 bytes — 1.2%.

Sequence numbers enable receivers to detect packet loss and discard
out-of-order arrivals.  No retransmission — UDP is fire-and-forget
by design.  For derived signals (bands, beat), MQTT provides reliable
delivery.

The ``dtype`` field tells the receiver how to interpret the payload
without parsing the signal name.  This avoids format negotiation.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import struct
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Magic bytes identifying a GlowUp UDP frame ("GWUP" in ASCII).
MAGIC: bytes = b"GWUP"

# Protocol version.  Increment on breaking wire-format changes.
PROTOCOL_VERSION: int = 1

# Message types.
MSG_SIGNAL_DATA: int = 0
MSG_HEARTBEAT: int = 1
MSG_ASSIGNMENT: int = 2

# Data types for signal payloads.
DTYPE_FLOAT32: int = 0
DTYPE_FLOAT64: int = 1
DTYPE_INT16_PCM: int = 2
DTYPE_RGB24: int = 3
DTYPE_JSON: int = 4

# Header struct format (little-endian):
#   4s = magic, H = version, H = msg_type, I = sequence,
#   H = name_len, I = payload_len, B = dtype
HEADER_FORMAT: str = "<4sHHIHIB"

# Fixed header size in bytes (before variable-length name and payload).
HEADER_SIZE: int = struct.calcsize(HEADER_FORMAT)  # 19 bytes

# Maximum signal name length (bytes).  Names longer than this are
# truncated — keeps the header bounded.
MAX_NAME_LENGTH: int = 255

# Maximum safe UDP payload (65535 IP - 20 IP header - 8 UDP header).
MAX_UDP_PAYLOAD: int = 65507

# Maximum total frame size (header + name + payload).
MAX_FRAME_SIZE: int = MAX_UDP_PAYLOAD

# Bytes per sample for common data types (for convenience).
BYTES_PER_FLOAT32: int = 4
BYTES_PER_FLOAT64: int = 8
BYTES_PER_INT16: int = 2
BYTES_PER_RGB24_PIXEL: int = 3


# ---------------------------------------------------------------------------
# SignalFrame dataclass
# ---------------------------------------------------------------------------

@dataclass
class SignalFrame:
    """A decoded UDP signal frame.

    Attributes:
        msg_type:    Message type (MSG_SIGNAL_DATA, MSG_HEARTBEAT, etc.).
        sequence:    Monotonic sequence number from the sender.
        name:        Signal name (UTF-8 string).
        payload:     Raw payload bytes.
        dtype:       Data type indicator (DTYPE_FLOAT32, etc.).
        version:     Protocol version from the header.
    """
    msg_type: int
    sequence: int
    name: str
    payload: bytes
    dtype: int
    version: int = PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Pack / Unpack
# ---------------------------------------------------------------------------

def pack_signal_frame(name: str, payload: bytes, dtype: int,
                      sequence: int,
                      msg_type: int = MSG_SIGNAL_DATA) -> bytes:
    """Serialize a signal frame into a UDP-ready byte string.

    Args:
        name:     Signal name (e.g. ``"mic:audio:pcm_raw"``).
        payload:  Raw payload bytes.
        dtype:    Data type indicator (DTYPE_FLOAT32, etc.).
        sequence: Monotonic sequence number.
        msg_type: Message type (default: MSG_SIGNAL_DATA).

    Returns:
        Complete frame as bytes, ready for ``socket.sendto()``.

    Raises:
        ValueError: If the total frame exceeds MAX_FRAME_SIZE.
    """
    # Encode signal name to UTF-8, truncate if too long.
    name_bytes: bytes = name.encode("utf-8")[:MAX_NAME_LENGTH]
    name_len: int = len(name_bytes)
    payload_len: int = len(payload)

    total_size: int = HEADER_SIZE + name_len + payload_len
    if total_size > MAX_FRAME_SIZE:
        raise ValueError(
            f"Frame too large: {total_size} bytes "
            f"(max {MAX_FRAME_SIZE}).  "
            f"Signal '{name}' payload is {payload_len} bytes."
        )

    # Pack the fixed header.
    header: bytes = struct.pack(
        HEADER_FORMAT,
        MAGIC,
        PROTOCOL_VERSION,
        msg_type,
        sequence & 0xFFFFFFFF,  # Wrap at 32-bit boundary.
        name_len,
        payload_len,
        dtype,
    )

    return header + name_bytes + payload


def unpack_signal_frame(data: bytes) -> Optional[SignalFrame]:
    """Deserialize a UDP datagram into a SignalFrame.

    Validates the magic bytes and version.  Returns ``None`` for
    malformed or unrecognized frames (rather than raising — the
    receiver loop should silently drop bad packets).

    Args:
        data: Raw bytes received from ``socket.recvfrom()``.

    Returns:
        A :class:`SignalFrame` on success, or ``None`` if the frame
        is malformed, has wrong magic, or unknown version.
    """
    if len(data) < HEADER_SIZE:
        return None

    try:
        (magic, version, msg_type, sequence,
         name_len, payload_len, dtype) = struct.unpack(
            HEADER_FORMAT, data[:HEADER_SIZE]
        )
    except struct.error:
        return None

    # Validate magic.
    if magic != MAGIC:
        return None

    # Validate version (forward-compatible: accept same major version).
    if version != PROTOCOL_VERSION:
        return None

    # Validate lengths against available data.
    expected_total: int = HEADER_SIZE + name_len + payload_len
    if len(data) < expected_total:
        return None

    # Extract name and payload.
    name_start: int = HEADER_SIZE
    name_end: int = name_start + name_len
    payload_start: int = name_end
    payload_end: int = payload_start + payload_len

    try:
        name: str = data[name_start:name_end].decode("utf-8")
    except UnicodeDecodeError:
        return None

    payload: bytes = data[payload_start:payload_end]

    return SignalFrame(
        msg_type=msg_type,
        sequence=sequence,
        name=name,
        payload=payload,
        dtype=dtype,
        version=version,
    )


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------

def pack_float32_array(values: list[float]) -> bytes:
    """Pack a list of floats as little-endian float32 bytes.

    Args:
        values: List of float values.

    Returns:
        Packed bytes (4 bytes per float).
    """
    return struct.pack(f"<{len(values)}f", *values)


def unpack_float32_array(data: bytes) -> list[float]:
    """Unpack little-endian float32 bytes into a list of floats.

    Args:
        data: Raw bytes (must be a multiple of 4).

    Returns:
        List of float values.
    """
    count: int = len(data) // BYTES_PER_FLOAT32
    if count == 0:
        return []
    return list(struct.unpack(f"<{count}f", data[:count * BYTES_PER_FLOAT32]))


def pack_int16_array(values: list[int]) -> bytes:
    """Pack a list of integers as little-endian signed int16 bytes.

    Args:
        values: List of integer values (clamped to [-32768, 32767]).

    Returns:
        Packed bytes (2 bytes per sample).
    """
    return struct.pack(f"<{len(values)}h", *values)


def unpack_int16_array(data: bytes) -> list[int]:
    """Unpack little-endian signed int16 bytes into a list of ints.

    Args:
        data: Raw bytes (must be a multiple of 2).

    Returns:
        List of integer values.
    """
    count: int = len(data) // BYTES_PER_INT16
    if count == 0:
        return []
    return list(struct.unpack(f"<{count}h", data[:count * BYTES_PER_INT16]))
