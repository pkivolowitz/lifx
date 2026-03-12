"""Traveling ease wave effect — bright humps roll along the strip.

A wave travels smoothly from one end to the other using cubic
ease-in-ease-out interpolation.  Each zone's normalized phase
(0 to 1) is mapped through the smoothstep function

    f(x) = 3x² − 2x³

which has zero derivative at both endpoints — brightness ramps
up gently from black, peaks, and ramps back down without any
perceptible flicker or quiver at the transitions.

On polychrome (color) string lights, only the center ring (zone 0
of each bulb) is animated.  The outer two rings are held black.
This eliminates visible quivering caused by the three concentric
tubes responding at slightly different rates.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.3"

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

# Polychrome string lights have 3 zones per bulb (concentric rings).
ZONES_PER_BULB: int = 3

# Black — used for the off portion of the wave and outer rings.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)


def _smoothstep(x: float) -> float:
    """Cubic ease-in-ease-out: 3x² − 2x³.

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

    Each bulb computes a traveling wave phase, then the positive
    half is remapped through cubic ease-in-ease-out (smoothstep).
    The negative half is black, creating distinct bright humps
    separated by dark gaps that scroll continuously.

    Only the center ring (zone 0) of each polychrome bulb is
    animated.  The outer rings are held black to prevent visible
    quivering from the concentric tubes responding at different rates.
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
    hue2 = Param(-1.0, min=-1.0, max=360.0,
                 description="Optional second hue for gradient along wave (-1 = disabled)")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")
    reverse = Param(0, min=0, max=1,
                    description="Reverse wave direction (0 = left-to-right, 1 = right-to-left)")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the traveling ease wave.

        Each bulb computes a traveling-wave phase.  The positive
        half-cycle is remapped through smoothstep for flicker-free
        brightness; the negative half-cycle is black.  Only zone 0
        (center ring) of each 3-zone bulb is lit; outer rings stay
        black.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        max_bri: int = pct_to_u16(self.brightness)
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

        bulb_count: int = max(1, zone_count // ZONES_PER_BULB)

        colors: list[HSBK] = []
        for i in range(zone_count):
            # Which zone within this bulb? (0 = center, 1 = middle, 2 = outer)
            zone_in_bulb: int = i % ZONES_PER_BULB

            # Only animate the center ring; outer rings stay black.
            if zone_in_bulb != 0:
                colors.append(BLACK)
                continue

            # Which bulb along the strip.
            bulb_index: int = i // ZONES_PER_BULB

            # Normalized position along the strip (0.0 to 1.0).
            x: float = bulb_index / bulb_count if bulb_count > 0 else 0.0

            # Traveling wave: sin(2π * (x/wavelength - t/speed))
            phase: float = TWO_PI * (x / self.wavelength - direction * t / self.speed)
            displacement: float = math.sin(phase)

            if displacement <= 0.0:
                # Negative half-cycle: black.
                colors.append(BLACK)
            else:
                # Positive half-cycle: remap through cubic ease-in-ease-out.
                eased: float = _smoothstep(displacement)
                bri: int = int(max_bri * eased)

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
