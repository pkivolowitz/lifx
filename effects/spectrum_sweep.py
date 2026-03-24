"""Spectrum sweep — three-phase sine waves drive color across zones.

Three sine waves, 120 degrees out of phase, sweep through the zones
at a configurable rate.  Each wave controls one of the three primary
hue regions (red, green, blue), producing a continuously shifting
color pattern that travels along the strip.

Designed to look like a frequency sweep across a spectrum analyzer.
No audio input needed — the waves are synthetic.

Usage::

    python3 glowup.py play spectrum_sweep --ip <device-ip>
    python3 glowup.py play spectrum_sweep --sim-only --zones 24
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
)

# Import colorspace module from project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from colorspace import lerp_color

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Full circle.
TWO_PI: float = 2.0 * math.pi

# Phase offset between waves (120 degrees = 2π/3).
PHASE_OFFSET: float = TWO_PI / 3.0

# Number of waves.
NUM_PHASES: int = 3

# Hue anchors for the three phases (degrees → LIFX u16).
# Red, green, blue — 120° apart on the color wheel.
PHASE_HUES: list[int] = [
    0,                                    # Red    (0°)
    int(120.0 / 360.0 * HSBK_MAX),       # Green  (120°)
    int(240.0 / 360.0 * HSBK_MAX),       # Blue   (240°)
]


class SpectrumSweep(Effect):
    """Three-phase sine sweep across zones — synthetic spectrum analyzer.

    Three sine waves at 120° phase separation travel along the strip.
    Each wave's amplitude controls brightness at its hue anchor.
    Where waves overlap, colors blend through Oklab.  The result is
    a smooth, continuously shifting rainbow that wraps and travels.
    """

    name: str = "spectrum_sweep"
    description: str = "Three-phase sine sweep — traveling rainbow"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_STRIP})

    speed = Param(3.0, min=0.5, max=20.0,
                  description="Seconds per full sweep cycle")
    waves = Param(2.0, min=0.5, max=8.0,
                  description="Number of wave periods across the strip")
    brightness = Param(100, min=0, max=100,
                       description="Peak brightness percent")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def period(self) -> float:
        """Full cycle period."""
        return self.speed

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the three-phase sweep.

        For each zone, compute three sine wave amplitudes (120° apart),
        use them as brightness weights for red, green, blue, and blend.

        Args:
            t:          Seconds elapsed.
            zone_count: Number of zones (logical bulbs after zpb).

        Returns:
            List of HSBK tuples.
        """
        peak_bri: float = self.brightness / 100.0
        colors: list[HSBK] = []

        for z in range(zone_count):
            # Spatial position along the strip (0 to 1).
            pos: float = z / max(zone_count - 1, 1)

            # Temporal phase — sweeps the pattern along the strip.
            time_phase: float = TWO_PI * t / self.speed

            # Spatial frequency — how many wave periods fit on the strip.
            spatial: float = TWO_PI * self.waves * pos

            # Three sine waves, 120° apart.
            # Map sine (-1..+1) to amplitude (0..1).
            amps: list[float] = []
            for i in range(NUM_PHASES):
                phase: float = i * PHASE_OFFSET
                s: float = math.sin(spatial - time_phase + phase)
                amp: float = (s + 1.0) / 2.0  # 0 to 1
                amps.append(amp)

            # Build three HSBK colors at the phase hues, weighted
            # by their amplitudes.
            bri_r: int = int(amps[0] * peak_bri * HSBK_MAX)
            bri_g: int = int(amps[1] * peak_bri * HSBK_MAX)
            bri_b: int = int(amps[2] * peak_bri * HSBK_MAX)

            c_r: HSBK = (PHASE_HUES[0], HSBK_MAX, bri_r, self.kelvin)
            c_g: HSBK = (PHASE_HUES[1], HSBK_MAX, bri_g, self.kelvin)
            c_b: HSBK = (PHASE_HUES[2], HSBK_MAX, bri_b, self.kelvin)

            # Blend: find the dominant wave and blend toward second.
            # Sort by amplitude — strongest first.
            ranked: list[tuple[float, HSBK]] = sorted(
                zip(amps, [c_r, c_g, c_b]),
                key=lambda x: -x[0],
            )

            # Blend the top two by their relative weights.
            a1: float = ranked[0][0]
            a2: float = ranked[1][0]
            total: float = a1 + a2
            if total > 0.0:
                blend: float = a2 / total
            else:
                blend = 0.0

            color: HSBK = lerp_color(ranked[0][1], ranked[1][1], blend)

            # Override brightness with the max amplitude.
            max_amp: float = max(amps)
            final_bri: int = int(max_amp * peak_bri * HSBK_MAX)
            color = (color[0], color[1], final_bri, color[3])

            colors.append(color)

        return colors
