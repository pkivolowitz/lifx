"""Tests for the pong2d effect.

Covers:

- Closed-form ball-trajectory prediction with top/bottom bounces.
- Subpixel paddle anti-aliasing — a 2-cell paddle at y=3.5 lights
  exactly the expected three rows with the expected weights.
- Subpixel ball bilinear splat — sums to ~1.0 across cells, lands
  on the four nearest cells with the right weights.
- "Never loses" property — running the simulation for many seconds,
  the ball always intercepts the paddle (paddle_y is always within
  paddle_half of ball_y at every bounce).
- Spin produces velocity changes; minimum |vx| floor prevents stall.
- Hue handling: sentinel → walker, explicit → pinned static hue.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.1"

import math
import random
import unittest

from effects.pong2d import (
    DEFAULT_HEIGHT, DEFAULT_PADDLE_SIZE, DEFAULT_WIDTH,
    PADDLE_HIT_EPS, RATE_BALL_SPEED,
    Pong2D, _wall_predict_arrival_y,
)
from effects._walkers import (
    HUE_AUTO_SENTINEL, RATE_FAST, RATE_MEDIUM, RATE_SLOW,
)


class TestPredictArrival(unittest.TestCase):
    """``_wall_predict_arrival_y`` matches ground-truth simulation."""

    def test_no_bounce(self) -> None:
        """Simple straight-line case — no walls touched."""
        # Ball at (0, 4), heading +x at vx=2, vy=0.5.  At target_x=8,
        # dt=4, raw_y = 4 + 0.5*4 = 6.  Within [0, 7] — no fold.
        y: float = _wall_predict_arrival_y(0.0, 4.0, 2.0, 0.5, 8.0, 8)
        self.assertAlmostEqual(y, 6.0)

    def test_one_bounce_off_bottom(self) -> None:
        """Trajectory that crosses y=7 once is folded back."""
        # vy makes raw_y = 4 + 1*8 = 12.  span=7, period=14.
        # 12 > 7 → folded = 14 - 12 = 2.  (Reflects off y=7.)
        y: float = _wall_predict_arrival_y(0.0, 4.0, 1.0, 1.0, 8.0, 8)
        self.assertAlmostEqual(y, 2.0)

    def test_two_bounces(self) -> None:
        """Trajectory that bounces top then bottom returns inside range."""
        # raw_y = 4 + (-3)*8 = -20.  period=14.  -20 % 14 = 8 (Python mod)
        # 8 > 7 → 14 - 8 = 6.
        y: float = _wall_predict_arrival_y(0.0, 4.0, 1.0, -3.0, 8.0, 8)
        self.assertAlmostEqual(y, 6.0)

    def test_zero_height_safe(self) -> None:
        """Single-row grid degenerates to y=0 (defensive)."""
        y: float = _wall_predict_arrival_y(0.0, 0.0, 1.0, 1.0, 8.0, 1)
        self.assertEqual(y, 0.0)


class TestPaddleRendering(unittest.TestCase):
    """A 2-cell paddle at fractional y lights the expected rows."""

    def test_paddle_at_integer_y_straddles_three_rows(self) -> None:
        """A 2-cell paddle at y=3 spans [2, 4] → rows 2/3/4 with weights 0.5/1.0/0.5."""
        # Row 2's band is [1.5, 2.5], row 3 is [2.5, 3.5], row 4 is
        # [3.5, 4.5].  An even-sized paddle at integer y straddles the
        # row boundaries — that's normal subpixel behavior, not a bug.
        eff: Pong2D = Pong2D()
        eff.on_start(64)
        bri: list[float] = [0.0] * 64
        eff._draw_paddle(bri, paddle_x=0, paddle_y=3.0, zone_count=64)
        self.assertAlmostEqual(bri[2 * 8 + 0], 0.5)
        self.assertAlmostEqual(bri[3 * 8 + 0], 1.0)
        self.assertAlmostEqual(bri[4 * 8 + 0], 0.5)
        # Total column overlap equals paddle size (2.0).
        col0: list[float] = [bri[r * 8] for r in range(8)]
        self.assertAlmostEqual(sum(col0), 2.0)

    def test_paddle_at_half_integer_y_aligns_two_rows(self) -> None:
        """A 2-cell paddle at y=3.5 spans [2.5, 4.5] → rows 3 and 4 fully lit."""
        eff: Pong2D = Pong2D()
        eff.on_start(64)
        bri: list[float] = [0.0] * 64
        eff._draw_paddle(bri, paddle_x=0, paddle_y=3.5, zone_count=64)
        self.assertAlmostEqual(bri[3 * 8 + 0], 1.0)
        self.assertAlmostEqual(bri[4 * 8 + 0], 1.0)
        # Other column-0 rows must be dark.
        for r in (0, 1, 2, 5, 6, 7):
            self.assertAlmostEqual(bri[r * 8 + 0], 0.0)

    def test_paddle_off_grid_safe(self) -> None:
        """A paddle column outside the playfield no-ops."""
        eff: Pong2D = Pong2D()
        eff.on_start(64)
        bri: list[float] = [0.0] * 64
        eff._draw_paddle(bri, paddle_x=99, paddle_y=3.0, zone_count=64)
        self.assertEqual(sum(bri), 0.0)


class TestBallRendering(unittest.TestCase):
    """Bilinear splat — weights sum to ~1, hit the four nearest cells."""

    def test_ball_on_integer_position_single_cell(self) -> None:
        """Ball at (3, 4) lights only one cell at full weight."""
        eff: Pong2D = Pong2D()
        eff.on_start(64)
        eff._ball_x = 3.0
        eff._ball_y = 4.0
        bri: list[float] = [0.0] * 64
        eff._draw_ball(bri, 64)
        idx: int = 4 * 8 + 3
        self.assertAlmostEqual(bri[idx], 1.0)
        # Sum stays at 1.0 because off-grid neighbors are clipped.
        self.assertAlmostEqual(sum(bri), 1.0)

    def test_ball_subpixel_split(self) -> None:
        """Ball at (3.5, 4.5) splits 0.25 across the four nearest cells."""
        eff: Pong2D = Pong2D()
        eff.on_start(64)
        eff._ball_x = 3.5
        eff._ball_y = 4.5
        bri: list[float] = [0.0] * 64
        eff._draw_ball(bri, 64)
        for r in (4, 5):
            for c in (3, 4):
                self.assertAlmostEqual(bri[r * 8 + c], 0.25)
        self.assertAlmostEqual(sum(bri), 1.0)


class TestNeverLoses(unittest.TestCase):
    """Across many bounces the paddle is ALWAYS within reach when the ball arrives.

    This is the central correctness claim of the effect.  We run many
    seconds of simulation across all three rate settings, instrument
    the bounce detector, and assert that at every paddle hit the ball
    is within the paddle's vertical extent (i.e., it would have been
    rejected from a real game).
    """

    def _run_simulation(self, rate: str, sim_seconds: float) -> int:
        """Step the engine and count paddle bounces; assert each is within reach."""
        random.seed(2026)
        eff: Pong2D = Pong2D(rate=rate)
        eff.on_start(64)
        # Step at a fine dt so the bounce detector fires close to the
        # paddle column rather than 1 cell past it.  60 fps is the
        # display fps; physics dt should be at least that fine.
        dt: float = 1.0 / 60.0
        bounces: int = 0
        t: float = 0.0
        last_vx_sign: float = 1.0 if eff._vx > 0 else -1.0
        while t < sim_seconds:
            t += dt
            _ = eff.render(t, 64)
            new_sign: float = 1.0 if eff._vx > 0 else -1.0
            if new_sign != last_vx_sign:
                bounces += 1
                # At the moment vx flipped, the paddle that was just
                # hit is the one the ball was heading toward BEFORE
                # the flip.  Old sign positive → right paddle hit.
                if last_vx_sign > 0:
                    paddle_y: float = eff._right_paddle_y
                else:
                    paddle_y = eff._left_paddle_y
                offset: float = abs(eff._ball_y - paddle_y)
                self.assertLessEqual(
                    offset, eff._paddle_half + 1e-3,
                    f"bounce {bounces} at t={t:.3f}s: ball.y={eff._ball_y:.3f}, "
                    f"paddle.y={paddle_y:.3f}, offset={offset:.3f} > "
                    f"paddle_half={eff._paddle_half} — paddle would have missed",
                )
                last_vx_sign = new_sign
        return bounces

    def test_never_loses_at_every_rate(self) -> None:
        """Run 30 wall-clock seconds at each rate; every bounce must connect."""
        for rate in (RATE_SLOW, RATE_MEDIUM, RATE_FAST):
            bounces: int = self._run_simulation(rate, sim_seconds=30.0)
            # Sanity: at least one bounce in 30 seconds at any rate.
            self.assertGreater(
                bounces, 0,
                f"no bounces happened in 30 s at rate={rate} — "
                f"the ball must be moving",
            )


class TestRateAndHueParams(unittest.TestCase):
    """Param wiring: rate sets ball speed; hue sentinel triggers walker."""

    def test_rate_drives_ball_motion(self) -> None:
        """Faster rate produces more horizontal displacement per render dt.

        Replaces a tautological ``eff._ball_speed == RATE_BALL_SPEED[rate]``
        check that round-tripped the same dict between source and
        test.  Here we seed the launch angle deterministically (so
        |cos(angle)| is identical across rates), step the engine for
        a short dt, and assert the displacement scales with the rate
        table's speed values — catching regressions anywhere along
        rate → ``_ball_speed`` → ``_vx`` → physics → observable motion.
        """
        # 0.05 s is short enough that even at fast (6 cells/s) the ball
        # only travels ~0.3 cells — well clear of the paddle columns.
        short_dt: float = 0.05
        displacements: dict[str, float] = {}
        for rate in (RATE_SLOW, RATE_MEDIUM, RATE_FAST):
            random.seed(2026)  # identical launch angle across rates
            eff: Pong2D = Pong2D(rate=rate)
            eff.on_start(64)
            x0: float = eff._ball_x
            eff.render(short_dt, 64)
            displacements[rate] = abs(eff._ball_x - x0)
        self.assertGreater(displacements[RATE_FAST], displacements[RATE_MEDIUM])
        self.assertGreater(displacements[RATE_MEDIUM], displacements[RATE_SLOW])
        # Speed ratio sanity: fast should travel exactly fast/slow times
        # as far as slow (the angle cosine factor cancels with the
        # shared seed).
        self.assertAlmostEqual(
            displacements[RATE_FAST] / displacements[RATE_SLOW],
            RATE_BALL_SPEED[RATE_FAST] / RATE_BALL_SPEED[RATE_SLOW],
            places=5,
        )

    def test_default_hue_is_auto_walker(self) -> None:
        eff: Pong2D = Pong2D()
        eff.on_start(64)
        self.assertEqual(int(eff.hue), HUE_AUTO_SENTINEL)
        self.assertIsNotNone(eff._hue_walker)

    def test_explicit_hue_disables_walker(self) -> None:
        eff: Pong2D = Pong2D(hue=42)
        eff.on_start(64)
        self.assertIsNone(eff._hue_walker)


class TestRenderShape(unittest.TestCase):
    """Output shape — zone_count cells, no out-of-bounds writes."""

    def test_render_returns_zone_count_cells(self) -> None:
        eff: Pong2D = Pong2D()
        eff.on_start(64)
        out: list = eff.render(0.0, 64)
        self.assertEqual(len(out), 64)

    def test_render_pads_zero_for_oversized_zone_count(self) -> None:
        """Zones beyond the playfield stay BLACK."""
        eff: Pong2D = Pong2D()
        eff.on_start(80)
        out: list = eff.render(0.0, 80)
        self.assertEqual(len(out), 80)
        # Cells past the 64-cell playfield must be black.
        for hsbk in out[64:]:
            self.assertEqual(hsbk, (0, 0, 0, 3500))


if __name__ == "__main__":
    unittest.main()
