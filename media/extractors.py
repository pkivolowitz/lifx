"""Signal extractors — compute named signals from raw media data.

Extractors sit between MediaSources (raw PCM / video frames) and the
SignalBus (normalized named floats / arrays).  Each extractor receives
raw byte chunks via its :meth:`process` callback, computes meaningful
signals, and writes them to the bus.

All signal values are normalized to [0.0, 1.0] using adaptive peak
tracking (peak hold with exponential decay).  This ensures consistent
behavior regardless of source volume or lighting conditions.

Concrete extractors:
    AudioExtractor — FFT-based frequency bands, RMS, beat detection

Factory function:
    create_extractors — build default extractors for a source's media type
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import math
import struct
import time
from abc import ABC, abstractmethod
from typing import Any

from . import SignalBus, SignalMeta
from .fft import fft_magnitudes, bin_to_bands, spectral_centroid, hann_window

logger: logging.Logger = logging.getLogger("glowup.media.extractors")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default FFT window size (samples).  Must be power of 2.
DEFAULT_WINDOW_SIZE: int = 1024

# Default number of frequency bands.
DEFAULT_BAND_COUNT: int = 8

# Default exponential smoothing factor (0 = no smoothing, 1 = frozen).
DEFAULT_SMOOTHING: float = 0.3

# Peak tracker decay rate per second.  Higher = faster decay back to
# ambient level, making normalization adapt to changing environments.
PEAK_DECAY_RATE: float = 0.5

# Minimum peak value to prevent division by near-zero.
MIN_PEAK: float = 1e-6

# Absolute noise floor — raw values below this are treated as silence.
# This prevents the adaptive peak tracker from normalizing mic hiss
# and electrical noise to full scale during quiet passages.
# Calibrated for 16-bit PCM: ~60 dB below full scale.
NOISE_FLOOR: float = 0.001

# Beat detection: energy must exceed this multiple of the recent average
# to qualify as a beat.
BEAT_THRESHOLD: float = 1.5

# Beat detection: number of recent energy samples for the running average.
BEAT_HISTORY_SIZE: int = 43  # ~2.7 seconds at 64ms windows

# Fixed gain applied to raw FFT band magnitudes.  Raw values are
# typically 0.001-0.01 for normal audio.  This brings them into the
# 0-1 range without any adaptive behavior.  The effect's sensitivity
# param provides additional user-controlled gain.
FFT_BASE_GAIN: float = 100.0

# Bytes per sample for 16-bit signed PCM.
BYTES_PER_SAMPLE: int = 2

# Maximum amplitude for 16-bit signed PCM.
PCM_16_MAX: float = 32768.0


# ---------------------------------------------------------------------------
# SignalExtractor ABC
# ---------------------------------------------------------------------------

class SignalExtractor(ABC):
    """Abstract base class for signal extractors.

    Subclasses implement :meth:`process` to receive raw byte chunks from
    a MediaSource and write computed signals to the bus.
    """

    @abstractmethod
    def process(self, chunk: bytes) -> None:
        """Process a raw media chunk and update the signal bus.

        This method is called from the source's reader thread.  It must
        be fast enough to keep up with the data rate (typically < 5 ms
        for audio, < 50 ms for video).

        Args:
            chunk: Raw bytes from the media source.
        """

    @abstractmethod
    def get_signal_names(self) -> list[str]:
        """Return the signal names this extractor produces.

        Used during initialization to register signals on the bus.

        Returns:
            List of hierarchical signal name strings.
        """


# ---------------------------------------------------------------------------
# Peak tracker (adaptive normalization)
# ---------------------------------------------------------------------------

class _PeakTracker:
    """Adaptive peak tracker for signal normalization.

    Maintains a running peak value that decays over time, allowing
    normalization to adapt to changing source levels (e.g., quiet
    vs. loud environments).

    Attributes:
        peak: Current peak value.
    """

    def __init__(self, decay_rate: float = PEAK_DECAY_RATE) -> None:
        """Initialize with zero peak.

        Args:
            decay_rate: Exponential decay rate per second.
        """
        self.peak: float = MIN_PEAK
        self._decay_rate: float = decay_rate
        self._last_update: float = time.monotonic()

    def update(self, value: float) -> float:
        """Update peak and return normalized value in [0.0, 1.0].

        Values below the absolute noise floor are treated as silence
        (returns 0.0) to prevent mic hiss from being normalized to
        full scale during quiet passages.

        Args:
            value: Raw (non-negative) signal value.

        Returns:
            Normalized value, or 0.0 if below noise floor.
        """
        now: float = time.monotonic()
        dt: float = now - self._last_update
        self._last_update = now

        # Noise gate: below absolute floor → silence.
        if value < NOISE_FLOOR:
            # Still decay the peak so it adapts when signal returns.
            self.peak *= math.exp(-self._decay_rate * dt)
            if self.peak < NOISE_FLOOR:
                self.peak = NOISE_FLOOR
            return 0.0

        # Decay the peak toward the ambient level.
        self.peak *= math.exp(-self._decay_rate * dt)
        if self.peak < NOISE_FLOOR:
            self.peak = NOISE_FLOOR

        # Update peak if new value exceeds it.
        if value > self.peak:
            self.peak = value

        # Normalize.
        return min(1.0, value / self.peak)


# ---------------------------------------------------------------------------
# AudioExtractor
# ---------------------------------------------------------------------------

class AudioExtractor(SignalExtractor):
    """FFT-based audio signal extractor.

    Accumulates 16-bit signed PCM samples into a ring buffer.  Every
    *window_size* samples, it runs a windowed FFT, bins the magnitude
    spectrum into logarithmic frequency bands, applies exponential
    smoothing, and writes normalized signals to the bus.

    Signals produced (all normalized to [0.0, 1.0]):

    =================== ======= =========================================
    Signal Name         Type    Description
    =================== ======= =========================================
    {src}:audio:bands   array   Per-band energy (length = band_count)
    {src}:audio:bass    scalar  Low-frequency energy (band 0-1 avg)
    {src}:audio:mid     scalar  Mid-frequency energy (middle bands avg)
    {src}:audio:treble  scalar  High-frequency energy (top 2 bands avg)
    {src}:audio:rms     scalar  Root-mean-square amplitude
    {src}:audio:energy  scalar  Total spectral energy
    {src}:audio:beat    scalar  Beat pulse (1.0 on beat, decays to 0.0)
    {src}:audio:centroid scalar  Spectral centroid (brightness)
    =================== ======= =========================================

    Args:
        source_name:  Name of the parent media source.
        sample_rate:  Audio sample rate in Hz.
        bus:          SignalBus to write signals to.
        window_size:  FFT window size in samples (default 1024).
        band_count:   Number of frequency bands (default 8).
        smoothing:    Exponential smoothing factor (default 0.3).
    """

    def __init__(self, source_name: str, sample_rate: int, bus: SignalBus,
                 window_size: int = DEFAULT_WINDOW_SIZE,
                 band_count: int = DEFAULT_BAND_COUNT,
                 smoothing: float = DEFAULT_SMOOTHING) -> None:
        """Initialize the audio extractor.

        Args:
            source_name: Parent source name for signal naming.
            sample_rate: Audio sample rate in Hz.
            bus:         SignalBus for output.
            window_size: FFT window size (must be power of 2).
            band_count:  Number of log-spaced frequency bands.
            smoothing:   EMA smoothing factor [0, 1).
        """
        self._source_name: str = source_name
        self._sample_rate: int = sample_rate
        self._bus: SignalBus = bus
        self._window_size: int = window_size
        self._band_count: int = band_count
        self._smoothing: float = smoothing

        # Ring buffer for accumulating PCM samples.
        self._buffer: list[float] = []

        # Precomputed Hann window.
        self._window: list[float] = hann_window(window_size)

        # Smoothed band values (initialized to zero).
        self._smooth_bands: list[float] = [0.0] * band_count

        # Beat detection state.
        self._energy_history: list[float] = []
        self._beat_value: float = 0.0
        self._last_beat_time: float = 0.0

        # Register signals on the bus.
        self._register_signals()

    def _register_signals(self) -> None:
        """Register all output signals with metadata on the bus."""
        prefix: str = f"{self._source_name}:audio"

        self._bus.register(
            f"{prefix}:bands",
            SignalMeta(
                signal_type="array",
                description=f"{self._band_count}-band frequency spectrum",
                source_name=self._source_name,
            ),
        )

        scalar_signals: dict[str, str] = {
            "bass": "Low-frequency energy",
            "mid": "Mid-frequency energy",
            "treble": "High-frequency energy",
            "rms": "Root-mean-square amplitude",
            "energy": "Total spectral energy",
            "beat": "Beat pulse (1.0 on beat, decays to 0.0)",
            "centroid": "Spectral centroid (brightness)",
        }
        for name, desc in scalar_signals.items():
            self._bus.register(
                f"{prefix}:{name}",
                SignalMeta(
                    signal_type="scalar",
                    description=desc,
                    source_name=self._source_name,
                ),
            )

    def get_signal_names(self) -> list[str]:
        """Return the signal names this extractor produces.

        Returns:
            List of signal name strings.
        """
        prefix: str = f"{self._source_name}:audio"
        return [
            f"{prefix}:bands",
            f"{prefix}:bass",
            f"{prefix}:mid",
            f"{prefix}:treble",
            f"{prefix}:rms",
            f"{prefix}:energy",
            f"{prefix}:beat",
            f"{prefix}:centroid",
        ]

    def process(self, chunk: bytes) -> None:
        """Process a chunk of raw 16-bit signed PCM audio.

        Accumulates samples and runs analysis when a full window is
        available.  Called from the source's reader thread.

        Args:
            chunk: Raw PCM bytes (16-bit signed, little-endian).
        """
        # Decode 16-bit signed PCM to float [-1.0, 1.0].
        n_samples: int = len(chunk) // BYTES_PER_SAMPLE
        if n_samples == 0:
            return

        fmt: str = f"<{n_samples}h"
        try:
            raw: tuple = struct.unpack(fmt, chunk[:n_samples * BYTES_PER_SAMPLE])
        except struct.error:
            return

        samples: list[float] = [s / PCM_16_MAX for s in raw]
        self._buffer.extend(samples)

        # Process complete windows.
        while len(self._buffer) >= self._window_size:
            window_samples: list[float] = self._buffer[:self._window_size]
            self._buffer = self._buffer[self._window_size:]
            self._analyze_window(window_samples)

    def _analyze_window(self, samples: list[float]) -> None:
        """Run FFT analysis on one window of samples and update the bus.

        Args:
            samples: Exactly *window_size* float samples.
        """
        prefix: str = f"{self._source_name}:audio"

        # --- RMS ---
        sum_sq: float = 0.0
        for s in samples:
            sum_sq += s * s
        rms_raw: float = math.sqrt(sum_sq / len(samples))

        # --- FFT magnitude spectrum ---
        mags: list[float] = fft_magnitudes(samples, self._window)

        # --- Frequency bands ---
        bands_raw: list[float] = bin_to_bands(
            mags, self._band_count, self._sample_rate
        )

        # Apply exponential smoothing.
        alpha: float = self._smoothing
        for i in range(self._band_count):
            self._smooth_bands[i] = (
                alpha * self._smooth_bands[i]
                + (1.0 - alpha) * bands_raw[i]
            )

        # Apply fixed gain to bring raw FFT magnitudes into [0, 1].
        # Raw values are typically 0.001-0.01 for normal audio — this
        # baseline scaling makes them visible.  The effect's sensitivity
        # param provides additional user-controlled gain on top.
        bands_out: list[float] = [
            min(1.0, b * FFT_BASE_GAIN) for b in self._smooth_bands
        ]

        # --- Derived scalar signals ---
        # Bass: average of lowest 2 bands (or 1 if only 1 band).
        bass_end: int = min(2, self._band_count)
        bass: float = sum(bands_out[:bass_end]) / bass_end if bass_end > 0 else 0.0

        # Treble: average of highest 2 bands.
        treble_start: int = max(0, self._band_count - 2)
        treble_count: int = self._band_count - treble_start
        treble: float = (
            sum(bands_out[treble_start:]) / treble_count
            if treble_count > 0 else 0.0
        )

        # Mid: average of middle bands.
        mid_start: int = bass_end
        mid_end: int = treble_start
        mid_count: int = mid_end - mid_start
        mid: float = (
            sum(bands_out[mid_start:mid_end]) / mid_count
            if mid_count > 0 else 0.0
        )

        # Energy: total spectral energy with base gain.
        energy: float = min(1.0, sum(self._smooth_bands) * FFT_BASE_GAIN / self._band_count)

        # RMS with base gain.
        rms_out: float = min(1.0, rms_raw * FFT_BASE_GAIN)

        # --- Beat detection ---
        energy_raw: float = sum(self._smooth_bands)
        self._energy_history.append(energy_raw)
        if len(self._energy_history) > BEAT_HISTORY_SIZE:
            self._energy_history = self._energy_history[-BEAT_HISTORY_SIZE:]

        avg_energy: float = (
            sum(self._energy_history) / len(self._energy_history)
            if self._energy_history else 0.0
        )
        now: float = time.monotonic()

        if energy_raw > avg_energy * BEAT_THRESHOLD and avg_energy > MIN_PEAK:
            # Debounce: minimum 150ms between beats.
            if now - self._last_beat_time > 0.15:
                self._beat_value = 1.0
                self._last_beat_time = now

        # Beat decays over ~200ms.
        dt: float = now - self._last_beat_time
        if dt > 0:
            self._beat_value = max(0.0, 1.0 - dt / 0.2)

        # --- Spectral centroid ---
        centroid: float = spectral_centroid(mags, self._sample_rate)

        # --- Write to bus ---
        self._bus.write(f"{prefix}:bands", bands_out)
        self._bus.write(f"{prefix}:bass", bass)
        self._bus.write(f"{prefix}:mid", mid)
        self._bus.write(f"{prefix}:treble", treble)
        self._bus.write(f"{prefix}:rms", rms_out)
        self._bus.write(f"{prefix}:energy", energy)
        self._bus.write(f"{prefix}:beat", self._beat_value)
        self._bus.write(f"{prefix}:centroid", centroid)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_extractors(source_name: str, media_type: str, sample_rate: int,
                      extractor_configs: dict[str, Any],
                      bus: SignalBus) -> list[SignalExtractor]:
    """Create default extractors for a media source.

    If no explicit extractor configuration is provided, sensible defaults
    are used based on the source's media type.

    Args:
        source_name:       Parent source name.
        media_type:        ``"audio"`` or ``"video"``.
        sample_rate:       Audio sample rate in Hz.
        extractor_configs: Explicit extractor config from ``server.json``.
        bus:               SignalBus for output.

    Returns:
        List of configured :class:`SignalExtractor` instances.
    """
    extractors: list[SignalExtractor] = []

    if media_type == "audio":
        audio_cfg: dict[str, Any] = extractor_configs.get("audio", {})
        extractors.append(AudioExtractor(
            source_name=source_name,
            sample_rate=sample_rate,
            bus=bus,
            window_size=audio_cfg.get("window_size", DEFAULT_WINDOW_SIZE),
            band_count=audio_cfg.get("bands", DEFAULT_BAND_COUNT),
            smoothing=audio_cfg.get("smoothing", DEFAULT_SMOOTHING),
        ))

    elif media_type == "video":
        # VideoExtractor is a future addition (Tier 3, requires opencv).
        logger.info(
            "Video extraction for '%s' is not yet implemented. "
            "Install opencv-python and check for updates.",
            source_name,
        )

    return extractors
