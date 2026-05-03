"""Tests for the boing_ball effect.

Covers:

- Antialiased brightness kernel — full inside, full off outside,
  linear ramp across the 1-cell edge.
- Bounding-box rendering hits exactly the cells the kernel says
  should be lit; no phantom corner pixels.
- Bouncing physics — ball never escapes the legal centre range
  across many seconds at every rate; reflections invert velocity.
- Hue / rate wiring matches the household convention.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.1"

import math
import random
import unittest

from effects.boing_ball import (
    BALL_RADIUS, BoingBall, EDGE_HALF_WIDTH, _ball_brightness,
)
from effects._walkers import (
    HUE_LEG_DURATION_SEC, RATE_CELLS_PER_SEC, RATE_FAST,
    RATE_MEDIUM, RATE_SLOW,
)


class TestBrightnessKernel(unittest.TestCase):
    """``_ball_brightness`` — soft 1-cell-wide edge."""

    def test_dead_centre_is_full(self) -> None:
        """Cell at the ball centre returns 1.0."""
        self.assertAlmostEqual(_ball_brightness(3.0, 3.0, 3.0, 3.0), 1.0)

    def test_inside_solid_disc_is_full(self) -> None:
        """Distance well inside ``radius - edge`` returns 1.0."""
        # dist = 0.5, radius - edge = 1.0 → fully lit.
        self.assertAlmostEqual(_ball_brightness(3.5, 3.0, 3.0, 3.0), 1.0)

    def test_far_outside_is_dark(self) -> None:
        """Distance well past ``radius + edge`` returns 0.0."""
        self.assertAlmostEqual(_ball_brightness(10.0, 10.0, 0.0, 0.0), 0.0)

    def test_on_outer_edge_is_zero(self) -> None:
        """At distance = radius + edge the kernel is exactly 0.0."""
        # ball at (0, 0), cell at (radius+edge, 0) = (2.0, 0).
        self.assertAlmostEqual(
            _ball_brightness(BALL_RADIUS + EDGE_HALF_WIDTH, 0.0, 0.0, 0.0),
            0.0,
        )

    def test_on_inner_edge_is_one(self) -> None:
        """At distance = radius - edge the kernel returns 1.0."""
        self.assertAlmostEqual(
            _ball_brightness(BALL_RADIUS - EDGE_HALF_WIDTH, 0.0, 0.0, 0.0),
            1.0,
        )

    def test_mid_edge_is_half(self) -> None:
        """At distance = radius the kernel is at the ramp midpoint (0.5)."""
        self.assertAlmostEqual(
            _ball_brightness(BALL_RADIUS, 0.0, 0.0, 0.0), 0.5,
        )


class TestRenderShape(unittest.TestCase):
    """Render emits the right cell count and lights only the ball region."""

    def test_returns_zone_count_cells(self) -> None:
        eff: BoingBall = BoingBall()
        eff.on_start(64)
        out: list = eff.render(0.0, 64)
        self.assertEqual(len(out), 64)

    def test_only_ball_neighborhood_lit(self) -> None:
        """Cells more than ``radius + edge`` from the ball centre are dark."""
        random.seed(0)
        eff: BoingBall = BoingBall()
        eff.on_start(64)
        out: list = eff.render(0.0, 64)
        for r in range(8):
            for c in range(8):
                dx: float = c - eff._ball_x
                dy: float = r - eff._ball_y
                dist: float = math.hypot(dx, dy)
                idx: int = r * 8 + c
                if dist > BALL_RADIUS + EDGE_HALF_WIDTH:
                    self.assertEqual(
                        out[idx][2], 0,
                        f"cell ({r},{c}) at dist {dist:.2f} should be dark",
                    )

    def test_pads_zero_for_oversized_zone_count(self) -> None:
        """Zones beyond the playfield stay BLACK."""
        eff: BoingBall = BoingBall()
        eff.on_start(80)
        out: list = eff.render(0.0, 80)
        self.assertEqual(len(out), 80)
        for hsbk in out[64:]:
            self.assertEqual(hsbk, (0, 0, 0, 3500))


class TestBouncing(unittest.TestCase):
    """Ball reflects off all four walls and never escapes the playfield."""

    def _run(self, rate: str, sim_seconds: float = 30.0) -> int:
        """Step the engine and count direction reversals; assert containment."""
        random.seed(2026)
        eff: BoingBall = BoingBall(rate=rate)
        eff.on_start(64)
        dt: float = 1.0 / 60.0
        bounces: int = 0
        last_vx_sign: float = 1.0 if eff._vx > 0 else -1.0
        last_vy_sign: float = 1.0 if eff._vy > 0 else -1.0
        t: float = 0.0
        while t < sim_seconds:
            t += dt
            _ = eff.render(t, 64)
            self.assertGreaterEqual(eff._ball_x, eff._x_min - 1e-6)
            self.assertLessEqual(eff._ball_x, eff._x_max + 1e-6)
            self.assertGreaterEqual(eff._ball_y, eff._y_min - 1e-6)
            self.assertLessEqual(eff._ball_y, eff._y_max + 1e-6)
            new_vx_sign: float = 1.0 if eff._vx > 0 else -1.0
            new_vy_sign: float = 1.0 if eff._vy > 0 else -1.0
            if new_vx_sign != last_vx_sign:
                bounces += 1
                last_vx_sign = new_vx_sign
            if new_vy_sign != last_vy_sign:
                bounces += 1
                last_vy_sign = new_vy_sign
        return bounces

    def test_bounces_at_every_rate(self) -> None:
        """Run 30 simulated seconds at each rate; ball stays in bounds, bounces."""
        for rate in (RATE_SLOW, RATE_MEDIUM, RATE_FAST):
            bounces: int = self._run(rate)
            self.assertGreater(
                bounces, 0,
                f"no bounces happened in 30 s at rate={rate}",
            )


class TestRateAndHueParams(unittest.TestCase):
    """Param wiring: rate sets ball speed; hue sentinel triggers walker."""

    def test_rate_drives_ball_motion(self) -> None:
        """Faster rate produces more displacement per render dt.

        Replaces a tautological ``eff._ball_speed == RATE_CELLS_PER_SEC[rate]``
        check.  Seeds the launch angle deterministically, steps for a
        short dt, and asserts the magnitude of the displacement
        vector scales with the rate table's speed values — exercising
        the full rate → ``_ball_speed`` → velocity → physics chain.
        """
        # 0.05 s × fast (6 c/s) = 0.3 cells — well short of any wall on
        # an 8x8 grid with the ball seeded near centre.
        short_dt: float = 0.05
        magnitudes: dict[str, float] = {}
        for rate in (RATE_SLOW, RATE_MEDIUM, RATE_FAST):
            random.seed(2026)  # shared angle across rates
            eff: BoingBall = BoingBall(rate=rate)
            eff.on_start(64)
            x0, y0 = eff._ball_x, eff._ball_y
            eff.render(short_dt, 64)
            dx: float = eff._ball_x - x0
            dy: float = eff._ball_y - y0
            magnitudes[rate] = math.hypot(dx, dy)
        self.assertGreater(magnitudes[RATE_FAST], magnitudes[RATE_MEDIUM])
        self.assertGreater(magnitudes[RATE_MEDIUM], magnitudes[RATE_SLOW])
        # With shared seed the angle factors cancel, so the magnitude
        # ratio collapses to the speed ratio.
        self.assertAlmostEqual(
            magnitudes[RATE_FAST] / magnitudes[RATE_SLOW],
            RATE_CELLS_PER_SEC[RATE_FAST] / RATE_CELLS_PER_SEC[RATE_SLOW],
            places=5,
        )

    @staticmethod
    def _hues_over_window(eff: BoingBall, t_end: float, frames: int) -> set[int]:
        """Render ``frames`` evenly-spaced frames over [0, t_end] and
        return the set of distinct hue values from any lit cell.

        Stepwise rendering matters here because boing_ball advances
        physics by ``dt = t - last_t`` per call — a single jump from
        t=0 to t_end would mirror-reflect the ball straight to a
        position where the antialiased kernel rounds to zero brightness.
        """
        hues: set[int] = set()
        for i in range(frames):
            t: float = t_end * (i / max(1, frames - 1))
            for hsbk in eff.render(t, 64):
                if hsbk[2] > 0:
                    hues.add(hsbk[0])
        return hues

    def test_auto_mode_rotates_hue_over_time(self) -> None:
        """Default (auto) hue mode drifts the rendered hue across
        multiple walker legs.
        """
        random.seed(11)
        eff: BoingBall = BoingBall()
        eff.on_start(64)
        hues = self._hues_over_window(eff, HUE_LEG_DURATION_SEC * 2.5, 60)
        self.assertGreater(len(hues), 1)

    def test_manual_hue_constant_over_time(self) -> None:
        """Explicit --hue pins every lit cell to a single hue across
        frames separated by multiple walker leg durations.
        """
        random.seed(11)
        eff: BoingBall = BoingBall(hue=200)
        eff.on_start(64)
        hues = self._hues_over_window(eff, HUE_LEG_DURATION_SEC * 5.0, 60)
        self.assertEqual(len(hues), 1)


if __name__ == "__main__":
    unittest.main()
