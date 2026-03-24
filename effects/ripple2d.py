"""Ripple — interfering concentric rings on a 2D grid.

Multiple ripple sources spawn at random positions within the virtual
coordinate space.  Each emits a sine wave in [-1, +1].  Waves sum at
every pixel — constructive interference produces bright peaks,
destructive interference cancels to black.  Sources expire and
respawn to keep the pattern evolving.

The virtual space extends well beyond the physical grid so origins
can be off-screen.  A single Luna sees partial arcs; a wall of Lunas
sharing coordinate offsets sees one unified interference pattern.

Computes on a full rectangular grid.  When ``--luna`` is enabled,
the four dead corner pixels are blacked out after rendering.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "2.0"

import math
import random

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

# How far outside the grid a source can spawn (grid units).
SPAWN_MARGIN: float = 3.0

# Minimum / maximum source lifetime before respawn (seconds).
MIN_LIFETIME: float = 4.0
MAX_LIFETIME: float = 10.0


class Ripple2D(Effect):
    """Interfering concentric rings from multiple random sources.

    Each source emits a sine wave in [-1, +1].  Waves sum and the
    absolute value drives brightness — constructive peaks are bright,
    destructive nodes are black.  Sources respawn at random positions
    to keep the pattern alive and evolving.
    """

    name: str = "ripple2d"
    description: str = "Interfering concentric ripples on a 2D grid"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    width = Param(DEFAULT_WIDTH, min=1, max=500,
                  description="Grid width in pixels (columns)")
    height = Param(DEFAULT_HEIGHT, min=1, max=300,
                   description="Grid height in pixels (rows)")
    sources = Param(2, min=1, max=8,
                    description="Number of simultaneous ripple sources")
    speed = Param(4.0, min=0.1, max=30.0,
                  description="Ring expansion speed (units per second)")
    wavelength = Param(2.5, min=0.5, max=10.0,
                       description="Distance between ring peaks (grid units)")
    hue = Param(200.0, min=0.0, max=360.0,
                description="Base hue in degrees (0-360)")
    hue_spread = Param(0.0, min=0.0, max=360.0,
                       description="Hue shift per grid unit from nearest source (0=mono)")
    brightness = Param(100, min=1, max=100,
                       description="Peak brightness (percent)")
    luna = Param(0, min=0, max=1,
                 description="Black out Luna dead corners (1=yes)")

    def on_start(self, zone_count: int) -> None:
        """Spawn initial ripple sources at random positions.

        Args:
            zone_count: Number of zones on the target device.
        """
        self._sources: list[tuple[float, float, float]] = []
        for _ in range(int(self.sources)):
            self._sources.append(self._new_source(0.0))

    def _new_source(self, t: float) -> tuple[float, float, float]:
        """Create a source at a random position with a random expiry.

        Args:
            t: Current time in seconds.

        Returns:
            Tuple of (x, y, expiry_time).
        """
        w: int = int(self.width)
        h: int = int(self.height)
        x: float = random.uniform(-SPAWN_MARGIN, w - 1 + SPAWN_MARGIN)
        y: float = random.uniform(-SPAWN_MARGIN, h - 1 + SPAWN_MARGIN)
        expiry: float = t + random.uniform(MIN_LIFETIME, MAX_LIFETIME)
        return (x, y, expiry)

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of interfering ripples.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels (ignored — uses width * height).

        Returns:
            A list of ``width * height`` HSBK tuples in row-major order.
        """
        w: int = int(self.width)
        h: int = int(self.height)
        total: int = w * h
        n: int = int(self.sources)

        spd: float = float(self.speed)
        wl: float = float(self.wavelength)
        base_hue_deg: float = float(self.hue)
        spread: float = float(self.hue_spread)
        bri_max: int = pct_to_u16(self.brightness)
        sat: int = HSBK_MAX

        # Spatial frequency (radians per grid unit).
        k: float = TWO_PI / wl

        # Respawn expired sources.
        for i in range(len(self._sources)):
            if t >= self._sources[i][2]:
                self._sources[i] = self._new_source(t)

        colors: list[HSBK] = [BLACK] * total

        for row in range(h):
            for col in range(w):
                # Sum waves from all sources.
                wave_sum: float = 0.0
                min_dist: float = 1e9
                for sx, sy, _ in self._sources:
                    dx: float = col - sx
                    dy: float = row - sy
                    dist: float = math.sqrt(dx * dx + dy * dy)
                    if dist < min_dist:
                        min_dist = dist
                    # Raw sine in [-1, +1].
                    wave_sum += math.sin(k * dist - spd * t)

                # abs(sum/n): zero-crossings → black, both peaks → bright.
                # Cube for non-linear contrast — crushes lows, punches highs.
                intensity: float = abs(wave_sum / n)
                intensity = intensity * intensity * intensity

                bri: int = int(bri_max * intensity)
                if bri < 1:
                    continue

                # Hue: base + optional spread by distance to nearest source.
                hue_deg: float = base_hue_deg + spread * min_dist
                hue_u16: int = hue_to_u16(hue_deg % 360.0)

                idx: int = row * w + col
                colors[idx] = (hue_u16, sat, bri, KELVIN_DEFAULT)

        # Luna dead corner mask.
        if int(self.luna):
            for r, c in LUNA_DEAD_ZONES:
                idx = r * w + c
                if idx < total:
                    colors[idx] = BLACK

        return colors
