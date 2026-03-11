"""Spin effect — migrates colors between the concentric rings of each bulb.

Each LIFX polychrome bulb is three concentric light-guide tubes nested
inside one shared diffuser — inner, middle, and outer rings (zones 0–2).
Spin cycles colors through these rings so hues appear to migrate between
the inside and outside of every bulb simultaneously.

Supports the shared palette system from ``rule_trio``: selecting a
named palette provides three curated colors that flow smoothly through
the rings via CIELAB interpolation.  The "custom" palette preserves
continuous-hue behaviour controlled by ``base_hue``, ``hue_spread``,
and ``bulb_offset``.

The ``zones_per_bulb`` parameter (default 3) controls how zones are
grouped into logical bulbs.  This must match the physical hardware —
LIFX string lights use 3 zones per bulb.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import math

from . import (
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


class Spin(Effect):
    """Migrate colors through the concentric rings of each polychrome bulb.

    Each bulb's three concentric rings (inner, middle, outer) cycle
    through a set of colors, creating the appearance of hues flowing
    between the inside and outside of every bulb.  Adjacent bulbs are
    offset so the whole string shimmers.

    When a palette preset is active, three curated colors flow through
    the rings with CIELAB interpolation for smooth transitions.  The
    "custom" palette preserves manual ``base_hue`` / ``hue_spread`` /
    ``bulb_offset`` behaviour.
    """

    name: str = "spin"
    description: str = "Migrate colors through the concentric rings of each bulb"

    speed = Param(2.0, min=0.2, max=30.0,
                  description="Seconds per full rotation")
    brightness = Param(100, min=0, max=100,
                       description="Brightness percent")
    saturation = Param(100, min=0, max=100,
                       description="Saturation percent (custom palette only)")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")
    palette = Param(
        "custom",
        description="Colour preset (non-custom overrides hue/sat params)",
        choices=sorted(PALETTE_NAMES.keys()),
    )
    hue_spread = Param(120.0, min=10.0, max=360.0,
                       description="Hue spread in degrees across zones (custom palette)")
    base_hue = Param(0.0, min=0.0, max=360.0,
                     description="Starting hue in degrees (custom palette)")
    bulb_offset = Param(30.0, min=0.0, max=360.0,
                        description="Hue offset in degrees between adjacent bulbs")
    zones_per_bulb = Param(ZONES_PER_BULB_DEFAULT, min=1, max=16,
                           description="Number of zones per physical bulb")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame — each bulb's zones rotate through colors.

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

        palette_key: str = str(self.palette)

        # --- Palette mode: 3 discrete colors rotate through zones ----------
        if palette_key in PALETTES:
            return self._render_palette(zone_count, zpb, bri, phase, palette_key)

        # --- Custom mode: continuous hue math (original behaviour) ---------
        return self._render_custom(zone_count, zpb, bri, phase)

    def _render_palette(
        self,
        zone_count: int,
        zpb: int,
        bri: int,
        phase: float,
        palette_key: str,
    ) -> list[HSBK]:
        """Render using a named palette's three fixed hues.

        Each bulb's zones smoothly interpolate between palette colors
        via ``lerp_color`` (CIELAB by default) — no hard snaps.  The
        ``bulb_offset`` parameter shifts the starting position between
        adjacent bulbs (every 120° advances one color slot).

        Args:
            zone_count:  Number of zones on the target device.
            zpb:         Zones per physical bulb.
            bri:         Brightness in HSBK units (0–65535).
            phase:       Rotation phase (0.0–1.0).
            palette_key: Key into ``PALETTES``.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        ha, hb, hc, sat_pct = PALETTES[palette_key]
        sat: int = pct_to_u16(sat_pct)

        # Pre-build the three palette HSBK anchors.
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

    def _render_custom(
        self,
        zone_count: int,
        zpb: int,
        bri: int,
        phase: float,
    ) -> list[HSBK]:
        """Render using continuous hue math (original behaviour).

        Args:
            zone_count: Number of zones on the target device.
            zpb:        Zones per physical bulb.
            bri:        Brightness in HSBK units (0–65535).
            phase:      Rotation phase (0.0–1.0).

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        sat: int = pct_to_u16(self.saturation)

        colors: list[HSBK] = []
        for i in range(zone_count):
            bulb_index: int = i // zpb
            zone_in_bulb: int = i % zpb

            # Each zone within a bulb gets an evenly-spaced hue offset.
            # The phase rotates all zones together.
            zone_fraction: float = (zone_in_bulb + phase * zpb) / zpb
            hue_degrees: float = (
                self.base_hue
                + zone_fraction * self.hue_spread
                + bulb_index * self.bulb_offset
            )
            hue: int = hue_to_u16(hue_degrees % 360.0)
            colors.append((hue, sat, bri, self.kelvin))

        return colors
