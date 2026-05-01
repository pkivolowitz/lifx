"""Tests for the arcs effect.

Covers:

- Bezier control point makes the curve pass through M at t=0.5.
- Side-A picker stays on top OR left edge of the surround;
  side-B picker stays on bottom OR right edge.
- on_start constructs NUM_ARCS arcs each with its own walker.
- Reseed fires when an arc's traversal completes.
- Render returns zone_count cells; additive overlap of two arcs at
  the same cell brightens the result.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.1"

import math
import random
import unittest

from effects import HSBK_MAX
from effects.arcs import (
    Arcs, BLOB_RADIUS, FADE_IN_FRAC, FADE_OUT_FRAC, NUM_ARCS,
    RATE_TRAVERSAL_SEC, SURROUND_OFFSET, TRAIL_SAMPLES,
    _Arc, _bezier_control, _bezier_eval,
    _pick_on_screen_point, _pick_side_a_point, _pick_side_b_point,
)
from effects._walkers import (
    HueWalker, RATE_FAST, RATE_MEDIUM, RATE_SLOW,
)


class TestBezier(unittest.TestCase):
    """The control-point trick makes the curve hit M exactly at t=0.5."""

    def test_curve_hits_endpoints(self) -> None:
        p0 = (1.0, 2.0)
        pm = (3.0, 4.0)
        p2 = (5.0, 6.0)
        p1 = _bezier_control(p0, pm, p2)
        x0, y0 = _bezier_eval(0.0, p0, p1, p2)
        x2, y2 = _bezier_eval(1.0, p0, p1, p2)
        self.assertAlmostEqual(x0, p0[0])
        self.assertAlmostEqual(y0, p0[1])
        self.assertAlmostEqual(x2, p2[0])
        self.assertAlmostEqual(y2, p2[1])

    def test_curve_passes_through_midpoint(self) -> None:
        p0 = (0.0, 0.0)
        pm = (5.0, -3.0)
        p2 = (10.0, 4.0)
        p1 = _bezier_control(p0, pm, p2)
        xm, ym = _bezier_eval(0.5, p0, p1, p2)
        self.assertAlmostEqual(xm, pm[0])
        self.assertAlmostEqual(ym, pm[1])


class TestPointPickers(unittest.TestCase):
    """A-edge picks land on top OR left; B-edge picks on bottom OR right."""

    def test_side_a_lives_on_top_or_left(self) -> None:
        random.seed(0)
        for _ in range(200):
            x, y = _pick_side_a_point(8, 8)
            on_top: bool = math.isclose(y, -SURROUND_OFFSET, abs_tol=1e-9)
            on_left: bool = math.isclose(x, -SURROUND_OFFSET, abs_tol=1e-9)
            self.assertTrue(
                on_top or on_left,
                f"side-A point ({x}, {y}) is neither on top nor left edge",
            )

    def test_side_b_lives_on_bottom_or_right(self) -> None:
        random.seed(0)
        for _ in range(200):
            x, y = _pick_side_b_point(8, 8)
            on_bottom: bool = math.isclose(y, 7 + SURROUND_OFFSET, abs_tol=1e-9)
            on_right: bool = math.isclose(x, 7 + SURROUND_OFFSET, abs_tol=1e-9)
            self.assertTrue(
                on_bottom or on_right,
                f"side-B point ({x}, {y}) is neither on bottom nor right edge",
            )

    def test_on_screen_inside_grid(self) -> None:
        random.seed(0)
        for _ in range(200):
            x, y = _pick_on_screen_point(8, 8)
            self.assertGreaterEqual(x, 0.0)
            self.assertLessEqual(x, 7.0)
            self.assertGreaterEqual(y, 0.0)
            self.assertLessEqual(y, 7.0)


class TestArcReseed(unittest.TestCase):
    """An arc reseeds its endpoints once the blob crosses t=1."""

    def test_reseed_after_full_traversal(self) -> None:
        random.seed(7)
        arc: _Arc = _Arc(
            w=8, h=8, traversal_sec=2.0, t0=0.0, walker=HueWalker(),
        )
        old_p0: tuple[float, float] = arc._p0
        old_pm: tuple[float, float] = arc._pm
        old_p2: tuple[float, float] = arc._p2
        # Step well past one traversal — reseed should fire.
        arc.maybe_reseed(2.5)
        # At least one of A / M / B should have changed (probability
        # of all three picks landing exactly on the previous values
        # is astronomically low with the float ranges involved).
        changed: bool = (
            arc._p0 != old_p0 or arc._pm != old_pm or arc._p2 != old_p2
        )
        self.assertTrue(changed)


class TestArcsEffect(unittest.TestCase):
    """End-to-end Arcs effect — params, render shape, additive overlap."""

    def test_creates_num_arcs(self) -> None:
        eff: Arcs = Arcs()
        eff.on_start(64)
        self.assertEqual(len(eff._arcs), NUM_ARCS)

    def test_arc_start_times_staggered(self) -> None:
        """Each arc's t_start should differ — that's what defeats the
        lockstep spawn / die that prompted bumping NUM_ARCS to 4."""
        random.seed(0)
        eff: Arcs = Arcs()
        eff.on_start(64)
        starts: set[float] = {arc._t_start for arc in eff._arcs}
        self.assertEqual(
            len(starts), NUM_ARCS,
            "arc start times collided — lockstep spawn risk",
        )

    def test_each_arc_has_own_walker(self) -> None:
        """Walkers must be distinct objects (independent random walks)."""
        eff: Arcs = Arcs()
        eff.on_start(64)
        walker_ids: set[int] = {id(arc._walker) for arc in eff._arcs}
        self.assertEqual(len(walker_ids), NUM_ARCS)

    def test_rate_drives_arc_traversal_speed(self) -> None:
        """At the same wall-clock t a fast arc has progressed further than slow.

        Replaces a tautological ``arc._traversal_sec == RATE_TRAVERSAL_SEC[rate]``
        check.  Both rates start an arc at ``t_start=0``, sample
        ``arc.position(t)`` at the same wall-clock t, and assert the
        progress fraction scales inversely with the traversal-time
        constant — exercising the rate → traversal_sec → progress
        chain that actually drives the visible animation.
        """
        common_t: float = 1.0  # seconds — short enough that even fast doesn't reseed
        progresses: dict[str, float] = {}
        for rate in (RATE_SLOW, RATE_MEDIUM, RATE_FAST):
            eff: Arcs = Arcs(rate=rate)
            eff.on_start(64)
            arc = eff._arcs[0]
            arc._t_start = 0.0  # pin so the comparison is rate-only
            _, _, progress = arc.position(common_t)
            progresses[rate] = progress
        self.assertGreater(progresses[RATE_FAST], progresses[RATE_MEDIUM])
        self.assertGreater(progresses[RATE_MEDIUM], progresses[RATE_SLOW])
        # progress = t / traversal_sec, so the ratio is the inverse of
        # the traversal-time ratio.
        self.assertAlmostEqual(
            progresses[RATE_FAST] / progresses[RATE_SLOW],
            RATE_TRAVERSAL_SEC[RATE_SLOW] / RATE_TRAVERSAL_SEC[RATE_FAST],
            places=5,
        )

    def test_render_returns_zone_count_cells(self) -> None:
        eff: Arcs = Arcs()
        eff.on_start(64)
        out: list = eff.render(0.0, 64)
        self.assertEqual(len(out), 64)

    def test_render_pads_zero_for_oversized_zone_count(self) -> None:
        eff: Arcs = Arcs()
        eff.on_start(80)
        out: list = eff.render(0.0, 80)
        for hsbk in out[64:]:
            self.assertEqual(hsbk, (0, 0, 0, 3500))

    def test_additive_overlap_desaturates(self) -> None:
        """Stacking red + green + blue arcs at one cell produces near-white.

        The previous test asserted only ``bri_three >= bri_one`` — a
        property that holds even if additive blending is entirely
        broken (both the single-arc and stacked cases hit the per-
        channel gamut ceiling, so V == 1 in both).  The actual
        contract of additive RGB blending is **desaturation**:
        red + green + blue → white (saturation → 0), which is
        observable in the HSBK output's saturation channel and
        cannot be reached by any single saturated arc.
        """
        # Override each arc's walker with one that has matching
        # ``_from`` and ``_to`` — at any t the lerp returns the seed
        # hue exactly, giving us a stable per-arc colour to test.
        def _pinned_walker(seed_hue_deg: float) -> HueWalker:
            walker: HueWalker = HueWalker(seed_hue_deg=seed_hue_deg)
            walker._to_deg = walker._from_deg
            return walker

        # Hues chosen 120° apart — at full saturation each lights
        # exactly one RGB channel, so the sum is (1, 1, 1) → white.
        red_deg: float = 0.0
        green_deg: float = 120.0
        blue_deg: float = 240.0

        eff: Arcs = Arcs(brightness=100)
        eff.on_start(64)
        # Force exactly three arcs (red, green, blue) all stacked at
        # cell (4, 4) with envelope at full plateau.
        eff._arcs = eff._arcs[:3]
        for arc, hue_deg in zip(eff._arcs, (red_deg, green_deg, blue_deg)):
            arc._p0 = (4.0, 4.0)
            arc._pm = (4.0, 4.0)
            arc._p2 = (4.0, 4.0)
            arc._p1 = (4.0, 4.0)
            arc._t_start = 0.0
            arc._walker = _pinned_walker(hue_deg)
        # t = 0.5 * traversal puts the envelope on the plateau (full).
        plateau_t: float = eff._arcs[0]._traversal_sec * 0.5
        out: list = eff.render(plateau_t, 64)
        centre: tuple[int, int, int, int] = out[4 * 8 + 4]
        sat_three: int = centre[1]

        # Single-arc baseline at red.
        eff_one: Arcs = Arcs(brightness=100)
        eff_one.on_start(64)
        eff_one._arcs = eff_one._arcs[:1]
        only: _Arc = eff_one._arcs[0]
        only._p0 = (4.0, 4.0)
        only._pm = (4.0, 4.0)
        only._p2 = (4.0, 4.0)
        only._p1 = (4.0, 4.0)
        only._t_start = 0.0
        only._walker = _pinned_walker(red_deg)
        out_one: list = eff_one.render(plateau_t, 64)
        sat_one: int = out_one[4 * 8 + 4][1]

        # Single saturated arc — saturation pegs at HSBK_MAX.
        self.assertEqual(
            sat_one, HSBK_MAX,
            "single red arc should be fully saturated (control case)",
        )
        # Three orthogonal hues stacked — saturation collapses toward
        # 0 (white).  Allow a small slack for HSB round-trip noise.
        self.assertLess(
            sat_three, HSBK_MAX // 8,
            f"red + green + blue should desaturate to near-white; "
            f"got saturation {sat_three} of {HSBK_MAX}",
        )


class TestEnvelope(unittest.TestCase):
    """The per-arc envelope ramps up at spawn, plateaus, ramps down at end."""

    def _arc(self) -> _Arc:
        random.seed(0)
        return _Arc(
            w=8, h=8, traversal_sec=4.0, t0=0.0, walker=HueWalker(),
        )

    def test_envelope_zero_at_spawn(self) -> None:
        arc: _Arc = self._arc()
        self.assertAlmostEqual(arc.envelope(0.0), 0.0)

    def test_envelope_full_after_fade_in(self) -> None:
        arc: _Arc = self._arc()
        # progress = FADE_IN_FRAC → envelope = 1.
        self.assertAlmostEqual(
            arc.envelope(arc._traversal_sec * FADE_IN_FRAC), 1.0,
        )

    def test_envelope_full_through_plateau(self) -> None:
        arc: _Arc = self._arc()
        self.assertAlmostEqual(
            arc.envelope(arc._traversal_sec * 0.5), 1.0,
        )

    def test_envelope_full_at_fade_out_start(self) -> None:
        arc: _Arc = self._arc()
        self.assertAlmostEqual(
            arc.envelope(arc._traversal_sec * (1.0 - FADE_OUT_FRAC)), 1.0,
        )

    def test_envelope_zero_at_end(self) -> None:
        arc: _Arc = self._arc()
        self.assertAlmostEqual(
            arc.envelope(arc._traversal_sec), 0.0,
        )

    def test_envelope_half_midway_through_fade_in(self) -> None:
        arc: _Arc = self._arc()
        t_half: float = arc._traversal_sec * (FADE_IN_FRAC * 0.5)
        self.assertAlmostEqual(arc.envelope(t_half), 0.5)


class TestTrailSamples(unittest.TestCase):
    """positions_for_trail returns TRAIL_SAMPLES samples, head intensity = 1."""

    def test_count_and_head(self) -> None:
        random.seed(0)
        arc: _Arc = _Arc(
            w=8, h=8, traversal_sec=4.0, t0=0.0, walker=HueWalker(),
        )
        samples: list = arc.positions_for_trail(2.0)
        self.assertEqual(len(samples), TRAIL_SAMPLES)
        # Head sample first, intensity == 1.0.
        self.assertAlmostEqual(samples[0][2], 1.0)
        # Tail sample last, intensity > 0 but < 1.
        self.assertGreater(samples[-1][2], 0.0)
        self.assertLess(samples[-1][2], 1.0)


if __name__ == "__main__":
    unittest.main()
