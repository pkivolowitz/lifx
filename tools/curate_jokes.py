"""Curate the taivop joke-dataset into a speech-friendly pool.

One-shot tool: reads the three raw JSON files (``reddit_jokes.json``,
``stupidstuff.json``, ``wocka.json``) from ``--src`` and writes a
single ``jokes.json`` to ``--dst``.  Output is the artefact loaded by
:mod:`voice.coordinator.joke_pool` at coordinator startup.

Filter pipeline (per source):

- **reddit**: requires ``score >= REDDIT_MIN_SCORE``.  The Reddit
  score is upvotes minus downvotes on r/jokes — a popularity signal
  that humans actually laughed at the joke.  Score-filtering also
  squelches lazy-crude content which doesn't accumulate score.
- **stupidstuff**: requires ``rating >= STUPIDSTUFF_MIN_RATING``
  (1-5 scale; mean rating is ~3.28, so 3.5 keeps the above-average half).
- **wocka**: no quality field — kept on length + blacklist alone.
- **all**: spoken-form length ``<= MAX_BODY_CHARS`` (voice-friendly).
- **all**: regex blacklist on title+body for slurs and explicit
  sexual-violence terms.  Score filtering does most of the work;
  the blacklist is a safety net for the residue.
- **all**: case-insensitive (title|body) hash dedup across sources.

Output schema is documented at :data:`SCHEMA_VERSION` and frozen in
the ``version`` field of the file; :mod:`joke_pool` refuses to load
any other version rather than guess at fields.

Run via the project venv on Conway / Bed::

    ~/venv/bin/python tools/curate_jokes.py \\
        --src ~/Downloads/joke-dataset \\
        --dst ~/models/jokes/jokes.json

The destination directory is created if missing.  This script writes
no other files and modifies no source data.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import argparse
import datetime
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants — filtering thresholds and blacklist
# ---------------------------------------------------------------------------

# Output schema version.  Bump when the joke record shape changes;
# joke_pool.py refuses to load any other version (intentional — silent
# schema drift between curator and reader has bitten us before).
SCHEMA_VERSION: str = "1"

# Reddit upvote-minus-downvote threshold.  Empirical distribution from
# the 195K-joke corpus: p50 = 3, p90 = 94, p99 = 2193.  Score >= 200
# keeps roughly the top 6% (~12K jokes) — broad approval, while still
# leaving the corpus large enough that random sampling stays varied.
# See Perry's discussion in the 2026-05-01 design conversation.
REDDIT_MIN_SCORE: int = 200

# StupidStuff jokes carry a 1-5 mean rating; corpus mean is ~3.28.
# 3.5 keeps the above-average half (~1.8K jokes) without being so
# strict that the source nearly empties.
STUPIDSTUFF_MIN_RATING: float = 3.5

# Maximum length of the spoken-form joke (title + body merged for
# reddit; body alone for the others).  300 chars at piper's default
# speaking rate is ~25 seconds — short enough that a forgetful
# household listener still has the setup in mind when the punchline
# lands.  Longer shaggy-dog jokes don't speak well.
MAX_BODY_CHARS: int = 300

# Blacklist patterns (compiled later, case-insensitive).  Word-boundary
# anchored so common substrings ("scunthorpe", "raccoon") don't
# false-positive.  This list is deliberately short — Reddit's score
# filter and r/jokes moderation already remove most of what we'd
# want gone; this is the defence-in-depth layer for the residue.
#
# The cost of an over-aggressive blacklist is silently dropping a
# small number of legitimate jokes from a 10K pool.  The cost of an
# under-aggressive blacklist is a slur landing in Perry's house at
# voice volume.  The asymmetry justifies erring strict.
_BLACKLIST_PATTERNS: tuple[str, ...] = (
    # Racial / ethnic / gender slurs.
    r"\bnigg(er|ers|a|as|ah|ahs)\b",
    r"\bfag(got|gots|gy|s)?\b",
    r"\bretard(s|ed)?\b",
    r"\bspick?s?\b",
    r"\bchinks?\b",
    r"\btrann(y|ies)\b",
    r"\bkikes?\b",
    r"\bgooks?\b",
    r"\bcoons?\b",
    r"\bbeaners?\b",
    # Sexual violence terms.
    r"\brap(e|ed|es|ist|ists|ing|ey)\b",
    r"\bmolest(s|ed|er|ers|ing|ation)?\b",
    r"\bpedo(phile|philes|s)?\b",
    r"\bgang\s*rap\w+",
    # Self-harm framing — voice telling these is bad UX even if the
    # joke is technically clean.
    r"\bsuicid(e|al|es)\b",
    r"\bkill\s+myself\b",
)
_BLACKLIST_RE: re.Pattern[str] = re.compile(
    "|".join(_BLACKLIST_PATTERNS), flags=re.IGNORECASE,
)

# Patterns that mark a record as not-really-a-joke or hard to speak.
# - URLs / image links — the joke is visual.
# - Markdown image embeds and reddit edit annotations.
# - Heavy markdown formatting (>3 list bullets) — speech-hostile.
_DROP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://\S+", re.IGNORECASE),
    re.compile(r"!\[", re.IGNORECASE),         # markdown image
    re.compile(r"\nedit\s*[:\-]", re.IGNORECASE),
    re.compile(r"^[\-*]\s.*\n[\-*]\s.*\n[\-*]\s", re.MULTILINE),
)

# Pre-compiled normalisation patterns.  Reddit bodies in particular
# carry CRLF line endings, leading/trailing whitespace, and the
# occasional ``&amp;`` from old HTML escaping; collapse them up front
# so length math and TTS see the same string.
_WHITESPACE_RUN: re.Pattern[str] = re.compile(r"\s+")
_HTML_AMP: re.Pattern[str] = re.compile(r"&amp;", re.IGNORECASE)
_HTML_LT: re.Pattern[str] = re.compile(r"&lt;", re.IGNORECASE)
_HTML_GT: re.Pattern[str] = re.compile(r"&gt;", re.IGNORECASE)
_HTML_QUOT: re.Pattern[str] = re.compile(r"&quot;", re.IGNORECASE)
_HTML_APOS: re.Pattern[str] = re.compile(r"&#39;", re.IGNORECASE)

# Source-key prefixes for ID disambiguation across the three datasets.
# Reddit IDs are short alphanumeric strings; wocka and stupidstuff use
# integers; prefixing with one letter avoids collisions on integer 1.
_SRC_PREFIX_REDDIT: str = "r"
_SRC_PREFIX_WOCKA: str = "w"
_SRC_PREFIX_STUPIDSTUFF: str = "s"

# File names within --src.  Hard-coded because the upstream repo's
# layout is stable and we want the script to fail loudly if any
# expected file is missing.
_REDDIT_FILE: str = "reddit_jokes.json"
_STUPIDSTUFF_FILE: str = "stupidstuff.json"
_WOCKA_FILE: str = "wocka.json"


logger: logging.Logger = logging.getLogger("curate_jokes")


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _normalize_text(s: str) -> str:
    """Decode HTML entities, collapse whitespace, strip ends.

    Used uniformly on titles and bodies before length checks and
    blacklist matching, so all sources speak with the same convention
    regardless of whatever junk the upstream JSON carried.
    """
    if not s:
        return ""
    s = _HTML_AMP.sub("&", s)
    s = _HTML_LT.sub("<", s)
    s = _HTML_GT.sub(">", s)
    s = _HTML_QUOT.sub('"', s)
    s = _HTML_APOS.sub("'", s)
    s = _WHITESPACE_RUN.sub(" ", s)
    return s.strip()


def _is_speech_clean(text: str) -> bool:
    """Return False if the text contains content unfit for voice utterance."""
    if _BLACKLIST_RE.search(text):
        return False
    for pat in _DROP_PATTERNS:
        if pat.search(text):
            return False
    return True


def _join_setup_punchline(title: str, body: str) -> str:
    """Merge a setup-style title with its body for voice utterance.

    Reddit posts use the title as the setup and the body as the
    follow-up; saying both gives the joke its rhythm.  Insert a
    sentence break only when the title doesn't already end in
    terminating punctuation.
    """
    title = title.strip()
    body = body.strip()
    if not title:
        return body
    if not body:
        return title
    if title[-1] in ".!?":
        return f"{title} {body}"
    return f"{title}. {body}"


def _record_hash(spoken: str) -> str:
    """Stable hash of the spoken text for cross-source dedup."""
    return hashlib.sha256(spoken.lower().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Per-source loaders
# ---------------------------------------------------------------------------


def _load_reddit(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Read reddit_jokes.json and return kept records + per-stage counts."""
    raw: list[dict[str, Any]] = json.loads(path.read_text())
    kept: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "in": len(raw), "below_score": 0, "blacklist": 0,
        "too_long": 0, "empty": 0, "kept": 0,
    }
    for rec in raw:
        score: int = int(rec.get("score", 0) or 0)
        if score < REDDIT_MIN_SCORE:
            counts["below_score"] += 1
            continue
        title: str = _normalize_text(rec.get("title", ""))
        body: str = _normalize_text(rec.get("body", ""))
        spoken: str = _join_setup_punchline(title, body)
        if not spoken:
            counts["empty"] += 1
            continue
        if not _is_speech_clean(spoken):
            counts["blacklist"] += 1
            continue
        if len(spoken) > MAX_BODY_CHARS:
            counts["too_long"] += 1
            continue
        rid: str = str(rec.get("id", ""))
        if not rid:
            counts["empty"] += 1
            continue
        kept.append({
            "id": f"{_SRC_PREFIX_REDDIT}:{rid}",
            "body": spoken,
            "source": "reddit",
            "score": float(score),
            "category": "",
        })
        counts["kept"] += 1
    return kept, counts


def _load_stupidstuff(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Read stupidstuff.json and return kept records + per-stage counts."""
    raw: list[dict[str, Any]] = json.loads(path.read_text())
    kept: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "in": len(raw), "below_rating": 0, "blacklist": 0,
        "too_long": 0, "empty": 0, "kept": 0,
    }
    for rec in raw:
        rating: float = float(rec.get("rating", 0.0) or 0.0)
        if rating < STUPIDSTUFF_MIN_RATING:
            counts["below_rating"] += 1
            continue
        body: str = _normalize_text(rec.get("body", ""))
        if not body:
            counts["empty"] += 1
            continue
        if not _is_speech_clean(body):
            counts["blacklist"] += 1
            continue
        if len(body) > MAX_BODY_CHARS:
            counts["too_long"] += 1
            continue
        kept.append({
            "id": f"{_SRC_PREFIX_STUPIDSTUFF}:{rec.get('id', '')}",
            "body": body,
            "source": "stupidstuff",
            "score": rating,
            "category": str(rec.get("category", "")).strip(),
        })
        counts["kept"] += 1
    return kept, counts


def _load_wocka(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Read wocka.json and return kept records + per-stage counts.

    Wocka entries have a ``title`` (a label, not a setup) and a
    ``body`` (the full joke).  Saying the title would just add noise,
    so the spoken form is body-only.  Categories are kept for the
    optional topic-substring filter at sample time.
    """
    raw: list[dict[str, Any]] = json.loads(path.read_text())
    kept: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "in": len(raw), "blacklist": 0, "too_long": 0,
        "empty": 0, "kept": 0,
    }
    for rec in raw:
        body: str = _normalize_text(rec.get("body", ""))
        if not body:
            counts["empty"] += 1
            continue
        if not _is_speech_clean(body):
            counts["blacklist"] += 1
            continue
        if len(body) > MAX_BODY_CHARS:
            counts["too_long"] += 1
            continue
        kept.append({
            "id": f"{_SRC_PREFIX_WOCKA}:{rec.get('id', '')}",
            "body": body,
            "source": "wocka",
            "score": 0.0,
            "category": str(rec.get("category", "")).strip(),
        })
        counts["kept"] += 1
    return kept, counts


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _dedup(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Drop duplicate jokes by lower-cased spoken-text hash; keep first.

    Reddit reposts and cross-source overlap are common (a joke from
    Wocka often appears on r/jokes years later).  First-seen wins;
    callers are expected to feed sources in their preferred order
    (we feed reddit→wocka→stupidstuff so the highest-quality-signal
    copy survives).
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    dropped: int = 0
    for rec in records:
        h: str = _record_hash(rec["body"])
        if h in seen:
            dropped += 1
            continue
        seen.add(h)
        out.append(rec)
    return out, dropped


def _file_sha256(path: Path) -> str:
    """Return the SHA256 of *path* as a hex string."""
    h: hashlib._Hash = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_output(
    src: Path,
    reddit_kept: list[dict[str, Any]], reddit_counts: dict[str, int],
    wocka_kept: list[dict[str, Any]], wocka_counts: dict[str, int],
    stupid_kept: list[dict[str, Any]], stupid_counts: dict[str, int],
    deduped: list[dict[str, Any]], dedup_dropped: int,
) -> dict[str, Any]:
    """Assemble the on-disk JSON document with provenance and counts."""
    return {
        "version": SCHEMA_VERSION,
        "generated_utc": datetime.datetime.now(
            tz=datetime.timezone.utc,
        ).isoformat(timespec="seconds"),
        "filters": {
            "reddit_min_score": REDDIT_MIN_SCORE,
            "stupidstuff_min_rating": STUPIDSTUFF_MIN_RATING,
            "max_body_chars": MAX_BODY_CHARS,
            "blacklist_patterns": list(_BLACKLIST_PATTERNS),
        },
        "sources": {
            _REDDIT_FILE: {
                "sha256": _file_sha256(src / _REDDIT_FILE),
                **reddit_counts,
            },
            _WOCKA_FILE: {
                "sha256": _file_sha256(src / _WOCKA_FILE),
                **wocka_counts,
            },
            _STUPIDSTUFF_FILE: {
                "sha256": _file_sha256(src / _STUPIDSTUFF_FILE),
                **stupid_counts,
            },
        },
        "dedup_dropped": dedup_dropped,
        "total_jokes": len(deduped),
        "jokes": deduped,
    }


def _atomic_write_json(path: Path, doc: dict[str, Any]) -> None:
    """Write *doc* to *path* via a temp file + rename for atomicity.

    A coordinator picking up the file mid-write would otherwise see a
    truncated JSON document and refuse to load — atomic rename keeps
    the prior pool live until the new one is fully on disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    p: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Curate the taivop joke-dataset for the voice coordinator.",
    )
    p.add_argument(
        "--src", required=True,
        help="Path to the cloned joke-dataset directory containing the three raw JSONs.",
    )
    p.add_argument(
        "--dst", required=True,
        help="Output path for the curated jokes.json (e.g. ~/models/jokes/jokes.json).",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log per-stage counts and a small sample of kept jokes.",
    )
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    """Run the curation pipeline; return a process exit code."""
    args: argparse.Namespace = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    src: Path = Path(args.src).expanduser().resolve()
    dst: Path = Path(args.dst).expanduser().resolve()

    if not src.is_dir():
        logger.error("--src is not a directory: %s", src)
        return 2
    for fname in (_REDDIT_FILE, _STUPIDSTUFF_FILE, _WOCKA_FILE):
        if not (src / fname).is_file():
            logger.error("missing source file: %s", src / fname)
            return 2

    logger.info("loading reddit_jokes.json …")
    reddit_kept, reddit_counts = _load_reddit(src / _REDDIT_FILE)
    logger.info(
        "reddit: in=%d kept=%d below_score=%d blacklist=%d too_long=%d empty=%d",
        reddit_counts["in"], reddit_counts["kept"],
        reddit_counts["below_score"], reddit_counts["blacklist"],
        reddit_counts["too_long"], reddit_counts["empty"],
    )

    logger.info("loading wocka.json …")
    wocka_kept, wocka_counts = _load_wocka(src / _WOCKA_FILE)
    logger.info(
        "wocka: in=%d kept=%d blacklist=%d too_long=%d empty=%d",
        wocka_counts["in"], wocka_counts["kept"],
        wocka_counts["blacklist"], wocka_counts["too_long"],
        wocka_counts["empty"],
    )

    logger.info("loading stupidstuff.json …")
    stupid_kept, stupid_counts = _load_stupidstuff(src / _STUPIDSTUFF_FILE)
    logger.info(
        "stupidstuff: in=%d kept=%d below_rating=%d blacklist=%d too_long=%d empty=%d",
        stupid_counts["in"], stupid_counts["kept"],
        stupid_counts["below_rating"], stupid_counts["blacklist"],
        stupid_counts["too_long"], stupid_counts["empty"],
    )

    # Order matters for dedup: reddit first so the score-bearing copy
    # survives when the same joke recurs across sources.
    combined: list[dict[str, Any]] = reddit_kept + wocka_kept + stupid_kept
    deduped, dedup_dropped = _dedup(combined)
    logger.info(
        "dedup: combined=%d unique=%d dropped=%d",
        len(combined), len(deduped), dedup_dropped,
    )

    doc: dict[str, Any] = _build_output(
        src=src,
        reddit_kept=reddit_kept, reddit_counts=reddit_counts,
        wocka_kept=wocka_kept, wocka_counts=wocka_counts,
        stupid_kept=stupid_kept, stupid_counts=stupid_counts,
        deduped=deduped, dedup_dropped=dedup_dropped,
    )
    _atomic_write_json(dst, doc)
    logger.info(
        "wrote %d jokes to %s (schema v%s)",
        len(deduped), dst, SCHEMA_VERSION,
    )

    if args.verbose:
        for sample in deduped[:3]:
            logger.debug(
                "sample [%s] (%s): %s",
                sample["id"], sample["source"], sample["body"][:160],
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
