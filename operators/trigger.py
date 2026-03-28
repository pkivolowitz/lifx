"""TriggerOperator — sensor-driven light actions as composable operators.

Replaces the monolithic :class:`~automation.AutomationManager` with
individual operator instances.  Each configured automation rule becomes
a TriggerOperator instance managed by the :class:`~operators.OperatorManager`.

A TriggerOperator:
    - Subscribes to input signals (via the Operator ABC's signal matching).
    - Evaluates a trigger condition (eq, gt, lt, gte, lte).
    - Fires an action on a device group via DeviceManager (play/stop).
    - Manages a watchdog timeout for off-actions.
    - Respects schedule conflict policies (defer, override, coexist).
    - Supports debounce to avoid hammering bulbs.

Because TriggerOperators are just operators, they compose naturally with
upstream operators like MotionGateOperator.  A trigger subscribing to
``ble:onvis_motion:motion:gated`` only fires when the occupancy gate
passes motion through — no special-case code needed.

Config example::

    {
        "type": "trigger",
        "name": "living_room_motion",
        "sensor": {"type": "zigbee", "label": "hallway_motion",
                   "characteristic": "occupancy"},
        "trigger": {"condition": "eq", "value": 1},
        "action": {"group": "living_room", "effect": "on",
                   "params": {"brightness": 70}},
        "off_trigger": {"type": "watchdog", "minutes": 30},
        "off_action": {"effect": "off", "params": {}},
        "schedule_conflict": "defer"
    }

The config is backward-compatible with the old automation data model.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from operators import Operator, TICK_BOTH, SignalValue
from operators.conditions import evaluate_condition
from param import Param

logger: logging.Logger = logging.getLogger("glowup.operators.trigger")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum seconds between repeated trigger actions to avoid hammering
# bulbs while a sensor fires continuously.
DEBOUNCE_SECONDS: float = 2.0

# Default watchdog timeout (minutes) when not specified.
DEFAULT_WATCHDOG_MINUTES: float = 30.0

# Seconds-per-minute conversion factor.
SECONDS_PER_MINUTE: float = 60.0

# Condition operators are in operators/conditions.py (shared module).

# Valid schedule-conflict policies.
VALID_CONFLICT_POLICIES: frozenset[str] = frozenset({
    "defer", "override", "coexist",
})

# Group identifier prefix (matches server.py GROUP_PREFIX).
_GROUP_PREFIX: str = "group:"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _device_id_for_group(group_name: str) -> str:
    """Build a ``group:name`` device identifier.

    Args:
        group_name: The group name without prefix.

    Returns:
        The prefixed identifier (e.g., ``"group:living_room"``).
    """
    if group_name.startswith(_GROUP_PREFIX):
        return group_name
    return _GROUP_PREFIX + group_name


# _evaluate_condition is now in operators.conditions.evaluate_condition.


def _signal_name_from_sensor(sensor: dict[str, Any]) -> str:
    """Build a SignalBus signal name from a sensor config dict.

    Signal names are transport-free: ``{device}:{property}``.
    The transport (ble, zigbee, vivint) is metadata on the signal,
    not part of the name.

    Args:
        sensor: Dict with ``type``, ``label``, ``characteristic``.

    Returns:
        Signal name like ``"hallway_motion:occupancy"``.
    """
    label: str = sensor.get("label", "*")
    char: str = sensor.get("characteristic", "*")
    return f"{label}:{char}"


# ---------------------------------------------------------------------------
# TriggerOperator
# ---------------------------------------------------------------------------

class TriggerOperator(Operator):
    """Sensor-driven light action — replaces a single automation rule.

    Each instance watches one sensor signal, evaluates a trigger condition,
    and fires play/stop actions on a device group.  Manages watchdog
    timeouts and schedule conflict policies.

    The ``_dm`` (DeviceManager) reference is injected via :meth:`on_configure`
    from the full server config's ``_device_manager`` key.
    """

    operator_type: str = "trigger"
    description: str = "Sensor-driven light action"
    depends_on: list[str] = ["motion_gate"]

    # input_signals set dynamically from sensor config.
    input_signals: list[str] = []
    output_signals: list[str] = []

    tick_mode: str = TICK_BOTH
    tick_hz: float = 1.0

    # Configurable per-trigger debounce — minimum seconds between actions.
    debounce_seconds = Param(
        DEBOUNCE_SECONDS, min=0.0, max=30.0,
        description="Minimum seconds between repeated trigger actions",
    )

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        bus: Any,
    ) -> None:
        """Initialize the trigger operator.

        Args:
            name:   Instance name (automation name).
            config: Automation-compatible config dict.
            bus:    SignalBus instance.
        """
        super().__init__(name, config, bus)

        # Sensor config.
        self._sensor: dict[str, Any] = config.get("sensor", {})
        self._signal_name: str = _signal_name_from_sensor(self._sensor)

        # Set input_signals for the OperatorManager's signal matching.
        self.input_signals = [self._signal_name]

        # Trigger condition.
        trigger: dict = config.get("trigger", {})
        self._condition: str = trigger.get("condition", "eq")
        self._threshold: Any = trigger.get("value", 1)

        # On-action.
        action: dict = config.get("action", {})
        self._group: str = action.get("group", "")
        self._effect: str = action.get("effect", "on")
        self._params: dict = action.get("params", {})

        # Off-trigger.
        off_trigger: dict = config.get("off_trigger", {})
        self._off_type: str = off_trigger.get("type", "watchdog")
        self._watchdog_minutes: float = off_trigger.get(
            "minutes", DEFAULT_WATCHDOG_MINUTES,
        )
        self._off_condition: str = off_trigger.get("condition", "eq")
        self._off_value: Any = off_trigger.get("value")

        # Off-action.
        off_action: dict = config.get("off_action", {})
        self._off_effect: str = off_action.get("effect", "off")
        self._off_params: dict = off_action.get("params", {})

        # Schedule conflict policy.
        self._schedule_conflict: str = config.get("schedule_conflict", "defer")

        # Enabled flag (supports disable without removing from config).
        self._enabled: bool = config.get("enabled", True)

        # Runtime state.
        self._active: bool = False
        self._last_trigger: float = 0.0
        self._last_action: float = 0.0
        self._last_off: float = 0.0

        # DeviceManager — injected via on_configure.
        self._dm: Any = None

        # Full server config — for schedule conflict checks.
        self._server_config: dict[str, Any] = {}

    def on_configure(self, config: dict[str, Any]) -> None:
        """Receive DeviceManager and full server config.

        Args:
            config: Full server config dict.  Must contain
                    ``_device_manager`` key (injected by server startup).
        """
        self._dm = config.get("_device_manager")
        self._server_config = config
        if self._dm is None:
            logger.warning(
                "TriggerOperator '%s': no DeviceManager available — "
                "actions will be no-ops", self.name,
            )

    def on_start(self) -> None:
        """Log startup."""
        if not self._enabled:
            logger.info("TriggerOperator '%s' disabled", self.name)
            return
        logger.info(
            "TriggerOperator '%s' started — signal: %s, "
            "condition: %s %s, group: %s",
            self.name, self._signal_name, self._condition,
            self._threshold, self._group,
        )

    def on_signal(self, name: str, value: SignalValue) -> None:
        """Evaluate trigger condition against an incoming signal.

        Args:
            name:  Signal name that changed.
            value: New signal value (float or list).
        """
        if not self._enabled:
            return

        # Coerce to numeric for comparison.
        try:
            fval: float = float(value) if not isinstance(value, list) else 0.0
        except (ValueError, TypeError):
            return

        # Coerce threshold to match — the old automation system did this
        # via _parse_value, we do it by matching types.
        threshold: Any = self._threshold
        if isinstance(threshold, int):
            try:
                cmp_val: Any = int(fval)
            except (ValueError, OverflowError):
                cmp_val = fval
        else:
            cmp_val = fval

        matched: bool = evaluate_condition(self._condition, threshold, cmp_val)

        if matched:
            # Reset watchdog timer on every matching event.
            self._last_trigger = time.time()

        if matched and not self._active:
            self._fire_on_action()
        elif not matched and self._active:
            # Condition-based off-trigger.
            if self._off_type == "condition":
                if evaluate_condition(self._off_condition, self._off_value, cmp_val):
                    self._fire_off_action()

    def on_tick(self, dt: float) -> None:
        """Check watchdog timeout.

        Args:
            dt: Seconds since last tick.
        """
        if not self._enabled:
            return
        if not self._active:
            return
        if self._off_type != "watchdog":
            return
        if self._last_trigger == 0.0:
            return  # Never triggered — don't fire spurious off-action.

        timeout_sec: float = self._watchdog_minutes * SECONDS_PER_MINUTE
        elapsed: float = time.time() - self._last_trigger

        if elapsed >= timeout_sec:
            logger.info(
                "TriggerOperator '%s': watchdog expired (%.0f min)",
                self.name, elapsed / SECONDS_PER_MINUTE,
            )
            self._fire_off_action()

    def on_stop(self) -> None:
        """Log shutdown."""
        logger.debug("TriggerOperator '%s' stopped", self.name)

    # --- Actions -----------------------------------------------------------

    def _fire_on_action(self) -> None:
        """Execute the on-action (play effect on group)."""
        now: float = time.time()

        # Debounce.
        if now - self._last_action < self.debounce_seconds:
            return

        if not self._group or not self._dm:
            return

        device_id: str = _device_id_for_group(self._group)

        # Schedule conflict check.
        if self._schedule_conflict == "defer":
            if self._is_schedule_active(self._group):
                logger.info(
                    "TriggerOperator '%s' deferred — schedule active for %s",
                    self.name, self._group,
                )
                return

        try:
            if self._effect == "off":
                self._dm.stop(device_id)
            else:
                self._dm.play(
                    device_id, self._effect, self._params,
                    source="trigger",
                )
            self._active = True
            self._last_action = now
            logger.info(
                "TriggerOperator '%s' fired: %s → %s (%s)",
                self.name, self._effect, self._group,
                json.dumps(self._params),
            )
        except Exception as exc:
            logger.error(
                "TriggerOperator '%s' action failed: %s",
                self.name, exc,
            )

    def _fire_off_action(self) -> None:
        """Execute the off-action (stop or play off-effect on group)."""
        if not self._group or not self._dm:
            return

        device_id: str = _device_id_for_group(self._group)

        try:
            if self._off_effect == "off":
                self._dm.stop(device_id)
            else:
                self._dm.play(
                    device_id, self._off_effect, self._off_params,
                    source="trigger",
                )
            self._active = False
            self._last_off = time.time()
            self._last_action = self._last_off
            logger.info(
                "TriggerOperator '%s' off-action: %s → %s",
                self.name, self._off_effect, self._group,
            )
        except Exception as exc:
            logger.error(
                "TriggerOperator '%s' off-action failed: %s",
                self.name, exc,
            )

    # --- Schedule conflict detection --------------------------------------

    def _is_schedule_active(self, group_name: str) -> bool:
        """Check if a schedule entry is active for a group.

        Args:
            group_name: The bare group name (no prefix).

        Returns:
            ``True`` if a schedule entry is currently active.
        """
        try:
            from schedule_utils import find_active_entry as _find_active_entry
        except ImportError:
            return False

        try:
            specs: list = list(self._server_config.get("schedule", []))
            loc: dict = self._server_config.get("location", {})
            lat: float = loc.get("latitude", 0.0)
            lon: float = loc.get("longitude", 0.0)
            now: datetime = datetime.now(timezone.utc).astimezone()
            active = _find_active_entry(specs, lat, lon, now, group_name)
            return active is not None
        except Exception as exc:
            logger.debug("Schedule conflict check failed: %s", exc)
            return False

    # --- Introspection (extends Operator.get_status) ----------------------

    def get_status(self) -> dict[str, Any]:
        """Return JSON-serializable status for API responses.

        Returns:
            Dict with trigger-specific fields added to base status.
        """
        status: dict[str, Any] = super().get_status()
        status["enabled"] = self._enabled
        status["active"] = self._active
        status["last_triggered"] = self._last_trigger
        status["signal"] = self._signal_name
        status["group"] = self._group
        status["effect"] = self._effect
        status["schedule_conflict"] = self._schedule_conflict
        # Include the full config for CRUD display.
        status["sensor"] = self._sensor
        status["trigger"] = {
            "condition": self._condition,
            "value": self._threshold,
        }
        status["action"] = {
            "group": self._group,
            "effect": self._effect,
            "params": self._params,
        }
        status["off_trigger"] = {
            "type": self._off_type,
        }
        if self._off_type == "watchdog":
            status["off_trigger"]["minutes"] = self._watchdog_minutes
        else:
            status["off_trigger"]["condition"] = self._off_condition
            status["off_trigger"]["value"] = self._off_value
        status["off_action"] = {
            "effect": self._off_effect,
            "params": self._off_params,
        }
        return status

    # --- Runtime control ---------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable this trigger at runtime.

        Args:
            enabled: Whether the trigger should fire.
        """
        self._enabled = enabled
        logger.info(
            "TriggerOperator '%s' %s",
            self.name, "enabled" if enabled else "disabled",
        )
