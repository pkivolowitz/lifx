"""Refresh ``docs/assets/previews/*`` and ``docs/effects.json``.

Re-renders every existing preview from its JSON sidecar so the GIF
matches current code, then renders new previews for any effects in
:data:`NEW_EFFECTS` that don't yet have a sidecar.  Aggregates all
per-effect sidecars into ``docs/effects.json`` (the manifest the
gallery's ``index.html`` reads).

Matrix-affinity effects (``DEVICE_TYPE_MATRIX in cls.affinity``)
render via ``glowup record --grid WxH`` at the canonical small-LED
matrix resolution :data:`GALLERY_GRID_W` x :data:`GALLERY_GRID_H` —
the resolution effects are *designed* for.  Higher gallery
resolutions blow sparse animations out into mostly-empty cells; 8x8
keeps the imagery legible and matches what real LIFX matrix devices
display.

Usage (from anywhere)::

    ~/venv/bin/python /Users/perrykivolowitz/glowup/tools/refresh_gallery.py

The ``glowup`` CLI is invoked via subprocess so each render runs in
its own process and a crash in one effect doesn't poison the rest.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "1.0"

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths.  REPO is derived from this file's location so the script works
# regardless of CWD; PYTHON points at Perry's venv since matplotlib /
# imageio / pillow live there, not in the system Python.
# ---------------------------------------------------------------------------
REPO: Path = Path(__file__).resolve().parent.parent
PREVIEWS_DIR: Path = REPO / "docs" / "assets" / "previews"
MANIFEST: Path = REPO / "docs" / "effects.json"
PYTHON: str = "/Users/perrykivolowitz/venv/bin/python"
GLOWUP: str = str(REPO / "glowup.py")

# Gallery JS resolves <img src> relative to the page URL, which is
# served from docs/ as the Pages root.  GIFs live in docs/assets/
# previews/, so each sidecar's ``media_url`` must be the path *from
# the gallery page to the GIF*, not just the basename — otherwise the
# browser requests https://…/glowup/<file>.gif and 404s.
MEDIA_URL_PREFIX: str = "assets/previews"

# ---------------------------------------------------------------------------
# Render geometry.
#
# GALLERY_GRID_W / GALLERY_GRID_H — the canonical matrix-LED resolution
# every 2D effect was *designed* for.  8x8 is the sweet spot for the
# LIFX Tile / SuperColor Ceiling form factor: large enough that the
# imagery (Conway gliders, Pong paddles, fireworks bursts) reads as
# such, small enough that effects tuned for native matrix output don't
# look sparse.  Earlier 24x24 previews scattered the same-density
# content over 9× more cells and looked broken on every effect.
#
# OUTPUT_PIXELS_2D — output GIF pixel dimensions for 2D previews.  The
# gallery card uses ``image-rendering: pixelated`` CSS so the matrix
# cells stay crisp when the browser scales them; we just need enough
# pixels to give each cell a clean integer-multiple block.  360 / 8 =
# 45 pixels per cell on the source image.
#
# OUTPUT_W_1D / OUTPUT_H_1D — strip-shaped output preserved for 1D
# effects (zones-along-a-strip).  Matches the original gallery's
# 600x80 ratio so existing 1D previews don't change shape.
# ---------------------------------------------------------------------------
GALLERY_GRID_W: int = 8
GALLERY_GRID_H: int = 8
OUTPUT_PIXELS_2D: int = 360
OUTPUT_W_1D: int = 600
OUTPUT_H_1D: int = 80

# ---------------------------------------------------------------------------
# Strip-shape defaults for 1D previews.  108 zones / 3 zones-per-bulb
# matches a typical LIFX Beam config (36 bulbs); these are what the
# original gallery used and what existing 1D sidecars store.
# ---------------------------------------------------------------------------
DEFAULT_ZONES_1D: int = 108
DEFAULT_ZPB_1D: int = 3

# ---------------------------------------------------------------------------
# Effects to add to the gallery if they don't already have a sidecar.
# Existing sidecars are re-rendered in place from their stored params /
# zones / zpb / duration; nothing here gets re-applied to those.
#
# ``is_2d`` opts the entry into matrix rendering (auto-detected from
# affinity at re-render time, but explicit here for first-time
# rendering before a sidecar exists).
# ---------------------------------------------------------------------------
NEW_EFFECTS: list[dict] = [
    {
        "effect": "arcs",
        "is_2d": True,
        "title": "Arcs",
        "description": "Sweeping color arcs traveling across a matrix",
        "duration": 6.0,
    },
    {
        "effect": "boing_ball",
        "is_2d": True,
        "title": "Boing Ball",
        "description": "The classic Amiga checkerboard sphere bouncing on a 2D grid",
        "duration": 6.0,
    },
    {
        "effect": "conway2d",
        "is_2d": True,
        "title": "Conway's Game of Life",
        "description": "Cellular automaton evolving on a 2D matrix device",
        "duration": 8.0,
    },
    {
        "effect": "fireworks2d",
        "is_2d": True,
        "title": "Fireworks (2D)",
        "description": "Bursts bloom from random points across a matrix surface",
        "duration": 8.0,
    },
    {
        "effect": "leapfrog",
        "zones": DEFAULT_ZONES_1D, "zpb": DEFAULT_ZPB_1D, "params": {},
        "title": "Leapfrog",
        "description": "Pairs of pulses leapfrog past one another along the strip",
        "duration": 6.0,
    },
    {
        "effect": "matrix_rain",
        "is_2d": True,
        "title": "Matrix Rain",
        "description": "Cascading green columns à la The Matrix on a 2D grid",
        "duration": 8.0,
    },
    {
        "effect": "plasma2d",
        "is_2d": True,
        "title": "Plasma (2D)",
        "description": "Smooth flowing plasma field across a matrix surface",
        "duration": 6.0,
    },
    {
        "effect": "pong2d",
        "is_2d": True,
        "title": "Pong",
        "description": "The 1972 paddle-and-ball game played by an autonomous AI",
        "duration": 8.0,
    },
    {
        "effect": "radar_scope",
        "is_2d": True,
        "title": "Radar Scope",
        "description": "Sweeping radar beam with persistent contact afterglow",
        "duration": 6.0,
    },
    {
        "effect": "ripple2d",
        "is_2d": True,
        "title": "Ripple (2D)",
        "description": "Concentric water-like ripples spreading across a matrix",
        "duration": 6.0,
    },
]


def _is_matrix_effect(effect_name: str) -> bool:
    """Return True if *effect_name* is a matrix-native (2D) effect.

    "Matrix-native" means the effect's ``affinity`` includes
    ``DEVICE_TYPE_MATRIX`` *and excludes* ``DEVICE_TYPE_STRIP``.  An
    effect that lists STRIP among its targets has a working 1D
    rendering already and the gallery should preview it as a strip;
    only effects that genuinely require a 2D grid (conway2d, arcs,
    boing_ball, …) get the matrix preview path.  Universal-affinity
    effects (breathe, morse, twinkle — affinity defaults to all three
    device types) thus render as strips, as they always have.
    """
    # Imported lazily so importing this module doesn't drag the whole
    # effect package into a caller that just wants the constants.
    sys.path.insert(0, str(REPO))
    from effects import DEVICE_TYPE_MATRIX, DEVICE_TYPE_STRIP, get_registry  # noqa: E402

    cls: Optional[type] = get_registry().get(effect_name)
    if cls is None:
        return False
    affinity: frozenset = getattr(cls, "affinity", frozenset())
    return DEVICE_TYPE_MATRIX in affinity and DEVICE_TYPE_STRIP not in affinity


# Keys that ``glowup record`` reserves for its own flags (output
# dimensions, etc.).  An effect can declare a Param with the same name
# (cylon.width = "Width of the eye in bulbs"); the recorder's argparse
# rightly rejects it as an effect-flag, but old sidecars may still
# carry such a value in ``params``.  Re-emitting it here as
# ``--width 50`` would override the recorder's own ``--width 600``
# output dimension and produce a 50×80 GIF instead of 600×80.
_RECORDER_RESERVED_FLAGS: frozenset[str] = frozenset({"width", "height"})


def _params_to_flags(params: dict) -> list[str]:
    """Convert ``{key: val}`` to ``["--key", str(val)]``, hyphenating keys.

    Matches the CLI's underscore-to-hyphen Param-name convention
    (``burst_spread`` → ``--burst-spread``).  Skips any key that
    collides with a recorder-reserved flag (see
    :data:`_RECORDER_RESERVED_FLAGS`) so a stale sidecar value can't
    silently override the recorder's output dimensions.
    """
    flags: list[str] = []
    for k, v in params.items():
        if k in _RECORDER_RESERVED_FLAGS:
            continue
        flag: str = "--" + k.replace("_", "-")
        flags.append(flag)
        flags.append(str(v))
    return flags


def render(
    effect: str,
    *,
    is_2d: bool,
    zones: int = DEFAULT_ZONES_1D,
    zpb: int = DEFAULT_ZPB_1D,
    params: Optional[dict] = None,
    duration: Optional[float] = None,
) -> bool:
    """Run ``glowup record`` for one effect.  Return True on success.

    For 2D effects the recorder is invoked with
    ``--grid GALLERY_GRID_W x GALLERY_GRID_H`` (which forces the
    effect's ``width`` / ``height`` Params to match) and a square
    output sized for crisp pixelated CSS scaling.  ``zones`` / ``zpb``
    are ignored in 2D mode — the grid forces ``zones = W*H`` and
    ``zpb = 1``.

    For 1D effects the existing strip-shaped output is preserved.
    """
    out: Path = PREVIEWS_DIR / f"{effect}.gif"
    cmd: list[str] = [
        PYTHON, GLOWUP, "-q", "record", effect,
        "--output", str(out),
        "--format", "gif",
        "--lerp", "lab",
        "--media-url", f"{MEDIA_URL_PREFIX}/{effect}.gif",
    ]
    if is_2d:
        cmd += [
            "--grid", f"{GALLERY_GRID_W}x{GALLERY_GRID_H}",
            "--width", str(OUTPUT_PIXELS_2D),
            "--height", str(OUTPUT_PIXELS_2D),
        ]
    else:
        cmd += [
            "--zones", str(zones),
            "--zpb", str(zpb),
            "--width", str(OUTPUT_W_1D),
            "--height", str(OUTPUT_H_1D),
        ]
    if duration is not None:
        cmd += ["--duration", str(duration)]
    # 2D effects use their natural defaults at the design resolution;
    # passing the sidecar's stored params would re-apply gallery
    # tweaks tuned for a different (probably higher) grid size and
    # break the look.  1D effects keep their stored params verbatim.
    if not is_2d and params:
        cmd += _params_to_flags(params)
    print(
        f"  rendering {effect}{' [2D]' if is_2d else ''} → {out.name}",
        flush=True,
    )
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Tail the stderr — full traces are noisy and the last few
        # hundred chars almost always show the actual error.
        tail: str = proc.stderr[-400:] if proc.stderr else "(no stderr)"
        print(f"    FAILED ({proc.returncode}):\n{tail}", flush=True)
        return False
    return True


def main() -> int:
    """Re-render existing previews, render new ones, rebuild manifest."""
    print("=== Re-rendering existing previews from sidecars ===")
    rerendered: list[str] = []
    failed_existing: list[str] = []
    for sidecar in sorted(PREVIEWS_DIR.glob("*.json")):
        with open(sidecar) as fh:
            meta: dict = json.load(fh)
        effect: str = meta["effect"]
        # Affinity is the single source of truth — sidecars from
        # earlier gallery generations may not record is_2d at all.
        is_2d: bool = _is_matrix_effect(effect)
        if is_2d:
            ok: bool = render(
                effect,
                is_2d=True,
                duration=meta.get("duration"),
            )
        else:
            zones: int = int(meta.get("zones", DEFAULT_ZONES_1D))
            zpb: int = int(meta.get("zpb", DEFAULT_ZPB_1D))
            params: dict = meta.get("params", {})
            ok = render(
                effect,
                is_2d=False,
                zones=zones,
                zpb=zpb,
                params=params,
                duration=meta.get("duration"),
            )
        (rerendered if ok else failed_existing).append(effect)

    print()
    print(f"=== Rendering new previews ({len(NEW_EFFECTS)} candidates) ===")
    rendered_new: list[str] = []
    skipped_existing: list[str] = []
    failed_new: list[str] = []
    for cfg in NEW_EFFECTS:
        sidecar: Path = PREVIEWS_DIR / f"{cfg['effect']}.json"
        if sidecar.exists():
            skipped_existing.append(cfg["effect"])
            continue
        if cfg.get("is_2d"):
            ok = render(
                cfg["effect"],
                is_2d=True,
                duration=cfg.get("duration"),
            )
        else:
            ok = render(
                cfg["effect"],
                is_2d=False,
                zones=cfg["zones"],
                zpb=cfg["zpb"],
                params=cfg.get("params") or {},
                duration=cfg.get("duration"),
            )
        if not ok:
            failed_new.append(cfg["effect"])
            continue
        rendered_new.append(cfg["effect"])
        # Splice title/description onto the freshly written sidecar.
        # The recorder writes a minimal sidecar (effect, params, etc.)
        # without gallery prose — those live here.
        with open(sidecar) as fh:
            meta = json.load(fh)
        meta["title"] = cfg["title"]
        meta["description"] = cfg["description"]
        with open(sidecar, "w") as fh:
            json.dump(meta, fh, indent=2, sort_keys=True)
            fh.write("\n")

    print()
    print("=== Aggregating effects.json manifest ===")
    entries: list[dict] = []
    for sidecar in sorted(PREVIEWS_DIR.glob("*.json")):
        with open(sidecar) as fh:
            entries.append(json.load(fh))
    with open(MANIFEST, "w") as fh:
        json.dump(entries, fh, indent=4, sort_keys=False)
        fh.write("\n")

    print()
    print(f"  re-rendered  {len(rerendered):3d}  ({len(failed_existing)} failed)")
    print(f"  new entries  {len(rendered_new):3d}  ({len(failed_new)} failed)")
    print(f"  pre-existing {len(skipped_existing):3d}  (skipped — sidecar present)")
    print(f"  manifest     {len(entries):3d} effects in {MANIFEST}")
    if failed_existing:
        print(f"  EXISTING FAILURES: {failed_existing}")
    if failed_new:
        print(f"  NEW FAILURES: {failed_new}")
    return 0 if not (failed_existing or failed_new) else 1


if __name__ == "__main__":
    sys.exit(main())
