"""Double slit interference effect — two coherent wave sources.

Two point sources emit sinusoidal waves that propagate along the strip.
Where the waves meet, constructive interference produces bright zones and
destructive interference produces dark zones — the classic Young's double
slit fringe pattern rendered on a 1D LED strip.

    wave_1(x, t) = sin(k · |x - s1| - ω · t)
    wave_2(x, t) = sin(k · |x - s2| - ω · t)
    amplitude(x, t) = wave_1 + wave_2

Slowly varying the wavelength or source separation makes the fringe
pattern breathe and shift.  Color encodes the sign of the combined
displacement: positive → hue1, negative → hue2, with brightness
proportional to |amplitude|.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
import os
import sys

from . import (
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

# Default zones per bulb for polychrome-aware rendering.
DEFAULT_ZPB: int = 1

# Floor brightness for zones near total destructive interference.
# Avoids LIFX hardware flicker at true black.
FLOOR_FRAC: float = 0.02

# Modulation depth for wavelength breathing.  The wavelength oscillates
# between wavelength * (1 - DEPTH) and wavelength * (1 + DEPTH).
BREATH_DEPTH: float = 0.3


class DoubleSlit(Effect):
    """Double slit interference — two coherent wave sources.

    Two point sources at configurable positions emit sinusoidal waves.
    The combined amplitude at each zone determines brightness and color:
    constructive interference is bright, destructive is dark.  The
    wavelength slowly breathes over time, making the fringe pattern
    shift and evolve.
    """

    name: str = "double_slit"
    description: str = "Two-source wave interference with shifting fringe patterns"

    speed = Param(4.0, min=0.5, max=30.0,
                  description="Wave propagation period in seconds")
    wavelength = Param(0.25, min=0.05, max=2.0,
                       description="Base wavelength as fraction of strip length")
    separation = Param(0.4, min=0.05, max=0.9,
                       description="Source separation as fraction of strip length")
    breathe = Param(20.0, min=0.0, max=120.0,
                    description="Wavelength modulation period in seconds (0 = off)")
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

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of double slit interference.

        Two sinusoidal waves propagate from point sources.  Their sum
        creates an interference pattern that shifts as the wavelength
        breathes.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        max_bri: int = pct_to_u16(self.brightness)
        min_bri: int = int(max_bri * FLOOR_FRAC)
        bri_range: int = max_bri - min_bri
        sat_u16: int = pct_to_u16(self.saturation)
        zpb: int = self.zones_per_bulb
        bulb_count: int = max(1, zone_count // zpb)

        # Source positions (symmetric about center).
        center: float = 0.5
        half_sep: float = self.separation / 2.0
        s1: float = center - half_sep  # normalized [0, 1]
        s2: float = center + half_sep

        # Wavelength with optional breathing modulation.
        wl: float = self.wavelength
        if self.breathe > 0.0:
            breath_phase: float = math.sin(TWO_PI * t / self.breathe)
            wl *= (1.0 + BREATH_DEPTH * breath_phase)

        # Wave number: k = 2π / (wavelength_in_zones).
        wl_zones: float = wl * bulb_count if bulb_count > 0 else 1.0
        k: float = TWO_PI / wl_zones if wl_zones > 0 else TWO_PI

        # Angular frequency: ω = 2π / speed.
        omega: float = TWO_PI / self.speed if self.speed > 0 else TWO_PI

        # Build endpoint colors.
        color_pos: HSBK = (hue_to_u16(self.hue1), sat_u16, max_bri, self.kelvin)
        color_neg: HSBK = (hue_to_u16(self.hue2), sat_u16, max_bri, self.kelvin)

        colors: list[HSBK] = []
        for i in range(zone_count):
            bulb_index: int = i // zpb

            # Normalized position along the strip.
            x: float = bulb_index / bulb_count if bulb_count > 0 else 0.5

            # Distance from each source (in zone units for wave number).
            d1: float = abs(x - s1) * bulb_count
            d2: float = abs(x - s2) * bulb_count

            # Superposition of two coherent waves.
            wave1: float = math.sin(k * d1 - omega * t)
            wave2: float = math.sin(k * d2 - omega * t)
            amplitude: float = (wave1 + wave2) / 2.0  # normalize to [-1, 1]

            # Brightness from |amplitude|.
            bri: int = min_bri + int(bri_range * abs(amplitude))

            # Color from sign of amplitude.
            if amplitude >= 0:
                blend: float = amplitude  # 0 → dim center, 1 → full hue1
                blended: HSBK = lerp_color(color_neg, color_pos, (blend + 1.0) / 2.0)
            else:
                blend = -amplitude
                blended = lerp_color(color_pos, color_neg, (blend + 1.0) / 2.0)

            colors.append((blended[0], blended[1], bri, self.kelvin))

        return colors

    def period(self) -> float:
        """Return the animation period for seamless loop recording.

        If breathing is enabled, the loop period is the LCM-like
        alignment of wave speed and breath cycle.  Otherwise just
        the wave speed.

        Returns:
            Period in seconds.
        """
        if self.breathe > 0.0:
            # Use the breath period — it's the longer cycle and the
            # wave pattern repeats within it.
            return float(self.breathe)
        return float(self.speed)
