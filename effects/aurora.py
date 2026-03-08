"""Aurora borealis effect — slow-moving curtains of color drift across the string.

Multiple overlapping sine waves at different frequencies and speeds create
organic-looking bands of green, blue, and purple that sweep and shimmer,
mimicking the northern lights.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TWO_PI: float = 2.0 * math.pi

# Aurora palette anchors in degrees — classic aurora colors.
HUE_GREEN: float = 120.0
HUE_TEAL: float = 170.0
HUE_BLUE: float = 220.0
HUE_PURPLE: float = 280.0

# Palette blend boundaries — divide [0, 1] into three color bands.
BAND_1: float = 0.33   # purple → blue
BAND_2: float = 0.66   # blue → green
BAND_3: float = 0.34   # green → teal (remaining fraction: 1.0 - 0.66)

# Wave layer tuning: (spatial_freq, temporal_freq, phase_offset, amplitude).
# These create the organic, overlapping curtain structure.
LAYER_1: tuple[float, float, float, float] = (2.3, 0.7, 0.0, 1.0)    # broad slow — main curtain
LAYER_2: tuple[float, float, float, float] = (3.7, -1.1, 1.0, 1.0)   # medium — secondary structure
LAYER_3: tuple[float, float, float, float] = (7.1, 2.3, 2.5, 0.3)    # fine shimmer — ripple
LAYER_4: tuple[float, float, float, float] = (1.1, 0.3, -0.8, 0.5)   # very slow — whole-pattern drift

# Normalization: maps the raw sum of four wave layers into [0, 1].
INTENSITY_OFFSET: float = 2.0
INTENSITY_SCALE: float = 4.8

# Hue wave parameters: (spatial_freq, temporal_freq, phase_offset).
HUE_WAVE_1: tuple[float, float, float] = (2.9, -0.5, 1.7)
HUE_WAVE_2: tuple[float, float, float] = (1.3, 0.8, 0.0)
HUE_WAVE_2_AMP: float = 0.5   # amplitude of the secondary hue modulation
HUE_OFFSET: float = 1.5
HUE_SCALE: float = 3.0


class Aurora(Effect):
    """Slow-moving curtains of color like the northern lights.

    Four overlapping sine wave layers produce organic brightness
    variation, while two independent hue waves sweep color bands
    across the string.  The result mimics the shimmering curtains
    of a real aurora borealis.
    """

    name: str = "aurora"
    description: str = "Slow-moving curtains of color like the northern lights"

    speed = Param(8.0, min=1.0, max=60.0,
                  description="Seconds per full drift cycle")
    brightness = Param(80, min=0, max=100,
                       description="Peak brightness percent")
    bg_bri = Param(5, min=0, max=100,
                   description="Background brightness percent")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the aurora effect.

        For each zone, four sine wave layers are summed to produce
        brightness intensity, and two separate hue waves select the
        color from a green-blue-purple palette.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        max_bri: int = pct_to_u16(self.brightness)
        bg_b: int = pct_to_u16(self.bg_bri)

        # Phase advances linearly with time, scaled by the speed parameter.
        phase: float = TWO_PI * t / self.speed

        colors: list[HSBK] = []
        for i in range(zone_count):
            # Normalized position along the string: 0.0 at first zone, 1.0 at last.
            x: float = i / max(zone_count - 1, 1)

            # --- Brightness: four overlapping wave layers ---
            w1: float = math.sin(LAYER_1[0] * math.pi * x + phase * LAYER_1[1] + LAYER_1[2]) * LAYER_1[3]
            w2: float = math.sin(LAYER_2[0] * math.pi * x + phase * LAYER_2[1] + LAYER_2[2]) * LAYER_2[3]
            w3: float = math.sin(LAYER_3[0] * math.pi * x + phase * LAYER_3[1] + LAYER_3[2]) * LAYER_3[3]
            w4: float = math.sin(LAYER_4[0] * math.pi * x + phase * LAYER_4[1] + LAYER_4[2]) * LAYER_4[3]

            # Normalize combined waves to [0, 1].
            raw: float = (w1 + w2 + w3 + w4 + INTENSITY_OFFSET) / INTENSITY_SCALE
            intensity: float = max(0.0, min(1.0, raw))

            # Square the intensity to sharpen bands — aurora has bright
            # curtains separated by dark gaps.
            intensity = intensity * intensity

            # --- Hue: two independent waves select color from the palette ---
            hue_wave: float = math.sin(HUE_WAVE_1[0] * math.pi * x + phase * HUE_WAVE_1[1] + HUE_WAVE_1[2])
            hue_wave2: float = math.sin(HUE_WAVE_2[0] * math.pi * x + phase * HUE_WAVE_2[1] + HUE_WAVE_2[2]) * HUE_WAVE_2_AMP

            hue_blend: float = (hue_wave + hue_wave2 + HUE_OFFSET) / HUE_SCALE
            hue_blend = max(0.0, min(1.0, hue_blend))

            # Map blend factor to the aurora palette:
            #   0.0 = purple, 0.33 = blue, 0.66 = green, 1.0 = teal
            if hue_blend < BAND_1:
                f: float = hue_blend / BAND_1
                hue_deg: float = HUE_PURPLE + (HUE_BLUE - HUE_PURPLE) * f
            elif hue_blend < BAND_2:
                f = (hue_blend - BAND_1) / (BAND_2 - BAND_1)
                hue_deg = HUE_BLUE + (HUE_GREEN - HUE_BLUE) * f
            else:
                f = (hue_blend - BAND_2) / BAND_3
                hue_deg = HUE_GREEN + (HUE_TEAL - HUE_GREEN) * f

            hue: int = int(hue_deg * HSBK_MAX / 360.0) % (HSBK_MAX + 1)
            sat: int = HSBK_MAX
            bri: int = int(bg_b + (max_bri - bg_b) * intensity)

            colors.append((hue, sat, bri, self.kelvin))

        return colors
