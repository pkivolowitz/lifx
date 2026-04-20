"""Speech-to-text facade for the voice coordinator.

Owns engine selection, load-time fallback, and the on-disk state file
the morning report reads to detect degraded STT.  The actual engine
implementations live in ``voice.coordinator.stt_engines``.

This file used to hold a single faster-whisper driver inline.  The
engines were extracted into a subpackage so MLX-Whisper (Apple Silicon
GPU) can be added as the primary on Daedalus with faster-whisper
retained as a CPU-only safety net.  See ``docs/36-stt-stack.md``.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "2.0"

import logging
from typing import Optional

from voice.coordinator.stt_engines import (
    FasterWhisperEngine,
    MockEngine,
    STTEngine,
    STTEngineLoadError,
    write_state,
)

logger: logging.Logger = logging.getLogger("glowup.voice.stt")


class SpeechToText:
    """Transcribe PCM audio to text via a pluggable engine.

    Stage-1 behaviour: always loads ``FasterWhisperEngine``.  Stage 3
    adds primary/fallback selection; stage 4 migrates the constructor
    to a config-dict signature.  The current kwargs are preserved so
    ``voice.coordinator.daemon`` does not need to change in lock-step
    with this refactor.
    """

    def __init__(
        self,
        model_size: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        engine: STTEngine = FasterWhisperEngine(
            model=model_size,
            device=device,
            compute_type=compute_type,
        )
        try:
            engine.load()
        except STTEngineLoadError:
            # Record the failure in the state file before re-raising so
            # operators have a paper trail even if the coordinator
            # can't start.  Stage 3 will catch and fall back instead.
            write_state(
                engine="none",
                fallback_reason=f"{engine.name} failed to load",
                primary_engine=engine.name,
            )
            raise
        self._engine: STTEngine = engine
        write_state(
            engine=engine.name,
            fallback_reason="",
            primary_engine=engine.name,
        )

    @property
    def engine_name(self) -> str:
        return self._engine.name

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        return self._engine.transcribe(pcm, sample_rate)


class MockSpeechToText:
    """Backwards-compatible alias for the mock STT engine.

    ``voice.coordinator.daemon`` still imports this name.  The
    underlying implementation lives in
    ``voice.coordinator.stt_engines.mock``.
    """

    def __init__(self, transcript: Optional[str] = None) -> None:
        self._engine: MockEngine = MockEngine(transcript=transcript)
        self._engine.load()

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        return self._engine.transcribe(pcm, sample_rate)
