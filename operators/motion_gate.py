"""MotionGateOperator — gate motion signals on occupancy state.

Reads raw motion signals and the occupancy signal from the bus.
Passes motion through when HOME, suppresses when AWAY.  Writes
gated motion signals back to the bus for downstream operators
and automations.

The household has 3 dogs — motion sensors fire constantly when
humans are away.  Dogs can't work a deadbolt, so lock-derived
occupancy is the only clean discriminator.  This operator is the
gate.

Config example::

    {
        "type": "motion_gate",
        "name": "gated_motion",
        "occupancy_signal": "house:occupancy:state",
        "motion_signals": ["zigbee:*:occupancy", "ble:*:motion"]
    }
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
from typing import Any, Union

from operators import Operator, TICK_REACTIVE, SignalValue
from param import Param

logger: logging.Logger = logging.getLogger("glowup.operators.motion_gate")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default occupancy signal to read.
DEFAULT_OCCUPANCY_SIGNAL: str = "house:occupancy:state"

# HOME value on the occupancy signal.
HOME_VALUE: float = 1.0

# Suffix appended to gated output signals.
GATED_SUFFIX: str = ":gated"


# ---------------------------------------------------------------------------
# MotionGateOperator
# ---------------------------------------------------------------------------

class MotionGateOperator(Operator):
    """Gate motion signals on occupancy — suppress when AWAY.

    For each incoming motion signal that matches ``motion_signals``
    patterns, writes a gated version to the bus:

    - If occupancy is HOME → pass through (``1.0`` stays ``1.0``).
    - If occupancy is AWAY → suppress (write ``0.0`` regardless).

    The gated signal name appends ``:gated`` to the original:
    ``ble:onvis_motion:motion`` → ``ble:onvis_motion:motion:gated``

    Downstream automations and operators should subscribe to the
    ``:gated`` signals, not the raw motion signals, to respect
    occupancy state.
    """

    operator_type: str = "motion_gate"
    description: str = "Gate motion signals on HOME/AWAY occupancy"

    # input_signals is set dynamically from config in on_configure.
    input_signals: list[str] = []
    output_signals: list[str] = []

    tick_mode: str = TICK_REACTIVE

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        bus: Any,
    ) -> None:
        """Initialize the motion gate operator.

        Args:
            name:   Instance name.
            config: Operator config dict.
            bus:    SignalBus instance.
        """
        super().__init__(name, config, bus)
        self._occupancy_signal: str = config.get(
            "occupancy_signal", DEFAULT_OCCUPANCY_SIGNAL,
        )
        # Motion signal patterns from config.
        self._motion_patterns: list[str] = config.get(
            "motion_signals", [],
        )
        # Set input_signals to include both motion patterns and occupancy.
        self.input_signals = list(self._motion_patterns)

    def on_start(self) -> None:
        """Log startup."""
        logger.info(
            "MotionGateOperator started — patterns: %s, occupancy: %s",
            self._motion_patterns, self._occupancy_signal,
        )

    def on_signal(self, name: str, value: SignalValue) -> None:
        """Handle a motion signal change.

        Args:
            name:  Signal name (e.g., ``"ble:onvis_motion:motion"``).
            value: Motion value (typically ``1.0`` or ``0.0``).
        """
        # Coerce to float.
        try:
            fval: float = float(value) if not isinstance(value, list) else 0.0
        except (ValueError, TypeError):
            return

        # Read current occupancy from the bus.
        occupancy: float = float(self.read(self._occupancy_signal, HOME_VALUE))

        # Gate: pass through if HOME, suppress if AWAY.
        if occupancy == HOME_VALUE:
            gated_value: float = fval
        else:
            gated_value = 0.0

        # Write the gated signal.
        gated_name: str = name + GATED_SUFFIX
        self.write(gated_name, gated_value)

        if fval == 1.0 and occupancy != HOME_VALUE:
            logger.debug(
                "Motion suppressed (AWAY): %s → %s = 0.0",
                name, gated_name,
            )

    def on_stop(self) -> None:
        """Log shutdown."""
        logger.debug("MotionGateOperator stopped")
