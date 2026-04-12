"""Tests for the GET /api/voice/gates handler.

Exercises :meth:`DiagnosticsHandlerMixin._handle_get_voice_gates`
without starting an HTTP server.  Instantiates the mixin bare,
attaches a mock ``self.server`` with or without ``_mqtt_bridge``,
captures ``_send_json`` calls, and verifies the contract:

- bridge missing    → empty dict
- bridge present    → whatever get_gates() returns
- bridge raises     → empty dict (logged)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import unittest
from typing import Any
from unittest.mock import MagicMock

from handlers.diagnostics import DiagnosticsHandlerMixin


class _HandlerStub(DiagnosticsHandlerMixin):
    """Minimal host for the mixin.

    The mixin's gate handler touches only ``self.server._mqtt_bridge``
    and ``self._send_json``.  Everything else is irrelevant, so we
    stub both.
    """

    def __init__(self, bridge: Any) -> None:
        self.server = MagicMock()
        self.server._mqtt_bridge = bridge
        self.last_status: int = 0
        self.last_body: Any = None

    def _send_json(self, status: int, body: Any) -> None:
        self.last_status = status
        self.last_body = body


class TestVoiceGatesHandler(unittest.TestCase):
    """Handler returns live gate state from the bridge."""

    def test_no_bridge_returns_empty(self) -> None:
        h = _HandlerStub(bridge=None)
        h._handle_get_voice_gates()
        self.assertEqual(h.last_status, 200)
        self.assertEqual(h.last_body, {})

    def test_bridge_without_get_gates_returns_empty(self) -> None:
        """Legacy/partial bridge lacking the new method fails soft."""
        bridge = object()  # no get_gates attr
        h = _HandlerStub(bridge=bridge)
        h._handle_get_voice_gates()
        self.assertEqual(h.last_status, 200)
        self.assertEqual(h.last_body, {})

    def test_returns_live_gates(self) -> None:
        bridge = MagicMock()
        bridge.get_gates.return_value = {
            "doorbell": {"enabled": True, "expires_at": 9999999999.0},
        }
        h = _HandlerStub(bridge=bridge)
        h._handle_get_voice_gates()
        self.assertEqual(h.last_status, 200)
        self.assertIn("doorbell", h.last_body)
        self.assertTrue(h.last_body["doorbell"]["enabled"])

    def test_bridge_exception_returns_empty(self) -> None:
        bridge = MagicMock()
        bridge.get_gates.side_effect = RuntimeError("boom")
        h = _HandlerStub(bridge=bridge)
        h._handle_get_voice_gates()
        self.assertEqual(h.last_status, 200)
        self.assertEqual(h.last_body, {})

    def test_empty_dict_passes_through(self) -> None:
        """No open gates → empty dict, not missing body."""
        bridge = MagicMock()
        bridge.get_gates.return_value = {}
        h = _HandlerStub(bridge=bridge)
        h._handle_get_voice_gates()
        self.assertEqual(h.last_body, {})


if __name__ == "__main__":
    unittest.main()
