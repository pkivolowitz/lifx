"""Conway's Game of Life — 8x8 toroidal cellular automaton.

Steps the classic B3/S23 rule on a fixed 8x8 toroidal grid.  Every
edge wraps to its opposite (top↔bottom, left↔right) so a glider,
blinker, toad, or beacon placed anywhere on the grid recurs forever
— there is no boundary to absorb it.

Patterns:

  1 Glider — 5 cells; moves diagonally; period 32 on 8x8 toroid.
            Random rotation + toroidal offset at start; loops
            forever, never reseeds.
  2 Random — fills the grid at ~35% live-cell density and steps
            until the system either dies out completely or settles
            into an attractor (still life, blinker, beacon, etc.).
            On detection the user gets a few extra generations to
            see the attractor, then the grid is reseeded.  In
            dissolve mode the reseed cross-fades from the dying
            attractor to the new random field.

Transitions between generations:

  --mode cut       — snap to the next state at the step boundary.
  --mode dissolve  — linearly blend brightness between the previous
                     and next states across the entire step interval.

Step rate (``--rate``):

  slow    1.0 s per generation
  medium  0.5 s per generation  (default)
  fast    0.25 s per generation

(Named ``--rate`` rather than ``--speed`` because matrix_rain already
owns the float-typed ``--speed`` flag at the CLI level — see
``glowup.py``'s per-effect param dedup.  ``--rate`` is conway2d's
private namespace.)

Hue:

  --hue <0..360>   pin all live cells to that hue.
  --hue -1         (default — equivalent to omitting the flag) drift
                   the global hue via a brownian walk in OkLab.  Each
                   leg picks the next hue at current ± d, where d is
                   uniform in [20°, 50°] (sign random) so the walk
                   never stalls — wrapped to [0, 360).  Successive
                   legs are interpolated through OkLab over a fixed
                   leg duration so the path stays perceptually
                   continuous and avoids muddy mid-transition colors.
                   All live cells share the current global hue.

Hardcoded to an 8x8 grid because the spec is "Conway on an 8x8
toroid".  On a matrix device whose ``zone_count`` differs from 64
the first ``zone_count`` cells (row-major) are emitted; oversize
devices are zero-padded.  The fixture mask layer (see
``transport.py``) handles dead-cell and uplight blackouts on the
SuperColor Ceiling, so this effect emits the full 8x8 buffer.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.2"

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
    RATE_FAST, RATE_MEDIUM, RATE_SLOW,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Toroid geometry — fixed by spec (Conway on an 8x8 toroid).
GRID_W: int = 8
GRID_H: int = 8
TOTAL_CELLS: int = GRID_W * GRID_H

# Black HSBK — emitted for dead cells.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Default peak brightness (percent) — visible without overpowering an
# unlit room.  Same default as matrix_rain.
DEFAULT_BRIGHTNESS_PCT: int = 80

# Step-interval per --rate choice.  Slow enough to follow an oscillator
# at slow=1.0 s/gen; fast enough to make a glider feel mobile at
# fast=0.25 s/gen.  Default ``medium``.  Rate vocabulary itself
# (``slow``/``medium``/``fast``) is shared in :mod:`._walkers`.
RATE_INTERVAL_SEC: dict[str, float] = {
    RATE_SLOW:   1.0,
    RATE_MEDIUM: 0.5,
    RATE_FAST:   0.25,
}

# Transition modes (--mode value).  Strings to keep the CLI literal.
MODE_CUT: str = "cut"
MODE_DISSOLVE: str = "dissolve"
ALLOWED_MODES: list[str] = [MODE_CUT, MODE_DISSOLVE]

# Pattern selector values (--pattern value).  Stationary oscillators
# (blinker, toad, beacon) were considered for the original 4-pattern
# set but rejected: on a 64-cell ceiling they read as "boring blinking
# corner" — the point of putting Conway on a wall is to see motion.
# Glider provides motion; random seed provides chaotic transient
# activity that's never twice the same.
PATTERN_GLIDER: int = 1
PATTERN_RANDOM: int = 2
ALLOWED_PATTERNS: list[int] = [PATTERN_GLIDER, PATTERN_RANDOM]

# Glider canonical shape (top-left origin).  Rotation + toroidal
# offset applied at on_start.  Other shipped patterns wouldn't be
# constants — pattern 2 is generated procedurally.
_SHAPE_GLIDER: tuple[tuple[int, int], ...] = (
    (0, 1), (1, 2), (2, 0), (2, 1), (2, 2),
)

PATTERN_SHAPES: dict[int, tuple[tuple[int, int], ...]] = {
    PATTERN_GLIDER: _SHAPE_GLIDER,
}

# Quarter-turn rotation count choices (0..3 = 0°, 90°, 180°, 270° CCW).
_ROTATION_QTURNS: tuple[int, ...] = (0, 1, 2, 3)

# Random-seed parameters for pattern 2.
# RANDOM_SEED_DENSITY: fraction of cells alive at seed time.  ~35% is
#   the empirical sweet spot on small grids — dense enough to produce
#   interesting transient activity, sparse enough that things don't
#   immediately die out from over-crowding.
# ATTRACTOR_HISTORY_DEPTH: how many recent grid states to retain for
#   cycle detection.  32 covers every short-period attractor an 8x8
#   toroid can settle into in practice (still life=1, blinker/toad/
#   beacon=2; longer periods exist but are rare on this size grid).
# ATTRACTOR_LINGER_STEPS: extra generations to run after detecting an
#   attractor, so the viewer sees the oscillator pulse a few times
#   before the reseed cross-fade kicks in.
RANDOM_SEED_DENSITY: float = 0.35
ATTRACTOR_HISTORY_DEPTH: int = 32
ATTRACTOR_LINGER_STEPS: int = 6


def _rotate_cell(r: int, c: int, qturns: int) -> tuple[int, int]:
    """Rotate ``(r, c)`` by ``qturns × 90°`` counter-clockwise about the origin.

    Used when randomly orienting a seed pattern on the toroid.  The
    coordinates may go negative — the caller re-anchors to non-negative
    before placement.
    """
    rr: int = r
    cc: int = c
    for _ in range(qturns % 4):
        rr, cc = -cc, rr
    return rr, cc


def _random_seed_grid() -> list[bool]:
    """Return an 8x8 grid with each cell alive with probability density.

    Used by pattern 2 (and on-the-fly reseeding when an attractor is
    reached or the grid dies out).  Density is the module-level
    ``RANDOM_SEED_DENSITY`` constant — see its definition for why
    ~35% is the chosen value.
    """
    return [
        random.random() < RANDOM_SEED_DENSITY for _ in range(TOTAL_CELLS)
    ]


def _seed_grid(pattern_id: int) -> list[bool]:
    """Build a fresh 8x8 grid for *pattern_id*.

    Glider gets the canonical 5-cell shape at a random quarter-turn
    rotation and toroidal offset (toroidal wrap means every offset is
    equally valid — no edge for the pattern to fall off).  Random
    pattern delegates to :func:`_random_seed_grid`.

    Raises:
        KeyError: If *pattern_id* is not a known pattern.
    """
    if pattern_id == PATTERN_RANDOM:
        return _random_seed_grid()
    raw_cells: tuple[tuple[int, int], ...] = PATTERN_SHAPES[pattern_id]
    qturns: int = random.choice(_ROTATION_QTURNS)
    rotated: list[tuple[int, int]] = [
        _rotate_cell(r, c, qturns) for r, c in raw_cells
    ]
    # Re-anchor rotated coordinates to non-negative so the offset math
    # is simple modular arithmetic rather than two-sided clamping.
    min_r: int = min(r for r, _ in rotated)
    min_c: int = min(c for _, c in rotated)
    normalized: list[tuple[int, int]] = [
        (r - min_r, c - min_c) for r, c in rotated
    ]
    off_r: int = random.randrange(GRID_H)
    off_c: int = random.randrange(GRID_W)
    grid: list[bool] = [False] * TOTAL_CELLS
    for r, c in normalized:
        rr: int = (r + off_r) % GRID_H
        cc: int = (c + off_c) % GRID_W
        grid[rr * GRID_W + cc] = True
    return grid


def _step_grid(grid: list[bool]) -> list[bool]:
    """Apply one Conway B3/S23 step with toroidal neighborhood wrap."""
    out: list[bool] = [False] * TOTAL_CELLS
    for r in range(GRID_H):
        for c in range(GRID_W):
            n: int = 0
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nr: int = (r + dr) % GRID_H
                    nc: int = (c + dc) % GRID_W
                    if grid[nr * GRID_W + nc]:
                        n += 1
            idx: int = r * GRID_W + c
            alive: bool = grid[idx]
            # B3/S23: dead cell with 3 live neighbors is born; live
            # cell with 2 or 3 stays alive; everything else dies.
            if alive and n in (2, 3):
                out[idx] = True
            elif (not alive) and n == 3:
                out[idx] = True
    return out


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------


class Conway2D(Effect):
    """Conway's Game of Life on an 8x8 toroidal grid.

    Two patterns ship: ``glider`` (canonical 5-cell shape, dropped at
    random rotation + toroidal offset; loops forever on the toroid)
    and ``random`` (~35% live-cell density seed; auto-reseeds when the
    grid dies out or settles into an attractor).  Stationary
    oscillators (blinker/toad/beacon) were considered and rejected —
    on a 64-cell ceiling they read as a static blinking corner.
    Transitions between generations are either an instant ``cut`` or a
    smooth ``dissolve`` (brightness blend over the full step interval).
    """

    name: str = "conway2d"
    description: str = "Conway's Game of Life on an 8x8 toroidal grid"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    pattern = Param(
        PATTERN_GLIDER, choices=ALLOWED_PATTERNS,
        description=(
            "Seed pattern.  1=glider (loops on the toroid forever); "
            "2=random seed that auto-reseeds when it dies or hits an "
            "attractor."
        ),
    )
    mode = Param(
        MODE_DISSOLVE, choices=ALLOWED_MODES,
        description=(
            "Transition between generations: 'cut' snaps; 'dissolve' "
            "linearly blends brightness across the step interval."
        ),
    )
    rate = Param(
        RATE_MEDIUM, choices=ALLOWED_RATES,
        description=(
            "Step rate: slow=1.0s, medium=0.5s, fast=0.25s per generation."
        ),
    )
    # Default sentinel ``-1`` means "no hue given" → brownian-walk auto
    # cycle in OkLab.  The min is ``-1`` so a user can request the auto
    # mode explicitly with ``--hue -1`` if they want.
    hue = Param(
        HUE_AUTO_SENTINEL, min=HUE_AUTO_SENTINEL, max=360,
        description=(
            "Live-cell hue in degrees (0=red, 120=green, 240=blue).  "
            "Omit (or pass -1) to drift the global hue via brownian "
            "walk in OkLab — uniform ±30° per leg, ~12 s per leg."
        ),
    )
    brightness = Param(
        DEFAULT_BRIGHTNESS_PCT, min=1, max=100,
        description="Live-cell peak brightness (percent)",
    )

    def on_start(self, zone_count: int) -> None:
        """Seed the grid, choose step interval, and prime the hue walker.

        Args:
            zone_count: Total pixels on the target device (informational
                — the grid is fixed at 8x8 regardless).
        """
        self._pattern_id: int = int(self.pattern)
        self._curr_grid: list[bool] = _seed_grid(self._pattern_id)
        # Previous generation starts identical to current so the first
        # dissolve frame doesn't ghost-fade nonexistent cells.
        self._prev_grid: list[bool] = list(self._curr_grid)

        # Reseed bookkeeping for pattern 2.  History is a list of
        # frozen grid tuples (cheap O(64) compare per frame).  Glider
        # never reseeds, so for pattern 1 these stay empty/None and
        # the reseed branch is skipped.
        self._step_index: int = 0
        self._grid_history: list[tuple[bool, ...]] = []
        self._attractor_detected_at_step: Optional[int] = None

        # Step interval is a function of --rate; resolved once at
        # start so a runtime ``set_params`` change wouldn't break the
        # already-ticking schedule (re-call on_start to apply).
        self._step_interval: float = RATE_INTERVAL_SEC[str(self.rate)]
        self._step_anchor_t: float = 0.0
        self._next_step_t: float = self._step_interval

        # Hue source.  Auto mode delegates to the shared HueWalker;
        # manual mode pins the hue from --hue and the walker stays
        # ``None`` so render branches on it.
        if int(self.hue) < 0:
            self._hue_walker: Optional[HueWalker] = HueWalker()
            self._static_hue_u16: int = 0  # unused when walker present
        else:
            self._hue_walker = None
            self._static_hue_u16 = hue_to_u16(float(self.hue))

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame.

        Steps the grid as many times as needed to catch up to *t*
        (a slow render loop won't desync the simulation), then writes
        the live cells with brightness scaled by the current dissolve
        blend factor.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels on the target device.

        Returns:
            ``zone_count`` HSBK tuples in row-major order.  Cells past
            the 8x8 grid are emitted black.
        """
        # Guard: render() called before on_start() (e.g., in tests).
        if not hasattr(self, "_curr_grid"):
            return [BLACK] * zone_count

        # Advance the Conway simulation to the current wall-clock time.
        # A while-loop catches the simulation up if multiple steps were
        # missed (slow frames, paused tab, etc.).
        while t >= self._next_step_t:
            self._prev_grid = self._curr_grid
            self._curr_grid = _step_grid(self._curr_grid)
            self._step_anchor_t = self._next_step_t
            self._next_step_t += self._step_interval
            self._step_index += 1
            # Pattern 2 only: detect death / attractor and reseed.
            # Glider on the toroid is provably periodic so this branch
            # is skipped for pattern 1 to avoid pointless work.
            if self._pattern_id == PATTERN_RANDOM:
                self._maybe_reseed()

        # Resolve the live-cell hue for this frame.  Auto mode samples
        # the shared OkLab walker; manual mode uses the static hue
        # snapshotted at on_start.
        if self._hue_walker is not None:
            hue_u16: int = self._hue_walker.hue_u16(t)
        else:
            hue_u16 = self._static_hue_u16

        bri_max: int = pct_to_u16(int(self.brightness))
        mode_str: str = str(self.mode)

        # Dissolve blend factor: 0 = fully prev, 1 = fully curr.  Cut
        # mode jumps to 1 immediately at every step boundary.
        if mode_str == MODE_CUT:
            alpha: float = 1.0
        else:
            phase: float = (t - self._step_anchor_t) / self._step_interval
            alpha = max(0.0, min(1.0, phase))

        colors: list[HSBK] = [BLACK] * zone_count
        # Iterate only as many cells as both the grid and the device
        # can hold — same-size for a Ceiling (zone_count=64); smaller
        # devices truncate, larger devices stay padded with BLACK.
        cells_to_emit: int = min(TOTAL_CELLS, zone_count)
        for i in range(cells_to_emit):
            was_alive: bool = self._prev_grid[i]
            is_alive: bool = self._curr_grid[i]
            if was_alive and is_alive:
                bri: int = bri_max
            elif (not was_alive) and (not is_alive):
                continue  # leave BLACK
            elif was_alive and not is_alive:
                # Cell dying — fade out across the dissolve interval.
                bri = int(bri_max * (1.0 - alpha))
            else:
                # Cell being born — fade in across the dissolve interval.
                bri = int(bri_max * alpha)
            if bri > 0:
                colors[i] = (hue_u16, HSBK_MAX, bri, KELVIN_DEFAULT)
        return colors

    def _maybe_reseed(self) -> None:
        """Pattern-2 hook: detect death or attractor; reseed when needed.

        Order matters: an all-dead grid is reseeded immediately (no
        sense lingering on a black ceiling), and then attractor
        detection runs against the live grid.  Detection works by
        keeping a small ring of recent grid snapshots and checking
        whether the new step's grid has been seen before — which is
        the literal definition of having entered an attractor (still
        life: history-1 match; oscillator: history-N match for period
        N <= ATTRACTOR_HISTORY_DEPTH).
        """
        # Death — reseed immediately.  _prev_grid still holds the last
        # live state, so dissolve cross-fades from "last embers" to
        # the new random field.
        if not any(self._curr_grid):
            self._do_reseed()
            return

        curr_state: tuple[bool, ...] = tuple(self._curr_grid)
        if (
            self._attractor_detected_at_step is None
            and curr_state in self._grid_history
        ):
            self._attractor_detected_at_step = self._step_index

        self._grid_history.append(curr_state)
        if len(self._grid_history) > ATTRACTOR_HISTORY_DEPTH:
            # Drop oldest — bounded ring prevents unbounded growth on
            # long-running random patterns that never actually settle.
            self._grid_history.pop(0)

        # Linger after detection so the viewer sees the attractor a
        # few times, then reseed.
        if self._attractor_detected_at_step is not None:
            steps_since: int = (
                self._step_index - self._attractor_detected_at_step
            )
            if steps_since >= ATTRACTOR_LINGER_STEPS:
                self._do_reseed()

    def _do_reseed(self) -> None:
        """Replace the current grid with a fresh random seed.

        Leaves ``_prev_grid`` untouched so the next dissolve frame
        cross-fades from the dying/stable old grid into the new seed.
        Resets attractor-detection state so the next attractor on the
        new field is judged against a clean slate.
        """
        self._curr_grid = _random_seed_grid()
        self._grid_history = []
        self._attractor_detected_at_step = None

    def period(self) -> Optional[float]:
        """Return ``None`` — pattern periods differ; recording isn't supported.

        Each pattern has its own period (glider 32 generations on 8x8
        toroid; oscillators 2 generations) and the random rotation/
        offset further changes when a recording would line up.  The
        recorder shouldn't try to capture a single loop; ``None`` tells
        it so.
        """
        return None
