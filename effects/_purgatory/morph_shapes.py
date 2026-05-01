"""Morphing shapes — shrink current shape to a point, expand into a new shape.

Inspired by classic After-Dark / Mystify-style screensavers.  Eight
geometric shapes — hline, vline, slash, bslash, square, diamond,
circle, asterisk — take turns on the grid.  Each cycle:

  shrink old shape  →  point at centre  →  expand new shape

Continuous (no dwell at full size).  When a new shape is picked it's
guaranteed not to repeat the previous one, so every cycle visibly
transforms.

Subpixel everything.  Shapes are evaluated as signed-distance fields
against each cell centre and rendered with a 1-cell-thick anti-
aliased line, so the shape's edges glide smoothly through pixel
boundaries as ``scale`` ramps 1 → 0 → 1.

Rate / hue / brightness semantics match the rest of the matrix
effect family (omit ``--hue`` for an OkLab brownian-walk auto-cycle;
``--rate slow|medium|fast`` sets the cycle duration).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import math
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

# Default playfield geometry — matches the SuperColor Ceiling.  Grid
# is square in practice; the math handles non-square via the smaller
# of the two dimensions for the shape's "max extent".
DEFAULT_WIDTH: int = 8
DEFAULT_HEIGHT: int = 8

# Default brightness — matches the rest of the matrix family.
DEFAULT_BRIGHTNESS_PCT: int = 80

# Black HSBK — emitted for unlit cells.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Per-cycle duration (seconds), per --rate choice.  Cycle = shrink +
# expand, no dwell — see module docstring for why.  Slightly longer
# than the bounce effects because the morph reads slower than ball
# motion: at fast=1.5s/cycle a viewer barely registers the transient
# centre-dot before the new shape emerges.
RATE_CYCLE_DURATION_SEC: dict[str, float] = {
    RATE_SLOW:   6.0,
    RATE_MEDIUM: 3.0,
    RATE_FAST:   1.5,
}

# Half of the cycle is "shrink", the other half is "expand".  Equal
# split is the symmetric choice — uneven split would mean shapes
# linger at one extreme more than the other for no visual benefit.
SHRINK_FRAC: float = 0.5

# Anti-aliased line thickness.  ``1.5 - distance`` rises from 0 at
# distance 1.5 to 1 at distance 0.5, clamped to [0, 1] — a 1-cell-
# thick line with a half-cell soft edge on each side.  Tweak only if
# the visual reads as too thick (raise threshold) or too thin (drop).
LINE_HALF_WIDTH: float = 0.5

# 1/sqrt(2) — used to set diagonal asterisk arms to the same total
# length as the cardinal arms (so the * looks symmetric, not stretched).
INV_SQRT_2: float = 1.0 / math.sqrt(2.0)

# Shape names (--shape would expose these but the spec is "random
# pick", so they're internal).
SHAPE_HLINE: str = "hline"
SHAPE_VLINE: str = "vline"
SHAPE_SLASH: str = "slash"
SHAPE_BSLASH: str = "bslash"
SHAPE_SQUARE: str = "square"
SHAPE_DIAMOND: str = "diamond"
SHAPE_CIRCLE: str = "circle"
SHAPE_ASTERISK: str = "asterisk"
ALL_SHAPES: tuple[str, ...] = (
    SHAPE_HLINE, SHAPE_VLINE, SHAPE_SLASH, SHAPE_BSLASH,
    SHAPE_SQUARE, SHAPE_DIAMOND, SHAPE_CIRCLE, SHAPE_ASTERISK,
)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _segment_distance(
    px: float, py: float,
    x1: float, y1: float, x2: float, y2: float,
) -> float:
    """Shortest distance from point (px, py) to line segment ((x1,y1)-(x2,y2)).

    Standard projection: project the point onto the infinite line,
    clamp the parameter to [0, 1] for the segment, take Euclidean
    distance to the clamped projection.  Degenerate segments (both
    endpoints equal — happens at scale=0) collapse to point-distance.
    """
    dx: float = x2 - x1
    dy: float = y2 - y1
    length_sq: float = dx * dx + dy * dy
    if length_sq <= 0.0:
        return math.hypot(px - x1, py - y1)
    # Projection parameter, clamped to the segment.
    raw_t: float = ((px - x1) * dx + (py - y1) * dy) / length_sq
    t: float = max(0.0, min(1.0, raw_t))
    proj_x: float = x1 + t * dx
    proj_y: float = y1 + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def _shape_segments(
    name: str, scale: float, cx: float, cy: float, half_extent: float,
) -> list[tuple[float, float, float, float]]:
    """Return the line-segment list defining *name* at the given *scale*.

    *half_extent* is half the longest the shape gets at scale=1; the
    actual extent for this frame is ``half_extent * scale``.  Circle
    has no segments — caller handles it via :func:`_circle_distance`.
    """
    h: float = half_extent * scale
    if name == SHAPE_HLINE:
        return [(cx - h, cy, cx + h, cy)]
    if name == SHAPE_VLINE:
        return [(cx, cy - h, cx, cy + h)]
    if name == SHAPE_SLASH:
        # /-shape — bottom-left to top-right (lower row index = higher).
        return [(cx - h, cy + h, cx + h, cy - h)]
    if name == SHAPE_BSLASH:
        # \-shape — top-left to bottom-right.
        return [(cx - h, cy - h, cx + h, cy + h)]
    if name == SHAPE_SQUARE:
        # Hollow square — four sides, no interior fill.
        return [
            (cx - h, cy - h, cx + h, cy - h),
            (cx + h, cy - h, cx + h, cy + h),
            (cx + h, cy + h, cx - h, cy + h),
            (cx - h, cy + h, cx - h, cy - h),
        ]
    if name == SHAPE_DIAMOND:
        # Rotated square — corners on the axes.
        return [
            (cx, cy - h, cx + h, cy),
            (cx + h, cy, cx, cy + h),
            (cx, cy + h, cx - h, cy),
            (cx - h, cy, cx, cy - h),
        ]
    if name == SHAPE_ASTERISK:
        # Eight rays from centre, length h each.  Diagonals scaled by
        # 1/sqrt(2) so the * has equal max-extent in every direction
        # rather than stretching past the cardinal arms diagonally.
        d: float = h * INV_SQRT_2
        return [
            (cx - h, cy, cx + h, cy),
            (cx, cy - h, cx, cy + h),
            (cx - d, cy - d, cx + d, cy + d),
            (cx - d, cy + d, cx + d, cy - d),
        ]
    return []


def _circle_distance(
    px: float, py: float, cx: float, cy: float, radius: float,
) -> float:
    """Shortest distance from (px, py) to a circle of *radius* centred at (cx, cy).

    Returns 0 on the circle, positive both inside and outside (so the
    same anti-aliased ramp draws a ring rather than a filled disc —
    matches the other shape outlines).
    """
    return abs(math.hypot(px - cx, py - cy) - radius)


def _line_brightness(distance: float) -> float:
    """Linear-ramp brightness at *distance* cells from the shape outline.

    Returns 1.0 inside ``LINE_HALF_WIDTH``, ramps linearly to 0 over
    the next cell, clamps below.  Result of the standard 1-cell-thick
    anti-aliased line kernel.
    """
    raw: float = (LINE_HALF_WIDTH + 1.0) - distance
    if raw <= 0.0:
        return 0.0
    if raw >= 1.0:
        return 1.0
    return raw


def _draw_shape(
    bri_cells: list[float], shape_name: str, scale: float,
    cx: float, cy: float, half_extent: float,
    grid_w: int, grid_h: int, zone_count: int,
) -> None:
    """Render *shape_name* at *scale* into the row-major brightness buffer.

    Iterates every cell of the playfield, computes the cell-centre's
    distance to the shape's outline, and adds the anti-aliased line
    brightness.  Additive into ``bri_cells`` so a future caller could
    composite multiple shapes (today we only ever draw one at a time).
    """
    if shape_name == SHAPE_CIRCLE:
        radius: float = half_extent * scale
        for r in range(grid_h):
            for c in range(grid_w):
                dist: float = _circle_distance(
                    float(c), float(r), cx, cy, radius,
                )
                bri: float = _line_brightness(dist)
                if bri <= 0.0:
                    continue
                idx: int = r * grid_w + c
                if 0 <= idx < zone_count:
                    bri_cells[idx] += bri
        return

    segments: list[tuple[float, float, float, float]] = _shape_segments(
        shape_name, scale, cx, cy, half_extent,
    )
    if not segments:
        return
    for r in range(grid_h):
        for c in range(grid_w):
            min_dist: float = min(
                _segment_distance(float(c), float(r), *seg)
                for seg in segments
            )
            bri = _line_brightness(min_dist)
            if bri <= 0.0:
                continue
            idx = r * grid_w + c
            if 0 <= idx < zone_count:
                bri_cells[idx] += bri


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------


class MorphShapes(Effect):
    """Shrink/expand morphing through a rotation of geometric shapes."""

    name: str = "morph_shapes"
    description: str = "Morphing shapes — shrink to a point, expand into a new shape"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    width = Param(
        DEFAULT_WIDTH, min=4, max=64,
        description="Playfield width in cells",
    )
    height = Param(
        DEFAULT_HEIGHT, min=4, max=64,
        description="Playfield height in cells",
    )
    rate = Param(
        RATE_MEDIUM, choices=ALLOWED_RATES,
        description=(
            "Per-cycle duration (shrink + expand): slow=6s, medium=3s, "
            "fast=1.5s.  Each cycle ends with a fully-drawn new shape."
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
        """Set geometry, choose first/second shapes, prime the cycle."""
        self._w: int = int(self.width)
        self._h: int = int(self.height)
        self._cx: float = (self._w - 1) / 2.0
        self._cy: float = (self._h - 1) / 2.0
        # Half the longest the shape gets at scale=1.  Use the smaller
        # of (w, h) so a non-square grid still keeps the shape inside.
        self._half_extent: float = min(self._w - 1, self._h - 1) / 2.0
        self._cycle_duration: float = RATE_CYCLE_DURATION_SEC[str(self.rate)]
        # Two shapes at all times: the one shrinking + the next one
        # expanding.  Pick non-equal pairs so every cycle visibly
        # transforms.  Initial shrinking shape is chosen randomly so
        # the first frame doesn't always start from the same outline.
        self._shrinking_shape: str = random.choice(ALL_SHAPES)
        self._expanding_shape: str = self._next_shape(self._shrinking_shape)
        self._current_cycle_index: int = 0

        # Hue source — same convention as the other matrix effects.
        if int(self.hue) < 0:
            self._hue_walker: Optional[HueWalker] = HueWalker()
            self._static_hue_u16: int = 0
        else:
            self._hue_walker = None
            self._static_hue_u16 = hue_to_u16(float(self.hue))

    def _next_shape(self, exclude: str) -> str:
        """Pick a random shape that's not *exclude* (avoid no-op transitions)."""
        choices: tuple[str, ...] = tuple(s for s in ALL_SHAPES if s != exclude)
        return random.choice(choices)

    def _advance_cycles_to(self, t: float) -> None:
        """Advance the shrinking/expanding pair if cycle boundaries elapsed."""
        target_cycle: int = int(t / self._cycle_duration)
        while self._current_cycle_index < target_cycle:
            # Cycle complete: the expanding shape becomes the new
            # shrinking shape, draw a fresh expanding shape.
            self._shrinking_shape = self._expanding_shape
            self._expanding_shape = self._next_shape(self._shrinking_shape)
            self._current_cycle_index += 1

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the morph cycle.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels on the target device.

        Returns:
            ``zone_count`` HSBK tuples in row-major order.  Cells past
            the configured playfield are emitted black.
        """
        if not hasattr(self, "_w"):
            return [BLACK] * zone_count

        self._advance_cycles_to(t)
        cycle_t: float = t - self._current_cycle_index * self._cycle_duration
        # Shrink phase: scale 1 → 0; expand phase: scale 0 → 1.
        shrink_window: float = self._cycle_duration * SHRINK_FRAC
        if cycle_t < shrink_window:
            phase_progress: float = (
                cycle_t / shrink_window if shrink_window > 0.0 else 0.0
            )
            scale_shrink: float = max(0.0, 1.0 - phase_progress)
            scale_expand: float = 0.0
            shape_to_draw: str = self._shrinking_shape
            scale_to_draw: float = scale_shrink
        else:
            expand_window: float = self._cycle_duration - shrink_window
            phase_progress = (
                (cycle_t - shrink_window) / expand_window
                if expand_window > 0.0 else 1.0
            )
            scale_shrink = 0.0
            scale_expand = max(0.0, min(1.0, phase_progress))
            shape_to_draw = self._expanding_shape
            scale_to_draw = scale_expand

        if self._hue_walker is not None:
            hue_u16: int = self._hue_walker.hue_u16(t)
        else:
            hue_u16 = self._static_hue_u16
        bri_max: int = pct_to_u16(int(self.brightness))

        bri_cells: list[float] = [0.0] * zone_count
        _draw_shape(
            bri_cells, shape_to_draw, scale_to_draw,
            self._cx, self._cy, self._half_extent,
            self._w, self._h, zone_count,
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
        """Aperiodic — random shape selection per cycle."""
        return None
