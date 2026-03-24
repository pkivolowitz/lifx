"""Ripple — expanding concentric rings on a 2D grid.

Rings radiate outward from a configurable origin point (cx, cy).
The origin can be placed anywhere — inside the grid, at the edge,
or well outside it — so a single Luna sees a partial arc, and a
wall of Lunas sharing the same coordinate space sees one unified
wavefront.

Computes on a full rectangular grid.  When ``--luna`` is enabled,
the four dead corner pixels are blacked out after rendering.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import math

from . import (
    DEVICE_TYPE_MATRIX,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Black — dead pixels and empty space.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Default grid dimensions — Luna protocol grid.
DEFAULT_WIDTH: int = 7
DEFAULT_HEIGHT: int = 5

# Luna dead corners — (row, col) positions with no physical LED.
LUNA_DEAD_ZONES: frozenset[tuple[int, int]] = frozenset({
    (0, 0), (0, 6), (4, 0), (4, 6),
})

# Two-pi constant.
TWO_PI: float = 2.0 * math.pi


class Ripple2D(Effect):
    """Expanding concentric rings from an arbitrary origin point.

    Each pixel's brightness is driven by a sine wave over its
    distance from (cx, cy), scrolled outward over time.  Rings
    fade with distance and can optionally shift hue to produce
    rainbow ripples.
    """

    name: str = "ripple2d"
    description: str = "Concentric ripple rings on a 2D grid"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    width = Param(DEFAULT_WIDTH, min=1, max=500,
                  description="Grid width in pixels (columns)")
    height = Param(DEFAULT_HEIGHT, min=1, max=300,
                   description="Grid height in pixels (rows)")
    cx = Param(3.0, min=-21.0, max=21.0,
               description="Ring center X (column, float — can be off-grid)")
    cy = Param(2.0, min=-15.0, max=15.0,
               description="Ring center Y (row, float — can be off-grid)")
    speed = Param(4.0, min=0.1, max=30.0,
                  description="Ring expansion speed (units per second)")
    wavelength = Param(2.5, min=0.5, max=10.0,
                       description="Distance between ring peaks (grid units)")
    decay = Param(0.3, min=0.0, max=1.0,
                  description="Ring fade-out rate (0=no fade, 1=fast fade)")
    hue = Param(200.0, min=0.0, max=360.0,
                description="Base hue in degrees (0-360)")
    hue_spread = Param(0.0, min=0.0, max=360.0,
                       description="Hue shift per grid unit of distance (0=mono)")
    brightness = Param(80, min=1, max=100,
                       description="Peak brightness (percent)")
    luna = Param(0, min=0, max=1,
                 description="Black out Luna dead corners (1=yes)")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of expanding rings.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels (ignored — uses width * height).

        Returns:
            A list of ``width * height`` HSBK tuples in row-major order.
        """
        w: int = int(self.width)
        h: int = int(self.height)
        total: int = w * h

        origin_x: float = float(self.cx)
        origin_y: float = float(self.cy)
        spd: float = float(self.speed)
        wl: float = float(self.wavelength)
        dk: float = float(self.decay)
        base_hue_deg: float = float(self.hue)
        spread: float = float(self.hue_spread)
        bri_max: int = pct_to_u16(self.brightness)
        sat: int = HSBK_MAX

        # Spatial frequency (radians per grid unit).
        k: float = TWO_PI / wl

        colors: list[HSBK] = [BLACK] * total

        for row in range(h):
            dy: float = row - origin_y
            dy2: float = dy * dy
            for col in range(w):
                dx: float = col - origin_x
                dist: float = math.sqrt(dx * dx + dy2)

                # Sine wave: peaks move outward over time.
                phase: float = k * dist - spd * t
                wave: float = (math.sin(phase) + 1.0) * 0.5  # 0..1

                # Distance decay.
                if dk > 0.0 and dist > 0.0:
                    atten: float = 1.0 / (1.0 + dk * dist * dist)
                else:
                    atten = 1.0

                bri: int = int(bri_max * wave * atten)
                if bri < 1:
                    continue

                # Hue: base + optional spread by distance.
                hue_deg: float = base_hue_deg + spread * dist
                hue_u16: int = hue_to_u16(hue_deg % 360.0)

                idx: int = row * w + col
                colors[idx] = (hue_u16, sat, bri, KELVIN_DEFAULT)

        # Luna dead corner mask.
        if int(self.luna):
            for row, col in LUNA_DEAD_ZONES:
                idx = row * w + col
                if idx < total:
                    colors[idx] = BLACK

        return colors
