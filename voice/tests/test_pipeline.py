"""Tests for voice pipeline — phrase cache, speak, process_utterance."""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import os
import tempfile
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock, patch

from voice.coordinator import pipeline
from voice.coordinator.pipeline import (
    _DISK_CACHE_MAX_WORDS,
    _elapsed_ms,
    _get_cached_phrase,
    _phrase_cache,
    _speak,
    _speak_cached,
    process_utterance,
)


# ---------------------------------------------------------------------------
# Fake components for testing
# ---------------------------------------------------------------------------

class FakeSTT:
    """Fake speech-to-text for testing."""

    def __init__(self, text: str = "turn on bedroom lights") -> None:
        self._text: str = text

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        """Return pre-set transcription."""
        return self._text


class RaisingSTT:
    """STT that always raises on transcribe — simulates total engine
    failure (e.g., both primary and fallback engines crashed at runtime).

    Used to verify that the pipeline catches the exception, emits a
    friendly error, and stays alive for the next utterance instead of
    leaving the satellite hanging.
    """

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc: Exception = exc or RuntimeError("engine unavailable")

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        raise self._exc


class FakeTTS:
    """Fake TTS that returns deterministic audio."""

    def __init__(self, audio: bytes = b"\x00" * 100) -> None:
        self._audio: bytes = audio

    def synthesize(self, text: str) -> tuple[bytes, int]:
        """Return pre-set audio."""
        return self._audio, 22050


class FailingTTS:
    """TTS that returns empty audio."""

    def synthesize(self, text: str) -> tuple[bytes, int]:
        """Return empty audio — synthesis failure."""
        return b"", 0


class FakePlayer:
    """Fake audio player that records calls."""

    def __init__(self) -> None:
        self.played: list[tuple[str, bytes]] = []

    def play(self, room: str, audio_bytes: bytes) -> bool:
        """Record the play call and return success."""
        self.played.append((room, audio_bytes))
        return True


class FakeIntentParser:
    """Fake intent parser."""

    def __init__(self, intent: dict[str, Any] | None = None) -> None:
        self._intent: dict[str, Any] = intent or {
            "action": "power",
            "target": "bedroom",
            "params": {"on": True},
        }

    def parse(self, text: str) -> dict[str, Any]:
        """Return pre-set intent."""
        return self._intent.copy()


class FakeExecutor:
    """Fake executor that records calls."""

    def __init__(
        self,
        result: dict[str, Any] | None = None,
        action_type: str = "command",
        action_label: str = "lights",
    ) -> None:
        self._result: dict[str, Any] = result or {
            "status": "ok",
            "confirmation": "Done.",
            "speak": False,
        }
        self._action_type: str = action_type
        self._action_label: str = action_label
        self.execute_calls: list[tuple[dict, str]] = []

    def execute(
        self, intent: dict[str, Any], room: str,
    ) -> dict[str, Any]:
        """Record and return result."""
        self.execute_calls.append((intent, room))
        return self._result

    def get_action_label(self, action: str) -> str:
        """Return pre-set label."""
        return self._action_label

    def get_action_type(self, action: str) -> str:
        """Return pre-set type."""
        return self._action_type


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestElapsedMs(unittest.TestCase):
    """Tests for _elapsed_ms helper."""

    def test_positive_duration(self) -> None:
        """Returns a positive number for a past timestamp."""
        import time
        t0: float = time.monotonic() - 0.1  # 100ms ago.
        ms: float = _elapsed_ms(t0)
        self.assertGreater(ms, 50)
        self.assertLess(ms, 500)

    def test_recent_timestamp(self) -> None:
        """Recent timestamp gives small duration."""
        import time
        t0: float = time.monotonic()
        ms: float = _elapsed_ms(t0)
        # Should be very close to 0.
        self.assertLess(ms, 50)


class TestPhraseCache(unittest.TestCase):
    """Tests for the phrase caching system."""

    def setUp(self) -> None:
        """Clear the cache between tests."""
        _phrase_cache.clear()

    def test_first_call_synthesizes(self) -> None:
        """First call to _get_cached_phrase generates audio."""
        tts = FakeTTS(audio=b"\x42" * 50)
        result = _get_cached_phrase("Got it.", tts)
        self.assertEqual(result, b"\x42" * 50)

    def test_second_call_uses_cache(self) -> None:
        """Second call returns cached audio without re-synthesizing."""
        call_count: int = 0

        class CountingTTS:
            def synthesize(self, text: str) -> tuple[bytes, int]:
                nonlocal call_count
                call_count += 1
                return b"\x42" * 50, 22050

        # Use a unique phrase and temp dir to avoid disk cache hits
        # from previous runs.
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(pipeline, "_PHRASE_CACHE_DIR", tmpdir):
                tts = CountingTTS()
                unique: str = f"unique test phrase {id(self)}"
                _get_cached_phrase(unique, tts)
                _get_cached_phrase(unique, tts)
                self.assertEqual(call_count, 1)

    def test_different_phrases_cached_separately(self) -> None:
        """Different phrases get separate cache entries."""
        tts = FakeTTS(audio=b"\x42" * 50)
        _get_cached_phrase("Got it.", tts)
        _get_cached_phrase("Waiting.", tts)
        self.assertIn("Got it.", _phrase_cache)
        self.assertIn("Waiting.", _phrase_cache)

    def test_synthesis_failure_returns_none(self) -> None:
        """Failed synthesis returns None."""
        tts = FailingTTS()
        result = _get_cached_phrase("anything", tts)
        self.assertIsNone(result)

    def test_disk_cache_short_phrase(self) -> None:
        """Short phrases are saved to and loaded from disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(pipeline, "_PHRASE_CACHE_DIR", tmpdir):
                tts = FakeTTS(audio=b"\xAA" * 30)
                # First call — generates and saves to disk.
                result1 = _get_cached_phrase("Hi.", tts)
                self.assertEqual(result1, b"\xAA" * 30)

                # Clear memory cache.
                _phrase_cache.clear()

                # Second call — should load from disk.
                result2 = _get_cached_phrase("Hi.", tts)
                self.assertEqual(result2, b"\xAA" * 30)


class TestSpeak(unittest.TestCase):
    """Tests for _speak function."""

    def test_no_tts_skips(self) -> None:
        """No TTS engine skips playback gracefully."""
        player = FakePlayer()
        _speak("test", "room", None, player, None)
        self.assertEqual(len(player.played), 0)

    def test_no_player_skips(self) -> None:
        """No player skips playback gracefully."""
        tts = FakeTTS()
        _speak("test", "room", tts, None, None)
        # No crash, no play.

    def test_successful_playback(self) -> None:
        """TTS + player produces playback."""
        tts = FakeTTS(audio=b"\x42" * 100)
        player = FakePlayer()
        _speak("hello", "bedroom", tts, player, None)
        self.assertEqual(len(player.played), 1)
        self.assertEqual(player.played[0][0], "bedroom")

    def test_notifier_not_called_by_speak(self) -> None:
        """_speak does NOT call notifier — the daemon brackets the pipeline.

        This avoids the True/False flicker between consecutive speaks.
        """
        tts = FakeTTS(audio=b"\x42" * 100)
        player = FakePlayer()
        calls: list[tuple[str, bool]] = []

        def notifier(room: str, playing: bool) -> None:
            calls.append((room, playing))

        _speak("hello", "bedroom", tts, player, notifier)
        self.assertEqual(len(calls), 0)

    def test_play_error_does_not_crash(self) -> None:
        """Player exception is caught — no crash."""
        tts = FakeTTS(audio=b"\x42" * 100)

        class FailingPlayer:
            def play(self, room: str, audio: bytes) -> bool:
                raise RuntimeError("play failed")

        # Should not raise.
        _speak("hello", "room", tts, FailingPlayer(), None)

    def test_empty_audio_skips_play(self) -> None:
        """Empty audio from TTS does not attempt playback."""
        tts = FailingTTS()
        player = FakePlayer()
        _speak("hello", "room", tts, player, None)
        self.assertEqual(len(player.played), 0)


class TestSpeakCached(unittest.TestCase):
    """Tests for _speak_cached function."""

    def setUp(self) -> None:
        _phrase_cache.clear()

    def test_caches_and_plays(self) -> None:
        """Phrase is cached and played."""
        tts = FakeTTS(audio=b"\x42" * 50)
        player = FakePlayer()
        _speak_cached("Got it.", "bedroom", tts, player, None)
        self.assertEqual(len(player.played), 1)
        self.assertIn("Got it.", _phrase_cache)

    def test_no_tts_skips(self) -> None:
        """No TTS skips gracefully."""
        player = FakePlayer()
        _speak_cached("Got it.", "room", None, player, None)
        self.assertEqual(len(player.played), 0)

    def test_notifier_not_called_by_speak_cached(self) -> None:
        """_speak_cached does NOT call notifier — daemon handles it."""
        tts = FakeTTS(audio=b"\x42" * 50)
        player = FakePlayer()
        calls: list[tuple[str, bool]] = []

        def notifier(room: str, playing: bool) -> None:
            calls.append((room, playing))

        _speak_cached("Got it.", "bedroom", tts, player, notifier)
        self.assertEqual(len(calls), 0)


class TestProcessUtterance(unittest.TestCase):
    """Tests for the full pipeline orchestration."""

    def test_empty_transcription_speaks_apology(self) -> None:
        """Empty STT result speaks 'didn't catch that'."""
        stt = FakeSTT(text="")
        intent = FakeIntentParser()
        executor = FakeExecutor()
        tts = FakeTTS()
        player = FakePlayer()

        result = process_utterance(
            "bedroom", b"\x00" * 100,
            {"sample_rate": 16000},
            stt, intent, executor, tts, player,
        )
        self.assertIsNone(result["intent"])
        # Player should have received the apology audio.
        self.assertGreater(len(player.played), 0)

    def test_stt_engine_failure_returns_friendly_error(self) -> None:
        """If both STT engines fail at transcribe time, the pipeline
        must catch the exception, publish a friendly error TTS, and
        return a normal error result — not propagate the exception
        upward and leave the satellite hanging.

        ``SpeechToText`` does load-time fallback only.  A runtime
        failure (CUDA crash, model file corruption, OOM, etc.) will
        raise out of ``transcribe()``; this test pins the contract
        that the pipeline absorbs it.
        """
        _phrase_cache.clear()
        published: list[tuple[str, str]] = []
        stt = RaisingSTT()
        intent = FakeIntentParser()
        executor = FakeExecutor()
        tts = FakeTTS()
        player = FakePlayer()

        result = process_utterance(
            "bedroom", b"\x00" * 100,
            {"sample_rate": 16000},
            stt, intent, executor, tts, player,
            tts_text_publisher=lambda r, t: published.append((r, t)),
        )

        self.assertEqual(result["result"]["status"], "error")
        self.assertTrue(result["result"]["speak"])
        # User-facing copy must not leak the exception type or message.
        confirm: str = result["result"]["confirmation"]
        self.assertNotIn("RuntimeError", confirm)
        self.assertNotIn("engine unavailable", confirm)
        # Friendly message reached both the local speaker and the MQTT
        # text channel that the satellite consumes.
        self.assertGreater(len(player.played), 0)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0][0], "bedroom")
        self.assertIn("speech engine", published[0][1].lower())

    def test_command_says_got_it(self) -> None:
        """Successful command plays 'Got it.'"""
        _phrase_cache.clear()
        stt = FakeSTT(text="turn on bedroom")
        intent = FakeIntentParser(intent={
            "action": "power", "target": "bedroom", "params": {"on": True},
        })
        executor = FakeExecutor(
            result={"status": "ok", "confirmation": "ok", "speak": False},
            action_type="command",
            action_label="lights",
        )
        tts = FakeTTS()
        player = FakePlayer()

        result = process_utterance(
            "bedroom", b"\x00" * 100,
            {"sample_rate": 16000},
            stt, intent, executor, tts, player,
        )
        self.assertEqual(result["result"]["status"], "ok")
        # Should have played "Got it." (cached).
        self.assertGreater(len(player.played), 0)

    def test_fast_query_skips_ack(self) -> None:
        """Fast queries (query_sensor) skip 'Waiting on...' ack."""
        _phrase_cache.clear()
        stt = FakeSTT(text="what's the temperature")
        intent = FakeIntentParser(intent={
            "action": "query_sensor", "target": "bedroom",
            "params": {"sensor_type": "temperature"},
        })
        executor = FakeExecutor(
            result={
                "status": "ok",
                "confirmation": "Bedroom is 72 degrees.",
                "speak": True,
            },
            action_type="query",
            action_label="sensors",
        )
        tts = FakeTTS()
        player = FakePlayer()

        result = process_utterance(
            "bedroom", b"\x00" * 100,
            {"sample_rate": 16000},
            stt, intent, executor, tts, player,
        )
        # Fast queries skip ack — result only = 1 play.
        self.assertEqual(len(player.played), 1)

    def test_chat_injects_full_transcription(self) -> None:
        """Chat action gets the full transcription as message."""
        stt = FakeSTT(text="tell me about the solar system")
        intent = FakeIntentParser(intent={
            "action": "chat", "target": "",
            "params": {"message": "tell me"},  # LLM truncated.
        })
        executor = FakeExecutor(
            result={"status": "ok", "confirmation": "The solar system...", "speak": True},
            action_type="chat",
            action_label="assistant",
        )

        result = process_utterance(
            "bedroom", b"\x00" * 100,
            {"sample_rate": 16000},
            stt, intent, executor,
        )
        # Executor should have received the full text, not the truncated one.
        called_intent = executor.execute_calls[0][0]
        self.assertEqual(
            called_intent["params"]["message"],
            "tell me about the solar system",
        )

    def test_pipeline_returns_latency(self) -> None:
        """Pipeline result includes latency_ms."""
        stt = FakeSTT(text="test")
        intent = FakeIntentParser()
        executor = FakeExecutor()

        result = process_utterance(
            "room", b"\x00", {"sample_rate": 16000},
            stt, intent, executor,
        )
        self.assertIn("latency_ms", result)
        self.assertIsInstance(result["latency_ms"], float)
        self.assertGreaterEqual(result["latency_ms"], 0)

    def test_pipeline_returns_text_and_intent(self) -> None:
        """Pipeline result includes transcribed text and parsed intent."""
        stt = FakeSTT(text="stop everything")
        intent_data: dict[str, Any] = {
            "action": "stop", "target": "all", "params": {},
        }
        intent = FakeIntentParser(intent=intent_data)
        executor = FakeExecutor()

        result = process_utterance(
            "room", b"\x00", {"sample_rate": 16000},
            stt, intent, executor,
        )
        self.assertEqual(result["text"], "stop everything")
        self.assertEqual(result["intent"]["action"], "stop")
        self.assertEqual(result["room"], "room")


class TestNoNotifierFlicker(unittest.TestCase):
    """Verify the duplicate notifier flicker is fixed.

    _speak and _speak_cached no longer call the notifier themselves.
    The daemon brackets the entire pipeline with a single True/False
    pair, eliminating the momentary False gap between consecutive speaks.
    """

    def test_consecutive_speaks_no_notifier_calls(self) -> None:
        """Two consecutive _speak calls produce zero notifier calls."""
        tts = FakeTTS(audio=b"\x42" * 50)
        player = FakePlayer()
        calls: list[tuple[str, bool]] = []

        def notifier(room: str, playing: bool) -> None:
            calls.append((room, playing))

        _speak("first", "room", tts, player, notifier)
        _speak("second", "room", tts, player, notifier)

        # No flicker — daemon is responsible for notifications.
        self.assertEqual(len(calls), 0)


if __name__ == "__main__":
    unittest.main()
