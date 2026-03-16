"""Primary cycle diagnostic — interpolate through R → G → B using the active color space.

Cycles through the three primary colors with smooth interpolation,
spending ``speed`` seconds per transition.  Useful for visually
comparing interpolation methods (``--lerp oklab`` vs ``lab`` vs ``hsb``)
and verifying color accuracy on hardware.

Usage::

    # Default: 1-second transitions through Oklab
    python3 glowup.py play _primary_cycle --ip <device-ip>

    # Compare interpolation methods side by side
    python3 glowup.py play _primary_cycle --ip <device-ip> --lerp oklab
    python3 glowup.py play _primary_cycle --ip <device-ip> --lerp lab
    python3 glowup.py play _primary_cycle --ip <device-ip> --lerp hsb

    # Slower transitions to study the gradient
    python3 glowup.py play _primary_cycle --ip <device-ip> --speed 3
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import os
import sys

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
)

# Import colorspace module from project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from colorspace import lerp_color

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Primary colors as HSBK tuples (full saturation, full brightness).
# Hue values: 0° = red, 120° = green, 240° = blue.
HUE_RED: int = 0
HUE_GREEN: int = int(120.0 / 360.0 * HSBK_MAX)
HUE_BLUE: int = int(240.0 / 360.0 * HSBK_MAX)

# Number of primary transitions per full cycle.
NUM_PRIMARIES: int = 3

# BT.709 relative luminance coefficients for sRGB primaries.
# Used to equalize perceived brightness across R, G, B.
LUMA_RED: float = 0.2126
LUMA_GREEN: float = 0.7152
LUMA_BLUE: float = 0.0722


class PrimaryCycle(Effect):
    """Interpolate through R → G → B → R using the active color space.

    Each transition takes ``speed`` seconds.  The full cycle is
    3 × speed seconds.  All zones show the same color — this is a
    temporal diagnostic, not a spatial one.
    """

    name: str = "_primary_cycle"
    description: str = "Interpolate R → G → B through the active color space"
    hidden: bool = True

    speed = Param(1.0, min=0.2, max=10.0,
                  description="Seconds per primary transition")
    hold = Param(0.5, min=0.0, max=5.0,
                 description="Seconds to hold each pure primary before transitioning")
    brightness = Param(100, min=0, max=100,
                       description="Brightness percent")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def period(self) -> float:
        """Full cycle period: 3 × (hold + transition)."""
        return (self.speed + self.hold) * NUM_PRIMARIES

    # Static transition order — no modular arithmetic, no ambiguity.
    # Each tuple is (from_color, to_color).
    _TRANSITIONS: list[tuple[str, str]] = [
        ("RED", "GREEN"),
        ("GREEN", "BLUE"),
        ("BLUE", "RED"),
    ]

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame — all zones show the same interpolated color.

        The order is always: RED → GREEN → BLUE → RED, repeating.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* identical HSBK tuples.
        """
        base_bri: float = self.brightness / 100.0

        # Equalize perceived brightness using BT.709 luma.
        # Scale each primary so perceived luminance matches.
        # Blue is the dimmest primary — it goes to max, others reduce.
        luma_min: float = LUMA_BLUE  # Weakest primary sets the ceiling.
        bri_r: int = int(base_bri * (luma_min / LUMA_RED) * HSBK_MAX)
        bri_g: int = int(base_bri * (luma_min / LUMA_GREEN) * HSBK_MAX)
        bri_b: int = int(base_bri * HSBK_MAX)  # Blue at full requested brightness.

        # Fixed primary colors — always in this order.
        red: HSBK = (HUE_RED, HSBK_MAX, bri_r, self.kelvin)
        green: HSBK = (HUE_GREEN, HSBK_MAX, bri_g, self.kelvin)
        blue: HSBK = (HUE_BLUE, HSBK_MAX, bri_b, self.kelvin)
        sequence: list[HSBK] = [red, green, blue]

        # Each segment is: hold (pure color) + transition (interpolate).
        segment_len: float = self.hold + self.speed
        pos: float = t % self.period()
        idx: int = min(int(pos / segment_len), NUM_PRIMARIES - 1)
        within: float = pos - idx * segment_len

        if within < self.hold:
            # Hold phase — pure primary.
            color: HSBK = sequence[idx]
        else:
            # Transition phase — interpolate to next primary.
            blend: float = (within - self.hold) / self.speed
            color = lerp_color(sequence[idx],
                               sequence[(idx + 1) % NUM_PRIMARIES],
                               blend)

        return [color] * zone_count
