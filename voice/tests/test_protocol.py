"""Exhaustive tests for the voice protocol wire format.

Covers round-trip encoding/decoding, edge cases, error handling,
malformed messages, large payloads, and unicode room names.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import struct
import time
import unittest
from typing import Any

from voice.protocol import (
    ProtocolError,
    _HEADER_LEN_FMT,
    _HEADER_LEN_SIZE,
    _MAX_HEADER_SIZE,
    decode,
    encode,
)


class TestProtocolEncode(unittest.TestCase):
    """Tests for the encode() function."""

    def _make_header(self, **overrides: Any) -> dict[str, Any]:
        """Create a minimal valid header with optional overrides."""
        h: dict[str, Any] = {
            "room": "bedroom",
            "sample_rate": 16000,
            "channels": 1,
            "bit_depth": 16,
            "timestamp": time.time(),
            "wake_score": 0.85,
        }
        h.update(overrides)
        return h

    def test_encode_returns_bytes(self) -> None:
        """Encoded message is a bytes object."""
        msg = encode(self._make_header(), b"\x00" * 100)
        self.assertIsInstance(msg, bytes)

    def test_encode_starts_with_header_length(self) -> None:
        """First 4 bytes are big-endian u32 header length."""
        header = self._make_header()
        pcm = b"\x00" * 50
        msg = encode(header, pcm)
        stored_len = struct.unpack(_HEADER_LEN_FMT, msg[:_HEADER_LEN_SIZE])[0]
        # The JSON header follows the length prefix.
        header_json = json.dumps(header, separators=(",", ":")).encode("utf-8")
        self.assertEqual(stored_len, len(header_json))

    def test_encode_pcm_follows_header(self) -> None:
        """PCM data immediately follows the JSON header."""
        header = self._make_header()
        pcm = b"\x01\x02\x03\x04"
        msg = encode(header, pcm)
        self.assertTrue(msg.endswith(pcm))

    def test_encode_empty_pcm(self) -> None:
        """Encoding with empty PCM is valid (edge case)."""
        msg = encode(self._make_header(), b"")
        header_out, pcm_out = decode(msg)
        self.assertEqual(pcm_out, b"")
        self.assertEqual(header_out["room"], "bedroom")

    def test_encode_requires_room(self) -> None:
        """Missing 'room' field raises ValueError."""
        h = {"sample_rate": 16000}
        with self.assertRaises(ValueError):
            encode(h, b"\x00")

    def test_encode_requires_sample_rate(self) -> None:
        """Missing 'sample_rate' field raises ValueError."""
        h = {"room": "bedroom"}
        with self.assertRaises(ValueError):
            encode(h, b"\x00")

    def test_encode_preserves_extra_fields(self) -> None:
        """Extra header fields are preserved through encode/decode."""
        header = self._make_header(custom_field="hello", priority=42)
        msg = encode(header, b"\x00")
        header_out, _ = decode(msg)
        self.assertEqual(header_out["custom_field"], "hello")
        self.assertEqual(header_out["priority"], 42)


class TestProtocolDecode(unittest.TestCase):
    """Tests for the decode() function."""

    def _make_message(
        self, header: dict[str, Any], pcm: bytes,
    ) -> bytes:
        """Build a raw message manually for testing."""
        return encode(header, pcm)

    def test_round_trip(self) -> None:
        """Encode → decode preserves header and PCM exactly."""
        header = {
            "room": "living",
            "sample_rate": 16000,
            "channels": 1,
            "bit_depth": 16,
            "timestamp": 1711929600.123,
            "wake_score": 0.92,
        }
        pcm = bytes(range(256)) * 10  # 2560 bytes of PCM.
        msg = encode(header, pcm)
        h_out, pcm_out = decode(msg)
        self.assertEqual(h_out["room"], "living")
        self.assertEqual(h_out["sample_rate"], 16000)
        self.assertAlmostEqual(h_out["timestamp"], 1711929600.123)
        self.assertAlmostEqual(h_out["wake_score"], 0.92)
        self.assertEqual(pcm_out, pcm)

    def test_unicode_room_name(self) -> None:
        """Unicode room names survive round-trip."""
        header = {"room": "chambre \u00e0 coucher", "sample_rate": 16000}
        msg = encode(header, b"\x00")
        h_out, _ = decode(msg)
        self.assertEqual(h_out["room"], "chambre \u00e0 coucher")

    def test_hebrew_room_name(self) -> None:
        """Hebrew room names survive round-trip."""
        header = {"room": "\u05D7\u05D3\u05E8 \u05E9\u05D9\u05E0\u05D4", "sample_rate": 16000}
        msg = encode(header, b"\x00")
        h_out, _ = decode(msg)
        self.assertEqual(h_out["room"], "\u05D7\u05D3\u05E8 \u05E9\u05D9\u05E0\u05D4")

    def test_emoji_room_name(self) -> None:
        """Emoji room names survive round-trip."""
        header = {"room": "\U0001F3E0 Home", "sample_rate": 16000}
        msg = encode(header, b"\x00")
        h_out, _ = decode(msg)
        self.assertEqual(h_out["room"], "\U0001F3E0 Home")

    def test_large_pcm(self) -> None:
        """Large PCM payloads (~160 KB, typical 5s utterance) work."""
        pcm = b"\x42" * 160000
        header = {"room": "test", "sample_rate": 16000}
        msg = encode(header, pcm)
        _, pcm_out = decode(msg)
        self.assertEqual(len(pcm_out), 160000)
        self.assertEqual(pcm_out, pcm)


class TestProtocolErrors(unittest.TestCase):
    """Tests for error handling in decode()."""

    def test_empty_payload(self) -> None:
        """Empty payload raises ProtocolError."""
        with self.assertRaises(ProtocolError):
            decode(b"")

    def test_truncated_header_length(self) -> None:
        """Payload shorter than 4 bytes raises ProtocolError."""
        with self.assertRaises(ProtocolError):
            decode(b"\x00\x00\x00")

    def test_truncated_header(self) -> None:
        """Header length exceeds available bytes raises ProtocolError."""
        # Claim 1000 bytes of header but only provide 10.
        payload = struct.pack(_HEADER_LEN_FMT, 1000) + b"\x00" * 10
        with self.assertRaises(ProtocolError):
            decode(payload)

    def test_invalid_json(self) -> None:
        """Non-JSON header raises ProtocolError."""
        bad_header = b"not json at all"
        payload = struct.pack(_HEADER_LEN_FMT, len(bad_header)) + bad_header
        with self.assertRaises(ProtocolError):
            decode(payload)

    def test_json_array_not_object(self) -> None:
        """JSON array (not object) raises ProtocolError."""
        bad_header = b"[1, 2, 3]"
        payload = struct.pack(_HEADER_LEN_FMT, len(bad_header)) + bad_header
        with self.assertRaises(ProtocolError):
            decode(payload)

    def test_json_string_not_object(self) -> None:
        """JSON string raises ProtocolError."""
        bad_header = b'"just a string"'
        payload = struct.pack(_HEADER_LEN_FMT, len(bad_header)) + bad_header
        with self.assertRaises(ProtocolError):
            decode(payload)

    def test_header_too_large(self) -> None:
        """Header length exceeding _MAX_HEADER_SIZE raises ProtocolError."""
        payload = struct.pack(_HEADER_LEN_FMT, _MAX_HEADER_SIZE + 1) + b"\x00" * 100
        with self.assertRaises(ProtocolError):
            decode(payload)

    def test_zero_header_length(self) -> None:
        """Zero-length header (empty JSON) raises ProtocolError."""
        payload = struct.pack(_HEADER_LEN_FMT, 0)
        # Empty string is not valid JSON.
        with self.assertRaises(ProtocolError):
            decode(payload)

    def test_invalid_utf8(self) -> None:
        """Invalid UTF-8 in header raises ProtocolError."""
        bad_bytes = b"\xff\xfe\x00\x01"
        payload = struct.pack(_HEADER_LEN_FMT, len(bad_bytes)) + bad_bytes
        with self.assertRaises(ProtocolError):
            decode(payload)

    def test_header_length_exactly_payload_size(self) -> None:
        """Header consuming entire payload leaves empty PCM (valid)."""
        header = {"room": "test", "sample_rate": 16000}
        header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
        payload = struct.pack(_HEADER_LEN_FMT, len(header_bytes)) + header_bytes
        h_out, pcm_out = decode(payload)
        self.assertEqual(h_out["room"], "test")
        self.assertEqual(pcm_out, b"")


class TestProtocolRoundTrip(unittest.TestCase):
    """Integration tests for encode → decode consistency."""

    def test_multiple_rooms_distinct(self) -> None:
        """Messages from different rooms decode independently."""
        rooms = ["bedroom", "kitchen", "office", "living"]
        messages = []
        for room in rooms:
            header = {"room": room, "sample_rate": 16000}
            pcm = room.encode("utf-8") * 100  # Unique PCM per room.
            messages.append(encode(header, pcm))

        for i, msg in enumerate(messages):
            h, p = decode(msg)
            self.assertEqual(h["room"], rooms[i])
            self.assertEqual(p, rooms[i].encode("utf-8") * 100)

    def test_binary_pcm_preserved(self) -> None:
        """All 256 byte values survive round-trip in PCM."""
        pcm = bytes(range(256))
        header = {"room": "test", "sample_rate": 16000}
        msg = encode(header, pcm)
        _, pcm_out = decode(msg)
        self.assertEqual(pcm_out, pcm)

    def test_float_precision(self) -> None:
        """Float values in header maintain precision."""
        header = {
            "room": "test",
            "sample_rate": 16000,
            "timestamp": 1711929600.123456789,
            "wake_score": 0.123456789,
        }
        msg = encode(header, b"\x00")
        h_out, _ = decode(msg)
        # JSON float precision is limited but should be close.
        self.assertAlmostEqual(h_out["timestamp"], 1711929600.123456789, places=5)
        self.assertAlmostEqual(h_out["wake_score"], 0.123456789, places=5)


if __name__ == "__main__":
    unittest.main()
