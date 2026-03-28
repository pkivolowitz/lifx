"""TLV8 codec — type-length-value encoding used by HAP.

Every HAP-BLE PDU body is a sequence of TLV8 items.  Each item is::

    type:   1 byte   (0–255, semantics defined by context)
    length: 1 byte   (0–255)
    value:  *length* bytes

Values longer than 255 bytes are *fragmented* across consecutive items
sharing the same type byte.  The decoder merges them transparently.

This module is a pure codec with no HAP-specific knowledge — type
constants live in :mod:`ble.hap_constants`.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging

logger: logging.Logger = logging.getLogger("glowup.ble.tlv")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum payload bytes per single TLV8 item (one byte encodes length).
TLV_MAX_FRAGMENT: int = 255


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def encode(pairs: list[tuple[int, bytes]]) -> bytes:
    """Encode a list of (type, value) pairs into a TLV8 byte string.

    Values longer than 255 bytes are automatically fragmented across
    consecutive items with the same type.

    Args:
        pairs: Ordered list of ``(type_code, value_bytes)`` tuples.
            ``type_code`` must be in 0–255.  ``value_bytes`` may be
            empty (``b""``) for a zero-length item.

    Returns:
        Concatenated TLV8 encoding.

    Raises:
        ValueError: If a type code is outside 0–255.
    """
    buf: bytearray = bytearray()
    for type_code, value in pairs:
        if not 0 <= type_code <= TLV_MAX_FRAGMENT:
            raise ValueError(
                f"TLV type code must be 0–255, got {type_code}"
            )
        _encode_one(buf, type_code, value)
    return bytes(buf)


def decode(data: bytes) -> list[tuple[int, bytes]]:
    """Decode a TLV8 byte string into a list of (type, value) pairs.

    Consecutive items with the same type are merged (defragmented).

    Args:
        data: Raw TLV8 bytes.

    Returns:
        Ordered list of ``(type_code, value_bytes)`` tuples with
        fragments merged.

    Raises:
        ValueError: If the data is truncated or structurally invalid.
    """
    raw: list[tuple[int, bytes]] = _decode_raw(data)
    return _merge_fragments(raw)


def encode_dict(items: dict[int, bytes]) -> bytes:
    """Convenience: encode a ``{type: value}`` dict.

    Iteration order of the dict determines wire order.  Use an
    ``OrderedDict`` or a plain dict (Python 3.7+ guarantees order)
    when wire order matters.
    """
    return encode(list(items.items()))


def decode_dict(data: bytes) -> dict[int, bytes]:
    """Convenience: decode into a ``{type: value}`` dict.

    If duplicate types appear (after merging fragments), only the
    *last* occurrence is kept.  Use :func:`decode` when duplicates
    matter.
    """
    return dict(decode(data))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _encode_one(buf: bytearray, type_code: int, value: bytes) -> None:
    """Append one (type, value) to *buf*, fragmenting if necessary."""
    offset: int = 0
    remaining: int = len(value)

    if remaining == 0:
        # Zero-length TLV — valid and meaningful in HAP (e.g. empty state).
        buf.append(type_code)
        buf.append(0)
        return

    while remaining > 0:
        chunk_len: int = min(remaining, TLV_MAX_FRAGMENT)
        buf.append(type_code)
        buf.append(chunk_len)
        buf.extend(value[offset:offset + chunk_len])
        offset += chunk_len
        remaining -= chunk_len


def _decode_raw(data: bytes) -> list[tuple[int, bytes]]:
    """Parse raw TLV8 items without merging fragments."""
    items: list[tuple[int, bytes]] = []
    pos: int = 0
    length: int = len(data)

    while pos < length:
        if pos + 1 >= length:
            raise ValueError(
                f"TLV8 truncated: need type+length at offset {pos}, "
                f"only {length - pos} byte(s) remain"
            )
        type_code: int = data[pos]
        val_len: int = data[pos + 1]
        pos += 2

        if pos + val_len > length:
            raise ValueError(
                f"TLV8 truncated: type 0x{type_code:02X} claims "
                f"{val_len} bytes at offset {pos}, only "
                f"{length - pos} available"
            )
        items.append((type_code, data[pos:pos + val_len]))
        pos += val_len

    return items


def _merge_fragments(raw: list[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
    """Merge consecutive items sharing the same type (TLV8 fragmentation)."""
    if not raw:
        return []

    merged: list[tuple[int, bytearray]] = []
    prev_type: int = -1

    for type_code, value in raw:
        if type_code == prev_type and merged:
            # Continuation fragment — append to the previous item.
            merged[-1][1].extend(value)
        else:
            merged.append((type_code, bytearray(value)))
            prev_type = type_code

    return [(t, bytes(v)) for t, v in merged]
