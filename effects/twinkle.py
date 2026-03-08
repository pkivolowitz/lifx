"""Twinkle effect — random zones sparkle and fade like Christmas lights.

Each zone independently triggers a sparkle at random intervals.  Sparkles
flash bright white (or a chosen hue) then decay smoothly back to the
background color.  Multiple sparkles overlap naturally.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import random
from typing import Optional

from . import (
    Effect, Param, HSBK,
    KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sparkle trigger rate multiplier.  The per-frame probability is
# ``density * dt * SPARKLE_RATE``, tuned so density ≈ 0.15 gives a
# pleasant twinkling rate at ~20 fps.
SPARKLE_RATE: float = 20.0

# Intensity threshold: above this the sparkle keeps its own hue;
# below it the hue snaps to the background.
HUE_BLEND_THRESHOLD: float = 0.5


class Twinkle(Effect):
    """Random zones sparkle and fade like Christmas lights.

    Each zone maintains an independent sparkle timer.  When it triggers,
    the zone flashes to peak brightness then decays via quadratic falloff
    (fast flash, slow tail) back to the background color.
    """

    name: str = "twinkle"
    description: str = "Random zones sparkle and fade like Christmas lights"

    speed = Param(0.5, min=0.1, max=5.0,
                  description="Sparkle fade duration in seconds")
    density = Param(0.15, min=0.01, max=1.0,
                    description="Probability a zone sparks per frame (0-1)")
    hue = Param(0.0, min=0.0, max=360.0,
                description="Sparkle hue in degrees (0=white when sat=0)")
    saturation = Param(0, min=0, max=100,
                       description="Sparkle saturation (0=white sparkle)")
    brightness = Param(100, min=0, max=100,
                       description="Peak sparkle brightness percent")
    bg_hue = Param(240.0, min=0.0, max=360.0,
                   description="Background hue in degrees")
    bg_sat = Param(80, min=0, max=100,
                   description="Background saturation percent")
    bg_bri = Param(10, min=0, max=100,
                   description="Background brightness percent")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def __init__(self, **overrides: dict) -> None:
        """Initialize sparkle state tracking.

        Args:
            **overrides: Parameter names mapped to override values.
        """
        super().__init__(**overrides)
        # Per-zone sparkle timer: time remaining in current sparkle (0 = idle).
        self._sparkle_t: Optional[list[float]] = None
        # Timestamp of the previous render call for computing delta-time.
        self._last_t: Optional[float] = None

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of twinkling sparkles.

        On each frame, every zone has a random chance of triggering a
        new sparkle.  Active sparkles decay quadratically toward the
        background.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        # Initialize or reinitialize sparkle timers if zone count changed.
        if self._sparkle_t is None or len(self._sparkle_t) != zone_count:
            self._sparkle_t = [0.0] * zone_count
            self._last_t = t

        # Compute time elapsed since last frame; clamp to zero on backwards jumps.
        dt: float = max(t - self._last_t, 0.0)
        self._last_t = t

        # Pre-compute color values in LIFX u16 range.
        max_bri: int = pct_to_u16(self.brightness)
        spark_hue: int = hue_to_u16(self.hue)
        spark_sat: int = pct_to_u16(self.saturation)
        bg_h: int = hue_to_u16(self.bg_hue)
        bg_s: int = pct_to_u16(self.bg_sat)
        bg_b: int = pct_to_u16(self.bg_bri)

        colors: list[HSBK] = []
        for i in range(zone_count):
            # Decay existing sparkle timers by the elapsed time.
            if self._sparkle_t[i] > 0:
                self._sparkle_t[i] -= dt
                if self._sparkle_t[i] < 0:
                    self._sparkle_t[i] = 0.0

            # Random chance to trigger a new sparkle on an idle zone.
            if self._sparkle_t[i] <= 0 and random.random() < self.density * dt * SPARKLE_RATE:
                self._sparkle_t[i] = self.speed

            if self._sparkle_t[i] > 0:
                # Fraction of sparkle remaining: 1.0 at trigger, 0.0 at end.
                frac: float = self._sparkle_t[i] / self.speed

                # Quadratic decay gives a fast flash then slow tail.
                intensity: float = frac * frac

                bri: int = int(bg_b + (max_bri - bg_b) * intensity)

                # Hold sparkle hue while bright, snap to background
                # hue as the sparkle fades past the threshold.
                zone_hue: int = spark_hue if intensity > HUE_BLEND_THRESHOLD else bg_h
                sat: int = int(spark_sat + (bg_s - spark_sat) * (1 - intensity))
                colors.append((zone_hue, sat, bri, self.kelvin))
            else:
                colors.append((bg_h, bg_s, bg_b, self.kelvin))

        return colors
