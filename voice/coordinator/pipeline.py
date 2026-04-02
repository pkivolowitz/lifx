"""Voice processing pipeline — STT, intent, execute, TTS, play.

Orchestrates the full voice command flow from raw PCM audio to
response playback.  Each step is a separate module; this function
wires them together.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import time
from typing import Any, Optional, Protocol

logger: logging.Logger = logging.getLogger("glowup.voice.pipeline")


# ---------------------------------------------------------------------------
# Protocol interfaces (duck typing contracts)
# ---------------------------------------------------------------------------

class STTLike(Protocol):
    """Anything with a transcribe(pcm, sample_rate) -> str method."""
    def transcribe(self, pcm: bytes, sample_rate: int) -> str: ...


class IntentLike(Protocol):
    """Anything with a parse(text) -> dict method."""
    def parse(self, text: str) -> dict[str, Any]: ...


class ExecutorLike(Protocol):
    """Anything with an execute(intent, room) -> dict method."""
    def execute(self, intent: dict[str, Any], room: str) -> dict[str, Any]: ...


class TTSLike(Protocol):
    """Anything with a synthesize(text) -> (bytes, sample_rate) method."""
    def synthesize(self, text: str) -> tuple[bytes, int]: ...


class PlayerLike(Protocol):
    """Anything with a play(room, audio_bytes) -> bool method."""
    def play(self, room: str, audio_bytes: bytes) -> bool: ...


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def process_utterance(
    room: str,
    pcm: bytes,
    meta: dict[str, Any],
    stt: STTLike,
    intent_parser: IntentLike,
    executor: ExecutorLike,
    tts: Optional[TTSLike] = None,
    player: Optional[PlayerLike] = None,
) -> dict[str, Any]:
    """Process a voice utterance through the full pipeline.

    Steps:
    - STT: transcribe PCM audio to text
    - Intent: parse text into structured intent via LLM
    - Execute: dispatch intent to GlowUp API
    - TTS + Play: if the response should be spoken, synthesize
      and stream to the room's speaker

    Args:
        room:          Room name (from satellite).
        pcm:           Raw PCM audio bytes.
        meta:          Message metadata (sample_rate, timestamp, etc.).
        stt:           Speech-to-text engine.
        intent_parser: Intent parser (Ollama or mock).
        executor:      GlowUp API executor.
        tts:           Text-to-speech engine (optional).
        player:        Audio player for response (optional).

    Returns:
        Pipeline result dict with ``text``, ``intent``, ``result``,
        ``latency_ms``.
    """
    t0: float = time.monotonic()
    sample_rate: int = meta.get("sample_rate", 16000)

    # Step 1: Speech-to-text.
    text: str = stt.transcribe(pcm, sample_rate)

    if not text:
        logger.info("[%s] Empty transcription — nothing heard", room)
        _speak(
            "Sorry, I didn't catch that.",
            room, tts, player,
        )
        return {
            "room": room,
            "text": "",
            "intent": None,
            "result": None,
            "latency_ms": _elapsed_ms(t0),
        }

    logger.info("[%s] Heard: '%s'", room, text)

    # Step 2: Intent parsing.
    intent: dict[str, Any] = intent_parser.parse(text)
    logger.info("[%s] Intent: %s", room, intent)

    # Step 3: Execute against GlowUp.
    result: dict[str, Any] = executor.execute(intent, room)
    logger.info("[%s] Result: %s", room, result)

    # Step 4: Speak response (if applicable).
    confirmation: str = result.get("confirmation", "")
    should_speak: bool = result.get("speak", False)

    if should_speak and confirmation:
        _speak(confirmation, room, tts, player)

    latency: float = _elapsed_ms(t0)
    logger.info(
        "[%s] Pipeline complete in %.0fms: '%s' → %s → '%s'%s",
        room, latency, text, intent.get("action", "?"),
        confirmation, " (spoken)" if should_speak else "",
    )

    return {
        "room": room,
        "text": text,
        "intent": intent,
        "result": result,
        "latency_ms": latency,
    }


def _speak(
    text: str,
    room: str,
    tts: Optional[TTSLike],
    player: Optional[PlayerLike],
) -> None:
    """Synthesize text and play it to the room's speaker.

    Args:
        text:   Confirmation text to speak.
        room:   Target room for audio playback.
        tts:    TTS engine (optional — skipped if None).
        player: Audio player (optional — skipped if None).
    """
    if tts is None or player is None:
        logger.info("[%s] Would speak: '%s' (no TTS/player)", room, text)
        return

    try:
        audio, sample_rate = tts.synthesize(text)
        if audio:
            player.play(room, audio)
            logger.info("[%s] Spoke: '%s'", room, text)
    except Exception as exc:
        logger.error("[%s] TTS/playback failed: %s", room, exc)


def _elapsed_ms(t0: float) -> float:
    """Return elapsed time since t0 in milliseconds."""
    return (time.monotonic() - t0) * 1000
