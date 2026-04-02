"""Speech-to-text using faster-whisper.

Loads the Whisper model once at startup and reuses it for all
transcriptions.  On M1 Mac, runs CPU-only with int8 quantization
(Metal acceleration requires manual CTranslate2 compilation).

Converts raw PCM to WAV via ffmpeg subprocess before transcription.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Optional

logger: logging.Logger = logging.getLogger("glowup.voice.stt")

# ---------------------------------------------------------------------------
# Optional import
# ---------------------------------------------------------------------------

try:
    from faster_whisper import WhisperModel
    _HAS_WHISPER: bool = True
except ImportError:
    WhisperModel = None  # type: ignore[assignment, misc]
    _HAS_WHISPER = False


class SpeechToText:
    """Transcribe PCM audio to text using faster-whisper.

    The model is loaded once in the constructor and reused for all
    subsequent calls.

    Args:
        model_size:   Whisper model name (``tiny.en``, ``base.en``,
                      ``small.en``, ``medium.en``).
        device:       Compute device (``cpu`` or ``cuda``).
        compute_type: Quantization type (``int8``, ``float16``,
                      ``float32``).
    """

    def __init__(
        self,
        model_size: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        """Initialize and load the Whisper model."""
        if not _HAS_WHISPER:
            raise ImportError(
                "faster-whisper not installed — "
                "pip install faster-whisper"
            )

        logger.info(
            "Loading Whisper model: %s (device=%s, compute=%s)",
            model_size, device, compute_type,
        )
        t0: float = time.monotonic()
        self._model: "WhisperModel" = WhisperModel(
            model_size, device=device, compute_type=compute_type,
        )
        elapsed: float = time.monotonic() - t0
        logger.info("Whisper model loaded in %.1fs", elapsed)

        # Locate ffmpeg — may not be in PATH when run via SSH.
        self._ffmpeg: str = (
            shutil.which("ffmpeg")
            or "/opt/homebrew/bin/ffmpeg"
            or "/usr/local/bin/ffmpeg"
        )
        if not os.path.exists(self._ffmpeg):
            logger.warning("ffmpeg not found — STT will fail")

    def transcribe(
        self,
        pcm: bytes,
        sample_rate: int = 16000,
    ) -> str:
        """Transcribe raw PCM audio to text.

        Writes PCM to a temp file, converts to WAV via ffmpeg,
        then runs Whisper transcription.

        Args:
            pcm:         Raw PCM bytes (16-bit signed LE mono).
            sample_rate: Audio sample rate in Hz.

        Returns:
            Transcribed text string.  Empty string if nothing
            was detected.
        """
        if len(pcm) == 0:
            return ""

        # Write raw PCM to temp file.
        raw_fd, raw_path = tempfile.mkstemp(suffix=".raw")
        wav_path: str = raw_path.replace(".raw", ".wav")

        try:
            with os.fdopen(raw_fd, "wb") as f:
                f.write(pcm)

            # Convert raw PCM to WAV via ffmpeg.
            result = subprocess.run(
                [
                    self._ffmpeg, "-y",
                    "-f", "s16le",
                    "-ar", str(sample_rate),
                    "-ac", "1",
                    "-i", raw_path,
                    wav_path,
                ],
                capture_output=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.warning(
                    "ffmpeg conversion failed: %s",
                    result.stderr.decode("utf-8", errors="replace")[:200],
                )
                return ""

            # Transcribe.
            t0: float = time.monotonic()
            segments, info = self._model.transcribe(
                wav_path, language="en",
            )
            text: str = " ".join(s.text for s in segments).strip()
            elapsed: float = time.monotonic() - t0

            logger.info(
                "Transcribed in %.2fs: '%s'",
                elapsed, text[:100],
            )
            return text

        except Exception as exc:
            logger.error("Transcription failed: %s", exc)
            return ""

        finally:
            # Clean up temp files.
            for path in (raw_path, wav_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


class MockSpeechToText:
    """Mock STT for testing without faster-whisper installed.

    Returns a pre-set transcript or prompts for typed input.

    Args:
        transcript: Fixed transcript to return.  If None, prompts
                    stdin for each call (interactive testing).
    """

    def __init__(self, transcript: Optional[str] = None) -> None:
        """Initialize the mock STT."""
        self._transcript: Optional[str] = transcript

    def transcribe(
        self, pcm: bytes, sample_rate: int = 16000,
    ) -> str:
        """Return mock transcript.

        Args:
            pcm:         Ignored.
            sample_rate: Ignored.

        Returns:
            The pre-set transcript or user input from stdin.
        """
        if self._transcript is not None:
            return self._transcript

        try:
            text = input("[MOCK STT] Type what you said: ").strip()
            return text
        except EOFError:
            return ""
