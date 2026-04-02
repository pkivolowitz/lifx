"""Text-to-speech — Piper TTS with macOS ``say`` fallback.

Produces WAV audio from text for playback via AirPlay or local
speakers.  Piper is the primary engine (cross-platform, good
quality, runs on CPU).  On macOS, ``say`` is available as a
zero-dependency fallback.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import io
import logging
import os
import platform
import subprocess
import tempfile
import wave
from typing import Optional

logger: logging.Logger = logging.getLogger("glowup.voice.tts")

# ---------------------------------------------------------------------------
# Optional Piper import
# ---------------------------------------------------------------------------

try:
    from piper import PiperVoice
    _HAS_PIPER: bool = True
except ImportError:
    PiperVoice = None  # type: ignore[assignment, misc]
    _HAS_PIPER = False


class TextToSpeech:
    """Synthesize speech from text.

    Tries Piper TTS first, falls back to macOS ``say``.

    Args:
        voice_model: Path to a Piper .onnx voice model.  If None,
                     falls back to ``say`` on macOS.
    """

    def __init__(self, voice_model: Optional[str] = None) -> None:
        """Initialize TTS engine."""
        self._piper: Optional["PiperVoice"] = None
        self._sample_rate: int = 22050  # Piper default.

        if voice_model and _HAS_PIPER and os.path.exists(voice_model):
            try:
                self._piper = PiperVoice.load(voice_model)
                # Get sample rate from the model config.
                if hasattr(self._piper, "config"):
                    self._sample_rate = getattr(
                        self._piper.config, "sample_rate", 22050,
                    )
                logger.info("Piper TTS loaded: %s", voice_model)
            except Exception as exc:
                logger.warning("Piper load failed: %s — using fallback", exc)
                self._piper = None
        elif _HAS_PIPER and voice_model:
            logger.warning(
                "Piper model not found: %s — using fallback", voice_model,
            )

        if self._piper is None:
            if platform.system() == "Darwin":
                logger.info("Using macOS 'say' for TTS")
            else:
                logger.warning("No TTS engine available")

    def synthesize(self, text: str) -> tuple[bytes, int]:
        """Convert text to WAV audio.

        Args:
            text: Text to speak.

        Returns:
            Tuple of (WAV bytes, sample_rate).  Returns (b"", 0)
            on failure.
        """
        if not text.strip():
            return b"", 0

        if self._piper is not None:
            return self._synthesize_piper(text)
        elif platform.system() == "Darwin":
            return self._synthesize_say(text)
        else:
            logger.warning("No TTS engine — cannot speak")
            return b"", 0

    def _synthesize_piper(self, text: str) -> tuple[bytes, int]:
        """Synthesize using Piper TTS.

        Args:
            text: Text to speak.

        Returns:
            Tuple of (WAV bytes, sample_rate).
        """
        try:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit.
                wf.setframerate(self._sample_rate)
                self._piper.synthesize(text, wf)  # type: ignore[union-attr]

            wav_bytes: bytes = buf.getvalue()
            logger.debug(
                "Piper synthesized %d bytes for: '%s'",
                len(wav_bytes), text[:50],
            )
            return wav_bytes, self._sample_rate

        except Exception as exc:
            logger.error("Piper synthesis failed: %s", exc)
            # Fall back to say if on macOS.
            if platform.system() == "Darwin":
                return self._synthesize_say(text)
            return b"", 0

    def _synthesize_say(self, text: str) -> tuple[bytes, int]:
        """Synthesize using macOS ``say`` command.

        Generates AIFF, then converts to WAV via afconvert.

        Args:
            text: Text to speak.

        Returns:
            Tuple of (WAV bytes, sample_rate).
        """
        # mkstemp avoids the race condition of mktemp (deprecated).
        aiff_fd, aiff_path = tempfile.mkstemp(suffix=".aiff")
        os.close(aiff_fd)
        wav_path: str = aiff_path.replace(".aiff", ".wav")

        try:
            # Generate AIFF via say.
            say_result = subprocess.run(
                ["say", "-o", aiff_path, text],
                capture_output=True,
                timeout=30,
            )

            if say_result.returncode != 0:
                logger.warning(
                    "say exited with %d: %s",
                    say_result.returncode, say_result.stderr[:200],
                )
                return b"", 0

            if not os.path.exists(aiff_path):
                logger.warning("say produced no output")
                return b"", 0

            # Convert AIFF to WAV via afconvert (native macOS, faster
            # than ffmpeg — no codec loading overhead).
            convert_result = subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", "LEI16",
                 aiff_path, wav_path],
                capture_output=True,
                timeout=10,
            )

            if convert_result.returncode != 0:
                logger.warning(
                    "afconvert exited with %d: %s",
                    convert_result.returncode, convert_result.stderr[:200],
                )
                return b"", 0

            if not os.path.exists(wav_path):
                logger.warning("afconvert produced no output")
                return b"", 0

            with open(wav_path, "rb") as f:
                wav_bytes: bytes = f.read()

            # Parse sample rate from WAV header.
            with wave.open(wav_path, "rb") as wf:
                sr: int = wf.getframerate()

            logger.debug(
                "macOS say synthesized %d bytes at %dHz for: '%s'",
                len(wav_bytes), sr, text[:50],
            )
            return wav_bytes, sr

        except Exception as exc:
            logger.error("macOS say synthesis failed: %s", exc)
            return b"", 0

        finally:
            for path in (aiff_path, wav_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
