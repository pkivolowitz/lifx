"""Tests for /api/ble/sensors backed by ble_trigger.sensor_data.

After the 2026-04-15 service-pattern pivot, the BLE diagnostic
endpoint reads from the in-process ``BleSensorData`` store hydrated
by ``BleTriggerManager``'s local MQTT subscriber, NOT from the
``SignalBus`` transport metadata (which the deleted ``BleAdapter``
used to populate).  These tests inject fake data into the store and
exercise the handler methods directly.

Bound to the real handler implementations via direct method call —
no HTTP server, no MQTT, no real bus.  If the production handler
changes its source of truth again, every test here should fail
loudly so the rewire is visible.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "2.0"

import unittest
from typing import Any, Optional

from infrastructure.ble_trigger import sensor_data as ble_sensor_data


# ---------------------------------------------------------------------------
# Minimal stand-in for GlowUpRequestHandler — enough to run the
# endpoint methods in isolation.  Captures _send_json so each test
# can inspect the response code and body.
# ---------------------------------------------------------------------------


class _FakeHandler:
    """Stand-in for GlowUpRequestHandler.

    Carries only the attributes the BLE handlers actually read:
    ``config`` (for sensor_locations enrichment).  No signal_bus,
    no ble_adapter, no automation_manager — those references were
    intentionally removed from the handler in the BLE pivot.
    """

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        self.config: dict[str, Any] = config or {}
        self.response_code: Optional[int] = None
        self.response_body: Optional[Any] = None

    def _send_json(self, code: int, body: Any) -> None:
        self.response_code = code
        self.response_body = body


def _reset_store() -> None:
    """Empty the global BleSensorData singleton between tests."""
    with ble_sensor_data._lock:
        ble_sensor_data._data.clear()


class TestBleSensorEndpoint(unittest.TestCase):
    """Test /api/ble/sensors backed by infrastructure.ble_trigger.sensor_data."""

    def setUp(self) -> None:
        _reset_store()

    def tearDown(self) -> None:
        _reset_store()

    def _call_get_ble_sensors(self, handler: _FakeHandler) -> None:
        from server import GlowUpRequestHandler
        GlowUpRequestHandler._handle_get_ble_sensors(handler)

    def _call_get_ble_sensor_detail(
        self, handler: _FakeHandler, label: str,
    ) -> None:
        from server import GlowUpRequestHandler
        GlowUpRequestHandler._handle_get_ble_sensor_detail(handler, label)

    def test_empty_store_returns_empty_dict(self) -> None:
        """No data in the store → empty 200 response."""
        handler = _FakeHandler()
        self._call_get_ble_sensors(handler)
        self.assertEqual(handler.response_code, 200)
        self.assertEqual(handler.response_body, {})

    def test_single_sensor_grouped_by_label(self) -> None:
        """A populated store appears in the response, grouped by label."""
        ble_sensor_data.update("onvis_motion", "motion", 1)
        ble_sensor_data.update("onvis_motion", "temperature", 22.5)
        ble_sensor_data.update("onvis_motion", "humidity", 55.0)

        handler = _FakeHandler()
        self._call_get_ble_sensors(handler)

        self.assertEqual(handler.response_code, 200)
        body: dict = handler.response_body
        self.assertIn("onvis_motion", body)
        sensor = body["onvis_motion"]
        # motion comes back as int (legacy contract for the frontend).
        self.assertEqual(sensor["motion"], 1)
        self.assertIsInstance(sensor["motion"], int)
        self.assertEqual(sensor["temperature"], 22.5)
        self.assertEqual(sensor["humidity"], 55.0)
        self.assertIn("last_update", sensor)

    def test_multiple_labels_kept_separate(self) -> None:
        """Two distinct labels do not bleed into each other."""
        ble_sensor_data.update("onvis_a", "motion", 1)
        ble_sensor_data.update("onvis_b", "motion", 0)

        handler = _FakeHandler()
        self._call_get_ble_sensors(handler)

        self.assertIn("onvis_a", handler.response_body)
        self.assertIn("onvis_b", handler.response_body)
        self.assertEqual(handler.response_body["onvis_a"]["motion"], 1)
        self.assertEqual(handler.response_body["onvis_b"]["motion"], 0)

    def test_motion_coerced_to_int(self) -> None:
        """A float motion value is coerced to int for the response."""
        ble_sensor_data.update("onvis_motion", "motion", 1.0)

        handler = _FakeHandler()
        self._call_get_ble_sensors(handler)

        self.assertEqual(handler.response_body["onvis_motion"]["motion"], 1)
        self.assertIsInstance(
            handler.response_body["onvis_motion"]["motion"], int,
        )

    def test_location_enrichment(self) -> None:
        """sensor_locations config enriches the response."""
        ble_sensor_data.update("onvis_motion", "motion", 0)
        handler = _FakeHandler(config={
            "sensor_locations": {"onvis_motion": "Living Room"},
        })

        self._call_get_ble_sensors(handler)

        sensor = handler.response_body["onvis_motion"]
        self.assertEqual(sensor["location"], "Living Room")

    def test_status_blob_passes_through(self) -> None:
        """JSON status blobs from glowup/ble/status/* show up in the response.

        After the pivot, the status blob is just another field in
        ``BleSensorData``, written by ``BleTriggerManager._on_message``
        when a ``glowup/ble/status/{label}`` message arrives.  The
        handler does not have to call out to a separate adapter
        proxy any more.
        """
        ble_sensor_data.update("onvis_motion", "motion", 0)
        ble_sensor_data.update(
            "onvis_motion", "status", {"state": "monitoring"},
        )

        handler = _FakeHandler()
        self._call_get_ble_sensors(handler)

        sensor = handler.response_body["onvis_motion"]
        self.assertEqual(sensor["status"], {"state": "monitoring"})

    def test_detail_endpoint_returns_single_sensor(self) -> None:
        """Detail endpoint returns data for a single label."""
        ble_sensor_data.update("onvis_motion", "temperature", 21.0)

        handler = _FakeHandler()
        self._call_get_ble_sensor_detail(handler, "onvis_motion")

        self.assertEqual(handler.response_code, 200)
        self.assertIn("temperature", handler.response_body)
        self.assertEqual(handler.response_body["temperature"], 21.0)

    def test_detail_endpoint_404_for_unknown(self) -> None:
        """Detail endpoint returns 404 for an unknown label."""
        handler = _FakeHandler()
        self._call_get_ble_sensor_detail(handler, "nonexistent")

        self.assertEqual(handler.response_code, 404)
        self.assertIn("error", handler.response_body)

    def test_detail_endpoint_includes_location(self) -> None:
        """Detail endpoint enriches with sensor_locations like the list view."""
        ble_sensor_data.update("onvis_motion", "motion", 1)
        handler = _FakeHandler(config={
            "sensor_locations": {"onvis_motion": "Hallway"},
        })

        self._call_get_ble_sensor_detail(handler, "onvis_motion")

        self.assertEqual(handler.response_code, 200)
        self.assertEqual(handler.response_body["location"], "Hallway")


if __name__ == "__main__":
    unittest.main()
