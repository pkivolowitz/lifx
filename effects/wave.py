"""Standing wave effect — simulates a vibrating string.

Bulbs oscillate between two colors in a standing wave pattern with fixed
nodes.  Adjacent segments swing in opposite directions, just like a real
vibrating string.

displacement(x, t) = sin(nodes * π * x / L) * sin(2π * t / speed)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.2"

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

# Brightness split: nodes dim to this fraction of max, antinodes add the rest.
# 30% base + 70% displacement-scaled = 100% at full displacement.
BRI_BASE_FRAC: float = 0.3
BRI_DISP_FRAC: float = 0.7


class Wave(Effect):
    """Standing wave — bulbs vibrate between two colors with fixed nodes.

    The spatial component ``sin(nodes * π * x / L)`` creates fixed
    zero-crossing points (nodes) along the string.  The temporal
    component ``sin(2π * t / speed)`` makes segments between nodes
    swing back and forth in alternating directions.
    """

    name: str = "wave"
    description: str = "Standing wave — bulbs vibrate between two colors with fixed nodes"

    speed = Param(3.0, min=0.3, max=30.0,
                  description="Seconds per oscillation cycle")
    nodes = Param(6, min=1, max=20,
                  description="Number of stationary nodes along the string")
    hue1 = Param(240.0, min=0.0, max=360.0,
                 description="Color 1 hue in degrees (negative displacement)")
    hue2 = Param(0.0, min=0.0, max=360.0,
                 description="Color 2 hue in degrees (positive displacement)")
    sat1 = Param(100, min=0, max=100,
                 description="Color 1 saturation percent")
    sat2 = Param(100, min=0, max=100,
                 description="Color 2 saturation percent")
    brightness = Param(100, min=0, max=100,
                       description="Overall brightness percent")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the standing wave.

        Each zone computes a displacement from the product of spatial
        and temporal sine waves, then maps that to a color blend and
        brightness level.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        max_bri: int = pct_to_u16(self.brightness)

        # Build endpoint HSBK tuples for the two colors.
        color1: HSBK = (hue_to_u16(self.hue1), pct_to_u16(self.sat1), max_bri, self.kelvin)
        color2: HSBK = (hue_to_u16(self.hue2), pct_to_u16(self.sat2), max_bri, self.kelvin)

        # Temporal oscillation swings the entire pattern between -1 and +1.
        temporal: float = math.sin(TWO_PI * t / self.speed)

        colors: list[HSBK] = []
        for i in range(zone_count):
            # Normalized position along the string (0.0 to 1.0).
            x: float = i / (zone_count - 1) if zone_count > 1 else 0.0

            # Spatial component: sin(nodes * π * x) creates fixed nodes
            # where the string doesn't move.  For a single-zone device
            # x=0 is always a node (sin(0)=0), so force full antinode
            # amplitude so the temporal oscillation drives the output.
            spatial: float = 1.0 if zone_count == 1 else math.sin(self.nodes * math.pi * x)

            # Combined displacement: -1.0 to +1.0.
            displacement: float = spatial * temporal

            # Map displacement to color blend factor.
            # -1 = pure color1, 0 = midpoint, +1 = pure color2.
            blend: float = (displacement + 1.0) / 2.0

            # Interpolate hue and saturation via the global color method
            # (Lab or HSB, selected by --lerp).
            blended: HSBK = lerp_color(color1, color2, blend)

            # Brightness peaks at antinodes (large displacement), dims at
            # nodes where displacement is near zero.  Override the blended
            # brightness with displacement-based modulation.
            bri: int = int(max_bri * (BRI_BASE_FRAC + BRI_DISP_FRAC * abs(displacement)))

            colors.append((blended[0], blended[1], bri, self.kelvin))

        return colors
