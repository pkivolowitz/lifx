"""Tests for distributed.protocol — UDP wire format pack/unpack.

Validates round-trip serialization, edge cases (empty payload, max name
length, sequence wrap), and malformed frame rejection.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import struct
import unittest

from distributed.protocol import (
    DTYPE_FLOAT32, DTYPE_INT16_PCM, DTYPE_JSON,
    HEADER_SIZE, MAGIC, MAX_NAME_LENGTH, MAX_FRAME_SIZE,
    MSG_ASSIGNMENT, MSG_HEARTBEAT, MSG_SIGNAL_DATA,
    PROTOCOL_VERSION, SignalFrame,
    pack_signal_frame, unpack_signal_frame,
    pack_float32_array, unpack_float32_array,
    pack_int16_array, unpack_int16_array,
)


class TestProtocolRoundTrip(unittest.TestCase):
    """Verify that pack → unpack produces identical data."""

    def test_basic_round_trip(self) -> None:
        """Pack a signal frame and unpack it; fields must match."""
        name: str = "mic:audio:pcm_raw"
        payload: bytes = b"\x00\x01\x02\x03" * 800  # 3200 bytes
        seq: int = 42

        frame_bytes: bytes = pack_signal_frame(
            name, payload, DTYPE_INT16_PCM, seq,
        )
        frame: SignalFrame = unpack_signal_frame(frame_bytes)

        self.assertIsNotNone(frame)
        self.assertEqual(frame.msg_type, MSG_SIGNAL_DATA)
        self.assertEqual(frame.sequence, seq)
        self.assertEqual(frame.name, name)
        self.assertEqual(frame.payload, payload)
        self.assertEqual(frame.dtype, DTYPE_INT16_PCM)
        self.assertEqual(frame.version, PROTOCOL_VERSION)

    def test_empty_payload(self) -> None:
        """Heartbeat-style frame with zero-length payload."""
        frame_bytes: bytes = pack_signal_frame(
            "node:health", b"", DTYPE_JSON, 0, MSG_HEARTBEAT,
        )
        frame: SignalFrame = unpack_signal_frame(frame_bytes)

        self.assertIsNotNone(frame)
        self.assertEqual(frame.msg_type, MSG_HEARTBEAT)
        self.assertEqual(frame.payload, b"")
        self.assertEqual(frame.name, "node:health")

    def test_empty_name(self) -> None:
        """Frame with an empty signal name."""
        frame_bytes: bytes = pack_signal_frame(
            "", b"\xff", DTYPE_FLOAT32, 1,
        )
        frame: SignalFrame = unpack_signal_frame(frame_bytes)

        self.assertIsNotNone(frame)
        self.assertEqual(frame.name, "")
        self.assertEqual(frame.payload, b"\xff")

    def test_sequence_wrap(self) -> None:
        """Sequence numbers wrap at 32-bit boundary."""
        seq: int = 0xFFFFFFFF
        frame_bytes: bytes = pack_signal_frame(
            "test", b"x", DTYPE_FLOAT32, seq,
        )
        frame: SignalFrame = unpack_signal_frame(frame_bytes)

        self.assertIsNotNone(frame)
        self.assertEqual(frame.sequence, 0xFFFFFFFF)

        # Wrap to 0.
        frame_bytes = pack_signal_frame(
            "test", b"x", DTYPE_FLOAT32, seq + 1,
        )
        frame = unpack_signal_frame(frame_bytes)
        self.assertEqual(frame.sequence, 0)

    def test_max_name_length(self) -> None:
        """Names longer than MAX_NAME_LENGTH are truncated."""
        long_name: str = "a" * (MAX_NAME_LENGTH + 50)
        frame_bytes: bytes = pack_signal_frame(
            long_name, b"data", DTYPE_FLOAT32, 1,
        )
        frame: SignalFrame = unpack_signal_frame(frame_bytes)

        self.assertIsNotNone(frame)
        self.assertEqual(len(frame.name), MAX_NAME_LENGTH)

    def test_all_message_types(self) -> None:
        """Each message type round-trips correctly."""
        for msg_type in (MSG_SIGNAL_DATA, MSG_HEARTBEAT, MSG_ASSIGNMENT):
            frame_bytes: bytes = pack_signal_frame(
                "test", b"data", DTYPE_FLOAT32, 1, msg_type,
            )
            frame: SignalFrame = unpack_signal_frame(frame_bytes)
            self.assertIsNotNone(frame)
            self.assertEqual(frame.msg_type, msg_type)

    def test_all_dtypes(self) -> None:
        """Each dtype round-trips correctly."""
        for dtype in (DTYPE_FLOAT32, DTYPE_INT16_PCM, DTYPE_JSON):
            frame_bytes: bytes = pack_signal_frame(
                "test", b"data", dtype, 1,
            )
            frame: SignalFrame = unpack_signal_frame(frame_bytes)
            self.assertIsNotNone(frame)
            self.assertEqual(frame.dtype, dtype)

    def test_unicode_name(self) -> None:
        """Signal names with non-ASCII characters survive round-trip."""
        name: str = "sensor:température:°C"
        frame_bytes: bytes = pack_signal_frame(
            name, b"data", DTYPE_FLOAT32, 1,
        )
        frame: SignalFrame = unpack_signal_frame(frame_bytes)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.name, name)


class TestProtocolReject(unittest.TestCase):
    """Verify that malformed frames are safely rejected."""

    def test_too_short(self) -> None:
        """Frames shorter than HEADER_SIZE return None."""
        self.assertIsNone(unpack_signal_frame(b""))
        self.assertIsNone(unpack_signal_frame(b"GWUP"))
        self.assertIsNone(unpack_signal_frame(b"\x00" * (HEADER_SIZE - 1)))

    def test_wrong_magic(self) -> None:
        """Frames with wrong magic bytes return None."""
        frame_bytes: bytes = pack_signal_frame(
            "test", b"data", DTYPE_FLOAT32, 1,
        )
        # Corrupt magic.
        corrupted: bytes = b"XXXX" + frame_bytes[4:]
        self.assertIsNone(unpack_signal_frame(corrupted))

    def test_wrong_version(self) -> None:
        """Frames with unknown version return None."""
        frame_bytes: bytes = pack_signal_frame(
            "test", b"data", DTYPE_FLOAT32, 1,
        )
        # Replace version field (bytes 4-5) with version 99.
        corrupted: bytearray = bytearray(frame_bytes)
        struct.pack_into("<H", corrupted, 4, 99)
        self.assertIsNone(unpack_signal_frame(bytes(corrupted)))

    def test_truncated_payload(self) -> None:
        """Frame claims more payload than available bytes."""
        frame_bytes: bytes = pack_signal_frame(
            "test", b"data", DTYPE_FLOAT32, 1,
        )
        # Chop off last byte.
        self.assertIsNone(unpack_signal_frame(frame_bytes[:-1]))

    def test_truncated_name(self) -> None:
        """Frame claims more name bytes than available."""
        frame_bytes: bytes = pack_signal_frame(
            "long_signal_name", b"data", DTYPE_FLOAT32, 1,
        )
        # Truncate into the name region.
        cut_point: int = HEADER_SIZE + 2
        self.assertIsNone(unpack_signal_frame(frame_bytes[:cut_point]))

    def test_oversized_frame(self) -> None:
        """Packing a frame that exceeds MAX_FRAME_SIZE raises ValueError."""
        huge_payload: bytes = b"\x00" * (MAX_FRAME_SIZE + 1)
        with self.assertRaises(ValueError):
            pack_signal_frame("test", huge_payload, DTYPE_FLOAT32, 1)


class TestPayloadHelpers(unittest.TestCase):
    """Verify float32 and int16 array pack/unpack helpers."""

    def test_float32_round_trip(self) -> None:
        """Float32 array survives pack/unpack with expected precision."""
        values: list[float] = [0.0, 1.0, -1.0, 0.5, 3.14159]
        packed: bytes = pack_float32_array(values)
        self.assertEqual(len(packed), len(values) * 4)
        unpacked: list[float] = unpack_float32_array(packed)
        self.assertEqual(len(unpacked), len(values))
        for orig, decoded in zip(values, unpacked):
            self.assertAlmostEqual(orig, decoded, places=5)

    def test_float32_empty(self) -> None:
        """Empty float32 array produces empty bytes and back."""
        self.assertEqual(pack_float32_array([]), b"")
        self.assertEqual(unpack_float32_array(b""), [])

    def test_int16_round_trip(self) -> None:
        """Int16 PCM array survives pack/unpack."""
        values: list[int] = [0, 32767, -32768, 1000, -1000]
        packed: bytes = pack_int16_array(values)
        self.assertEqual(len(packed), len(values) * 2)
        unpacked: list[int] = unpack_int16_array(packed)
        self.assertEqual(unpacked, values)

    def test_int16_empty(self) -> None:
        """Empty int16 array produces empty bytes and back."""
        self.assertEqual(pack_int16_array([]), b"")
        self.assertEqual(unpack_int16_array(b""), [])

    def test_float32_partial_bytes_ignored(self) -> None:
        """Trailing bytes that don't form a complete float32 are ignored."""
        values: list[float] = [1.0, 2.0]
        packed: bytes = pack_float32_array(values) + b"\x00\x00"  # 2 extra
        unpacked: list[float] = unpack_float32_array(packed)
        self.assertEqual(len(unpacked), 2)


if __name__ == "__main__":
    unittest.main()
