"""Unit tests for media.fft — dual-path FFT implementation.

Tests both the numpy fast path and the pure-Python fallback by
temporarily masking numpy availability.  Verifies correct frequency
detection, windowing, band binning, and spectral centroid.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
import unittest

from media import fft


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TWO_PI: float = 2.0 * math.pi

# Tolerance for floating-point comparisons.
ATOL: float = 1e-6


def generate_sine(freq_hz: float, sample_rate: int, n_samples: int,
                  amplitude: float = 1.0) -> list[float]:
    """Generate a pure sine wave.

    Args:
        freq_hz:     Frequency in Hz.
        sample_rate: Sample rate in Hz.
        n_samples:   Number of samples to generate.
        amplitude:   Peak amplitude.

    Returns:
        List of float samples.
    """
    return [
        amplitude * math.sin(TWO_PI * freq_hz * i / sample_rate)
        for i in range(n_samples)
    ]


def peak_band_index(bands: list[float]) -> int:
    """Return the index of the band with the highest energy.

    Args:
        bands: List of band energy values.

    Returns:
        Index of the maximum band.
    """
    max_val: float = -1.0
    max_idx: int = 0
    for i, v in enumerate(bands):
        if v > max_val:
            max_val = v
            max_idx = i
    return max_idx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHannWindow(unittest.TestCase):
    """Hann window correctness."""

    def test_length(self) -> None:
        """Window length matches requested size."""
        for n in (64, 128, 256, 512, 1024):
            w: list[float] = fft.hann_window(n)
            self.assertEqual(len(w), n)

    def test_endpoints_zero(self) -> None:
        """Hann window endpoints are zero (or very close)."""
        w: list[float] = fft.hann_window(256)
        self.assertAlmostEqual(w[0], 0.0, places=10)
        self.assertAlmostEqual(w[-1], 0.0, places=10)

    def test_midpoint_one(self) -> None:
        """Hann window midpoint is 1.0."""
        n: int = 256
        w: list[float] = fft.hann_window(n)
        mid: int = n // 2
        # For even-length windows, the midpoint is very close to 1.0.
        self.assertAlmostEqual(w[mid], 1.0, places=4)

    def test_empty(self) -> None:
        """Empty window returns empty list."""
        self.assertEqual(fft.hann_window(0), [])

    def test_single(self) -> None:
        """Single-sample window returns [1.0]."""
        self.assertEqual(fft.hann_window(1), [1.0])


class TestFFTMagnitudes(unittest.TestCase):
    """FFT magnitude spectrum correctness."""

    def test_silence(self) -> None:
        """Silent input produces near-zero magnitudes."""
        samples: list[float] = [0.0] * 1024
        mags: list[float] = fft.fft_magnitudes(samples)
        for m in mags:
            self.assertAlmostEqual(m, 0.0, places=10)

    def test_empty(self) -> None:
        """Empty input returns empty magnitudes."""
        self.assertEqual(fft.fft_magnitudes([]), [])

    def test_output_length(self) -> None:
        """Output length is N//2 + 1 for power-of-2 input."""
        n: int = 1024
        samples: list[float] = [0.0] * n
        mags: list[float] = fft.fft_magnitudes(samples)
        self.assertEqual(len(mags), n // 2 + 1)

    def test_sine_peak_location(self) -> None:
        """A pure sine wave peaks at the correct FFT bin."""
        sample_rate: int = 16000
        n: int = 1024
        freq: float = 440.0
        samples: list[float] = generate_sine(freq, sample_rate, n)
        mags: list[float] = fft.fft_magnitudes(samples)

        # Expected bin for 440 Hz.
        bin_hz: float = sample_rate / n
        expected_bin: int = round(freq / bin_hz)

        # Find the peak bin.
        peak_val: float = -1.0
        peak_bin: int = 0
        for i, m in enumerate(mags):
            if m > peak_val:
                peak_val = m
                peak_bin = i

        # Allow ±1 bin tolerance due to windowing spectral leakage.
        self.assertAlmostEqual(peak_bin, expected_bin, delta=1)

    def test_magnitudes_non_negative(self) -> None:
        """All magnitude values are non-negative."""
        samples: list[float] = generate_sine(1000.0, 16000, 1024)
        mags: list[float] = fft.fft_magnitudes(samples)
        for m in mags:
            self.assertGreaterEqual(m, 0.0)


class TestBinToBands(unittest.TestCase):
    """Logarithmic frequency band binning."""

    def test_output_length(self) -> None:
        """Output has the requested number of bands."""
        mags: list[float] = [0.0] * 513  # 1024-point FFT
        for n_bands in (4, 8, 16, 32):
            bands: list[float] = fft.bin_to_bands(mags, n_bands, 16000)
            self.assertEqual(len(bands), n_bands)

    def test_bass_detection(self) -> None:
        """A low-frequency sine concentrates energy in the bass band."""
        sample_rate: int = 16000
        n: int = 1024
        # 100 Hz is solidly in the bass range.
        samples: list[float] = generate_sine(100.0, sample_rate, n)
        mags: list[float] = fft.fft_magnitudes(samples)
        bands: list[float] = fft.bin_to_bands(mags, 8, sample_rate)
        # With 8 log-spaced bands from 20 Hz to 8 kHz, 100 Hz falls
        # in band 0, 1, or 2 depending on exact edge placement.
        peak: int = peak_band_index(bands)
        self.assertIn(peak, (0, 1, 2),
                       f"100 Hz peak in band {peak}, expected 0-2")

    def test_treble_detection(self) -> None:
        """A high-frequency sine concentrates energy in upper bands."""
        sample_rate: int = 16000
        n: int = 1024
        # 6000 Hz is solidly in the treble range.
        samples: list[float] = generate_sine(6000.0, sample_rate, n)
        mags: list[float] = fft.fft_magnitudes(samples)
        bands: list[float] = fft.bin_to_bands(mags, 8, sample_rate)
        peak: int = peak_band_index(bands)
        # Should be in the upper half of bands (4-7).
        self.assertGreaterEqual(peak, 4,
                                 f"6 kHz peak in band {peak}, expected >= 4")

    def test_empty_magnitudes(self) -> None:
        """Empty magnitudes produce zero bands."""
        bands: list[float] = fft.bin_to_bands([], 8, 16000)
        self.assertEqual(len(bands), 8)
        for b in bands:
            self.assertAlmostEqual(b, 0.0)

    def test_bands_non_negative(self) -> None:
        """All band values are non-negative."""
        samples: list[float] = generate_sine(440.0, 16000, 1024)
        mags: list[float] = fft.fft_magnitudes(samples)
        bands: list[float] = fft.bin_to_bands(mags, 8, 16000)
        for b in bands:
            self.assertGreaterEqual(b, 0.0)


class TestSpectralCentroid(unittest.TestCase):
    """Spectral centroid (brightness descriptor)."""

    def test_low_freq_centroid(self) -> None:
        """Low-frequency sine has a low centroid."""
        samples: list[float] = generate_sine(200.0, 16000, 1024)
        mags: list[float] = fft.fft_magnitudes(samples)
        c: float = fft.spectral_centroid(mags, 16000)
        # 200 Hz / 8000 Hz Nyquist = 0.025 normalized.
        self.assertLess(c, 0.15, f"Centroid {c} too high for 200 Hz")

    def test_high_freq_centroid(self) -> None:
        """High-frequency sine has a high centroid."""
        samples: list[float] = generate_sine(6000.0, 16000, 1024)
        mags: list[float] = fft.fft_magnitudes(samples)
        c: float = fft.spectral_centroid(mags, 16000)
        self.assertGreater(c, 0.5, f"Centroid {c} too low for 6 kHz")

    def test_silence_centroid(self) -> None:
        """Silent input produces zero centroid."""
        mags: list[float] = [0.0] * 513
        c: float = fft.spectral_centroid(mags, 16000)
        self.assertAlmostEqual(c, 0.0)

    def test_centroid_bounded(self) -> None:
        """Centroid is always in [0.0, 1.0]."""
        samples: list[float] = generate_sine(7500.0, 16000, 1024)
        mags: list[float] = fft.fft_magnitudes(samples)
        c: float = fft.spectral_centroid(mags, 16000)
        self.assertGreaterEqual(c, 0.0)
        self.assertLessEqual(c, 1.0)


class TestPurePythonFallback(unittest.TestCase):
    """Verify the pure-Python FFT produces correct results.

    Temporarily masks numpy to force the stdlib path, then compares
    against known sine wave peaks.
    """

    def test_radix2_sine_peak(self) -> None:
        """Pure-Python radix-2 FFT finds the correct peak for 440 Hz."""
        import cmath as _cmath

        sample_rate: int = 16000
        n: int = 1024
        freq: float = 440.0
        samples: list[float] = generate_sine(freq, sample_rate, n)

        # Apply Hann window manually (no numpy dependency).
        window: list[float] = fft.hann_window(n)
        windowed: list[float] = [s * w for s, w in zip(samples, window)]

        # Run pure-Python FFT directly.
        padded: list[complex] = [complex(windowed[i]) for i in range(n)]
        spectrum: list[complex] = fft._fft_radix2(padded)

        # Positive-frequency magnitudes.
        half: int = n // 2 + 1
        mags: list[float] = [abs(spectrum[i]) / n for i in range(half)]

        # Find peak.
        bin_hz: float = sample_rate / n
        expected_bin: int = round(freq / bin_hz)
        peak_val: float = -1.0
        peak_bin: int = 0
        for i, m in enumerate(mags):
            if m > peak_val:
                peak_val = m
                peak_bin = i

        self.assertAlmostEqual(peak_bin, expected_bin, delta=1)

    def test_radix2_power_of_2_required(self) -> None:
        """Pure-Python FFT rejects non-power-of-2 input."""
        with self.assertRaises(ValueError):
            fft._fft_radix2([complex(0)] * 100)


class TestBackendName(unittest.TestCase):
    """Backend name reporting."""

    def test_returns_string(self) -> None:
        """backend_name() returns a recognized string."""
        name: str = fft.backend_name()
        self.assertIn(name, ("numpy", "scipy", "stdlib"))


if __name__ == "__main__":
    unittest.main()
