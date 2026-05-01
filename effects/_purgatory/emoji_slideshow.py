"""Emoji slideshow — random faces with random transition styles.

Cycles through hand-painted 8x8 emoticon bitmaps (smiley family), one
per slide, each shown with one of three transition styles:

  --mode dissolve  — brightness fades the whole emoji in, holds, fades out.
  --mode wipe      — a curtain reveals the emoji from one side, holds,
                     then a curtain hides it from the same side.  Random
                     direction (top/bottom/left/right) per slide.
  --mode slide     — the emoji translates onto the grid from off-screen,
                     dwells centred, then translates off the opposite
                     edge.  Random direction per slide.
  --mode random    — pick one of the three above fresh per slide.

Each slide takes the same total time, split 25 % transition-in /
50 % dwell / 25 % transition-out.  The total per-slide time is set
by ``--rate`` (slow=6 s, medium=3 s, fast=1.5 s — same vocabulary as
the other effects).

Bitmaps are deliberately features-only (no face circle).  At 8x8 a
circle frame eats ~24 of the 64 cells and dominates the silhouette
so all faces look the same from across the room; freeing those cells
for the eyes/mouth/eyebrow features makes each emoji distinct.

Hue handling: same convention as conway2d / pong2d — omit ``--hue``
for an OkLab brownian-walk auto-cycle; pass a number 0..360 to pin.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import random
from typing import Optional

from .. import (
    DEVICE_TYPE_MATRIX,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT,
    hue_to_u16, pct_to_u16,
)
from .._walkers import (
    ALLOWED_RATES, HUE_AUTO_SENTINEL, HueWalker,
    RATE_FAST, RATE_MEDIUM, RATE_SLOW,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Bitmap geometry — 8x8 to match the SuperColor Ceiling.  Effects that
# want to drive a different size emit a black-padded buffer.
GRID_W: int = 8
GRID_H: int = 8

# Default brightness (percent) — matches conway2d / pong2d.
DEFAULT_BRIGHTNESS_PCT: int = 80

# Black HSBK — emitted for empty cells.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Transition modes (--mode value).
MODE_DISSOLVE: str = "dissolve"
MODE_WIPE: str = "wipe"
MODE_SLIDE: str = "slide"
MODE_RANDOM: str = "random"
ALLOWED_MODES: list[str] = [MODE_DISSOLVE, MODE_WIPE, MODE_SLIDE, MODE_RANDOM]

# Per-slide total duration in seconds.  Split 25 / 50 / 25 between
# transition-in, dwell, and transition-out.  Same rate vocabulary as
# the bounce effects but the unit is "seconds per slide" not
# "cells per second" — the mapping is the right one for a slideshow,
# not the right one for a moving ball.
RATE_SLIDE_DURATION_SEC: dict[str, float] = {
    RATE_SLOW:   6.0,
    RATE_MEDIUM: 3.0,
    RATE_FAST:   1.5,
}

# Phase split — ratios of the slide cycle.  TRANSITION_IN_FRAC and
# TRANSITION_OUT_FRAC sum with DWELL_FRAC to 1.0.
TRANSITION_IN_FRAC: float = 0.25
DWELL_FRAC: float = 0.50
TRANSITION_OUT_FRAC: float = 0.25
DWELL_END_FRAC: float = TRANSITION_IN_FRAC + DWELL_FRAC

# Direction codes for wipe / slide (random per slide).
DIR_LEFT: str = "left"
DIR_RIGHT: str = "right"
DIR_UP: str = "up"
DIR_DOWN: str = "down"
DIRECTIONS: tuple[str, ...] = (DIR_LEFT, DIR_RIGHT, DIR_UP, DIR_DOWN)


# ---------------------------------------------------------------------------
# Emoji bitmaps — features-only, 8 rows x 8 cols.
# ---------------------------------------------------------------------------
#
# Each bitmap is a tuple of 8 strings, each string 8 chars long.  ``X``
# = lit cell, anything else (typically ``.``) = dark.  Order matters
# only for debugging — the list is shuffled at runtime, and the random
# draw picks from the values.
#
# Verifying a new bitmap: it must be exactly 8 strings of exactly 8
# characters each.  ``_validate_bitmaps`` runs at import time and
# raises ValueError on any malformed entry so a typo can't ship.

EMOJI_SMILEY: tuple[str, ...] = (
    "........",
    "........",
    "..X..X..",
    "........",
    "........",
    ".X....X.",
    "..XXXX..",
    "........",
)

EMOJI_FROWN: tuple[str, ...] = (
    "........",
    "........",
    "..X..X..",
    "........",
    "........",
    "..XXXX..",
    ".X....X.",
    "........",
)

EMOJI_GRIN: tuple[str, ...] = (
    "........",
    "..X..X..",
    "........",
    ".XXXXXX.",
    ".X....X.",
    ".XXXXXX.",
    "........",
    "........",
)

EMOJI_WINK: tuple[str, ...] = (
    "........",
    "........",
    "..X.....",
    "....XXX.",
    "........",
    ".X....X.",
    "..XXXX..",
    "........",
)

EMOJI_SURPRISED: tuple[str, ...] = (
    "........",
    ".XX..XX.",
    ".XX..XX.",
    "........",
    "........",
    "..XXXX..",
    ".X....X.",
    "..XXXX..",
)

EMOJI_NEUTRAL: tuple[str, ...] = (
    "........",
    "........",
    "..X..X..",
    "........",
    "........",
    "........",
    ".XXXXXX.",
    "........",
)

EMOJI_TONGUE: tuple[str, ...] = (
    "........",
    "........",
    "..X..X..",
    "........",
    ".XXXXXX.",
    ".X...XX.",
    ".XXXXXX.",
    "....XX..",
)

EMOJI_SUNGLASSES: tuple[str, ...] = (
    "........",
    "........",
    ".XXXXXX.",
    ".XXXXXX.",
    "........",
    ".X....X.",
    "..XXXX..",
    "........",
)

EMOJI_CAT: tuple[str, ...] = (
    "........",
    "........",
    "..X..X..",
    "........",
    "........",
    ".X....X.",
    "..X..X..",
    "...XX...",
)

EMOJI_ANGRY: tuple[str, ...] = (
    "X......X",
    ".XX..XX.",
    "..X..X..",
    "........",
    "........",
    "........",
    "..XXXX..",
    "........",
)

EMOJI_SKEPTICAL: tuple[str, ...] = (
    "........",
    "........",
    "..X..X..",
    "........",
    "........",
    "......X.",
    ".....X..",
    "....X...",
)

EMOJI_LAUGHING: tuple[str, ...] = (
    "........",
    "X.X..X.X",
    ".X....X.",
    "X.X..X.X",
    "........",
    ".XXXXXX.",
    "X......X",
    ".XXXXXX.",
)

EMOJI_CRYING: tuple[str, ...] = (
    "........",
    "........",
    ".XX..XX.",
    "..X..X..",
    "..X..X..",
    "..X..X..",
    "........",
    ".XXXXXX.",
)

EMOJI_HAPPY_EYES: tuple[str, ...] = (
    "........",
    "........",
    "..X..X..",
    ".X.XX.X.",
    "........",
    "........",
    ".XXXXXX.",
    "........",
)

EMOJI_SQUINT: tuple[str, ...] = (
    "........",
    "........",
    "X......X",
    ".X....X.",
    "X......X",
    "........",
    ".XXXXXX.",
    "........",
)

EMOJI_ANNOYED: tuple[str, ...] = (
    "........",
    "........",
    "........",
    ".XX..XX.",
    "........",
    "........",
    ".XXXXXX.",
    "........",
)

EMOJI_DIZZY: tuple[str, ...] = (
    "........",
    "........",
    ".XX..XX.",
    ".XX..XX.",
    "........",
    "........",
    ".XXXXXX.",
    "........",
)

EMOJI_STARRY: tuple[str, ...] = (
    "........",
    ".X....X.",
    "XXX..XXX",
    ".X....X.",
    "........",
    "........",
    ".XXXXXX.",
    "........",
)

EMOJI_QUIZZICAL: tuple[str, ...] = (
    "........",
    "........",
    ".XX..X..",
    ".XX.....",
    "........",
    "........",
    "...XX...",
    "........",
)

EMOJI_SLEEPING: tuple[str, ...] = (
    "....XXX.",
    ".......X",
    "......X.",
    ".....XXX",
    "........",
    ".XX..XX.",
    "........",
    ".XXXXXX.",
)

# Master list — shuffled at on_start so the first slide is also random.
EMOJI_BITMAPS: tuple[tuple[str, ...], ...] = (
    EMOJI_SMILEY, EMOJI_FROWN, EMOJI_GRIN, EMOJI_WINK,
    EMOJI_SURPRISED, EMOJI_NEUTRAL, EMOJI_TONGUE, EMOJI_SUNGLASSES,
    EMOJI_CAT, EMOJI_ANGRY, EMOJI_SKEPTICAL, EMOJI_LAUGHING,
    EMOJI_CRYING, EMOJI_HAPPY_EYES, EMOJI_SQUINT, EMOJI_ANNOYED,
    EMOJI_DIZZY, EMOJI_STARRY, EMOJI_QUIZZICAL, EMOJI_SLEEPING,
)


def _validate_bitmaps() -> None:
    """Verify every bitmap is exactly GRID_H rows of GRID_W chars.

    Runs at import — a typo in a new bitmap would otherwise surface as
    an IndexError mid-slide on the ceiling.  Failing fast at import is
    cheap and surfaces the bad bitmap by index.
    """
    for i, bmp in enumerate(EMOJI_BITMAPS):
        if len(bmp) != GRID_H:
            raise ValueError(
                f"emoji bitmap #{i} has {len(bmp)} rows, expected {GRID_H}"
            )
        for r, row in enumerate(bmp):
            if len(row) != GRID_W:
                raise ValueError(
                    f"emoji bitmap #{i} row {r} has {len(row)} cols, "
                    f"expected {GRID_W}: {row!r}"
                )


_validate_bitmaps()


def _bitmap_to_grid(bmp: tuple[str, ...]) -> list[list[float]]:
    """Convert a bitmap-strings table to a row-major list of float (0/1) cells.

    Rows nested as lists for easy ``grid[row][col]`` indexing.  Values
    are floats so the rendering math (which scales by anti-aliasing
    fractions) doesn't mix int and float pointlessly.
    """
    return [
        [1.0 if ch == "X" else 0.0 for ch in row] for row in bmp
    ]


def _sample_bitmap_bilinear(
    grid: list[list[float]], x: float, y: float,
) -> float:
    """Bilinear-interpolate the bitmap at fractional (x, y).

    Used by the slide transition to read the bitmap at a sub-pixel
    offset.  Coordinates outside the bitmap return 0 — slid-off pixels
    become dark, which is what the slideshow wants.
    """
    if x < 0.0 or y < 0.0 or x > GRID_W - 1 or y > GRID_H - 1:
        return 0.0
    x0: int = int(x)
    y0: int = int(y)
    fx: float = x - x0
    fy: float = y - y0
    x1: int = min(GRID_W - 1, x0 + 1)
    y1: int = min(GRID_H - 1, y0 + 1)
    v00: float = grid[y0][x0]
    v10: float = grid[y0][x1]
    v01: float = grid[y1][x0]
    v11: float = grid[y1][x1]
    return (
        (v00 * (1.0 - fx) + v10 * fx) * (1.0 - fy)
        + (v01 * (1.0 - fx) + v11 * fx) * fy
    )


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------


class EmojiSlideshow(Effect):
    """Random emoji + random transition slideshow."""

    name: str = "emoji_slideshow"
    description: str = "Random smiley-family emoji with dissolve/wipe/slide transitions"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    mode = Param(
        MODE_RANDOM, choices=ALLOWED_MODES,
        description=(
            "Transition style: 'dissolve' fades; 'wipe' curtains in/out "
            "from a random side; 'slide' translates from off-screen and "
            "off the opposite side; 'random' picks fresh per slide."
        ),
    )
    rate = Param(
        RATE_MEDIUM, choices=ALLOWED_RATES,
        description=(
            "Per-slide duration: slow=6s, medium=3s, fast=1.5s.  "
            "Split 25/50/25 between transition-in / dwell / "
            "transition-out."
        ),
    )
    hue = Param(
        HUE_AUTO_SENTINEL, min=HUE_AUTO_SENTINEL, max=360,
        description=(
            "Live-pixel hue in degrees.  Omit (or pass -1) to drift "
            "the global hue via brownian walk in OkLab — uniform "
            "20°-50° per leg, ~12 s per leg."
        ),
    )
    brightness = Param(
        DEFAULT_BRIGHTNESS_PCT, min=1, max=100,
        description="Live-pixel peak brightness (percent)",
    )

    def on_start(self, zone_count: int) -> None:
        """Pre-convert bitmaps to grids; pick the first slide's parameters."""
        # Pre-convert each bitmap once — render runs every frame and
        # the conversion is pure waste if it happens per-frame.
        self._grids: list[list[list[float]]] = [
            _bitmap_to_grid(bmp) for bmp in EMOJI_BITMAPS
        ]
        self._slide_duration: float = RATE_SLIDE_DURATION_SEC[str(self.rate)]
        # Slide-cycle bookkeeping.  ``_current_slide_index`` < 0 forces
        # the first render() call to roll a fresh slide; subsequent
        # calls only re-roll when the integer index changes.
        self._current_slide_index: int = -1
        self._current_grid: list[list[float]] = self._grids[0]
        self._current_mode: str = MODE_DISSOLVE
        self._current_dir: str = DIR_LEFT

        # Hue source — same convention as conway2d / pong2d.
        if int(self.hue) < 0:
            self._hue_walker: Optional[HueWalker] = HueWalker()
            self._static_hue_u16: int = 0
        else:
            self._hue_walker = None
            self._static_hue_u16 = hue_to_u16(float(self.hue))

    def _resolve_mode(self) -> str:
        """Return the active transition mode for this slide.

        Consumer Param's ``mode`` may be ``random`` — in which case we
        roll a fresh concrete mode per slide so the user sees variety
        without having to swap effects.
        """
        configured: str = str(self.mode)
        if configured == MODE_RANDOM:
            return random.choice(
                (MODE_DISSOLVE, MODE_WIPE, MODE_SLIDE),
            )
        return configured

    def _roll_next_slide(self) -> None:
        """Pick the next slide's emoji, mode, and direction."""
        self._current_grid = random.choice(self._grids)
        self._current_mode = self._resolve_mode()
        self._current_dir = random.choice(DIRECTIONS)

    # -- Phase math ------------------------------------------------------

    def _phase_progress(
        self, cycle_t: float,
    ) -> tuple[str, float]:
        """Classify the current point in the slide cycle.

        Returns (phase_name, progress) where progress is in [0, 1]:
        - "in"    : 0 → 1 across the transition-in window
        - "dwell" : always 1.0 (full visibility for the whole window)
        - "out"   : 1 → 0 across the transition-out window
        """
        in_end: float = self._slide_duration * TRANSITION_IN_FRAC
        dwell_end: float = self._slide_duration * DWELL_END_FRAC
        if cycle_t < in_end:
            progress: float = cycle_t / in_end if in_end > 0.0 else 1.0
            return ("in", max(0.0, min(1.0, progress)))
        if cycle_t < dwell_end:
            return ("dwell", 1.0)
        out_window: float = self._slide_duration - dwell_end
        if out_window <= 0.0:
            return ("out", 0.0)
        progress = 1.0 - (cycle_t - dwell_end) / out_window
        return ("out", max(0.0, min(1.0, progress)))

    # -- Mode rendering --------------------------------------------------

    def _render_dissolve(
        self, bri_cells: list[float], grid: list[list[float]],
        progress: float, zone_count: int,
    ) -> None:
        """Multiply every lit cell by *progress* — straightforward fade."""
        for r in range(GRID_H):
            for c in range(GRID_W):
                v: float = grid[r][c]
                if v <= 0.0:
                    continue
                idx: int = r * GRID_W + c
                if 0 <= idx < zone_count:
                    bri_cells[idx] += v * progress

    def _render_wipe(
        self, bri_cells: list[float], grid: list[list[float]],
        phase: str, progress: float, direction: str, zone_count: int,
    ) -> None:
        """Curtain wipe — visible region grows from one side, then shrinks from same side.

        Anti-aliased boundary: the boundary cell gets a fractional
        contribution equal to its sub-cell coverage, so motion feels
        smooth even at fast rates.

        The "in" phase reveals: visibility extent grows from 0 to W
        (or H) along the chosen axis.  The "out" phase hides from the
        same edge: visibility extent shrinks from W back to 0.
        """
        if direction in (DIR_LEFT, DIR_RIGHT):
            # Wipe along x.  Extent in cells from the chosen edge.
            extent: float = self._wipe_extent(progress, phase, GRID_W)
            for r in range(GRID_H):
                for c in range(GRID_W):
                    v: float = grid[r][c]
                    if v <= 0.0:
                        continue
                    coverage: float = self._cell_coverage(
                        c, extent, ascending=(direction == DIR_LEFT),
                        grid_size=GRID_W,
                    )
                    if coverage <= 0.0:
                        continue
                    idx: int = r * GRID_W + c
                    if 0 <= idx < zone_count:
                        bri_cells[idx] += v * coverage
        else:
            extent = self._wipe_extent(progress, phase, GRID_H)
            for r in range(GRID_H):
                coverage_row: float = self._cell_coverage(
                    r, extent, ascending=(direction == DIR_UP),
                    grid_size=GRID_H,
                )
                if coverage_row <= 0.0:
                    continue
                for c in range(GRID_W):
                    v = grid[r][c]
                    if v <= 0.0:
                        continue
                    idx = r * GRID_W + c
                    if 0 <= idx < zone_count:
                        bri_cells[idx] += v * coverage_row

    def _wipe_extent(
        self, progress: float, phase: str, grid_size: int,
    ) -> float:
        """Visible-band length in cells for the current wipe phase.

        For a "wipe in" the extent grows 0 → grid_size along the
        chosen axis.  For "wipe out" the extent SHRINKS from grid_size
        back to 0 from the SAME edge — so the side that revealed first
        also disappears first.  Dwell holds at full extent.
        """
        if phase == "dwell":
            return float(grid_size)
        return progress * float(grid_size)

    def _cell_coverage(
        self, idx: int, extent: float, ascending: bool, grid_size: int,
    ) -> float:
        """Fraction (0..1) of this cell that's visible given the wipe extent.

        For ``ascending=True`` the wipe runs from index 0 upward — a
        cell at index ``idx`` is fully covered when ``idx + 1 <=
        extent``.  For ``ascending=False`` (wipe from the opposite
        edge) the geometry is mirrored.
        """
        if not ascending:
            # Mirror: cell at idx is at distance grid_size - 1 - idx
            # from the wipe origin.  Reuse the ascending math against
            # that mirrored index.
            idx = grid_size - 1 - idx
        if extent >= idx + 1.0:
            return 1.0
        if extent <= float(idx):
            return 0.0
        return extent - float(idx)

    def _render_slide(
        self, bri_cells: list[float], grid: list[list[float]],
        phase: str, progress: float, direction: str, zone_count: int,
    ) -> None:
        """Translate the emoji on/off the grid along the chosen axis.

        During "in" the emoji enters from off-screen on one side and
        ends centred at offset 0.  During "out" it continues in the
        same direction off the opposite edge.  Subpixel offset →
        bilinear sample so motion is smooth.
        """
        offset_x: float
        offset_y: float
        offset_x, offset_y = self._slide_offset(phase, progress, direction)
        for r in range(GRID_H):
            for c in range(GRID_W):
                v: float = _sample_bitmap_bilinear(
                    grid, c - offset_x, r - offset_y,
                )
                if v <= 0.0:
                    continue
                idx: int = r * GRID_W + c
                if 0 <= idx < zone_count:
                    bri_cells[idx] += v

    def _slide_offset(
        self, phase: str, progress: float, direction: str,
    ) -> tuple[float, float]:
        """Compute the (offset_x, offset_y) of the bitmap origin for this frame.

        The emoji's "natural" position is offset (0, 0).  During slide-
        in it animates from off-screen (offset = ±grid_size on the
        chosen axis) up to (0, 0).  During slide-out it continues to
        the opposite edge (offset = ∓grid_size).
        """
        if direction == DIR_LEFT:
            # Enters from right (positive offset), exits left (negative).
            if phase == "in":
                return (GRID_W * (1.0 - progress), 0.0)
            if phase == "dwell":
                return (0.0, 0.0)
            # phase == "out": progress goes 1 → 0; offset 0 → -GRID_W.
            return (-GRID_W * (1.0 - progress), 0.0)
        if direction == DIR_RIGHT:
            if phase == "in":
                return (-GRID_W * (1.0 - progress), 0.0)
            if phase == "dwell":
                return (0.0, 0.0)
            return (GRID_W * (1.0 - progress), 0.0)
        if direction == DIR_UP:
            if phase == "in":
                return (0.0, GRID_H * (1.0 - progress))
            if phase == "dwell":
                return (0.0, 0.0)
            return (0.0, -GRID_H * (1.0 - progress))
        # DIR_DOWN
        if phase == "in":
            return (0.0, -GRID_H * (1.0 - progress))
        if phase == "dwell":
            return (0.0, 0.0)
        return (0.0, GRID_H * (1.0 - progress))

    # -- Render ----------------------------------------------------------

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one slideshow frame.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels on the target device.

        Returns:
            ``zone_count`` HSBK tuples in row-major order.  Cells past
            the 8x8 bitmap are emitted black.
        """
        if not hasattr(self, "_grids"):
            return [BLACK] * zone_count

        slide_index: int = int(t / self._slide_duration)
        if slide_index != self._current_slide_index:
            self._current_slide_index = slide_index
            self._roll_next_slide()

        cycle_t: float = t - slide_index * self._slide_duration
        phase, progress = self._phase_progress(cycle_t)

        if self._hue_walker is not None:
            hue_u16: int = self._hue_walker.hue_u16(t)
        else:
            hue_u16 = self._static_hue_u16
        bri_max: int = pct_to_u16(int(self.brightness))

        bri_cells: list[float] = [0.0] * zone_count
        if self._current_mode == MODE_DISSOLVE:
            self._render_dissolve(
                bri_cells, self._current_grid, progress, zone_count,
            )
        elif self._current_mode == MODE_WIPE:
            self._render_wipe(
                bri_cells, self._current_grid, phase, progress,
                self._current_dir, zone_count,
            )
        else:  # MODE_SLIDE
            self._render_slide(
                bri_cells, self._current_grid, phase, progress,
                self._current_dir, zone_count,
            )

        colors: list[HSBK] = [BLACK] * zone_count
        for i, frac in enumerate(bri_cells):
            if frac <= 0.0:
                continue
            bri: int = int(bri_max * min(1.0, frac))
            if bri > 0:
                colors[i] = (hue_u16, HSBK_MAX, bri, KELVIN_DEFAULT)
        return colors

    def period(self) -> Optional[float]:
        """Aperiodic — random emoji + random mode + random direction."""
        return None
