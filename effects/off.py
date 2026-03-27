"""Off effect — power off devices (transient builtin).

A one-shot effect: the play command sends ``power_off`` (via the
``wants_power_on = False`` flag), then sleeps until SIGTERM.  No
render loop, no Engine threads.

The ``render()`` method is retained for simulator/preview compatibility
but is never called in production.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

from typing import Any, Optional

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
    """Power off the device (transient builtin).

    Sets ``wants_power_on = False`` so the play command sends
    ``power_off`` at startup.  The ``execute`` method is a no-op
    because the power-off is the entire action.
    """

    name: str = "off"
    description: str = "Power off — turn lights off"
    is_transient: bool = True

    # Tell the play command to power off instead of on.
    wants_power_on: bool = False

    def execute(self, emitter: Any) -> None:
        """No-op — power_off was already sent by the play command.

        Args:
            emitter: A :class:`~emitters.lifx.LifxEmitter` instance
                     (unused).
        """
        # Power-off is handled by cmd_play checking wants_power_on.
        # Nothing additional to do here.

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce black frames (simulator/preview fallback only).

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
