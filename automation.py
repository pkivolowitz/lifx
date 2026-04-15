"""Legacy-config validation and migration helpers.

This module used to contain the monolithic ``AutomationManager``, a
``SensorData`` store, and a ``_TRANSPORT_PREFIXES`` dispatch dict
that drove sensor-driven light triggers from a single MQTT
subscriber on the hub.  All of that machinery has been retired:

- Trigger logic moved to the operator framework
  (``operators/trigger.py`` and the ``trigger`` operator type in
  ``server.json``'s ``operators:`` block).  See
  [Chapter 31: Operator Framework](docs/31-operators.md).
- BLE sensor data flows from ``glowup-ble-sensor`` on broker-2
  cross-host directly to the hub (see Chapter 28); the hub's
  in-process ``BleSensorData`` store is owned by
  ``infrastructure.ble_trigger`` now.
- Zigbee data flows from ``glowup-zigbee-service`` (see Chapter 29).
- Vivint data flows from the in-process ``VivintAdapter``.

What survives in this module are the two helpers that
``server.py`` still calls on every startup:

* :func:`validate_automation` — schema check for entries in the
  ``automations:`` block of ``server.json``.  The
  ``/api/automations`` REST handlers also call it before persisting
  user edits.

* :func:`migrate_ble_triggers` — one-shot migration that converts
  the legacy ``ble_triggers:`` config block (now removed from the
  example configs) into the modern ``automations:`` block.  Kept
  so that any old config file in the wild upgrades cleanly on
  next startup.

Everything else has been removed.  Do not re-introduce
``AutomationManager``, ``SensorData``, ``TRANSPORT_PREFIXES``, or
the ``_evaluate_condition`` helper here — the operator framework
owns those responsibilities now (see
[Chapter 35: Service vs. Adapter](docs/35-service-vs-adapter.md)
for the architectural rule about where new producer/consumer code
should live).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "2.0"

import logging
import operator as _op
from typing import Any, Callable

logger: logging.Logger = logging.getLogger("glowup.automation")

# ---------------------------------------------------------------------------
# Constants — only those still referenced by the surviving helpers.
# ---------------------------------------------------------------------------

# Default watchdog timeout (minutes) when an automation entry omits
# ``off_trigger.minutes``.  Used by both validate and migrate paths.
DEFAULT_WATCHDOG_MINUTES: float = 30.0

# Valid trigger condition operators — used by validate_automation
# to reject malformed ``trigger.condition`` and
# ``off_trigger.condition`` strings.  The operator framework has
# its own copy in ``operators.conditions``; do NOT import from
# here at runtime, this dict is for validation only.
_CONDITION_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq":  _op.eq,
    "gt":  _op.gt,
    "lt":  _op.lt,
    "gte": _op.ge,
    "lte": _op.le,
}

# Valid ``sensor.characteristic`` values — MQTT subtopics across BLE
# and Zigbee.  Kept frozen so that adding a new sensor type forces
# an explicit edit here (and a corresponding update in whichever
# producer publishes that characteristic).
VALID_CHARACTERISTICS: frozenset[str] = frozenset({
    "motion", "temperature", "humidity",       # BLE
    "lock_state", "contact", "battery",        # Zigbee locks/contacts
    "occupancy", "illuminance",                # Zigbee motion/light
})

# Valid ``schedule_conflict`` policy strings.
VALID_CONFLICT_POLICIES: frozenset[str] = frozenset({
    "defer", "override", "coexist",
})

# Legacy ``"group:"`` name prefix used in ``ble_triggers`` config
# entries.  ``migrate_ble_triggers`` strips it because the modern
# automation/operator format uses bare group names.
_GROUP_PREFIX: str = "group:"


# ---------------------------------------------------------------------------
# validate_automation
# ---------------------------------------------------------------------------


def validate_automation(
    entry: dict[str, Any],
    known_groups: set[str],
    known_effects: set[str],
    media_effects: set[str],
) -> list[str]:
    """Validate an automation entry, returning a list of error strings.

    An empty list means the entry is valid.  Called by the
    ``/api/automations`` POST/PUT handlers in ``handlers/sensors.py``
    before persisting user edits, and by ``server.py`` startup for
    every entry in the ``automations:`` block of ``server.json``.

    Args:
        entry:         The automation dict to validate.
        known_groups:  Set of valid group names.
        known_effects: Set of registered effect names.
        media_effects: Set of MediaEffect subclass names (not allowed).

    Returns:
        List of human-readable error strings (empty if valid).
    """
    errors: list[str] = []

    # Required top-level fields.
    if not entry.get("name"):
        errors.append("Missing 'name'")

    # Sensor validation.  Coerce to dict — garbage types (int, str,
    # list) from malformed input must not crash the validator.
    sensor = entry.get("sensor", {})
    if not isinstance(sensor, dict):
        sensor = {}
    if not sensor.get("label"):
        errors.append("Missing sensor.label")
    if sensor.get("characteristic") not in VALID_CHARACTERISTICS:
        errors.append(
            f"Invalid sensor.characteristic: {sensor.get('characteristic')!r}"
        )

    # Trigger validation.
    trigger = entry.get("trigger", {})
    if not isinstance(trigger, dict):
        trigger = {}
    if trigger.get("condition") not in _CONDITION_OPS:
        errors.append(
            f"Invalid trigger.condition: {trigger.get('condition')!r}"
        )
    if "value" not in trigger:
        errors.append("Missing trigger.value")

    # Action validation.
    action = entry.get("action", {})
    if not isinstance(action, dict):
        action = {}
    group_name: str = action.get("group", "")
    if group_name and group_name not in known_groups:
        errors.append(f"Unknown group: {group_name!r}")
    if not group_name:
        errors.append("Missing action.group")

    effect: str = action.get("effect", "")
    if effect and effect not in known_effects:
        errors.append(f"Unknown effect: {effect!r}")
    elif effect in media_effects:
        errors.append(f"Audio/media effects not allowed: {effect!r}")
    if not effect:
        errors.append("Missing action.effect")

    # Off-trigger validation.
    off_trigger = entry.get("off_trigger", {})
    if not isinstance(off_trigger, dict):
        off_trigger = {}
    off_type: str = off_trigger.get("type", "watchdog")
    if off_type == "watchdog":
        minutes = off_trigger.get("minutes", DEFAULT_WATCHDOG_MINUTES)
        if not isinstance(minutes, (int, float)) or minutes <= 0:
            errors.append(f"Invalid off_trigger.minutes: {minutes!r}")
    elif off_type == "condition":
        if off_trigger.get("condition") not in _CONDITION_OPS:
            errors.append(
                f"Invalid off_trigger.condition: "
                f"{off_trigger.get('condition')!r}"
            )
    else:
        errors.append(f"Invalid off_trigger.type: {off_type!r}")

    # Off-action validation (optional — defaults to stop/power-off).
    off_action = entry.get("off_action", {})
    if not isinstance(off_action, dict):
        off_action = {}
    off_effect: str = off_action.get("effect", "off")
    if off_effect and off_effect not in known_effects:
        errors.append(f"Unknown off_action effect: {off_effect!r}")
    elif off_effect in media_effects:
        errors.append(
            f"Audio/media effects not allowed in off_action: {off_effect!r}"
        )

    # Schedule conflict policy.  Coerce to string — unhashable types
    # (list, dict) crash the ``in`` operator on a set.
    policy = entry.get("schedule_conflict", "defer")
    try:
        is_valid: bool = policy in VALID_CONFLICT_POLICIES
    except TypeError:
        is_valid = False
    if not is_valid:
        errors.append(f"Invalid schedule_conflict: {policy!r}")

    return errors


# ---------------------------------------------------------------------------
# migrate_ble_triggers
# ---------------------------------------------------------------------------


def migrate_ble_triggers(config: dict[str, Any]) -> bool:
    """Auto-migrate a legacy ``ble_triggers`` block to ``automations``.

    Called by ``server.py`` on every startup.  No-op on configs that
    already have an ``automations`` block (which is the normal case
    today).  Kept as a one-way migration so that any old hand-edited
    config file in the wild upgrades cleanly without manual work.

    Args:
        config: The full server config dict, modified in place.

    Returns:
        ``True`` if migration was performed, ``False`` otherwise.
    """
    if "automations" in config:
        return False
    old: dict[str, Any] = config.get("ble_triggers", {})
    if not old:
        return False

    automations: list[dict[str, Any]] = []
    for label, cfg in old.items():
        group: str = cfg.get("group", "")
        # Strip "group:" prefix if present — the new format uses
        # bare names.
        if group.startswith(_GROUP_PREFIX):
            group = group[len(_GROUP_PREFIX):]

        on_motion: dict = cfg.get("on_motion", {})
        brightness: int = on_motion.get("brightness", 70)
        watchdog: float = cfg.get(
            "watchdog_minutes", DEFAULT_WATCHDOG_MINUTES,
        )

        entry: dict[str, Any] = {
            "name": f"{label} (migrated)",
            "enabled": True,
            "sensor": {
                "type": "ble",
                "label": label,
                "characteristic": "motion",
            },
            "trigger": {"condition": "eq", "value": 1},
            "action": {
                "group": group,
                "effect": "on",
                "params": {"brightness": brightness},
            },
            "off_trigger": {
                "type": "watchdog",
                "minutes": watchdog,
            },
            "off_action": {"effect": "off", "params": {}},
            "schedule_conflict": "defer",
        }
        automations.append(entry)

    config["automations"] = automations
    logger.info(
        "Migrated %d ble_trigger(s) to automations format",
        len(automations),
    )
    return True
