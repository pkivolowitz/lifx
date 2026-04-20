"""faster-whisper STT engine.

CTranslate2 port of OpenAI Whisper.  Runs CPU-only on Apple Silicon
(Metal acceleration requires a manual CTranslate2 rebuild we do not
ship).  Cross-platform — serves as the fallback engine on macOS and
as the primary engine on non-Apple-Silicon hosts.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

import logging
import os
import shutil
import time
from typing import Any, Optional

from voice.coordinator.stt_engines.base import (
    STTEngineLoadError,
    pcm_to_wav,
)

logger: logging.Logger = logging.getLogger("glowup.voice.stt")


class FasterWhisperEngine:
    """faster-whisper backed STT engine."""

    name: str = "faster-whisper"

    def __init__(
        self,
        model: str = "base.en",
        model_path: Optional[str] = None,
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "en",
    ) -> None:
        self._model_name: str = model
        self._model_path: Optional[str] = model_path
        self._device: str = device
        self._compute_type: str = compute_type
        self._language: str = language
        self._model: Any = None
        self._ffmpeg: str = (
            shutil.which("ffmpeg")
            or "/opt/homebrew/bin/ffmpeg"
            or "/usr/local/bin/ffmpeg"
        )

    @classmethod
    def is_available(cls) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def load(self) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise STTEngineLoadError(
                f"faster-whisper not installed: {exc}"
            ) from exc

        if not os.path.exists(self._ffmpeg):
            raise STTEngineLoadError(
                f"ffmpeg not found at {self._ffmpeg} — "
                "required for PCM→WAV conversion"
            )

        # Prefer a local model directory if provided; otherwise fall
        # back to the HF-cache-managed model name.
        target: str = self._model_path or self._model_name

        logger.info(
            "Loading faster-whisper model: %s (device=%s, compute=%s)",
            target, self._device, self._compute_type,
        )
        t0: float = time.monotonic()
        try:
            self._model = WhisperModel(
                target,
                device=self._device,
                compute_type=self._compute_type,
            )
        except Exception as exc:
            raise STTEngineLoadError(
                f"faster-whisper model load failed: {exc}"
            ) from exc
        elapsed: float = time.monotonic() - t0
        logger.info("faster-whisper model loaded in %.1fs", elapsed)

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        if not pcm:
            return ""
        wav_path: Optional[str] = pcm_to_wav(pcm, sample_rate, self._ffmpeg)
        if wav_path is None:
            return ""
        try:
            t0: float = time.monotonic()
            segments, _info = self._model.transcribe(
                wav_path, language=self._language,
            )
            text: str = " ".join(s.text for s in segments).strip()
            elapsed: float = time.monotonic() - t0
            logger.info(
                "faster-whisper transcribed in %.2fs: '%s'",
                elapsed, text[:100],
            )
            return text
        except Exception as exc:
            logger.error("faster-whisper transcription failed: %s", exc)
            return ""
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
