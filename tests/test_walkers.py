"""Tests for the shared HueWalker (effects/_walkers.py).

Covers per-leg magnitude bounds, sign distribution, leg-boundary
catch-up across skipped frames, and that explicit --hue style
construction yields a hue stream that actually changes over time.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.1"

import random
import unittest

from effects._walkers import (
    HUE_DELTA_MAX_DEG, HUE_DELTA_MIN_DEG, HUE_DEGREES_FULL_CIRCLE,
    HUE_LEG_DURATION_SEC, HueWalker,
)


class TestHueWalkerSteps(unittest.TestCase):
    """Per-leg target draws stay inside the magnitude band."""

    def test_per_leg_magnitude_in_band(self) -> None:
        """Sample many leg transitions; every shortest-arc delta is in [MIN, MAX]."""
        random.seed(2026)
        walker: HueWalker = HueWalker()
        prev_from: float = walker._from_deg
        # Drive forward 200 legs and verify every transition stays in
        # the band.  Using leg-boundary times exactly so each call
        # advances by one leg.
        for i in range(1, 201):
            t: float = i * HUE_LEG_DURATION_SEC
            _ = walker.hue_u16(t)
            new_from: float = walker._from_deg
            diff: float = abs(
                ((new_from - prev_from + 180.0) % 360.0) - 180.0
            )
            self.assertGreaterEqual(
                diff, HUE_DELTA_MIN_DEG - 1e-9,
                f"leg {i}: |delta|={diff} should be >= "
                f"{HUE_DELTA_MIN_DEG} (floor prevents stall)",
            )
            self.assertLessEqual(diff, HUE_DELTA_MAX_DEG + 1e-9)
            self.assertGreaterEqual(new_from, 0.0)
            self.assertLess(new_from, HUE_DEGREES_FULL_CIRCLE)
            prev_from = new_from


class TestHueWalkerTimeBehavior(unittest.TestCase):
    """Walker advances cleanly over time and across skipped frames."""

    def test_within_leg_returns_same_endpoints(self) -> None:
        """Two samples inside the same leg only differ in interpolation."""
        random.seed(0)
        walker: HueWalker = HueWalker()
        from_before: float = walker._from_deg
        to_before: float = walker._to_deg
        _ = walker.hue_u16(HUE_LEG_DURATION_SEC * 0.25)
        _ = walker.hue_u16(HUE_LEG_DURATION_SEC * 0.75)
        # No leg boundary crossed — endpoints unchanged.
        self.assertEqual(walker._from_deg, from_before)
        self.assertEqual(walker._to_deg, to_before)

    def test_catches_up_across_skipped_legs(self) -> None:
        """A jump past several leg boundaries advances the walker correctly."""
        random.seed(1)
        walker: HueWalker = HueWalker()
        # Jump 5 legs forward in one call.
        _ = walker.hue_u16(HUE_LEG_DURATION_SEC * 5.5)
        # _leg_start_t should have advanced by 5 leg durations.
        self.assertAlmostEqual(
            walker._leg_start_t, HUE_LEG_DURATION_SEC * 5.0,
        )

    def test_hue_drifts_substantially_over_many_legs(self) -> None:
        """Many samples across many legs cover a non-trivial fraction of the wheel.

        Replacement for the older "h_a != h_b" check, which was weak —
        a brownian walk can produce near-collisions and the assertion
        wouldn't notice a walker stuck within a degenerate band as
        long as the first-vs-last samples happened to differ.  Here
        we sample 50 points across 50 legs and require the resulting
        hue distribution to span at least 90° on the colour wheel,
        which catches stall and near-stall regressions.
        """
        random.seed(99)
        walker: HueWalker = HueWalker()
        sample_count: int = 50
        hues: list[int] = [
            walker.hue_u16(i * HUE_LEG_DURATION_SEC) for i in range(sample_count)
        ]
        spread: int = max(hues) - min(hues)
        # 90° in u16 units = 65536 / 4 = 16384.
        min_spread_u16: int = 65536 // 4
        self.assertGreater(
            spread, min_spread_u16,
            f"hue spread {spread} u16 over {sample_count} legs is "
            f"narrower than 90° — walker may be stalling",
        )


class TestHueWalkerSeedAndAnchor(unittest.TestCase):
    """``seed_hue_deg`` and ``start_t`` constructor parameters.

    These were never tested but are load-bearing for the ``arcs``
    effect (per-arc seed for distinct starting colours) and for any
    walker constructed mid-effect (a non-zero ``start_t`` must anchor
    the leg boundary so the first sample doesn't fire a fake leg
    crossing).
    """

    def test_seed_hue_deg_pins_initial_from(self) -> None:
        """seed_hue_deg becomes the walker's initial ``_from_deg`` exactly."""
        walker: HueWalker = HueWalker(seed_hue_deg=180.0)
        self.assertEqual(walker._from_deg, 180.0)

    def test_seed_hue_deg_wraps_modulo_360(self) -> None:
        """Out-of-range seeds fold into [0, 360)."""
        walker: HueWalker = HueWalker(seed_hue_deg=370.0)
        self.assertAlmostEqual(walker._from_deg, 10.0)

    def test_start_t_anchors_leg_boundaries(self) -> None:
        """start_t becomes the leg-start anchor; first boundary is at start_t + duration."""
        walker: HueWalker = HueWalker(start_t=100.0)
        self.assertEqual(walker._leg_start_t, 100.0)
        # Sample just past the first leg boundary — anchor advances by
        # exactly one duration, not from t=0.
        walker.hue_u16(100.0 + HUE_LEG_DURATION_SEC + 1e-3)
        self.assertAlmostEqual(
            walker._leg_start_t, 100.0 + HUE_LEG_DURATION_SEC,
        )


if __name__ == "__main__":
    unittest.main()
