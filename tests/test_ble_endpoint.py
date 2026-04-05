"""Tests for the BLE sensor REST endpoint backed by SignalBus.

Exercises _handle_get_ble_sensors and _handle_get_ble_sensor_detail
with a real SignalBus to ensure the transport-metadata-based query
and response formatting work end-to-end.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import time
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock, patch

from media import SignalBus, SignalMeta


# ---------------------------------------------------------------------------
# Minimal mock for GlowUpRequestHandler — enough to test the endpoint
# logic without starting a real HTTP server.
# ---------------------------------------------------------------------------

class _FakeHandler:
    """Minimal stand-in for GlowUpRequestHandler.

    Captures _send_json calls so tests can inspect the response.
    """

    config: dict[str, Any] = {}
    signal_bus: Optional[SignalBus] = None
    ble_adapter: Optional[Any] = None
    lock_manager = None
    automation_manager = None

    def __init__(self) -> None:
        """Initialize with empty response capture."""
        self.response_code: Optional[int] = None
        self.response_body: Optional[dict] = None

    def _send_json(self, code: int, body: dict) -> None:
        """Capture the response."""
        self.response_code = code
        self.response_body = body


class TestBleSensorEndpoint(unittest.TestCase):
    """Test /api/ble/sensors backed by SignalBus."""

    def _make_handler(
        self, bus: SignalBus, config: Optional[dict] = None,
        adapter: Optional[Any] = None,
    ) -> _FakeHandler:
        """Create a handler with injected dependencies."""
        handler = _FakeHandler()
        handler.signal_bus = bus
        handler.config = config or {}
        handler.ble_adapter = adapter
        return handler

    def _call_get_ble_sensors(self, handler: _FakeHandler) -> None:
        """Invoke the endpoint method directly."""
        # Import the actual handler method and bind it.
        from server import GlowUpRequestHandler
        method = GlowUpRequestHandler._handle_get_ble_sensors
        method(handler)

    def _call_get_ble_sensor_detail(
        self, handler: _FakeHandler, label: str,
    ) -> None:
        """Invoke the detail endpoint method directly."""
        from server import GlowUpRequestHandler
        method = GlowUpRequestHandler._handle_get_ble_sensor_detail
        method(handler, label)

    def test_empty_bus_returns_empty_dict(self) -> None:
        """No signals → empty response."""
        bus = SignalBus()
        handler = self._make_handler(bus)
        self._call_get_ble_sensors(handler)
        self.assertEqual(handler.response_code, 200)
        self.assertEqual(handler.response_body, {})

    def test_no_bus_returns_empty_dict(self) -> None:
        """No signal bus at all → empty response."""
        handler = _FakeHandler()
        handler.signal_bus = None
        handler.config = {}
        handler.ble_adapter = None
        from server import GlowUpRequestHandler
        GlowUpRequestHandler._handle_get_ble_sensors(handler)
        self.assertEqual(handler.response_code, 200)
        self.assertEqual(handler.response_body, {})

    def test_ble_signals_grouped_by_label(self) -> None:
        """BLE signals appear grouped by device label."""
        bus = SignalBus()
        # Register and write signals as the BleAdapter would.
        bus.register("onvis_motion:motion", SignalMeta(
            transport="ble", source_name="onvis_motion",
        ))
        bus.write("onvis_motion:motion", 1.0)
        bus.register("onvis_motion:temperature", SignalMeta(
            transport="ble", source_name="onvis_motion",
        ))
        bus.write("onvis_motion:temperature", 22.5)
        bus.register("onvis_motion:humidity", SignalMeta(
            transport="ble", source_name="onvis_motion",
        ))
        bus.write("onvis_motion:humidity", 55.0)

        handler = self._make_handler(bus)
        self._call_get_ble_sensors(handler)

        self.assertEqual(handler.response_code, 200)
        body: dict = handler.response_body
        self.assertIn("onvis_motion", body)

        sensor: dict = body["onvis_motion"]
        self.assertEqual(sensor["motion"], 1)  # int, not float
        self.assertEqual(sensor["temperature"], 22.5)
        self.assertEqual(sensor["humidity"], 55.0)
        self.assertIn("last_update", sensor)

    def test_non_ble_signals_excluded(self) -> None:
        """Zigbee signals do not appear in the BLE endpoint."""
        bus = SignalBus()
        bus.register("onvis_motion:motion", SignalMeta(transport="ble"))
        bus.write("onvis_motion:motion", 1.0)
        bus.register("hallway:occupancy", SignalMeta(transport="zigbee"))
        bus.write("hallway:occupancy", 1.0)

        handler = self._make_handler(bus)
        self._call_get_ble_sensors(handler)

        body: dict = handler.response_body
        self.assertIn("onvis_motion", body)
        self.assertNotIn("hallway", body)

    def test_location_enrichment(self) -> None:
        """sensor_locations config enriches the response."""
        bus = SignalBus()
        bus.register("onvis_motion:motion", SignalMeta(transport="ble"))
        bus.write("onvis_motion:motion", 0.0)

        handler = self._make_handler(bus, config={
            "sensor_locations": {"onvis_motion": "Living Room"},
        })
        self._call_get_ble_sensors(handler)

        sensor: dict = handler.response_body["onvis_motion"]
        self.assertEqual(sensor["location"], "Living Room")

    def test_status_blob_from_adapter(self) -> None:
        """BLE adapter proxy status blobs are included in the response."""
        bus = SignalBus()
        bus.register("onvis_motion:motion", SignalMeta(transport="ble"))
        bus.write("onvis_motion:motion", 0.0)

        # Mock the proxy interface — handler calls send_command().
        adapter = MagicMock()
        adapter.send_command.return_value = {
            "status": "ok", "blob": {"state": "monitoring"},
        }

        handler = self._make_handler(bus, adapter=adapter)
        self._call_get_ble_sensors(handler)

        sensor: dict = handler.response_body["onvis_motion"]
        self.assertEqual(sensor["status"], {"state": "monitoring"})

    def test_detail_endpoint_returns_single_sensor(self) -> None:
        """Detail endpoint returns data for a single label."""
        bus = SignalBus()
        bus.register("onvis_motion:temperature", SignalMeta(transport="ble"))
        bus.write("onvis_motion:temperature", 21.0)

        handler = self._make_handler(bus)
        self._call_get_ble_sensor_detail(handler, "onvis_motion")

        self.assertEqual(handler.response_code, 200)
        self.assertIn("temperature", handler.response_body)
        self.assertEqual(handler.response_body["temperature"], 21.0)

    def test_detail_endpoint_404_for_unknown(self) -> None:
        """Detail endpoint returns 404 for unknown sensor."""
        bus = SignalBus()
        handler = self._make_handler(bus)
        self._call_get_ble_sensor_detail(handler, "nonexistent")

        self.assertEqual(handler.response_code, 404)
        self.assertIn("error", handler.response_body)


if __name__ == "__main__":
    unittest.main()
