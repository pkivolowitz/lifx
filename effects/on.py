"""On effect — set devices to a static color (transient builtin).

A one-shot effect: powers on the device, sends a single ``set_color``
command, then sleeps until SIGTERM.  No render loop, no Engine threads.
Designed for schedule entries and automations that simply turn lights on.

The ``render()`` method is retained for simulator/preview compatibility
but is never called in production.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

from typing import Any, Optional

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

# Minimum safe LIFX transition time in milliseconds.
# LIFX safety rule: never use transition_time=0 — a bulb was bricked
# 2026-03-17 by rapid-fire zero-transition commands.
_MIN_TRANSITION_MS: int = 50


def _resolve_color(color_name: str, brightness_pct: int,
                   kelvin: int) -> HSBK:
    """Convert named color + brightness + kelvin to an HSBK tuple.

    Args:
        color_name:     Key into :data:`_COLORS` (falls back to white).
        brightness_pct: Brightness as 0-100 percent.
        kelvin:         Color temperature in Kelvin.

    Returns:
        An HSBK tuple ready for LIFX protocol.
    """
    hue_deg, sat_pct = _COLORS.get(color_name, _COLORS["white"])
    hue: int = hue_to_u16(hue_deg)
    sat: int = pct_to_u16(sat_pct)
    bri: int = pct_to_u16(brightness_pct)
    return (hue, sat, bri, kelvin)


class On(Effect):
    """Set the device to a static color (transient builtin).

    Powers on the device, sends one ``set_color`` command, then the
    play command sleeps until SIGTERM.  No render loop runs.
    """

    name: str = "on"
    description: str = "Static color — turn lights on"
    is_transient: bool = True

    brightness = Param(100, min=0, max=100,
                       description="Brightness percent")
    color = Param("white", choices=_COLOR_NAMES,
                  description="Named color")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin (white only)")

    def execute(self, emitter: Any) -> None:
        """Send a single set_color command to the emitter.

        The play command has already called ``power_on`` before this
        method runs.  We send one color frame with a safe transition
        time and return — the play command handles the sleep.

        Args:
            emitter: A :class:`~emitters.lifx.LifxEmitter` instance.
        """
        hue, sat, bri, kelvin = _resolve_color(
            self.color, self.brightness, self.kelvin,
        )
        emitter.send_color(hue, sat, bri, kelvin,
                           duration_ms=_MIN_TRANSITION_MS)

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame (simulator/preview fallback only).

        Args:
            t:          Seconds elapsed (unused — static effect).
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* identical HSBK tuples.
        """
        pixel: HSBK = _resolve_color(
            self.color, self.brightness, self.kelvin,
        )
        return [pixel] * zone_count

    def period(self) -> Optional[float]:
        """Static effect — no repeating cycle.

        Returns:
            ``None`` (aperiodic).
        """
        return None
