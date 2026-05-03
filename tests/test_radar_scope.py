"""Tests for the radar_scope effect.

Covers:

- Sweep tip is brightest along the current sweep angle.
- Cells just behind the tip are dimmer (linear tail decay).
- Cells beyond the configured tail arc are dark.
- Outside-radius cells stay dark (clean circular scope shape).
- Hue / rate wiring matches the household convention.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.1"

import math
import random
import unittest

from effects.radar_scope import (
    CENTRE_R_THRESHOLD, MAX_RADIUS_FRAC, RATE_ROTATION_PERIOD_SEC,
    RadarScope, TAIL_ANGLE_RAD, TAU,
)
from effects._walkers import (
    HUE_LEG_DURATION_SEC, RATE_FAST, RATE_MEDIUM, RATE_SLOW,
)


def _cell_angle(eff: RadarScope, r: int, c: int) -> float:
    """Cell-centre angle (radians, [0, TAU)) about the radar centre."""
    dy: float = r - eff._cy
    dx: float = c - eff._cx
    return math.atan2(dy, dx) % TAU


def _cell_bri(out: list, w: int, r: int, c: int) -> int:
    """Brightness u16 of cell (r, c) in a row-major ``render`` output."""
    return out[r * w + c][2]


class TestSweepTipBrightest(unittest.TestCase):
    """At t=0 the sweep is at angle 0; cells just behind the tip are brightest."""

    def test_brightest_cell_is_near_tip(self) -> None:
        """The brightest cell's small (theta - phi) mod TAU is near zero."""
        eff: RadarScope = RadarScope(brightness=100)
        eff.on_start(64)
        out: list = eff.render(0.0, 64)
        # Find the brightest cell off the centre disc.
        best_cell: tuple[int, int] = (0, 0)
        best_bri: int = 0
        for r in range(8):
            for c in range(8):
                radius: float = ((r - eff._cy) ** 2 + (c - eff._cx) ** 2) ** 0.5
                if radius < 1.0:
                    continue  # ignore the always-on centre cells
                bri: int = _cell_bri(out, 8, r, c)
                if bri > best_bri:
                    best_bri = bri
                    best_cell = (r, c)
        self.assertGreater(best_bri, 0)
        # Brightest cell's delta = (theta - phi) mod TAU should be small
        # (near the leading edge of the sweep).
        phi: float = _cell_angle(eff, *best_cell)
        delta: float = (0.0 - phi) % TAU
        self.assertLess(
            delta, TAU * 0.15,
            f"brightest off-centre cell should be near the sweep tip; "
            f"got delta={delta:.3f} rad (cell={best_cell}, phi={phi:.3f})",
        )

    def test_tail_decays_with_angular_distance(self) -> None:
        """A cell deeper into the trail is dimmer than one near the tip."""
        eff: RadarScope = RadarScope(brightness=100)
        eff.on_start(64)
        out: list = eff.render(0.0, 64)
        near_tip: int = -1
        deep_tail: int = -1
        for r in range(8):
            for c in range(8):
                radius: float = ((r - eff._cy) ** 2 + (c - eff._cx) ** 2) ** 0.5
                if radius < 1.0:
                    continue
                phi: float = _cell_angle(eff, r, c)
                delta: float = (0.0 - phi) % TAU  # theta=0 at t=0
                bri: int = _cell_bri(out, 8, r, c)
                if 0.05 < delta < 0.5 and bri > near_tip:
                    near_tip = bri
                if (TAIL_ANGLE_RAD * 0.85) < delta < (TAIL_ANGLE_RAD - 0.05) and bri > deep_tail:
                    deep_tail = bri
        self.assertGreater(near_tip, 0)
        self.assertGreater(near_tip, deep_tail)


class TestBeyondTailDark(unittest.TestCase):
    """Cells with delta > TAIL_ANGLE_RAD from the sweep are dark."""

    def test_dark_arc_just_ahead_of_sweep(self) -> None:
        """At t=0 the dark arc covers cell-angles phi in (0, TAU - TAIL_ANGLE_RAD)."""
        eff: RadarScope = RadarScope(brightness=100)
        eff.on_start(64)
        out: list = eff.render(0.0, 64)
        # Dark arc in delta-space is delta > TAIL_ANGLE_RAD.  With
        # theta=0, delta = (0 - phi) % TAU = TAU - phi for phi > 0.
        # So delta > TAIL_ANGLE_RAD ↔ phi < TAU - TAIL_ANGLE_RAD = π/2.
        # The dark arc in phi-space is therefore phi in (0, π/2)
        # excluding cells too close to the centre.
        found_dark: bool = False
        for r in range(8):
            for c in range(8):
                radius: float = ((r - eff._cy) ** 2 + (c - eff._cx) ** 2) ** 0.5
                if radius < 1.0:
                    continue
                phi: float = _cell_angle(eff, r, c)
                # Cells solidly in the dark arc: phi well inside (0, π/2).
                if 0.2 < phi < (TAU - TAIL_ANGLE_RAD - 0.1):
                    bri: int = _cell_bri(out, 8, r, c)
                    self.assertEqual(
                        bri, 0,
                        f"cell ({r},{c}) at phi={phi:.2f} should be dark",
                    )
                    found_dark = True
        self.assertTrue(
            found_dark,
            "no test cells found in the dark arc — grid geometry mismatch",
        )


class TestOutsideRadius(unittest.TestCase):
    """Cells beyond the inscribed circle stay dark (clean scope outline)."""

    def test_corners_dark(self) -> None:
        eff: RadarScope = RadarScope(brightness=100)
        eff.on_start(64)
        out: list = eff.render(0.0, 64)
        # On 8x8 with centre (3.5, 3.5), max_radius = 3.5.  Corner
        # (0, 0) is at distance √(3.5² + 3.5²) ≈ 4.95, beyond the
        # circle.
        for r, c in ((0, 0), (0, 7), (7, 0), (7, 7)):
            bri: int = _cell_bri(out, 8, r, c)
            self.assertEqual(
                bri, 0,
                f"corner cell ({r},{c}) should be dark (beyond scope radius)",
            )


class TestRotation(unittest.TestCase):
    """The sweep rotates with time — bright cells shift around the centre."""

    def test_bright_cells_at_t0_vs_quarter_period(self) -> None:
        """A quarter-period later, the brightest sector has shifted by ~90°."""
        eff: RadarScope = RadarScope(brightness=100, rate=RATE_FAST)
        eff.on_start(64)
        out_t0: list = eff.render(0.0, 64)
        out_q: list = eff.render(eff._rotation_period * 0.25, 64)

        # Find the cell with maximum brightness in each frame; the
        # angle of that cell should differ by ~π/2 (= TAU/4).
        best_t0: tuple[int, int] = (0, 0)
        best_q: tuple[int, int] = (0, 0)
        max_t0: int = 0
        max_q: int = 0
        for r in range(8):
            for c in range(8):
                if _cell_bri(out_t0, 8, r, c) > max_t0:
                    max_t0 = _cell_bri(out_t0, 8, r, c)
                    best_t0 = (r, c)
                if _cell_bri(out_q, 8, r, c) > max_q:
                    max_q = _cell_bri(out_q, 8, r, c)
                    best_q = (r, c)

        ang_t0: float = _cell_angle(eff, *best_t0)
        ang_q: float = _cell_angle(eff, *best_q)
        # Difference should be roughly π/2 (90°), wrapped.  Allow a
        # generous slack since the brightest discrete cell on an 8x8
        # grid might not sit exactly at the sweep angle.
        diff: float = (ang_q - ang_t0) % TAU
        self.assertGreater(diff, TAU * 0.15)
        self.assertLess(diff, TAU * 0.35)


class TestRateAndHueParams(unittest.TestCase):
    """Param wiring matches the household convention."""

    def test_rate_drives_rotation(self) -> None:
        """Sweeping for one full rate-defined period returns the same frame.

        Replaces a tautological ``_rotation_period == RATE_ROTATION_PERIOD_SEC[rate]``
        check.  Renders at t=0 and at t=period and asserts the two
        frames are identical — ``theta = (omega * t) % TAU`` returns
        to 0 after exactly one period, so the entire frame must
        repeat.  This exercises rate → ``_rotation_period`` → ``_omega``
        → angular position → frame contents.
        """
        for rate in (RATE_SLOW, RATE_MEDIUM, RATE_FAST):
            eff: RadarScope = RadarScope(rate=rate, brightness=100, hue=0)
            eff.on_start(64)
            out_t0: list = eff.render(0.0, 64)
            out_full: list = eff.render(eff._rotation_period, 64)
            self.assertEqual(
                out_t0, out_full,
                f"rate={rate}: frame at t=period should match t=0",
            )
            # And a frame mid-period must differ — proves the sweep is
            # actually moving, not stuck at a constant theta.
            out_mid: list = eff.render(eff._rotation_period * 0.5, 64)
            self.assertNotEqual(out_t0, out_mid)

    @staticmethod
    def _hues_over_window(eff: RadarScope, t_end: float, frames: int) -> set[int]:
        """Render evenly-spaced frames and collect distinct lit-cell hues."""
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
        random.seed(17)
        eff: RadarScope = RadarScope()
        eff.on_start(64)
        hues = self._hues_over_window(eff, HUE_LEG_DURATION_SEC * 2.5, 60)
        self.assertGreater(len(hues), 1)

    def test_manual_hue_constant_over_time(self) -> None:
        """Explicit --hue pins every lit cell to one hue across frames."""
        eff: RadarScope = RadarScope(hue=42)
        eff.on_start(64)
        hues = self._hues_over_window(eff, HUE_LEG_DURATION_SEC * 5.0, 60)
        self.assertEqual(len(hues), 1)


class TestCentreCell(unittest.TestCase):
    """Cells under ``CENTRE_R_THRESHOLD`` are always lit (sweep-independent).

    On the default 8x8 grid the geometric centre is (3.5, 3.5) and no
    cell is closer than √(0.5² + 0.5²) ≈ 0.707 to it, so the centre-
    always-lit branch is dead code at default geometry.  Test on an
    odd grid (9x9, centre (4, 4)) where the centre cell sits exactly
    on the geometric centre and falls under the half-cell threshold.
    """

    # 9x9 keeps the test small but exposes the centre-cell branch.
    ODD_GRID: int = 9

    def test_centre_cell_lit_at_all_sweep_angles(self) -> None:
        """The centre cell is at full brightness regardless of t."""
        eff: RadarScope = RadarScope(
            width=self.ODD_GRID, height=self.ODD_GRID, brightness=100,
        )
        eff.on_start(self.ODD_GRID * self.ODD_GRID)
        centre_idx: int = (self.ODD_GRID // 2) * self.ODD_GRID + (self.ODD_GRID // 2)
        # Sample at several t spread across the rotation; the centre
        # cell's brightness must not depend on the sweep angle.
        for frac in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9):
            out: list = eff.render(
                eff._rotation_period * frac,
                self.ODD_GRID * self.ODD_GRID,
            )
            bri: int = out[centre_idx][2]
            self.assertEqual(
                bri, 65535,
                f"centre cell dimmed to {bri} at frac={frac} — "
                f"CENTRE_R_THRESHOLD branch isn't pulsing the centre",
            )


class TestRenderShape(unittest.TestCase):
    """Output buffer length matches zone_count."""

    def test_returns_zone_count_cells(self) -> None:
        eff: RadarScope = RadarScope()
        eff.on_start(64)
        out: list = eff.render(0.0, 64)
        self.assertEqual(len(out), 64)

    def test_pads_zero_for_oversized_zone_count(self) -> None:
        eff: RadarScope = RadarScope()
        eff.on_start(80)
        out: list = eff.render(0.0, 80)
        for hsbk in out[64:]:
            self.assertEqual(hsbk, (0, 0, 0, 3500))


if __name__ == "__main__":
    unittest.main()
