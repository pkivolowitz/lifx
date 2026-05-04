"""Tests for config-driven voice executor — dispatch, handlers, chat."""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import time
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from voice.coordinator.executor import (
    GlowUpExecutor,
    _CHAT_HISTORY_MAX,
    _CHAT_HISTORY_TTL_S,
)


class _MockExecutor:
    """Factory for GlowUpExecutor with mocked API and actions."""

    @staticmethod
    def make(
        actions: dict[str, Any] | None = None,
    ) -> GlowUpExecutor:
        """Create an executor with given action definitions.

        Patches _load_actions so no YAML file is needed.

        Args:
            actions: Action definitions (defaults to standard set).

        Returns:
            Configured GlowUpExecutor.
        """
        if actions is None:
            actions = {
                "power": {
                    "type": "command",
                    "label": "lights",
                    "method": "POST",
                    "path": "/api/devices/{target}/power",
                    "body": {"on": "param:on"},
                    "confirm": "{target} is {on_off}.",
                    "param_map": {
                        "on": {"true": "on", "false": "off", "key": "on_off"},
                    },
                    "speak": False,
                },
                "query_status": {
                    "type": "query",
                    "label": "lights",
                    "handler": "power_state",
                    "speak": True,
                },
                "query_sensor": {
                    "type": "query",
                    "label": "sensors",
                    "handler": "sensor_reading",
                    "speak": True,
                },
                "query_soil": {
                    "type": "query",
                    "label": "water sensor",
                    "handler": "soil_moisture",
                    "zones": {
                        "backyard": {
                            "sensors": [
                                {"name": "SBYRD", "direction": "south"},
                            ],
                        },
                        "front yard": {
                            "sensors": [
                                {"name": "SLIS", "direction": "south"},
                                {"name": "SFRNT", "direction": "center"},
                            ],
                        },
                    },
                    "threshold": 40.0,
                    "speak": True,
                },
                "chat": {
                    "type": "chat",
                    "label": "assistant",
                    "speak": True,
                },
            }

        with patch.object(GlowUpExecutor, "_load_actions", return_value=actions):
            return GlowUpExecutor(
                api_base="http://test:8420",
                auth_token="test-token",
            )


class TestGetActionLabel(unittest.TestCase):
    """Tests for get_action_label."""

    def test_known_action(self) -> None:
        """Returns the label for a known action."""
        ex = _MockExecutor.make()
        self.assertEqual(ex.get_action_label("power"), "lights")

    def test_unknown_action(self) -> None:
        """Returns empty string for an unknown action."""
        ex = _MockExecutor.make()
        self.assertEqual(ex.get_action_label("nonexistent"), "")

    def test_chat_action(self) -> None:
        """Chat action returns 'assistant' label."""
        ex = _MockExecutor.make()
        self.assertEqual(ex.get_action_label("chat"), "assistant")


class TestGetActionType(unittest.TestCase):
    """Tests for get_action_type."""

    def test_command_type(self) -> None:
        """Power is a command type."""
        ex = _MockExecutor.make()
        self.assertEqual(ex.get_action_type("power"), "command")

    def test_query_type(self) -> None:
        """Query status is a query type."""
        ex = _MockExecutor.make()
        self.assertEqual(ex.get_action_type("query_status"), "query")

    def test_chat_type(self) -> None:
        """Chat is a chat type."""
        ex = _MockExecutor.make()
        self.assertEqual(ex.get_action_type("chat"), "chat")

    def test_unknown_returns_empty(self) -> None:
        """Unknown action returns empty string."""
        ex = _MockExecutor.make()
        self.assertEqual(ex.get_action_type("bogus"), "")


class TestResolveBody(unittest.TestCase):
    """Tests for _resolve_body param:key substitution."""

    def setUp(self) -> None:
        self.ex = _MockExecutor.make()

    def test_simple_param_ref(self) -> None:
        """'param:on' resolves to the actual value."""
        result = self.ex._resolve_body(
            {"on": "param:on"}, {"on": True},
        )
        self.assertEqual(result, {"on": True})

    def test_missing_param_returns_template(self) -> None:
        """Missing param key returns the original 'param:...' string."""
        result = self.ex._resolve_body(
            {"val": "param:missing"}, {},
        )
        self.assertEqual(result, {"val": "param:missing"})

    def test_nested_dict(self) -> None:
        """Nested dicts are recursively resolved."""
        template: dict[str, Any] = {
            "effect": "solid",
            "params": {"color": "param:color"},
        }
        result = self.ex._resolve_body(template, {"color": "red"})
        self.assertEqual(result["params"]["color"], "red")
        self.assertEqual(result["effect"], "solid")

    def test_list_values(self) -> None:
        """Lists are recursively resolved."""
        template = ["param:a", "literal", "param:b"]
        result = self.ex._resolve_body(template, {"a": 1, "b": 2})
        self.assertEqual(result, [1, "literal", 2])

    def test_non_param_string_preserved(self) -> None:
        """Strings not starting with 'param:' pass through."""
        result = self.ex._resolve_body("just a string", {})
        self.assertEqual(result, "just a string")

    def test_numeric_passthrough(self) -> None:
        """Numeric values pass through unchanged."""
        result = self.ex._resolve_body(42, {})
        self.assertEqual(result, 42)

    def test_boolean_passthrough(self) -> None:
        """Boolean values pass through unchanged (and stay bool)."""
        result_true = self.ex._resolve_body(True, {})
        result_false = self.ex._resolve_body(False, {})
        self.assertIs(result_true, True)
        self.assertIs(result_false, False)

    def test_preserves_param_type(self) -> None:
        """Resolved param preserves its Python type (not stringified)."""
        result = self.ex._resolve_body(
            {"brightness": "param:brightness"}, {"brightness": 75},
        )
        self.assertIsInstance(result["brightness"], int)
        self.assertEqual(result["brightness"], 75)


class TestFormatConfirm(unittest.TestCase):
    """Tests for _format_confirm template formatting."""

    def setUp(self) -> None:
        self.ex = _MockExecutor.make()

    def test_simple_target(self) -> None:
        """Basic {target} substitution."""
        result = self.ex._format_confirm(
            "{target} stopped.", "Bedroom", {}, None,
        )
        self.assertEqual(result, "Bedroom stopped.")

    def test_param_map_bool_to_text(self) -> None:
        """Param map converts boolean True → 'on'."""
        param_map: dict[str, Any] = {
            "on": {"true": "on", "false": "off", "key": "on_off"},
        }
        result = self.ex._format_confirm(
            "{target} is {on_off}.",
            "Living Room",
            {"on": True},
            param_map,
        )
        self.assertEqual(result, "Living Room is on.")

    def test_param_map_false(self) -> None:
        """Param map converts False → 'off'."""
        param_map: dict[str, Any] = {
            "on": {"true": "on", "false": "off", "key": "on_off"},
        }
        result = self.ex._format_confirm(
            "{target} is {on_off}.",
            "Kitchen",
            {"on": False},
            param_map,
        )
        self.assertEqual(result, "Kitchen is off.")

    def test_missing_template_key(self) -> None:
        """Missing substitution key returns template as-is."""
        result = self.ex._format_confirm(
            "{target} set to {missing_key}.", "Test", {}, None,
        )
        # Should return original template (KeyError caught).
        self.assertEqual(result, "{target} set to {missing_key}.")

    def test_no_param_map(self) -> None:
        """Works without param_map (None)."""
        result = self.ex._format_confirm(
            "Playing {effect} on {target}.",
            "Bedroom",
            {"effect": "cylon"},
            None,
        )
        self.assertEqual(result, "Playing cylon on Bedroom.")


class TestDispatchCommand(unittest.TestCase):
    """Tests for _dispatch_command."""

    def test_successful_command(self) -> None:
        """Command dispatches API call and returns ok status."""
        ex = _MockExecutor.make()
        with patch.object(ex, "_request", return_value={"status": "ok"}):
            result = ex._dispatch_command(
                ex._actions["power"],
                "group%3ABedroom",
                "Bedroom",
                {"on": True},
            )
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["speak"])

    def test_command_confirmation_text(self) -> None:
        """Command produces formatted confirmation."""
        ex = _MockExecutor.make()
        with patch.object(ex, "_request", return_value={}):
            result = ex._dispatch_command(
                ex._actions["power"],
                "group%3ABedroom",
                "Bedroom",
                {"on": False},
            )
        self.assertIn("off", result["confirmation"])
        self.assertIn("Bedroom", result["confirmation"])


class TestDispatchQuery(unittest.TestCase):
    """Tests for _dispatch_query with handler lookup."""

    def test_missing_handler(self) -> None:
        """Unknown handler name returns error."""
        ex = _MockExecutor.make()
        cfg: dict[str, Any] = {"handler": "nonexistent_handler"}
        result = ex._dispatch_query(cfg, "test", "test", "test", {})
        self.assertEqual(result["status"], "error")
        self.assertIn("not found", result["confirmation"])

    def test_empty_handler(self) -> None:
        """Empty handler string returns error."""
        ex = _MockExecutor.make()
        cfg: dict[str, Any] = {"handler": ""}
        result = ex._dispatch_query(cfg, "test", "test", "test", {})
        self.assertEqual(result["status"], "error")


class TestHandleSensorReading(unittest.TestCase):
    """Tests for _handle_sensor_reading — the unguarded handler."""

    def test_api_failure_returns_sensor_specific_error(self) -> None:
        """API failure in sensor reading returns a sensor-specific message."""
        ex = _MockExecutor.make()
        import urllib.error
        with patch.object(
            ex, "_request",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = ex._handle_sensor_reading(
                {}, "", "", "Bedroom", {"sensor_type": "temperature"},
            )
        self.assertEqual(result["status"], "ok")
        self.assertIn("can't reach the sensors", result["confirmation"])

    def test_temperature_conversion(self) -> None:
        """Celsius to Fahrenheit conversion in sensor reading."""
        ex = _MockExecutor.make()
        # 20 C = 68 F.
        api_data: dict[str, Any] = {
            "Bedroom": {"temperature": 20.0, "humidity": 45.0},
        }
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_sensor_reading(
                {}, "", "", "Bedroom", {"sensor_type": "temperature"},
            )
        self.assertIn("68", result["confirmation"])

    def test_humidity_reading(self) -> None:
        """Humidity reading is formatted with percent."""
        ex = _MockExecutor.make()
        api_data: dict[str, Any] = {
            "Bedroom": {"humidity": 55.3},
        }
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_sensor_reading(
                {}, "", "", "Bedroom", {"sensor_type": "humidity"},
            )
        self.assertIn("55", result["confirmation"])
        self.assertIn("percent", result["confirmation"])

    def test_motion_detected(self) -> None:
        """Motion true → 'detected'."""
        ex = _MockExecutor.make()
        api_data: dict[str, Any] = {
            "Hallway": {"motion": True},
        }
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_sensor_reading(
                {}, "", "", "Hallway", {"sensor_type": "motion"},
            )
        self.assertIn("detected", result["confirmation"])

    def test_motion_clear(self) -> None:
        """Motion false → 'clear'."""
        ex = _MockExecutor.make()
        api_data: dict[str, Any] = {
            "Hallway": {"motion": False},
        }
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_sensor_reading(
                {}, "", "", "Hallway", {"sensor_type": "motion"},
            )
        self.assertIn("clear", result["confirmation"])

    def test_sensor_not_found(self) -> None:
        """Missing sensor returns 'don't have' message."""
        ex = _MockExecutor.make()
        api_data: dict[str, Any] = {
            "Kitchen": {"temperature": 22.0},
        }
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_sensor_reading(
                {}, "", "", "Bedroom", {"sensor_type": "temperature"},
            )
        self.assertIn("don't have", result["confirmation"])


class TestHandleSoilMoisture(unittest.TestCase):
    """Tests for _handle_soil_moisture zone-based queries."""

    def test_unknown_zone(self) -> None:
        """Zone not in config returns error."""
        ex = _MockExecutor.make()
        cfg: dict[str, Any] = ex._actions["query_soil"]
        result = ex._handle_soil_moisture(
            cfg, "", "", "rooftop garden", {},
        )
        self.assertIn("don't have soil sensors", result["confirmation"])

    def test_fuzzy_zone_match(self) -> None:
        """Partial zone name matches config key."""
        ex = _MockExecutor.make()
        cfg: dict[str, Any] = ex._actions["query_soil"]
        api_data: dict[str, Any] = {
            "sensors": [
                {"name": "SBYRD", "soil_moisture": 55.0},
            ],
        }
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_soil_moisture(
                cfg, "", "", "backyard", {},
            )
        self.assertIn("55", result["confirmation"])
        self.assertEqual(result["status"], "ok")

    def test_watering_suggested(self) -> None:
        """Below threshold suggests watering."""
        ex = _MockExecutor.make()
        cfg: dict[str, Any] = ex._actions["query_soil"]
        api_data: dict[str, Any] = {
            "sensors": [
                {"name": "SBYRD", "soil_moisture": 20.0},
            ],
        }
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_soil_moisture(
                cfg, "", "", "backyard", {},
            )
        self.assertIn("suggested", result["confirmation"])

    def test_watering_not_suggested(self) -> None:
        """Above threshold does not suggest watering."""
        ex = _MockExecutor.make()
        cfg: dict[str, Any] = ex._actions["query_soil"]
        api_data: dict[str, Any] = {
            "sensors": [
                {"name": "SBYRD", "soil_moisture": 65.0},
            ],
        }
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_soil_moisture(
                cfg, "", "", "backyard", {},
            )
        self.assertIn("not suggested", result["confirmation"])

    def test_no_readings_available(self) -> None:
        """Sensors in config but no data returns graceful message."""
        ex = _MockExecutor.make()
        cfg: dict[str, Any] = ex._actions["query_soil"]
        api_data: dict[str, Any] = {"sensors": []}
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_soil_moisture(
                cfg, "", "", "backyard", {},
            )
        self.assertIn("No soil readings", result["confirmation"])

    def test_missing_name_key_in_sensor(self) -> None:
        """Sensor dict missing 'name' key is filtered out gracefully."""
        ex = _MockExecutor.make()
        cfg: dict[str, Any] = ex._actions["query_soil"]
        api_data: dict[str, Any] = {
            "sensors": [
                {"soil_moisture": 55.0},  # Missing 'name' key — filtered.
            ],
        }
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_soil_moisture(
                cfg, "", "", "backyard", {},
            )
        # No named sensors matched, so no readings available.
        self.assertEqual(result["status"], "ok")
        self.assertIn("No soil readings", result["confirmation"])

    def test_api_failure(self) -> None:
        """API error returns graceful message."""
        ex = _MockExecutor.make()
        cfg: dict[str, Any] = ex._actions["query_soil"]
        with patch.object(ex, "_request", side_effect=Exception("timeout")):
            result = ex._handle_soil_moisture(
                cfg, "", "", "backyard", {},
            )
        self.assertEqual(result["status"], "error")
        self.assertIn("couldn't check", result["confirmation"])


class TestChatHistory(unittest.TestCase):
    """Tests for per-room chat history management."""

    def test_fresh_room_empty(self) -> None:
        """New room starts with empty history."""
        ex = _MockExecutor.make()
        history = ex._get_chat_history("bedroom")
        self.assertEqual(history, [])

    def test_history_persists_across_calls(self) -> None:
        """Same room returns the same list object (mutable)."""
        ex = _MockExecutor.make()
        h1 = ex._get_chat_history("bedroom")
        h1.append({"role": "user", "content": "hello"})
        h2 = ex._get_chat_history("bedroom")
        self.assertEqual(len(h2), 1)
        self.assertEqual(h2[0]["content"], "hello")

    def test_rooms_isolated(self) -> None:
        """Different rooms have independent histories."""
        ex = _MockExecutor.make()
        ex._get_chat_history("bedroom").append(
            {"role": "user", "content": "bedroom msg"},
        )
        kitchen = ex._get_chat_history("kitchen")
        self.assertEqual(kitchen, [])

    def test_ttl_expiry(self) -> None:
        """History expires after TTL elapses."""
        ex = _MockExecutor.make()
        h = ex._get_chat_history("bedroom")
        h.append({"role": "user", "content": "old message"})
        # Simulate time passing beyond TTL.
        ex._chat_timestamps["bedroom"] = (
            time.time() - _CHAT_HISTORY_TTL_S - 1
        )
        h2 = ex._get_chat_history("bedroom")
        self.assertEqual(h2, [])

    def test_ttl_not_expired(self) -> None:
        """History survives within TTL window."""
        ex = _MockExecutor.make()
        h = ex._get_chat_history("bedroom")
        h.append({"role": "user", "content": "recent"})
        # Just called, so timestamp is now — should not expire.
        h2 = ex._get_chat_history("bedroom")
        self.assertEqual(len(h2), 1)


class TestExecuteDispatch(unittest.TestCase):
    """Tests for the top-level execute() dispatch logic."""

    def test_unknown_action_falls_back_to_chat(self) -> None:
        """Unknown action routes to chat if chat config exists."""
        ex = _MockExecutor.make()
        intent: dict[str, Any] = {
            "action": "gibberish",
            "target": "what is the weather",
            "params": {},
        }
        with patch.object(
            ex, "_exec_chat",
            return_value={"status": "ok", "confirmation": "test", "speak": True},
        ) as mock_chat:
            with patch.object(ex, "_resolve_target", return_value="all"):
                result = ex.execute(intent, "bedroom")
        mock_chat.assert_called_once()
        self.assertEqual(result["status"], "ok")

    def test_totally_unknown_no_chat(self) -> None:
        """Unknown action with no chat config returns error."""
        ex = _MockExecutor.make(actions={"power": {"type": "command"}})
        intent: dict[str, Any] = {
            "action": "gibberish",
            "target": "all",
            "params": {},
        }
        with patch.object(ex, "_resolve_target", return_value="all"):
            result = ex.execute(intent, "bedroom")
        self.assertEqual(result["status"], "error")
        self.assertIn("don't know how to", result["confirmation"])

    def test_display_target_strips_group_prefix(self) -> None:
        """group:Bedroom → display as 'Bedroom'."""
        ex = _MockExecutor.make()
        with patch.object(ex, "_resolve_target", return_value="group:Bedroom"):
            with patch.object(ex, "_request", return_value={}):
                result = ex.execute(
                    {"action": "power", "target": "Bedroom", "params": {"on": True}},
                    "room",
                )
        self.assertIn("Bedroom", result["confirmation"])
        self.assertNotIn("group:", result["confirmation"])

    def test_handler_exception_returns_generic_error(self) -> None:
        """A handler raising an unexpected exception must not crash the
        worker — the executor's outer try/except (executor.py around
        line 817) is the last line of defense for the voice path.
        Without it, a single malformed upstream API response would
        deadlock the satellite waiting on TTS that never arrives.
        """
        ex = _MockExecutor.make()
        with patch.object(ex, "_resolve_target", return_value="all"):
            with patch.object(
                ex, "_dispatch_query",
                side_effect=RuntimeError("simulated handler crash"),
            ):
                result = ex.execute(
                    {
                        "action": "query_status",
                        "target": "all",
                        "params": {},
                    },
                    "room",
                )
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["speak"])
        # The exact text is the user-facing apology — pinned so a
        # well-meaning rewording doesn't make the response empty.
        self.assertIn("went wrong", result["confirmation"].lower())

    def test_command_dispatch_exception_returns_generic_error(self) -> None:
        """Same contract for the command path — a network blip or a
        handler crash inside ``_dispatch_command`` must not bubble out.
        """
        ex = _MockExecutor.make()
        with patch.object(ex, "_resolve_target", return_value="all"):
            with patch.object(
                ex, "_dispatch_command",
                side_effect=ConnectionError("simulated network blip"),
            ):
                result = ex.execute(
                    {
                        "action": "power",
                        "target": "all",
                        "params": {"on": True},
                    },
                    "room",
                )
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["speak"])
        self.assertIn("went wrong", result["confirmation"].lower())


class TestExecChat(unittest.TestCase):
    """Tests for _exec_chat Ollama integration."""

    def test_empty_message(self) -> None:
        """Empty message returns 'didn't catch that'."""
        ex = _MockExecutor.make()
        result = ex._exec_chat("", "bedroom")
        self.assertEqual(result["status"], "error")
        self.assertIn("didn't catch", result["confirmation"])

    def test_whitespace_message(self) -> None:
        """Whitespace-only message returns error."""
        ex = _MockExecutor.make()
        result = ex._exec_chat("   ", "bedroom")
        self.assertEqual(result["status"], "error")

    def test_history_trimming(self) -> None:
        """History beyond max is trimmed in pairs."""
        ex = _MockExecutor.make()
        # Stuff history beyond max.
        h = ex._get_chat_history("bedroom")
        for i in range(_CHAT_HISTORY_MAX + 5):
            h.append({"role": "user", "content": f"msg {i}"})
            h.append({"role": "assistant", "content": f"reply {i}"})

        # Mock Ollama response.
        mock_response: bytes = json.dumps({
            "message": {"content": "test reply"},
        }).encode()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            ex._exec_chat("new message", "bedroom")

        # History should be trimmed to max * 2 entries (pairs).
        self.assertLessEqual(len(h), _CHAT_HISTORY_MAX * 2)


class TestHandlePowerState(unittest.TestCase):
    """Tests for _handle_power_state query handler."""

    def test_all_on(self) -> None:
        """All devices on → 'is on'."""
        ex = _MockExecutor.make()
        api_data: dict[str, Any] = {
            "devices": [
                {"label": "Bulb 1", "group": "Bedroom", "power": True},
                {"label": "Bulb 2", "group": "Bedroom", "power": True},
            ],
        }
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_power_state(
                {}, "", "group:Bedroom", "Bedroom", {},
            )
        self.assertIn("on", result["confirmation"].lower())

    def test_all_off(self) -> None:
        """All devices off → 'is off'."""
        ex = _MockExecutor.make()
        api_data: dict[str, Any] = {
            "devices": [
                {"label": "Bulb 1", "group": "Bedroom", "power": False},
            ],
        }
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_power_state(
                {}, "", "group:Bedroom", "Bedroom", {},
            )
        self.assertIn("off", result["confirmation"].lower())

    def test_partially_on(self) -> None:
        """Mixed power states → 'partially on'."""
        ex = _MockExecutor.make()
        api_data: dict[str, Any] = {
            "devices": [
                {"label": "Bulb 1", "group": "Bedroom", "power": True},
                {"label": "Bulb 2", "group": "Bedroom", "power": False},
            ],
        }
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_power_state(
                {}, "", "group:Bedroom", "Bedroom", {},
            )
        self.assertIn("partially", result["confirmation"].lower())

    def test_no_matching_devices_falls_back_to_effect(self) -> None:
        """No devices found falls back to effect status."""
        ex = _MockExecutor.make()
        devices_data: dict[str, Any] = {"devices": []}
        effect_data: dict[str, Any] = {"effect": "cylon", "elapsed": 30}
        with patch.object(
            ex, "_request", side_effect=[devices_data, effect_data],
        ):
            result = ex._handle_power_state(
                {}, "test", "test", "test", {},
            )
        # Should have fallen through to _handle_effect_status.
        self.assertEqual(result["status"], "ok")


class TestHandlePowerSummary(unittest.TestCase):
    """Tests for _handle_power_summary."""

    def test_cost_calculation(self) -> None:
        """Electricity cost uses the hardcoded rate."""
        ex = _MockExecutor.make()
        api_data: dict[str, Any] = {
            "avg_watts": 100,
            "peak_watts": 200,
            "total_kwh": 10.0,
        }
        with patch.object(ex, "_request", return_value=api_data):
            result = ex._handle_power_summary(
                {}, "", "", "Server", {},
            )
        # 10 kWh * $0.171 = $1.71.
        self.assertIn("$1.71", result["confirmation"])

    def test_fallback_to_global_summary(self) -> None:
        """Per-device query failure falls back to global summary."""
        ex = _MockExecutor.make()
        global_data: dict[str, Any] = {
            "avg_watts": 50,
            "peak_watts": 100,
            "total_kwh": 5.0,
        }
        with patch.object(
            ex, "_request",
            side_effect=[Exception("not found"), global_data],
        ):
            result = ex._handle_power_summary(
                {}, "test", "", "test", {},
            )
        self.assertEqual(result["status"], "ok")
        self.assertIn("50", result["confirmation"])


class TestHandleAlarmStatus(unittest.TestCase):
    """Tests for _handle_alarm_status.

    Pins the contract that this handler reads the ``alarm`` key from
    the hub's security-status response — not ``alarm_state``.
    Caught 2026-05-02 while debugging a stale Vivint cache: the
    handler had been silently returning "in an unknown state" for
    every query because the field name didn't match the API.
    """

    def _make(self) -> Any:
        return _MockExecutor.make(actions={
            "query_alarm": {
                "type": "query",
                "label": "alarm",
                "handler": "alarm_status",
                "speak": True,
            },
        })

    def test_armed_stay_spoken(self) -> None:
        ex = self._make()
        with patch.object(
            ex, "_request",
            return_value={"alarm": "armed_stay", "doors": [], "sensors": {}},
        ):
            result = ex._handle_alarm_status({}, "", "all", "all", {})
        self.assertEqual(result["status"], "ok")
        self.assertIn("armed in stay mode", result["confirmation"])

    def test_armed_away_spoken(self) -> None:
        ex = self._make()
        with patch.object(
            ex, "_request",
            return_value={"alarm": "armed_away"},
        ):
            result = ex._handle_alarm_status({}, "", "all", "all", {})
        self.assertIn("armed in away mode", result["confirmation"])

    def test_disarmed_spoken(self) -> None:
        ex = self._make()
        with patch.object(
            ex, "_request",
            return_value={"alarm": "disarmed"},
        ):
            result = ex._handle_alarm_status({}, "", "all", "all", {})
        self.assertIn("disarmed", result["confirmation"])

    def test_missing_field_falls_back_to_unknown(self) -> None:
        """Hub returns no ``alarm`` field (e.g. adapter not yet ready)
        → spoken "in an unknown state", not crash.
        """
        ex = self._make()
        with patch.object(ex, "_request", return_value={}):
            result = ex._handle_alarm_status({}, "", "all", "all", {})
        self.assertIn("unknown", result["confirmation"])

    def test_api_unreachable_returns_friendly_error(self) -> None:
        ex = self._make()
        with patch.object(
            ex, "_request",
            side_effect=ConnectionError("hub down"),
        ):
            result = ex._handle_alarm_status({}, "", "all", "all", {})
        self.assertEqual(result["status"], "error")
        self.assertIn("security system", result["confirmation"].lower())


class TestVoiceGateHandlers(unittest.TestCase):
    """Tests for enable_voice_gate / disable_voice_gate handlers."""

    def _make_gated_executor(self) -> "GlowUpExecutor":
        """Build an executor with a mock MQTT client attached."""
        ex = _MockExecutor.make({
            "enable_voice_gate": {
                "type": "query", "label": "voice gate",
                "handler": "enable_voice_gate", "speak": True,
            },
            "disable_voice_gate": {
                "type": "query", "label": "voice gate",
                "handler": "disable_voice_gate", "speak": True,
            },
        })
        ex.set_mqtt_client(MagicMock())
        return ex

    def test_parse_duration_seconds_int(self) -> None:
        self.assertEqual(
            GlowUpExecutor._parse_duration_seconds(
                {"duration_seconds": 3600},
            ), 3600,
        )

    def test_parse_duration_words_hours(self) -> None:
        self.assertEqual(
            GlowUpExecutor._parse_duration_seconds(
                {"duration": "two hours"},
            ), 7200,
        )

    def test_parse_duration_words_minutes(self) -> None:
        self.assertEqual(
            GlowUpExecutor._parse_duration_seconds(
                {"duration": "thirty minutes"},
            ), 1800,
        )

    def test_parse_duration_compact(self) -> None:
        self.assertEqual(
            GlowUpExecutor._parse_duration_seconds({"duration": "2h"}),
            7200,
        )

    def test_parse_duration_missing(self) -> None:
        self.assertEqual(
            GlowUpExecutor._parse_duration_seconds({}), 0,
        )

    def test_parse_duration_malformed(self) -> None:
        self.assertEqual(
            GlowUpExecutor._parse_duration_seconds(
                {"duration": "forever"},
            ), 0,
        )

    def test_enable_rejects_untrusted_room(self) -> None:
        ex = self._make_gated_executor()
        ex._current_room = "Doorbell"  # gated exterior room
        result = ex._handle_enable_voice_gate(
            {}, "doorbell", "doorbell", "doorbell",
            {"duration_seconds": 600},
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("can't enable", result["confirmation"].lower())
        ex._mqtt_client.publish.assert_not_called()

    def test_enable_rejects_missing_duration(self) -> None:
        ex = self._make_gated_executor()
        ex._current_room = "Main Bedroom"
        result = ex._handle_enable_voice_gate(
            {}, "doorbell", "doorbell", "doorbell", {},
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("how long", result["confirmation"].lower())
        ex._mqtt_client.publish.assert_not_called()

    def test_enable_rejects_zero_duration(self) -> None:
        ex = self._make_gated_executor()
        ex._current_room = "Dining Room"
        result = ex._handle_enable_voice_gate(
            {}, "doorbell", "doorbell", "doorbell",
            {"duration_seconds": 0},
        )
        self.assertEqual(result["status"], "error")
        ex._mqtt_client.publish.assert_not_called()

    def test_enable_publishes_retained_on_success(self) -> None:
        ex = self._make_gated_executor()
        ex._current_room = "Main Bedroom"
        before: float = time.time()
        result = ex._handle_enable_voice_gate(
            {}, "doorbell", "doorbell", "doorbell",
            {"duration_seconds": 600},
        )
        self.assertEqual(result["status"], "ok")
        ex._mqtt_client.publish.assert_called_once()
        args, kwargs = ex._mqtt_client.publish.call_args
        self.assertEqual(args[0], "glowup/voice/gate/doorbell")
        payload: dict[str, Any] = json.loads(args[1])
        self.assertTrue(payload["enabled"])
        self.assertGreaterEqual(payload["expires_at"], before + 599)
        self.assertTrue(kwargs.get("retain"))
        self.assertEqual(kwargs.get("qos"), 1)

    def test_enable_clamps_to_two_hours(self) -> None:
        ex = self._make_gated_executor()
        ex._current_room = "Main Bedroom"
        before: float = time.time()
        result = ex._handle_enable_voice_gate(
            {}, "doorbell", "doorbell", "doorbell",
            {"duration_seconds": 99999},
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("two hours", result["confirmation"].lower())
        args, _ = ex._mqtt_client.publish.call_args
        payload = json.loads(args[1])
        # expires_at is at most 7200s from now (allow slack for clock).
        self.assertLessEqual(payload["expires_at"], before + 7200 + 1)
        self.assertGreaterEqual(payload["expires_at"], before + 7200 - 5)

    def test_enable_porch_alias_resolves_to_doorbell_slug(self) -> None:
        ex = self._make_gated_executor()
        ex._current_room = "Main Bedroom"
        ex._handle_enable_voice_gate(
            {}, "porch", "porch", "porch",
            {"duration_seconds": 600},
        )
        args, _ = ex._mqtt_client.publish.call_args
        self.assertEqual(args[0], "glowup/voice/gate/doorbell")

    def test_disable_ignores_allowlist(self) -> None:
        """Closing a gate is always safe — no room check."""
        ex = self._make_gated_executor()
        ex._current_room = "Doorbell"  # untrusted room
        result = ex._handle_disable_voice_gate(
            {}, "doorbell", "doorbell", "doorbell", {},
        )
        self.assertEqual(result["status"], "ok")
        args, kwargs = ex._mqtt_client.publish.call_args
        self.assertEqual(args[0], "glowup/voice/gate/doorbell")
        payload = json.loads(args[1])
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["expires_at"], 0)
        self.assertTrue(kwargs.get("retain"))

    def test_enable_without_mqtt_client_errors(self) -> None:
        ex = _MockExecutor.make({
            "enable_voice_gate": {
                "type": "query", "label": "voice gate",
                "handler": "enable_voice_gate", "speak": True,
            },
        })
        ex._current_room = "Main Bedroom"
        result = ex._handle_enable_voice_gate(
            {}, "doorbell", "doorbell", "doorbell",
            {"duration_seconds": 600},
        )
        self.assertEqual(result["status"], "error")


class TestWeatherFormatting(unittest.TestCase):
    """Tests for _format_weather aspect routing and feels-like spread."""

    def setUp(self) -> None:
        self.ex = _MockExecutor.make()

    def _cc(self, **kw: Any) -> Any:
        from voice.coordinator.weather_sources import CurrentConditions
        defaults: dict[str, Any] = dict(
            temp_f=78.0, apparent_f=78.0, humidity_pct=60.0,
            wind_mph=5.0, condition="clear sky", source="NWS",
        )
        defaults.update(kw)
        return CurrentConditions(**defaults)

    def test_aspect_temperature_only(self) -> None:
        msg = self.ex._format_weather(self._cc(), "temperature")
        self.assertEqual(msg, "It is 78 degrees outside.")

    def test_aspect_humidity_only(self) -> None:
        msg = self.ex._format_weather(self._cc(), "humidity")
        self.assertEqual(msg, "Outdoor humidity is 60 percent.")

    def test_aspect_wind_only(self) -> None:
        msg = self.ex._format_weather(self._cc(), "wind")
        self.assertEqual(msg, "Wind is 5 miles per hour.")

    def test_aspect_condition_only(self) -> None:
        msg = self.ex._format_weather(self._cc(), "condition")
        self.assertEqual(msg, "It is currently clear sky.")

    def test_aspect_feels_like(self) -> None:
        msg = self.ex._format_weather(
            self._cc(temp_f=88.0, apparent_f=98.0), "feels_like",
        )
        self.assertEqual(msg, "It feels like 98 degrees.")

    def test_default_includes_feels_like_when_spread_large(self) -> None:
        msg = self.ex._format_weather(
            self._cc(temp_f=88.0, apparent_f=98.0), "all",
        )
        self.assertIn("feels like 98", msg)
        self.assertIn("88 degrees", msg)

    def test_default_omits_feels_like_when_spread_small(self) -> None:
        msg = self.ex._format_weather(
            self._cc(temp_f=70.0, apparent_f=71.0), "all",
        )
        self.assertNotIn("feels like", msg)

    def test_default_handles_missing_temperature(self) -> None:
        msg = self.ex._format_weather(
            self._cc(temp_f=None, apparent_f=None), "all",
        )
        self.assertIn("clear sky", msg)
        self.assertNotIn("None", msg)

    def test_temperature_aspect_missing_value(self) -> None:
        msg = self.ex._format_weather(self._cc(temp_f=None), "temperature")
        self.assertIn("don't have", msg)


class TestForecastFormatting(unittest.TestCase):
    """Tests for period selection and forecast formatting."""

    def setUp(self) -> None:
        self.ex = _MockExecutor.make()
        from voice.coordinator.weather_sources import ForecastPeriod
        self.FP = ForecastPeriod
        self.periods = [
            ForecastPeriod(
                name="Today", is_daytime=True, temperature_f=85.0,
                condition="Sunny", precip_probability_pct=10.0,
                wind_mph_desc="5 mph",
            ),
            ForecastPeriod(
                name="Tonight", is_daytime=False, temperature_f=68.0,
                condition="Clear", precip_probability_pct=0.0,
                wind_mph_desc="calm",
            ),
            ForecastPeriod(
                name="Tomorrow", is_daytime=True, temperature_f=88.0,
                condition="Partly Sunny", precip_probability_pct=60.0,
                wind_mph_desc="10 mph",
            ),
        ]

    def test_selects_today(self) -> None:
        p = self.ex._select_period(self.periods, "today")
        self.assertEqual(p.name, "Today")

    def test_selects_tonight(self) -> None:
        p = self.ex._select_period(self.periods, "tonight")
        self.assertEqual(p.name, "Tonight")

    def test_selects_tomorrow(self) -> None:
        p = self.ex._select_period(self.periods, "tomorrow")
        self.assertEqual(p.name, "Tomorrow")

    def test_tomorrow_falls_back_to_next_daytime(self) -> None:
        """When the list lacks a "Tomorrow" label (NWS uses weekday names),
        the next future daytime period is returned."""
        alt = [
            self.FP(name="This Afternoon", is_daytime=True, temperature_f=80.0,
                    condition="Sunny", precip_probability_pct=0.0,
                    wind_mph_desc=""),
            self.FP(name="Tonight", is_daytime=False, temperature_f=60.0,
                    condition="Clear", precip_probability_pct=0.0,
                    wind_mph_desc=""),
            self.FP(name="Thursday", is_daytime=True, temperature_f=82.0,
                    condition="Sunny", precip_probability_pct=0.0,
                    wind_mph_desc=""),
        ]
        p = self.ex._select_period(alt, "tomorrow")
        self.assertEqual(p.name, "Thursday")

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(self.ex._select_period([], "today"))

    def test_format_default_all(self) -> None:
        msg = self.ex._format_forecast(self.periods[2], "all")
        self.assertIn("Tomorrow", msg)
        self.assertIn("Partly Sunny", msg)
        self.assertIn("high of 88", msg)
        self.assertIn("60 percent chance of rain", msg)

    def test_format_rain_aspect(self) -> None:
        msg = self.ex._format_forecast(self.periods[2], "rain")
        self.assertIn("60 percent", msg)

    def test_format_high_aspect(self) -> None:
        msg = self.ex._format_forecast(self.periods[0], "high")
        self.assertIn("high is 85", msg)

    def test_format_low_aspect(self) -> None:
        msg = self.ex._format_forecast(self.periods[1], "low")
        self.assertIn("low is 68", msg)

    def test_format_low_night_daytime_mismatch(self) -> None:
        """Asking for the low on a daytime period is reported as unavailable."""
        msg = self.ex._format_forecast(self.periods[0], "low")
        self.assertIn("no overnight low", msg)


class TestAirQualityFormatting(unittest.TestCase):
    """Tests for air-quality aspect routing and pollen summaries."""

    def setUp(self) -> None:
        self.ex = _MockExecutor.make()

    def _aq(self, **kw: Any) -> Any:
        from voice.coordinator.weather_sources import AirQuality
        defaults: dict[str, Any] = dict(
            pm2_5=12.0, pm10=25.0, ozone=50.0, us_aqi=42.0,
            uv_index=6.0, pollen={}, source="Open-Meteo",
        )
        defaults.update(kw)
        return AirQuality(**defaults)

    def test_aspect_aqi_good(self) -> None:
        msg = self.ex._format_air_quality(self._aq(us_aqi=42.0), "aqi", "")
        self.assertIn("good", msg)

    def test_aspect_aqi_unhealthy(self) -> None:
        msg = self.ex._format_air_quality(
            self._aq(us_aqi=175.0), "aqi", "",
        )
        self.assertIn("unhealthy", msg)

    def test_aspect_uv_band(self) -> None:
        msg = self.ex._format_air_quality(
            self._aq(uv_index=9.0), "uv", "",
        )
        self.assertIn("very high", msg)

    def test_pollen_specific_species(self) -> None:
        msg = self.ex._format_air_quality(
            self._aq(pollen={"ragweed_pollen": 150.0}),
            "pollen", "ragweed",
        )
        self.assertIn("Ragweed pollen is high", msg)

    def test_pollen_unknown_species(self) -> None:
        msg = self.ex._format_air_quality(
            self._aq(pollen={"grass_pollen": 5.0}),
            "pollen", "ragweed",
        )
        self.assertIn("don't have", msg)

    def test_pollen_all_low_summary(self) -> None:
        msg = self.ex._format_air_quality(
            self._aq(pollen={
                "grass_pollen": 2.0, "ragweed_pollen": 3.0,
            }),
            "pollen", "",
        )
        self.assertIn("low", msg)

    def test_pollen_surfaces_worst_category(self) -> None:
        msg = self.ex._format_air_quality(
            self._aq(pollen={
                "grass_pollen": 2.0,       # low
                "ragweed_pollen": 210.0,   # very high
                "birch_pollen": 90.0,      # high
            }),
            "pollen", "",
        )
        self.assertIn("very high", msg)
        self.assertIn("ragweed", msg)

    def test_default_all_includes_aqi_and_pollen(self) -> None:
        msg = self.ex._format_air_quality(
            self._aq(pollen={"grass_pollen": 90.0}),
            "all", "",
        )
        self.assertIn("good", msg)       # AQI 42 → good
        self.assertIn("high", msg)       # Grass at 90 → high


class TestInterimSpeaker(unittest.TestCase):
    """Executor interim-speaker plumbing for weather retry notices."""

    def test_set_and_emit(self) -> None:
        ex = _MockExecutor.make()
        cb = MagicMock()
        ex.set_interim_speaker(cb)
        ex._current_room = "Main Bedroom"
        ex._speak_interim("Retrying with backup weather service.")
        cb.assert_called_once_with(
            "Main Bedroom", "Retrying with backup weather service.",
        )

    def test_no_room_noop(self) -> None:
        ex = _MockExecutor.make()
        cb = MagicMock()
        ex.set_interim_speaker(cb)
        ex._current_room = ""
        ex._speak_interim("anything")
        cb.assert_not_called()

    def test_no_callback_noop(self) -> None:
        ex = _MockExecutor.make()
        ex.set_interim_speaker(None)
        ex._current_room = "Main Bedroom"
        ex._speak_interim("anything")  # Must not raise.

    def test_callback_exception_swallowed(self) -> None:
        ex = _MockExecutor.make()
        ex.set_interim_speaker(MagicMock(side_effect=RuntimeError("boom")))
        ex._current_room = "Main Bedroom"
        ex._speak_interim("anything")  # Must not raise.


class TestPlayAssetResolve(unittest.TestCase):
    """Asset resolution — direct param, trigger map, sanitization."""

    def test_explicit_asset_param_passes_through(self) -> None:
        from voice.coordinator.executor import GlowUpExecutor as _E
        self.assertEqual(
            _E._resolve_play_asset({"asset": "daisy_bell"}),
            "daisy_bell",
        )

    def test_explicit_asset_with_path_traversal_rejected(self) -> None:
        from voice.coordinator.executor import GlowUpExecutor as _E
        self.assertIsNone(_E._resolve_play_asset({"asset": "../etc/passwd"}))
        self.assertIsNone(_E._resolve_play_asset({"asset": "a/b"}))
        self.assertIsNone(_E._resolve_play_asset({"asset": ".hidden"}))
        self.assertIsNone(_E._resolve_play_asset({"asset": ""}))
        self.assertIsNone(_E._resolve_play_asset({"asset": "x" * 65}))

    def test_trigger_map_matches_daisy_phrases(self) -> None:
        from voice.coordinator.executor import GlowUpExecutor as _E
        for phrase in (
            "sing daisy",
            "sing me daisy bell",
            "please sing daisy from 2001",
            "sing the song from 2001 a space odyssey",
            "Daisy Daisy give me your answer do",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    _E._resolve_play_asset({"message": phrase}),
                    "daisy_bell",
                )

    def test_no_match_returns_none(self) -> None:
        from voice.coordinator.executor import GlowUpExecutor as _E
        self.assertIsNone(_E._resolve_play_asset({"message": "sing happy birthday"}))
        self.assertIsNone(_E._resolve_play_asset({}))


class TestHandlePlayAsset(unittest.TestCase):
    """End-to-end handler: publish on success, fall through on misses."""

    def _make(self) -> GlowUpExecutor:
        ex = _MockExecutor.make({
            "play_asset": {
                "type": "query",
                "label": "easter egg",
                "handler": "play_asset",
                "speak": True,
            },
        })
        ex._current_room = "Main Bedroom"
        ex._mqtt_client = MagicMock()
        return ex

    def test_published_on_match(self) -> None:
        ex = self._make()
        result = ex._handle_play_asset(
            cfg={}, target_url="all", target_raw="all",
            display_target="all",
            params={"message": "sing daisy"},
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["confirmation"], "")
        self.assertTrue(result["speak"])  # speak=True + empty = silent
        ex._mqtt_client.publish.assert_called_once()
        topic, payload, *_ = ex._mqtt_client.publish.call_args.args
        self.assertEqual(topic, "glowup/voice/play_asset")
        body = json.loads(payload)
        self.assertEqual(body["asset"], "daisy_bell")
        self.assertEqual(body["room"], "Main Bedroom")

    def test_no_match_returns_friendly_error(self) -> None:
        ex = self._make()
        result = ex._handle_play_asset(
            cfg={}, target_url="all", target_raw="all",
            display_target="all",
            params={"message": "sing despacito"},
        )
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["speak"])
        ex._mqtt_client.publish.assert_not_called()

    def test_no_mqtt_client_returns_friendly_error(self) -> None:
        ex = self._make()
        ex._mqtt_client = None
        result = ex._handle_play_asset(
            cfg={}, target_url="all", target_raw="all",
            display_target="all",
            params={"message": "sing daisy"},
        )
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["speak"])

    def test_publish_exception_returns_friendly_error(self) -> None:
        ex = self._make()
        ex._mqtt_client.publish.side_effect = RuntimeError("broker gone")
        result = ex._handle_play_asset(
            cfg={}, target_url="all", target_raw="all",
            display_target="all",
            params={"message": "sing daisy"},
        )
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["speak"])


if __name__ == "__main__":
    unittest.main()
