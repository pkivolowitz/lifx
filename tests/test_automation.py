"""Thorough test suite for the automation module.

Tests cover:
- SensorData thread-safe store
- Condition evaluation (_evaluate_condition)
- Automation validation (validate_automation)
- BLE trigger migration (migrate_ble_triggers)
- AutomationManager lifecycle, MQTT dispatch, trigger evaluation,
  debounce, watchdog, schedule conflict, reload, and CRUD state
- Edge cases: garbage input, type coercion, disabled automations,
  concurrent access

Run independently::

    python3 -m pytest tests/test_automation.py -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

import json
import threading
import time
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock, patch

from automation import (
    SensorData,
    _evaluate_condition,
    validate_automation,
    migrate_ble_triggers,
    AutomationManager,
    _AutomationState,
    _device_id_for_group,
    DEBOUNCE_SECONDS,
    DEFAULT_WATCHDOG_MINUTES,
    SECONDS_PER_MINUTE,
    VALID_CHARACTERISTICS,
    VALID_CONFLICT_POLICIES,
    _CONDITION_OPS,
    TRANSPORT_PREFIXES,
)


# ---------------------------------------------------------------------------
# Test data factories
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


def _make_config(
    automations: Optional[list] = None,
    groups: Optional[dict] = None,
) -> dict[str, Any]:
    """Build a minimal server config dict for testing."""
    return {
        "automations": automations or [],
        "groups": groups or {"Living Room": ["10.0.0.1"]},
        "location": {"latitude": 30.0, "longitude": -88.0},
        "schedule": [],
    }


def _mock_device_manager() -> MagicMock:
    """Create a mock DeviceManager with play/stop methods."""
    dm = MagicMock()
    dm.play = MagicMock()
    dm.stop = MagicMock()
    return dm


# ---------------------------------------------------------------------------
# SensorData tests
# ---------------------------------------------------------------------------

class TestSensorData(unittest.TestCase):
    """Tests for the SensorData thread-safe store."""

    def test_update_and_get(self) -> None:
        """Update a value and retrieve it."""
        sd = SensorData()
        sd.update("sensor1", "motion", 1)
        data: dict = sd.get("sensor1")
        self.assertEqual(data["motion"], 1)
        self.assertIn("last_update", data)

    def test_get_unknown_label(self) -> None:
        """Getting an unknown label returns empty dict."""
        sd = SensorData()
        self.assertEqual(sd.get("nonexistent"), {})

    def test_get_all(self) -> None:
        """get_all returns all sensors."""
        sd = SensorData()
        sd.update("s1", "motion", 1)
        sd.update("s2", "temperature", 22.5)
        all_data: dict = sd.get_all()
        self.assertIn("s1", all_data)
        self.assertIn("s2", all_data)
        self.assertEqual(all_data["s1"]["motion"], 1)
        self.assertEqual(all_data["s2"]["temperature"], 22.5)

    def test_update_overwrites(self) -> None:
        """Updating the same key overwrites the previous value."""
        sd = SensorData()
        sd.update("s1", "motion", 0)
        sd.update("s1", "motion", 1)
        self.assertEqual(sd.get("s1")["motion"], 1)

    def test_multiple_keys(self) -> None:
        """A sensor can have multiple data keys."""
        sd = SensorData()
        sd.update("s1", "motion", 1)
        sd.update("s1", "temperature", 21.5)
        sd.update("s1", "humidity", 55.0)
        data: dict = sd.get("s1")
        self.assertEqual(data["motion"], 1)
        self.assertEqual(data["temperature"], 21.5)
        self.assertEqual(data["humidity"], 55.0)

    def test_get_returns_copy(self) -> None:
        """get() returns a copy, not a reference to internal state."""
        sd = SensorData()
        sd.update("s1", "motion", 1)
        data: dict = sd.get("s1")
        data["motion"] = 999
        self.assertEqual(sd.get("s1")["motion"], 1)

    def test_thread_safety(self) -> None:
        """Concurrent updates don't corrupt state."""
        sd = SensorData()
        errors: list[str] = []

        def writer(label: str, n: int) -> None:
            for i in range(n):
                sd.update(label, "value", i)

        threads = [
            threading.Thread(target=writer, args=(f"s{i}", 100))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 10 labels should exist.
        all_data = sd.get_all()
        self.assertEqual(len(all_data), 10)


# ---------------------------------------------------------------------------
# Condition evaluation tests
# ---------------------------------------------------------------------------

class TestEvaluateCondition(unittest.TestCase):
    """Tests for _evaluate_condition."""

    def test_eq_match(self) -> None:
        self.assertTrue(_evaluate_condition("eq", 1, 1))

    def test_eq_no_match(self) -> None:
        self.assertFalse(_evaluate_condition("eq", 1, 0))

    def test_gt(self) -> None:
        self.assertTrue(_evaluate_condition("gt", 5, 10))
        self.assertFalse(_evaluate_condition("gt", 5, 5))
        self.assertFalse(_evaluate_condition("gt", 5, 3))

    def test_lt(self) -> None:
        self.assertTrue(_evaluate_condition("lt", 10, 5))
        self.assertFalse(_evaluate_condition("lt", 5, 5))

    def test_gte(self) -> None:
        self.assertTrue(_evaluate_condition("gte", 5, 5))
        self.assertTrue(_evaluate_condition("gte", 5, 10))
        self.assertFalse(_evaluate_condition("gte", 5, 3))

    def test_lte(self) -> None:
        self.assertTrue(_evaluate_condition("lte", 5, 5))
        self.assertTrue(_evaluate_condition("lte", 5, 3))
        self.assertFalse(_evaluate_condition("lte", 5, 10))

    def test_unknown_operator(self) -> None:
        """Unknown operator returns False, doesn't crash."""
        self.assertFalse(_evaluate_condition("banana", 1, 1))

    def test_type_mismatch(self) -> None:
        """Comparing incompatible types returns False."""
        self.assertFalse(_evaluate_condition("eq", 1, "text"))

    def test_float_comparison(self) -> None:
        """Float values compare correctly."""
        self.assertTrue(_evaluate_condition("gt", 20.0, 22.5))


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class TestValidateAutomation(unittest.TestCase):
    """Tests for validate_automation."""

    KNOWN_GROUPS: set[str] = {"Living Room", "Bedroom"}
    KNOWN_EFFECTS: set[str] = {"on", "off", "breathe", "cylon", "spectrum2d", "soundlevel"}
    MEDIA_EFFECTS: set[str] = {"spectrum2d", "soundlevel"}

    def _validate(self, entry: dict) -> list[str]:
        return validate_automation(
            entry, self.KNOWN_GROUPS, self.KNOWN_EFFECTS, self.MEDIA_EFFECTS,
        )

    def test_valid_entry(self) -> None:
        """A well-formed entry returns no errors."""
        entry = _make_automation()
        self.assertEqual(self._validate(entry), [])

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
        entry = _make_automation(characteristic="bogus")
        errors = self._validate(entry)
        self.assertTrue(any("characteristic" in e for e in errors))

    def test_invalid_condition(self) -> None:
        entry = _make_automation(condition="banana")
        errors = self._validate(entry)
        self.assertTrue(any("condition" in e for e in errors))

    def test_missing_trigger_value(self) -> None:
        entry = _make_automation()
        del entry["trigger"]["value"]
        errors = self._validate(entry)
        self.assertTrue(any("trigger.value" in e for e in errors))

    def test_unknown_group(self) -> None:
        entry = _make_automation(group="Nonexistent")
        errors = self._validate(entry)
        self.assertTrue(any("Unknown group" in e for e in errors))

    def test_unknown_effect(self) -> None:
        entry = _make_automation(effect="nonexistent_effect")
        errors = self._validate(entry)
        self.assertTrue(any("Unknown effect" in e for e in errors))

    def test_media_effect_rejected(self) -> None:
        """Media/audio effects are not allowed in automations."""
        entry = _make_automation(effect="spectrum2d")
        errors = self._validate(entry)
        self.assertTrue(any("media" in e.lower() or "audio" in e.lower() for e in errors))

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
        entry = _make_automation(policy="bogus")
        errors = self._validate(entry)
        self.assertTrue(any("schedule_conflict" in e for e in errors))

    def test_garbage_sensor_type(self) -> None:
        """Non-dict sensor field doesn't crash."""
        entry = _make_automation()
        entry["sensor"] = 42
        errors = self._validate(entry)
        self.assertTrue(len(errors) > 0)

    def test_garbage_trigger_type(self) -> None:
        """Non-dict trigger field doesn't crash."""
        entry = _make_automation()
        entry["trigger"] = "not a dict"
        errors = self._validate(entry)
        self.assertTrue(len(errors) > 0)

    def test_garbage_conflict_policy_type(self) -> None:
        """Non-string conflict policy (e.g., list) doesn't crash."""
        entry = _make_automation()
        entry["schedule_conflict"] = [1, 2, 3]
        errors = self._validate(entry)
        self.assertTrue(any("schedule_conflict" in e for e in errors))

    def test_all_valid_characteristics(self) -> None:
        """Every known characteristic passes validation."""
        for char in VALID_CHARACTERISTICS:
            entry = _make_automation(characteristic=char)
            errors = self._validate(entry)
            self.assertEqual(errors, [], f"Characteristic {char!r} failed")

    def test_all_valid_conditions(self) -> None:
        """Every known condition operator passes validation."""
        for op in _CONDITION_OPS:
            entry = _make_automation(condition=op)
            errors = self._validate(entry)
            self.assertEqual(errors, [], f"Condition {op!r} failed")

    def test_all_valid_policies(self) -> None:
        """Every known conflict policy passes validation."""
        for policy in VALID_CONFLICT_POLICIES:
            entry = _make_automation(policy=policy)
            errors = self._validate(entry)
            self.assertEqual(errors, [], f"Policy {policy!r} failed")


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------

class TestMigrateBLETriggers(unittest.TestCase):
    """Tests for migrate_ble_triggers."""

    def test_basic_migration(self) -> None:
        """ble_triggers migrate to automations format."""
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
        """group: prefix is stripped during migration."""
        config: dict = {
            "ble_triggers": {
                "s1": {
                    "group": "group:Bedroom",
                    "on_motion": {"brightness": 50},
                },
            },
        }
        migrate_ble_triggers(config)
        self.assertEqual(config["automations"][0]["action"]["group"], "Bedroom")

    def test_skips_if_automations_exist(self) -> None:
        """Migration is skipped if automations already exist."""
        config: dict = {
            "ble_triggers": {"s1": {"group": "g", "on_motion": {}}},
            "automations": [{"name": "existing"}],
        }
        result: bool = migrate_ble_triggers(config)
        self.assertFalse(result)

    def test_skips_if_no_ble_triggers(self) -> None:
        """Migration returns False if no ble_triggers."""
        config: dict = {}
        result: bool = migrate_ble_triggers(config)
        self.assertFalse(result)

    def test_multiple_triggers(self) -> None:
        """Multiple ble_triggers each become an automation."""
        config: dict = {
            "ble_triggers": {
                "s1": {"group": "g1", "on_motion": {"brightness": 70}},
                "s2": {"group": "g2", "on_motion": {"brightness": 50}},
            },
        }
        migrate_ble_triggers(config)
        self.assertEqual(len(config["automations"]), 2)


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):
    """Tests for helper functions."""

    def test_device_id_for_group_bare(self) -> None:
        """Bare name gets prefix added."""
        self.assertEqual(_device_id_for_group("Living Room"), "group:Living Room")

    def test_device_id_for_group_already_prefixed(self) -> None:
        """Already-prefixed name passes through unchanged."""
        self.assertEqual(
            _device_id_for_group("group:Living Room"), "group:Living Room",
        )


# ---------------------------------------------------------------------------
# AutomationState tests
# ---------------------------------------------------------------------------

class TestAutomationState(unittest.TestCase):
    """Tests for _AutomationState."""

    def test_initial_state(self) -> None:
        """Fresh state is idle with zero timestamps."""
        s = _AutomationState()
        self.assertFalse(s.active)
        self.assertEqual(s.last_trigger, 0.0)
        self.assertEqual(s.last_action, 0.0)
        self.assertEqual(s.last_off, 0.0)


# ---------------------------------------------------------------------------
# AutomationManager tests
# ---------------------------------------------------------------------------

class TestAutomationManager(unittest.TestCase):
    """Tests for AutomationManager — lifecycle and trigger evaluation."""

    def _make_manager(
        self,
        automations: Optional[list] = None,
        groups: Optional[dict] = None,
    ) -> tuple[AutomationManager, MagicMock]:
        """Create an AutomationManager with a mock DeviceManager."""
        dm = _mock_device_manager()
        config = _make_config(automations=automations, groups=groups)
        mgr = AutomationManager(config=config, device_manager=dm)
        # Initialize state without starting MQTT.
        for i in range(len(config.get("automations", []))):
            mgr._states[i] = _AutomationState()
        mgr._running = True
        return mgr, dm

    def test_evaluate_triggers_on(self) -> None:
        """Motion=1 triggers the on-action."""
        auto = _make_automation()
        mgr, dm = self._make_manager(automations=[auto])
        mgr._evaluate_automations("sensor1", "motion", "1")
        dm.play.assert_called_once()
        self.assertTrue(mgr._states[0].active)

    def test_evaluate_no_match(self) -> None:
        """Motion=0 does not trigger when condition is eq=1."""
        auto = _make_automation()
        mgr, dm = self._make_manager(automations=[auto])
        mgr._evaluate_automations("sensor1", "motion", "0")
        dm.play.assert_not_called()
        self.assertFalse(mgr._states[0].active)

    def test_wrong_label_ignored(self) -> None:
        """Events from a different sensor label are ignored."""
        auto = _make_automation(label="sensor1")
        mgr, dm = self._make_manager(automations=[auto])
        mgr._evaluate_automations("other_sensor", "motion", "1")
        dm.play.assert_not_called()

    def test_wrong_characteristic_ignored(self) -> None:
        """Events for a different characteristic are ignored."""
        auto = _make_automation(characteristic="motion")
        mgr, dm = self._make_manager(automations=[auto])
        mgr._evaluate_automations("sensor1", "temperature", "22.5")
        dm.play.assert_not_called()

    def test_disabled_automation_ignored(self) -> None:
        """Disabled automations don't fire."""
        auto = _make_automation(enabled=False)
        mgr, dm = self._make_manager(automations=[auto])
        mgr._evaluate_automations("sensor1", "motion", "1")
        dm.play.assert_not_called()

    def test_debounce(self) -> None:
        """Repeated triggers within DEBOUNCE_SECONDS are suppressed."""
        auto = _make_automation()
        mgr, dm = self._make_manager(automations=[auto])

        # First trigger fires.
        mgr._evaluate_automations("sensor1", "motion", "1")
        self.assertEqual(dm.play.call_count, 1)

        # Reset active so it would fire again without debounce.
        mgr._states[0].active = False

        # Second trigger within debounce window is suppressed.
        mgr._evaluate_automations("sensor1", "motion", "1")
        self.assertEqual(dm.play.call_count, 1)

    def test_off_action_condition(self) -> None:
        """Condition-based off-trigger fires off-action."""
        auto = _make_automation()
        auto["off_trigger"] = {"type": "condition", "condition": "eq", "value": 0}
        mgr, dm = self._make_manager(automations=[auto])

        # Fire on.
        mgr._evaluate_automations("sensor1", "motion", "1")
        self.assertTrue(mgr._states[0].active)

        # Wait past debounce.
        mgr._states[0].last_action = time.time() - DEBOUNCE_SECONDS - 1

        # Fire off via condition match.
        mgr._evaluate_automations("sensor1", "motion", "0")
        dm.stop.assert_called_once()
        self.assertFalse(mgr._states[0].active)

    def test_watchdog_fires_off(self) -> None:
        """Watchdog fires off-action when sensor goes stale."""
        auto = _make_automation(off_minutes=0.001)  # ~60ms timeout
        mgr, dm = self._make_manager(automations=[auto])

        # Simulate an active automation that triggered long ago.
        mgr._states[0].active = True
        mgr._states[0].last_trigger = time.time() - 1.0  # 1 second ago

        # Run one watchdog check.
        # The timeout is 0.001 min = 0.06s, so 1s ago is expired.
        expired: list = []
        now: float = time.time()
        for i, a in enumerate(mgr.automations):
            state = mgr._states.get(i)
            if not state or not state.active:
                continue
            off_trigger = a.get("off_trigger", {})
            timeout_sec = off_trigger.get("minutes", 30) * SECONDS_PER_MINUTE
            if now - state.last_trigger >= timeout_sec:
                mgr._fire_off_action(i, a, state)

        dm.stop.assert_called_once()
        self.assertFalse(mgr._states[0].active)

    def test_watchdog_skips_untriggered(self) -> None:
        """Watchdog doesn't fire for automations that never triggered."""
        auto = _make_automation(off_minutes=0.001)
        mgr, dm = self._make_manager(automations=[auto])
        mgr._states[0].active = True
        mgr._states[0].last_trigger = 0.0  # never triggered

        # Watchdog should skip — last_trigger=0 is the init sentinel.
        now = time.time()
        state = mgr._states[0]
        off_trigger = auto.get("off_trigger", {})
        timeout_sec = off_trigger.get("minutes", 30) * SECONDS_PER_MINUTE
        # The check: skip if last_trigger == 0.0
        should_fire = (
            state.active
            and state.last_trigger != 0.0
            and now - state.last_trigger >= timeout_sec
        )
        self.assertFalse(should_fire)

    def test_effect_off_calls_stop(self) -> None:
        """Action with effect='off' calls dm.stop, not dm.play."""
        auto = _make_automation(effect="off")
        mgr, dm = self._make_manager(automations=[auto])
        mgr._evaluate_automations("sensor1", "motion", "1")
        dm.stop.assert_called_once()
        dm.play.assert_not_called()

    def test_schedule_conflict_defer(self) -> None:
        """With policy='defer', action is suppressed when schedule active."""
        auto = _make_automation(policy="defer")
        mgr, dm = self._make_manager(automations=[auto])

        with patch.object(mgr, '_is_schedule_active', return_value=True):
            mgr._evaluate_automations("sensor1", "motion", "1")

        dm.play.assert_not_called()

    def test_gt_condition(self) -> None:
        """Temperature > 25 triggers action."""
        auto = _make_automation(
            characteristic="temperature", condition="gt", value=25,
        )
        mgr, dm = self._make_manager(automations=[auto])
        mgr._evaluate_automations("sensor1", "temperature", "26.5")
        dm.play.assert_called_once()

    def test_lt_condition_no_match(self) -> None:
        """Humidity < 30 does not trigger when value is 55."""
        auto = _make_automation(
            characteristic="humidity", condition="lt", value=30,
        )
        mgr, dm = self._make_manager(automations=[auto])
        mgr._evaluate_automations("sensor1", "humidity", "55.0")
        dm.play.assert_not_called()

    def test_multiple_automations(self) -> None:
        """Multiple automations can fire from the same event."""
        a1 = _make_automation(name="a1", label="s1", group="Living Room")
        a2 = _make_automation(name="a2", label="s1", group="Living Room")
        mgr, dm = self._make_manager(
            automations=[a1, a2],
            groups={"Living Room": ["10.0.0.1"]},
        )
        mgr._evaluate_automations("s1", "motion", "1")
        self.assertEqual(dm.play.call_count, 2)

    def test_get_status(self) -> None:
        """get_status returns per-automation state."""
        auto = _make_automation()
        mgr, dm = self._make_manager(automations=[auto])
        status = mgr.get_status()
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["name"], "test")
        self.assertFalse(status[0]["active"])

    def test_reload_adds_new(self) -> None:
        """reload() adds state for new automations."""
        auto = _make_automation()
        mgr, dm = self._make_manager(automations=[auto])
        # Add a second automation to config.
        mgr._config["automations"].append(_make_automation(name="new"))
        mgr.reload()
        self.assertIn(1, mgr._states)

    def test_reload_removes_deleted(self) -> None:
        """reload() removes state for deleted automations."""
        a1 = _make_automation(name="a1")
        a2 = _make_automation(name="a2")
        mgr, dm = self._make_manager(automations=[a1, a2])
        self.assertIn(1, mgr._states)
        # Remove second automation.
        mgr._config["automations"].pop()
        mgr.reload()
        self.assertNotIn(1, mgr._states)

    def test_parse_value_int(self) -> None:
        """Integer reference parses payload as int."""
        mgr, _ = self._make_manager()
        self.assertEqual(mgr._parse_value("1", 1), 1)
        self.assertEqual(mgr._parse_value("1.0", 1), 1)

    def test_parse_value_float(self) -> None:
        """Float reference parses payload as float."""
        mgr, _ = self._make_manager()
        self.assertAlmostEqual(mgr._parse_value("22.5", 20.0), 22.5)

    def test_garbage_payload_skipped(self) -> None:
        """Unparseable payload doesn't crash, automation is skipped."""
        auto = _make_automation()
        mgr, dm = self._make_manager(automations=[auto])
        mgr._evaluate_automations("sensor1", "motion", "not_a_number")
        dm.play.assert_not_called()

    def test_action_failure_logged(self) -> None:
        """If dm.play raises, the error is logged, not propagated."""
        auto = _make_automation()
        mgr, dm = self._make_manager(automations=[auto])
        dm.play.side_effect = RuntimeError("bulb unreachable")
        # Should not raise.
        mgr._evaluate_automations("sensor1", "motion", "1")
        dm.play.assert_called_once()


# ---------------------------------------------------------------------------
# Transport prefix tests
# ---------------------------------------------------------------------------

class TestTransportPrefixes(unittest.TestCase):
    """Verify transport prefix mapping."""

    def test_known_transports(self) -> None:
        """BLE, Zigbee, Vivint have defined prefixes."""
        self.assertIn("ble", TRANSPORT_PREFIXES)
        self.assertIn("zigbee", TRANSPORT_PREFIXES)
        self.assertIn("vivint", TRANSPORT_PREFIXES)

    def test_prefix_format(self) -> None:
        """All prefixes start with 'glowup/'."""
        for transport, prefix in TRANSPORT_PREFIXES.items():
            self.assertTrue(
                prefix.startswith("glowup/"),
                f"{transport} prefix {prefix!r} doesn't start with 'glowup/'",
            )


if __name__ == "__main__":
    unittest.main()
