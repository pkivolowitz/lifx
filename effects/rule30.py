"""Wolfram elementary 1-D cellular automaton effect.

Each zone on the strip is one cell.  Each frame the simulation advances
by one or more generations using the selected Wolfram elementary CA rule.

Rule 30  — chaotic / pseudo-random; great for organic, unpredictable animation.
Rule 90  — Sierpiński fractal triangle; self-similar nested pattern.
Rule 110 — Turing-complete; rich structured behaviour.

The default seed is a single live cell in the centre.  Boundary conditions
are periodic (the strip wraps left-to-right), so no cells are ever "stuck"
at the edges.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import random

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Bits in one elementary CA neighbourhood (left, center, right).
NEIGHBOURHOOD_BITS: int = 3

# Total number of Wolfram elementary CA rules.
RULE_COUNT: int = 256

# Number of distinct neighbourhood patterns (2^3).
PATTERN_COUNT: int = 8

# Seed-mode identifiers.
SEED_CENTER: int = 0   # one live cell in the centre
SEED_RANDOM: int = 1   # random initial state
SEED_ALL: int    = 2   # every cell alive

# Sentinel indicating the rule table has not yet been built.
RULE_UNINITIALISED: int = -1


class Rule30(Effect):
    """Wolfram elementary 1-D cellular automaton on the zone strip.

    Each zone is a cell; live cells are shown in the configured colour,
    dead cells are dim or dark.  The CA rule is applied once per
    generation; generation rate is set by *speed*.

    Notable rules
    -------------
    * **30**  — Chaotic.  Produces visually random patterns from a single
      seed cell; never settles into a repeating cycle on large strips.
    * **90**  — Sierpiński triangle.  Self-similar fractal; beautiful.
    * **110** — Turing-complete.  Complex but structured glider-like behaviour.
    * **184** — Traffic flow model; waves of live cells propagate to the right.
    """

    name: str = "rule30"
    description: str = (
        "Wolfram 1-D cellular automaton — default Rule 30 (chaotic organic animation)"
    )

    # ------------------------------------------------------------------
    # Tunable parameters
    # ------------------------------------------------------------------

    speed = Param(
        8.0, min=0.5, max=120.0,
        description="Generations per second (higher = faster evolution)",
    )
    rule = Param(
        30, min=0, max=255,
        description="Wolfram elementary CA rule number (30=chaotic, 90=fractal, 110=complex)",
    )
    hue = Param(
        200.0, min=0.0, max=360.0,
        description="Live-cell hue in degrees (0=red, 120=green, 200=teal, 240=blue)",
    )
    brightness = Param(
        100, min=1, max=100,
        description="Live-cell brightness as percent",
    )
    bg = Param(
        0, min=0, max=30,
        description="Dead-cell background brightness as percent (0 = fully off)",
    )
    kelvin = Param(
        KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
        description="Color temperature in Kelvin",
    )
    seed = Param(
        SEED_CENTER, min=0, max=2,
        description="Initial seed: 0=single centre cell, 1=random, 2=all alive",
    )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def period(self) -> None:
        """Cellular automata are aperiodic — no loopable cycle."""
        return None

    def __init__(self, **overrides: dict) -> None:
        """Initialise with default params, applying any *overrides*.

        Args:
            **overrides: Parameter names mapped to override values.
        """
        super().__init__(**overrides)

        # CA state: list of 0/1 ints, one per zone.
        self._state: list[int] = []

        # How many generations have elapsed since the effect started.
        self._generation: int = 0

        # Lookup table: index = 3-bit neighbourhood, value = next cell state.
        self._rule_table: list[int] = []

        # Track which rule the table was built for; rebuild if rule param changes.
        self._built_rule: int = RULE_UNINITIALISED

    def on_start(self, zone_count: int) -> None:
        """Seed the initial CA state for *zone_count* cells.

        Args:
            zone_count: Number of zones on the target device.
        """
        self._generation = 0
        self._state = self._make_seed(zone_count)
        self._ensure_rule_table()

    def on_stop(self) -> None:
        """Reset state so a restart always begins from generation 0."""
        self._state = []
        self._generation = 0

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one animation frame of the CA.

        Advances the simulation to the generation that corresponds to
        elapsed time *t*, then maps live/dead cells to HSBK values.

        Args:
            t:          Seconds elapsed since the effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        # Guard: on_start may not have been called (e.g. first render).
        if len(self._state) != zone_count:
            self.on_start(zone_count)

        # Rebuild the rule table if the rule param changed at runtime.
        self._ensure_rule_table()

        # Advance the CA to the generation that matches elapsed time.
        target_gen: int = int(t * self.speed)
        while self._generation < target_gen:
            self._step()

        # Map cell states to HSBK colours.
        alive_hue: int = hue_to_u16(self.hue)
        alive_bri: int = pct_to_u16(self.brightness)
        dead_bri:  int = pct_to_u16(self.bg)

        colors: list[HSBK] = []
        for cell in self._state:
            if cell:
                colors.append((alive_hue, HSBK_MAX, alive_bri, self.kelvin))
            else:
                # Dead cells share the hue so a non-zero bg looks like a dim glow.
                colors.append((alive_hue, HSBK_MAX, dead_bri, self.kelvin))

        return colors

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_seed(self, zone_count: int) -> list[int]:
        """Create the initial cell array for *zone_count* cells.

        Args:
            zone_count: Number of cells.

        Returns:
            A list of 0/1 ints representing the initial generation.
        """
        seed_mode: int = int(self.seed)
        if seed_mode == SEED_RANDOM:
            return [random.randint(0, 1) for _ in range(zone_count)]
        if seed_mode == SEED_ALL:
            return [1] * zone_count
        # SEED_CENTER (default): single live cell in the middle.
        state: list[int] = [0] * zone_count
        state[zone_count // 2] = 1
        return state

    def _ensure_rule_table(self) -> None:
        """Rebuild the lookup table if the current rule param has changed."""
        rule_int: int = int(self.rule)
        if rule_int == self._built_rule:
            return  # already current

        # Each of the 8 possible (L, C, R) neighbourhoods maps to one bit of
        # the rule number.  Neighbourhood encoded as (L<<2 | C<<1 | R).
        self._rule_table = [
            (rule_int >> i) & 1
            for i in range(PATTERN_COUNT)
        ]
        self._built_rule = rule_int

    def _step(self) -> None:
        """Advance the CA by one generation with periodic boundary conditions.

        The strip is treated as a ring: the leftmost cell's left neighbour
        is the rightmost cell and vice versa.  This prevents edge artefacts
        and keeps all cells equally active.
        """
        n: int = len(self._state)
        new_state: list[int] = [0] * n

        for i in range(n):
            # Wrap around the strip edges.
            left:   int = self._state[(i - 1) % n]
            center: int = self._state[i]
            right:  int = self._state[(i + 1) % n]

            # Pack the neighbourhood into an index (Wolfram's standard encoding).
            neighbourhood: int = (left << 2) | (center << 1) | right
            new_state[i] = self._rule_table[neighbourhood]

        self._state = new_state
        self._generation += 1
