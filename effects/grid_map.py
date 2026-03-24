"""Grid map — diagnostic tool for discovering 2D matrix zone layouts.

Lights one zone at a time with white, advancing sequentially through
the entire grid.  The terminal prints the flat zone index and its
(row, col) coordinates on each advance, so the user can correlate
the lit pixel with its position in the data array.

The effect produces ``width * height`` HSBK values in row-major order,
matching the emitter's tile protocol.  For Luna: ``--width 7 --height 5``.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import sys

from . import (
    DEVICE_TYPE_MATRIX,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT,
    pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Black — used for unlit zones.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Default grid dimensions — matches LIFX Luna (7 columns x 5 rows).
DEFAULT_WIDTH: int = 7
DEFAULT_HEIGHT: int = 5

# How long each zone is held before advancing to the next.
DEFAULT_HOLD_SECONDS: float = 1.5

# Default brightness for the lit zone (percent).
DEFAULT_BRIGHTNESS: int = 50


class GridMap(Effect):
    """Diagnostic tool — walks one white pixel across a 2D grid.

    Lights a single zone at a time, advancing sequentially through
    every position in row-major order.  Prints the flat index and
    (row, col) to the terminal on each advance.

    After all zones have been shown, the grid goes dark for one beat
    then restarts.
    """

    name: str = "_grid_map"
    description: str = "Diagnostic — walk one white pixel across a 2D matrix grid"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    width = Param(DEFAULT_WIDTH, min=1, max=500,
                  description="Grid width in pixels (columns)")
    height = Param(DEFAULT_HEIGHT, min=1, max=300,
                   description="Grid height in pixels (rows)")
    hold = Param(DEFAULT_HOLD_SECONDS, min=0.1, max=10.0,
                 description="Seconds each zone is held before advancing")
    brightness = Param(DEFAULT_BRIGHTNESS, min=1, max=100,
                       description="Lit zone brightness (percent)")

    def on_start(self, zone_count: int) -> None:
        """Reset per-run state when the effect starts.

        Args:
            zone_count: Number of zones on the target device.
        """
        self._last_step: int = -1

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame — all black except the current zone.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels (ignored — uses width * height).

        Returns:
            A list of ``width * height`` HSBK tuples in row-major order.
        """
        w: int = int(self.width)
        h: int = int(self.height)
        total: int = w * h

        # One extra step for the "all dark" pause at the end.
        total_steps: int = total + 1
        step: int = int(t / self.hold) % total_steps

        # Print the zone index and grid coordinates on each advance.
        if step != self._last_step:
            self._last_step = step
            if step < total:
                row: int = step // w
                col: int = step % w
                sys.stdout.write(
                    f"\r  zone {step:3d}/{total}  "
                    f"row={row} col={col}  "
                )
            else:
                sys.stdout.write(f"\r  {'— restart —':^30s}  ")
            sys.stdout.flush()

        # The pause beat: all dark.
        if step >= total:
            return [BLACK] * total

        # Light the current zone with white at the configured brightness.
        bri: int = pct_to_u16(self.brightness)
        # White = hue 0, saturation 0, configured brightness, default kelvin.
        lit: HSBK = (0, 0, bri, KELVIN_DEFAULT)

        colors: list[HSBK] = [BLACK] * total
        colors[step] = lit
        return colors

    def on_stop(self) -> None:
        """Clean up terminal output when the effect stops."""
        if getattr(self, '_last_step', -1) >= 0:
            sys.stdout.write("\r" + " " * 50 + "\r")
            sys.stdout.flush()
