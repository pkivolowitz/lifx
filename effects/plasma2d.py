"""2D plasma — classic sum-of-sines interference pattern.

Generates a full-color 2D plasma field by summing sine waves at different
frequencies, phases, and orientations.  The result is a continuously shifting
interference pattern that fills the entire pixel grid.

The effect produces ``width * height`` HSBK values in row-major order.
The emitter interprets this flat list as a 2D grid.  The ``width`` and
``height`` parameters are normally set automatically by the preview tool
to match the emitter's pixel dimensions.

Works on both 1D strips (single-row plasma) and 2D matrix emitters.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math

from . import Effect, Param, HSBK, HSBK_MAX, KELVIN_DEFAULT

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Two pi — full circle in radians.
TWO_PI: float = 2.0 * math.pi

# Default grid dimensions (overridden by the preview tool).
DEFAULT_WIDTH: int = 78
DEFAULT_HEIGHT: int = 22

# Brightness as fraction of HSBK_MAX.
DEFAULT_BRIGHTNESS: float = 0.76


class Plasma2D(Effect):
    """2D plasma — sine-wave interference color field.

    Sums four sine waves with different spatial frequencies and directions
    (horizontal, vertical, diagonal, radial) to produce a complex
    interference pattern.  Hue maps across the full spectrum.
    """

    name: str = "plasma2d"
    description: str = "2D plasma — sine-wave interference color field"

    speed = Param(1.0, min=0.1, max=5.0,
                  description="Animation speed multiplier")
    scale = Param(12.0, min=2.0, max=50.0,
                  description="Spatial scale (larger = coarser pattern)")
    width = Param(DEFAULT_WIDTH, min=4, max=500,
                  description="Grid width in pixels (set by viewer)")
    height = Param(DEFAULT_HEIGHT, min=4, max=300,
                   description="Grid height in pixels (set by viewer)")
    brightness = Param(76, min=10, max=100,
                       description="Peak brightness (percent)")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the 2D plasma.

        Evaluates four sine functions per pixel to build a complex
        interference pattern.  The hue cycles through the full spectrum.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels (should equal width * height).

        Returns:
            A list of ``width * height`` HSBK tuples in row-major order.
        """
        w: int = int(self.width)
        h: int = int(self.height)
        sc: float = self.scale
        spd: float = self.speed
        bri: int = int(HSBK_MAX * self.brightness / 100.0)

        # Pre-compute time-varying offsets.
        t1: float = t * spd
        t2: float = t * spd * 0.7
        t3: float = t * spd * 0.5
        t4: float = t * spd * 0.3

        # Precompute inverse scale to avoid repeated division.
        inv_sc: float = 1.0 / sc
        inv_sc_diag: float = 1.0 / (sc * 1.5)
        inv_sc_rad: float = 1.0 / sc

        # Center point for radial component.
        cx: float = w * 0.5
        cy: float = h * 0.5

        colors: list[HSBK] = []
        sin = math.sin
        sqrt = math.sqrt

        for y in range(h):
            # Pre-compute y-dependent terms outside inner loop.
            sin_y: float = sin(y * inv_sc + t2)
            dy: float = y - cy
            dy_sq: float = dy * dy

            for x in range(w):
                # Four interference components:
                #   1. Horizontal wave
                #   2. Vertical wave (pre-computed above)
                #   3. Diagonal wave
                #   4. Radial wave (distance from center)
                v: float = sin(x * inv_sc + t1)
                v += sin_y
                v += sin((x + y) * inv_sc_diag + t3)
                dx: float = x - cx
                v += sin(sqrt(dx * dx + dy_sq) * inv_sc_rad + t4)

                # v ranges approximately [-4, +4].  Normalize to [0, 1].
                hue: int = int(((v + 4.0) * 0.125) * HSBK_MAX) % (HSBK_MAX + 1)
                colors.append((hue, HSBK_MAX, bri, KELVIN_DEFAULT))

        return colors

    def period(self) -> None:
        """Plasma is quasi-periodic — no clean loop point."""
        return None
