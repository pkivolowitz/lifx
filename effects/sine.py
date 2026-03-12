"""Traveling sine wave effect — a colored sine wave rolls along the strip.

A single sine wave travels smoothly from one end to the other.  The positive
half-cycle displays a user-chosen color with brightness proportional to the
sine value.  The negative half-cycle maps to black (off), creating distinct
bright humps separated by dark gaps that roll continuously.

displacement(x, t) = sin(2π * (x / wavelength - t / speed))
    if displacement > 0: brightness = displacement * max_brightness
    if displacement ≤ 0: brightness = 0 (black)
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

# Black — used for the negative half of the sine wave.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)


class Sine(Effect):
    """Traveling sine wave — bright humps roll along the strip.

    The sine function's positive half maps to a colored hump whose
    brightness follows the curve.  The negative half is black,
    producing dark gaps between the humps.  The wave scrolls
    continuously at a configurable speed.
    """

    name: str = "sine"
    description: str = "Traveling sine wave — bright humps roll along the strip with dark gaps"

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
    zones_per_bulb = Param(DEFAULT_ZPB, min=1, max=16,
                           description="Zones per physical bulb (3 for string lights)")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")
    reverse = Param(0, min=0, max=1,
                    description="Reverse wave direction (0 = left-to-right, 1 = right-to-left)")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the traveling sine wave.

        Each zone computes its displacement from a standard traveling
        wave equation.  Positive displacement becomes brightness;
        negative displacement becomes black.

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

        zpb: int = self.zones_per_bulb
        bulb_count: int = max(1, zone_count // zpb)

        colors: list[HSBK] = []
        for i in range(zone_count):
            # Polychrome-aware: all zones within one bulb share position.
            bulb_index: int = i // zpb

            # Normalized position along the strip (0.0 to 1.0).
            x: float = bulb_index / bulb_count if bulb_count > 0 else 0.0

            # Traveling wave: sin(2π * (x/wavelength - t/speed))
            # The spatial term x/wavelength controls how many humps are
            # visible.  The temporal term t/speed scrolls the pattern.
            phase: float = TWO_PI * (x / self.wavelength - direction * t / self.speed)
            displacement: float = math.sin(phase)

            if displacement <= 0.0:
                # Negative half-cycle: black.
                colors.append(BLACK)
            else:
                # Positive half-cycle: square the displacement so the
                # derivative is zero at the zero crossing.  This
                # eliminates visible quivering on the slopes — the
                # brightness ramps in and out smoothly instead of
                # jumping between black and dim.
                bri: int = int(max_bri * displacement * displacement)

                if use_gradient:
                    # Blend between hue and hue2 based on position along strip.
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
