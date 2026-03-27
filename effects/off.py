"""Off effect — power off devices.

A schedule-friendly effect that powers off the target device and holds
it dark.  Sets ``wants_power_on = False`` so that the play command
sends a power-off instead of power-on at startup, avoiding a visible
flash between schedule entries.

The render method produces black frames as a safety net — if any frame
leaks to the wire while the device is still responding, it will be
invisible.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

from typing import Optional

from . import (
    Effect, HSBK,
    KELVIN_DEFAULT,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Black pixel — zero brightness, neutral kelvin.
_BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)


class Off(Effect):
    """Power off the device.

    Instructs the play command to send a power-off instead of power-on,
    then renders black frames until the scheduler kills the subprocess.
    """

    name: str = "off"
    description: str = "Power off — turn lights off"

    # Tell the play command to power off instead of on.  Without this,
    # play would send power_on(0) and the device would flash briefly
    # at its last color before the first black frame arrives.
    wants_power_on: bool = False

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce black frames as a safety net.

        Args:
            t:          Seconds elapsed (unused).
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* black HSBK tuples.
        """
        return [_BLACK] * zone_count

    def period(self) -> Optional[float]:
        """Static effect — no repeating cycle.

        Returns:
            ``None`` (aperiodic).
        """
        return None
