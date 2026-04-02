"""Wake word detection with VAD and sliding confidence window.

Wraps openWakeWord with additional false-positive protection:
- Silero VAD integration (built into openWakeWord)
- Sliding confidence window (N consecutive frames above threshold)
- Cooldown period after detection

For development without a trained model, ``MockWakeDetector``
triggers on a keyboard press (Enter key) instead.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import time
from typing import Optional

import numpy as np

logger: logging.Logger = logging.getLogger("glowup.voice.wake")

# ---------------------------------------------------------------------------
# Optional openWakeWord import
# ---------------------------------------------------------------------------

try:
    from openwakeword.model import Model as OWWModel
    _HAS_OWW: bool = True
except ImportError:
    OWWModel = None  # type: ignore[assignment, misc]
    _HAS_OWW = False


class WakeDetector:
    """Detect wake word using openWakeWord with safety filters.

    Args:
        model_path:        Path to the .onnx wake word model.
        threshold:         Confidence threshold (0.0--1.0).
        vad_threshold:     Silero VAD threshold (0 = disabled).
        confidence_window: Consecutive frames above threshold needed.
        cooldown:          Seconds to suppress re-detection after trigger.
    """

    def __init__(
        self,
        model_path: str,
        threshold: float = 0.5,
        vad_threshold: float = 0.5,
        confidence_window: int = 2,
        cooldown: float = 6.0,
    ) -> None:
        """Initialize the wake word detector."""
        if not _HAS_OWW:
            raise ImportError(
                "openWakeWord not installed — "
                "pip install openwakeword"
            )

        self._threshold: float = threshold
        self._window_size: int = confidence_window
        self._cooldown: float = cooldown
        self._last_trigger: float = 0.0
        self._consecutive: int = 0

        # Determine the model name from the filename.
        # openWakeWord uses the stem as the prediction key.
        import os
        self._model_name: str = os.path.splitext(
            os.path.basename(model_path),
        )[0]

        # openWakeWord API varies by version:
        # 0.4.x uses wakeword_model_paths, 0.6+ uses wakeword_models.
        import inspect
        params = inspect.signature(OWWModel.__init__).parameters
        if "wakeword_model_paths" in params:
            self._model = OWWModel(
                wakeword_model_paths=[model_path],
                vad_threshold=vad_threshold,
            )
        else:
            self._model = OWWModel(
                wakeword_models=[model_path],
                inference_framework="onnx",
                vad_threshold=vad_threshold,
            )

        logger.info(
            "WakeDetector initialized: model=%s threshold=%.2f "
            "vad=%.2f window=%d cooldown=%.1fs",
            self._model_name, threshold, vad_threshold,
            confidence_window, cooldown,
        )

    def feed(self, audio_chunk: np.ndarray) -> Optional[float]:
        """Feed an audio chunk and check for wake word.

        Args:
            audio_chunk: NumPy array of int16 PCM samples (one frame).

        Returns:
            Wake word confidence score if triggered, None otherwise.
        """
        # Cooldown — suppress during capture/response.
        now: float = time.monotonic()
        if now - self._last_trigger < self._cooldown:
            return None

        prediction: dict = self._model.predict(audio_chunk)
        score: float = prediction.get(self._model_name, 0.0)

        if score >= self._threshold:
            self._consecutive += 1
            if self._consecutive >= self._window_size:
                self._last_trigger = now
                self._consecutive = 0
                logger.info(
                    "Wake word detected (score=%.3f)", score,
                )
                return score
        else:
            self._consecutive = 0

        return None

    def reset(self) -> None:
        """Reset internal state after processing an utterance."""
        self._consecutive = 0
        self._model.reset()


class MockWakeDetector:
    """Mock wake detector for development without a trained model.

    Triggers on a threading Event instead of audio analysis.
    Used when testing the pipeline with a laptop mic.

    Args:
        cooldown: Seconds to suppress re-detection after trigger.
    """

    def __init__(self, cooldown: float = 8.0) -> None:
        """Initialize the mock wake detector."""
        self._cooldown: float = cooldown
        self._last_trigger: float = 0.0
        self._triggered: bool = False

    def trigger(self) -> None:
        """Manually trigger the wake word (called from keyboard handler)."""
        self._triggered = True

    def feed(self, audio_chunk: np.ndarray) -> Optional[float]:
        """Check if manually triggered.

        Args:
            audio_chunk: Ignored (mock detector doesn't analyze audio).

        Returns:
            1.0 if triggered, None otherwise.
        """
        now: float = time.monotonic()
        if now - self._last_trigger < self._cooldown:
            self._triggered = False
            return None

        if self._triggered:
            self._triggered = False
            self._last_trigger = now
            return 1.0

        return None

    def reset(self) -> None:
        """Reset state."""
        self._triggered = False
