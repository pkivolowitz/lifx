"""Matrix rain — falling green streams on a 2D grid.

Digital rain inspired by The Matrix.  Each column independently spawns
falling trails of green characters (represented as brightness
variations).  Trails have a bright white head and a fading green tail.

The rain flows through a *canonical* grid where heads spawn at row 0
and fall toward increasing row numbers.  ``--rotate`` rotates that
canonical pattern by 0/90/180/270 degrees clockwise into the physical
grid.  This exists because the matrix fixtures (e.g. the LIFX 15"
SuperColor Ceiling) have no marked "up" — at install time we don't
know which physical edge the user considers the top.  Try the four
rotations and pick the one that looks right for the mounting.

Canonical dimensions are swapped relative to physical for 90/270:
rain that falls down the long axis of a rectangular fixture turns
into rain that falls along the short axis when rotated.  Square
fixtures (Ceiling 8x8) are unaffected.

Computes on a full rectangular grid.  When ``--luna`` is enabled, the
four dead corner pixels are blacked out after rendering — Luna's dead
corners are described in *physical* coordinates and are applied after
the rotation transform.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.1"

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

# Allowed rotation angles in degrees clockwise from canonical
# (rain-falls-down).  Only the four cardinal rotations make sense
# for axis-aligned trails on a pixel grid.
ROTATE_ANGLES: list[int] = [0, 90, 180, 270]


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
                  description="Grid width in pixels (columns), physical")
    height = Param(DEFAULT_HEIGHT, min=1, max=300,
                   description="Grid height in pixels (rows), physical")
    speed = Param(10.0, min=0.5, max=20.0,
                  description="Rows per second (fall speed)")
    tail = Param(2, min=1, max=10,
                 description="Trail tail length in rows")
    brightness = Param(80, min=1, max=100,
                       description="Peak brightness (percent)")
    rotate = Param(0, choices=ROTATE_ANGLES,
                   description="Rain direction in degrees clockwise from canonical "
                               "(0=down, 90=right, 180=up, 270=left).  Use to pick "
                               "which physical edge the rain falls toward when the "
                               "fixture has no marked 'up'.")
    luna = Param(0, min=0, max=1,
                 description="Black out Luna dead corners (1=yes)")

    def _canonical_dims(self) -> tuple[int, int]:
        """Return the canonical (rain-falls-down) (width, height).

        For 90/270 rotations the canonical canvas is the transpose of
        the physical canvas — heads still spawn at canonical row 0 and
        the rasterizer maps them to a side edge of the physical grid.
        """
        pw: int = int(self.width)
        ph: int = int(self.height)
        if int(self.rotate) in (90, 270):
            return ph, pw
        return pw, ph

    def on_start(self, zone_count: int) -> None:
        """Seed per-column trail state in canonical orientation.

        Args:
            zone_count: Number of zones on the target device.
        """
        cw, _ = self._canonical_dims()
        # Each canonical column tracks: current head row (float),
        # active flag, and next spawn time.  State arrays are sized
        # to the canonical width so the rain math is rotation-agnostic
        # — only the final rasterization step knows about rotation.
        self._heads: list[float] = [-1.0] * cw
        self._active: list[bool] = [False] * cw
        self._next_spawn: list[float] = [
            random.uniform(0.0, MAX_SPAWN_INTERVAL) for _ in range(cw)
        ]
        self._last_t: float = 0.0

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of falling rain.

        Rain math runs in canonical (rain-falls-down) coordinates.
        At rasterization time each canonical (crow, ccol) pixel is
        mapped to a physical (prow, pcol) by the rotation transform
        and written into the physical row-major output buffer.  The
        Luna dead-corner mask is applied last because it is described
        in physical coordinates.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels (ignored — uses width * height).

        Returns:
            A list of physical ``width * height`` HSBK tuples in
            row-major order.
        """
        pw: int = int(self.width)
        ph: int = int(self.height)
        cw, ch = self._canonical_dims()
        rot: int = int(self.rotate)
        total: int = pw * ph
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

        # Advance heads and spawn new trails (canonical coords).
        for ccol in range(cw):
            if self._active[ccol]:
                self._heads[ccol] += dt * fall_speed
                # Trail fully off-screen: deactivate.
                if self._heads[ccol] - tail_len >= ch:
                    self._active[ccol] = False
                    self._next_spawn[ccol] = t + random.uniform(
                        MIN_SPAWN_INTERVAL, MAX_SPAWN_INTERVAL,
                    )
            else:
                if t >= self._next_spawn[ccol]:
                    self._active[ccol] = True
                    self._heads[ccol] = 0.0

        # Build the frame in physical coords by rotating canonical
        # (crow, ccol) into physical (prow, pcol) on the fly.
        colors: list[HSBK] = [BLACK] * total
        for ccol in range(cw):
            if not self._active[ccol]:
                continue
            head_pos: float = self._heads[ccol]
            for crow in range(ch):
                dist: float = head_pos - crow
                if dist < 0.0 or dist > tail_len:
                    continue

                # Canonical → physical transform.  The four cases are
                # the cardinal cw rotations of an axis-aligned canvas.
                if rot == 0:
                    prow, pcol = crow, ccol
                elif rot == 90:
                    prow, pcol = ccol, pw - 1 - crow
                elif rot == 180:
                    prow, pcol = ph - 1 - crow, pw - 1 - ccol
                else:  # 270
                    prow, pcol = ph - 1 - ccol, crow
                idx: int = prow * pw + pcol

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

        # Luna dead corner mask — physical coordinates, applied after
        # the rotation transform so a rotated rain still avoids the
        # physical dead corners.
        if int(self.luna):
            for row, col in LUNA_DEAD_ZONES:
                idx = row * pw + col
                if idx < total:
                    colors[idx] = BLACK

        return colors
