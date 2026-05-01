"""Tests for ``voice.coordinator.joke_pool``.

Covers:

- Empty / not-found / schema-mismatch handling at load time.
- Happy-path file load builds the right number of :class:`Joke` records.
- ``sample`` with no filters returns a member of the pool.
- ``sample`` with topic substring filter restricts to category-or-body matches.
- ``sample`` with style substring filter restricts to body matches.
- ``sample`` with ``exclude_ids`` honours the recency ring.
- ``sample`` returns None when filters AND exclusion empty the pool,
  and when the pool is empty to begin with.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import os
import tempfile
import unittest
from pathlib import Path

from voice.coordinator.joke_pool import (
    SCHEMA_VERSION, Joke, JokePool,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

# Hand-built minimal pool — five jokes spanning all three sources and
# covering enough body/category variety to exercise the substring
# filters distinctly.
_TEST_JOKES: list[Joke] = [
    Joke(id="r:001", body="Why did the chicken cross the road? To get to the other side.",
         source="reddit", score=500.0, category=""),
    Joke(id="r:002", body="What's brown and sticky? A stick.",
         source="reddit", score=320.0, category=""),
    Joke(id="w:001", body="What do you call a cow with no legs? Ground beef.",
         source="wocka", score=0.0, category="Animal"),
    Joke(id="w:002", body="Time flies like an arrow. Fruit flies like a banana.",
         source="wocka", score=0.0, category="Wordplay"),
    Joke(id="s:001", body="I told my wife she should embrace her mistakes. She gave me a hug.",
         source="stupidstuff", score=4.1, category="Marriage"),
]


def _write_pool_file(path: Path, jokes: list[Joke], version: str = SCHEMA_VERSION) -> None:
    """Write a curated-pool JSON file at *path* with the given records."""
    doc: dict = {
        "version": version,
        "generated_utc": "2026-05-01T15:00:00+00:00",
        "filters": {},
        "sources": {},
        "dedup_dropped": 0,
        "total_jokes": len(jokes),
        "jokes": [
            {"id": j.id, "body": j.body, "source": j.source,
             "score": j.score, "category": j.category}
            for j in jokes
        ],
    }
    path.write_text(json.dumps(doc))


class _TempPoolCase(unittest.TestCase):
    """Common setup — temp dir for pool files."""

    def setUp(self) -> None:
        self._tmpdir: tempfile.TemporaryDirectory = tempfile.TemporaryDirectory()
        self.path: Path = Path(self._tmpdir.name) / "jokes.json"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()


# ---------------------------------------------------------------------------
# Load / construction
# ---------------------------------------------------------------------------


class TestLoad(_TempPoolCase):
    """Loading from a curated file — happy path and the failure modes."""

    def test_load_happy_path(self) -> None:
        """A well-formed file at SCHEMA_VERSION yields the expected joke count."""
        _write_pool_file(self.path, _TEST_JOKES)
        pool: JokePool = JokePool.from_file(self.path)
        self.assertEqual(len(pool), len(_TEST_JOKES))
        self.assertFalse(pool.is_empty())

    def test_load_missing_file_raises(self) -> None:
        """Nonexistent path → FileNotFoundError (executor catches this)."""
        missing: Path = Path(self._tmpdir.name) / "does_not_exist.json"
        with self.assertRaises(FileNotFoundError):
            JokePool.from_file(missing)

    def test_load_schema_mismatch_raises(self) -> None:
        """Wrong ``version`` → ValueError (no silent fallthrough to old schema)."""
        _write_pool_file(self.path, _TEST_JOKES, version="999")
        with self.assertRaises(ValueError):
            JokePool.from_file(self.path)

    def test_load_jokes_field_not_list_raises(self) -> None:
        """Structural malformation in the document body → ValueError."""
        self.path.write_text(json.dumps({
            "version": SCHEMA_VERSION,
            "jokes": "not a list",
        }))
        with self.assertRaises(ValueError):
            JokePool.from_file(self.path)

    def test_malformed_record_skipped_pool_keeps_loading(self) -> None:
        """One bad record does not abort the entire load."""
        # Mix one valid and one malformed (missing ``body``) record.
        doc: dict = {
            "version": SCHEMA_VERSION,
            "jokes": [
                {"id": "r:001", "body": "ok", "source": "reddit",
                 "score": 100.0, "category": ""},
                {"id": "r:002", "source": "reddit"},  # no body
                {"id": "r:003", "body": "also ok", "source": "reddit",
                 "score": 50.0, "category": ""},
            ],
        }
        self.path.write_text(json.dumps(doc))
        pool: JokePool = JokePool.from_file(self.path)
        self.assertEqual(len(pool), 2)


# ---------------------------------------------------------------------------
# Empty / sentinel
# ---------------------------------------------------------------------------


class TestEmpty(unittest.TestCase):
    """``JokePool.empty()`` is the no-jokes-available sentinel."""

    def test_empty_is_empty(self) -> None:
        pool: JokePool = JokePool.empty()
        self.assertTrue(pool.is_empty())
        self.assertEqual(len(pool), 0)
        self.assertIsNone(pool.sample())


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


class TestSampleNoFilters(unittest.TestCase):
    """No filters / no exclusions → uniformly random pick."""

    def test_returns_member_of_pool(self) -> None:
        pool: JokePool = JokePool(_TEST_JOKES)
        joke = pool.sample()
        self.assertIsNotNone(joke)
        self.assertIn(joke, _TEST_JOKES)


class TestSampleTopicFilter(unittest.TestCase):
    """Topic matches against body OR category, case-insensitive."""

    def test_topic_matches_category(self) -> None:
        """Topic 'animal' should land on the wocka 'Animal'-category joke."""
        pool: JokePool = JokePool(_TEST_JOKES)
        # Run multiple times to be sure the filter, not luck, is at work.
        for _ in range(10):
            joke = pool.sample(topic="animal")
            self.assertIsNotNone(joke)
            self.assertEqual(joke.id, "w:001")

    def test_topic_matches_body(self) -> None:
        """Topic 'chicken' lands on the chicken joke (body match)."""
        pool: JokePool = JokePool(_TEST_JOKES)
        for _ in range(10):
            joke = pool.sample(topic="chicken")
            self.assertIsNotNone(joke)
            self.assertEqual(joke.id, "r:001")

    def test_topic_no_match_returns_none(self) -> None:
        """Topic with no matching joke → None (caller falls back unfiltered)."""
        pool: JokePool = JokePool(_TEST_JOKES)
        self.assertIsNone(pool.sample(topic="quantum-thermodynamics"))


class TestSampleStyleFilter(unittest.TestCase):
    """Style matches against body, case-insensitive."""

    def test_style_substring_in_body(self) -> None:
        pool: JokePool = JokePool(_TEST_JOKES)
        for _ in range(10):
            joke = pool.sample(style="time flies")
            self.assertIsNotNone(joke)
            self.assertEqual(joke.id, "w:002")


class TestSampleExcludeIds(unittest.TestCase):
    """``exclude_ids`` honours the recency ring."""

    def test_excluded_id_never_chosen(self) -> None:
        pool: JokePool = JokePool(_TEST_JOKES)
        excluded: set[str] = {j.id for j in _TEST_JOKES if j.id != "s:001"}
        # Only one survivor — every sample must be it.
        for _ in range(20):
            joke = pool.sample(exclude_ids=excluded)
            self.assertIsNotNone(joke)
            self.assertEqual(joke.id, "s:001")

    def test_all_excluded_returns_none(self) -> None:
        pool: JokePool = JokePool(_TEST_JOKES)
        excluded: set[str] = {j.id for j in _TEST_JOKES}
        self.assertIsNone(pool.sample(exclude_ids=excluded))

    def test_filter_plus_exclude(self) -> None:
        """Topic filter PLUS exclusion of the only match → None."""
        pool: JokePool = JokePool(_TEST_JOKES)
        # Topic 'cow' uniquely matches w:001; excluding it must yield None.
        self.assertIsNone(
            pool.sample(topic="cow", exclude_ids={"w:001"}),
        )


if __name__ == "__main__":
    unittest.main()
