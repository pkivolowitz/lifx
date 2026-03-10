"""Zone map — diagnostic strobe that reveals zone positions within bulbs.

Cycles through zone offsets within each bulb group at a configurable
rate.  With the default stride of 3, it lights zones 0, 3, 6, ...
then zones 1, 4, 7, ... then zones 2, 5, 8, ... in a repeating loop.
Each phase uses a distinct color (red, green, blue, ...) so the
physical cylinder layers are easily distinguishable.  Unlit zones
are black.

This makes it easy to see which physical LED position inside a
polychrome bulb corresponds to each zone index.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Black — used for unlit zones.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Default stride matches LIFX polychrome string lights (3 zones/bulb).
DEFAULT_STRIDE: int = 3

# How long each phase is displayed before advancing.
DEFAULT_HOLD_SECONDS: float = 1.5

# Phase colors — each zone offset gets a distinct color so concentric
# cylinders are easily distinguishable.  Extends beyond 3 for larger
# strides; wraps if stride exceeds the palette length.
PHASE_COLORS: list[HSBK] = [
    (0,     HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT),  # Red     (0°)
    (21845, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT),  # Green   (120°)
    (43690, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT),  # Blue    (240°)
    (10922, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT),  # Yellow  (60°)
    (54613, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT),  # Magenta (300°)
    (32768, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT),  # Cyan    (180°)
]


class ZoneMap(Effect):
    """Diagnostic strobe that lights one zone offset at a time.

    With stride=3, cycles through three phases, each in a distinct color:
      Phase 0: zones 0, 3, 6, 9, ... lit RED   — all others black
      Phase 1: zones 1, 4, 7, 10, ... lit GREEN — all others black
      Phase 2: zones 2, 5, 8, 11, ... lit BLUE  — all others black

    Watch the physical bulbs to see which concentric cylinder lights
    up in each phase.
    """

    name: str = "_zone_map"
    description: str = "Diagnostic R/G/B strobe cycling through zone offsets within each bulb"

    stride = Param(DEFAULT_STRIDE, min=2, max=16,
                   description="Number of zones per bulb (stride between lit zones)")
    hold = Param(DEFAULT_HOLD_SECONDS, min=0.3, max=10.0,
                 description="Seconds each phase is held before advancing")
    solo = Param(0, min=-1, max=15,
                 description="Solo one zone offset (-1=off, 0-2=solo that zone with rainbow)")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame — only zones matching the current phase are lit.

        Each phase uses a distinct color from the PHASE_COLORS palette
        so the concentric cylinder layers are immediately identifiable.

        When solo >= 0, only that zone offset is lit and its hue rotates
        through the full spectrum — all other zones are black.  This tests
        whether a single zone can produce any color independently.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        # --- Solo mode: one zone offset, rotating hue ---
        if self.solo >= 0:
            # Full hue rotation every 6 seconds.
            hue_degrees: float = (t / 6.0 * 360.0) % 360.0
            lit: HSBK = (
                hue_to_u16(hue_degrees),
                HSBK_MAX,
                HSBK_MAX,
                KELVIN_DEFAULT,
            )
            colors: list[HSBK] = []
            for i in range(zone_count):
                if i % self.stride == self.solo:
                    colors.append(lit)
                else:
                    colors.append(BLACK)
            return colors

        # --- Normal mode: cycle phases with distinct R/G/B colors ---
        phase: int = int(t / self.hold) % self.stride

        # Pick the color for this phase, wrapping if stride > palette size.
        lit = PHASE_COLORS[phase % len(PHASE_COLORS)]

        colors = []
        for i in range(zone_count):
            if i % self.stride == phase:
                colors.append(lit)
            else:
                colors.append(BLACK)
        return colors
