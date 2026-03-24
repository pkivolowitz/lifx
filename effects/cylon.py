"""Cylon / Larson scanner effect.

A bright "eye" sweeps back and forth with a smooth falloff trail.
Classic Battlestar Galactica / Knight Rider look.

The eye position follows a sinusoidal easing curve so direction
reversals at the edges look natural rather than abrupt.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
from typing import Optional

from . import (
    DEVICE_TYPE_BULB, DEVICE_TYPE_STRIP,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum travel distance to prevent division by zero on single-zone devices.
MIN_TRAVEL: int = 1

# Divisor for the cosine eye shape — splits the eye width in half.
COSINE_DIVISOR: float = 2.0

# Sinusoidal easing uses a full cycle (2π) mapped across one sweep period.
FULL_CYCLE: float = 2.0 * math.pi


class Cylon(Effect):
    """Larson scanner — a bright eye sweeps back and forth.

    The eye has a cosine-shaped brightness profile so it tapers smoothly
    on both sides.  Eye width, color, and speed are all tunable.
    """

    name: str = "cylon"
    description: str = "Larson scanner — a bright eye sweeps back and forth"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_BULB, DEVICE_TYPE_STRIP})

    speed = Param(2.0, min=0.2, max=30.0,
                  description="Seconds per full sweep (there and back)")
    width = Param(5, min=1, max=50,
                  description="Width of the eye in bulbs (1 zone = 1 bulb)")
    hue = Param(0.0, min=0.0, max=360.0,
                description="Eye color hue in degrees (0=red, 120=green, 240=blue)")
    brightness = Param(100, min=0, max=100,
                       description="Eye brightness as percent")
    bg = Param(0, min=0, max=100,
               description="Background brightness as percent")
    trail = Param(0.4, min=0.0, max=1.0,
                  description="Trail decay factor (0=no trail, 1=max trail)")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def __init__(self, **overrides: dict) -> None:
        """Initialize with optional parameter overrides.

        Args:
            **overrides: Parameter names mapped to override values.
        """
        super().__init__(**overrides)
        self._prev_position: Optional[float] = None

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the Cylon scanner.

        The eye position is computed via sinusoidal easing across the
        zone range.  Each zone's brightness is the cosine falloff from
        the eye center, floored at the background brightness.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        hue_16: int = hue_to_u16(self.hue)
        max_bri: int = pct_to_u16(self.brightness)
        bg_bri: int = pct_to_u16(self.bg)
        half: float = self.width / COSINE_DIVISOR

        # Travel distance is the number of inter-zone gaps the eye crosses.
        travel: int = max(zone_count - 1, MIN_TRAVEL)

        # Phase within the current sweep cycle (0.0 to 1.0).
        phase: float = (t % self.speed) / self.speed

        # Sinusoidal easing: cos maps [0..2π] to [1..-1..1], scaled to
        # [0..travel..0] for a smooth bounce at both ends.
        position: float = travel * (1 - math.cos(phase * FULL_CYCLE)) / COSINE_DIVISOR

        colors: list[HSBK] = []
        for i in range(zone_count):
            dist: float = abs(i - position)

            if dist < half:
                # Cosine falloff: full brightness at center, tapering to zero
                # at the eye edges for a smooth, rounded profile.
                t_norm: float = dist / half
                eye_bri: int = int(max_bri * (math.cos(t_norm * math.pi) + 1) / COSINE_DIVISOR)
                zone_bri: int = max(eye_bri, bg_bri)
            else:
                zone_bri = bg_bri

            colors.append((hue_16, HSBK_MAX, zone_bri, self.kelvin))

        return colors
