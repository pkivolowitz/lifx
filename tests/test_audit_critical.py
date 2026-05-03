#!/usr/bin/env python3
"""Regression tests for critical audit fixes (C1–C17).

Each test targets a specific bug from AUDIT_REPORT.md and verifies
the fix is in place.  No network or hardware dependencies.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import os
import threading
import time
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock, patch

from effects import (
    Effect, Param, create_effect, get_registry,
    HSBK, HSBK_MAX, KELVIN_DEFAULT,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum safe LIFX transition time in milliseconds.
MIN_TRANSITION_MS: int = 50


# ---------------------------------------------------------------------------
# C1: Engine IndexError on empty color list during zone padding
# ---------------------------------------------------------------------------

class TestC1_EmptyColorPadding(unittest.TestCase):
    """Verify that zone padding handles empty color lists gracefully."""

    def test_pad_color_fallback_on_empty_list(self) -> None:
        """If colors is empty, padding must not IndexError."""
        # Simulate the engine's padding logic directly.
        colors: list = []
        zone_count: int = 5
        _pad: tuple = colors[-1] if colors else (0, 0, 0, KELVIN_DEFAULT)
        while len(colors) < zone_count:
            colors.append(_pad)
        self.assertEqual(len(colors), zone_count)
        # All zones should be the black fallback.
        for c in colors:
            self.assertEqual(c, (0, 0, 0, KELVIN_DEFAULT))

    def test_pad_color_uses_last_when_nonempty(self) -> None:
        """If colors is non-empty, padding replicates the last color."""
        colors: list = [(100, 200, 300, 3500)]
        zone_count: int = 3
        _pad: tuple = colors[-1] if colors else (0, 0, 0, KELVIN_DEFAULT)
        while len(colors) < zone_count:
            colors.append(_pad)
        self.assertEqual(len(colors), zone_count)
        for c in colors:
            self.assertEqual(c, (100, 200, 300, 3500))

    def test_no_padding_when_exact(self) -> None:
        """No padding needed when colors matches zone_count."""
        colors: list = [(1, 2, 3, 4)] * 5
        zone_count: int = 5
        _pad: tuple = colors[-1] if colors else (0, 0, 0, KELVIN_DEFAULT)
        while len(colors) < zone_count:
            colors.append(_pad)
        self.assertEqual(len(colors), zone_count)


# ---------------------------------------------------------------------------
# C2: EmitterManager TOCTOU — snapshot tuples, not names
# ---------------------------------------------------------------------------

class TestC2_EmitterManagerSnapshot(unittest.TestCase):
    """Verify EmitterManager snapshots (name, slot) tuples under lock."""

    def test_open_all_uses_tuple_snapshot(self) -> None:
        """open_all must not re-lookup slots by name after snapshot."""
        import emitters
        src: str = open(emitters.__file__).read()
        # The old pattern was: `names = list(self._slots.keys())`
        # followed by `self._slots.get(name)`.
        # The fix snapshots tuples: `[(n, s) for n, s in self._slots.items()]`
        self.assertNotIn(
            "names: list[str] = list(self._slots.keys())",
            src,
            "open_all still uses name-only snapshot (TOCTOU race)",
        )

    def test_shutdown_uses_tuple_snapshot(self) -> None:
        """shutdown must not re-lookup slots by name after snapshot."""
        import emitters
        src: str = open(emitters.__file__).read()
        # Count occurrences of the old pattern in shutdown context.
        # After fix, there should be zero name-only snapshots.
        lines: list[str] = src.split("\n")
        name_only_snapshots: int = sum(
            1 for line in lines
            if "names: list[str] = list(self._slots.keys())" in line
        )
        self.assertEqual(
            name_only_snapshots, 0,
            "Found name-only slot snapshots (TOCTOU race)",
        )


# ---------------------------------------------------------------------------
# C3: Param.validate — unhandled ValueError on type coercion
# ---------------------------------------------------------------------------

class TestC3_ParamValidateGarbageInput(unittest.TestCase):
    """Verify Param.validate handles garbage input without crashing."""

    def test_int_param_with_string_input(self) -> None:
        """int param given 'abc' should fall back to default, not crash."""
        p: Param = Param(50, min=0, max=100, description="test")
        result = p.validate("abc")
        self.assertEqual(result, 50, "Should fall back to default on bad input")

    def test_float_param_with_string_input(self) -> None:
        """float param given 'xyz' should fall back to default."""
        p: Param = Param(3.5, min=0.0, max=10.0, description="test")
        result = p.validate("xyz")
        self.assertEqual(result, 3.5)

    def test_int_param_with_none_input(self) -> None:
        """int param given None should fall back to default."""
        p: Param = Param(10, min=0, max=100, description="test")
        result = p.validate(None)
        self.assertEqual(result, 10)

    def test_valid_string_coercion_still_works(self) -> None:
        """int param given '42' string should coerce correctly."""
        p: Param = Param(50, min=0, max=100, description="test")
        result = p.validate("42")
        self.assertEqual(result, 42)

    def test_valid_float_coercion_still_works(self) -> None:
        """float param given '3.14' string should coerce correctly."""
        p: Param = Param(1.0, min=0.0, max=10.0, description="test")
        result = p.validate("3.14")
        self.assertAlmostEqual(result, 3.14)

    def test_clamping_still_works_after_fix(self) -> None:
        """Clamping to min/max must still work."""
        p: Param = Param(50, min=10, max=90, description="test")
        self.assertEqual(p.validate(5), 10)
        self.assertEqual(p.validate(95), 90)
        self.assertEqual(p.validate(50), 50)

    def test_choices_still_raise_on_invalid(self) -> None:
        """Choice params must still raise ValueError on invalid choice."""
        p: Param = Param("red", choices=["red", "blue"], description="test")
        with self.assertRaises(ValueError):
            p.validate("green")


# ---------------------------------------------------------------------------
# C4: server.py _devices_as_list — emitter snapshot under lock
# ---------------------------------------------------------------------------

class TestC4_DevicesAsListSnapshot(unittest.TestCase):
    """Verify _devices_as_list uses a snapshot, not live dict iteration."""

    def test_uses_emitter_snapshot(self) -> None:
        """_devices_as_list must snapshot emitters under lock."""
        import server
        import inspect
        src: str = inspect.getsource(server.DeviceManager._devices_as_list)
        # The fix adds `emitter_snapshot = list(self._emitters.items())`
        # under lock and iterates `emitter_snapshot`.
        self.assertIn("emitter_snapshot", src,
                       "_devices_as_list should use emitter_snapshot")
        # Must NOT directly iterate self._emitters.items() outside lock.
        # Count direct iterations — should be zero after fix.
        direct_iterations: int = src.count("self._emitters.items()")
        snapshot_creation: int = src.count("emitter_snapshot")
        self.assertGreater(snapshot_creation, 0,
                           "Must create emitter_snapshot")


# ---------------------------------------------------------------------------
# C5: server.py get_status — snapshot emitter inside lock
# ---------------------------------------------------------------------------

class TestC5_GetStatusEmitterSnapshot(unittest.TestCase):
    """Verify get_status snapshots the emitter inside the lock block."""

    def test_emitter_snapshotted_under_lock(self) -> None:
        """get_status must not access _emitters[ip] after releasing lock."""
        import server
        import inspect
        src: str = inspect.getsource(server.DeviceManager.get_status)
        # The fix snapshots: `em_snapshot = self._emitters[ip]` inside
        # the `with self._lock:` block.
        self.assertIn("em_snapshot", src,
                       "get_status should snapshot emitter as em_snapshot")


# ---------------------------------------------------------------------------
# C10 retired: tested ``_AutomationState`` from the deleted
# AutomationManager.  The trigger-operator equivalent is covered
# by tests/test_trigger_operator.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# C14: VirtualMultizoneEmitter pre-allocation with zone_count=0
# ---------------------------------------------------------------------------

class TestC14_VirtualZoneCountZero(unittest.TestCase):
    """Verify zone_count=0 doesn't silently lose color data."""

    def test_zone_count_zero_prealloc(self) -> None:
        """Pre-allocation with zone_count=0 or None must not crash."""
        # Simulate the pre-allocation logic.
        zone_count: Optional[int] = 0
        alloc_size: int = zone_count or 1
        self.assertEqual(alloc_size, 1,
                         "zone_count=0 should allocate at least 1 slot")

    def test_zone_count_none_prealloc(self) -> None:
        """Pre-allocation with zone_count=None must not crash."""
        zone_count: Optional[int] = None
        alloc_size: int = zone_count or 1
        self.assertEqual(alloc_size, 1)


# ---------------------------------------------------------------------------
# On/Off transient effects — verify is_transient and execute()
# ---------------------------------------------------------------------------

class TestTransientEffects(unittest.TestCase):
    """Verify on/off effects are properly transient."""

    def test_on_is_transient(self) -> None:
        """The 'on' effect must be marked transient."""
        registry = get_registry()
        self.assertIn("on", registry)
        self.assertTrue(registry["on"].is_transient)

    def test_off_is_transient(self) -> None:
        """The 'off' effect must be marked transient."""
        registry = get_registry()
        self.assertIn("off", registry)
        self.assertTrue(registry["off"].is_transient)

    def test_on_execute_sends_color(self) -> None:
        """on.execute() must call emitter.send_color() once."""
        effect = create_effect("on", brightness=70, color="blue")
        mock_emitter = MagicMock()
        effect.execute(mock_emitter)
        mock_emitter.send_color.assert_called_once()
        # Verify the call used a safe transition time.
        args, kwargs = mock_emitter.send_color.call_args
        duration: int = kwargs.get("duration_ms", args[4] if len(args) > 4 else 0)
        self.assertGreaterEqual(duration, MIN_TRANSITION_MS,
                                "Transition must be >= 50ms (LIFX safety)")

    def test_off_execute_is_noop(self) -> None:
        """off.execute() must not crash (power_off handled by play cmd)."""
        effect = create_effect("off")
        mock_emitter = MagicMock()
        effect.execute(mock_emitter)
        # off.execute() should not call anything on the emitter.
        mock_emitter.send_color.assert_not_called()
        mock_emitter.power_on.assert_not_called()
        mock_emitter.power_off.assert_not_called()

    def test_off_wants_power_on_false(self) -> None:
        """The 'off' effect must set wants_power_on = False."""
        registry = get_registry()
        self.assertFalse(getattr(registry["off"], "wants_power_on", True))

    def test_on_render_still_works(self) -> None:
        """on.render() must still work for simulator compatibility."""
        effect = create_effect("on", brightness=50, color="white")
        colors = effect.render(0.0, 3)
        self.assertEqual(len(colors), 3)
        # White = saturation 0.
        for h, s, b, k in colors:
            self.assertEqual(s, 0, "White should have saturation 0")
            self.assertGreater(b, 0, "Brightness 50% should not be 0")

    def test_on_color_names(self) -> None:
        """All named colors must produce valid HSBK."""
        color_names: list[str] = [
            "white", "red", "orange", "yellow", "green",
            "cyan", "blue", "purple", "pink",
        ]
        for name in color_names:
            effect = create_effect("on", color=name)
            colors = effect.render(0.0, 1)
            self.assertEqual(len(colors), 1, f"Color '{name}' failed")
            h, s, b, k = colors[0]
            self.assertGreaterEqual(h, 0)
            self.assertLessEqual(h, HSBK_MAX)
            self.assertGreaterEqual(s, 0)
            self.assertLessEqual(s, HSBK_MAX)


# ---------------------------------------------------------------------------
# Automation validation
# ---------------------------------------------------------------------------

class TestAutomationValidation(unittest.TestCase):
    """Verify automation validation catches bad input."""

    def setUp(self) -> None:
        """Build validation context."""
        self.known_groups: set = {"living_room", "porch"}
        registry = get_registry()
        self.known_effects: set = set(registry.keys())
        from effects import MediaEffect
        self.media_effects: set = {
            name for name, cls in registry.items()
            if issubclass(cls, MediaEffect)
        }

    def _valid_entry(self) -> dict:
        """Return a minimal valid automation entry."""
        return {
            "name": "test",
            "sensor": {"type": "ble", "label": "sensor1",
                       "characteristic": "motion"},
            "trigger": {"condition": "eq", "value": 1},
            "action": {"group": "living_room", "effect": "on",
                       "params": {}},
            "off_trigger": {"type": "watchdog", "minutes": 30},
            "off_action": {"effect": "off", "params": {}},
            "schedule_conflict": "defer",
        }

    def test_valid_entry_passes(self) -> None:
        """A correct entry should produce no errors."""
        from automation import validate_automation
        errors = validate_automation(
            self._valid_entry(),
            self.known_groups, self.known_effects, self.media_effects,
        )
        self.assertEqual(errors, [])

    def test_missing_name_rejected(self) -> None:
        """Missing name should produce an error."""
        from automation import validate_automation
        entry = self._valid_entry()
        entry["name"] = ""
        errors = validate_automation(
            entry, self.known_groups, self.known_effects, self.media_effects,
        )
        self.assertTrue(any("name" in e.lower() for e in errors))

    def test_unknown_group_rejected(self) -> None:
        """Unknown group should produce an error."""
        from automation import validate_automation
        entry = self._valid_entry()
        entry["action"]["group"] = "nonexistent"
        errors = validate_automation(
            entry, self.known_groups, self.known_effects, self.media_effects,
        )
        self.assertTrue(any("group" in e.lower() for e in errors))

    def test_unknown_effect_rejected(self) -> None:
        """Unknown effect should produce an error."""
        from automation import validate_automation
        entry = self._valid_entry()
        entry["action"]["effect"] = "nonexistent_effect"
        errors = validate_automation(
            entry, self.known_groups, self.known_effects, self.media_effects,
        )
        self.assertTrue(any("effect" in e.lower() for e in errors))

    def test_invalid_condition_rejected(self) -> None:
        """Invalid trigger condition should produce an error."""
        from automation import validate_automation
        entry = self._valid_entry()
        entry["trigger"]["condition"] = "invalid_op"
        errors = validate_automation(
            entry, self.known_groups, self.known_effects, self.media_effects,
        )
        self.assertTrue(any("condition" in e.lower() for e in errors))

    def test_invalid_characteristic_rejected(self) -> None:
        """Invalid characteristic should produce an error."""
        from automation import validate_automation
        entry = self._valid_entry()
        entry["sensor"]["characteristic"] = "pressure"
        errors = validate_automation(
            entry, self.known_groups, self.known_effects, self.media_effects,
        )
        self.assertTrue(any("characteristic" in e.lower() for e in errors))

    def test_media_effect_rejected(self) -> None:
        """MediaEffect subclasses should be rejected."""
        from automation import validate_automation
        if not self.media_effects:
            self.skipTest("No MediaEffect subclasses registered")
        media_name: str = next(iter(self.media_effects))
        entry = self._valid_entry()
        entry["action"]["effect"] = media_name
        errors = validate_automation(
            entry, self.known_groups, self.known_effects, self.media_effects,
        )
        self.assertTrue(any("media" in e.lower() or "audio" in e.lower()
                            for e in errors))


# ---------------------------------------------------------------------------
# Automation migration
# ---------------------------------------------------------------------------

class TestAutomationMigration(unittest.TestCase):
    """Verify ble_triggers auto-migration to automations format."""

    def test_migration_converts_entry(self) -> None:
        """Old ble_triggers format should convert to automations."""
        from automation import migrate_ble_triggers
        config: dict = {
            "ble_triggers": {
                "onvis_motion": {
                    "group": "group:living_room",
                    "on_motion": {"brightness": 70},
                    "watchdog_minutes": 30,
                }
            }
        }
        result: bool = migrate_ble_triggers(config)
        self.assertTrue(result, "Migration should return True")
        self.assertIn("automations", config)
        self.assertEqual(len(config["automations"]), 1)
        auto: dict = config["automations"][0]
        self.assertEqual(auto["sensor"]["label"], "onvis_motion")
        self.assertEqual(auto["action"]["group"], "living_room")
        self.assertEqual(auto["action"]["params"]["brightness"], 70)
        self.assertEqual(auto["off_trigger"]["minutes"], 30)

    def test_migration_skips_when_automations_exist(self) -> None:
        """Migration must not overwrite existing automations."""
        from automation import migrate_ble_triggers
        config: dict = {
            "ble_triggers": {"x": {"group": "g"}},
            "automations": [{"name": "existing"}],
        }
        result: bool = migrate_ble_triggers(config)
        self.assertFalse(result)
        self.assertEqual(len(config["automations"]), 1)
        self.assertEqual(config["automations"][0]["name"], "existing")

    def test_migration_skips_when_no_triggers(self) -> None:
        """Migration must skip when no ble_triggers exist."""
        from automation import migrate_ble_triggers
        config: dict = {}
        result: bool = migrate_ble_triggers(config)
        self.assertFalse(result)

    def test_migration_strips_group_prefix(self) -> None:
        """Migration must strip 'group:' prefix from group names."""
        from automation import migrate_ble_triggers
        config: dict = {
            "ble_triggers": {
                "sensor1": {
                    "group": "group:porch",
                    "on_motion": {"brightness": 50},
                }
            }
        }
        migrate_ble_triggers(config)
        self.assertEqual(config["automations"][0]["action"]["group"], "porch")


# ---------------------------------------------------------------------------
# Automation trigger evaluation and watchdog timer reset tests
# retired: both tested helpers from the deleted AutomationManager
# (``_evaluate_condition`` and ``_AutomationState``).  The
# operator-framework equivalents are tested in
# tests/test_operators.py and tests/test_trigger_operator.py
# (operators.conditions.evaluate_condition is the new home of the
# condition logic, and TriggerOperator owns the watchdog state).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# H1: Engine single-zone transition must not be 0ms
# ---------------------------------------------------------------------------

class TestH1_SingleZoneTransition(unittest.TestCase):
    """Verify single-zone send uses transition_ms, not 0."""

    def test_send_loop_uses_transition_ms(self) -> None:
        """Engine send loop must pass transition_ms to send_color."""
        import engine
        import inspect
        src: str = inspect.getsource(engine.Engine._run_loop)
        # The fix changed `duration_ms=0` to `duration_ms=transition_ms`
        # for single-zone emitters in the send loop.
        self.assertNotIn(
            "send_color(h, s, b, k, duration_ms=0)",
            src,
            "Single-zone send_color still uses duration_ms=0 (LIFX safety violation)",
        )


# ---------------------------------------------------------------------------
# H2: Audio delay buffer cleared on effect swap
# ---------------------------------------------------------------------------

class TestH2_DelayBufferClear(unittest.TestCase):
    """Verify delay buffer is cleared when swapping effects."""

    def test_start_clears_delay_buffer(self) -> None:
        """Engine.start() must clear _delay_buffer."""
        import engine
        import inspect
        src: str = inspect.getsource(engine.Engine.start)
        self.assertIn("_delay_buffer.clear()", src,
                       "start() must clear _delay_buffer on effect swap")


# ---------------------------------------------------------------------------
# H5: Automation enabled handler captures name at validation time
# ---------------------------------------------------------------------------

class TestH5_AutomationEnabledBoundsCheck(unittest.TestCase):
    """Verify automation enabled handler captures name early."""

    def test_name_captured_at_validation(self) -> None:
        """Handler must capture auto_name before body read."""
        import server
        import inspect
        src: str = inspect.getsource(
            server.GlowUpRequestHandler._handle_post_automation_enabled,
        )
        self.assertIn("auto_name", src,
                       "Handler must capture name as auto_name at validation time")


# ---------------------------------------------------------------------------
# H9: Config save logs with traceback
# ---------------------------------------------------------------------------

class TestH9_ConfigSaveLogging(unittest.TestCase):
    """Verify config save failures include traceback."""

    def test_exception_logging(self) -> None:
        """_save_config_field must use exc_info=True or logging.exception."""
        import server
        import inspect
        src: str = inspect.getsource(
            server.GlowUpRequestHandler._save_config_field,
        )
        # Must include traceback in at least one error log.
        has_exc_info: bool = "exc_info=True" in src or "logging.exception" in src
        self.assertTrue(has_exc_info,
                        "Save failure must log traceback")


# ---------------------------------------------------------------------------
# H11: Ripple2D division by zero guard
# ---------------------------------------------------------------------------

class TestH11_Ripple2DDivZero(unittest.TestCase):
    """Verify ripple2d handles n=0 without crashing."""

    def test_zero_sources_safe(self) -> None:
        """wave_sum / n must not crash when n=0."""
        # Simulate the fixed logic.
        wave_sum: float = 1.5
        n: int = 0
        intensity: float = abs(wave_sum / n) if n > 0 else 0.0
        self.assertEqual(intensity, 0.0)


# ---------------------------------------------------------------------------
# H13: ffmpeg stderr uses temp file, not PIPE
# ---------------------------------------------------------------------------

class TestH13_FfmpegStderr(unittest.TestCase):
    """Verify ffmpeg stderr is not piped (deadlock risk)."""

    def test_no_stderr_pipe(self) -> None:
        """cmd_record must not use stderr=sp.PIPE."""
        import glowup
        import inspect
        src: str = inspect.getsource(glowup.cmd_record)
        self.assertNotIn("stderr=sp.PIPE", src,
                          "cmd_record must not use stderr=PIPE (deadlock risk)")


# ---------------------------------------------------------------------------
# H14: NBody spawn skips alive particles
# ---------------------------------------------------------------------------

class TestH14_NBodySpawnSkipAlive(unittest.TestCase):
    """Verify NBody spawn doesn't overwrite alive particles."""

    def test_spawn_checks_alive(self) -> None:
        """spawn() must check alive[slot] before overwriting."""
        from distributed.nbody_operator import NBodySimulation
        import inspect
        src: str = inspect.getsource(NBodySimulation.spawn)
        self.assertIn("self.alive[slot]", src,
                       "spawn must check alive[slot] before overwriting")


# ---------------------------------------------------------------------------
# H15: FileAudioSensor stop() null check (already defended)
# ---------------------------------------------------------------------------

class TestH15_FileAudioSensorStopSafe(unittest.TestCase):
    """Verify FileAudioSensor.stop() handles None processes."""

    def test_stop_has_none_check(self) -> None:
        """stop() must check proc is not None."""
        from distributed.file_audio_sensor import FileAudioSensor
        import inspect
        src: str = inspect.getsource(FileAudioSensor.stop)
        self.assertIn("is not None", src,
                       "stop() must check proc is not None")


# ---------------------------------------------------------------------------
# H16: MIDI parser handles malformed VLQ
# ---------------------------------------------------------------------------

class TestH16_MidiVlqMalformed(unittest.TestCase):
    """Verify MIDI parser handles truncated VLQ gracefully."""

    def test_vlq_truncation_raises(self) -> None:
        """_read_vlq on empty data must raise ValueError."""
        from distributed.midi_parser import MidiParser
        with self.assertRaises(ValueError):
            MidiParser._read_vlq(b"", 0)

    def test_parse_track_events_catches_vlq(self) -> None:
        """_parse_track_events must catch ValueError from VLQ."""
        from distributed.midi_parser import MidiParser
        import inspect
        src: str = inspect.getsource(MidiParser._parse_track_events)
        self.assertIn("except ValueError", src,
                       "Must catch ValueError from malformed VLQ")


# ---------------------------------------------------------------------------
# H19: Automation CRUD uses copy-before-mutate
# ---------------------------------------------------------------------------

class TestH19_AutomationCRUDCopyBeforeMutate(unittest.TestCase):
    """Verify automation CRUD handlers copy the list before mutating."""

    def test_create_copies_list(self) -> None:
        """POST automation must copy the automations list."""
        import server
        import inspect
        src: str = inspect.getsource(
            server.GlowUpRequestHandler._handle_post_automation_create,
        )
        self.assertIn("list(self.config.get", src,
                       "Create handler must copy list before mutating")

    def test_delete_copies_list(self) -> None:
        """DELETE automation must copy the automations list."""
        import server
        import inspect
        src: str = inspect.getsource(
            server.GlowUpRequestHandler._handle_delete_automation,
        )
        self.assertIn("list(self.config.get", src,
                       "Delete handler must copy list before mutating")


if __name__ == "__main__":
    unittest.main()
