"""Matrix rain — falling green streams on a 2D grid.

Digital rain inspired by The Matrix.  Each column independently spawns
falling trails of green characters (represented as brightness
variations).  Trails have a bright white head and a fading green tail.

Computes on a full rectangular grid.  When ``--luna`` is enabled, the
four dead corner pixels are blacked out after rendering.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

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

# Matrix green hue in degrees.
MATRIX_GREEN_HUE_DEG: float = 120.0

# Head of a falling trail is white-green (low saturation).
HEAD_SATURATION_PCT: int = 30

# Body of a trail is fully saturated green.
BODY_SATURATION_PCT: int = 100

# Luna dead corners — (row, col) positions with no physical LED.
LUNA_DEAD_ZONES: frozenset[tuple[int, int]] = frozenset({
    (0, 0), (0, 6), (4, 0), (4, 6),
})

# Minimum interval between trail spawns (seconds).
MIN_SPAWN_INTERVAL: float = 0.3

# Maximum interval between trail spawns (seconds).
MAX_SPAWN_INTERVAL: float = 1.5


class MatrixRain(Effect):
    """Digital rain — falling green trails on a 2D matrix grid.

    Each column independently spawns trails that fall downward.
    The trail head is a bright white-green; the tail fades to dark
    green then black.  Columns spawn at random intervals for an
    organic, asynchronous look.
    """

    name: str = "matrix_rain"
    description: str = "Falling green digital rain (The Matrix)"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    width = Param(DEFAULT_WIDTH, min=1, max=500,
                  description="Grid width in pixels (columns)")
    height = Param(DEFAULT_HEIGHT, min=1, max=300,
                   description="Grid height in pixels (rows)")
    speed = Param(10.0, min=0.5, max=20.0,
                  description="Rows per second (fall speed)")
    tail = Param(2, min=1, max=10,
                 description="Trail tail length in rows")
    brightness = Param(80, min=1, max=100,
                       description="Peak brightness (percent)")
    luna = Param(0, min=0, max=1,
                 description="Black out Luna dead corners (1=yes)")

    def on_start(self, zone_count: int) -> None:
        """Seed per-column trail state.

        Args:
            zone_count: Number of zones on the target device.
        """
        w: int = int(self.width)
        # Each column tracks: current head row (float), active flag,
        # and next spawn time.
        self._heads: list[float] = [-1.0] * w
        self._active: list[bool] = [False] * w
        self._next_spawn: list[float] = [
            random.uniform(0.0, MAX_SPAWN_INTERVAL) for _ in range(w)
        ]
        self._last_t: float = 0.0

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of falling rain.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels (ignored — uses width * height).

        Returns:
            A list of ``width * height`` HSBK tuples in row-major order.
        """
        w: int = int(self.width)
        h: int = int(self.height)
        total: int = w * h
        # Guard: on_start() initializes state.  If render() is called
        # before on_start() (e.g., in tests), return black.
        if not hasattr(self, "_last_t"):
            return [(0, 0, 0, KELVIN_DEFAULT)] * total
        dt: float = t - self._last_t
        self._last_t = t

        fall_speed: float = float(self.speed)
        tail_len: int = int(self.tail)
        bri_max: int = pct_to_u16(self.brightness)
        hue: int = hue_to_u16(MATRIX_GREEN_HUE_DEG)
        head_sat: int = pct_to_u16(HEAD_SATURATION_PCT)
        body_sat: int = pct_to_u16(BODY_SATURATION_PCT)

        # Advance heads and spawn new trails.
        for col in range(w):
            if self._active[col]:
                self._heads[col] += dt * fall_speed
                # Trail fully off-screen: deactivate.
                if self._heads[col] - tail_len >= h:
                    self._active[col] = False
                    self._next_spawn[col] = t + random.uniform(
                        MIN_SPAWN_INTERVAL, MAX_SPAWN_INTERVAL,
                    )
            else:
                if t >= self._next_spawn[col]:
                    self._active[col] = True
                    self._heads[col] = 0.0

        # Build the frame.
        colors: list[HSBK] = [BLACK] * total
        for col in range(w):
            if not self._active[col]:
                continue
            head_pos: float = self._heads[col]
            for row in range(h):
                dist: float = head_pos - row
                if dist < 0.0 or dist > tail_len:
                    continue
                idx: int = row * w + col
                if dist < 1.0:
                    # Head pixel — bright, low saturation (white-green).
                    frac: float = 1.0 - dist
                    bri: int = int(bri_max * frac)
                    colors[idx] = (hue, head_sat, bri, KELVIN_DEFAULT)
                else:
                    # Tail pixel — fading green.
                    fade: float = 1.0 - (dist / tail_len)
                    bri = int(bri_max * fade * fade)
                    colors[idx] = (hue, body_sat, bri, KELVIN_DEFAULT)

        # Luna dead corner mask.
        if int(self.luna):
            for row, col in LUNA_DEAD_ZONES:
                idx = row * w + col
                if idx < total:
                    colors[idx] = BLACK

        return colors
