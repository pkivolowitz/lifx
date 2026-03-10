"""Polychrome test — static R/G/B pattern to reveal zone positions.

Diagnostic effect that sets each bulb's 3 zones to red, green, and
blue respectively.  Run this on a string light and observe which
physical position inside the bulb corresponds to zone 0, 1, and 2.

This knowledge informs the design of polychrome-aware effects like
:class:`~effects.spin.Spin`.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default number of zones per physical bulb (LIFX polychrome string lights).
ZONES_PER_BULB_DEFAULT: int = 3

# Pure red, green, blue at full brightness.
RED: HSBK = (0, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT)
GREEN: HSBK = (21845, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT)   # 120° = 21845
BLUE: HSBK = (43690, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT)    # 240° = 43690

# Zone color palette — one color per zone within a bulb.
ZONE_COLORS: list[HSBK] = [RED, GREEN, BLUE]


class PolychromeTest(Effect):
    """Static R/G/B test pattern for mapping polychrome zone positions.

    Sets zone 0 = red, zone 1 = green, zone 2 = blue within each
    bulb.  Observe the physical bulb to determine which LED position
    corresponds to each zone index.
    """

    name: str = "_polychrome_test"
    description: str = "Static R/G/B pattern to reveal zone positions within each bulb"

    zones_per_bulb = Param(ZONES_PER_BULB_DEFAULT, min=1, max=16,
                           description="Number of zones per physical bulb")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce a static frame — each bulb shows R/G/B.

        Args:
            t:          Seconds elapsed (unused — pattern is static).
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        colors: list[HSBK] = []
        for i in range(zone_count):
            zone_in_bulb: int = i % self.zones_per_bulb
            if zone_in_bulb < len(ZONE_COLORS):
                colors.append(ZONE_COLORS[zone_in_bulb])
            else:
                colors.append(RED)
        return colors
