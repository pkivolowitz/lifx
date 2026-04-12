"""Tests for the satellite-side voice gate state machine.

Validates the gate message parser, auto-expiry transition, retained
republish on expiry, and the ``_gate_permits_audio`` flow used by
the main audio loop.  Does not start a real satellite — constructs
the daemon object with a dummy config and pokes gate methods
directly.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import time
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from voice import constants as C
from voice.satellite.daemon import SatelliteDaemon


def _make_gated_sat() -> SatelliteDaemon:
    """Build a SatelliteDaemon with voice_gate enabled and MQTT mocked.

    PiperPool init and capture construction are sidestepped — we only
    exercise gate state, not audio.  A mock MQTT client is attached
    so ``_publish_gate_closed`` can be observed.
    """
    cfg: dict[str, Any] = {
        "room": "Doorbell",
        "mqtt": {"broker": "localhost", "port": 1883},
        "voice_gate": {"gated": True},
        "tts_output": "mqtt",  # skip piper pool init
    }
    with patch.object(SatelliteDaemon, "_init_piper_pool"):
        sat = SatelliteDaemon(cfg)
    sat._mqtt_client = MagicMock()
    return sat


def _msg(topic: str, payload: dict[str, Any]) -> SimpleNamespace:
    """Fabricate a paho-mqtt-like message object."""
    return SimpleNamespace(
        topic=topic, payload=json.dumps(payload).encode("utf-8"),
    )


class TestGateBootstrap(unittest.TestCase):
    """Gated satellites boot with the gate closed."""

    def test_default_closed(self) -> None:
        sat = _make_gated_sat()
        self.assertTrue(sat._gated)
        self.assertFalse(sat._gate_open)
        self.assertFalse(sat._gate_permits_audio())

    def test_slug_derivation(self) -> None:
        sat = _make_gated_sat()
        self.assertEqual(sat._gate_slug, "doorbell")
        self.assertEqual(
            sat._gate_topic, "glowup/voice/gate/doorbell",
        )

    def test_multi_word_room_slug(self) -> None:
        cfg: dict[str, Any] = {
            "room": "Front Gate",
            "voice_gate": {"gated": True},
            "tts_output": "mqtt",
        }
        with patch.object(SatelliteDaemon, "_init_piper_pool"):
            sat = SatelliteDaemon(cfg)
        self.assertEqual(sat._gate_slug, "front_gate")
        self.assertEqual(
            sat._gate_topic, "glowup/voice/gate/front_gate",
        )


class TestGateNonGated(unittest.TestCase):
    """Rooms without ``gated: true`` always permit audio."""

    def test_non_gated_is_transparent(self) -> None:
        cfg: dict[str, Any] = {
            "room": "Main Bedroom",
            "tts_output": "mqtt",
        }
        with patch.object(SatelliteDaemon, "_init_piper_pool"):
            sat = SatelliteDaemon(cfg)
        self.assertFalse(sat._gated)
        self.assertTrue(sat._gate_permits_audio())


class TestGateMessageParsing(unittest.TestCase):
    """``_on_gate_message`` updates state per payload."""

    def test_enable_opens_gate(self) -> None:
        sat = _make_gated_sat()
        expires: float = time.time() + 600
        sat._on_gate_message(_msg(
            sat._gate_topic,
            {"enabled": True, "expires_at": expires},
        ))
        self.assertTrue(sat._gate_open)
        self.assertAlmostEqual(sat._gate_expires, expires, delta=0.01)
        self.assertTrue(sat._gate_permits_audio())

    def test_disable_closes_gate(self) -> None:
        sat = _make_gated_sat()
        sat._gate_open = True
        sat._gate_expires = time.time() + 600
        sat._on_gate_message(_msg(
            sat._gate_topic, {"enabled": False, "expires_at": 0},
        ))
        self.assertFalse(sat._gate_open)
        self.assertEqual(sat._gate_expires, 0.0)

    def test_past_expiry_enable_stays_closed(self) -> None:
        sat = _make_gated_sat()
        sat._on_gate_message(_msg(
            sat._gate_topic,
            {"enabled": True, "expires_at": time.time() - 10},
        ))
        self.assertFalse(sat._gate_open)

    def test_clamped_when_coordinator_oversized(self) -> None:
        """Satellite re-clamps to 2h even if coordinator sends more."""
        sat = _make_gated_sat()
        huge: float = time.time() + 10 * 3600
        sat._on_gate_message(_msg(
            sat._gate_topic,
            {"enabled": True, "expires_at": huge},
        ))
        self.assertTrue(sat._gate_open)
        # The stored expiry must be at most now + MAX.
        self.assertLessEqual(
            sat._gate_expires - time.time(),
            float(C.VOICE_GATE_MAX_SECONDS) + 1.0,
        )

    def test_corrupt_payload_closes_gate(self) -> None:
        sat = _make_gated_sat()
        sat._gate_open = True
        sat._gate_expires = time.time() + 600
        bad = SimpleNamespace(topic=sat._gate_topic, payload=b"not json")
        sat._on_gate_message(bad)
        self.assertFalse(sat._gate_open)


class TestGateAutoExpiry(unittest.TestCase):
    """Auto-expiry flips state and republishes retained closed."""

    def test_expiry_closes_and_publishes(self) -> None:
        sat = _make_gated_sat()
        # Open in the past — first permit check must auto-close.
        sat._gate_open = True
        sat._gate_expires = time.time() - 1
        permitted: bool = sat._gate_permits_audio()
        self.assertFalse(permitted)
        self.assertFalse(sat._gate_open)
        # Republish was retained + closed.
        sat._mqtt_client.publish.assert_called_once()
        args, kwargs = sat._mqtt_client.publish.call_args
        self.assertEqual(args[0], sat._gate_topic)
        payload: dict[str, Any] = json.loads(args[1])
        self.assertFalse(payload["enabled"])
        self.assertTrue(kwargs.get("retain"))

    def test_open_and_live_permits_audio(self) -> None:
        sat = _make_gated_sat()
        sat._gate_open = True
        sat._gate_expires = time.time() + 600
        self.assertTrue(sat._gate_permits_audio())
        sat._mqtt_client.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
