"""Fixture geometry database — loads JSON descriptors from
``data/fixtures/`` and exposes per-fixture knowledge that the LIFX
LAN protocol does not carry: dead cells in matrix grids, special-
purpose cells (uplight rings, status LEDs), rim-shifted cells, and
virtual sub-component definitions.

The protocol gives us numeric `pid`, `matrix_width`, and
`matrix_height`.  That is enough to disambiguate one physical fixture
from another even when LIFX re-uses a pid across product variants
(pid 176 covers both the 11" round Ceiling and the 15" SuperColor
round Ceiling — same protocol, different geometry).  Lookup key is
``(vendor, pid, width, height)``.

Used by:

- :class:`transport.LifxDevice.query_all` to populate ``mask_cells``
  so matrix effects do not unintentionally write to dead cells or
  separately-addressable sub-components like uplight rings.
- :func:`infrastructure.discover.main` to emit additional rows for
  virtual sub-devices in the discovery table.
- ``glowup.py`` CLI to resolve ``--component <id>`` selectors.

Adding a new fixture: drop a JSON file in ``data/fixtures/`` with
the schema documented in ``data/fixtures/lifx_ceiling_15_supercolor.json``.
The cache reloads at process start; no code change required.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

__version__: str = "1.0"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fixture descriptor directory, relative to repo root.  Resolved at
# import time so a relocated repo still works without env-var fiddling.
FIXTURE_DIR: Path = (
    Path(__file__).resolve().parent.parent / "data" / "fixtures"
)

# JSON schema constants — match the field names used in fixture files.
KEY_PROTOCOL: str = "protocol"
KEY_MATRIX: str = "matrix"
KEY_VENDOR: str = "vendor"
KEY_PID: str = "pid"
KEY_WIDTH: str = "width"
KEY_HEIGHT: str = "height"
KEY_DEAD_CELLS: str = "dead_cells"
KEY_UPLIGHT_CELLS: str = "uplight_cells"
KEY_RIM_SHIFTED: str = "rim_shifted_cells"

# Component-kind strings returned by :func:`get_components`.  Effect-side
# code dispatches on these when constructing virtual sub-device emitters.
COMPONENT_KIND_SINGLE_COLOR: str = "single_color"
COMPONENT_KIND_MATRIX: str = "matrix"
COMPONENT_KIND_MULTIZONE: str = "multizone"

logger: logging.Logger = logging.getLogger("glowup.fixture_db")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

# In-memory cache: (vendor, pid, w, h) -> raw fixture dict from disk.
# Populated on first lookup; subsequent lookups are O(1).
_cache: dict[tuple[int, int, int, int], dict[str, Any]] = {}
_cache_loaded: bool = False


def _load_all() -> None:
    """Populate the in-memory cache from ``FIXTURE_DIR``.

    Idempotent — first call scans the directory and parses every
    ``*.json`` file; later calls are no-ops.  Files that fail to parse
    or lack the required protocol/matrix keys are skipped with a debug
    log; we never raise here because a single bad fixture file should
    not crash device discovery on otherwise-healthy hardware.
    """
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    if not FIXTURE_DIR.is_dir():
        logger.debug("fixture dir %s does not exist", FIXTURE_DIR)
        return
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        try:
            data: dict[str, Any] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skipping fixture %s: %s", path.name, exc)
            continue
        proto: dict[str, Any] = data.get(KEY_PROTOCOL, {})
        matrix: dict[str, Any] = data.get(KEY_MATRIX, {})
        try:
            key: tuple[int, int, int, int] = (
                int(proto[KEY_VENDOR]),
                int(proto[KEY_PID]),
                int(matrix[KEY_WIDTH]),
                int(matrix[KEY_HEIGHT]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "skipping fixture %s: missing protocol/matrix keys (%s)",
                path.name, exc,
            )
            continue
        _cache[key] = data
        logger.debug("loaded fixture %s key=%s", path.name, key)


def lookup(
    vendor: Optional[int],
    pid: Optional[int],
    width: Optional[int],
    height: Optional[int],
) -> Optional[dict[str, Any]]:
    """Return the raw fixture dict matching (vendor, pid, w, h), or None.

    All four args may be ``None`` (e.g., before ``query_version`` /
    ``query_device_chain`` have run).  In that case we return ``None``
    immediately rather than risk a false match against fixtures whose
    keys happen to contain zeros.
    """
    if vendor is None or pid is None or width is None or height is None:
        return None
    _load_all()
    return _cache.get((int(vendor), int(pid), int(width), int(height)))


def _cells_to_flat_indices(
    cells: list[list[int]],
    width: int,
) -> set[int]:
    """Convert ``[[row, col], ...]`` to flat ``{row * width + col, ...}``.

    Silently drops any entry that is not a 2-element [row, col] pair —
    a malformed fixture should not crash the engine.
    """
    out: set[int] = set()
    for entry in cells:
        if isinstance(entry, list) and len(entry) == 2:
            try:
                r: int = int(entry[0])
                c: int = int(entry[1])
            except (TypeError, ValueError):
                continue
            out.add(r * width + c)
    return out


def get_mask_cells(
    vendor: Optional[int],
    pid: Optional[int],
    width: Optional[int],
    height: Optional[int],
) -> set[int]:
    """Return the set of flat cell indices a matrix emitter must mask.

    The mask is the union of dead cells (positions with no LED) and
    uplight cells (positions that drive a separately-addressable
    component sharing the matrix protocol).  Both classes need to be
    forced to black on every frame so matrix effects neither waste
    bytes on phantom positions nor unintentionally light up the
    uplight ring.

    Returns an empty set if the fixture is unknown — fail-soft so an
    unrecognized fixture still works as a plain matrix.
    """
    fx = lookup(vendor, pid, width, height)
    if fx is None or width is None:
        return set()
    return (
        _cells_to_flat_indices(fx.get(KEY_DEAD_CELLS, []), int(width))
        | _cells_to_flat_indices(fx.get(KEY_UPLIGHT_CELLS, []), int(width))
    )


def lookup_by_pid(
    vendor: Optional[int],
    pid: Optional[int],
) -> Optional[dict[str, Any]]:
    """Return the first fixture matching just (vendor, pid), or None.

    Used by the discovery table where matrix dimensions haven't been
    queried yet but we still want to surface sub-components in the
    listing.  When LIFX re-uses a pid for products with different
    geometries, this returns the first-loaded match (fixture file
    iteration order) — that's wrong for disambiguation but correct
    enough for "is there any fixture data for this product" checks.
    Use :func:`lookup` whenever the matrix dimensions are available.
    """
    if vendor is None or pid is None:
        return None
    _load_all()
    target: tuple[int, int] = (int(vendor), int(pid))
    for (v, p, _w, _h), data in _cache.items():
        if (v, p) == target:
            return data
    return None


def get_components(
    vendor: Optional[int],
    pid: Optional[int],
    width: Optional[int],
    height: Optional[int],
) -> list[dict[str, Any]]:
    """Return the list of virtual sub-components for this fixture.

    Each component dict has::

        {
          "id":            short stable name, e.g. "uplight"
          "kind":          one of COMPONENT_KIND_*
          "cells":         list of [row, col] this component owns
          "label_suffix":  appended to parent label for sub-device label
        }

    Today the only auto-derived component is ``uplight`` (single
    component per fixture, owning all entries in ``uplight_cells``).
    Future fixtures with richer component graphs can declare them
    directly in JSON; this function will pass that list through once
    we add the schema.

    Returns an empty list if the fixture is unknown or has no
    components.
    """
    fx = lookup(vendor, pid, width, height)
    if fx is None:
        return []
    components: list[dict[str, Any]] = []
    uplights: list[list[int]] = fx.get(KEY_UPLIGHT_CELLS, [])
    if uplights:
        components.append({
            "id": "uplight",
            "kind": COMPONENT_KIND_SINGLE_COLOR,
            "cells": [list(cell) for cell in uplights],
            "label_suffix": "Uplight",
        })
    return components
