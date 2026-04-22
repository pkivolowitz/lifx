"""Tests for zigbee_service device-type inference.

Covers ``infer_device_type`` and the sticky-classification contract in
``StateRegistry.update`` — the hub's device list, group model, and
scheduler will all filter by ``type``, so mis-classification leaks
sensors onto control surfaces intended for plugs.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.

__version__ = "1.0"

import sys
import os
import unittest
from typing import Any

# zigbee_service is a peer directory to tests/ inside the repo root.
# Add the repo root to sys.path so the import below resolves without
# requiring the service to be pip-installed.
_REPO_ROOT: str = os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."),
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Taxonomy lives in device_types (dep-free, importable by hub clients);
# StateRegistry lives in service (needs paho, only loadable in the dev
# environment).  Test both surfaces.
from zigbee_service.device_types import (
    TYPE_BUTTON,
    TYPE_CONTACT,
    TYPE_MOTION,
    TYPE_PLUG,
    TYPE_SOIL,
    TYPE_UNKNOWN,
    infer_device_type,
)
from zigbee_service.service import StateRegistry


class TypeInferenceTests(unittest.TestCase):
    """Pure-function tests for ``infer_device_type``."""

    def test_empty_payload_is_unknown(self) -> None:
        """A device that has reported nothing useful stays UNKNOWN."""
        self.assertEqual(infer_device_type({}), TYPE_UNKNOWN)

    def test_linkquality_only_is_unknown(self) -> None:
        """Bare heartbeat with no distinguishing field is UNKNOWN."""
        self.assertEqual(infer_device_type({"linkquality": 208}), TYPE_UNKNOWN)

    def test_plug_with_metering(self) -> None:
        """ThirdReality Gen3 shape — state + power + voltage + current."""
        payload: dict[str, Any] = {
            "state": "ON", "power": 12.3, "voltage": 121.0,
            "current": 0.1, "energy": 0.456,
        }
        self.assertEqual(infer_device_type(payload), TYPE_PLUG)

    def test_plug_without_metering(self) -> None:
        """A bare relay that only reports state is still a plug."""
        self.assertEqual(infer_device_type({"state": "OFF"}), TYPE_PLUG)

    def test_soil_sensor(self) -> None:
        """ThirdReality soil sensor shape — soil_moisture is the marker."""
        payload: dict[str, Any] = {
            "soil_moisture": 59.71, "humidity": 43.54,
            "temperature": 18.64, "battery": 100,
        }
        self.assertEqual(infer_device_type(payload), TYPE_SOIL)

    def test_soil_with_spurious_state_still_classifies_as_soil(self) -> None:
        """Sensor fingerprint wins over the generic state field."""
        payload: dict[str, Any] = {"soil_moisture": 42.0, "state": "ON"}
        self.assertEqual(infer_device_type(payload), TYPE_SOIL)

    def test_contact_sensor(self) -> None:
        """Door-window sensor — contact=True/False is the marker."""
        self.assertEqual(
            infer_device_type({"contact": False, "battery": 91}),
            TYPE_CONTACT,
        )

    def test_motion_via_occupancy(self) -> None:
        """Z2M standard occupancy field classifies as motion."""
        self.assertEqual(
            infer_device_type({"occupancy": True, "illuminance": 30}),
            TYPE_MOTION,
        )

    def test_motion_via_motion_alias(self) -> None:
        """Some devices publish ``motion`` instead of ``occupancy``."""
        self.assertEqual(infer_device_type({"motion": True}), TYPE_MOTION)

    def test_button_via_action(self) -> None:
        """Scene controllers publish an ``action`` like ``single``/``hold``."""
        self.assertEqual(infer_device_type({"action": "single"}), TYPE_BUTTON)


class StickyClassificationTests(unittest.TestCase):
    """Type must stick once a distinguishing field has ever arrived."""

    def test_soil_sensor_stays_soil_after_heartbeat(self) -> None:
        """Follow-up payload with only linkquality must not downgrade type."""
        reg: StateRegistry = StateRegistry()
        reg.update("SBYRD", {
            "soil_moisture": 59.71, "humidity": 43.54, "battery": 100,
        })
        dev = reg.get("SBYRD")
        self.assertIsNotNone(dev)
        assert dev is not None  # narrow for mypy
        self.assertEqual(dev.type, TYPE_SOIL)
        # Next message carries only linkquality — raw accumulates, so
        # soil_moisture is still present and the type remains soil.
        reg.update("SBYRD", {"linkquality": 208})
        dev = reg.get("SBYRD")
        assert dev is not None
        self.assertEqual(dev.type, TYPE_SOIL)

    def test_plug_starts_unknown_then_becomes_plug(self) -> None:
        """A new plug whose first message is a heartbeat is UNKNOWN, then plug."""
        reg: StateRegistry = StateRegistry()
        reg.update("LRTV", {"linkquality": 80})
        dev = reg.get("LRTV")
        assert dev is not None
        self.assertEqual(dev.type, TYPE_UNKNOWN)
        reg.update("LRTV", {"state": "ON", "power": 42.0})
        dev = reg.get("LRTV")
        assert dev is not None
        self.assertEqual(dev.type, TYPE_PLUG)

    def test_type_appears_in_serialised_output(self) -> None:
        """/devices consumers read ``type`` — confirm it's in to_dict()."""
        reg: StateRegistry = StateRegistry()
        reg.update("LRTV", {"state": "ON", "power": 12.0})
        dev = reg.get("LRTV")
        assert dev is not None
        serialised: dict[str, Any] = dev.to_dict()
        self.assertIn("type", serialised)
        self.assertEqual(serialised["type"], TYPE_PLUG)


if __name__ == "__main__":
    unittest.main()
