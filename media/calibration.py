"""Audio-light synchronization calibration utilities.

Generates and detects calibration pulses for measuring the one-way
audio latency between the server (where FFT/lights are instantaneous)
and the client (where audio arrives via TCP + ffplay pipeline).

The calibration pulse is a short, full-scale sine burst at a known
frequency, embedded in silence.  It travels through the same extractor
callback chain as real audio, so it hits both the SignalBus (lights)
and the TCP stream (speakers) at the same instant on the server side.
The client detects the pulse arrival and reports the measured delay
back to the server, which applies it as a frame delay in the render
pipeline.

Classes:
    CalibrationPulse  — immutable pulse descriptor
    PulseGenerator    — synthesizes raw PCM calibration sequences
    PulseDetector     — detects calibration pulses in a raw PCM stream
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
import struct
import time
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Calibration pulse parameters.
CALIBRATION_FREQ_HZ: int = 1000
CALIBRATION_DURATION_MS: int = 50
CALIBRATION_AMPLITUDE: float = 0.95
CALIBRATION_SILENCE_MS: int = 500
CALIBRATION_PULSE_COUNT: int = 3
CALIBRATION_GAP_MS: int = 1000

# Detection threshold: RMS must exceed this fraction of full scale
# to be considered a pulse.  Full scale for 16-bit PCM normalized
# to [-1, 1] is amplitude ~0.95, so RMS of a sine is ~0.67.
DETECTION_RMS_THRESHOLD: float = 0.3

# Detection window size in samples (at 44100 Hz, 512 samples ≈ 11.6ms).
DETECTION_WINDOW_SAMPLES: int = 512

# Bytes per sample for 16-bit signed PCM.
BYTES_PER_SAMPLE: int = 2


# ---------------------------------------------------------------------------
# Pulse descriptor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalibrationPulse:
    """Immutable descriptor for a single calibration pulse.

    Attributes:
        sequence_id:  Pulse index (0, 1, 2, ...).
        emit_time:    Server-side ``time.monotonic()`` at emission.
    """
    sequence_id: int
    emit_time: float


# ---------------------------------------------------------------------------
# Pulse generation
# ---------------------------------------------------------------------------

class PulseGenerator:
    """Synthesize raw PCM calibration pulse sequences.

    Generates silence-pulse-silence byte sequences suitable for
    injection into a :class:`MediaSource` extractor callback chain.

    Args:
        sample_rate:  Audio sample rate in Hz (default 44100).
        frequency:    Pulse tone frequency in Hz (default 1000).
        duration_ms:  Pulse duration in milliseconds (default 50).
        amplitude:    Peak amplitude [0, 1] (default 0.95).
    """

    def __init__(
        self,
        sample_rate: int = 44100,
        frequency: int = CALIBRATION_FREQ_HZ,
        duration_ms: int = CALIBRATION_DURATION_MS,
        amplitude: float = CALIBRATION_AMPLITUDE,
    ) -> None:
        self._sample_rate: int = sample_rate
        self._frequency: int = frequency
        self._duration_ms: int = duration_ms
        self._amplitude: float = amplitude

        # Pre-generate the pulse tone as raw PCM bytes.
        n_samples: int = int(sample_rate * duration_ms / 1000)
        samples: list[int] = []
        for i in range(n_samples):
            t: float = i / sample_rate
            value: float = amplitude * math.sin(
                2.0 * math.pi * frequency * t
            )
            samples.append(int(value * 32767))
        self._pulse_bytes: bytes = struct.pack(
            f"<{len(samples)}h", *samples
        )

    @property
    def pulse_bytes(self) -> bytes:
        """The raw PCM bytes of a single calibration pulse."""
        return self._pulse_bytes

    def generate_silence(self, duration_ms: int) -> bytes:
        """Generate silent PCM bytes.

        Args:
            duration_ms: Duration of silence in milliseconds.

        Returns:
            Raw PCM bytes of silence.
        """
        n_samples: int = int(self._sample_rate * duration_ms / 1000)
        return b"\x00" * (n_samples * BYTES_PER_SAMPLE)

    def generate_sequence(
        self,
        pulse_count: int = CALIBRATION_PULSE_COUNT,
        silence_ms: int = CALIBRATION_SILENCE_MS,
        gap_ms: int = CALIBRATION_GAP_MS,
    ) -> list[tuple[str, bytes]]:
        """Generate the full calibration sequence as tagged chunks.

        Returns a list of ``(tag, pcm_bytes)`` tuples where tag is
        ``"silence"`` or ``"pulse:{n}"`` (n = sequence ID).  The
        caller injects each chunk into the source's extractor chain
        and records timestamps for ``"pulse:*"`` chunks.

        Args:
            pulse_count: Number of pulses to generate.
            silence_ms:  Leading/trailing silence per pulse (ms).
            gap_ms:      Gap between pulses (ms).

        Returns:
            List of (tag, bytes) tuples in playback order.
        """
        chunks: list[tuple[str, bytes]] = []

        # Leading silence.
        chunks.append(("silence", self.generate_silence(silence_ms)))

        for i in range(pulse_count):
            # The pulse itself.
            chunks.append((f"pulse:{i}", self._pulse_bytes))
            # Trailing silence / gap.
            gap: int = gap_ms if i < pulse_count - 1 else silence_ms
            chunks.append(("silence", self.generate_silence(gap)))

        return chunks


# ---------------------------------------------------------------------------
# Pulse detection
# ---------------------------------------------------------------------------

class PulseDetector:
    """Detect calibration pulses in a raw PCM byte stream.

    Reads 16-bit signed LE mono PCM and computes RMS over a sliding
    window.  When the RMS exceeds the detection threshold after a
    period of silence, a pulse detection is recorded with the local
    ``time.monotonic()`` timestamp.

    Args:
        sample_rate:    Audio sample rate in Hz (default 44100).
        threshold:      RMS threshold for pulse detection (default 0.3).
        window_samples: Detection window size in samples (default 512).
    """

    def __init__(
        self,
        sample_rate: int = 44100,
        threshold: float = DETECTION_RMS_THRESHOLD,
        window_samples: int = DETECTION_WINDOW_SAMPLES,
    ) -> None:
        self._sample_rate: int = sample_rate
        self._threshold: float = threshold
        self._window_samples: int = window_samples
        self._window_bytes: int = window_samples * BYTES_PER_SAMPLE

        # Detection state.
        self._detections: list[float] = []
        self._in_pulse: bool = False
        # Minimum silence between pulses to prevent double-triggering.
        self._min_gap_samples: int = int(sample_rate * 0.2)
        self._samples_since_last: int = self._min_gap_samples

    @property
    def detections(self) -> list[float]:
        """List of ``time.monotonic()`` timestamps for detected pulses."""
        return list(self._detections)

    @property
    def detection_count(self) -> int:
        """Number of pulses detected so far."""
        return len(self._detections)

    def feed(self, data: bytes) -> Optional[float]:
        """Feed raw PCM bytes and check for pulse detection.

        Args:
            data: Raw 16-bit signed LE mono PCM bytes.

        Returns:
            The detection timestamp if a new pulse was detected in
            this chunk, or ``None``.
        """
        n_samples: int = len(data) // BYTES_PER_SAMPLE
        if n_samples == 0:
            return None

        # Decode samples.
        samples: list[int] = list(
            struct.unpack(f"<{n_samples}h", data[:n_samples * BYTES_PER_SAMPLE])
        )

        # Compute RMS over the chunk.
        sum_sq: float = 0.0
        for s in samples:
            norm: float = s / 32767.0
            sum_sq += norm * norm
        rms: float = math.sqrt(sum_sq / n_samples)

        self._samples_since_last += n_samples

        # State machine: silence → pulse transition.
        if rms >= self._threshold:
            if (not self._in_pulse
                    and self._samples_since_last >= self._min_gap_samples):
                # New pulse detected.
                self._in_pulse = True
                self._samples_since_last = 0
                detect_time: float = time.monotonic()
                self._detections.append(detect_time)
                return detect_time
        else:
            self._in_pulse = False

        return None

    def reset(self) -> None:
        """Clear all detection state."""
        self._detections.clear()
        self._in_pulse = False
        self._samples_since_last = self._min_gap_samples
