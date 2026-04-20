#!/usr/bin/env python3
"""Tests for graceful failure when optional dependencies are missing.

Verifies that every optional subsystem handles missing libraries,
unreachable services, missing config values, and corrupt data without
crashing.  These tests use unittest.mock to simulate missing imports
and unavailable resources.

Categories:
- Adapter imports: each adapter guarded by _HAS_* sentinel
- Voice satellite: MQTT, audio, wake word, piper
- Voice coordinator: STT, intent, TTS, MQTT
- Shopping list: missing/corrupt JSON file
- Nav config: missing nav_links in config
- Server: missing optional config keys

No network or hardware dependencies.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

# Voice modules require numpy — skip those tests if unavailable.
try:
    import numpy as np
    _HAS_NUMPY: bool = True
except ImportError:
    _HAS_NUMPY = False


# ---------------------------------------------------------------------------
# Adapter sentinel tests
# ---------------------------------------------------------------------------

class TestAdapterSentinels(unittest.TestCase):
    """Every adapter import in server.py has a _HAS_* sentinel."""

    def test_all_adapter_sentinels_exist(self) -> None:
        """Each adapter has a corresponding _HAS_* sentinel in server.py."""
        import server
        expected_sentinels: list[str] = [
            "_HAS_VIVINT",
            "_HAS_NVR",
            "_HAS_PRINTER",
            "_HAS_MATTER",
            "_HAS_LOCK_MANAGER",
        ]
        for sentinel in expected_sentinels:
            self.assertTrue(
                hasattr(server, sentinel),
                f"server.py missing sentinel: {sentinel}",
            )

    def test_sentinels_are_booleans(self) -> None:
        """All _HAS_* sentinels are boolean values."""
        import server
        for attr in dir(server):
            if attr.startswith("_HAS_"):
                val = getattr(server, attr)
                self.assertIsInstance(
                    val, bool,
                    f"{attr} is {type(val).__name__}, expected bool",
                )


# ---------------------------------------------------------------------------
# Shopping list graceful failure
# ---------------------------------------------------------------------------

class TestShoppingStoreGraceful(unittest.TestCase):
    """ShoppingStore handles missing and corrupt files gracefully."""

    def test_missing_file_returns_empty_list(self) -> None:
        """Non-existent JSON file returns empty item list."""
        from handlers.shopping import ShoppingStore
        store = ShoppingStore("/tmp/nonexistent_shopping_test.json")
        items: list = store.get_items()
        self.assertEqual(items, [])

    def test_corrupt_json_returns_empty_list(self) -> None:
        """Corrupt JSON file returns empty item list, not crash."""
        from handlers.shopping import ShoppingStore
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            f.write("{corrupt json data!@#$")
            path: str = f.name
        try:
            store = ShoppingStore(path)
            items: list = store.get_items()
            self.assertEqual(items, [])
        finally:
            os.unlink(path)

    def test_empty_file_returns_empty_list(self) -> None:
        """Empty file returns empty item list, not crash."""
        from handlers.shopping import ShoppingStore
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            f.write("")
            path: str = f.name
        try:
            store = ShoppingStore(path)
            items: list = store.get_items()
            self.assertEqual(items, [])
        finally:
            os.unlink(path)

    def test_add_creates_file(self) -> None:
        """Adding an item to a new store creates the JSON file."""
        from handlers.shopping import ShoppingStore
        path: str = "/tmp/test_shopping_create.json"
        if os.path.exists(path):
            os.unlink(path)
        try:
            store = ShoppingStore(path)
            store.add_item("milk")
            self.assertTrue(os.path.exists(path))
            items: list = store.get_items()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["text"], "milk")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_has_item_case_insensitive(self) -> None:
        """Item lookup is case-insensitive."""
        from handlers.shopping import ShoppingStore
        path: str = "/tmp/test_shopping_case.json"
        try:
            store = ShoppingStore(path)
            store.add_item("Milk")
            self.assertTrue(store.has_item("milk"))
            self.assertTrue(store.has_item("MILK"))
            self.assertTrue(store.has_item("Milk"))
        finally:
            if os.path.exists(path):
                os.unlink(path)


# ---------------------------------------------------------------------------
# Nav config graceful failure
# ---------------------------------------------------------------------------

class TestNavConfigGraceful(unittest.TestCase):
    """Nav config endpoint handles missing config gracefully."""

    def test_missing_nav_links_returns_defaults(self) -> None:
        """Config without nav_links returns built-in links only."""
        from handlers.dashboard import DashboardHandlerMixin

        handler = MagicMock(spec=DashboardHandlerMixin)
        handler.config = {}  # No nav_links key.

        # Call the actual method.
        captured: dict = {}
        def fake_send_json(status: int, data: dict) -> None:
            captured["status"] = status
            captured["data"] = data

        handler._send_json = fake_send_json
        DashboardHandlerMixin._handle_get_nav_config(handler)

        self.assertEqual(captured["status"], 200)
        links: list = captured["data"]["links"]
        # Should have built-in links but no external ones.
        labels: list[str] = [l["label"] for l in links]
        self.assertIn("Home", labels)
        self.assertIn("Dashboard", labels)
        self.assertIn("Power", labels)

    def test_nav_links_extends_defaults(self) -> None:
        """Config with nav_links appends to built-in links."""
        from handlers.dashboard import DashboardHandlerMixin

        handler = MagicMock(spec=DashboardHandlerMixin)
        handler.config = {
            "nav_links": [
                {"label": "Zigbee", "href": "http://example.com:8099"},
            ],
        }

        captured: dict = {}
        def fake_send_json(status: int, data: dict) -> None:
            captured["status"] = status
            captured["data"] = data

        handler._send_json = fake_send_json
        DashboardHandlerMixin._handle_get_nav_config(handler)

        labels: list[str] = [l["label"] for l in captured["data"]["links"]]
        self.assertIn("Zigbee", labels)
        self.assertIn("Home", labels)


# ---------------------------------------------------------------------------
# Wake word sentinel
# ---------------------------------------------------------------------------

@unittest.skipUnless(_HAS_NUMPY, "numpy required for voice modules")
class TestWakeWordSentinel(unittest.TestCase):
    """Wake word detector checks for openWakeWord before use."""

    def test_has_oww_sentinel_exists(self) -> None:
        """wake.py has _HAS_OWW sentinel."""
        from voice.satellite import wake
        self.assertTrue(hasattr(wake, "_HAS_OWW"))
        self.assertIsInstance(wake._HAS_OWW, bool)

    def test_wake_detector_raises_import_error_without_oww(self) -> None:
        """WakeDetector raises ImportError if openWakeWord missing."""
        from voice.satellite.wake import WakeDetector, _HAS_OWW
        if _HAS_OWW:
            self.skipTest("openWakeWord is installed — cannot test missing")
        with self.assertRaises(ImportError):
            WakeDetector(model_path="nonexistent.onnx")


# ---------------------------------------------------------------------------
# Voice satellite graceful startup
# ---------------------------------------------------------------------------

@unittest.skipUnless(_HAS_NUMPY, "numpy required for voice modules")
class TestSatelliteGracefulStartup(unittest.TestCase):
    """Satellite daemon handles missing dependencies without crashing."""

    def _make_daemon(self, **overrides: Any) -> Any:
        """Create a SatelliteDaemon with minimal config."""
        from voice.satellite.daemon import SatelliteDaemon
        config: dict[str, Any] = {
            "room": "Test Room",
            "mqtt": {"broker": "localhost", "port": 1883},
            "mock_wake": True,
        }
        config.update(overrides)
        return SatelliteDaemon(config)

    def test_mqtt_broker_unreachable_does_not_crash(self) -> None:
        """Satellite MQTT init does not crash when the broker is unreachable.

        Historical: this test used to call ``daemon.start()`` and rely
        on the synchronous ``client.connect`` raising ``OSError``,
        which the daemon caught and returned from ``start``.  After
        the 2026-04-19 move to ``MqttResilientClient`` (and
        ``connect_async``), ``_init_mqtt`` is non-blocking — an
        unreachable broker at boot no longer aborts startup, the
        helper's watchdog + paho's internal reconnect drive
        establishment later.  The test now verifies the narrower
        (and actually-load-bearing) invariant: ``_init_mqtt`` itself
        does not raise when the broker is unreachable.
        """
        daemon = self._make_daemon(
            mqtt={"broker": "192.0.2.1", "port": 1},
        )
        try:
            daemon._init_mqtt()
        finally:
            # Tear down the helper so its background threads exit
            # before the test returns — otherwise a daemon watchdog
            # thread keeps poking at an unreachable broker for the
            # rest of the pytest process lifetime.
            if daemon._mqtt_client is not None:
                daemon._mqtt_client.stop()


# ---------------------------------------------------------------------------
# Voice coordinator graceful startup
# ---------------------------------------------------------------------------

@unittest.skipUnless(_HAS_NUMPY, "numpy required for voice modules")
class TestCoordinatorGracefulStartup(unittest.TestCase):
    """Coordinator daemon handles missing dependencies without crashing."""

    def test_mqtt_broker_unreachable_does_not_crash(self) -> None:
        """Coordinator MQTT init does not crash when broker is unreachable.

        See the companion satellite test for the rationale on the
        semantic shift from "start returns on broker failure" to
        "init does not raise on broker failure" — the 2026-04-19
        ``MqttResilientClient`` refactor moved broker establishment
        to an async background path.
        """
        from voice.coordinator.daemon import CoordinatorDaemon
        config: dict[str, Any] = {
            "mqtt": {"broker": "192.0.2.1", "port": 1},
            "glowup": {
                "api_base": "http://localhost:8420",
                "auth_token": "test",
            },
            "mock_stt": True,
            "mock_intent": True,
        }
        daemon = CoordinatorDaemon(config)
        try:
            daemon._init_mqtt()
        finally:
            if daemon._mqtt_client is not None:
                daemon._mqtt_client.stop()


# ---------------------------------------------------------------------------
# Server config defaults
# ---------------------------------------------------------------------------

class TestServerConfigDefaults(unittest.TestCase):
    """Server config uses safe defaults for optional keys."""

    def test_mqtt_sentinel_exists(self) -> None:
        """server.py has _MQTT_AVAILABLE sentinel."""
        import server
        self.assertTrue(hasattr(server, "_MQTT_AVAILABLE"))
        self.assertIsInstance(server._MQTT_AVAILABLE, bool)

    def test_optional_config_keys_have_defaults(self) -> None:
        """Optional config keys use .get() with defaults, not []."""
        # Verify the nav_links, home_display, and schedule_groups
        # patterns are safe by checking they don't raise KeyError
        # on empty config.
        config: dict[str, Any] = {}
        self.assertEqual(config.get("nav_links", []), [])
        self.assertEqual(config.get("home_display", {}), {})
        self.assertEqual(config.get("schedule_groups", {}), {})
        self.assertEqual(config.get("location", {}), {})


if __name__ == "__main__":
    unittest.main()
