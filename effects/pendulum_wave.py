"""Pendulum wave effect — a row of pendulums with slightly different periods.

Each bulb is one pendulum.  All start in phase, then drift apart as their
slightly different frequencies create traveling waves, standing waves, and
apparent chaos — before magically realigning.  This is a real physics
demonstration (search "pendulum wave" on YouTube).

The math is simple harmonic motion with linearly varying periods:

    θ_n(t) = sin(2π · t / T_n)

where T_n = T_base / (N_cycles + n/num_pendulums) so that after T_base
seconds the fastest pendulum has completed exactly N_cycles + 1 full
oscillations and the slowest has completed N_cycles — putting them all
back in phase.

Displacement maps to color blend (two endpoint colors) and brightness
modulation, producing a mesmerizing visual wave machine.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
import os
import sys

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

TWO_PI: float = 2.0 * math.pi

# Brightness split: pendulums at rest (displacement ≈ 0) dim to this
# fraction, full displacement adds the remainder.
BRI_BASE_FRAC: float = 0.3
BRI_DISP_FRAC: float = 0.7

# Default zones per bulb for polychrome-aware rendering.
DEFAULT_ZPB: int = 1

# Minimum gap between pendulums in zones (prevents cramming on long strips).
MIN_GAP: int = 0


class PendulumWave(Effect):
    """Pendulum wave — a row of pendulums drifting in and out of phase.

    Each pendulum swings at a slightly different frequency.  Over one full
    cycle (``speed`` seconds) the ensemble passes through traveling waves,
    standing waves, and chaos before all pendulums realign perfectly.

    Displacement maps to a color blend between two hues and to brightness
    modulation — pendulums at the extremes of their swing are brightest,
    pendulums passing through center are dimmest.
    """

    name: str = "pendulum_wave"
    description: str = "Row of pendulums drifting in and out of phase"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_STRIP})

    speed = Param(30.0, min=5.0, max=120.0,
                  description="Seconds for full realignment cycle")
    cycles = Param(10, min=3, max=50,
                   description="Oscillations of the slowest pendulum per cycle")
    hue1 = Param(240.0, min=0.0, max=360.0,
                 description="Color at negative displacement (degrees)")
    hue2 = Param(0.0, min=0.0, max=360.0,
                 description="Color at positive displacement (degrees)")
    sat1 = Param(100, min=0, max=100,
                 description="Saturation at negative displacement")
    sat2 = Param(100, min=0, max=100,
                 description="Saturation at positive displacement")
    brightness = Param(100, min=0, max=100,
                       description="Peak brightness percent")
    zones_per_bulb = Param(DEFAULT_ZPB, min=1, max=16,
                           description="Zones per physical bulb (3 for string lights)")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the pendulum wave.

        Each pendulum's displacement is computed from its individual
        frequency, then mapped to color and brightness.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        max_bri: int = pct_to_u16(self.brightness)
        zpb: int = self.zones_per_bulb
        bulb_count: int = max(1, zone_count // zpb)

        # Build endpoint colors for the blend.
        color1: HSBK = (hue_to_u16(self.hue1), pct_to_u16(self.sat1),
                        max_bri, self.kelvin)
        color2: HSBK = (hue_to_u16(self.hue2), pct_to_u16(self.sat2),
                        max_bri, self.kelvin)

        # Number of pendulums = number of bulbs.
        n_pend: int = bulb_count

        colors: list[HSBK] = []
        for i in range(zone_count):
            bulb_index: int = i // zpb

            # Each pendulum has a slightly different period.
            # T_n = speed / (cycles + bulb_index / n_pend)
            # At t=speed: pendulum 0 has done exactly `cycles` oscillations,
            # pendulum (n_pend-1) has done `cycles + (n_pend-1)/n_pend` —
            # nearly one extra.  They realign at t = speed.
            freq: float = self.cycles + bulb_index / n_pend if n_pend > 0 else self.cycles
            period: float = self.speed / freq if freq > 0 else self.speed

            # Simple harmonic motion: displacement in [-1, 1].
            displacement: float = math.sin(TWO_PI * t / period)

            # Map displacement to color blend: -1 → color1, +1 → color2.
            blend: float = (displacement + 1.0) / 2.0
            blended: HSBK = lerp_color(color1, color2, blend)

            # Brightness modulation: extremes are bright, center is dim.
            bri: int = int(max_bri * (BRI_BASE_FRAC + BRI_DISP_FRAC * abs(displacement)))

            colors.append((blended[0], blended[1], bri, self.kelvin))

        return colors

    def period(self) -> float:
        """Return the realignment cycle for seamless loop recording.

        Returns:
            The speed parameter — one full pendulum wave cycle.
        """
        return float(self.speed)
