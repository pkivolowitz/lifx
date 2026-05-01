"""Tests for the conway2d effect.

Covers:

- B3/S23 step rule on the toroidal neighborhood (one classic case).
- Toroidal wrap actually wraps (a blinker at the seam still survives).
- Glider — the only deterministic shipped pattern — is periodic on
  the 8x8 toroid (period 32, c/4 motion).  Pattern 2 (random) is not
  tested for periodicity by design.
- Effect.render dispatch: cut mode produces only 0/full brightness on
  changing cells (verified across a step boundary, not just at t=0);
  dissolve mode produces intermediate values mid-step.
- Rate selection observably drives the step count: at the same
  wall-clock time, ``rate=fast`` fires more steps than ``rate=slow``.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.1"

import random
import unittest

from effects.conway2d import (
    ALLOWED_PATTERNS, ATTRACTOR_LINGER_STEPS, GRID_H, GRID_W,
    MODE_CUT, MODE_DISSOLVE, PATTERN_GLIDER, PATTERN_RANDOM,
    RANDOM_SEED_DENSITY, RATE_FAST, RATE_INTERVAL_SEC,
    RATE_MEDIUM, RATE_SLOW, TOTAL_CELLS,
    Conway2D, _random_seed_grid, _seed_grid, _step_grid,
)
from effects._walkers import HUE_AUTO_SENTINEL, HUE_LEG_DURATION_SEC


# Step interval used in legacy tests that pre-dated the --rate param.
# Equals the medium-default step interval.
STEP_INTERVAL_SEC: float = RATE_INTERVAL_SEC[RATE_MEDIUM]


# Generous upper bound for periodicity search on 8x8.  Glider has the
# longest known period (32) of the four shipped patterns; doubling it
# leaves headroom for any rotation that might hit a longer recurrence.
PERIOD_SEARCH_LIMIT: int = 128


def _grid_from_cells(cells: list[tuple[int, int]]) -> list[bool]:
    """Build a flat 8x8 grid with the given (row, col) cells alive."""
    g: list[bool] = [False] * TOTAL_CELLS
    for r, c in cells:
        g[r * GRID_W + c] = True
    return g


class TestStepRule(unittest.TestCase):
    """One canonical Conway transition — proves B3/S23 + toroidal wrap."""

    def test_blinker_oscillates(self) -> None:
        """A horizontal 3-row blinker becomes vertical, then horizontal again."""
        horiz: list[bool] = _grid_from_cells([(3, 2), (3, 3), (3, 4)])
        vert_expected: list[bool] = _grid_from_cells(
            [(2, 3), (3, 3), (4, 3)]
        )
        step1: list[bool] = _step_grid(horiz)
        self.assertEqual(step1, vert_expected)
        step2: list[bool] = _step_grid(step1)
        self.assertEqual(step2, horiz)

    def test_block_is_still_life(self) -> None:
        """A 2x2 block is stable — _step_grid returns the same grid."""
        block: list[bool] = _grid_from_cells(
            [(2, 2), (2, 3), (3, 2), (3, 3)]
        )
        self.assertEqual(_step_grid(block), block)

    def test_toroidal_wrap_blinker_at_seam(self) -> None:
        """A blinker straddling the row=0/row=7 seam still oscillates."""
        # Vertical blinker spanning rows 7, 0, 1 at col 4 — only valid
        # if neighborhood lookups wrap correctly.
        seam: list[bool] = _grid_from_cells([(7, 4), (0, 4), (1, 4)])
        expected_horiz: list[bool] = _grid_from_cells(
            [(0, 3), (0, 4), (0, 5)]
        )
        self.assertEqual(_step_grid(seam), expected_horiz)


class TestPatternPeriodicity(unittest.TestCase):
    """Glider — the only deterministic shipped pattern — repeats on the toroid.

    Pattern 2 (random) is intentionally not periodicity-tested: random
    seeds are aperiodic by definition and ``_maybe_reseed`` covers the
    deterministic side of pattern 2's behaviour separately.
    """

    def _assert_periodic(self, pattern_id: int) -> int:
        """Run the pattern; return the smallest period > 0 found.

        Tries every rotation (0, 1, 2, 3 quarter-turns) at a fixed
        offset of (0, 0) so the test is deterministic across runs.
        Failure raises an AssertionError with the rotation that broke.
        """
        from effects.conway2d import _rotate_cell, PATTERN_SHAPES
        shapes = PATTERN_SHAPES[pattern_id]
        worst_period: int = 0
        for qturns in range(4):
            rotated: list[tuple[int, int]] = [
                _rotate_cell(r, c, qturns) for r, c in shapes
            ]
            min_r: int = min(r for r, _ in rotated)
            min_c: int = min(c for _, c in rotated)
            seed: list[bool] = _grid_from_cells(
                [(r - min_r, c - min_c) for r, c in rotated]
            )
            grid: list[bool] = list(seed)
            period: int = 0
            for step in range(1, PERIOD_SEARCH_LIMIT + 1):
                grid = _step_grid(grid)
                if grid == seed:
                    period = step
                    break
            self.assertGreater(
                period, 0,
                f"pattern {pattern_id} rotation {qturns} did not "
                f"return to its seed within {PERIOD_SEARCH_LIMIT} "
                f"generations on the 8x8 toroid",
            )
            worst_period = max(worst_period, period)
        return worst_period

    def test_glider_periodic(self) -> None:
        """Glider on 8x8 toroid loops within the search limit."""
        period: int = self._assert_periodic(PATTERN_GLIDER)
        # Glider's signature speed-c/4 motion produces period 32 on 8x8
        # (8 cells in each axis at c/4 = 32 generations).  Any deviation
        # would mean either a step-rule bug or a wrap bug.
        self.assertEqual(period, 32)


class TestSeedPlacement(unittest.TestCase):
    """Glider seeds 5 cells; random seed has density-bounded count."""

    def test_glider_seed_cell_count(self) -> None:
        """Glider always lights exactly its 5 cells regardless of rotation."""
        random.seed(0)
        for _ in range(8):
            grid: list[bool] = _seed_grid(PATTERN_GLIDER)
            self.assertEqual(sum(grid), 5)
            self.assertEqual(len(grid), TOTAL_CELLS)

    def test_random_seed_density(self) -> None:
        """``_random_seed_grid`` averages around RANDOM_SEED_DENSITY."""
        random.seed(0)
        # Average over 200 seeds — variance shrinks fast.  A 5-sigma
        # binomial bound at 64 cells, p=0.35, n=200 is well inside ±3%.
        total: int = sum(sum(_random_seed_grid()) for _ in range(200))
        avg_density: float = total / (200 * TOTAL_CELLS)
        self.assertAlmostEqual(avg_density, RANDOM_SEED_DENSITY, delta=0.03)


class TestEffectRender(unittest.TestCase):
    """End-to-end ``Conway2D.render`` — modes and timing."""

    def test_render_returns_zone_count_cells(self) -> None:
        """Even on a non-64 device the output length equals zone_count."""
        eff: Conway2D = Conway2D()
        eff.on_start(35)
        out: list = eff.render(0.0, 35)
        self.assertEqual(len(out), 35)

    def test_cut_mode_only_full_or_zero(self) -> None:
        """In cut mode every cell is either full brightness or BLACK."""
        eff: Conway2D = Conway2D(mode=MODE_CUT, brightness=100)
        eff.on_start(64)
        # Sample the very first frame — no step has fired yet, so the
        # only live cells are the seed; alpha == 1 (cut), so brightness
        # is full for live cells.
        out: list = eff.render(0.0, 64)
        live: list[int] = [hsbk[2] for hsbk in out if hsbk[2] != 0]
        self.assertTrue(all(b == 65535 for b in live))

    def test_cut_mode_after_step_boundary_still_binary(self) -> None:
        """Across a step boundary, dying/born cells in cut mode snap (no fade).

        The original cut-mode test only sampled t=0 (before any step
        fired), where cut and dissolve are indistinguishable.  This
        forces a step transition (horizontal blinker → vertical) and
        samples mid-way through the next step interval — in cut mode
        the dying cells must be 0 and born cells must be full, with
        no intermediate brightness values anywhere on the grid.
        """
        eff: Conway2D = Conway2D(
            pattern=PATTERN_GLIDER, mode=MODE_CUT, brightness=100,
        )
        eff.on_start(64)
        # Plant a horizontal blinker so the step transition produces
        # both dying cells (at row 3, cols 2 and 4) and born cells
        # (at col 3, rows 2 and 4).
        eff._curr_grid = [False] * 64
        for c in (2, 3, 4):
            eff._curr_grid[3 * GRID_W + c] = True
        eff._prev_grid = list(eff._curr_grid)

        # Drive past the first step boundary, then sample mid-way
        # through the next step interval (where dissolve mode would
        # show partial-brightness cells).
        _ = eff.render(STEP_INTERVAL_SEC + 0.001, 64)  # step fires
        mid_t: float = STEP_INTERVAL_SEC + STEP_INTERVAL_SEC / 2.0
        out: list = eff.render(mid_t, 64)
        partial: list[int] = [
            hsbk[2] for hsbk in out if 0 < hsbk[2] < 65535
        ]
        self.assertEqual(
            partial, [],
            "cut mode produced intermediate-brightness cells "
            "after a step boundary",
        )

    def test_dissolve_mode_produces_intermediate_brightness(self) -> None:
        """A frame mid-step in dissolve mode shows partial brightness."""
        # Pre-seed with a manual horizontal blinker so the next step
        # produces both dying and newly-born cells (vertical blinker)
        # and the dissolve has both fade-in and fade-out cells to show.
        random.seed(1)
        eff: Conway2D = Conway2D(
            pattern=PATTERN_GLIDER, mode=MODE_DISSOLVE, brightness=100,
        )
        eff.on_start(64)
        eff._curr_grid = [False] * 64
        # Horizontal blinker at row 3, cols 2-4.
        for c in (2, 3, 4):
            eff._curr_grid[3 * GRID_W + c] = True
        eff._prev_grid = list(eff._curr_grid)

        # Drive past the first step so prev/curr differ, then sample
        # mid-way through the dissolve interval.
        _ = eff.render(STEP_INTERVAL_SEC + 0.001, 64)  # step fires
        mid_t: float = STEP_INTERVAL_SEC + STEP_INTERVAL_SEC / 2.0
        mid: list = eff.render(mid_t, 64)
        partial: list[int] = [
            hsbk[2] for hsbk in mid if 0 < hsbk[2] < 65535
        ]
        # At least one cell should be in the partial-brightness band
        # mid-dissolve (both dying and being-born cells live there).
        self.assertGreater(
            len(partial), 0,
            "dissolve mode should produce intermediate-brightness cells",
        )


class TestRandomReseed(unittest.TestCase):
    """Pattern 2 — auto-reseed on death and on attractor detection."""

    def test_all_dead_triggers_reseed(self) -> None:
        """A grid that dies out is replaced on the very next step."""
        eff: Conway2D = Conway2D(pattern=PATTERN_RANDOM, mode=MODE_CUT)
        eff.on_start(64)
        # Force the current grid to all-dead and step.
        eff._curr_grid = [False] * 64
        eff._prev_grid = [False] * 64
        random.seed(7)
        # One step past the next boundary triggers _maybe_reseed.
        _ = eff.render(eff._next_step_t + 0.001, 64)
        self.assertGreater(
            sum(eff._curr_grid), 0,
            "all-dead grid should have been reseeded with live cells",
        )

    def test_still_life_attractor_triggers_reseed_after_linger(self) -> None:
        """A 2x2 block (period 1 still life) reseeds after the linger window."""
        eff: Conway2D = Conway2D(
            pattern=PATTERN_RANDOM, mode=MODE_CUT, rate=RATE_FAST,
        )
        eff.on_start(64)
        # Plant a stable 2x2 block at (3,3)-(4,4) — provably period 1.
        block_indices: set[int] = {
            3 * GRID_W + 3, 3 * GRID_W + 4,
            4 * GRID_W + 3, 4 * GRID_W + 4,
        }
        new_grid: list[bool] = [i in block_indices for i in range(64)]
        eff._curr_grid = list(new_grid)
        eff._prev_grid = list(new_grid)
        eff._grid_history = []
        eff._attractor_detected_at_step = None

        random.seed(11)
        # Step the engine forward by enough generations to detect the
        # cycle (1 step) plus the linger window plus 1 buffer step.
        step_dt: float = eff._step_interval
        # First step: history empty; populates with the block state.
        _ = eff.render(eff._next_step_t + 0.001, 64)
        # Second step: new grid matches history → detection fires.
        _ = eff.render(eff._next_step_t + 0.001, 64)
        self.assertIsNotNone(
            eff._attractor_detected_at_step,
            "attractor should be detected on the second step",
        )
        # Run linger steps; on the linger-completion step the reseed
        # fires and the grid changes.
        for _ in range(ATTRACTOR_LINGER_STEPS + 1):
            _ = eff.render(eff._next_step_t + 0.001, 64)

        live_indices: set[int] = {
            i for i, alive in enumerate(eff._curr_grid) if alive
        }
        self.assertNotEqual(
            live_indices, block_indices,
            "after linger the still-life block should have been reseeded",
        )

    def test_glider_pattern_never_reseeds(self) -> None:
        """Pattern 1 (glider) skips the reseed branch — history stays empty."""
        eff: Conway2D = Conway2D(
            pattern=PATTERN_GLIDER, mode=MODE_CUT, rate=RATE_FAST,
        )
        eff.on_start(64)
        # Drive past several steps; reseed branch should never run.
        for _ in range(20):
            _ = eff.render(eff._next_step_t + 0.001, 64)
        self.assertEqual(eff._grid_history, [])
        self.assertIsNone(eff._attractor_detected_at_step)
        # Glider stays present — never went all-dead.
        self.assertEqual(sum(eff._curr_grid), 5)


class TestRateParam(unittest.TestCase):
    """``--rate`` selects the step interval used for both ticks and dissolve.

    Named ``--rate`` (not ``--speed``) because matrix_rain owns the
    float-typed ``--speed`` flag at the CLI dedup layer; conway2d's
    string-with-choices version would never be reached.
    """

    def _step_count_after(self, rate: str, t: float) -> int:
        """Run the engine to wall-clock *t* and return the step counter."""
        eff: Conway2D = Conway2D(rate=rate, pattern=PATTERN_GLIDER)
        eff.on_start(64)
        eff.render(t, 64)
        return eff._step_index

    def test_rate_drives_step_count(self) -> None:
        """At the same wall-clock t, faster rate fires more generations.

        Replaces a pair of tautological tests that just round-tripped
        ``RATE_INTERVAL_SEC`` between the dict and the effect's cached
        copy of it.  Here we drive the engine for one wall-clock
        second at each rate and assert the observable step count
        matches the rate's intended cadence (fast=0.25s/gen → 4 steps,
        medium=0.5s/gen → 2 steps, slow=1.0s/gen → 1 step).  Catches
        any regression in either the ``RATE_INTERVAL_SEC`` table OR
        the catch-up loop that consumes it.
        """
        wall_clock_s: float = 1.0
        fast_steps: int = self._step_count_after(RATE_FAST, wall_clock_s)
        med_steps: int = self._step_count_after(RATE_MEDIUM, wall_clock_s)
        slow_steps: int = self._step_count_after(RATE_SLOW, wall_clock_s)
        self.assertGreater(fast_steps, med_steps)
        self.assertGreater(med_steps, slow_steps)
        self.assertEqual(fast_steps, 4)
        self.assertEqual(med_steps, 2)
        self.assertEqual(slow_steps, 1)

    def test_rate_invalid_rejected(self) -> None:
        """Param.choices catches typos at construction time."""
        with self.assertRaises(ValueError):
            Conway2D(rate="ludicrous")


class TestHueAutoCycle(unittest.TestCase):
    """Sentinel ``--hue -1`` triggers brownian-walk OkLab hue cycling."""

    def test_default_is_auto_mode(self) -> None:
        """Omitting --hue leaves the default sentinel → auto mode."""
        eff: Conway2D = Conway2D()
        eff.on_start(64)
        self.assertIsNotNone(eff._hue_walker)
        self.assertEqual(int(eff.hue), HUE_AUTO_SENTINEL)

    def test_explicit_hue_disables_auto(self) -> None:
        """Any non-negative hue pins the cells to that hue."""
        eff: Conway2D = Conway2D(hue=200)
        eff.on_start(64)
        self.assertIsNone(eff._hue_walker)
        self.assertTrue(hasattr(eff, "_static_hue_u16"))

    def test_auto_mode_rotates_hue_over_time(self) -> None:
        """A frame far past one leg sees a different hue than the seed."""
        random.seed(7)
        eff: Conway2D = Conway2D(brightness=100)
        eff.on_start(64)
        # Sample at t=0 and t=2 full legs later — hue must have changed.
        out_t0: list = eff.render(0.0, 64)
        out_late: list = eff.render(HUE_LEG_DURATION_SEC * 2.5, 64)
        # Take the hue from any live cell (all share the global hue).
        live_t0: list[int] = [hsbk[0] for hsbk in out_t0 if hsbk[2] > 0]
        live_late: list[int] = [hsbk[0] for hsbk in out_late if hsbk[2] > 0]
        self.assertGreater(len(live_t0), 0)
        self.assertGreater(len(live_late), 0)
        # Brownian walk after two-and-a-half legs of ±30° will not, with
        # probability ~1, return to exactly the seed hue.  (Equality
        # would imply zero net drift across multiple legs of uniform
        # random deltas — astronomically unlikely with the fixed seed.)
        self.assertNotEqual(live_t0[0], live_late[0])

    def test_manual_hue_constant_over_time(self) -> None:
        """Static --hue is invariant across frames."""
        eff: Conway2D = Conway2D(hue=42, brightness=100, mode=MODE_CUT)
        eff.on_start(64)
        out_a: list = eff.render(0.0, 64)
        out_b: list = eff.render(HUE_LEG_DURATION_SEC * 5.0, 64)
        live_a: list[int] = [hsbk[0] for hsbk in out_a if hsbk[2] > 0]
        live_b: list[int] = [hsbk[0] for hsbk in out_b if hsbk[2] > 0]
        self.assertEqual(set(live_a), set(live_b))
        self.assertEqual(len(set(live_a)), 1)  # single shared hue


if __name__ == "__main__":
    unittest.main()
