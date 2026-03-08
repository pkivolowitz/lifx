"""Breathe effect — all bulbs oscillate between two colors via sine wave.

Color 1 is shown at the trough (sin < 0), color 2 at the peak (sin > 0),
with smooth blending through the full cycle.  Hue interpolation takes the
shortest path around the color wheel.
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

# Halfway point of the HSBK hue range — used to decide the shorter
# interpolation path around the color wheel.
HUE_HALFWAY: int = HSBK_MAX // 2


class Breathe(Effect):
    """All bulbs oscillate between two colors via sine wave.

    The sine wave produces a smooth blend factor: at the trough the
    string shows color 1, at the peak it shows color 2, with seamless
    interpolation in between.
    """

    name: str = "breathe"
    description: str = "All bulbs oscillate between two colors via sine wave"

    speed = Param(4.0, min=0.5, max=30.0,
                  description="Seconds per full cycle")
    hue1 = Param(240.0, min=0.0, max=360.0,
                 description="Color 1 hue in degrees (shown at sin < 0)")
    hue2 = Param(0.0, min=0.0, max=360.0,
                 description="Color 2 hue in degrees (shown at sin > 0)")
    sat1 = Param(100, min=0, max=100,
                 description="Color 1 saturation percent")
    sat2 = Param(100, min=0, max=100,
                 description="Color 2 saturation percent")
    brightness = Param(100, min=0, max=100,
                       description="Overall brightness percent")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame — every zone gets the same blended color.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* identical HSBK tuples.
        """
        # Sine wave oscillates -1 to +1 over the speed period.
        s: float = math.sin(TWO_PI * t / self.speed)

        # Map sine range [-1, +1] to blend factor [0.0, 1.0].
        # 0.0 = pure color1, 1.0 = pure color2.
        blend: float = (s + 1.0) / 2.0

        h1: float = self.hue1 * HSBK_MAX / 360.0
        h2: float = self.hue2 * HSBK_MAX / 360.0
        s1: int = pct_to_u16(self.sat1)
        s2: int = pct_to_u16(self.sat2)
        bri: int = pct_to_u16(self.brightness)

        # Interpolate hue via shortest path around the color wheel.
        # If the gap exceeds half the wheel, wrap through zero instead.
        diff: float = h2 - h1
        if diff > HUE_HALFWAY:
            diff -= HSBK_MAX
        elif diff < -HUE_HALFWAY:
            diff += HSBK_MAX
        hue: int = int(h1 + diff * blend) % (HSBK_MAX + 1)

        sat: int = int(s1 + (s2 - s1) * blend)

        color: HSBK = (hue, sat, bri, self.kelvin)
        return [color] * zone_count
