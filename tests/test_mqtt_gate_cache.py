"""Tests for the MqttBridge voice-gate cache.

Exercises :meth:`MqttBridge._update_gate_cache` and
:meth:`MqttBridge.get_gates` in isolation — no broker, no paho
loop, no device manager.  The bridge is constructed with a
MagicMock device manager and a minimal config because the cache
path does not touch either.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import time
import unittest
from typing import Any
from unittest.mock import MagicMock

from infrastructure.mqtt_bridge import MqttBridge


def _make_bridge() -> MqttBridge:
    """Construct a bare MqttBridge with no real MQTT client.

    The gate cache code never touches ``self._client`` or the
    DeviceManager, so MagicMock is sufficient.
    """
    return MqttBridge(MagicMock(), {"mqtt": {"broker": "test"}})


def _payload(enabled: bool, expires_at: float) -> bytes:
    return json.dumps(
        {"enabled": enabled, "expires_at": expires_at},
    ).encode("utf-8")


class TestGateCacheUpdate(unittest.TestCase):
    """_update_gate_cache handles every payload shape."""

    def test_open_gate_recorded(self) -> None:
        b = _make_bridge()
        expires: float = time.time() + 600
        b._update_gate_cache(
            "glowup/voice/gate/doorbell",
            _payload(True, expires),
        )
        self.assertIn("doorbell", b._gates)
        self.assertTrue(b._gates["doorbell"]["enabled"])
        self.assertAlmostEqual(
            b._gates["doorbell"]["expires_at"], expires, delta=0.01,
        )

    def test_closed_gate_recorded(self) -> None:
        """Closed state is still cached so get_gates() sees the latest."""
        b = _make_bridge()
        b._update_gate_cache(
            "glowup/voice/gate/doorbell",
            _payload(False, 0),
        )
        self.assertIn("doorbell", b._gates)
        self.assertFalse(b._gates["doorbell"]["enabled"])

    def test_multiple_slugs_independent(self) -> None:
        b = _make_bridge()
        now: float = time.time()
        b._update_gate_cache(
            "glowup/voice/gate/doorbell", _payload(True, now + 600),
        )
        b._update_gate_cache(
            "glowup/voice/gate/side_gate", _payload(True, now + 1200),
        )
        self.assertEqual(
            set(b._gates.keys()), {"doorbell", "side_gate"},
        )

    def test_empty_payload_clears_slot(self) -> None:
        b = _make_bridge()
        b._gates["doorbell"] = {"enabled": True, "expires_at": 9e9}
        b._update_gate_cache("glowup/voice/gate/doorbell", b"")
        self.assertNotIn("doorbell", b._gates)

    def test_malformed_json_clears_slot(self) -> None:
        b = _make_bridge()
        b._gates["doorbell"] = {"enabled": True, "expires_at": 9e9}
        b._update_gate_cache(
            "glowup/voice/gate/doorbell", b"{not json",
        )
        self.assertNotIn("doorbell", b._gates)

    def test_wrong_type_payload_clears_slot(self) -> None:
        b = _make_bridge()
        b._gates["doorbell"] = {"enabled": True, "expires_at": 9e9}
        b._update_gate_cache(
            "glowup/voice/gate/doorbell",
            json.dumps({"enabled": True, "expires_at": "soon"}).encode(),
        )
        self.assertNotIn("doorbell", b._gates)

    def test_blank_slug_ignored(self) -> None:
        b = _make_bridge()
        # Topic with trailing slash produces an empty slug — must not
        # populate the cache with a "" key.
        b._update_gate_cache(
            "glowup/voice/gate/",
            _payload(True, time.time() + 600),
        )
        self.assertNotIn("", b._gates)

    def test_slug_with_underscores(self) -> None:
        b = _make_bridge()
        b._update_gate_cache(
            "glowup/voice/gate/side_porch",
            _payload(True, time.time() + 300),
        )
        self.assertIn("side_porch", b._gates)


class TestGetGates(unittest.TestCase):
    """get_gates filters to live, enabled gates at read time."""

    def test_returns_only_enabled(self) -> None:
        b = _make_bridge()
        now: float = time.time()
        b._update_gate_cache(
            "glowup/voice/gate/doorbell",
            _payload(True, now + 600),
        )
        b._update_gate_cache(
            "glowup/voice/gate/side",
            _payload(False, 0),
        )
        snap: dict[str, Any] = b.get_gates()
        self.assertEqual(list(snap.keys()), ["doorbell"])

    def test_filters_expired(self) -> None:
        b = _make_bridge()
        b._update_gate_cache(
            "glowup/voice/gate/doorbell",
            _payload(True, time.time() - 1),
        )
        self.assertEqual(b.get_gates(), {})

    def test_empty_when_no_messages(self) -> None:
        self.assertEqual(_make_bridge().get_gates(), {})

    def test_snapshot_is_copy(self) -> None:
        """Mutating the returned dict must not poison the cache."""
        b = _make_bridge()
        b._update_gate_cache(
            "glowup/voice/gate/doorbell",
            _payload(True, time.time() + 600),
        )
        snap = b.get_gates()
        snap["doorbell"]["enabled"] = False
        snap["malicious"] = {"enabled": True, "expires_at": 9e9}
        # Original cache entry unchanged.
        self.assertTrue(b._gates["doorbell"]["enabled"])
        self.assertNotIn("malicious", b._gates)


if __name__ == "__main__":
    unittest.main()
