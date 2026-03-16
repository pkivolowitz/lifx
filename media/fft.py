"""Fast Fourier Transform with dual-path implementation.

Provides a unified API for FFT-based spectral analysis.  When numpy is
available (``pip install numpy``), the fast C-backed ``np.fft.rfft`` is
used (~0.1 ms for 1024 samples).  Otherwise, a pure-Python radix-2
Cooley-Tukey implementation serves as a fallback (~2-3 ms on a Pi 4).

The caller never needs to know which backend is active — the public
functions have identical signatures and return plain Python lists.

Optional scipy integration (Tier 2) adds advanced window functions and
spectral descriptors (centroid, rolloff) when ``scipy.signal`` is present.

Exports:
    fft_magnitudes  — windowed FFT → magnitude spectrum (list[float])
    bin_to_bands    — magnitude spectrum → logarithmic frequency bands
    hann_window     — precomputed Hann window coefficients
    backend_name    — "numpy", "scipy", or "stdlib" (for diagnostics)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import cmath
import math
from functools import lru_cache
from typing import Optional

# ---------------------------------------------------------------------------
# Optional dependency detection
# ---------------------------------------------------------------------------

try:
    import numpy as np
    _HAS_NUMPY: bool = True
except ImportError:
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

try:
    import scipy.signal as _scipy_signal
    _HAS_SCIPY: bool = True
except ImportError:
    _scipy_signal = None  # type: ignore[assignment]
    _HAS_SCIPY = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum FFT window size (must be power of 2).
MIN_WINDOW: int = 64

# Maximum FFT window size.
MAX_WINDOW: int = 8192

# Default number of frequency bands for logarithmic binning.
DEFAULT_BAND_COUNT: int = 8

# Minimum frequency for band binning (Hz).  Below this is subsonic rumble.
MIN_FREQ_HZ: float = 20.0

# Small epsilon to prevent log(0) in band edge computation.
LOG_EPSILON: float = 1e-10


def backend_name() -> str:
    """Return the name of the active FFT backend.

    Returns:
        ``"scipy"`` if scipy is available, ``"numpy"`` if only numpy is
        available, or ``"stdlib"`` for the pure-Python fallback.
    """
    if _HAS_SCIPY:
        return "scipy"
    if _HAS_NUMPY:
        return "numpy"
    return "stdlib"


# ---------------------------------------------------------------------------
# Hann window
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8)
def hann_window(n: int) -> list[float]:
    """Return a Hann (raised cosine) window of length *n*.

    The window is symmetric and suitable for spectral analysis.  Results
    are cached so repeated calls with the same *n* are free.

    Args:
        n: Window length in samples.

    Returns:
        List of *n* float coefficients in [0.0, 1.0].
    """
    if n < 1:
        return []
    if _HAS_NUMPY:
        return np.hanning(n).tolist()
    # Pure-Python Hann window: w[k] = 0.5 * (1 - cos(2πk / (N-1)))
    if n == 1:
        return [1.0]
    factor: float = 2.0 * math.pi / (n - 1)
    return [0.5 * (1.0 - math.cos(factor * k)) for k in range(n)]


# ---------------------------------------------------------------------------
# Pure-Python radix-2 Cooley-Tukey FFT
# ---------------------------------------------------------------------------

def _fft_radix2(x: list[complex]) -> list[complex]:
    """Compute the DFT of *x* using the radix-2 Cooley-Tukey algorithm.

    The input length **must** be a power of 2.  This is the pure-Python
    fallback used when numpy is not installed.

    Args:
        x: Input samples as complex numbers.

    Returns:
        List of complex frequency-domain coefficients.

    Raises:
        ValueError: If ``len(x)`` is not a power of 2.
    """
    n: int = len(x)
    if n <= 1:
        return list(x)
    if n & (n - 1) != 0:
        raise ValueError(f"FFT length must be a power of 2, got {n}")

    # Bit-reversal permutation.
    bits: int = n.bit_length() - 1
    result: list[complex] = [complex(0)] * n
    for i in range(n):
        rev: int = 0
        tmp: int = i
        for _ in range(bits):
            rev = (rev << 1) | (tmp & 1)
            tmp >>= 1
        result[rev] = x[i]

    # Butterfly passes.
    size: int = 2
    while size <= n:
        half: int = size // 2
        angle_step: float = -2.0 * math.pi / size
        for start in range(0, n, size):
            angle: float = 0.0
            for j in range(half):
                w: complex = cmath.exp(complex(0, angle))
                even: complex = result[start + j]
                odd: complex = result[start + j + half] * w
                result[start + j] = even + odd
                result[start + j + half] = even - odd
                angle += angle_step
        size *= 2

    return result


def _next_power_of_2(n: int) -> int:
    """Return the smallest power of 2 >= *n*.

    Args:
        n: Input integer.

    Returns:
        Power of 2 >= *n*.
    """
    if n <= 0:
        return 1
    p: int = 1
    while p < n:
        p <<= 1
    return p


# ---------------------------------------------------------------------------
# Public API: fft_magnitudes
# ---------------------------------------------------------------------------

def fft_magnitudes(samples: list[float],
                   window: Optional[list[float]] = None) -> list[float]:
    """Compute the magnitude spectrum of real-valued *samples*.

    Applies a Hann window (unless *window* is provided), runs the FFT,
    and returns the magnitude of each positive-frequency bin.  The DC
    component (bin 0) is included; the Nyquist bin is the last element.

    Args:
        samples: Raw PCM samples as floats (typically [-1.0, 1.0]).
        window:  Optional pre-computed window coefficients.  If ``None``,
                 a Hann window matching ``len(samples)`` is used.

    Returns:
        List of ``N // 2 + 1`` magnitude values (non-negative floats)
        where *N* is the (possibly zero-padded) FFT length.
    """
    n: int = len(samples)
    if n == 0:
        return []

    # Apply window.
    if window is None:
        window = hann_window(n)
    windowed: list[float] = [s * w for s, w in zip(samples, window)]

    if _HAS_NUMPY:
        # numpy fast path: real FFT returns only positive frequencies.
        arr = np.array(windowed, dtype=np.float64)
        fft_len: int = _next_power_of_2(n)
        spectrum = np.fft.rfft(arr, n=fft_len)
        mags = np.abs(spectrum)
        # Normalize by window length for consistent amplitude.
        return (mags / n).tolist()

    # Pure-Python fallback.
    fft_len = _next_power_of_2(n)
    # Zero-pad to power of 2.
    padded: list[complex] = [complex(windowed[i] if i < n else 0.0)
                             for i in range(fft_len)]
    spectrum_full: list[complex] = _fft_radix2(padded)
    # Only positive frequencies (DC through Nyquist).
    half: int = fft_len // 2 + 1
    inv_n: float = 1.0 / n
    return [abs(spectrum_full[i]) * inv_n for i in range(half)]


# ---------------------------------------------------------------------------
# Public API: bin_to_bands
# ---------------------------------------------------------------------------

def bin_to_bands(magnitudes: list[float],
                 band_count: int = DEFAULT_BAND_COUNT,
                 sample_rate: int = 16000) -> list[float]:
    """Map a magnitude spectrum into logarithmically-spaced frequency bands.

    Each band spans a range of FFT bins whose center frequencies are
    distributed on a log scale from ``MIN_FREQ_HZ`` to the Nyquist
    frequency.  The value of each band is the **mean** magnitude of its
    constituent bins, giving a perceptually even energy distribution.

    Args:
        magnitudes: Positive-frequency magnitudes from :func:`fft_magnitudes`.
        band_count: Number of output bands (default 8).
        sample_rate: Sample rate in Hz (default 16000).

    Returns:
        List of *band_count* floats representing per-band energy.
    """
    n_bins: int = len(magnitudes)
    if n_bins == 0 or band_count < 1:
        return [0.0] * max(band_count, 0)

    nyquist: float = sample_rate / 2.0
    # FFT bin width in Hz.
    # n_bins = N//2 + 1, so N = (n_bins - 1) * 2.
    fft_len: int = (n_bins - 1) * 2
    if fft_len == 0:
        return [0.0] * band_count
    bin_hz: float = sample_rate / fft_len

    # Logarithmic band edges from MIN_FREQ_HZ to Nyquist.
    log_min: float = math.log(max(MIN_FREQ_HZ, LOG_EPSILON))
    log_max: float = math.log(max(nyquist, MIN_FREQ_HZ + 1.0))
    edges: list[float] = [
        math.exp(log_min + (log_max - log_min) * i / band_count)
        for i in range(band_count + 1)
    ]

    bands: list[float] = []
    for b in range(band_count):
        lo_bin: int = max(0, int(edges[b] / bin_hz))
        hi_bin: int = min(n_bins - 1, int(edges[b + 1] / bin_hz))
        if hi_bin < lo_bin:
            hi_bin = lo_bin
        # Mean magnitude across the band.
        count: int = hi_bin - lo_bin + 1
        total: float = 0.0
        for i in range(lo_bin, hi_bin + 1):
            total += magnitudes[i]
        bands.append(total / count if count > 0 else 0.0)

    return bands


# ---------------------------------------------------------------------------
# Scipy-enhanced spectral descriptors (Tier 2)
# ---------------------------------------------------------------------------

def spectral_centroid(magnitudes: list[float],
                      sample_rate: int = 16000) -> float:
    """Compute the spectral centroid (brightness) of a magnitude spectrum.

    The centroid is the weighted mean frequency, where weights are the
    magnitudes.  Higher values indicate brighter, more treble-heavy audio.
    Requires no optional dependencies — works with plain lists.

    Args:
        magnitudes: Positive-frequency magnitudes.
        sample_rate: Sample rate in Hz.

    Returns:
        Centroid frequency in Hz, normalized to [0.0, 1.0] relative to
        Nyquist.  Returns 0.0 for silent frames.
    """
    n_bins: int = len(magnitudes)
    if n_bins == 0:
        return 0.0

    fft_len: int = (n_bins - 1) * 2
    if fft_len == 0:
        return 0.0
    bin_hz: float = sample_rate / fft_len
    nyquist: float = sample_rate / 2.0

    total_mag: float = 0.0
    weighted_sum: float = 0.0
    for i in range(n_bins):
        freq: float = i * bin_hz
        total_mag += magnitudes[i]
        weighted_sum += freq * magnitudes[i]

    if total_mag < 1e-12:
        return 0.0
    centroid_hz: float = weighted_sum / total_mag
    return min(1.0, centroid_hz / nyquist)
