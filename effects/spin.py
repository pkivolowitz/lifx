"""Spin effect — migrates colors between the concentric rings of each bulb.

Each LIFX polychrome bulb is three concentric light-guide tubes nested
inside one shared diffuser — inner, middle, and outer rings (zones 0–2).
Spin cycles colors through these rings so hues appear to migrate between
the inside and outside of every bulb simultaneously.

Colors are selected from the shared palette system (50 named presets).
The "custom" palette uses three evenly-spaced rainbow hues (red, green,
blue at full saturation).  All palettes interpolate smoothly via CIELAB.

The ``zones_per_bulb`` parameter (default 3) controls how zones are
grouped into logical bulbs.  This must match the physical hardware —
LIFX string lights use 3 zones per bulb.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.2"

from . import (
    DEVICE_TYPE_STRIP,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)
from .rule_trio import PALETTES, PALETTE_NAMES
from colorspace import lerp_color

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default number of zones per physical bulb (LIFX polychrome string lights).
ZONES_PER_BULB_DEFAULT: int = 3

# "custom" palette: evenly-spaced rainbow hues (red, green, blue) at full sat.
CUSTOM_PALETTE: tuple[float, float, float, int] = (0.0, 120.0, 240.0, 100)


class Spin(Effect):
    """Migrate colors through the concentric rings of each polychrome bulb.

    Each bulb's three concentric rings (inner, middle, outer) cycle
    through a set of colors, creating the appearance of hues flowing
    between the inside and outside of every bulb.  Adjacent bulbs are
    offset so the whole string shimmers.

    All rendering uses CIELAB interpolation for perceptually smooth
    transitions.  The "custom" palette provides an evenly-spaced
    rainbow (red / green / blue).
    """

    name: str = "spin"
    description: str = "Migrate colors through the concentric rings of each bulb"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_STRIP})

    speed = Param(2.0, min=0.2, max=30.0,
                  description="Seconds per full rotation")
    brightness = Param(100, min=0, max=100,
                       description="Brightness percent")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")
    palette = Param(
        "custom",
        description="Colour preset — 50 named palettes or rainbow default",
        choices=sorted(PALETTE_NAMES.keys()),
    )
    bulb_offset = Param(30.0, min=0.0, max=360.0,
                        description="Hue offset in degrees between adjacent bulbs")
    zones_per_bulb = Param(ZONES_PER_BULB_DEFAULT, min=1, max=16,
                           description="Number of zones per physical bulb")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame — each bulb's rings cycle through colors.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        zpb: int = self.zones_per_bulb
        bri: int = pct_to_u16(self.brightness)

        # Rotation phase: 0.0 to 1.0 over the speed period.
        phase: float = (t / self.speed) % 1.0

        # Resolve palette — named preset or built-in rainbow.
        palette_key: str = str(self.palette)
        ha, hb, hc, sat_pct = PALETTES.get(palette_key, CUSTOM_PALETTE)
        sat: int = pct_to_u16(sat_pct)

        # Pre-build the three HSBK anchors.
        anchors: list[HSBK] = [
            (hue_to_u16(ha), sat, bri, self.kelvin),
            (hue_to_u16(hb), sat, bri, self.kelvin),
            (hue_to_u16(hc), sat, bri, self.kelvin),
        ]

        # How many color slots to shift per bulb (120° = 1 slot).
        bulb_color_shift: float = self.bulb_offset / 120.0

        colors: list[HSBK] = []
        for i in range(zone_count):
            bulb_index: int = i // zpb
            zone_in_bulb: int = i % zpb

            # Continuous position in the 3-color cycle.
            slot: float = zone_in_bulb + phase * zpb + bulb_index * bulb_color_shift
            slot_mod: float = slot % 3.0
            idx_a: int = int(slot_mod)
            idx_b: int = (idx_a + 1) % 3
            frac: float = slot_mod - idx_a

            # Perceptually smooth interpolation via CIELAB.
            colors.append(lerp_color(anchors[idx_a], anchors[idx_b], frac))

        return colors
