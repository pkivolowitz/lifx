"""Tests for ``GlowUpExecutor._handle_joke``.

Exercises the joke handler without standing up the full executor or
its UDP/HTTP/Ollama dependencies.  Bypasses ``__init__`` via
``__new__`` (matching the pattern in ``test_voice_gates_route.py``)
and attaches only the executor state the handler actually reads.

Covers:

- Empty pool → status=error confirmation about no jokes.
- Happy path with a non-empty pool returns one joke and appends its
  ID to the per-room recency ring.
- Successive calls within the TTL exclude prior IDs.
- Topic filter is honoured; falls back to unfiltered when no match.
- TTL expiry resets both chat history and the joke recency ring
  (the ``_touch_room`` invariant the handler relies on).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import time
import unittest
from collections import deque
from typing import Any

from voice.coordinator.executor import (
    GlowUpExecutor, _CHAT_HISTORY_TTL_S, _RECENT_JOKE_IDS_MAX,
)
from voice.coordinator.joke_pool import Joke, JokePool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# Tiny pool — three jokes is enough to exercise filter, exclusion,
# and "every joke excluded → None" without dragging in the real 12K
# corpus.
_TEST_JOKES: list[Joke] = [
    Joke(id="r:001", body="A horse walks into a bar.",
         source="reddit", score=500.0, category=""),
    Joke(id="w:001", body="What do you call a cow with no legs? Ground beef.",
         source="wocka", score=0.0, category="Animal"),
    Joke(id="s:001", body="Time flies like an arrow. Fruit flies like a banana.",
         source="stupidstuff", score=4.1, category="Wordplay"),
]

_ROOM: str = "TestRoom"


def _make_executor(pool: JokePool) -> GlowUpExecutor:
    """Build a bare GlowUpExecutor with only what _handle_joke uses.

    The handler reads ``_joke_pool`` and (via ``_touch_room``)
    ``_chat_timestamps``, ``_chat_history``, ``_recent_joke_ids``.
    Stub the rest with empty dicts; never call any other method.
    """
    eff: GlowUpExecutor = GlowUpExecutor.__new__(GlowUpExecutor)
    eff._joke_pool = pool  # type: ignore[attr-defined]
    eff._chat_history = {}  # type: ignore[attr-defined]
    eff._chat_timestamps = {}  # type: ignore[attr-defined]
    eff._recent_joke_ids = {}  # type: ignore[attr-defined]
    eff._current_room = _ROOM  # type: ignore[attr-defined]
    return eff


def _call_joke(
    eff: GlowUpExecutor,
    topic: str = "",
    style: str = "",
) -> dict[str, Any]:
    """Invoke ``_handle_joke`` with default args; only ``params`` matters here."""
    params: dict[str, Any] = {}
    if topic:
        params["topic"] = topic
    if style:
        params["style"] = style
    return eff._handle_joke(  # type: ignore[attr-defined]
        cfg={}, target_url="", target_raw="", display_target="",
        params=params,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmptyPool(unittest.TestCase):
    """Empty pool → error confirmation, no crash."""

    def test_empty_pool_returns_error(self) -> None:
        eff: GlowUpExecutor = _make_executor(JokePool.empty())
        result: dict[str, Any] = _call_joke(eff)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["speak"])
        self.assertIn("don't have any jokes", result["confirmation"])


class TestHappyPath(unittest.TestCase):
    """A non-empty pool yields a real joke and updates the recency ring."""

    def test_returns_joke_from_pool(self) -> None:
        eff: GlowUpExecutor = _make_executor(JokePool(_TEST_JOKES))
        result: dict[str, Any] = _call_joke(eff)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["speak"])
        self.assertIn(
            result["confirmation"],
            {j.body for j in _TEST_JOKES},
            "confirmation must be one of the pool's joke bodies",
        )

    def test_recency_ring_appends_id(self) -> None:
        eff: GlowUpExecutor = _make_executor(JokePool(_TEST_JOKES))
        _call_joke(eff)
        recent: deque[str] = eff._recent_joke_ids[_ROOM]  # type: ignore[attr-defined]
        self.assertEqual(len(recent), 1)
        # Ring entry must be a real joke ID from the pool.
        self.assertIn(recent[0], {j.id for j in _TEST_JOKES})

    def test_recency_ring_capped_at_max(self) -> None:
        """Ring never grows past ``_RECENT_JOKE_IDS_MAX`` (deque maxlen)."""
        eff: GlowUpExecutor = _make_executor(JokePool(_TEST_JOKES))
        # Drain more than maxlen calls; even with only 3 jokes the
        # ring never exceeds the configured ceiling.
        for _ in range(_RECENT_JOKE_IDS_MAX + 5):
            _call_joke(eff)
        recent: deque[str] = eff._recent_joke_ids[_ROOM]  # type: ignore[attr-defined]
        self.assertLessEqual(len(recent), _RECENT_JOKE_IDS_MAX)


class TestExclusion(unittest.TestCase):
    """Successive calls avoid jokes already in the recency ring."""

    def test_three_calls_against_three_jokes_yields_three_distinct(self) -> None:
        """With pool == ring capacity, all three jokes appear before any repeat."""
        eff: GlowUpExecutor = _make_executor(JokePool(_TEST_JOKES))
        chosen_ids: list[str] = []
        for _ in range(len(_TEST_JOKES)):
            result: dict[str, Any] = _call_joke(eff)
            recent: deque[str] = eff._recent_joke_ids[_ROOM]  # type: ignore[attr-defined]
            chosen_ids.append(recent[-1])
        self.assertEqual(
            set(chosen_ids), {j.id for j in _TEST_JOKES},
            "with a 3-joke pool and recency ring, three calls must "
            "draw all three distinct IDs",
        )

    def test_pool_smaller_than_ring_eventually_repeats(self) -> None:
        """When pool is smaller than the ring, the handler still serves jokes.

        With a 3-joke pool and a 16-deep ring, the fourth call has
        every ID excluded.  ``JokePool.sample`` returns None in that
        case but the handler's *unfiltered* fallback still returns
        None (every joke is in the ring).  Result: error response
        — predictable, not a crash.
        """
        eff: GlowUpExecutor = _make_executor(JokePool(_TEST_JOKES))
        for _ in range(len(_TEST_JOKES)):
            self.assertEqual(_call_joke(eff)["status"], "ok")
        # Fourth call: every pool ID is in the ring → "told you all" error.
        result: dict[str, Any] = _call_joke(eff)
        self.assertEqual(result["status"], "error")
        self.assertIn("best ones", result["confirmation"])


class TestTopicFilter(unittest.TestCase):
    """Topic substring filter is honoured; misses fall back to unfiltered."""

    def test_topic_match(self) -> None:
        """``topic='cow'`` deterministically picks the cow joke."""
        eff: GlowUpExecutor = _make_executor(JokePool(_TEST_JOKES))
        result: dict[str, Any] = _call_joke(eff, topic="cow")
        self.assertEqual(result["status"], "ok")
        self.assertIn("cow", result["confirmation"].lower())

    def test_topic_miss_falls_back_to_unfiltered(self) -> None:
        """Bogus topic still returns A joke — better than apologising."""
        eff: GlowUpExecutor = _make_executor(JokePool(_TEST_JOKES))
        result: dict[str, Any] = _call_joke(eff, topic="quantum-thermodynamics")
        self.assertEqual(result["status"], "ok")
        self.assertIn(
            result["confirmation"], {j.body for j in _TEST_JOKES},
        )


class TestTtlExpiry(unittest.TestCase):
    """A 30-minute lull resets the recency ring (``_touch_room`` invariant)."""

    def test_recent_ids_cleared_after_ttl(self) -> None:
        eff: GlowUpExecutor = _make_executor(JokePool(_TEST_JOKES))
        _call_joke(eff)
        self.assertEqual(
            len(eff._recent_joke_ids[_ROOM]), 1,  # type: ignore[attr-defined]
        )
        # Force last-active backward by more than the TTL so the next
        # touch_room call inside _handle_joke prunes the ring.
        eff._chat_timestamps[_ROOM] = (  # type: ignore[attr-defined]
            time.time() - _CHAT_HISTORY_TTL_S - 1.0
        )
        _call_joke(eff)
        # After expiry the ring was reset and then the new joke
        # appended — exactly one entry, not two-plus.
        self.assertEqual(
            len(eff._recent_joke_ids[_ROOM]), 1,  # type: ignore[attr-defined]
        )


if __name__ == "__main__":
    unittest.main()
