"""Pluggable STT engines for the voice coordinator.

Each engine is a concrete implementation of the ``STTEngine`` protocol
defined in ``base.py``.  The ``SpeechToText`` facade in
``voice.coordinator.stt`` selects and instantiates engines based on
configuration and handles load-time fallback.

Engines:
    ``FasterWhisperEngine`` — CTranslate2 port of Whisper, CPU on Apple
        Silicon (Metal requires manual build).  Cross-platform.
    ``MLXWhisperEngine`` — Apple MLX framework; uses the M-series GPU
        via unified memory.  macOS arm64 only.
    ``MockEngine``        — Deterministic or interactive mock for tests.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from voice.coordinator.stt_engines.base import (
    STTEngine,
    STTEngineLoadError,
    pcm_to_wav,
    write_state,
)
from voice.coordinator.stt_engines.faster_whisper import FasterWhisperEngine
from voice.coordinator.stt_engines.mlx_whisper import MLXWhisperEngine
from voice.coordinator.stt_engines.mock import MockEngine

__all__ = [
    "STTEngine",
    "STTEngineLoadError",
    "pcm_to_wav",
    "write_state",
    "FasterWhisperEngine",
    "MLXWhisperEngine",
    "MockEngine",
]
