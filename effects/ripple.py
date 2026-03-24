"""Ripple tank effect — raindrops on a 1D water surface.

Simulates a one-dimensional wave equation with damping.  Drops fall at
random positions, each injecting a sharp impulse into the surface.
Wavefronts propagate outward in both directions at the configured speed,
reflect off the strip endpoints, and interfere with one another.

The wave equation (discretized, with damping):

    u(x, t+dt) = 2·u(x,t) - u(x,t-dt)
                 + c²·dt²·(u(x-1,t) - 2·u(x,t) + u(x+1,t))/dx²
                 - damping·(u(x,t) - u(x,t-dt))

Displacement maps to brightness (|u| → bright) and color (positive
displacement → hue1, negative → hue2).  The result looks like rain
falling on still water seen from above.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
import os
import random
import sys
from typing import Optional

from . import (
    DEVICE_TYPE_STRIP,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# Import colorspace module from project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from colorspace import lerp_color

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Physics timestep for the wave equation (seconds).
# Smaller = more accurate but more CPU.  60 Hz is plenty for LED animation.
PHYSICS_DT: float = 1.0 / 60.0

# Maximum simulation steps per render call (prevents runaway on lag spikes).
MAX_STEPS_PER_FRAME: int = 10

# Impulse amplitude when a drop hits the surface.
DROP_IMPULSE: float = 1.0

# Floor brightness to avoid LIFX flicker near true black.
FLOOR_FRAC: float = 0.02

# Default zones per bulb for polychrome-aware rendering.
DEFAULT_ZPB: int = 1


class Ripple(Effect):
    """Ripple tank — raindrops on a 1D water surface.

    Random drops hit the surface, launching wavefronts that propagate,
    reflect off the ends, and interfere.  Displacement maps to color
    and brightness — the result looks like rain on still water.
    """

    name: str = "ripple"
    description: str = "Raindrops on water — wavefronts propagate, reflect, and interfere"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_STRIP})

    speed = Param(30.0, min=5.0, max=100.0,
                  description="Wave propagation speed in zones per second")
    damping = Param(0.03, min=0.001, max=0.2,
                    description="Wave damping factor (higher = faster fade)")
    drop_rate = Param(1.5, min=0.1, max=10.0,
                      description="Average drops per second")
    hue1 = Param(200.0, min=0.0, max=360.0,
                 description="Color for positive displacement (degrees)")
    hue2 = Param(330.0, min=0.0, max=360.0,
                 description="Color for negative displacement (degrees)")
    saturation = Param(100, min=0, max=100,
                       description="Wave color saturation percent")
    brightness = Param(100, min=0, max=100,
                       description="Peak brightness percent")
    zones_per_bulb = Param(DEFAULT_ZPB, min=1, max=16,
                           description="Zones per physical bulb (3 for string lights)")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, **overrides: dict) -> None:
        """Initialize wave simulation state.

        Args:
            **overrides: Parameter names mapped to override values.
        """
        super().__init__(**overrides)
        # Wave equation needs two previous time steps.
        self._u_curr: list[float] = []     # displacement at current step
        self._u_prev: list[float] = []     # displacement at previous step
        self._sim_time: float = 0.0        # accumulated simulation time
        self._last_t: Optional[float] = None
        self._next_drop_t: float = 0.0
        self._n_cells: int = 0

    def on_start(self, zone_count: int) -> None:
        """Reset simulation when effect becomes active.

        Args:
            zone_count: Number of zones on the target device.
        """
        zpb: int = self.zones_per_bulb
        self._n_cells = max(1, zone_count // zpb)
        self._u_curr = [0.0] * self._n_cells
        self._u_prev = [0.0] * self._n_cells
        self._sim_time = 0.0
        self._last_t = None
        self._next_drop_t = 0.0

    def period(self) -> None:
        """Ripple is stochastic — no loopable period."""
        return None

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------

    def _step(self) -> None:
        """Advance the wave equation by one timestep.

        Uses the standard second-order finite difference discretization
        of the 1D wave equation with first-order damping.  Boundary
        conditions are reflective (fixed endpoints, u=0 at boundaries).
        """
        n: int = self._n_cells
        if n < 3:
            return

        # Wave speed in cells per second → Courant number.
        c: float = self.speed
        dt: float = PHYSICS_DT
        dx: float = 1.0  # one cell = one spatial unit
        c2dt2_dx2: float = (c * dt / dx) ** 2

        # Clamp the Courant number for numerical stability.
        # CFL condition: c·dt/dx ≤ 1.
        if c2dt2_dx2 > 1.0:
            c2dt2_dx2 = 1.0

        damp: float = self.damping

        u_next: list[float] = [0.0] * n
        for i in range(1, n - 1):
            # Second-order central difference in space.
            laplacian: float = self._u_curr[i - 1] - 2.0 * self._u_curr[i] + self._u_curr[i + 1]

            # Velocity term for damping.
            velocity: float = self._u_curr[i] - self._u_prev[i]

            u_next[i] = (
                2.0 * self._u_curr[i]
                - self._u_prev[i]
                + c2dt2_dx2 * laplacian
                - damp * velocity
            )

        # Reflective boundaries (fixed endpoints: u = 0).
        u_next[0] = 0.0
        u_next[n - 1] = 0.0

        self._u_prev = self._u_curr
        self._u_curr = u_next

    def _maybe_drop(self) -> None:
        """Inject a drop impulse if the schedule says it's time."""
        if self._sim_time >= self._next_drop_t:
            if self._n_cells > 2:
                # Drop lands at a random interior position.
                pos: int = random.randint(1, self._n_cells - 2)
                self._u_curr[pos] += DROP_IMPULSE
            # Schedule next drop (Poisson process).
            self._next_drop_t = self._sim_time + random.expovariate(self.drop_rate)

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the ripple tank.

        Advances the wave simulation to match the current time, then
        maps displacement to color and brightness.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        zpb: int = self.zones_per_bulb
        n_cells: int = max(1, zone_count // zpb)

        # Re-initialize if zone count changed.
        if n_cells != self._n_cells:
            self._n_cells = n_cells
            self._u_curr = [0.0] * n_cells
            self._u_prev = [0.0] * n_cells

        # First-frame initialization.
        if self._last_t is None:
            self._last_t = t
            self._sim_time = t

        # How much real time has elapsed since last render.
        dt_real: float = t - self._last_t
        self._last_t = t

        # Advance simulation in fixed timesteps.
        steps: int = min(MAX_STEPS_PER_FRAME, int(dt_real / PHYSICS_DT))
        for _ in range(max(1, steps)):
            self._maybe_drop()
            self._step()
            self._sim_time += PHYSICS_DT

        # Find maximum displacement for normalization.
        max_disp: float = 0.0
        for u in self._u_curr:
            a: float = abs(u)
            if a > max_disp:
                max_disp = a
        # Avoid division by zero; also prevents over-amplification of
        # tiny residual waves after damping has mostly killed them.
        if max_disp < 0.05:
            max_disp = 1.0

        max_bri: int = pct_to_u16(self.brightness)
        min_bri: int = int(max_bri * FLOOR_FRAC)
        bri_range: int = max_bri - min_bri
        sat_u16: int = pct_to_u16(self.saturation)

        color_pos: HSBK = (hue_to_u16(self.hue1), sat_u16, max_bri, self.kelvin)
        color_neg: HSBK = (hue_to_u16(self.hue2), sat_u16, max_bri, self.kelvin)

        colors: list[HSBK] = []
        for i in range(zone_count):
            bulb_index: int = min(i // zpb, n_cells - 1)

            # Normalized displacement in [-1, 1].
            disp: float = self._u_curr[bulb_index] / max_disp
            disp = max(-1.0, min(1.0, disp))

            # Brightness from |displacement|.
            bri: int = min_bri + int(bri_range * abs(disp))

            # Color from sign: positive → hue1, negative → hue2.
            blend: float = (disp + 1.0) / 2.0  # 0 = full hue2, 1 = full hue1
            blended: HSBK = lerp_color(color_neg, color_pos, blend)

            colors.append((blended[0], blended[1], bri, self.kelvin))

        return colors
