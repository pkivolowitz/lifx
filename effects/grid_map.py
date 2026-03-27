"""Grid map — diagnostic tool for discovering 2D matrix zone layouts.

Lights one zone at a time with white, advancing sequentially through
the grid.  Two mutually exclusive modes:

- ``--mode show_all`` (default): walks every position in the rectangular
  protocol grid, including dead zones with no physical LED.  Use this
  to discover which positions are dead.

- ``--mode hide_missing``: skips dead zones listed in ``--missing``,
  so only physical LEDs are visited.  At low ``--hold`` values this
  produces a rapid-fire sweep with no dark beats.

The ``--missing`` parameter encodes dead zones as ``row:col`` pairs.
Default is the Luna oval mask: corners (0,0), (0,6), (4,0), (4,6).

When ``--hold`` is >= 0.5s, macOS ``say`` announces column numbers
audibly.  Below that threshold speech is suppressed to avoid pileup.

For Luna: ``--width 7 --height 5`` (the default).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "2.0"

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

# Luna dead zones — the four corners of the 7x5 oval.
LUNA_DEAD_ZONES: str = "0:0,0:6,4:0,4:6"

# Minimum hold time (seconds) before speech is enabled.
# Below this, say commands pile up faster than they can finish.
SPEECH_THRESHOLD_SECONDS: float = 0.5


def _parse_missing(spec: str) -> set[tuple[int, int]]:
    """Parse a dead-zone spec string into a set of (row, col) tuples.

    Args:
        spec: Comma-separated ``row:col`` pairs, e.g. ``"0:0,0:6,4:0,4:6"``.
              Empty string means no dead zones.

    Returns:
        Set of (row, col) integer tuples.
    """
    result: set[tuple[int, int]] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        parts = token.split(":")
        if len(parts) != 2:
            continue
        result.add((int(parts[0]), int(parts[1])))
    return result


class GridMap(Effect):
    """Diagnostic tool — walks one white pixel across a 2D matrix grid.

    Two modes:

    - **show_all** — visits every protocol grid position including dead
      zones.  Dead zones produce a dark beat (no LED lights).  Use to
      discover which positions are physically absent.

    - **hide_missing** — skips positions listed in ``missing``, visiting
      only physical LEDs.  Produces a continuous sweep with no gaps.

    After all visited zones have been shown, the grid goes dark for
    one beat then restarts.
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
    mode = Param("show_all", choices=["show_all", "hide_missing"],
                 description="show_all visits all grid slots; "
                             "hide_missing skips dead zones")
    missing = Param(LUNA_DEAD_ZONES,
                    description="Dead zones as row:col pairs "
                                "(e.g. 0:0,0:6,4:0,4:6)")

    def on_start(self, zone_count: int) -> None:
        """Build the step sequence and reset per-run state.

        Args:
            zone_count: Number of zones on the target device.
        """
        self._last_step: int = -1
        self._last_row: int = -1

        w: int = int(self.width)
        h: int = int(self.height)

        dead: set[tuple[int, int]] = _parse_missing(str(self.missing))

        # Build ordered list of flat indices to visit.
        # Each entry is (flat_index, row, col).
        self._steps: list[tuple[int, int, int]] = []
        hide: bool = (str(self.mode) == "hide_missing")
        for idx in range(w * h):
            row: int = idx // w
            col: int = idx % w
            if hide and (row, col) in dead:
                continue
            self._steps.append((idx, row, col))

        # Whether speech is enabled (suppressed at fast hold rates).
        self._speak_enabled: bool = (float(self.hold) >= SPEECH_THRESHOLD_SECONDS)

    def _speak(self, text: str) -> None:
        """Speak text via macOS ``say`` command (non-blocking).

        Suppressed when hold time is below the speech threshold,
        on non-macOS platforms, or during unit tests.

        Args:
            text: Words to speak aloud.
        """
        if not self._speak_enabled:
            return
        # Suppress speech during unit tests to avoid garbled audio.
        if "unittest" in sys.modules:
            return
        try:
            subprocess.Popen(
                ["say", "-r", "250", text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
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
        num_steps: int = len(self._steps)

        # One extra step for the "all dark" pause at the end.
        total_steps: int = num_steps + 1
        step_idx: int = int(t / self.hold) % total_steps

        # Print and speak on each advance.
        if step_idx != self._last_step:
            self._last_step = step_idx
            if step_idx < num_steps:
                flat, row, col = self._steps[step_idx]
                sys.stdout.write(
                    f"\r  zone {flat:3d}/{total}  "
                    f"row={row} col={col}  "
                    f"[{step_idx + 1}/{num_steps}]  "
                )
                if row != self._last_row:
                    self._last_row = row
                    self._speak(f"row {row}, {col}")
                else:
                    self._speak(str(col))
            else:
                sys.stdout.write(
                    f"\r  {'— restart —':^40s}  "
                )
                self._last_row = -1
                self._speak("restart")
            sys.stdout.flush()

        # The pause beat: all dark.
        if step_idx >= num_steps:
            return [BLACK] * total

        # Light the current zone with white at the configured brightness.
        flat, _, _ = self._steps[step_idx]
        bri: int = pct_to_u16(self.brightness)
        lit: HSBK = (0, 0, bri, KELVIN_DEFAULT)

        colors: list[HSBK] = [BLACK] * total
        colors[flat] = lit
        return colors

    def on_stop(self) -> None:
        """Clean up terminal output when the effect stops."""
        if getattr(self, '_last_step', -1) >= 0:
            sys.stdout.write("\r" + " " * 60 + "\r")
            sys.stdout.flush()
