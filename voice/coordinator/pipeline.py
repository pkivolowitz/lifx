"""Voice processing pipeline — STT, intent, execute, TTS, play.

Orchestrates the full voice command flow from raw PCM audio to
response playback.  Each step is a separate module; this function
wires them together.

Common phrases ("Got it.", "Waiting on the ___") are pre-generated
at first use and cached so they play instantly without TTS latency.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.2"

import logging
import os
import time
from typing import Any, Callable, Optional, Protocol

from voice import constants as C

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
    def get_action_label(self, action: str) -> str: ...
    def get_action_type(self, action: str) -> str: ...


class TTSLike(Protocol):
    """Anything with a synthesize(text) -> (bytes, sample_rate) method."""
    def synthesize(self, text: str) -> tuple[bytes, int]: ...


class PlayerLike(Protocol):
    """Anything with a play(room, audio_bytes) -> bool method."""
    def play(self, room: str, audio_bytes: bytes) -> bool: ...


class PlaybackNotifier(Protocol):
    """Callback to notify satellites about playback state."""
    def __call__(self, room: str, playing: bool) -> None: ...


class TTSTextPublisher(Protocol):
    """Callback to publish TTS text for satellite-local speech."""
    def __call__(self, room: str, text: str) -> None: ...


# ---------------------------------------------------------------------------
# Epoch staleness check
# ---------------------------------------------------------------------------

def _is_stale(epoch: int, get_epoch: Optional[Callable[[], int]]) -> bool:
    """Check if the current pipeline invocation has been superseded.

    A flush command increments the coordinator's epoch.  Any pipeline
    whose captured epoch no longer matches the coordinator's current
    epoch should abort — its response is stale and would confuse the
    user if spoken.

    Args:
        epoch:     Epoch captured at pipeline entry.
        get_epoch: Callable returning the coordinator's current epoch.

    Returns:
        True if this pipeline is stale and should abort.
    """
    if get_epoch is None:
        return False
    return get_epoch() != epoch


# ---------------------------------------------------------------------------
# Phrase cache — pre-generate common TTS phrases on first use
# ---------------------------------------------------------------------------

# Disk-backed phrase cache for short TTS phrases.
# Phrases of 2 words or fewer are saved to disk so they survive restarts.
# Longer phrases are cached in memory only (session lifetime).
_PHRASE_CACHE_DIR: str = os.path.join(
    os.path.expanduser("~"), ".glowup_phrase_cache",
)

# In-memory cache: phrase → WAV bytes.
_phrase_cache: dict[str, bytes] = {}

# Maximum word count for disk persistence.
# "Waiting on the water sensor." = 5 words. Cache all stock phrases.
_DISK_CACHE_MAX_WORDS: int = 6


def _phrase_cache_path(phrase: str) -> str:
    """Get the disk cache path for a phrase.

    Args:
        phrase: Text to cache.

    Returns:
        File path for the cached WAV.
    """
    import hashlib
    # Use hash to avoid filesystem issues with special characters.
    key: str = hashlib.md5(phrase.encode()).hexdigest()
    return os.path.join(_PHRASE_CACHE_DIR, f"{key}.wav")


def _get_cached_phrase(
    phrase: str, tts: "TTSLike",
) -> Optional[bytes]:
    """Get cached WAV bytes for a phrase, generating on first use.

    Short phrases (2 words or fewer) are persisted to disk.
    Longer phrases are cached in memory only.

    Args:
        phrase: Text to synthesize.
        tts:    TTS engine.

    Returns:
        WAV bytes, or None on failure.
    """
    # Check memory cache first.
    if phrase in _phrase_cache:
        return _phrase_cache[phrase]

    # Check disk cache for short phrases.
    word_count: int = len(phrase.split())
    disk_path: str = _phrase_cache_path(phrase)

    if word_count <= _DISK_CACHE_MAX_WORDS and os.path.exists(disk_path):
        try:
            with open(disk_path, "rb") as f:
                audio: bytes = f.read()
            _phrase_cache[phrase] = audio
            logger.info(
                "Loaded cached phrase from disk: '%s' (%d bytes)",
                phrase, len(audio),
            )
            return audio
        except Exception as exc:
            logger.warning("Failed to read phrase cache: %s", exc)

    # Generate via TTS.
    audio, sr = tts.synthesize(phrase)
    if not audio:
        return None

    _phrase_cache[phrase] = audio
    logger.info("Cached phrase: '%s' (%d bytes)", phrase, len(audio))

    # Persist short phrases to disk.
    if word_count <= _DISK_CACHE_MAX_WORDS:
        try:
            os.makedirs(_PHRASE_CACHE_DIR, exist_ok=True)
            with open(disk_path, "wb") as f:
                f.write(audio)
            logger.info("Saved phrase to disk: '%s'", phrase)
        except Exception as exc:
            logger.warning("Failed to write phrase cache: %s", exc)

    return audio


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
    playback_notifier: Optional[PlaybackNotifier] = None,
    tts_text_publisher: Optional[TTSTextPublisher] = None,
    epoch: int = 0,
    get_epoch: Optional[Callable[[], int]] = None,
    on_flush: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """Process a voice utterance through the full pipeline.

    Steps:
    - STT: transcribe PCM audio to text
    - Flush check: if the utterance is "flush it", cancel all
      in-flight work and confirm
    - Intent: parse text into structured intent via LLM
    - Execute: dispatch intent to GlowUp API
    - TTS + Play: if the response should be spoken, synthesize
      and stream to the room's speaker

    Each expensive step checks the epoch counter first.  If a flush
    command has been processed since this pipeline started, the epoch
    will have advanced and this pipeline aborts silently.

    Args:
        room:               Room name (from satellite).
        pcm:                Raw PCM audio bytes.
        meta:               Message metadata (sample_rate, timestamp, etc.).
        stt:                Speech-to-text engine.
        intent_parser:      Intent parser (Ollama or mock).
        executor:           GlowUp API executor.
        tts:                Text-to-speech engine (optional).
        player:             Audio player for response (optional).
        tts_text_publisher: Callback to publish text for satellite-local TTS.
        epoch:              Epoch counter at time of dispatch.
        get_epoch:          Returns coordinator's current epoch.
        on_flush:           Called when flush command detected — increments
                            epoch and broadcasts to satellites.

    Returns:
        Pipeline result dict with ``text``, ``intent``, ``result``,
        ``latency_ms``.  Includes ``aborted: True`` if superseded
        by a flush.
    """
    t0: float = time.monotonic()
    sample_rate: int = meta.get("sample_rate", 16000)

    # Step 1: Speech-to-text.
    text: str = stt.transcribe(pcm, sample_rate)

    if not text:
        logger.info("[%s] Empty transcription — nothing heard", room)
        _no_catch: str = "Sorry, I didn't catch that."
        _speak(
            _no_catch,
            room, tts, player, playback_notifier,
        )
        if tts_text_publisher:
            tts_text_publisher(room, _no_catch)
        return {
            "room": room,
            "text": "",
            "intent": None,
            "result": None,
            "latency_ms": _elapsed_ms(t0),
        }

    logger.info("[%s] Heard: '%s'", room, text)

    # Step 1.5: Flush check — intercept before intent parsing.
    # "Hey <wake_word> flush it" → STT produces "flush it".
    if text.strip().lower() in C.FLUSH_PATTERNS:
        logger.info("[%s] FLUSH command detected", room)
        if on_flush is not None:
            on_flush()
        _flush_msg: str = "Flushed."
        _speak_cached(_flush_msg, room, tts, player, playback_notifier)
        if tts_text_publisher:
            tts_text_publisher(room, _flush_msg)
        return {
            "room": room,
            "text": text,
            "intent": {"action": "flush"},
            "result": {"status": "ok", "confirmation": _flush_msg},
            "latency_ms": _elapsed_ms(t0),
        }

    # Epoch check — abort if a flush arrived while STT was running.
    if _is_stale(epoch, get_epoch):
        logger.info("[%s] Pipeline aborted after STT (epoch stale)", room)
        return {
            "room": room, "text": text, "intent": None,
            "result": None, "aborted": True,
            "latency_ms": _elapsed_ms(t0),
        }

    # Step 2: Intent parsing.
    intent: dict[str, Any] = intent_parser.parse(text)

    # For chat actions, ensure the full transcription is passed as the
    # message — the intent LLM sometimes truncates it.
    if intent.get("action") == "chat":
        intent.setdefault("params", {})["message"] = text

    logger.info("[%s] Intent: %s", room, intent)

    # Epoch check — abort if flushed during intent parsing.
    if _is_stale(epoch, get_epoch):
        logger.info("[%s] Pipeline aborted after intent (epoch stale)", room)
        return {
            "room": room, "text": text, "intent": intent,
            "result": None, "aborted": True,
            "latency_ms": _elapsed_ms(t0),
        }

    # Step 2.5: Acknowledge — speak "Waiting on the {label}" only for
    # slow actions (chat, weather).  Commands are instant (physical change
    # is the feedback).  Queries hit local APIs and return in milliseconds
    # — the "Waiting" TTS + AirPlay takes longer than the query itself.
    _SLOW_ACTIONS: set[str] = {"chat", "query_weather"}
    action_name: str = intent.get("action", "")
    action_label: str = executor.get_action_label(action_name)
    if action_name in _SLOW_ACTIONS and action_label:
        _wait_msg: str = f"Waiting on the {action_label}."
        _speak_cached(
            _wait_msg,
            room, tts, player, playback_notifier,
        )
        if tts_text_publisher:
            tts_text_publisher(room, _wait_msg)

    # Step 3: Execute against GlowUp.
    result: dict[str, Any] = executor.execute(intent, room)
    logger.info("[%s] Result: %s", room, result)

    # Epoch check — abort if flushed during execution.
    if _is_stale(epoch, get_epoch):
        logger.info("[%s] Pipeline aborted after execute (epoch stale)", room)
        return {
            "room": room, "text": text, "intent": intent,
            "result": result, "aborted": True,
            "latency_ms": _elapsed_ms(t0),
        }

    # Step 4: Speak response.
    confirmation: str = result.get("confirmation", "")
    should_speak: bool = result.get("speak", False)

    if should_speak and confirmation:
        # Queries and chat: speak the full result.
        _speak(confirmation, room, tts, player, playback_notifier)
        if tts_text_publisher:
            tts_text_publisher(room, confirmation)
    elif not should_speak and result.get("status") == "ok":
        # Commands: physical change is the feedback, just say "Got it."
        _speak_cached("Got it.", room, tts, player, playback_notifier)
        if tts_text_publisher:
            tts_text_publisher(room, "Got it.")

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


def _play_audio(
    audio: bytes,
    room: str,
    player: PlayerLike,
    label: str,
) -> None:
    """Stream audio to a room's speaker.

    The daemon-level notifier brackets the entire pipeline, so
    individual speak calls do NOT send their own True/False — that
    caused a momentary un-suppression gap between consecutive speaks.

    Args:
        audio:  WAV audio bytes.
        room:   Target room.
        player: Audio player.
        label:  Descriptive label for logging.
    """
    player.play(room, audio)
    logger.info("[%s] Spoke%s", room, f" ({label})" if label else "")


def _speak(
    text: str,
    room: str,
    tts: Optional[TTSLike],
    player: Optional[PlayerLike],
    notifier: Optional[PlaybackNotifier] = None,
) -> None:
    """Synthesize text and play it to the room's speaker.

    If no player is configured, tries ``tts.speak_direct()`` to
    output through the local speakers (macOS ``say`` with no file).

    Args:
        text:     Confirmation text to speak.
        room:     Target room for audio playback.
        tts:      TTS engine (optional — skipped if None).
        player:   Audio player (optional — skipped if None).
        notifier: Unused — kept for API compatibility.
    """
    if tts is None:
        logger.info("[%s] Would speak: '%s' (no TTS)", room, text)
        return

    try:
        if player is None:
            # No AirPlay — satellites handle TTS locally via MQTT.
            # Skip speak_direct so we don't block on macOS 'say'.
            logger.info("[%s] TTS delegated to satellite: %s", room, text[:40])
            return

        audio, sample_rate = tts.synthesize(text)
        if audio:
            _play_audio(audio, room, player, text[:40])
    except Exception as exc:
        logger.error("[%s] TTS/playback failed: %s", room, exc)


def _speak_cached(
    text: str,
    room: str,
    tts: Optional[TTSLike],
    player: Optional[PlayerLike],
    notifier: Optional[PlaybackNotifier] = None,
) -> None:
    """Speak a cacheable phrase — skips TTS after first synthesis.

    First call generates and caches the WAV. Subsequent calls
    stream the cached bytes directly to AirPlay, eliminating
    the 2-second TTS + afconvert overhead.  If no player is
    configured, uses ``tts.speak_direct()`` for local speakers.

    Args:
        text:     Phrase to speak (used as cache key).
        room:     Target room.
        tts:      TTS engine (for first-time generation).
        player:   Audio player.
        notifier: Unused — see _speak docstring.
    """
    if tts is None:
        logger.info("[%s] Would speak: '%s' (no TTS)", room, text)
        return

    if player is None:
        # Satellites handle TTS locally via MQTT.
        logger.info("[%s] TTS delegated to satellite (cached): %s", room, text[:30])
        return

    try:
        audio: Optional[bytes] = _get_cached_phrase(text, tts)
        if audio:
            _play_audio(audio, room, player, f"cached: {text[:30]}")
    except Exception as exc:
        logger.error("[%s] Cached playback failed: %s", room, exc)


def _elapsed_ms(t0: float) -> float:
    """Return elapsed time since t0 in milliseconds."""
    return (time.monotonic() - t0) * 1000
