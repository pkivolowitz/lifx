"""Grid map — diagnostic tool for discovering 2D matrix zone layouts.

Lights one zone at a time with white, advancing sequentially through
the rectangular protocol grid.  The terminal prints the flat zone
index and its (row, col) coordinates on each advance, and macOS
``say`` announces each column number audibly so the user can watch
the device without looking at the terminal.

The LIFX tile protocol always uses a rectangular grid (``width * height``
HSBK values in row-major order).  Oval devices like the Luna have
physical pixels at only a subset of grid positions — the missing
positions are "dead zones" that accept data but have no LED.

For Luna: ``--width 7 --height 5`` (the default).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.3"

import subprocess
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

# Default grid dimensions — matches LIFX Luna protocol grid (7x5).
DEFAULT_WIDTH: int = 7
DEFAULT_HEIGHT: int = 5

# How long each zone is held before advancing to the next.
DEFAULT_HOLD_SECONDS: float = 2.0

# Default brightness for the lit zone (percent).
DEFAULT_BRIGHTNESS: int = 50


class GridMap(Effect):
    """Diagnostic tool — walks one white pixel across a rectangular grid.

    Lights a single zone at a time, advancing sequentially through
    every position in row-major order.  Prints the flat index and
    (row, col) to the terminal on each advance.

    Uses macOS ``say`` to announce "row N" at the start of each row
    and the column number for each zone, so the user can keep their
    eyes on the device.

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
        self._last_row: int = -1

    def _speak(self, text: str) -> None:
        """Speak text via macOS ``say`` command (non-blocking).

        Falls back silently on non-macOS platforms.

        Args:
            text: Words to speak aloud.
        """
        try:
            # Fire and forget — don't block the render loop.
            subprocess.Popen(
                ["say", "-r", "250", text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            # Not macOS — no ``say`` binary.  Silent fallback.
            pass

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

        # Print and speak the zone index and grid coordinates on advance.
        if step != self._last_step:
            self._last_step = step
            if step < total:
                row: int = step // w
                col: int = step % w
                sys.stdout.write(
                    f"\r  zone {step:3d}/{total}  "
                    f"row={row} col={col}  "
                )
                # Announce new row, then column number.
                if row != self._last_row:
                    self._last_row = row
                    self._speak(f"row {row}")
                else:
                    self._speak(str(col))
            else:
                sys.stdout.write(f"\r  {'— restart —':^30s}  ")
                self._last_row = -1
                self._speak("restart")
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
