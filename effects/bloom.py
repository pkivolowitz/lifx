"""Bloom effect — bulbs appear to grow and shrink using polychrome zones.

Exploits the concentric cylinder architecture of LIFX polychrome bulbs.
The three zones are concentric light-guide cylinders with very different
visual contributions: the inner tube is small and concentrated (dominates
apparent color), the middle is moderate, and the outer ring is large and
nearly clear (contributes very little).

Brightness weights compensate for this non-linearity so each zone's
activation produces a roughly equal perceptual step.  The sine wave
drives a smooth expansion/contraction cycle.
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

# Full sine cycle in radians.
TWO_PI: float = 2.0 * math.pi

# Default zones per bulb for LIFX polychrome string lights.
DEFAULT_ZPB: int = 3

# Perceptual brightness weights for each concentric zone.
# Zone 0 (inner tube): small, concentrated — dominates visible color.
# Zone 1 (middle tube): moderate contribution.
# Zone 2 (outer ring): large, nearly clear — contributes very little.
# These weights compensate so each zone activation looks like an equal
# perceptual step.  Outer zones get boosted, inner zones attenuated.
ZONE_WEIGHTS: list[float] = [0.4, 0.7, 1.0]


class Bloom(Effect):
    """Bulbs pulse by progressively lighting concentric zone cylinders.

    A sine wave drives all zones simultaneously, but each zone's
    brightness is weighted to compensate for the non-linear visual
    contribution of each concentric cylinder.  The inner tube (zone 0)
    is attenuated because it dominates perceived color; the outer ring
    (zone 2) is boosted because it's nearly transparent.

    The result is a smooth breathing effect where the bulb appears to
    grow and shrink in size as zones fade in from inner to outer and
    back out from outer to inner.
    """

    name: str = "_bloom"
    description: str = "Bulbs grow and shrink using polychrome concentric zones"

    speed = Param(4.0, min=0.5, max=30.0,
                  description="Seconds per full bloom cycle")
    hue = Param(0.0, min=0.0, max=360.0,
                description="Hue of the bloom in degrees (0=red)")
    brightness = Param(100, min=0, max=100,
                       description="Peak brightness percent")
    saturation = Param(100, min=0, max=100,
                       description="Saturation percent")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")
    zones_per_bulb = Param(DEFAULT_ZPB, min=1, max=16,
                           description="Number of zones per physical bulb")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame — zones light progressively from inner to outer.

        A sine wave drives a continuous 'level' from 0.0 to zpb.
        Each zone lights when the level reaches its position.  The
        fractional part controls fade-in/out, and the per-zone weight
        compensates for the concentric cylinder geometry.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        zpb: int = self.zones_per_bulb
        hue_u16: int = hue_to_u16(self.hue)
        sat_u16: int = pct_to_u16(self.saturation)
        max_bri: int = pct_to_u16(self.brightness)

        # Sine wave: 0.0 → 1.0 → 0.0 over the speed period.
        # Maps to level: 0.0 → zpb → 0.0 (how many zones are lit).
        wave: float = (math.sin(TWO_PI * t / self.speed - math.pi / 2.0) + 1.0) / 2.0
        level: float = wave * zpb

        colors: list[HSBK] = []
        for i in range(zone_count):
            zone_in_bulb: int = i % zpb

            # How much of this zone is "reached" by the level.
            # zone 0 lights when level > 0, zone 1 when level > 1, etc.
            zone_fill: float = level - zone_in_bulb

            if zone_fill <= 0.0:
                # Zone not yet reached — black.
                colors.append((0, 0, 0, self.kelvin))
            else:
                # Clamp fill to 1.0 for fully-lit zones.
                fill: float = min(zone_fill, 1.0)

                # Apply perceptual weight for this cylinder layer.
                # Inner zones are attenuated (they dominate visually),
                # outer zones are boosted (they contribute little).
                weight: float = ZONE_WEIGHTS[zone_in_bulb] if zone_in_bulb < len(ZONE_WEIGHTS) else 1.0
                bri: int = int(max_bri * fill * weight)
                colors.append((hue_u16, sat_u16, bri, self.kelvin))

        return colors
