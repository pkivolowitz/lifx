"""Unit tests for media.SignalBus and media.extractors.AudioExtractor.

Tests the core signal bus operations (read, write, register, list)
and the audio extractor pipeline (PCM decode, FFT, band binning,
beat detection, spectral centroid, normalization).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
import struct
import time
import unittest

from media import SignalBus, SignalMeta, MediaManager
from media.extractors import (
    AudioExtractor, _PeakTracker, SignalExtractor,
    DEFAULT_WINDOW_SIZE, DEFAULT_BAND_COUNT,
    BYTES_PER_SAMPLE, PCM_16_MAX,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TWO_PI: float = 2.0 * math.pi


def generate_pcm_sine(freq_hz: float, sample_rate: int, n_samples: int,
                      amplitude: float = 0.8) -> bytes:
    """Generate 16-bit signed PCM bytes for a pure sine wave.

    Args:
        freq_hz:     Frequency in Hz.
        sample_rate: Sample rate in Hz.
        n_samples:   Number of samples.
        amplitude:   Peak amplitude [0.0, 1.0].

    Returns:
        Raw PCM bytes (little-endian, signed 16-bit).
    """
    samples: list[int] = []
    for i in range(n_samples):
        val: float = amplitude * math.sin(TWO_PI * freq_hz * i / sample_rate)
        clamped: int = max(-32768, min(32767, int(val * 32767)))
        samples.append(clamped)
    return struct.pack(f"<{n_samples}h", *samples)


def generate_pcm_silence(n_samples: int) -> bytes:
    """Generate silent PCM bytes.

    Args:
        n_samples: Number of zero samples.

    Returns:
        Raw PCM bytes (all zeros).
    """
    return b"\x00" * (n_samples * BYTES_PER_SAMPLE)


# ---------------------------------------------------------------------------
# SignalBus Tests
# ---------------------------------------------------------------------------

class TestSignalBusReadWrite(unittest.TestCase):
    """Basic read/write operations."""

    def setUp(self) -> None:
        """Create a fresh bus for each test."""
        self.bus: SignalBus = SignalBus()

    def test_read_default(self) -> None:
        """Reading a nonexistent signal returns the default."""
        self.assertEqual(self.bus.read("nonexistent"), 0.0)
        self.assertEqual(self.bus.read("nonexistent", 42.0), 42.0)

    def test_write_scalar(self) -> None:
        """Write and read back a scalar signal."""
        self.bus.write("test:signal", 0.75)
        self.assertAlmostEqual(self.bus.read("test:signal"), 0.75)

    def test_write_array(self) -> None:
        """Write and read back an array signal."""
        bands: list[float] = [0.1, 0.2, 0.3, 0.4]
        self.bus.write("test:bands", bands)
        result = self.bus.read("test:bands")
        self.assertEqual(result, bands)

    def test_overwrite(self) -> None:
        """Writing overwrites the previous value."""
        self.bus.write("x", 1.0)
        self.bus.write("x", 2.0)
        self.assertAlmostEqual(self.bus.read("x"), 2.0)

    def test_read_many(self) -> None:
        """read_many returns multiple signals atomically."""
        self.bus.write("a", 0.1)
        self.bus.write("b", 0.2)
        result = self.bus.read_many(["a", "b", "missing"])
        self.assertAlmostEqual(result["a"], 0.1)
        self.assertAlmostEqual(result["b"], 0.2)
        self.assertAlmostEqual(result["missing"], 0.0)

    def test_read_many_custom_default(self) -> None:
        """read_many respects custom default value."""
        result = self.bus.read_many(["x"], default=-1.0)
        self.assertAlmostEqual(result["x"], -1.0)


class TestSignalBusRegistration(unittest.TestCase):
    """Signal registration and discovery."""

    def setUp(self) -> None:
        """Create a fresh bus for each test."""
        self.bus: SignalBus = SignalBus()

    def test_register_scalar(self) -> None:
        """Registering a scalar signal initializes it to 0.0."""
        self.bus.register("test:scalar", SignalMeta(signal_type="scalar"))
        self.assertAlmostEqual(self.bus.read("test:scalar"), 0.0)

    def test_register_array(self) -> None:
        """Registering an array signal initializes it to empty list."""
        self.bus.register("test:array", SignalMeta(signal_type="array"))
        self.assertEqual(self.bus.read("test:array"), [])

    def test_register_preserves_existing(self) -> None:
        """Re-registering does not overwrite an existing value."""
        self.bus.write("test:val", 0.5)
        self.bus.register("test:val", SignalMeta())
        self.assertAlmostEqual(self.bus.read("test:val"), 0.5)

    def test_unregister(self) -> None:
        """Unregistering removes both value and metadata."""
        self.bus.register("test:rm", SignalMeta())
        self.bus.write("test:rm", 1.0)
        self.bus.unregister("test:rm")
        self.assertAlmostEqual(self.bus.read("test:rm"), 0.0)  # default
        self.assertEqual(len(self.bus.list_signals()), 0)

    def test_list_signals(self) -> None:
        """list_signals returns metadata for registered signals."""
        self.bus.register("a:b:c", SignalMeta(
            signal_type="scalar",
            description="Test signal",
            source_name="a",
        ))
        signals = self.bus.list_signals()
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["name"], "a:b:c")
        self.assertEqual(signals[0]["type"], "scalar")
        self.assertEqual(signals[0]["source"], "a")

    def test_signal_names(self) -> None:
        """signal_names returns sorted names."""
        self.bus.register("z:sig", SignalMeta())
        self.bus.register("a:sig", SignalMeta())
        names = self.bus.signal_names()
        self.assertEqual(names, ["a:sig", "z:sig"])

    def test_list_signals_sorted(self) -> None:
        """list_signals returns entries sorted by name."""
        self.bus.register("c:sig", SignalMeta())
        self.bus.register("a:sig", SignalMeta())
        self.bus.register("b:sig", SignalMeta())
        names = [s["name"] for s in self.bus.list_signals()]
        self.assertEqual(names, ["a:sig", "b:sig", "c:sig"])


class TestSignalMeta(unittest.TestCase):
    """SignalMeta dataclass behavior."""

    def test_defaults(self) -> None:
        """Default values are sensible."""
        meta = SignalMeta()
        self.assertEqual(meta.signal_type, "scalar")
        self.assertAlmostEqual(meta.min_val, 0.0)
        self.assertAlmostEqual(meta.max_val, 1.0)

    def test_to_dict(self) -> None:
        """to_dict produces JSON-safe output."""
        meta = SignalMeta(
            signal_type="array",
            description="Bands",
            source_name="cam1",
        )
        d = meta.to_dict()
        self.assertEqual(d["type"], "array")
        self.assertEqual(d["description"], "Bands")
        self.assertEqual(d["source"], "cam1")


# ---------------------------------------------------------------------------
# PeakTracker Tests
# ---------------------------------------------------------------------------

class TestPeakTracker(unittest.TestCase):
    """Adaptive peak normalization."""

    def test_first_value_is_one(self) -> None:
        """First nonzero value normalizes to 1.0."""
        pt = _PeakTracker()
        result = pt.update(0.5)
        self.assertAlmostEqual(result, 1.0)

    def test_smaller_value_below_one(self) -> None:
        """A value below peak normalizes to less than 1.0."""
        pt = _PeakTracker()
        pt.update(1.0)
        result = pt.update(0.5)
        self.assertLess(result, 1.0)

    def test_zero_returns_zero(self) -> None:
        """Zero value returns zero (after non-zero peak)."""
        pt = _PeakTracker()
        pt.update(1.0)
        result = pt.update(0.0)
        self.assertAlmostEqual(result, 0.0)

    def test_clamped_to_one(self) -> None:
        """Output never exceeds 1.0."""
        pt = _PeakTracker()
        for _ in range(100):
            result = pt.update(999.0)
            self.assertLessEqual(result, 1.0)


# ---------------------------------------------------------------------------
# AudioExtractor Tests
# ---------------------------------------------------------------------------

class TestAudioExtractorSignalRegistration(unittest.TestCase):
    """AudioExtractor registers correct signals on the bus."""

    def test_signal_count(self) -> None:
        """AudioExtractor registers exactly 8 signals."""
        bus = SignalBus()
        ext = AudioExtractor("test", 16000, bus)
        names = ext.get_signal_names()
        self.assertEqual(len(names), 8)

    def test_signal_names(self) -> None:
        """Signal names follow the {src}:audio:{signal} convention."""
        bus = SignalBus()
        ext = AudioExtractor("cam1", 16000, bus)
        names = ext.get_signal_names()
        expected_suffixes = [
            "bands", "bass", "mid", "treble",
            "rms", "energy", "beat", "centroid",
        ]
        for suffix in expected_suffixes:
            full_name = f"cam1:audio:{suffix}"
            self.assertIn(full_name, names)

    def test_signals_registered_on_bus(self) -> None:
        """All signals appear in bus.list_signals() after construction."""
        bus = SignalBus()
        AudioExtractor("src", 16000, bus)
        registered = bus.signal_names()
        self.assertIn("src:audio:bass", registered)
        self.assertIn("src:audio:bands", registered)


class TestAudioExtractorProcessing(unittest.TestCase):
    """AudioExtractor signal processing pipeline."""

    def test_silence_produces_zero_signals(self) -> None:
        """Silent input produces near-zero signal values."""
        bus = SignalBus()
        ext = AudioExtractor("test", 16000, bus, window_size=1024)
        silence = generate_pcm_silence(1024)
        ext.process(silence)

        self.assertAlmostEqual(bus.read("test:audio:rms"), 0.0, places=3)
        self.assertAlmostEqual(bus.read("test:audio:bass"), 0.0, places=3)
        self.assertAlmostEqual(bus.read("test:audio:beat"), 0.0, places=3)

    def test_sine_produces_nonzero_signals(self) -> None:
        """A loud sine wave produces nonzero band energy."""
        bus = SignalBus()
        ext = AudioExtractor("test", 16000, bus, window_size=1024)
        pcm = generate_pcm_sine(440.0, 16000, 1024)
        ext.process(pcm)

        rms = bus.read("test:audio:rms")
        self.assertGreater(rms, 0.0)

        energy = bus.read("test:audio:energy")
        self.assertGreater(energy, 0.0)

    def test_bands_correct_length(self) -> None:
        """Bands array has the configured number of elements."""
        bus = SignalBus()
        n_bands = 16
        ext = AudioExtractor("test", 16000, bus, window_size=1024,
                             band_count=n_bands)
        pcm = generate_pcm_sine(440.0, 16000, 1024)
        ext.process(pcm)

        bands = bus.read("test:audio:bands")
        self.assertIsInstance(bands, list)
        self.assertEqual(len(bands), n_bands)

    def test_bands_normalized(self) -> None:
        """All band values are in [0.0, 1.0]."""
        bus = SignalBus()
        ext = AudioExtractor("test", 16000, bus, window_size=1024)
        pcm = generate_pcm_sine(1000.0, 16000, 1024)
        ext.process(pcm)

        bands = bus.read("test:audio:bands")
        for b in bands:
            self.assertGreaterEqual(b, 0.0)
            self.assertLessEqual(b, 1.0)

    def test_bass_high_for_low_freq(self) -> None:
        """A low-frequency sine produces higher bass than treble."""
        bus = SignalBus()
        ext = AudioExtractor("test", 16000, bus, window_size=1024,
                             smoothing=0.0)
        pcm = generate_pcm_sine(80.0, 16000, 1024)
        ext.process(pcm)

        bass = bus.read("test:audio:bass")
        treble = bus.read("test:audio:treble")
        self.assertGreater(bass, treble,
                           f"bass={bass} should exceed treble={treble} for 80 Hz")

    def test_treble_high_for_high_freq(self) -> None:
        """A high-frequency sine produces higher treble than bass."""
        bus = SignalBus()
        ext = AudioExtractor("test", 16000, bus, window_size=1024,
                             smoothing=0.0)
        pcm = generate_pcm_sine(6000.0, 16000, 1024)
        ext.process(pcm)

        bass = bus.read("test:audio:bass")
        treble = bus.read("test:audio:treble")
        self.assertGreater(treble, bass,
                           f"treble={treble} should exceed bass={bass} for 6 kHz")

    def test_centroid_bounded(self) -> None:
        """Spectral centroid is always in [0.0, 1.0]."""
        bus = SignalBus()
        ext = AudioExtractor("test", 16000, bus, window_size=1024)
        pcm = generate_pcm_sine(3000.0, 16000, 1024)
        ext.process(pcm)

        centroid = bus.read("test:audio:centroid")
        self.assertGreaterEqual(centroid, 0.0)
        self.assertLessEqual(centroid, 1.0)

    def test_multiple_windows_accumulate(self) -> None:
        """Processing 2x window_size samples triggers 2 analyses."""
        bus = SignalBus()
        ext = AudioExtractor("test", 16000, bus, window_size=512)
        pcm = generate_pcm_sine(440.0, 16000, 1024)
        ext.process(pcm)

        # After 1024 samples with window_size=512, we should have
        # processed 2 windows and have non-zero values.
        rms = bus.read("test:audio:rms")
        self.assertGreater(rms, 0.0)

    def test_partial_chunk_buffered(self) -> None:
        """A chunk smaller than window_size is buffered, not dropped."""
        bus = SignalBus()
        ext = AudioExtractor("test", 16000, bus, window_size=1024)

        # Send half a window.
        pcm_half = generate_pcm_sine(440.0, 16000, 512)
        ext.process(pcm_half)

        # RMS should still be zero (no complete window yet).
        bands = bus.read("test:audio:bands")
        # Bands might be empty list (initial registered value).
        self.assertTrue(
            bands == [] or all(b == 0.0 for b in bands),
            "No analysis should have run yet",
        )

        # Send the other half.
        ext.process(pcm_half)
        rms = bus.read("test:audio:rms")
        self.assertGreater(rms, 0.0, "Second half should complete the window")

    def test_empty_chunk_ignored(self) -> None:
        """An empty chunk does not crash."""
        bus = SignalBus()
        ext = AudioExtractor("test", 16000, bus)
        ext.process(b"")  # Should not raise.


class TestAudioExtractorBeatDetection(unittest.TestCase):
    """Beat detection in the audio extractor."""

    def test_beat_on_loud_burst(self) -> None:
        """A sudden loud burst after silence triggers a beat."""
        bus = SignalBus()
        ext = AudioExtractor("test", 16000, bus, window_size=1024,
                             smoothing=0.0)

        # Feed several windows of silence to build baseline.
        silence = generate_pcm_silence(1024)
        for _ in range(5):
            ext.process(silence)

        # Feed a loud burst.
        loud = generate_pcm_sine(200.0, 16000, 1024, amplitude=0.9)
        ext.process(loud)

        beat = bus.read("test:audio:beat")
        self.assertGreater(beat, 0.0,
                           "Loud burst after silence should trigger beat")


# ---------------------------------------------------------------------------
# MediaManager Tests
# ---------------------------------------------------------------------------

class TestMediaManager(unittest.TestCase):
    """MediaManager lifecycle basics."""

    def test_bus_property(self) -> None:
        """MediaManager exposes a SignalBus."""
        mm = MediaManager()
        self.assertIsInstance(mm.bus, SignalBus)

    def test_get_status_empty(self) -> None:
        """Empty manager returns empty status."""
        mm = MediaManager()
        status = mm.get_status()
        self.assertEqual(status["sources"], [])
        self.assertEqual(status["signal_count"], 0)

    def test_extract_source_name(self) -> None:
        """extract_source_name parses correctly."""
        mm = MediaManager()
        self.assertEqual(mm.extract_source_name("backyard:audio:bass"),
                         "backyard")
        self.assertEqual(mm.extract_source_name("x:y"), "x")
        self.assertIsNone(mm.extract_source_name("noseparator"))

    def test_get_source_names_empty(self) -> None:
        """No sources returns empty list."""
        mm = MediaManager()
        self.assertEqual(mm.get_source_names(), [])

    def test_shutdown_idempotent(self) -> None:
        """Shutdown on empty manager doesn't crash."""
        mm = MediaManager()
        mm.shutdown()  # Should not raise.


if __name__ == "__main__":
    unittest.main()
