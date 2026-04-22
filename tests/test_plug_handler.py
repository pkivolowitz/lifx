"""Tests for the ``/api/plugs`` handler mixin.

Unit-level: exercises the mixin with stubbed request-handler helpers
(``_send_json``, ``_read_json_body``).  No HTTP server, no TCP sockets
— the mixin's contract is "call manager methods, translate exceptions
to status codes, send JSON", and that is what these tests check.

For full end-to-end route dispatch see tests/test_rest_integration.py.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "0.1"

import json
import unittest
import urllib.error
from typing import Any, Optional
from unittest.mock import MagicMock, patch

from handlers.plug import PlugHandlerMixin
from plug_manager import PlugManager


_TEST_BROKER: str = "10.255.255.1"

_FULL_CONFIG: dict[str, Any] = {
    "zigbee": {"broker": _TEST_BROKER, "http_port": 8422},
    "plugs": {
        "devices": {
            "LRTV": {"ieee": "0x4ce175525c6b0000", "room": "Living Room"},
            "MBTV": {"ieee": "0x4ce17552545b0000", "room": "Main Bedroom"},
        },
    },
}


class _FakeHandler(PlugHandlerMixin):
    """Stand-in for ``GlowUpRequestHandler`` carrying the PlugHandlerMixin.

    Replaces the HTTP-plumbing helpers (``_send_json``,
    ``_read_json_body``) with in-memory captures so a test can inspect
    both without standing up a real server.
    """

    def __init__(self, plug_manager: Optional[PlugManager],
                 body: Optional[dict[str, Any]] = None) -> None:
        self.plug_manager: Optional[PlugManager] = plug_manager
        self._body: Optional[dict[str, Any]] = body
        self.responses: list[tuple[int, dict[str, Any]]] = []

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        """Record the JSON response instead of writing to a socket."""
        self.responses.append((status, body))

    def _read_json_body(self) -> Optional[dict[str, Any]]:
        """Return the pre-seeded body.  Mirrors server-side semantics:

        The real helper sends its own 400 response and returns ``None``
        on malformed bodies.  Tests that want to simulate that path
        pass ``body=None`` and pre-populate ``self.responses`` with
        the expected 400.
        """
        return self._body


def _ok_body(state: str) -> dict[str, Any]:
    """Canonical zigbee_service POST-response body for successful echo."""
    return {
        "device": "X", "desired": state, "echoed": True,
        "current_state": state, "power_w": 0.0,
    }


def _mock_response(body: dict[str, Any]) -> MagicMock:
    """Context-manager mock for ``urllib.request.urlopen``."""
    resp: MagicMock = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.read.return_value = json.dumps(body).encode("utf-8")
    return resp


# ---------------------------------------------------------------------------
# GET /api/plugs
# ---------------------------------------------------------------------------

class GetPlugsTests(unittest.TestCase):
    def test_lists_configured_plugs(self) -> None:
        mgr: PlugManager = PlugManager(_FULL_CONFIG)
        h: _FakeHandler = _FakeHandler(mgr)
        h._handle_get_plugs()
        self.assertEqual(len(h.responses), 1)
        status, body = h.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 2)
        self.assertIn("LRTV", body["plugs"])
        self.assertIn("MBTV", body["plugs"])

    def test_no_manager_returns_empty(self) -> None:
        """Hub without any Zigbee plugs → 200 with empty manifest."""
        h: _FakeHandler = _FakeHandler(plug_manager=None)
        h._handle_get_plugs()
        status, body = h.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(body, {"plugs": {}, "count": 0})

    def test_empty_config_returns_empty(self) -> None:
        """Manager configured with no plugs → 200 with empty manifest."""
        mgr: PlugManager = PlugManager({})
        h: _FakeHandler = _FakeHandler(mgr)
        h._handle_get_plugs()
        status, body = h.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 0)


# ---------------------------------------------------------------------------
# POST /api/plugs/{label}/power
# ---------------------------------------------------------------------------

class PostPlugPowerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr: PlugManager = PlugManager(_FULL_CONFIG)

    def test_power_on_dispatches_to_manager(self) -> None:
        resp: MagicMock = _mock_response(_ok_body("ON"))
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp):
            h: _FakeHandler = _FakeHandler(self.mgr, body={"on": True})
            h._handle_post_plug_power("LRTV")
        status, body = h.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(body["label"], "LRTV")
        self.assertEqual(body["desired"], "ON")
        self.assertEqual(body["current_state"], "ON")

    def test_power_off_dispatches_to_manager(self) -> None:
        resp: MagicMock = _mock_response(_ok_body("OFF"))
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp):
            h: _FakeHandler = _FakeHandler(self.mgr, body={"on": False})
            h._handle_post_plug_power("MBTV")
        status, body = h.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(body["desired"], "OFF")

    def test_unknown_label_returns_404(self) -> None:
        h: _FakeHandler = _FakeHandler(self.mgr, body={"on": True})
        h._handle_post_plug_power("NOPE")
        status, body = h.responses[0]
        self.assertEqual(status, 404)
        self.assertIn("known_plugs", body)
        self.assertIn("LRTV", body["known_plugs"])

    def test_missing_on_field_returns_400(self) -> None:
        h: _FakeHandler = _FakeHandler(self.mgr, body={})
        h._handle_post_plug_power("LRTV")
        status, body = h.responses[0]
        self.assertEqual(status, 400)
        self.assertIn("boolean", body["error"])

    def test_non_bool_on_field_returns_400(self) -> None:
        h: _FakeHandler = _FakeHandler(self.mgr, body={"on": "yes"})
        h._handle_post_plug_power("LRTV")
        status, body = h.responses[0]
        self.assertEqual(status, 400)

    def test_broker_failure_returns_502(self) -> None:
        """Upstream broker error → 502 Bad Gateway (not 500)."""
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("unreachable")):
            h: _FakeHandler = _FakeHandler(self.mgr, body={"on": True})
            h._handle_post_plug_power("LRTV")
        status, body = h.responses[0]
        self.assertEqual(status, 502)
        self.assertEqual(body["label"], "LRTV")
        self.assertEqual(body["desired"], "ON")

    def test_no_manager_returns_503(self) -> None:
        """Unconfigured hub → 503 Service Unavailable."""
        h: _FakeHandler = _FakeHandler(plug_manager=None, body={"on": True})
        h._handle_post_plug_power("LRTV")
        status, body = h.responses[0]
        self.assertEqual(status, 503)


# ---------------------------------------------------------------------------
# POST /api/plugs/refresh
# ---------------------------------------------------------------------------

class PostPlugsRefreshTests(unittest.TestCase):
    def test_refresh_returns_per_plug_results(self) -> None:
        mgr: PlugManager = PlugManager(_FULL_CONFIG)
        resp: MagicMock = _mock_response({
            "state": "ON", "power_w": 1.0, "online": True,
        })
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp):
            h: _FakeHandler = _FakeHandler(mgr)
            h._handle_post_plugs_refresh()
        status, body = h.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 2)
        self.assertEqual(set(body["refreshed"].keys()), {"LRTV", "MBTV"})

    def test_refresh_no_manager_returns_empty(self) -> None:
        h: _FakeHandler = _FakeHandler(plug_manager=None)
        h._handle_post_plugs_refresh()
        status, body = h.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(body, {"refreshed": {}, "count": 0})


if __name__ == "__main__":
    unittest.main()
