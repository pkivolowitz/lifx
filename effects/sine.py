"""Traveling ease wave effect — bright humps roll along the strip.

A wave travels smoothly from one end to the other using cubic
ease-in-ease-out interpolation.  Each zone's normalized phase
(0 to 1) is mapped through the smoothstep function

    f(x) = 3x² - 2x³

which has zero derivative at both endpoints — brightness ramps
up gently from black, peaks, and ramps back down without any
perceptible flicker or quiver at the transitions.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.4"

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

# Black — used for the off portion of the wave.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)


def _smoothstep(x: float) -> float:
    """Cubic ease-in-ease-out: 3x² - 2x³.

    Input and output are both in [0, 1].  First and second
    derivatives are zero at x=0 and x=1, producing silky-smooth
    brightness transitions with no perceptible flicker.

    Args:
        x: Normalized input, clamped to [0, 1].

    Returns:
        Eased output in [0, 1].
    """
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


class Sine(Effect):
    """Traveling ease wave — bright humps roll along the strip.

    Each zone computes a traveling wave phase, then the positive
    half is remapped through cubic ease-in-ease-out (smoothstep).
    The negative half is black, creating distinct bright humps
    separated by dark gaps that scroll continuously.
    """

    name: str = "sine"
    description: str = "Traveling ease wave — bright humps roll with smooth cubic transitions"

    speed = Param(4.0, min=0.3, max=30.0,
                  description="Seconds per full wave cycle (travel speed)")
    wavelength = Param(0.5, min=0.1, max=5.0,
                       description="Wavelength as fraction of strip length (0.5 = two humps visible)")
    hue = Param(200.0, min=0.0, max=360.0,
                description="Wave color hue in degrees")
    saturation = Param(100, min=0, max=100,
                       description="Wave color saturation percent")
    brightness = Param(100, min=0, max=100,
                       description="Peak brightness percent")
    floor = Param(2, min=0, max=50,
                  description="Minimum brightness percent (avoids flicker near black)")
    hue2 = Param(-1.0, min=-1.0, max=360.0,
                 description="Optional second hue for gradient along wave (-1 = disabled)")
    zones_per_bulb = Param(DEFAULT_ZPB, min=1, max=16,
                           description="Zones per physical bulb (3 for string lights)")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")
    reverse = Param(0, min=0, max=1,
                    description="Reverse wave direction (0 = left-to-right, 1 = right-to-left)")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the traveling ease wave.

        Each zone computes a traveling-wave phase.  The positive
        half-cycle is remapped through smoothstep for flicker-free
        brightness; the negative half-cycle is black.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        max_bri: int = pct_to_u16(self.brightness)
        min_bri: int = pct_to_u16(self.floor)
        bri_range: int = max_bri - min_bri
        hue_u16: int = hue_to_u16(self.hue)
        sat_u16: int = pct_to_u16(self.saturation)

        # Build base color for the wave crest.
        base_color: HSBK = (hue_u16, sat_u16, max_bri, self.kelvin)

        # Optional second color for gradient along the wave crest.
        use_gradient: bool = self.hue2 >= 0.0
        if use_gradient:
            end_color: HSBK = (hue_to_u16(self.hue2), sat_u16, max_bri, self.kelvin)

        # Direction multiplier.
        direction: float = -1.0 if self.reverse else 1.0

        zpb: int = self.zones_per_bulb
        bulb_count: int = max(1, zone_count // zpb)

        colors: list[HSBK] = []
        for i in range(zone_count):
            # Polychrome-aware: all zones within one bulb share position.
            bulb_index: int = i // zpb

            # Normalized position along the strip (0.0 to 1.0).
            x: float = bulb_index / bulb_count if bulb_count > 0 else 0.0

            # Traveling wave: sin(2π * (x/wavelength - t/speed))
            phase: float = TWO_PI * (x / self.wavelength - direction * t / self.speed)
            displacement: float = math.sin(phase)

            if displacement <= 0.0:
                # Negative half-cycle: hold at floor brightness.
                colors.append((hue_u16, sat_u16, min_bri, self.kelvin))
            else:
                # Positive half-cycle: remap through cubic ease-in-ease-out.
                # Scale from floor to peak brightness.
                eased: float = _smoothstep(displacement)
                bri: int = min_bri + int(bri_range * eased)

                if use_gradient:
                    blended: HSBK = lerp_color(base_color, end_color, x)
                    colors.append((blended[0], blended[1], bri, self.kelvin))
                else:
                    colors.append((hue_u16, sat_u16, bri, self.kelvin))

        return colors

    def period(self) -> float:
        """Return the wave cycle period for seamless loop recording.

        Returns:
            The speed parameter — one full cycle of the traveling wave.
        """
        return float(self.speed)
