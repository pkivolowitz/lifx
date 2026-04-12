"""Tests for intent parsing with mock Ollama responses."""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import unittest
from unittest.mock import MagicMock, patch
from typing import Any

from voice.coordinator.intent import IntentParser, MockIntentParser


class TestMockIntentParser(unittest.TestCase):
    """Tests for the MockIntentParser."""

    def test_fixed_intent(self) -> None:
        """Returns the pre-set intent for any text."""
        intent = {"action": "power", "target": "bedroom", "params": {"on": False}}
        parser = MockIntentParser(intent=intent)
        result = parser.parse("turn off the bedroom lights")
        self.assertEqual(result, intent)

    def test_fixed_intent_ignores_text(self) -> None:
        """Pre-set intent is returned regardless of input text."""
        intent = {"action": "stop", "target": "all", "params": {}}
        parser = MockIntentParser(intent=intent)
        self.assertEqual(parser.parse("something completely different"), intent)
        self.assertEqual(parser.parse(""), intent)

    def test_should_refresh_always_false(self) -> None:
        """Mock parser never needs capability refresh."""
        parser = MockIntentParser()
        self.assertFalse(parser.should_refresh())

    def test_refresh_capabilities_is_noop(self) -> None:
        """Calling refresh on mock does not raise."""
        parser = MockIntentParser()
        parser.refresh_capabilities("http://fake", "fake-token")


class TestIntentParserPrompt(unittest.TestCase):
    """Tests for IntentParser system prompt construction."""

    def test_build_system_prompt_without_capabilities(self) -> None:
        """System prompt works with no capabilities loaded."""
        parser = IntentParser.__new__(IntentParser)
        parser._model = "test"
        parser._ollama_host = "http://localhost:11434"
        parser._timeout = 10.0
        parser._max_retries = 1
        parser._capabilities_text = ""
        parser._capabilities_last_refresh = 0.0

        prompt = parser._build_system_prompt()
        self.assertIn("voice command parser", prompt)
        self.assertIn("power", prompt)
        self.assertIn("play_effect", prompt)
        self.assertIn("No capability data", prompt)

    def test_build_system_prompt_with_capabilities(self) -> None:
        """System prompt includes capabilities when loaded."""
        parser = IntentParser.__new__(IntentParser)
        parser._model = "test"
        parser._ollama_host = "http://localhost:11434"
        parser._timeout = 10.0
        parser._max_retries = 1
        parser._capabilities_text = "Available effects: cylon, breathe, rainbow"
        parser._capabilities_last_refresh = 0.0

        prompt = parser._build_system_prompt()
        self.assertIn("cylon", prompt)
        self.assertIn("breathe", prompt)
        self.assertIn("rainbow", prompt)
        self.assertNotIn("No capability data", prompt)

    def test_prompt_includes_all_actions(self) -> None:
        """System prompt documents all supported action types."""
        parser = IntentParser.__new__(IntentParser)
        parser._model = "test"
        parser._ollama_host = "http://localhost:11434"
        parser._timeout = 10.0
        parser._max_retries = 1
        parser._capabilities_text = ""
        parser._capabilities_last_refresh = 0.0

        prompt = parser._build_system_prompt()
        for action in [
            "power", "brightness", "color", "temperature",
            "play_effect", "stop", "query_sensor", "query_power",
            "query_status", "scene",
        ]:
            self.assertIn(action, prompt, f"Missing action: {action}")

    def test_prompt_includes_voice_gate_actions(self) -> None:
        """System prompt documents the voice-gate enable/disable actions.

        Regression guard: these lines drive the Ollama parser for the
        doorbell gate feature.  Accidentally removing them would
        silently downgrade "enable the porch" into a chat action.
        """
        parser = IntentParser.__new__(IntentParser)
        parser._model = "test"
        parser._ollama_host = "http://localhost:11434"
        parser._timeout = 10.0
        parser._max_retries = 1
        parser._capabilities_text = ""
        parser._capabilities_last_refresh = 0.0

        prompt = parser._build_system_prompt()
        self.assertIn("enable_voice_gate", prompt)
        self.assertIn("disable_voice_gate", prompt)
        # Duration parameter must be advertised so the LLM emits it.
        self.assertIn("duration_seconds", prompt)
        # An example containing "two hours" -> 7200 pins the unit.
        self.assertIn("7200", prompt)
        # A zero-duration fall-through example must exist so the
        # executor can ask "how long?" instead of silently defaulting.
        self.assertIn('"duration_seconds": 0', prompt)


class TestIntentParserParse(unittest.TestCase):
    """Tests for IntentParser.parse() with mocked Ollama."""

    def _make_parser(self) -> IntentParser:
        """Create a parser instance without calling __init__."""
        parser = IntentParser.__new__(IntentParser)
        parser._model = "test"
        parser._ollama_host = "http://localhost:11434"
        parser._timeout = 10.0
        parser._max_retries = 1
        parser._capabilities_text = ""
        parser._capabilities_last_refresh = 0.0
        return parser

    def test_empty_text_returns_unknown(self) -> None:
        """Empty input returns unknown action."""
        parser = self._make_parser()
        result = parser.parse("")
        self.assertEqual(result["action"], "unknown")

    def test_whitespace_only_returns_unknown(self) -> None:
        """Whitespace-only input returns unknown action."""
        parser = self._make_parser()
        result = parser.parse("   ")
        self.assertEqual(result["action"], "unknown")

    @patch.object(IntentParser, "_call_ollama")
    def test_valid_response_returned(self, mock_call: MagicMock) -> None:
        """Valid Ollama response is returned as-is."""
        expected = {"action": "power", "target": "bedroom", "params": {"on": False}}
        mock_call.return_value = expected
        parser = self._make_parser()
        result = parser.parse("turn off bedroom")
        self.assertEqual(result, expected)

    @patch.object(IntentParser, "_call_ollama")
    def test_none_response_retries(self, mock_call: MagicMock) -> None:
        """None response triggers retry."""
        expected = {"action": "stop", "target": "all", "params": {}}
        mock_call.side_effect = [None, expected]
        parser = self._make_parser()
        result = parser.parse("stop everything")
        self.assertEqual(result, expected)
        self.assertEqual(mock_call.call_count, 2)

    @patch.object(IntentParser, "_call_ollama")
    def test_all_retries_exhausted(self, mock_call: MagicMock) -> None:
        """All retries failing returns unknown."""
        mock_call.return_value = None
        parser = self._make_parser()
        result = parser.parse("gibberish input")
        self.assertEqual(result["action"], "unknown")
        # 1 initial + 1 retry = 2 calls.
        self.assertEqual(mock_call.call_count, 2)

    @patch.object(IntentParser, "_call_ollama")
    def test_non_dict_response_retries(self, mock_call: MagicMock) -> None:
        """Non-dict response (like a list) triggers retry."""
        expected = {"action": "brightness", "target": "all", "params": {"brightness": 50}}
        mock_call.side_effect = ["not a dict", expected]
        parser = self._make_parser()
        result = parser.parse("set brightness to 50")
        self.assertEqual(result, expected)


class TestIntentParserRefresh(unittest.TestCase):
    """Tests for capability refresh logic."""

    def test_should_refresh_initially(self) -> None:
        """Fresh parser should refresh (last refresh is 0)."""
        parser = IntentParser.__new__(IntentParser)
        parser._capabilities_last_refresh = 0.0
        self.assertTrue(parser.should_refresh())

    def test_should_not_refresh_recently(self) -> None:
        """Parser refreshed recently should not need refresh."""
        import time
        parser = IntentParser.__new__(IntentParser)
        parser._capabilities_last_refresh = time.time()
        self.assertFalse(parser.should_refresh())


if __name__ == "__main__":
    unittest.main()
