"""Spin effect — rotates colors within each polychrome bulb.

Exploits the LIFX polychrome architecture where each physical bulb
contains 3 independently addressable LED zones.  A set of colors
rotates through the 3 zones of every bulb, creating the illusion of
each bulb spinning like a tiny color wheel.

The ``zones_per_bulb`` parameter (default 3) controls how zones are
grouped into logical bulbs.  This must match the physical hardware —
LIFX string lights use 3 zones per bulb.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default number of zones per physical bulb (LIFX polychrome string lights).
ZONES_PER_BULB_DEFAULT: int = 3

# Number of hue colors in the default palette.
DEFAULT_COLOR_COUNT: int = 3


class Spin(Effect):
    """Rotate colors within each polychrome bulb.

    Each bulb's zones cycle through a set of evenly-spaced hues,
    creating a spinning color wheel effect within every bulb.
    Adjacent bulbs are offset so the whole string shimmers.
    """

    name: str = "spin"
    description: str = "Rotate colors within each polychrome bulb"

    speed = Param(2.0, min=0.2, max=30.0,
                  description="Seconds per full rotation")
    brightness = Param(100, min=0, max=100,
                       description="Brightness percent")
    saturation = Param(100, min=0, max=100,
                       description="Saturation percent")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")
    hue_spread = Param(120.0, min=10.0, max=360.0,
                       description="Hue spread in degrees across zones within a bulb")
    base_hue = Param(0.0, min=0.0, max=360.0,
                     description="Starting hue in degrees (0=red, 120=green, 240=blue)")
    bulb_offset = Param(30.0, min=0.0, max=360.0,
                        description="Hue offset in degrees between adjacent bulbs")
    zones_per_bulb = Param(ZONES_PER_BULB_DEFAULT, min=1, max=16,
                           description="Number of zones per physical bulb")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame — each bulb's zones rotate through colors.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        zpb: int = self.zones_per_bulb
        bri: int = pct_to_u16(self.brightness)
        sat: int = pct_to_u16(self.saturation)

        # Rotation phase: 0.0 to 1.0 over the speed period.
        phase: float = (t / self.speed) % 1.0

        colors: list[HSBK] = []
        for i in range(zone_count):
            bulb_index: int = i // zpb
            zone_in_bulb: int = i % zpb

            # Each zone within a bulb gets an evenly-spaced hue offset.
            # The phase rotates all zones together.
            zone_fraction: float = (zone_in_bulb + phase * zpb) / zpb
            hue_degrees: float = (
                self.base_hue
                + zone_fraction * self.hue_spread
                + bulb_index * self.bulb_offset
            )
            hue: int = hue_to_u16(hue_degrees % 360.0)
            colors.append((hue, sat, bri, self.kelvin))

        return colors
