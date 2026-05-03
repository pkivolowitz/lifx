"""Tests for utterance capture and RMS calculation."""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import struct
import unittest

from voice.satellite.capture import UtteranceCapture, compute_rms


class TestComputeRMS(unittest.TestCase):
    """Tests for the compute_rms() function."""

    def test_silence_is_zero(self) -> None:
        """All-zero PCM returns 0.0 RMS."""
        pcm = b"\x00\x00" * 100
        self.assertAlmostEqual(compute_rms(pcm), 0.0)

    def test_empty_is_zero(self) -> None:
        """Empty input returns 0.0."""
        self.assertAlmostEqual(compute_rms(b""), 0.0)

    def test_single_byte_is_zero(self) -> None:
        """Single byte (not a full sample) returns 0.0."""
        self.assertAlmostEqual(compute_rms(b"\xff"), 0.0)

    def test_max_amplitude(self) -> None:
        """Full-scale 16-bit samples produce high RMS."""
        # 32767 is max positive int16.
        pcm = struct.pack("<h", 32767) * 100
        rms = compute_rms(pcm)
        self.assertGreater(rms, 30000)

    def test_negative_samples(self) -> None:
        """Negative samples contribute to RMS (squared)."""
        pcm = struct.pack("<h", -10000) * 100
        rms = compute_rms(pcm)
        self.assertAlmostEqual(rms, 10000.0, delta=1)

    def test_mixed_signal(self) -> None:
        """Alternating positive/negative samples produce correct RMS."""
        samples = [1000, -1000] * 50
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        rms = compute_rms(pcm)
        self.assertAlmostEqual(rms, 1000.0, delta=1)

    def test_quiet_room(self) -> None:
        """Low-amplitude noise produces RMS in the expected range."""
        # Typical quiet room: 50-150 RMS.
        samples = [50, -30, 80, -60, 40, -20, 70, -50] * 20
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        rms = compute_rms(pcm)
        self.assertGreater(rms, 20)
        self.assertLess(rms, 200)


class TestUtteranceCapture(unittest.TestCase):
    """Tests for UtteranceCapture configuration and ring buffer."""

    def test_custom_params_translate_to_chunk_bounds(self) -> None:
        """Constructor converts seconds-based parameters into chunk
        counts that bound the capture loop, using the chosen sample
        rate / chunk size as the conversion basis.
        """
        # 16 kHz / 1280 samples per chunk → 12.5 chunks per second.
        cap = UtteranceCapture(
            sample_rate=16000,
            chunk_samples=1280,
            max_seconds=10.0,
            silence_timeout=3.0,
            silence_rms=100,
            min_seconds=0.5,
            pre_wake_seconds=0.5,
        )
        self.assertEqual(cap._sample_rate, 16000)
        self.assertEqual(cap._chunk_samples, 1280)
        self.assertEqual(cap._silence_rms, 100)
        self.assertEqual(cap._max_chunks, int(16000 / 1280 * 10.0))
        self.assertEqual(cap._silence_chunks, int(3.0 * 16000 / 1280))
        self.assertEqual(cap._min_chunks, int(0.5 * 16000 / 1280))
        self.assertEqual(
            cap._ring.maxlen, max(1, int(0.5 * 16000 / 1280)),
        )

    def test_ring_buffer_fills(self) -> None:
        """Feeding chunks into the ring buffer accumulates data."""
        cap = UtteranceCapture(
            sample_rate=16000,
            chunk_samples=1280,
            pre_wake_seconds=0.2,
        )
        chunk = b"\x00" * (1280 * 2)
        for _ in range(10):
            cap.feed_ring(chunk)
        # Ring buffer has a maxlen, so it doesn't grow unbounded.
        self.assertLessEqual(len(cap._ring), cap._ring.maxlen)

    def test_ring_buffer_maxlen(self) -> None:
        """Ring buffer respects its maximum length."""
        cap = UtteranceCapture(
            sample_rate=16000,
            chunk_samples=1280,
            pre_wake_seconds=0.2,
        )
        chunk = b"\x42" * (1280 * 2)
        # Feed more than the buffer can hold.
        for _ in range(100):
            cap.feed_ring(chunk)
        self.assertEqual(len(cap._ring), cap._ring.maxlen)

    def test_zero_pre_wake(self) -> None:
        """Zero pre-wake seconds still creates a valid ring buffer."""
        cap = UtteranceCapture(pre_wake_seconds=0.0)
        # maxlen should be at least 1.
        self.assertGreaterEqual(cap._ring.maxlen, 1)


class TestSilenceThresholds(unittest.TestCase):
    """Tests for silence detection thresholds."""

    def test_below_threshold_is_silence(self) -> None:
        """RMS below threshold is detected as silence."""
        threshold = 50
        samples = [10, -10, 5, -5] * 320
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        rms = compute_rms(pcm)
        self.assertLess(rms, threshold)

    def test_above_threshold_is_speech(self) -> None:
        """RMS above threshold is detected as speech."""
        threshold = 50
        samples = [500, -500, 300, -300] * 320
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        rms = compute_rms(pcm)
        self.assertGreater(rms, threshold)

    def test_laptop_mic_typical_range(self) -> None:
        """Typical laptop mic speech levels exceed low threshold."""
        # Laptop mics at normal volume: 200-2000 RMS.
        threshold = 50
        samples = [500] * 1280
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        rms = compute_rms(pcm)
        self.assertGreater(rms, threshold)


if __name__ == "__main__":
    unittest.main()
