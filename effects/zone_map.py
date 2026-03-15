"""Zone map — diagnostic tool for understanding physical zone layouts.

Three modes of operation, selected by the ``mode`` parameter:

**stride** (default):
    Cycles through zone offsets within each bulb group at a configurable
    stride.  With stride=3, lights zones 0,3,6,... then 1,4,7,... then
    2,5,8,... in distinct R/G/B colors.  Designed for string lights where
    3 adjacent zones form one physical bulb.

**walk**:
    Lights a single zone at a time, advancing one position per hold period.
    The hue encodes the zone's position in the strip (rainbow gradient),
    and the current zone index is printed to stdout on each advance.
    Use this to map which physical LED corresponds to each zone index.

**fill**:
    Progressively fills zones from 0 to N, adding one zone per hold period.
    Reveals the physical direction and ordering of the strip.  Each zone
    gets a unique hue so you can see the order even after it wraps.

All modes support the ``solo`` param to isolate a single zone offset
with a rotating hue for detailed inspection.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.0"

import sys
from typing import Optional

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT,
    hue_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Black — used for unlit zones.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Default stride matches LIFX polychrome string lights (3 zones/bulb).
DEFAULT_STRIDE: int = 3

# How long each step is displayed before advancing.
DEFAULT_HOLD_SECONDS: float = 1.5

# Phase colors — each zone offset gets a distinct color so concentric
# cylinders are easily distinguishable.  Extends beyond 3 for larger
# strides; wraps if stride exceeds the palette length.
PHASE_COLORS: list[HSBK] = [
    (0,     HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT),  # Red     (0°)
    (21845, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT),  # Green   (120°)
    (43690, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT),  # Blue    (240°)
    (10922, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT),  # Yellow  (60°)
    (54613, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT),  # Magenta (300°)
    (32768, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT),  # Cyan    (180°)
]

# Full hue rotation period for solo mode (seconds).
SOLO_ROTATION_SECONDS: float = 6.0

# Degrees in a full rotation.
DEGREES_FULL: float = 360.0

# Mode constants — correspond to the ``mode`` param choices.
MODE_STRIDE: int = 0
MODE_WALK: int = 1
MODE_FILL: int = 2

# Mode names for the choices list.
MODE_CHOICES: list[str] = ["stride", "walk", "fill"]


class ZoneMap(Effect):
    """Diagnostic tool that reveals physical zone positions and ordering.

    Three modes are available (selected via the ``mode`` parameter):

    - **stride** (0): Cycles phase colors through zone offsets.  Best
      for string lights where N adjacent zones form one bulb.
    - **walk** (1): Lights one zone at a time, advancing sequentially.
      Prints the zone index to stdout for easy identification.
    - **fill** (2): Progressively fills from zone 0 to zone N, showing
      the strip's physical direction and ordering.

    In all modes, ``solo`` overrides to isolate a single zone offset
    with a rotating rainbow hue.
    """

    name: str = "_zone_map"
    description: str = "Diagnostic tool — walk, fill, or stride-strobe to reveal zone layout"

    mode = Param(MODE_STRIDE, min=MODE_STRIDE, max=MODE_FILL,
                 description="0=stride (R/G/B cycle), 1=walk (single zone), 2=fill (progressive)")
    stride = Param(DEFAULT_STRIDE, min=1, max=82,
                   description="Zones per bulb group (stride mode only)")
    hold = Param(DEFAULT_HOLD_SECONDS, min=0.1, max=10.0,
                 description="Seconds each step is held before advancing")
    solo = Param(-1, min=-1, max=81,
                 description="Solo one zone offset (-1=off, 0+=solo that offset with rainbow)")
    hue = Param(-1.0, min=-1.0, max=360.0,
                description="Fixed hue in degrees (-1=rainbow gradient, 0-360=constant color)")

    def on_start(self, zone_count: int) -> None:
        """Reset per-run state when the effect starts.

        Args:
            zone_count: Number of zones on the target device.
        """
        self._last_printed_step: int = -1

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame based on the current mode.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        # --- Solo mode: one zone offset, rotating hue ---
        if self.solo >= 0:
            return self._render_solo(t, zone_count)

        # Dispatch to the active mode.
        mode: int = int(self.mode)
        if mode == MODE_WALK:
            return self._render_walk(t, zone_count)
        if mode == MODE_FILL:
            return self._render_fill(t, zone_count)
        return self._render_stride(t, zone_count)

    def _render_solo(self, t: float, zone_count: int) -> list[HSBK]:
        """Render solo mode: isolate one zone offset with rotating hue.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        hue_degrees: float = (t / SOLO_ROTATION_SECONDS * DEGREES_FULL) % DEGREES_FULL
        lit: HSBK = (
            hue_to_u16(hue_degrees),
            HSBK_MAX,
            HSBK_MAX,
            KELVIN_DEFAULT,
        )
        stride_val: int = max(1, int(self.stride))
        colors: list[HSBK] = []
        for i in range(zone_count):
            if i % stride_val == self.solo:
                colors.append(lit)
            else:
                colors.append(BLACK)
        return colors

    def _render_stride(self, t: float, zone_count: int) -> list[HSBK]:
        """Render stride mode: cycle R/G/B through zone offsets.

        With stride=3, cycles through three phases, each in a distinct color:
          Phase 0: zones 0, 3, 6, 9, ... lit RED   — all others black
          Phase 1: zones 1, 4, 7, 10, ... lit GREEN — all others black
          Phase 2: zones 2, 5, 8, 11, ... lit BLUE  — all others black

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        stride_val: int = max(1, int(self.stride))
        phase: int = int(t / self.hold) % stride_val
        lit: HSBK = PHASE_COLORS[phase % len(PHASE_COLORS)]

        colors: list[HSBK] = []
        for i in range(zone_count):
            if i % stride_val == phase:
                colors.append(lit)
            else:
                colors.append(BLACK)
        return colors

    def _render_walk(self, t: float, zone_count: int) -> list[HSBK]:
        """Render walk mode: light one zone at a time, advancing sequentially.

        The lit zone's hue encodes its position in the strip as a rainbow
        gradient so the ordering is visible even without the terminal output.
        The current zone index is printed to stdout on each advance.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        step: int = int(t / self.hold) % zone_count

        # Track step for external consumers (no terminal I/O in render thread).
        if step != getattr(self, '_last_printed_step', -1):
            self._last_printed_step = step

        # Use fixed hue if set, otherwise rainbow-encode position.
        if self.hue >= 0:
            hue_val: int = hue_to_u16(self.hue)
        else:
            hue_val = hue_to_u16(step * DEGREES_FULL / zone_count)
        lit: HSBK = (hue_val, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT)

        colors: list[HSBK] = [BLACK] * zone_count
        colors[step] = lit
        return colors

    def _render_fill(self, t: float, zone_count: int) -> list[HSBK]:
        """Render fill mode: progressively fill zones from 0 to N.

        Each zone receives a unique hue (rainbow gradient) so the
        physical ordering is visible.  After all zones are filled,
        the strip goes black for one beat, then restarts.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        # One extra step for the "all black" pause at the end.
        total_steps: int = zone_count + 1
        step: int = int(t / self.hold) % total_steps

        # Print progress on advance.
        if step != getattr(self, '_last_printed_step', -1):
            self._last_printed_step = step
            if step < zone_count:
                sys.stdout.write(f"\r  filling zone {step:3d}/{zone_count}  ")
            else:
                sys.stdout.write(f"\r  {'— reset —':^30s}  ")
            sys.stdout.flush()

        # The pause beat: all black.
        if step >= zone_count:
            return [BLACK] * zone_count

        # Fill zones 0..step with rainbow gradient.
        colors: list[HSBK] = [BLACK] * zone_count
        for i in range(step + 1):
            hue: int = hue_to_u16(i * DEGREES_FULL / zone_count)
            colors[i] = (hue, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT)
        return colors

    def on_stop(self) -> None:
        """Clean up terminal output when the effect stops."""
        # Clear the inline status if we printed anything.
        if getattr(self, '_last_printed_step', -1) >= 0:
            sys.stdout.write("\r" + " " * 40 + "\r")
            sys.stdout.flush()
