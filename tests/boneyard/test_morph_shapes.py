"""Tests for the morph_shapes effect.

Covers:

- Anti-aliased line kernel: full inside half-cell, ramps over the
  next cell, off beyond.
- Each shape produces non-empty output at full scale.
- At scale=0 only the centre region lights (single anti-aliased dot).
- Cycle progression rolls the shape pair correctly across boundaries.
- Hue / rate wiring matches the household convention.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import random
import unittest

from effects.morph_shapes import (
    ALL_SHAPES, LINE_HALF_WIDTH, MorphShapes, RATE_CYCLE_DURATION_SEC,
    SHAPE_ASTERISK, SHAPE_CIRCLE, SHAPE_HLINE,
    _circle_distance, _draw_shape, _line_brightness, _segment_distance,
)
from effects._walkers import (
    HUE_AUTO_SENTINEL, RATE_FAST, RATE_MEDIUM, RATE_SLOW,
)


class TestSegmentDistance(unittest.TestCase):
    """``_segment_distance`` returns Euclidean distance to a segment."""

    def test_endpoint_zero(self) -> None:
        """Distance from a segment endpoint to itself is 0."""
        self.assertAlmostEqual(
            _segment_distance(1.0, 2.0, 1.0, 2.0, 5.0, 6.0), 0.0,
        )

    def test_perpendicular_drop(self) -> None:
        """Perpendicular distance to a horizontal segment."""
        # Segment along y=0 from x=0..10; point at (5, 3) → dist = 3.
        self.assertAlmostEqual(
            _segment_distance(5.0, 3.0, 0.0, 0.0, 10.0, 0.0), 3.0,
        )

    def test_off_endpoint_clamps(self) -> None:
        """Past a segment endpoint, distance falls off to that endpoint."""
        # Segment 0..10 on x-axis; point at (-3, 0) → dist = 3.
        self.assertAlmostEqual(
            _segment_distance(-3.0, 0.0, 0.0, 0.0, 10.0, 0.0), 3.0,
        )

    def test_degenerate_collapses_to_point(self) -> None:
        """Both endpoints equal — segment is a point; distance is to that point."""
        self.assertAlmostEqual(
            _segment_distance(3.0, 4.0, 0.0, 0.0, 0.0, 0.0), 5.0,
        )


class TestCircleDistance(unittest.TestCase):
    """``_circle_distance`` returns |dist_from_centre - radius|."""

    def test_on_circle(self) -> None:
        """Point on the circumference returns 0."""
        self.assertAlmostEqual(
            _circle_distance(3.0, 0.0, 0.0, 0.0, 3.0), 0.0,
        )

    def test_inside_returns_inset(self) -> None:
        """Point inside the disc returns the radial gap."""
        self.assertAlmostEqual(
            _circle_distance(1.0, 0.0, 0.0, 0.0, 3.0), 2.0,
        )

    def test_outside_returns_overshoot(self) -> None:
        """Point outside the disc returns the radial overshoot."""
        self.assertAlmostEqual(
            _circle_distance(5.0, 0.0, 0.0, 0.0, 3.0), 2.0,
        )


class TestLineBrightnessKernel(unittest.TestCase):
    """``_line_brightness`` — 1-cell-thick anti-aliased line."""

    def test_dead_centre_full(self) -> None:
        self.assertAlmostEqual(_line_brightness(0.0), 1.0)

    def test_at_inner_edge_full(self) -> None:
        """Distance == LINE_HALF_WIDTH still full bright (kernel ramps after)."""
        self.assertAlmostEqual(_line_brightness(LINE_HALF_WIDTH), 1.0)

    def test_at_outer_edge_zero(self) -> None:
        """At distance LINE_HALF_WIDTH + 1 the kernel is exactly 0."""
        self.assertAlmostEqual(
            _line_brightness(LINE_HALF_WIDTH + 1.0), 0.0,
        )

    def test_mid_ramp_half(self) -> None:
        """Midway through the ramp the brightness is 0.5."""
        self.assertAlmostEqual(
            _line_brightness(LINE_HALF_WIDTH + 0.5), 0.5,
        )

    def test_far_off_zero(self) -> None:
        self.assertAlmostEqual(_line_brightness(10.0), 0.0)


class TestShapeRendering(unittest.TestCase):
    """Each shape lights at least one cell at full scale."""

    def test_every_shape_lights_cells_at_full_scale(self) -> None:
        for name in ALL_SHAPES:
            bri: list[float] = [0.0] * 64
            _draw_shape(
                bri, name, scale=1.0,
                cx=3.5, cy=3.5, half_extent=3.5,
                grid_w=8, grid_h=8, zone_count=64,
            )
            self.assertGreater(
                sum(1 for v in bri if v > 0.0), 0,
                f"shape {name} produced no lit cells at full scale",
            )

    def test_scale_zero_lights_only_centre_neighborhood(self) -> None:
        """At scale=0 the shape collapses to a single point at centre."""
        for name in ALL_SHAPES:
            bri: list[float] = [0.0] * 64
            _draw_shape(
                bri, name, scale=0.0,
                cx=3.5, cy=3.5, half_extent=3.5,
                grid_w=8, grid_h=8, zone_count=64,
            )
            for r in range(8):
                for c in range(8):
                    # Anything more than (LINE_HALF_WIDTH + 1) cells
                    # from the centre must be dark.
                    dx: float = c - 3.5
                    dy: float = r - 3.5
                    dist: float = (dx * dx + dy * dy) ** 0.5
                    if dist > LINE_HALF_WIDTH + 1.0 + 1e-6:
                        self.assertAlmostEqual(
                            bri[r * 8 + c], 0.0,
                            msg=(
                                f"shape {name} at scale=0 lit cell "
                                f"({r},{c}) at dist {dist:.2f}"
                            ),
                        )


class TestCycleProgression(unittest.TestCase):
    """Cycle boundaries advance the shape pair; new shape != old."""

    def test_initial_pair_distinct(self) -> None:
        random.seed(0)
        eff: MorphShapes = MorphShapes()
        eff.on_start(64)
        self.assertNotEqual(eff._shrinking_shape, eff._expanding_shape)

    def test_advance_swaps_shapes(self) -> None:
        random.seed(0)
        eff: MorphShapes = MorphShapes(rate=RATE_FAST)
        eff.on_start(64)
        first_shrinking: str = eff._shrinking_shape
        first_expanding: str = eff._expanding_shape
        # Step past one full cycle — the expanding shape should
        # become the new shrinking shape.
        _ = eff.render(eff._cycle_duration + 0.001, 64)
        self.assertEqual(eff._shrinking_shape, first_expanding)
        # And the new expanding shape must differ from the (new)
        # shrinking shape.
        self.assertNotEqual(eff._expanding_shape, eff._shrinking_shape)
        self.assertNotEqual(
            eff._expanding_shape, first_shrinking,
            "extreme bad luck or non-distinct draw — test seed should "
            "guarantee a fresh pick",
        )

    def test_catches_up_across_skipped_cycles(self) -> None:
        """Big time jumps advance multiple cycles in one call."""
        random.seed(1)
        eff: MorphShapes = MorphShapes(rate=RATE_FAST)
        eff.on_start(64)
        _ = eff.render(eff._cycle_duration * 4.5, 64)
        self.assertEqual(eff._current_cycle_index, 4)


class TestRenderShape(unittest.TestCase):
    """Output buffer length matches zone_count; oversize zones stay BLACK."""

    def test_returns_zone_count_cells(self) -> None:
        eff: MorphShapes = MorphShapes()
        eff.on_start(64)
        out: list = eff.render(0.0, 64)
        self.assertEqual(len(out), 64)

    def test_pads_zero_for_oversized_zone_count(self) -> None:
        eff: MorphShapes = MorphShapes()
        eff.on_start(80)
        out: list = eff.render(0.0, 80)
        for hsbk in out[64:]:
            self.assertEqual(hsbk, (0, 0, 0, 3500))


class TestRateAndHueParams(unittest.TestCase):
    """Param wiring matches the household convention."""

    def test_rate_maps_to_cycle_duration(self) -> None:
        for rate, expected in RATE_CYCLE_DURATION_SEC.items():
            eff: MorphShapes = MorphShapes(rate=rate)
            eff.on_start(64)
            self.assertAlmostEqual(eff._cycle_duration, expected)

    def test_default_hue_is_auto_walker(self) -> None:
        eff: MorphShapes = MorphShapes()
        eff.on_start(64)
        self.assertEqual(int(eff.hue), HUE_AUTO_SENTINEL)
        self.assertIsNotNone(eff._hue_walker)

    def test_explicit_hue_disables_walker(self) -> None:
        eff: MorphShapes = MorphShapes(hue=42)
        eff.on_start(64)
        self.assertIsNone(eff._hue_walker)


if __name__ == "__main__":
    unittest.main()
