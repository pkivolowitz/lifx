"""Utterance capture with silence detection.

Captures raw PCM audio from a PyAudio stream until silence is
detected or the maximum duration is reached.  Includes a pre-wake
ring buffer to avoid clipping the beginning of speech.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import collections
import logging
import math
import struct
from typing import Any, Optional

from voice import constants as C

logger: logging.Logger = logging.getLogger("glowup.voice.capture")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Number of ring buffer frames for pre-wake audio.  Computed from
# PRE_WAKE_BUFFER_S in constants.py at runtime.
_DEFAULT_PRE_WAKE_FRAMES: int = 4


def compute_rms(data: bytes) -> float:
    """Compute RMS amplitude of 16-bit signed LE PCM.

    Args:
        data: Raw PCM bytes (16-bit signed little-endian).

    Returns:
        RMS amplitude as a float.  Returns 0.0 for empty input.
    """
    n_samples: int = len(data) // 2
    if n_samples == 0:
        return 0.0
    fmt: str = f"<{n_samples}h"
    samples = struct.unpack(fmt, data[:n_samples * 2])
    sum_sq: float = sum(s * s for s in samples)
    return math.sqrt(sum_sq / n_samples)


class UtteranceCapture:
    """Captures an utterance from a PyAudio stream.

    Uses RMS-based silence detection to determine when the speaker
    has finished.  Prepends pre-wake audio from a ring buffer to
    avoid clipping the beginning of speech.

    Args:
        sample_rate:     Audio sample rate (Hz).
        chunk_samples:   Samples per read chunk.
        max_seconds:     Maximum capture duration.
        silence_timeout: Seconds of silence before stopping.
        silence_rms:     RMS threshold below which audio is silence.
        min_seconds:     Minimum capture duration (reject short triggers).
        pre_wake_seconds: Seconds of pre-wake audio to retain.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        chunk_samples: int = 1280,
        max_seconds: float = 5.0,
        silence_timeout: float = 1.5,
        silence_rms: int = 200,
        min_seconds: float = 0.3,
        pre_wake_seconds: float = 0.2,
    ) -> None:
        """Initialize the utterance capture."""
        self._sample_rate: int = sample_rate
        self._chunk_samples: int = chunk_samples
        self._chunk_bytes: int = chunk_samples * C.BYTES_PER_SAMPLE
        self.chunk_bytes: int = self._chunk_bytes  # Public for ALSA reads.
        self._max_chunks: int = int(
            sample_rate / chunk_samples * max_seconds,
        )
        self._silence_chunks: int = int(
            silence_timeout * sample_rate / chunk_samples,
        )
        self._silence_rms: int = silence_rms
        self._min_chunks: int = int(
            min_seconds * sample_rate / chunk_samples,
        )

        # Pre-wake ring buffer.
        pre_wake_frames: int = max(
            1, int(pre_wake_seconds * sample_rate / chunk_samples),
        )
        self._ring: collections.deque[bytes] = collections.deque(
            maxlen=pre_wake_frames,
        )

    def feed_ring(self, chunk: bytes) -> None:
        """Feed a chunk into the pre-wake ring buffer.

        Call this continuously from the main loop while waiting for
        the wake word.

        Args:
            chunk: Raw PCM chunk (one frame).
        """
        self._ring.append(chunk)

    def capture(self, stream: Any, use_alsa: bool = False) -> Optional[bytes]:
        """Capture an utterance from the audio stream.

        Uses adaptive silence detection: the noise floor is measured
        from the pre-wake ring buffer, and silence is defined as
        RMS below (noise_floor * multiplier).  This handles fans,
        AC, and other ambient noise automatically.

        Reads chunks until silence is detected or the maximum
        duration is reached.  Prepends pre-wake ring buffer
        contents.

        Args:
            stream: An open PyAudio stream or ALSA pipe (stdout).
            use_alsa: If True, read raw bytes from pipe instead
                      of calling stream.read(n_frames).

        Returns:
            Raw PCM bytes of the captured utterance, or None if
            the capture was too short (below min_seconds).
        """
        # Measure noise floor from the pre-wake ring buffer.
        # This is audio from just before the wake word — pure ambient.
        noise_floor: float = 0.0
        if self._ring:
            ring_rms: list[float] = [compute_rms(chunk) for chunk in self._ring]
            noise_floor = sum(ring_rms) / len(ring_rms)

        # Silence threshold: noise floor + headroom multiplier.
        # Speech is typically 3-10x louder than ambient noise.
        # Use 1.8x as the boundary — anything below is silence.
        _NOISE_MULTIPLIER: float = 1.8
        # Minimum threshold prevents zero-floor edge case (dead quiet room).
        _MIN_THRESHOLD: float = 50.0
        # SPECULATIVE (2026-04-16): hard cap on adaptive threshold.
        # The Dining Room Jabra near the NAS sees bursty equipment
        # noise (floor swings 121–4471 RMS within 30s).  When the
        # floor spikes, 1.8x pushes the threshold to 8000+ and
        # normal speech never registers — capture runs to the 10s
        # cap.  3000 lets the detector still hear conversational
        # speech at arm's length from a Jabra.  Needs confirmation
        # that this doesn't cause premature cutoff in loud rooms.
        _MAX_THRESHOLD: float = 3000.0
        adaptive_threshold: float = min(
            max(noise_floor * _NOISE_MULTIPLIER, _MIN_THRESHOLD),
            _MAX_THRESHOLD,
        )
        logger.info(
            "Adaptive silence: floor=%.0f threshold=%.0f "
            "(cap=%.0f, configured=%d)",
            noise_floor, adaptive_threshold,
            _MAX_THRESHOLD, self._silence_rms,
        )

        frames: list[bytes] = list(self._ring)
        self._ring.clear()

        silent_count: int = 0
        speech_seen: bool = False
        total_chunks: int = 0

        for _ in range(self._max_chunks):
            try:
                if use_alsa:
                    data = stream.read(self._chunk_bytes)
                    if not data:
                        break
                else:
                    data = stream.read(
                        self._chunk_samples, exception_on_overflow=False,
                    )
            except Exception as exc:
                logger.warning("Audio read error during capture: %s", exc)
                break

            frames.append(data)
            total_chunks += 1

            rms: float = compute_rms(data)
            if rms >= adaptive_threshold:
                speech_seen = True
                silent_count = 0
            else:
                # Only count silence after speech has been seen.
                # This prevents cutting off before the speaker starts.
                if speech_seen:
                    silent_count += 1
                    if silent_count >= self._silence_chunks:
                        break

        if total_chunks < self._min_chunks:
            logger.debug(
                "Capture too short (%d chunks, need %d) — discarding",
                total_chunks, self._min_chunks,
            )
            return None

        pcm: bytes = b"".join(frames)
        duration: float = len(pcm) / (self._sample_rate * C.BYTES_PER_SAMPLE)
        logger.info("Captured %.1fs utterance (%d bytes)", duration, len(pcm))
        return pcm
