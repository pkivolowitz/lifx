"""In-memory joke pool for the voice coordinator.

Loaded once at coordinator startup from the file written by
``tools/curate_jokes.py``.  Provides random sampling with optional
style/topic substring filtering and explicit ID exclusion (so the
caller can avoid repeating recently-spoken jokes within a session).

Why a single in-RAM JSON file rather than a database:

- Curated pool is ~12K jokes ≈ 3 MB on disk; loaded into Python
  objects this is well under 10 MB — invisible on Daedalus or the
  hub Pi.
- Voice "tell me a joke" requests are infrequent (a few per day) and
  the read pattern is single-process: there is no second consumer
  that would benefit from a centralised store.
- The corpus is read-only between curation runs.  Postgres'
  transactional guarantees would buy nothing while introducing a
  network dependency on the path that previously had none.
- See the 2026-05-01 design discussion for the full pros/cons.

Schema discipline: this module refuses to load any ``version`` other
than :data:`SCHEMA_VERSION`.  Curator and reader marching in lockstep
matters more than backwards-compatibility helpers — silent schema
drift between the two has bitten the project before.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The curated-file schema version this reader supports.  Bump in
# lockstep with ``tools/curate_jokes.py`` if the joke record shape
# changes; no fallback path on mismatch — load fails loudly.
SCHEMA_VERSION: str = "1"

# Maximum sample-retry attempts when ``exclude_ids`` rejects the
# random pick.  16 is plenty even when the recent-jokes set overlaps
# heavily with a small filtered subset; if every retry collides the
# code falls through to a deterministic linear scan rather than
# spinning forever.
_MAX_SAMPLE_ATTEMPTS: int = 16


@dataclass(frozen=True)
class Joke:
    """One curated joke ready to be spoken.

    Attributes are populated by :meth:`JokePool.from_file` from the
    on-disk JSON record; callers should treat instances as immutable
    value objects.
    """

    id: str           # globally unique — source-prefixed (e.g., "r:5tz52q")
    body: str         # spoken-form joke text
    source: str       # "reddit" | "wocka" | "stupidstuff"
    score: float      # source-specific quality signal (0.0 if absent)
    category: str     # source-specific topic label (may be empty)


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


class JokePool:
    """Speech-friendly joke pool with topic/style substring filtering.

    Construct via :meth:`from_file` for normal use, or directly with
    a list of :class:`Joke` for tests.  All operations are O(N) over
    the pool — at 12K jokes this is microseconds, so no index is
    maintained for the substring filter (the simpler code wins).
    """

    def __init__(self, jokes: list[Joke]) -> None:
        """Initialise from a pre-built list of :class:`Joke` records."""
        self._jokes: list[Joke] = list(jokes)

    # -- Construction helpers -------------------------------------------

    @classmethod
    def empty(cls) -> "JokePool":
        """Return an empty pool — what the coordinator falls back to.

        Used when the on-disk file is missing or unreadable: the
        coordinator stays up but ``_handle_joke`` reports "no jokes
        available" instead of crashing.
        """
        return cls([])

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "JokePool":
        """Load a curated pool from *path*.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError:        If the file's schema version does not
                               match :data:`SCHEMA_VERSION`, or the
                               document is structurally malformed.
        """
        p: Path = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"joke pool file not found: {p}")
        doc: dict = json.loads(p.read_text())
        version: str = str(doc.get("version", ""))
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"joke pool schema mismatch at {p}: "
                f"file version={version!r}, reader expects "
                f"{SCHEMA_VERSION!r} — re-run tools/curate_jokes.py",
            )
        raw_records: list[dict] = doc.get("jokes", [])
        if not isinstance(raw_records, list):
            raise ValueError(
                f"joke pool {p}: 'jokes' field is not a list",
            )
        jokes: list[Joke] = []
        for rec in raw_records:
            try:
                jokes.append(Joke(
                    id=str(rec["id"]),
                    body=str(rec["body"]),
                    source=str(rec.get("source", "")),
                    score=float(rec.get("score", 0.0) or 0.0),
                    category=str(rec.get("category", "") or ""),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                # Skip malformed records but keep loading — one bad
                # row shouldn't kill the entire pool.  Logged at
                # debug level because the curator is the SoT and
                # malformed rows there warrant fixing the script.
                logger.debug("dropped malformed joke record: %s (%s)", rec, exc)
        logger.info("loaded %d jokes from %s", len(jokes), p)
        return cls(jokes)

    # -- Read-side API --------------------------------------------------

    def __len__(self) -> int:
        """Total joke count."""
        return len(self._jokes)

    def is_empty(self) -> bool:
        """True if the pool has no jokes (e.g., load fell back to empty())."""
        return not self._jokes

    def sample(
        self,
        topic: str = "",
        style: str = "",
        exclude_ids: Optional[set[str]] = None,
    ) -> Optional[Joke]:
        """Return one joke matching the filters, or None.

        Args:
            topic:       Optional case-insensitive substring required
                         in the joke's body or category.  Empty = no
                         topic filter.
            style:       Optional case-insensitive substring required
                         in the joke's body.  Empty = no style filter.
                         (Style and topic match against overlapping
                         fields; both are kept because the intent
                         layer fills them independently.)
            exclude_ids: Set of joke IDs to refuse — used by the
                         caller to avoid recent repeats.

        Returns:
            A :class:`Joke`, or ``None`` if no joke survives the
            filters and exclusion set.  Caller is expected to retry
            without filters before reporting "no joke" to the user.
        """
        excluded: set[str] = exclude_ids or set()
        topic_lower: str = topic.strip().lower()
        style_lower: str = style.strip().lower()

        # No filters AND no exclusion overlap → fast path: random pick.
        if not (topic_lower or style_lower) and not excluded:
            if not self._jokes:
                return None
            return random.choice(self._jokes)

        # No filters but exclusion present → bounded retry on the full
        # pool; falls back to linear scan if retries exhaust.
        if not (topic_lower or style_lower):
            return self._sample_excluding(self._jokes, excluded)

        # Filters present — build the candidate list once, then sample.
        candidates: list[Joke] = [
            j for j in self._jokes
            if self._matches(j, topic_lower, style_lower)
        ]
        return self._sample_excluding(candidates, excluded)

    @staticmethod
    def _matches(joke: Joke, topic_lower: str, style_lower: str) -> bool:
        """True if *joke* satisfies the (already-lowercased) substring filters.

        Topic is matched against body OR category (so "weather" finds
        wocka jokes filed under category="Weather" without their body
        also containing the literal word).  Style is matched against
        body only — it's a delivery-shape hint, not a topic.
        """
        if topic_lower:
            haystack: str = (joke.body + " " + joke.category).lower()
            if topic_lower not in haystack:
                return False
        if style_lower:
            if style_lower not in joke.body.lower():
                return False
        return True

    def _sample_excluding(
        self,
        candidates: list[Joke],
        excluded: set[str],
    ) -> Optional[Joke]:
        """Pick a random joke from *candidates* whose id is not in *excluded*.

        Uses bounded random retries first (cheap when overlap is
        small) and falls through to a linear scan only if retries
        keep colliding (cheap when overlap is large).  Returns None
        only when every candidate is excluded.
        """
        if not candidates:
            return None
        for _ in range(_MAX_SAMPLE_ATTEMPTS):
            pick: Joke = random.choice(candidates)
            if pick.id not in excluded:
                return pick
        # Retries all collided — scan the survivors deterministically.
        survivors: list[Joke] = [j for j in candidates if j.id not in excluded]
        if not survivors:
            return None
        return random.choice(survivors)
