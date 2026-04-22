"""Tests for ZigbeePlugEmitter — the HTTP-based plug driver.

The emitter is pure stdlib (urllib.request) so we mock at the
``urllib.request.urlopen`` layer.  No broker-2 needed, no paho
dependency, no network.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "0.1"

import io
import json
import unittest
import urllib.error
from typing import Any
from unittest.mock import MagicMock, patch

from emitters import get_registry
from emitters.zigbee_plug import PlugCommandError, ZigbeePlugEmitter


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

# Broker host reused across tests — must not resolve to a real host.
_TEST_BROKER: str = "10.255.255.1"

# Canonical successful POST response shape from zigbee_service.
_OK_BODY_ON: dict[str, Any] = {
    "device": "LRTV", "desired": "ON", "echoed": True,
    "current_state": "ON", "power_w": 0.0,
}
_OK_BODY_OFF: dict[str, Any] = {
    "device": "LRTV", "desired": "OFF", "echoed": True,
    "current_state": "OFF", "power_w": 0.0,
}


def _mock_urlopen_response(body: dict[str, Any]) -> MagicMock:
    """Build a context-manager mock suitable for ``urllib.request.urlopen``.

    ``urlopen`` is normally used as ``with urlopen(...) as resp: resp.read()``.
    The returned mock supports the context-manager protocol and yields a
    ``.read()`` that returns the encoded body.

    Args:
        body: Dict to JSON-encode as the response body.

    Returns:
        MagicMock configured as a context manager.
    """
    resp: MagicMock = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.read.return_value = json.dumps(body).encode("utf-8")
    return resp


def _http_error(code: int, body: dict[str, Any]) -> urllib.error.HTTPError:
    """Construct an ``HTTPError`` that carries a readable JSON body."""
    return urllib.error.HTTPError(
        url=f"http://{_TEST_BROKER}:8422/devices/LRTV/state",
        code=code,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(json.dumps(body).encode("utf-8")),
    )


# ---------------------------------------------------------------------------
# Creation / registration
# ---------------------------------------------------------------------------

class CreationTests(unittest.TestCase):
    """Verify both creation paths produce a usable emitter."""

    def test_registered_as_zigbee_plug(self) -> None:
        """The subclass auto-registers under emitter_type 'zigbee_plug'."""
        self.assertIn("zigbee_plug", get_registry())
        self.assertIs(get_registry()["zigbee_plug"], ZigbeePlugEmitter)

    def test_config_path_sets_fields(self) -> None:
        emitter = ZigbeePlugEmitter("LRTV", {
            "broker": _TEST_BROKER,
            "device_name": "LRTV",
        })
        self.assertEqual(emitter.emitter_type, "zigbee_plug")
        self.assertEqual(emitter.label, "LRTV")
        self.assertEqual(emitter.emitter_id, "LRTV")
        self.assertIsNone(emitter.last_state)

    def test_device_name_defaults_to_instance_name(self) -> None:
        """When 'device_name' is absent config, instance name is used."""
        emitter = ZigbeePlugEmitter("LRTV", {"broker": _TEST_BROKER})
        self.assertEqual(emitter.emitter_id, "LRTV")

    def test_from_plug_factory(self) -> None:
        emitter = ZigbeePlugEmitter.from_plug(
            name="LRTV", broker_host=_TEST_BROKER)
        emitter.on_configure({})
        self.assertEqual(emitter.emitter_id, "LRTV")
        self.assertEqual(emitter._broker_host, _TEST_BROKER)
        self.assertEqual(emitter._http_port, 8422)

    def test_on_configure_requires_broker(self) -> None:
        """Missing broker must fail fast at configure time."""
        emitter = ZigbeePlugEmitter("LRTV", {})
        with self.assertRaises(ValueError):
            emitter.on_configure({})


# ---------------------------------------------------------------------------
# Command path — POST /devices/{name}/state
# ---------------------------------------------------------------------------

class CommandTests(unittest.TestCase):
    """Verify set_power / power_on / power_off correctness."""

    def setUp(self) -> None:
        self.emitter: ZigbeePlugEmitter = ZigbeePlugEmitter("LRTV", {
            "broker": _TEST_BROKER,
            "device_name": "LRTV",
        })
        self.emitter.on_configure({})

    def test_power_on_posts_on_body(self) -> None:
        resp: MagicMock = _mock_urlopen_response(_OK_BODY_ON)
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp) as mock_open:
            self.emitter.power_on()
            mock_open.assert_called_once()
            args, _kwargs = mock_open.call_args
            req = args[0]
            self.assertEqual(req.full_url,
                             f"http://{_TEST_BROKER}:8422"
                             "/devices/LRTV/state")
            self.assertEqual(req.get_method(), "POST")
            self.assertEqual(json.loads(req.data), {"state": "ON"})
        self.assertEqual(self.emitter.last_state, "ON")

    def test_power_off_posts_off_body(self) -> None:
        resp: MagicMock = _mock_urlopen_response(_OK_BODY_OFF)
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp) as mock_open:
            self.emitter.power_off()
            args, _ = mock_open.call_args
            req = args[0]
            self.assertEqual(json.loads(req.data), {"state": "OFF"})
        self.assertEqual(self.emitter.last_state, "OFF")

    def test_idempotent_skip_second_call(self) -> None:
        """Second identical command is suppressed by the cache."""
        resp: MagicMock = _mock_urlopen_response(_OK_BODY_ON)
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp) as mock_open:
            self.emitter.power_on()
            self.emitter.power_on()
            self.assertEqual(mock_open.call_count, 1)

    def test_current_state_from_response_preferred(self) -> None:
        """Cache reflects the device's echoed state, not the request."""
        # Device echoes OFF even though we asked for ON (e.g., physical
        # override in the same instant).  Cache must reflect reality.
        resp: MagicMock = _mock_urlopen_response({
            "echoed": True, "current_state": "OFF",
        })
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp):
            self.emitter.power_on()
        self.assertEqual(self.emitter.last_state, "OFF")

    def test_echo_timeout_raises_and_preserves_cache(self) -> None:
        """504 echo-timeout raises; cache remains None for retry."""
        err: urllib.error.HTTPError = _http_error(504, {
            "device": "LRTV", "desired": "ON", "echoed": False,
            "error": "timed out",
        })
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   side_effect=err):
            with self.assertRaises(PlugCommandError):
                self.emitter.power_on()
        self.assertIsNone(self.emitter.last_state)

    def test_network_error_raises_and_preserves_cache(self) -> None:
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("unreachable")):
            with self.assertRaises(PlugCommandError):
                self.emitter.power_on()
        self.assertIsNone(self.emitter.last_state)

    def test_echoed_false_raises(self) -> None:
        """Service-200 with echoed=False is still a failure."""
        resp: MagicMock = _mock_urlopen_response({
            "echoed": False, "current_state": None,
        })
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp):
            with self.assertRaises(PlugCommandError):
                self.emitter.power_on()

    def test_non_json_response_raises(self) -> None:
        resp: MagicMock = MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.read.return_value = b"<html>gateway</html>"
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp):
            with self.assertRaises(PlugCommandError):
                self.emitter.power_on()

    def test_retry_after_failure(self) -> None:
        """Failure clears nothing but leaves cache open — next call retries."""
        first: urllib.error.URLError = urllib.error.URLError("unreachable")
        second: MagicMock = _mock_urlopen_response(_OK_BODY_ON)
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   side_effect=[first, second]) as mock_open:
            with self.assertRaises(PlugCommandError):
                self.emitter.power_on()
            self.emitter.power_on()
            self.assertEqual(mock_open.call_count, 2)
        self.assertEqual(self.emitter.last_state, "ON")


# ---------------------------------------------------------------------------
# on_emit / capabilities
# ---------------------------------------------------------------------------

class OnEmitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.emitter: ZigbeePlugEmitter = ZigbeePlugEmitter("LRTV", {
            "broker": _TEST_BROKER,
        })
        self.emitter.on_configure({})

    def test_on_emit_accepts_bool_true(self) -> None:
        resp: MagicMock = _mock_urlopen_response(_OK_BODY_ON)
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp):
            ok: bool = self.emitter.on_emit(True, {})
        self.assertTrue(ok)
        self.assertEqual(self.emitter.last_state, "ON")

    def test_on_emit_accepts_bool_false(self) -> None:
        resp: MagicMock = _mock_urlopen_response(_OK_BODY_OFF)
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp):
            ok: bool = self.emitter.on_emit(False, {})
        self.assertTrue(ok)

    def test_on_emit_rejects_hsbk(self) -> None:
        """HSBK frames must be rejected — effects do not drive plugs."""
        ok: bool = self.emitter.on_emit((0, 0, 65535, 3500), {})
        self.assertFalse(ok)

    def test_on_emit_converts_failure_to_false(self) -> None:
        """Transport failures surface as on_emit returning False."""
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("boom")):
            ok: bool = self.emitter.on_emit(True, {})
        self.assertFalse(ok)


class CapabilitiesTests(unittest.TestCase):
    def test_binary_frame_type(self) -> None:
        emitter = ZigbeePlugEmitter("LRTV", {"broker": _TEST_BROKER})
        caps = emitter.capabilities()
        self.assertEqual(caps.accepted_frame_types, ["binary"])
        self.assertEqual(caps.max_rate_hz, 1.0)
        self.assertEqual(caps.extra["device_name"], "LRTV")


# ---------------------------------------------------------------------------
# Query path — GET /devices/{name}
# ---------------------------------------------------------------------------

class QueryStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.emitter: ZigbeePlugEmitter = ZigbeePlugEmitter("LRTV", {
            "broker": _TEST_BROKER,
        })
        self.emitter.on_configure({})

    def test_query_updates_cache(self) -> None:
        resp: MagicMock = _mock_urlopen_response({
            "name": "LRTV", "state": "ON", "power_w": 5.4,
            "online": True, "age_sec": 2.1,
        })
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp):
            data = self.emitter.query_state()
        self.assertEqual(data["state"], "ON")
        self.assertEqual(self.emitter.last_state, "ON")

    def test_query_handles_null_state(self) -> None:
        """An offline device may report state=null — cache stays None."""
        resp: MagicMock = _mock_urlopen_response({
            "name": "LRTV", "state": None, "online": False,
        })
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp):
            data = self.emitter.query_state()
        self.assertIsNone(data["state"])
        self.assertIsNone(self.emitter.last_state)

    def test_query_network_error_raises(self) -> None:
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("nope")):
            with self.assertRaises(PlugCommandError):
                self.emitter.query_state()


if __name__ == "__main__":
    unittest.main()
