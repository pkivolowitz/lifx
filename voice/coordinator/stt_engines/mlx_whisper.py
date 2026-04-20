"""MLX-Whisper STT engine — Apple Silicon GPU via the MLX framework.

MLX-Whisper runs OpenAI Whisper weights on Apple's MLX array library,
which uses unified memory on M-series Macs and exercises the GPU.
Same accuracy ceiling as faster-whisper at the same model size, but
~2-3x faster on the Mac Studio because it is not CPU-bound.

Platform: macOS arm64 only.  On every other host ``is_available()``
returns False and the facade picks the fallback engine without
treating this as a load failure.

Audio path: the raw 16-bit PCM is converted to a float32 numpy array
in-process and handed to ``mlx_whisper.transcribe()`` directly — no
ffmpeg round-trip.  If the PCM arrives at a non-16 kHz rate (Whisper
requires 16 kHz) the engine resamples via ``scipy.signal.resample_poly``.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

import logging
import platform
import sys
import time
from math import gcd
from typing import Any, Optional

from voice.coordinator.stt_engines.base import STTEngineLoadError

logger: logging.Logger = logging.getLogger("glowup.voice.stt")


TARGET_SAMPLE_RATE: int = 16000


class MLXWhisperEngine:
    """MLX-Whisper backed STT engine (Apple Silicon GPU)."""

    name: str = "mlx-whisper"

    def __init__(
        self,
        model: str = "large-v3-turbo",
        model_path: Optional[str] = None,
        language: str = "en",
    ) -> None:
        """Configure the engine.

        Args:
            model:       Model name.  Used as the HF repo suffix if
                         ``model_path`` is not set (e.g. a model of
                         ``large-v3-turbo`` resolves to the HF repo
                         ``mlx-community/whisper-large-v3-turbo``).
            model_path:  Absolute path to a locally downloaded MLX
                         model directory.  When set, takes precedence
                         over ``model`` and bypasses the HF cache.
            language:    ISO-639-1 language code passed to Whisper.
        """
        self._model_name: str = model
        self._model_path: Optional[str] = model_path
        self._language: str = language
        self._model_ref: str = ""
        self._mlx: Any = None
        self._np: Any = None

    @classmethod
    def is_available(cls) -> bool:
        if sys.platform != "darwin" or platform.machine() != "arm64":
            return False
        try:
            import mlx_whisper  # noqa: F401
            import numpy  # noqa: F401
            return True
        except ImportError:
            return False

    def load(self) -> None:
        if sys.platform != "darwin" or platform.machine() != "arm64":
            raise STTEngineLoadError(
                f"mlx-whisper requires macOS arm64 "
                f"(host is {sys.platform}/{platform.machine()})"
            )
        try:
            import mlx_whisper
            import numpy as np
        except ImportError as exc:
            raise STTEngineLoadError(
                f"mlx-whisper or numpy not installed: {exc}"
            ) from exc

        self._mlx = mlx_whisper
        self._np = np

        # Local path wins if provided; otherwise use the mlx-community
        # HF repo so the first run pulls the model to ~/.cache
        # (the deploy flow pre-fetches to Mini-Dock and sets model_path,
        # but this keeps the engine usable in a dev/test context too).
        if self._model_path:
            self._model_ref = self._model_path
        else:
            self._model_ref = f"mlx-community/whisper-{self._model_name}"

        logger.info("Loading mlx-whisper model: %s", self._model_ref)
        t0: float = time.monotonic()
        try:
            # Force the model into MLX's internal LRU cache by
            # transcribing one second of silence.  Also exercises
            # the full audio path so a broken model surfaces at load
            # time rather than on the first real utterance.
            silence = np.zeros(TARGET_SAMPLE_RATE, dtype=np.float32)
            mlx_whisper.transcribe(
                silence,
                path_or_hf_repo=self._model_ref,
                language=self._language,
            )
        except Exception as exc:
            raise STTEngineLoadError(
                f"mlx-whisper model load failed ({self._model_ref}): {exc}"
            ) from exc
        elapsed: float = time.monotonic() - t0
        logger.info("mlx-whisper model loaded in %.1fs", elapsed)

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        if not pcm:
            return ""
        try:
            audio = self._np.frombuffer(pcm, dtype=self._np.int16).astype(
                self._np.float32
            ) / 32768.0

            if sample_rate != TARGET_SAMPLE_RATE:
                from scipy.signal import resample_poly
                g = gcd(sample_rate, TARGET_SAMPLE_RATE)
                audio = resample_poly(
                    audio,
                    TARGET_SAMPLE_RATE // g,
                    sample_rate // g,
                ).astype(self._np.float32)

            t0: float = time.monotonic()
            result = self._mlx.transcribe(
                audio,
                path_or_hf_repo=self._model_ref,
                language=self._language,
            )
            text: str = (result.get("text") or "").strip()
            elapsed: float = time.monotonic() - t0
            logger.info(
                "mlx-whisper transcribed in %.2fs: '%s'",
                elapsed, text[:100],
            )
            return text
        except Exception as exc:
            logger.error("mlx-whisper transcription failed: %s", exc)
            return ""
