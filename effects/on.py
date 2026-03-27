"""On effect — set devices to a static color.

A non-animating effect that holds a constant color at a fixed brightness.
Designed for schedule entries that simply turn lights on, but works from
the CLI too.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

from typing import Optional

from . import (
    Effect, Param, HSBK,
    KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Named color definitions: (hue_degrees, saturation_percent).
# "white" is special — saturation 0 lets the kelvin parameter control
# color temperature (warm amber through cool daylight).
_COLORS: dict[str, tuple[float, int]] = {
    "white":  (0.0, 0),
    "red":    (0.0, 100),
    "orange": (30.0, 100),
    "yellow": (60.0, 100),
    "green":  (120.0, 100),
    "cyan":   (180.0, 100),
    "blue":   (240.0, 100),
    "purple": (280.0, 100),
    "pink":   (320.0, 100),
}

# Sorted for deterministic choices display.
_COLOR_NAMES: list[str] = sorted(_COLORS.keys())


class On(Effect):
    """Set all zones to a single static color.

    Renders the same HSBK value every frame.  When used from a schedule,
    the lights stay at the requested color and brightness until the
    scheduler transitions to the next entry.
    """

    name: str = "on"
    description: str = "Static color — turn lights on"

    brightness = Param(100, min=0, max=100,
                       description="Brightness percent")
    color = Param("white", choices=_COLOR_NAMES,
                  description="Named color")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin (white only)")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame — every zone gets the same constant color.

        Args:
            t:          Seconds elapsed (unused — static effect).
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* identical HSBK tuples.
        """
        hue_deg, sat_pct = _COLORS.get(self.color, _COLORS["white"])
        hue: int = hue_to_u16(hue_deg)
        sat: int = pct_to_u16(sat_pct)
        bri: int = pct_to_u16(self.brightness)
        pixel: HSBK = (hue, sat, bri, self.kelvin)
        return [pixel] * zone_count

    def period(self) -> Optional[float]:
        """Static effect — no repeating cycle.

        Returns:
            ``None`` (aperiodic).
        """
        return None
