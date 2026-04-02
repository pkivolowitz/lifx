"""Wire format for voice utterance messages over MQTT.

Encodes audio + metadata into a single MQTT message to avoid the
race condition of publishing PCM and JSON as separate messages.

Wire format::

    ┌──────────────────┬──────────────────┬─────────────────────┐
    │ 4 bytes          │ N bytes          │ remaining bytes     │
    │ header length    │ JSON header      │ raw PCM audio       │
    │ (big-endian u32) │ (UTF-8)          │ (16-bit LE mono)    │
    └──────────────────┴──────────────────┴─────────────────────┘

The header is a JSON object containing room identity, audio format
parameters, timestamp, and wake word confidence score.  The PCM
audio immediately follows the header with no padding or delimiter.

Typical payload size: ~160 KB for a 5-second utterance at 16 kHz.
Well within MQTT's 256 MB limit.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import struct
from typing import Any

# Big-endian unsigned 32-bit integer for the header length prefix.
_HEADER_LEN_FMT: str = ">I"
_HEADER_LEN_SIZE: int = struct.calcsize(_HEADER_LEN_FMT)

# Maximum header size (bytes).  Prevents malformed messages from
# causing unbounded memory allocation.
_MAX_HEADER_SIZE: int = 65536


class ProtocolError(Exception):
    """Raised when a message cannot be decoded."""


def encode(header: dict[str, Any], pcm: bytes) -> bytes:
    """Encode a voice utterance message for MQTT transport.

    Args:
        header: Metadata dict.  Must include at minimum ``room``
                and ``sample_rate``.  Typical keys::

                    {
                        "room": "bedroom",
                        "sample_rate": 16000,
                        "channels": 1,
                        "bit_depth": 16,
                        "timestamp": 1711929600.123,
                        "wake_score": 0.87
                    }

        pcm:    Raw PCM audio bytes (16-bit signed LE mono).

    Returns:
        Single bytes object ready for ``mqtt.publish()``.

    Raises:
        ValueError: If header is missing required fields.
    """
    if "room" not in header:
        raise ValueError("Header must include 'room'")
    if "sample_rate" not in header:
        raise ValueError("Header must include 'sample_rate'")

    header_bytes: bytes = json.dumps(
        header, separators=(",", ":"),
    ).encode("utf-8")
    header_len: int = len(header_bytes)

    return (
        struct.pack(_HEADER_LEN_FMT, header_len)
        + header_bytes
        + pcm
    )


def decode(payload: bytes) -> tuple[dict[str, Any], bytes]:
    """Decode an MQTT voice utterance message.

    Args:
        payload: Raw MQTT message payload.

    Returns:
        Tuple of (header_dict, pcm_bytes).

    Raises:
        ProtocolError: If the message is malformed, truncated, or
                       contains invalid JSON.
    """
    if len(payload) < _HEADER_LEN_SIZE:
        raise ProtocolError(
            f"Message too short: {len(payload)} bytes "
            f"(need at least {_HEADER_LEN_SIZE} for header length)"
        )

    header_len: int = struct.unpack(
        _HEADER_LEN_FMT, payload[:_HEADER_LEN_SIZE],
    )[0]

    if header_len > _MAX_HEADER_SIZE:
        raise ProtocolError(
            f"Header length {header_len} exceeds maximum {_MAX_HEADER_SIZE}"
        )

    header_end: int = _HEADER_LEN_SIZE + header_len

    if len(payload) < header_end:
        raise ProtocolError(
            f"Message truncated: have {len(payload)} bytes, "
            f"need {header_end} for header"
        )

    header_bytes: bytes = payload[_HEADER_LEN_SIZE:header_end]

    try:
        header: dict[str, Any] = json.loads(
            header_bytes.decode("utf-8"),
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"Invalid header JSON: {exc}") from exc

    if not isinstance(header, dict):
        raise ProtocolError(
            f"Header must be a JSON object, got {type(header).__name__}"
        )

    pcm: bytes = payload[header_end:]

    return header, pcm
