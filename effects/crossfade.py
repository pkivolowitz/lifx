"""Crossfade A/B/C test — compare color transition methods.

Cycles through red → green → blue three times per loop:

  **Method A (HSB flat):** All zones transition in lockstep by
  interpolating the HSB hue channel.  Naive color wheel traversal.

  **Method B (HSB staggered):** Same HSB hue interpolation but with
  concentric zone lag — inner zone leads, outer trails.

  **Method C (Lab staggered):** Interpolation through CIELAB perceptual
  color space with the same concentric zone lag.  CIELAB was designed
  so that equal numeric distances produce equal perceived color
  differences, avoiding the brightness dips and muddy intermediates
  of HSB hue interpolation.

This lets you directly compare all three approaches on physical hardware
and see the perceptual difference.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.0"

import math
import subprocess
import sys
import os

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# Import colorspace module from project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from colorspace import hsbk_to_lab, lab_to_hsbk

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Halfway point of the HSBK hue range — shortest-path threshold.
HUE_HALFWAY: int = HSBK_MAX // 2

# Default zones per bulb for LIFX polychrome string lights.
DEFAULT_ZPB: int = 3

# The three test hues in degrees: red, green, blue.
TEST_HUES: list[float] = [0.0, 120.0, 240.0]

# BT.709 luminous efficiency coefficients — how strongly the human
# visual system responds to each primary at equal physical intensity.
# Green dominates (~71%), red is moderate (~21%), blue is weak (~7%).
LUMA_R: float = 0.2126
LUMA_G: float = 0.7152
LUMA_B: float = 0.0722

# Per-hue brightness scale factors to achieve equal perceived luminance.
# Computed as: target_luma / channel_luma, normalized so the weakest
# (blue) stays at 1.0 and the others are attenuated.
# This makes red, green, and blue look equally bright to the human eye.
_TARGET_LUMA: float = LUMA_B  # Normalize to the weakest primary.
HUE_BRIGHTNESS: list[float] = [
    _TARGET_LUMA / LUMA_R,    # Red:   0.0722 / 0.2126 ≈ 0.340
    _TARGET_LUMA / LUMA_G,    # Green: 0.0722 / 0.7152 ≈ 0.101
    _TARGET_LUMA / LUMA_B,    # Blue:  0.0722 / 0.0722 = 1.000
]

# Number of color transitions per method (R→G, G→B, B→R).
TRANSITIONS_PER_METHOD: int = len(TEST_HUES)

# Number of methods in the A/B comparison (A=HSB flat, B=Lab staggered).
METHOD_COUNT: int = 2

# Spoken labels for each method — announced via macOS `say` at transitions.
METHOD_LABELS: list[str] = ["A", "B"]


def _lerp_hue(h1_u16: float, h2_u16: float, blend: float) -> int:
    """Interpolate between two hues via the shortest path around the wheel.

    Args:
        h1_u16: Start hue in HSBK units (0–65535).
        h2_u16: End hue in HSBK units (0–65535).
        blend:  Blend factor 0.0 (pure h1) to 1.0 (pure h2).

    Returns:
        Interpolated hue in HSBK units.
    """
    diff: float = h2_u16 - h1_u16
    if diff > HUE_HALFWAY:
        diff -= (HSBK_MAX + 1)
    elif diff < -HUE_HALFWAY:
        diff += (HSBK_MAX + 1)
    return int(h1_u16 + diff * blend) % (HSBK_MAX + 1)


class Crossfade(Effect):
    """A/B comparison of HSB-flat vs Lab-staggered color transitions.

    Each method runs through R→G→B (3 transitions), then the next method
    takes over.  One full loop = 6 transitions, then repeats.
    """

    name: str = "_crossfade"
    description: str = "A/B test: HSB flat vs Lab staggered color transitions"

    speed = Param(2.0, min=0.5, max=30.0,
                  description="Seconds per color transition")
    lag = Param(0.3, min=0.05, max=0.9,
                description="Zone lag as fraction of transition (0=flat, 0.5=half cycle)")
    brightness = Param(100, min=0, max=100,
                       description="Brightness percent")
    saturation = Param(100, min=0, max=100,
                       description="Saturation percent")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")
    zones_per_bulb = Param(DEFAULT_ZPB, min=1, max=16,
                           description="Number of zones per physical bulb")

    def __init__(self) -> None:
        """Precompute CIELAB coordinates for the test color endpoints."""
        super().__init__()
        self._lab_cache: dict[int, tuple[float, float, float]] = {}
        self._last_method: int = -1  # Track method changes for announcements.

    def _get_lab(self, hue_u16: int, sat_u16: int, bri_u16: int) -> tuple[float, float, float]:
        """Return cached Lab coordinates for an HSBK color.

        Avoids redundant HSB→sRGB→XYZ→Lab conversion every frame.

        Args:
            hue_u16: LIFX hue (0–65535).
            sat_u16: LIFX saturation (0–65535).
            bri_u16: LIFX brightness (0–65535).

        Returns:
            Tuple of (L*, a*, b*) in CIELAB space.
        """
        key: int = (hue_u16 << 32) | (sat_u16 << 16) | bri_u16
        if key not in self._lab_cache:
            self._lab_cache[key] = hsbk_to_lab(hue_u16, sat_u16, bri_u16)
        return self._lab_cache[key]

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame — method A or B depending on position in loop.

        The full loop has 6 transition slots:
          Slots 0–2: Method A — HSB flat     (R→G, G→B, B→R)
          Slots 3–5: Method B — Lab staggered (R→G, G→B, B→R)

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        zpb: int = self.zones_per_bulb
        sat_u16: int = pct_to_u16(self.saturation)
        bri_u16: int = pct_to_u16(self.brightness)

        # Total slots in one full A/B loop.
        total_slots: int = TRANSITIONS_PER_METHOD * METHOD_COUNT

        # Which slot are we in, and how far through it?
        slot_progress: float = t / self.speed
        slot_index: int = int(slot_progress) % total_slots
        blend: float = slot_progress - int(slot_progress)

        # Which method and which color transition?
        method: int = slot_index // TRANSITIONS_PER_METHOD  # 0=A, 1=B
        color_index: int = slot_index % TRANSITIONS_PER_METHOD

        # Announce method changes via macOS text-to-speech (non-blocking).
        if method != self._last_method:
            self._last_method = method
            label: str = METHOD_LABELS[method]
            subprocess.Popen(
                ["say", label],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        # Source and destination indices.
        next_color_index: int = (color_index + 1) % len(TEST_HUES)

        # Source and destination hues in HSBK units.
        hue_from_u16: int = hue_to_u16(TEST_HUES[color_index])
        hue_to_u16_val: int = hue_to_u16(TEST_HUES[next_color_index])

        # Per-hue brightness compensation for equal perceived luminance.
        bri_from: int = int(bri_u16 * HUE_BRIGHTNESS[color_index])
        bri_to: int = int(bri_u16 * HUE_BRIGHTNESS[next_color_index])

        # For Lab method, precompute Lab endpoints (cached) with
        # compensated brightness already applied.
        if method == 1:
            lab_from: tuple[float, float, float] = self._get_lab(hue_from_u16, sat_u16, bri_from)
            lab_to: tuple[float, float, float] = self._get_lab(hue_to_u16_val, sat_u16, bri_to)

        colors: list[HSBK] = []
        for i in range(zone_count):
            zone_in_bulb: int = i % zpb

            if method == 0:
                # Method A: HSB flat — all zones same blend, no stagger.
                zone_blend: float = blend
                hue: int = _lerp_hue(hue_from_u16, hue_to_u16_val, zone_blend)
                bri: int = int(bri_from + (bri_to - bri_from) * zone_blend)
                colors.append((hue, sat_u16, bri, self.kelvin))
            else:
                # Method B: Lab staggered — inner leads, outer trails.
                zone_blend = blend - zone_in_bulb * self.lag
                denominator: float = 1.0 - (zpb - 1) * self.lag
                if denominator > 0.0:
                    zone_blend = zone_blend / denominator
                zone_blend = max(0.0, min(1.0, zone_blend))

                L: float = lab_from[0] + (lab_to[0] - lab_from[0]) * zone_blend
                a: float = lab_from[1] + (lab_to[1] - lab_from[1]) * zone_blend
                b: float = lab_from[2] + (lab_to[2] - lab_from[2]) * zone_blend
                colors.append(lab_to_hsbk(L, a, b, self.kelvin))

        return colors
