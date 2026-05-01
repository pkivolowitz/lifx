"""Boing Ball — antialiased 3x3 circle bouncing off all four walls.

A homage to the 1984 Amiga Boing Ball demo, scaled down to a matrix
grid: a small antialiased circle with a 3x3 cell footprint bounces
inside the playfield, reflecting off all four walls.  No paddles,
no spin, no pattern on the ball — just the iconic motion.

Subpixel everything.  The ball's centre is a pair of floats; each
cell's brightness is the smooth-edge fall-off of the cell-centre's
distance from the ball-centre, evaluated at every frame (so the
ball glides through pixel boundaries without quantising).

Hue and rate semantics match conway2d / pong2d (omit ``--hue`` for
an OkLab brownian-walk auto-cycle; ``--rate slow|medium|fast`` sets
the ball's travel speed in cells per second — same numbers as pong
so the household rate knob feels uniform across effects).

Hardcoded geometry is the SuperColor Ceiling's 8x8 by default but
``--width`` / ``--height`` exposes the playfield for other matrix
fixtures without code changes.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import math
import random
from typing import Optional

from . import (
    DEVICE_TYPE_MATRIX,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT,
    hue_to_u16, pct_to_u16,
)
from ._walkers import (
    ALLOWED_RATES, HUE_AUTO_SENTINEL, HueWalker,
    RATE_CELLS_PER_SEC, RATE_MEDIUM,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default playfield geometry — matches the SuperColor Ceiling.
DEFAULT_WIDTH: int = 8
DEFAULT_HEIGHT: int = 8

# Default brightness — same as conway2d / pong2d for household feel.
DEFAULT_BRIGHTNESS_PCT: int = 80

# Black HSBK — emitted for empty cells.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Ball geometry.  A 3x3 cell footprint inscribes a circle of radius
# 1.5 (cell units).  The antialiased edge spans one cell width — at
# distance ``radius - 0.5`` from centre a cell is fully lit, at
# ``radius + 0.5`` it's fully dark, linear in between.  This is the
# classic 1-pixel ramp anti-aliasing kernel.
BALL_RADIUS: float = 1.5
EDGE_HALF_WIDTH: float = 0.5

# Maximum initial launch angle from the horizontal (radians).  Same
# value pong2d uses — keeps the ball off near-vertical or near-
# horizontal trajectories that look stuck for the first few seconds.
MAX_LAUNCH_ANGLE_RAD: float = math.pi / 4.0  # 45°


def _ball_brightness(
    cell_x: float, cell_y: float, ball_x: float, ball_y: float,
) -> float:
    """Smooth-edge brightness contribution of a single cell.

    ``cell_x``/``cell_y`` are the cell centre coordinates (integer
    column/row).  The kernel is a 1-cell-wide linear ramp on the
    distance from the ball centre — full-on inside ``BALL_RADIUS -
    EDGE_HALF_WIDTH``, full-off beyond ``BALL_RADIUS +
    EDGE_HALF_WIDTH``, linear between.  Returns 0.0..1.0.
    """
    dx: float = cell_x - ball_x
    dy: float = cell_y - ball_y
    dist: float = math.sqrt(dx * dx + dy * dy)
    edge: float = BALL_RADIUS + EDGE_HALF_WIDTH - dist
    if edge <= 0.0:
        return 0.0
    if edge >= 1.0:
        return 1.0
    return edge


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------


class BoingBall(Effect):
    """Antialiased 3x3 circle bouncing off all four walls."""

    name: str = "boing_ball"
    description: str = "Amiga Boing Ball — antialiased circle bouncing in a box"
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
            "Ball speed: slow=1.5, medium=3.0, fast=6.0 cells per second."
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
        """Place the ball at the centre with a random diagonal velocity."""
        self._w: int = int(self.width)
        self._h: int = int(self.height)
        self._ball_speed: float = RATE_CELLS_PER_SEC[str(self.rate)]
        # Legal CENTRE-coordinate range (so the ball stays fully on grid).
        # The ball edge sits at radius from centre; the legal centre
        # range is [radius - 0.5, (w-1) - (radius - 0.5)] because the
        # outermost cells span [-0.5, 0.5] and [w-1-0.5, w-1+0.5].
        # In practice the simpler [radius, w-1-radius] range matches
        # the visual expectation (ball flush with the wall on bounce).
        self._x_min: float = BALL_RADIUS
        self._x_max: float = (self._w - 1) - BALL_RADIUS
        self._y_min: float = BALL_RADIUS
        self._y_max: float = (self._h - 1) - BALL_RADIUS

        self._ball_x: float = (self._w - 1) / 2.0
        self._ball_y: float = (self._h - 1) / 2.0
        angle: float = random.uniform(
            -MAX_LAUNCH_ANGLE_RAD, MAX_LAUNCH_ANGLE_RAD,
        )
        x_sign: float = 1.0 if random.random() < 0.5 else -1.0
        y_sign: float = 1.0 if random.random() < 0.5 else -1.0
        # |cos(angle)|, |sin(angle)| ensure |vx|, |vy| nonzero so the
        # ball never spawns on a perfectly axis-aligned trajectory
        # (which would look like it's just sliding back and forth).
        self._vx: float = x_sign * self._ball_speed * abs(math.cos(angle))
        self._vy: float = y_sign * self._ball_speed * abs(math.sin(angle))

        # Hue source — same convention as conway2d / pong2d.
        if int(self.hue) < 0:
            self._hue_walker: Optional[HueWalker] = HueWalker()
            self._static_hue_u16: int = 0
        else:
            self._hue_walker = None
            self._static_hue_u16 = hue_to_u16(float(self.hue))

        self._last_t: float = 0.0

    # -- Physics ---------------------------------------------------------

    def _step_physics(self, dt: float) -> None:
        """Advance ball by *dt* seconds with reflection off all four walls.

        Reflection mirrors the centre about the wall it crossed.  Doing
        the mirror (rather than just clamping) matters when *dt* is
        large enough that the ball has stepped well past the wall —
        without it, the ball would visibly stick to the wall for one
        frame after a deep overshoot.
        """
        if dt <= 0.0:
            return
        self._ball_x += self._vx * dt
        self._ball_y += self._vy * dt

        if self._ball_x < self._x_min:
            self._ball_x = 2.0 * self._x_min - self._ball_x
            self._vx = -self._vx
        elif self._ball_x > self._x_max:
            self._ball_x = 2.0 * self._x_max - self._ball_x
            self._vx = -self._vx

        if self._ball_y < self._y_min:
            self._ball_y = 2.0 * self._y_min - self._ball_y
            self._vy = -self._vy
        elif self._ball_y > self._y_max:
            self._ball_y = 2.0 * self._y_max - self._ball_y
            self._vy = -self._vy

    # -- Rendering -------------------------------------------------------

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the bouncing antialiased ball."""
        if not hasattr(self, "_w"):
            return [BLACK] * zone_count
        dt: float = t - self._last_t
        self._last_t = t
        self._step_physics(dt)

        if self._hue_walker is not None:
            hue_u16: int = self._hue_walker.hue_u16(t)
        else:
            hue_u16 = self._static_hue_u16
        bri_max: int = pct_to_u16(int(self.brightness))

        colors: list[HSBK] = [BLACK] * zone_count
        # Iterate only the bounding box of the ball — at 3x3 footprint
        # that's at most a 4x4 cell window, so we touch <=16 cells per
        # frame rather than scanning all 64.  The window expands by
        # ``EDGE_HALF_WIDTH`` to catch the soft edge.
        x_lo: int = max(0, int(math.floor(self._ball_x - BALL_RADIUS - EDGE_HALF_WIDTH)))
        x_hi: int = min(self._w - 1,
                         int(math.ceil(self._ball_x + BALL_RADIUS + EDGE_HALF_WIDTH)))
        y_lo: int = max(0, int(math.floor(self._ball_y - BALL_RADIUS - EDGE_HALF_WIDTH)))
        y_hi: int = min(self._h - 1,
                         int(math.ceil(self._ball_y + BALL_RADIUS + EDGE_HALF_WIDTH)))
        for r in range(y_lo, y_hi + 1):
            for c in range(x_lo, x_hi + 1):
                frac: float = _ball_brightness(
                    float(c), float(r), self._ball_x, self._ball_y,
                )
                if frac <= 0.0:
                    continue
                idx: int = r * self._w + c
                if 0 <= idx < zone_count:
                    bri: int = int(bri_max * frac)
                    if bri > 0:
                        colors[idx] = (hue_u16, HSBK_MAX, bri, KELVIN_DEFAULT)
        return colors

    def period(self) -> Optional[float]:
        """Aperiodic — random launch angle yields non-repeating bouncing."""
        return None
