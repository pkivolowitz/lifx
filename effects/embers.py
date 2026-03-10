"""Embers effect — convection simulation of rising, cooling embers.

Heat is randomly injected at the bottom of the string (zone 0).  Each
frame, the temperature field is diffused using a simple 1D averaging
kernel and multiplied by a cooling factor:

    T'[i] = (T[i-1] + T[i] + T[i+1]) / 3 × cooling

Temperature maps to a color gradient:
    0.0 → black  (cold/dead)
    0.3 → deep red
    0.6 → orange
    1.0 → bright yellow-white  (hottest)

The result looks like glowing embers rising from one end of the string,
cooling and dimming as they drift upward.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import random

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Color gradient control points (temperature → hue/saturation/brightness).
# Hue in degrees: 0=red, 30=orange, 50=yellow.
HUE_RED: float = 0.0
HUE_ORANGE: float = 30.0
HUE_YELLOW: float = 50.0

# Temperature thresholds for gradient mapping.
THRESH_BLACK: float = 0.05     # Below this → black (invisible).
THRESH_RED: float = 0.30       # Below this → black-to-red ramp.
THRESH_ORANGE: float = 0.60    # Below this → red-to-orange ramp.
# Above THRESH_ORANGE → orange-to-yellow ramp.

# Default zones per bulb (1 = per-zone, 3 = polychrome string lights).
DEFAULT_ZPB: int = 1


def _temp_to_hsbk(temp: float, max_bri: int, kelvin: int) -> HSBK:
    """Map a temperature value (0.0–1.0) to an HSBK color.

    The gradient proceeds: black → deep red → orange → bright yellow.

    Args:
        temp:    Normalised temperature, clamped to [0, 1].
        max_bri: Maximum brightness (from the user's brightness param).
        kelvin:  Color temperature in Kelvin.

    Returns:
        An HSBK tuple.
    """
    if temp < THRESH_BLACK:
        return (0, 0, 0, kelvin)

    if temp < THRESH_RED:
        # Black → deep red.
        frac: float = (temp - THRESH_BLACK) / (THRESH_RED - THRESH_BLACK)
        hue: int = hue_to_u16(HUE_RED)
        sat: int = HSBK_MAX
        bri: int = int(max_bri * 0.4 * frac)
    elif temp < THRESH_ORANGE:
        # Deep red → orange.
        frac = (temp - THRESH_RED) / (THRESH_ORANGE - THRESH_RED)
        hue_deg: float = HUE_RED + (HUE_ORANGE - HUE_RED) * frac
        hue = hue_to_u16(hue_deg)
        sat = HSBK_MAX
        bri = int(max_bri * (0.4 + 0.3 * frac))
    else:
        # Orange → bright yellow-white.
        frac = (temp - THRESH_ORANGE) / (1.0 - THRESH_ORANGE)
        frac = min(frac, 1.0)
        hue_deg = HUE_ORANGE + (HUE_YELLOW - HUE_ORANGE) * frac
        hue = hue_to_u16(hue_deg)
        # Saturation drops toward white at peak temperature.
        sat = int(HSBK_MAX * (1.0 - 0.3 * frac))
        bri = int(max_bri * (0.7 + 0.3 * frac))

    return (hue, sat, bri, kelvin)


class Embers(Effect):
    """Embers — convection simulation of rising, cooling embers.

    Heat is injected randomly at the bottom of the strip each frame.
    A 1D diffusion kernel with a cooling factor makes heat drift upward,
    dim, and die — like glowing embers in a chimney.
    """

    name: str = "embers"
    description: str = "Rising embers — heat diffusion with cooling gradient"

    intensity = Param(0.7, min=0.0, max=1.0,
                      description="Probability of heat injection per frame")
    cooling = Param(0.96, min=0.80, max=0.999,
                    description="Cooling factor per diffusion step (lower = faster fade)")
    brightness = Param(100, min=0, max=100,
                       description="Overall brightness percent")
    zones_per_bulb = Param(DEFAULT_ZPB, min=1, max=16,
                           description="Zones per physical bulb (3 for string lights)")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def __init__(self, **overrides: dict) -> None:
        """Initialise the embers effect with an empty heat buffer."""
        super().__init__(**overrides)
        self._heat: list[float] = []

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the embers convection simulation.

        Each call diffuses the temperature field, injects new heat at
        the bottom, and maps temperatures to the ember color gradient.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        zpb: int = self.zones_per_bulb
        bulb_count: int = max(1, zone_count // zpb)

        # Lazily initialise or resize the heat buffer to match bulb count.
        if len(self._heat) != bulb_count:
            self._heat = [0.0] * bulb_count

        heat: list[float] = self._heat

        # --- Inject heat at the bottom ------------------------------------
        if random.random() < self.intensity:
            heat[0] = min(heat[0] + random.uniform(0.5, 1.0), 1.0)

        # --- Diffuse: average each cell with its neighbours ---------------
        new_heat: list[float] = [0.0] * bulb_count
        for i in range(bulb_count):
            below: float = heat[i - 1] if i > 0 else 0.0
            above: float = heat[i + 1] if i < bulb_count - 1 else 0.0
            new_heat[i] = (below + heat[i] + above) / 3.0 * self.cooling

        # Clamp to [0, 1].
        self._heat = [max(0.0, min(1.0, h)) for h in new_heat]

        # --- Map temperature to color and expand to zones -----------------
        max_bri: int = pct_to_u16(self.brightness)
        colors: list[HSBK] = []
        for i in range(zone_count):
            bulb_index: int = i // zpb
            colors.append(_temp_to_hsbk(self._heat[bulb_index], max_bri, self.kelvin))

        return colors
