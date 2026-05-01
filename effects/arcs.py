"""Arcs — four blobs trace smooth curves with additive RGB blending.

Four independent arcs run simultaneously.  For each arc:

- A point ``A`` is picked on the imaginary surround's top-or-left
  edge (one cell beyond the visible grid).
- A point ``B`` is picked on the surround's bottom-or-right edge.
- A third point ``M`` is picked anywhere inside the visible grid.
- A smooth curve threads ``A → M → B`` (implemented as a quadratic
  Bezier with the control point chosen so the sample at t=0.5 lands
  exactly on ``M``; the user-facing concept is just "smooth curve
  through three points").
- A small anti-aliased blob travels A → M → B over the cycle,
  trailing a comet-style fade behind it.
- When the blob arrives at B, the arc reseeds — fresh A, M, B.  Hue
  keeps drifting via this arc's own walker without resetting.
- Each arc starts at a random offset within its own traversal cycle,
  so the four arcs spawn / die / cross at uncoordinated times rather
  than blinking on and off in unison.

Each arc carries an independent OkLab brownian-walk hue source —
four different starting hues, four independent drifts.  No
``--hue`` knob: the multi-hue interaction IS the effect, so a single
pinned hue would defeat it.

All four arcs render into a per-cell linear-RGB accumulator; cells
where multiple blobs overlap show additively-blended colour
(red + green = yellow, all four overlapping → near-white) clipped
per-channel to the gamut.

Rate / brightness semantics match the rest of the matrix family.
``--rate slow|medium|fast`` controls how long a single A→B traversal
takes.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import math
import os
import random
import sys
from typing import Optional

from . import (
    DEVICE_TYPE_MATRIX,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT,
    hue_to_u16, pct_to_u16,
)
from ._walkers import (
    ALLOWED_RATES, HueWalker,
    RATE_FAST, RATE_MEDIUM, RATE_SLOW,
)

# Project-root import for sRGB↔HSB helpers — same pattern as the
# walker module uses for ``lerp_color``.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from colorspace import hsb_to_srgb, srgb_to_hsb  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default playfield geometry.
DEFAULT_WIDTH: int = 8
DEFAULT_HEIGHT: int = 8

# Default brightness — matches the rest of the matrix family.
DEFAULT_BRIGHTNESS_PCT: int = 80

# Black HSBK — emitted for unlit cells.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Number of arcs running simultaneously.  Bumped from 3 to 4 after
# initial testing (2026-05-01) — three arcs reseeding in lockstep
# (all starting at t0=0) read as "spawn together, die together"; a
# fourth arc plus randomised initial phases (see ``on_start``) gives
# the layered, uncoordinated spawn/die feel the effect needs.
NUM_ARCS: int = 4

# Cycle duration (seconds for one A→B traversal) per --rate choice.
# Same vocabulary as the bounce effects but expressed as time per
# arc traversal.  Slow gives a meditative crawl; fast pushes the
# blob across in two seconds with the comet still visible.
RATE_TRAVERSAL_SEC: dict[str, float] = {
    RATE_SLOW:   8.0,
    RATE_MEDIUM: 4.0,
    RATE_FAST:   2.0,
}

# Comet trail length as a fraction of the arc traversal time.  At
# 0.25 the trail covers the most recent 25 % of the curve — long
# enough to read as motion, short enough not to overwrite the entire
# Bezier at every frame.
TRAIL_FRAC_OF_TRAVERSAL: float = 0.25

# Number of samples used to discretise the comet trail.  Each sample
# is rendered as an anti-aliased blob with brightness scaling from
# 1.0 at the head to 0 at the tail end.  12 samples is the elbow:
# fewer leaves visible "beads", more is wasted compute.
TRAIL_SAMPLES: int = 12

# Blob geometry — small spot with a 1-cell-radius core and half-cell
# soft edge.  Smaller than boing_ball's ball so the curve traced by
# the head-of-trail reads clearly without overwhelming the grid.
BLOB_RADIUS: float = 1.0
BLOB_EDGE_HALF_WIDTH: float = 0.5

# Surround offset — points A and B sit one cell BEYOND the visible
# grid so the blob enters and exits with motion (not from inside).
SURROUND_OFFSET: int = 1

# Fade envelope for the per-arc appearance / disappearance.  The arc
# ramps from 0 to full brightness over the first FADE_IN_FRAC of its
# traversal and ramps back to 0 over the last FADE_OUT_FRAC.  Both
# applied uniformly to head + comet trail so the whole arc breathes
# in and out as a unit (a per-sample envelope would leave the trail
# bright while the head dimmed at the end — visually weird).  15% on
# each end leaves 70% of the traversal at full brightness.
FADE_IN_FRAC: float = 0.15
FADE_OUT_FRAC: float = 0.15

# Full circle in degrees — used when seeding each arc's hue walker
# with a uniformly-random starting hue.
DEGREES_FULL_CIRCLE: float = 360.0


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _pick_side_a_point(w: int, h: int) -> tuple[float, float]:
    """Pick a uniformly-random point on the surround's top OR left edge.

    Equal probability between the two sides; uniform along each.
    Returned coordinates are floats so the curve math stays in
    floating-point throughout.
    """
    if random.random() < 0.5:
        # Top edge — row = -1, col uniform across the surround.
        return (
            random.uniform(-SURROUND_OFFSET, w - 1 + SURROUND_OFFSET),
            float(-SURROUND_OFFSET),
        )
    # Left edge — col = -1, row uniform across the surround.
    return (
        float(-SURROUND_OFFSET),
        random.uniform(-SURROUND_OFFSET, h - 1 + SURROUND_OFFSET),
    )


def _pick_side_b_point(w: int, h: int) -> tuple[float, float]:
    """Pick a uniformly-random point on the surround's bottom OR right edge."""
    if random.random() < 0.5:
        # Bottom edge — row = h, col uniform across the surround.
        return (
            random.uniform(-SURROUND_OFFSET, w - 1 + SURROUND_OFFSET),
            float(h - 1 + SURROUND_OFFSET),
        )
    # Right edge.
    return (
        float(w - 1 + SURROUND_OFFSET),
        random.uniform(-SURROUND_OFFSET, h - 1 + SURROUND_OFFSET),
    )


def _pick_on_screen_point(w: int, h: int) -> tuple[float, float]:
    """Pick a uniformly-random point inside the visible grid."""
    return (
        random.uniform(0.0, float(w - 1)),
        random.uniform(0.0, float(h - 1)),
    )


def _bezier_control(
    p0: tuple[float, float], pm: tuple[float, float], p2: tuple[float, float],
) -> tuple[float, float]:
    """Compute the control point so the quadratic Bezier passes through *pm* at t=0.5.

    The standard quadratic Bezier is
    ``B(t) = (1-t)²·P0 + 2(1-t)t·P1 + t²·P2``.  Setting t=0.5 gives
    ``B(0.5) = 0.25·P0 + 0.5·P1 + 0.25·P2``; solving for P1 to make
    that equal to ``pm`` yields ``P1 = 2·pm - 0.5·(P0 + P2)``.
    """
    return (
        2.0 * pm[0] - 0.5 * (p0[0] + p2[0]),
        2.0 * pm[1] - 0.5 * (p0[1] + p2[1]),
    )


def _bezier_eval(
    t: float,
    p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float],
) -> tuple[float, float]:
    """Sample the quadratic Bezier at parameter *t* in [0, 1]."""
    omt: float = 1.0 - t
    a: float = omt * omt
    b: float = 2.0 * omt * t
    c: float = t * t
    return (
        a * p0[0] + b * p1[0] + c * p2[0],
        a * p0[1] + b * p1[1] + c * p2[1],
    )


def _blob_brightness(
    cell_x: float, cell_y: float, blob_x: float, blob_y: float,
) -> float:
    """Anti-aliased brightness contribution of a soft blob at a cell centre.

    Same kernel as boing_ball — full inside ``BLOB_RADIUS -
    BLOB_EDGE_HALF_WIDTH``, linear ramp over the next cell, off
    beyond ``BLOB_RADIUS + BLOB_EDGE_HALF_WIDTH``.
    """
    dx: float = cell_x - blob_x
    dy: float = cell_y - blob_y
    dist: float = math.sqrt(dx * dx + dy * dy)
    edge: float = BLOB_RADIUS + BLOB_EDGE_HALF_WIDTH - dist
    if edge <= 0.0:
        return 0.0
    if edge >= 1.0:
        return 1.0
    return edge


# ---------------------------------------------------------------------------
# Per-arc state
# ---------------------------------------------------------------------------


class _Arc:
    """One independent arc — control points, hue walker, traversal time.

    Re-rolls A, M, B when the blob reaches B.  Each arc owns its
    own :class:`HueWalker` so the three arcs drift through colour-
    space independently — exactly the source of the visual variety
    the additive-overlap effect needs.
    """

    def __init__(
        self,
        w: int, h: int, traversal_sec: float, t0: float,
        walker: HueWalker,
    ) -> None:
        self._w: int = w
        self._h: int = h
        self._traversal_sec: float = traversal_sec
        self._walker: HueWalker = walker
        self._roll(t0)

    def _roll(self, t_start: float) -> None:
        """Pick fresh A, M, B + control point + new traversal start time."""
        self._p0: tuple[float, float] = _pick_side_a_point(self._w, self._h)
        self._pm: tuple[float, float] = _pick_on_screen_point(self._w, self._h)
        self._p2: tuple[float, float] = _pick_side_b_point(self._w, self._h)
        self._p1: tuple[float, float] = _bezier_control(
            self._p0, self._pm, self._p2,
        )
        self._t_start: float = t_start

    def position(self, t: float) -> tuple[float, float, float]:
        """Return blob (x, y, traversal_progress) at wall-clock *t*.

        Caller checks the returned progress against 1.0; values >= 1
        mean the arc should reseed before this position is consumed.
        """
        progress: float = (t - self._t_start) / self._traversal_sec
        bx, by = _bezier_eval(
            max(0.0, min(1.0, progress)),
            self._p0, self._p1, self._p2,
        )
        return (bx, by, progress)

    def maybe_reseed(self, t: float) -> None:
        """Reseed if the blob has crossed t=1.  Walker keeps drifting."""
        progress: float = (t - self._t_start) / self._traversal_sec
        while progress >= 1.0:
            self._roll(self._t_start + self._traversal_sec)
            progress = (t - self._t_start) / self._traversal_sec

    def hue_u16(self, t: float) -> int:
        """Current arc hue (LIFX 16-bit) sampled from this arc's walker."""
        return self._walker.hue_u16(t)

    def envelope(self, t: float) -> float:
        """Brightness multiplier in [0, 1] based on head progress.

        Linear ramp up over the first FADE_IN_FRAC of the traversal,
        plateau at 1, linear ramp down over the last FADE_OUT_FRAC.
        Applied uniformly to head + trail so the whole arc fades in
        and out as a unit, rather than the head dimming while the
        trail behind stays at full brightness.
        """
        progress: float = (t - self._t_start) / self._traversal_sec
        progress = max(0.0, min(1.0, progress))
        if progress < FADE_IN_FRAC:
            return progress / FADE_IN_FRAC if FADE_IN_FRAC > 0.0 else 1.0
        if progress > 1.0 - FADE_OUT_FRAC:
            return (
                (1.0 - progress) / FADE_OUT_FRAC
                if FADE_OUT_FRAC > 0.0 else 1.0
            )
        return 1.0

    def positions_for_trail(
        self, t: float,
    ) -> list[tuple[float, float, float]]:
        """Return (x, y, intensity) for the head + comet-trail samples.

        Index 0 is the current head (intensity 1); later entries fall
        off linearly to the tail.  Samples whose corresponding curve
        time is before t_start are clamped to the start.
        """
        out: list[tuple[float, float, float]] = []
        trail_total: float = self._traversal_sec * TRAIL_FRAC_OF_TRAVERSAL
        trail_step: float = (
            trail_total / TRAIL_SAMPLES if TRAIL_SAMPLES > 0 else 0.0
        )
        for i in range(TRAIL_SAMPLES):
            sample_t: float = t - i * trail_step
            progress: float = max(
                0.0, min(1.0, (sample_t - self._t_start) / self._traversal_sec),
            )
            x, y = _bezier_eval(progress, self._p0, self._p1, self._p2)
            intensity: float = 1.0 - i / TRAIL_SAMPLES
            out.append((x, y, intensity))
        return out


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------


class Arcs(Effect):
    """Three blobs tracing cubic curves with additive RGB blending."""

    name: str = "arcs"
    description: str = "Three blobs trace curving paths; overlaps blend additively"
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
            "Per-arc A→B traversal: slow=8s, medium=4s, fast=2s."
        ),
    )
    brightness = Param(
        DEFAULT_BRIGHTNESS_PCT, min=1, max=100,
        description="Live-pixel peak brightness (percent)",
    )

    def on_start(self, zone_count: int) -> None:
        """Construct ``NUM_ARCS`` arcs with staggered start times + walkers."""
        self._w: int = int(self.width)
        self._h: int = int(self.height)
        traversal_sec: float = RATE_TRAVERSAL_SEC[str(self.rate)]
        # Stagger each arc's traversal start time by a uniformly-
        # random offset in [-traversal_sec, 0].  At t=0 each arc is
        # therefore at a random point in its own cycle — they don't
        # all spawn from A together and they reseed at different
        # times, giving the layered, uncoordinated comet look the
        # additive-blending effect needs.
        # Each walker also gets a uniformly-random seed hue so the
        # four arcs don't begin overlapping in colour.
        self._arcs: list[_Arc] = [
            _Arc(
                w=self._w, h=self._h, traversal_sec=traversal_sec,
                t0=-random.uniform(0.0, traversal_sec),
                walker=HueWalker(
                    seed_hue_deg=random.uniform(
                        0.0, DEGREES_FULL_CIRCLE,
                    ),
                ),
            )
            for _ in range(NUM_ARCS)
        ]

    # -- Render ----------------------------------------------------------

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame.

        For each arc: advance any reseed boundaries, then add the
        head + comet-trail's RGB contribution to a per-cell linear-
        RGB accumulator.  After all three arcs render, convert each
        cell's accumulator to HSBK with per-channel clipping at 1.0
        (additive overlap → desaturated brighter colour, capped at
        the gamut edge).
        """
        if not hasattr(self, "_arcs"):
            return [BLACK] * zone_count

        bri_max: int = pct_to_u16(int(self.brightness))

        # Per-cell linear-RGB accumulators.  Floats so accumulation
        # can exceed 1 before the final clip.
        rgb_r: list[float] = [0.0] * zone_count
        rgb_g: list[float] = [0.0] * zone_count
        rgb_b: list[float] = [0.0] * zone_count

        for arc in self._arcs:
            arc.maybe_reseed(t)
            envelope: float = arc.envelope(t)
            if envelope <= 0.0:
                continue  # arc fully faded — skip its render entirely
            hue_u16: int = arc.hue_u16(t)
            # Convert the arc's hue to fully-saturated RGB once per
            # frame; this is the "ink" the comet writes with.
            arc_r, arc_g, arc_b = hsb_to_srgb(
                hue_u16 / float(HSBK_MAX), 1.0, 1.0,
            )
            for sx, sy, intensity in arc.positions_for_trail(t):
                if intensity <= 0.0:
                    continue
                # Modulate by the per-arc envelope so the whole arc
                # (head + trail) fades in/out together.
                intensity *= envelope
                # Bounding window for the blob — same trick as
                # boing_ball, keeps work to ~16 cells per sample.
                x_lo: int = max(0, int(math.floor(
                    sx - BLOB_RADIUS - BLOB_EDGE_HALF_WIDTH,
                )))
                x_hi: int = min(self._w - 1, int(math.ceil(
                    sx + BLOB_RADIUS + BLOB_EDGE_HALF_WIDTH,
                )))
                y_lo: int = max(0, int(math.floor(
                    sy - BLOB_RADIUS - BLOB_EDGE_HALF_WIDTH,
                )))
                y_hi: int = min(self._h - 1, int(math.ceil(
                    sy + BLOB_RADIUS + BLOB_EDGE_HALF_WIDTH,
                )))
                for r in range(y_lo, y_hi + 1):
                    for c in range(x_lo, x_hi + 1):
                        coverage: float = _blob_brightness(
                            float(c), float(r), sx, sy,
                        )
                        if coverage <= 0.0:
                            continue
                        contrib: float = coverage * intensity
                        idx: int = r * self._w + c
                        if 0 <= idx < zone_count:
                            rgb_r[idx] += contrib * arc_r
                            rgb_g[idx] += contrib * arc_g
                            rgb_b[idx] += contrib * arc_b

        # Convert the accumulator to HSBK.  Clip per-channel at 1.0
        # (overlapping arcs blend additively — red+green=yellow, all
        # three=white — and a saturated blob crossing another saturated
        # blob hits the channel ceiling and desaturates, which IS the
        # intended additive look).
        colors: list[HSBK] = [BLACK] * zone_count
        for i in range(zone_count):
            r: float = min(1.0, rgb_r[i])
            g: float = min(1.0, rgb_g[i])
            b: float = min(1.0, rgb_b[i])
            if r <= 0.0 and g <= 0.0 and b <= 0.0:
                continue
            h_frac, s_frac, v_frac = srgb_to_hsb(r, g, b)
            bri: int = int(bri_max * v_frac)
            if bri <= 0:
                continue
            colors[i] = (
                int(round(h_frac * HSBK_MAX)),
                int(round(s_frac * HSBK_MAX)),
                bri,
                KELVIN_DEFAULT,
            )
        return colors

    def period(self) -> Optional[float]:
        """Aperiodic — random reseed plus per-arc walkers."""
        return None
