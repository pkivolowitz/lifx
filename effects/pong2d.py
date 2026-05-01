"""Pong on a 2D matrix grid — left/right paddles, infinite rally.

A ball bounces between two paddles positioned at the leftmost and
rightmost columns of a width × height grid (default 8x8 — fits the
LIFX SuperColor Ceiling).  Both paddles are AI-controlled and play
"perfect defense" — each predicts the ball's arrival y at its column
(accounting for top/bottom wall reflections), then moves toward
that prediction at exactly the rate needed to arrive on time.  No
serves, no scoring, no game-over — every collision bounces.

Subpixel motion: the ball's (x, y) and the paddles' y-positions are
floats.  The frame is rasterised by a simple anti-aliased point
splat for the ball (bilinear contribution to the four nearest
cells) and a vertical-extent overlap for each paddle (a 2-cell
paddle at y=3.5 lights row 2 at 0.5, row 3 at 1.0, row 4 at 0.5).
The result is smooth motion across discrete pixels — the ball does
not "tick" cell-to-cell.

Spin: when the ball hits a paddle off-centre, its vertical velocity
gets a kick proportional to the offset, so the rally evolves rather
than locking into a steady horizontal line.

Hardcoded for the 8x8 toroidal mask layer's underlying grid by
default but the geometry parameters are exposed (``--width``,
``--height``, ``--paddle-size``) so the same effect drives a Tile,
a Luna, or a future fixture without code changes.

Hue and rate semantics match conway2d (omit ``--hue`` for an OkLab
brownian-walk auto-cycle; ``--rate slow|medium|fast`` controls the
ball's travel speed).  See ``effects/_walkers.py`` for the shared
``--hue`` / ``--rate`` machinery.
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
    RATE_CELLS_PER_SEC, RATE_FAST, RATE_MEDIUM, RATE_SLOW,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default playfield geometry — matches the SuperColor Ceiling's 8x8.
DEFAULT_WIDTH: int = 8
DEFAULT_HEIGHT: int = 8

# Default paddle vertical extent in cells.  A 2-cell paddle is just
# big enough to read as "paddle, not ball" on an 8-tall grid while
# leaving most of the field for the ball to roam.
DEFAULT_PADDLE_SIZE: int = 2

# Default brightness — same as conway2d/matrix_rain for a consistent
# household feel.
DEFAULT_BRIGHTNESS_PCT: int = 80

# Black HSBK — emitted for empty cells.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Ball-speed mapping per --rate choice — pulled from the shared
# RATE_CELLS_PER_SEC table in :mod:`._walkers` so pong/boing/future
# bounce effects all feel like the same knob.  Re-exported here so
# tests don't have to chase the value across modules.
RATE_BALL_SPEED: dict[str, float] = RATE_CELLS_PER_SEC

# Maximum initial launch angle from the horizontal (radians).  Keeping
# the initial vy modest avoids spawning the ball on a near-vertical
# trajectory that would just tap top↔bottom forever before reaching a
# paddle.  Spin during play can push the angle higher.
MAX_LAUNCH_ANGLE_RAD: float = math.pi / 4.0  # 45°

# Spin coefficient — fraction of the (ball.y - paddle.y) offset added
# to vy on a paddle hit.  Tuned so an edge hit (offset = paddle_half)
# noticeably bends the trajectory without sending the ball into a
# vertical loop.
SPIN_COEFFICIENT: float = 0.6

# Cap on |vy| after spin so the ball never accumulates so much vertical
# momentum that it's effectively bouncing on the side walls.  Multiple
# of the configured ball speed.
MAX_VY_MULTIPLIER: float = 1.5

# Minimum |vx| (cells/sec).  After spin, vx might decay below this
# from energy conservation; we don't enforce energy conservation
# (we want the rally to keep moving), but a tiny vx would stall the
# game.  This floor keeps the ball moving toward the next paddle.
MIN_VX_FRACTION: float = 0.4

# Tolerance for "ball reached the paddle column" detection.  At
# discrete dt ticks the ball can overshoot the column by a fraction
# of a cell; the bounce code reflects from the exact x of the paddle
# rather than wherever the ball happened to land.  Float-comparison
# epsilon keeps the bounce detector robust without affecting visuals.
PADDLE_HIT_EPS: float = 1e-6


def _wall_predict_arrival_y(
    ball_x: float, ball_y: float, vx: float, vy: float,
    target_x: float, height: int,
) -> float:
    """Predict the ball's y when it reaches *target_x*, with top/bottom bounces.

    Closed-form: project the ball's y forward by ``vy * dt`` then
    "fold" the resulting infinite-y coordinate back into the legal
    range [0, height-1] via a triangle wave (period = 2*(height-1)).
    Equivalent to simulating the bounces step-by-step but O(1).

    Args:
        ball_x, ball_y: Current ball position in cells.
        vx, vy:         Current velocity in cells/sec.  ``vx`` MUST be
                        nonzero and pointing toward *target_x*.
        target_x:       The x-coordinate the paddle column sits at.
        height:         Grid height in cells (rows 0..height-1).

    Returns:
        The predicted y-coordinate in [0, height-1].
    """
    # Time to reach target_x.  vx sign matches direction toward target.
    dt: float = (target_x - ball_x) / vx
    raw_y: float = ball_y + vy * dt
    span: float = float(height - 1)
    if span <= 0.0:
        return 0.0
    period: float = 2.0 * span
    folded: float = raw_y % period
    if folded < 0:
        folded += period
    if folded > span:
        folded = period - folded
    return folded


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------


class Pong2D(Effect):
    """Subpixel pong with perfect-defense AI on both paddles."""

    name: str = "pong2d"
    description: str = "Pong with perfect-defense paddles, infinite rally"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    width = Param(
        DEFAULT_WIDTH, min=4, max=64,
        description="Playfield width in cells (paddles at columns 0 and width-1)",
    )
    height = Param(
        DEFAULT_HEIGHT, min=4, max=64,
        description="Playfield height in cells",
    )
    paddle_size = Param(
        DEFAULT_PADDLE_SIZE, min=1, max=8,
        description="Paddle vertical extent in cells",
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
        """Set up geometry, ball, paddles, and the hue source.

        Args:
            zone_count: Total pixels on the target device (informational
                — the playfield is governed by --width/--height; cells
                past the playfield are emitted black).
        """
        self._w: int = int(self.width)
        self._h: int = int(self.height)
        self._psz: int = int(self.paddle_size)
        # Half the paddle extent — used for both rendering overlap
        # and bounce-detection y-range.  Floats throughout for
        # subpixel accuracy.
        self._paddle_half: float = self._psz / 2.0
        self._ball_speed: float = RATE_BALL_SPEED[str(self.rate)]
        # Min |vx| — ensures the ball keeps progressing horizontally
        # even after a high-spin hit shifts most velocity into vy.
        self._min_vx: float = self._ball_speed * MIN_VX_FRACTION
        self._max_vy: float = self._ball_speed * MAX_VY_MULTIPLIER

        # Ball: dead-centre, random angle, random horizontal direction.
        self._ball_x: float = self._w / 2.0
        self._ball_y: float = self._h / 2.0
        angle: float = random.uniform(
            -MAX_LAUNCH_ANGLE_RAD, MAX_LAUNCH_ANGLE_RAD,
        )
        x_sign: float = 1.0 if random.random() < 0.5 else -1.0
        self._vx: float = x_sign * self._ball_speed * math.cos(angle)
        self._vy: float = self._ball_speed * math.sin(angle)

        # Paddle x-positions are fixed (left=0, right=w-1); only y
        # animates.  Both paddles start centred so the first frame
        # doesn't show them off-screen.
        self._left_paddle_x: float = 0.0
        self._right_paddle_x: float = float(self._w - 1)
        # Legal y range for the paddle CENTRE so it stays fully on grid.
        self._paddle_y_min: float = self._paddle_half - 0.5
        self._paddle_y_max: float = (self._h - 1) - (self._paddle_half - 0.5)
        centre_y: float = (self._h - 1) / 2.0
        self._left_paddle_y: float = centre_y
        self._right_paddle_y: float = centre_y

        # Hue source — same convention as conway2d (sentinel < 0 →
        # auto-cycle via shared HueWalker; otherwise pin to --hue).
        if int(self.hue) < 0:
            self._hue_walker: Optional[HueWalker] = HueWalker()
            self._static_hue_u16: int = 0
        else:
            self._hue_walker = None
            self._static_hue_u16 = hue_to_u16(float(self.hue))

        # Wall-clock anchor for dt computation.
        self._last_t: float = 0.0

    # -- Physics ---------------------------------------------------------

    def _step_physics(self, dt: float) -> None:
        """Advance ball + paddles by *dt* seconds with bounces and AI tracking."""
        if dt <= 0.0:
            return
        # Move ball.
        self._ball_x += self._vx * dt
        self._ball_y += self._vy * dt

        # Top / bottom wall reflections.  Reflect the position so a
        # large dt doesn't leave the ball stuck outside the field.
        if self._ball_y < 0.0:
            self._ball_y = -self._ball_y
            self._vy = -self._vy
        elif self._ball_y > self._h - 1:
            self._ball_y = 2.0 * (self._h - 1) - self._ball_y
            self._vy = -self._vy

        # Left paddle: bounce when ball crosses x=0 going leftward.
        if self._ball_x <= self._left_paddle_x + PADDLE_HIT_EPS and self._vx < 0:
            self._bounce_off_paddle(self._left_paddle_y, going_right=True)
            # Reflect position so ball ends up on the playfield side.
            self._ball_x = 2.0 * self._left_paddle_x - self._ball_x
        elif (
            self._ball_x >= self._right_paddle_x - PADDLE_HIT_EPS
            and self._vx > 0
        ):
            self._bounce_off_paddle(self._right_paddle_y, going_right=False)
            self._ball_x = 2.0 * self._right_paddle_x - self._ball_x

        # Update both paddles toward their predicted intercept y.
        self._update_paddle_left(dt)
        self._update_paddle_right(dt)

    def _bounce_off_paddle(
        self, paddle_y: float, going_right: bool,
    ) -> None:
        """Reflect vx, kick vy by spin proportional to off-centre offset.

        The ``going_right`` arg is the post-bounce direction; only used
        to clamp |vx| so that high-spin hits never reduce vx below the
        minimum that would stall the rally.
        """
        offset: float = self._ball_y - paddle_y
        self._vx = -self._vx
        self._vy += offset * SPIN_COEFFICIENT * abs(self._vx)
        # Clamp |vy| so vertical doesn't run away.
        if self._vy > self._max_vy:
            self._vy = self._max_vy
        elif self._vy < -self._max_vy:
            self._vy = -self._max_vy
        # Enforce minimum |vx| so the ball keeps progressing.
        if going_right and self._vx < self._min_vx:
            self._vx = self._min_vx
        elif (not going_right) and self._vx > -self._min_vx:
            self._vx = -self._min_vx

    def _predict_intercept(self, paddle_x: float) -> float:
        """Predict the ball's y at *paddle_x*, clamped to legal paddle-y range."""
        if self._vx == 0.0:
            return self._ball_y
        target_y: float = _wall_predict_arrival_y(
            self._ball_x, self._ball_y, self._vx, self._vy,
            paddle_x, self._h,
        )
        # Clamp to the range the paddle CENTRE can reach without going
        # off-grid; the ball can still hit because the paddle extent
        # straddles its centre.
        return max(self._paddle_y_min, min(self._paddle_y_max, target_y))

    def _update_paddle_left(self, dt: float) -> None:
        """Move the left paddle toward its perfect-defense intercept y."""
        if self._vx >= 0:
            return  # ball heading away — paddle stays put
        target: float = self._predict_intercept(self._left_paddle_x)
        self._left_paddle_y = self._tracking_step(
            self._left_paddle_y, target, dt,
            self._left_paddle_x,
        )

    def _update_paddle_right(self, dt: float) -> None:
        """Move the right paddle toward its perfect-defense intercept y."""
        if self._vx <= 0:
            return
        target: float = self._predict_intercept(self._right_paddle_x)
        self._right_paddle_y = self._tracking_step(
            self._right_paddle_y, target, dt,
            self._right_paddle_x,
        )

    def _tracking_step(
        self, current_y: float, target_y: float, dt: float, paddle_x: float,
    ) -> float:
        """Move *current_y* toward *target_y* at exactly the rate needed to arrive on time.

        The required speed is ``|target - current| / time_to_arrival``;
        moving at that speed for ``dt`` seconds lands the paddle on
        target by the time the ball gets there.  Re-evaluated every
        frame so changing predictions (post-spin, post-bounce) are
        absorbed seamlessly without the paddle ever needing to "rush".
        """
        if abs(self._vx) < PADDLE_HIT_EPS:
            return current_y
        time_to_arrival: float = abs((paddle_x - self._ball_x) / self._vx)
        if time_to_arrival <= dt:
            # Ball lands this tick — snap so the bounce code finds the
            # paddle exactly at the prediction.
            return target_y
        delta: float = target_y - current_y
        # Linear schedule — distance shrinks at the same rate
        # time_to_arrival shrinks, so we land exactly on time.
        return current_y + delta * (dt / time_to_arrival)

    # -- Rendering -------------------------------------------------------

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels on the target device.

        Returns:
            ``zone_count`` HSBK tuples in row-major order.  Cells past
            the configured playfield are emitted black.
        """
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

        # Per-cell brightness fraction in [0, 1]; converted to HSBK
        # at the end.  Float buffer lets ball + paddles blend cleanly
        # if they happen to overlap a cell (additive cap at 1.0).
        bri_cells: list[float] = [0.0] * zone_count

        self._draw_paddle(
            bri_cells, paddle_x=int(round(self._left_paddle_x)),
            paddle_y=self._left_paddle_y, zone_count=zone_count,
        )
        self._draw_paddle(
            bri_cells, paddle_x=int(round(self._right_paddle_x)),
            paddle_y=self._right_paddle_y, zone_count=zone_count,
        )
        self._draw_ball(bri_cells, zone_count)

        colors: list[HSBK] = [BLACK] * zone_count
        for i, frac in enumerate(bri_cells):
            if frac <= 0.0:
                continue
            bri: int = int(bri_max * min(1.0, frac))
            if bri > 0:
                colors[i] = (hue_u16, HSBK_MAX, bri, KELVIN_DEFAULT)
        return colors

    def _draw_paddle(
        self,
        bri_cells: list[float],
        paddle_x: int,
        paddle_y: float,
        zone_count: int,
    ) -> None:
        """Add anti-aliased paddle brightness to the column at *paddle_x*.

        A paddle of extent ``psz`` centred at ``paddle_y`` covers the
        vertical interval ``[paddle_y - psz/2, paddle_y + psz/2]``.
        Each grid row's contribution is the overlap between that row's
        unit-tall band ``[row - 0.5, row + 0.5]`` and the paddle's
        interval, clipped to [0, 1].
        """
        if paddle_x < 0 or paddle_x >= self._w:
            return
        top: float = paddle_y - self._paddle_half
        bot: float = paddle_y + self._paddle_half
        first_row: int = max(0, int(math.floor(top - 0.5)))
        last_row: int = min(self._h - 1, int(math.ceil(bot - 0.5)))
        for row in range(first_row, last_row + 1):
            row_top: float = row - 0.5
            row_bot: float = row + 0.5
            overlap: float = (
                min(bot, row_bot) - max(top, row_top)
            )
            if overlap <= 0.0:
                continue
            idx: int = row * self._w + paddle_x
            if 0 <= idx < zone_count:
                bri_cells[idx] += overlap

    def _draw_ball(
        self, bri_cells: list[float], zone_count: int,
    ) -> None:
        """Bilinear-splat the ball across the four nearest cells.

        Weights are the standard bilinear kernel for a unit-step
        sample at fractional position (fx, fy):

            (1-fx)*(1-fy) → (col,   row)
            (  fx)*(1-fy) → (col+1, row)
            (1-fx)*(  fy) → (col,   row+1)
            (  fx)*(  fy) → (col+1, row+1)

        Cells outside the playfield are skipped — the ball can briefly
        overshoot the edges between physics tick and bounce reflection
        but the rendering should never light a phantom cell.
        """
        col: int = int(math.floor(self._ball_x))
        row: int = int(math.floor(self._ball_y))
        fx: float = self._ball_x - col
        fy: float = self._ball_y - row
        weights: tuple[tuple[int, int, float], ...] = (
            (col,     row,     (1.0 - fx) * (1.0 - fy)),
            (col + 1, row,     fx * (1.0 - fy)),
            (col,     row + 1, (1.0 - fx) * fy),
            (col + 1, row + 1, fx * fy),
        )
        for c, r, w in weights:
            if w <= 0.0:
                continue
            if 0 <= c < self._w and 0 <= r < self._h:
                idx: int = r * self._w + c
                if 0 <= idx < zone_count:
                    bri_cells[idx] += w

    def period(self) -> Optional[float]:
        """Aperiodic — random launch angle + spin make the rally non-repeating."""
        return None
