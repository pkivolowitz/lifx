"""Mock STT engine for tests and interactive debugging."""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

from typing import Optional


class MockEngine:
    """Deterministic or interactive STT mock.

    If ``transcript`` is provided, every ``transcribe()`` returns that
    string.  Otherwise each call prompts stdin for a typed utterance,
    which is useful when exercising the coordinator locally without
    audio hardware.
    """

    name: str = "mock"

    def __init__(self, transcript: Optional[str] = None) -> None:
        self._transcript: Optional[str] = transcript

    @classmethod
    def is_available(cls) -> bool:
        return True

    def load(self) -> None:
        return None

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        if self._transcript is not None:
            return self._transcript
        try:
            return input("[MOCK STT] Type what you said: ").strip()
        except EOFError:
            return ""
