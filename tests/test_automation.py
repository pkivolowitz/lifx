"""Tests for the surviving helpers in automation.py.

The bulk of automation.py was retired in 2026-04 — the
``AutomationManager``, ``SensorData`` store, ``_AutomationState``,
and ``TRANSPORT_PREFIXES`` machinery moved to the operator
framework (see ``operators/trigger.py`` and Chapter 31 of the
manual) and the producer-side service pattern (see Chapters 28
and 29).  All that's left in ``automation.py`` are two helpers
that ``server.py`` and ``handlers/sensors.py`` still call:

* :func:`automation.validate_automation` — schema check used by
  the REST handlers and by startup validation of the
  ``automations:`` block in ``server.json``.

* :func:`automation.migrate_ble_triggers` — one-shot migration
  from the legacy ``ble_triggers:`` config block to the modern
  ``automations:`` block, run on every startup.

This test file covers both.  The pre-2026-04 tests for the
deleted classes were removed in the same commit — do not
restore them; the dead code they exercised is gone.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "2.0"

import unittest
from typing import Any, Optional

from automation import (
    DEFAULT_WATCHDOG_MINUTES,
    VALID_CHARACTERISTICS,
    VALID_CONFLICT_POLICIES,
    _CONDITION_OPS,
    migrate_ble_triggers,
    validate_automation,
)


# ---------------------------------------------------------------------------
# Test data factory
# ---------------------------------------------------------------------------


def _make_automation(
    name: str = "test",
    label: str = "sensor1",
    characteristic: str = "motion",
    condition: str = "eq",
    value: Any = 1,
    group: str = "Living Room",
    effect: str = "on",
    params: Optional[dict] = None,
    off_type: str = "watchdog",
    off_minutes: float = 30.0,
    enabled: bool = True,
    policy: str = "defer",
) -> dict[str, Any]:
    """Build a valid automation entry for testing."""
    return {
        "name": name,
        "enabled": enabled,
        "sensor": {
            "type": "ble",
            "label": label,
            "characteristic": characteristic,
        },
        "trigger": {"condition": condition, "value": value},
        "action": {
            "group": group,
            "effect": effect,
            "params": params or {"brightness": 70},
        },
        "off_trigger": {"type": off_type, "minutes": off_minutes},
        "off_action": {"effect": "off", "params": {}},
        "schedule_conflict": policy,
    }


# ---------------------------------------------------------------------------
# validate_automation
# ---------------------------------------------------------------------------


class TestValidateAutomation(unittest.TestCase):
    """Tests for :func:`automation.validate_automation`."""

    KNOWN_GROUPS: set[str] = {"Living Room", "Bedroom"}
    KNOWN_EFFECTS: set[str] = {
        "on", "off", "breathe", "cylon", "spectrum2d", "soundlevel",
    }
    MEDIA_EFFECTS: set[str] = {"spectrum2d", "soundlevel"}

    def _validate(self, entry: dict) -> list[str]:
        return validate_automation(
            entry,
            self.KNOWN_GROUPS,
            self.KNOWN_EFFECTS,
            self.MEDIA_EFFECTS,
        )

    def test_valid_entry(self) -> None:
        """A well-formed entry returns no errors."""
        self.assertEqual(self._validate(_make_automation()), [])

    def test_missing_name(self) -> None:
        entry = _make_automation()
        del entry["name"]
        errors = self._validate(entry)
        self.assertTrue(any("name" in e.lower() for e in errors))

    def test_missing_sensor_label(self) -> None:
        entry = _make_automation()
        del entry["sensor"]["label"]
        errors = self._validate(entry)
        self.assertTrue(any("sensor.label" in e for e in errors))

    def test_invalid_characteristic(self) -> None:
        errors = self._validate(_make_automation(characteristic="bogus"))
        self.assertTrue(any("characteristic" in e for e in errors))

    def test_invalid_condition(self) -> None:
        errors = self._validate(_make_automation(condition="banana"))
        self.assertTrue(any("condition" in e for e in errors))

    def test_missing_trigger_value(self) -> None:
        entry = _make_automation()
        del entry["trigger"]["value"]
        errors = self._validate(entry)
        self.assertTrue(any("trigger.value" in e for e in errors))

    def test_unknown_group(self) -> None:
        errors = self._validate(_make_automation(group="Nonexistent"))
        self.assertTrue(any("Unknown group" in e for e in errors))

    def test_unknown_effect(self) -> None:
        errors = self._validate(_make_automation(effect="nope"))
        self.assertTrue(any("Unknown effect" in e for e in errors))

    def test_media_effect_rejected(self) -> None:
        """Media/audio effects are not allowed in automations."""
        errors = self._validate(_make_automation(effect="spectrum2d"))
        self.assertTrue(
            any("media" in e.lower() or "audio" in e.lower() for e in errors)
        )

    def test_invalid_watchdog_minutes(self) -> None:
        entry = _make_automation()
        entry["off_trigger"]["minutes"] = -5
        errors = self._validate(entry)
        self.assertTrue(any("minutes" in e for e in errors))

    def test_invalid_off_trigger_type(self) -> None:
        entry = _make_automation()
        entry["off_trigger"]["type"] = "bogus"
        errors = self._validate(entry)
        self.assertTrue(any("off_trigger.type" in e for e in errors))

    def test_invalid_conflict_policy(self) -> None:
        errors = self._validate(_make_automation(policy="bogus"))
        self.assertTrue(any("schedule_conflict" in e for e in errors))

    def test_garbage_sensor_type(self) -> None:
        """Non-dict sensor field doesn't crash."""
        entry = _make_automation()
        entry["sensor"] = 42
        self.assertTrue(len(self._validate(entry)) > 0)

    def test_garbage_trigger_type(self) -> None:
        """Non-dict trigger field doesn't crash."""
        entry = _make_automation()
        entry["trigger"] = "not a dict"
        self.assertTrue(len(self._validate(entry)) > 0)

    def test_garbage_conflict_policy_type(self) -> None:
        """Non-string conflict policy (e.g., list) doesn't crash."""
        entry = _make_automation()
        entry["schedule_conflict"] = [1, 2, 3]
        errors = self._validate(entry)
        self.assertTrue(any("schedule_conflict" in e for e in errors))

    def test_all_valid_characteristics(self) -> None:
        """Every known characteristic passes validation."""
        for char in VALID_CHARACTERISTICS:
            errors = self._validate(_make_automation(characteristic=char))
            self.assertEqual(
                errors, [], f"Characteristic {char!r} failed",
            )

    def test_all_valid_conditions(self) -> None:
        """Every known condition operator passes validation."""
        for op_name in _CONDITION_OPS:
            errors = self._validate(_make_automation(condition=op_name))
            self.assertEqual(
                errors, [], f"Condition {op_name!r} failed",
            )

    def test_all_valid_policies(self) -> None:
        """Every known conflict policy passes validation."""
        for policy in VALID_CONFLICT_POLICIES:
            errors = self._validate(_make_automation(policy=policy))
            self.assertEqual(
                errors, [], f"Policy {policy!r} failed",
            )

    def test_default_watchdog_minutes_constant_present(self) -> None:
        """``DEFAULT_WATCHDOG_MINUTES`` is exported and positive.

        ``validate_automation`` uses it as the default when an
        entry omits ``off_trigger.minutes``.  If the constant is
        ever deleted or set to a non-positive value, every entry
        without an explicit watchdog minutes will start failing
        validation silently.
        """
        self.assertIsInstance(DEFAULT_WATCHDOG_MINUTES, (int, float))
        self.assertGreater(DEFAULT_WATCHDOG_MINUTES, 0)


# ---------------------------------------------------------------------------
# migrate_ble_triggers
# ---------------------------------------------------------------------------


class TestMigrateBLETriggers(unittest.TestCase):
    """Tests for :func:`automation.migrate_ble_triggers`."""

    def test_basic_migration(self) -> None:
        """A ``ble_triggers`` block becomes an ``automations`` block."""
        config: dict = {
            "ble_triggers": {
                "onvis_motion": {
                    "group": "group:Living Room",
                    "on_motion": {"brightness": 70},
                    "watchdog_minutes": 30,
                },
            },
        }
        result: bool = migrate_ble_triggers(config)
        self.assertTrue(result)
        self.assertIn("automations", config)
        self.assertEqual(len(config["automations"]), 1)
        auto = config["automations"][0]
        self.assertEqual(auto["sensor"]["label"], "onvis_motion")
        self.assertEqual(auto["action"]["group"], "Living Room")
        self.assertEqual(auto["action"]["params"]["brightness"], 70)

    def test_strips_group_prefix(self) -> None:
        """``group:`` prefix is stripped during migration."""
        config: dict = {
            "ble_triggers": {
                "s1": {
                    "group": "group:Bedroom",
                    "on_motion": {"brightness": 50},
                },
            },
        }
        migrate_ble_triggers(config)
        self.assertEqual(
            config["automations"][0]["action"]["group"], "Bedroom",
        )

    def test_skips_if_automations_exist(self) -> None:
        """Migration is a no-op if ``automations`` already exists."""
        config: dict = {
            "ble_triggers": {"s1": {"group": "g", "on_motion": {}}},
            "automations": [{"name": "existing"}],
        }
        self.assertFalse(migrate_ble_triggers(config))

    def test_skips_if_no_ble_triggers(self) -> None:
        """No ``ble_triggers`` → ``False``, no mutation."""
        config: dict = {}
        self.assertFalse(migrate_ble_triggers(config))
        self.assertNotIn("automations", config)

    def test_multiple_triggers(self) -> None:
        """Multiple ``ble_triggers`` each become an automation."""
        config: dict = {
            "ble_triggers": {
                "s1": {
                    "group": "g1", "on_motion": {"brightness": 70},
                },
                "s2": {
                    "group": "g2", "on_motion": {"brightness": 50},
                },
            },
        }
        migrate_ble_triggers(config)
        self.assertEqual(len(config["automations"]), 2)

    def test_default_watchdog_minutes_when_missing(self) -> None:
        """An entry without ``watchdog_minutes`` gets the default."""
        config: dict = {
            "ble_triggers": {
                "s1": {"group": "g", "on_motion": {"brightness": 70}},
            },
        }
        migrate_ble_triggers(config)
        self.assertEqual(
            config["automations"][0]["off_trigger"]["minutes"],
            DEFAULT_WATCHDOG_MINUTES,
        )


if __name__ == "__main__":
    unittest.main()
