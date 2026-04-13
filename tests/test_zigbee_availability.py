"""Tests for ZigbeeAdapter availability gate (Option-3 retained-MQTT defense).

The Zigbee adapter watches ``zigbee2mqtt/<device>/availability`` and
tracks per-device online/offline state.  When a device is known to be
offline, any inbound ``zigbee2mqtt/<device>`` base-topic payload is
dropped — this prevents stale retained MQTT from being re-ingested
into the SignalBus and the power logger every time the adapter
reconnects.  On an online → offline transition the adapter also calls
``power_logger.mark_offline(device)`` which writes a NULL sentinel
row to ``power.db``, ensuring the /power dashboard renders "—" for
the offline device instead of the last pre-offline value.

See feedback_retained_mqtt_replays.md and reference_broker2.md for
the 2026-04-12 ML_Power incident that motivated this defense.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import unittest
from typing import Any
from unittest.mock import MagicMock

from adapters.zigbee_adapter import (
    AVAILABILITY_OFFLINE,
    AVAILABILITY_ONLINE,
    ZigbeeAdapter,
)


class _StubBus:
    """Minimal SignalBus stand-in that records writes."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, float]] = []
        self.registrations: list[str] = []

    def register(self, name: str, meta: Any) -> None:
        self.registrations.append(name)

    def write(self, name: str, value: float) -> None:
        self.writes.append((name, value))


def _make_adapter() -> tuple[ZigbeeAdapter, _StubBus, MagicMock]:
    """Build an un-started ZigbeeAdapter wired to a stub bus and a
    mock power logger.  ``_handle_message`` can be called directly
    without ever starting MQTT."""
    bus = _StubBus()
    adapter = ZigbeeAdapter(
        config={},
        bus=bus,
        broker="localhost",
        port=1883,
    )
    power_logger = MagicMock()
    adapter._power_logger = power_logger
    return adapter, bus, power_logger


class TestZigbeeAvailabilityParse(unittest.TestCase):
    """Parsing Z2M availability messages in both JSON and bare-string forms."""

    def test_json_online_updates_state(self) -> None:
        adapter, _, _ = _make_adapter()
        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b'{"state":"online"}',
        )
        self.assertEqual(
            adapter._availability.get("ML_Power"),
            AVAILABILITY_ONLINE,
        )

    def test_json_offline_updates_state(self) -> None:
        adapter, _, _ = _make_adapter()
        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b'{"state":"offline"}',
        )
        self.assertEqual(
            adapter._availability.get("ML_Power"),
            AVAILABILITY_OFFLINE,
        )

    def test_bare_string_online_accepted(self) -> None:
        """Older Z2M versions publish plain 'online' / 'offline'."""
        adapter, _, _ = _make_adapter()
        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b"online",
        )
        self.assertEqual(
            adapter._availability.get("ML_Power"),
            AVAILABILITY_ONLINE,
        )

    def test_bare_string_offline_accepted(self) -> None:
        adapter, _, _ = _make_adapter()
        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b"offline",
        )
        self.assertEqual(
            adapter._availability.get("ML_Power"),
            AVAILABILITY_OFFLINE,
        )

    def test_unknown_state_value_ignored(self) -> None:
        """Unexpected state strings (not online/offline) leave state unchanged."""
        adapter, _, _ = _make_adapter()
        adapter._availability["ML_Power"] = AVAILABILITY_ONLINE
        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b'{"state":"banana"}',
        )
        self.assertEqual(
            adapter._availability["ML_Power"],
            AVAILABILITY_ONLINE,
        )

    def test_malformed_json_does_not_crash(self) -> None:
        adapter, _, _ = _make_adapter()
        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b"{not json",
        )
        self.assertNotIn("ML_Power", adapter._availability)


class TestZigbeeAvailabilityGate(unittest.TestCase):
    """The offline gate on base-topic payloads."""

    def _offline_payload(self) -> bytes:
        """Representative retained ML_Power payload."""
        return json.dumps({
            "state": "ON",
            "power": 168.5,
            "voltage": 121.6,
            "current": 1.42,
            "energy": 17.4,
            "power_factor": 0.94,
        }).encode()

    def test_base_payload_dropped_when_offline(self) -> None:
        """A payload on zigbee2mqtt/<device> is ignored if device is offline."""
        adapter, bus, power_logger = _make_adapter()
        # Mark offline via availability message.
        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b'{"state":"offline"}',
        )
        # mark_offline should have been called exactly once on the transition.
        self.assertEqual(power_logger.mark_offline.call_count, 1)
        power_logger.reset_mock()

        # Now simulate a retained replay on the base topic.
        adapter._handle_message(
            "zigbee2mqtt/ML_Power",
            self._offline_payload(),
        )

        # Bus should see no writes and logger should see no record() calls.
        self.assertEqual(bus.writes, [])
        power_logger.record.assert_not_called()

    def test_base_payload_accepted_when_online(self) -> None:
        """A payload on zigbee2mqtt/<device> is processed normally when online."""
        adapter, bus, power_logger = _make_adapter()
        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b'{"state":"online"}',
        )
        adapter._handle_message(
            "zigbee2mqtt/ML_Power",
            self._offline_payload(),
        )
        # Should have bus writes for each numeric key.
        written = {name for name, _ in bus.writes}
        self.assertIn("ML_Power:power", written)
        self.assertIn("ML_Power:voltage", written)
        power_logger.record.assert_any_call("ML_Power", "power", 168.5)

    def test_base_payload_accepted_when_never_seen(self) -> None:
        """Devices never having sent availability default to online."""
        adapter, bus, power_logger = _make_adapter()
        adapter._handle_message(
            "zigbee2mqtt/NewPlug",
            self._offline_payload(),
        )
        self.assertTrue(any(name == "NewPlug:power" for name, _ in bus.writes))
        power_logger.record.assert_any_call("NewPlug", "power", 168.5)


class TestZigbeeOfflineTransitionMarksLogger(unittest.TestCase):
    """mark_offline is called exactly on the online → offline edge."""

    def test_online_to_offline_calls_mark_offline(self) -> None:
        adapter, _, power_logger = _make_adapter()
        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b'{"state":"online"}',
        )
        power_logger.mark_offline.assert_not_called()

        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b'{"state":"offline"}',
        )
        power_logger.mark_offline.assert_called_once_with("ML_Power")

    def test_offline_to_offline_does_not_remark(self) -> None:
        """Duplicate offline messages should not spam mark_offline."""
        adapter, _, power_logger = _make_adapter()
        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b'{"state":"offline"}',
        )
        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b'{"state":"offline"}',
        )
        self.assertEqual(power_logger.mark_offline.call_count, 1)

    def test_never_seen_to_offline_calls_mark_offline(self) -> None:
        """First message being offline still records the transition so
        the dashboard renders correctly for a device that boots offline."""
        adapter, _, power_logger = _make_adapter()
        adapter._handle_message(
            "zigbee2mqtt/NeverSeen/availability",
            b'{"state":"offline"}',
        )
        power_logger.mark_offline.assert_called_once_with("NeverSeen")

    def test_online_does_not_call_mark_offline(self) -> None:
        adapter, _, power_logger = _make_adapter()
        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b'{"state":"online"}',
        )
        power_logger.mark_offline.assert_not_called()

    def test_mark_offline_exception_does_not_crash_handler(self) -> None:
        """Failures in the power logger must not prevent state update."""
        adapter, _, power_logger = _make_adapter()
        power_logger.mark_offline.side_effect = RuntimeError("db gone")
        # Must not raise.
        adapter._handle_message(
            "zigbee2mqtt/ML_Power/availability",
            b'{"state":"offline"}',
        )
        # State is still tracked despite the logger crash.
        self.assertEqual(
            adapter._availability["ML_Power"],
            AVAILABILITY_OFFLINE,
        )


if __name__ == "__main__":
    unittest.main()
