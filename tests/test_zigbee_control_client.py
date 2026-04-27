"""Tests for ``zigbee_service.client.ZigbeeControlClient``.

The client is the single hub-side publisher to glowup-zigbee-service.
Every failure mode exercised here is one the production service can
actually produce: successful echo, timed-out echo, unreachable host,
4xx with structured error body, malformed body, non-JSON response.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.

__version__ = "1.0"

import io
import json
import os
import sys
import unittest
import urllib.error
from typing import Any
from unittest.mock import patch, MagicMock

_REPO_ROOT: str = os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."),
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from zigbee_service.client import (
    DEFAULT_TIMEOUT_S,
    CommandResult,
    VALID_STATES,
    ZigbeeControlClient,
)


def _fake_response(body: Any) -> MagicMock:
    """Build an object that mimics ``urllib.request.urlopen`` context."""
    payload: bytes = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body
    resp = MagicMock()
    resp.read.return_value = payload
    ctx = MagicMock()
    ctx.__enter__.return_value = resp
    ctx.__exit__.return_value = False
    return ctx


class ConstructionTests(unittest.TestCase):
    """Basic input validation on __init__."""

    def test_rejects_empty_base_url(self) -> None:
        """Empty base URL is a programmer error, surface it early."""
        with self.assertRaises(ValueError):
            ZigbeeControlClient("")

    def test_strips_trailing_slash(self) -> None:
        """Trailing slash on base_url must be tolerated (caller sloppy)."""
        c = ZigbeeControlClient("http://127.0.0.1:8422/")
        self.assertEqual(c._base, "http://127.0.0.1:8422")

    def test_default_timeout_used(self) -> None:
        """Client picks up the module default when none is supplied."""
        c = ZigbeeControlClient("http://x")
        self.assertEqual(c._timeout, DEFAULT_TIMEOUT_S)


class SetStateTests(unittest.TestCase):
    """Set-state path — including the echoed/not-echoed distinction."""

    def _client(self) -> ZigbeeControlClient:
        return ZigbeeControlClient("http://127.0.0.1:8422")

    def test_rejects_blank_name_without_network(self) -> None:
        """Empty name is a client-side validation error, no HTTP."""
        with patch("urllib.request.urlopen") as m:
            result: CommandResult = self._client().set_state("", "ON")
            m.assert_not_called()
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_rejects_invalid_state_without_network(self) -> None:
        """Arbitrary strings like 'TOGGLE' are caught before the call."""
        with patch("urllib.request.urlopen") as m:
            result = self._client().set_state("LRTV", "TOGGLE")
            m.assert_not_called()
        self.assertFalse(result.ok)
        assert result.error is not None
        self.assertIn("state must be", result.error)

    def test_on_and_off_are_both_valid(self) -> None:
        """ON and OFF are the only accepted power states."""
        self.assertEqual(VALID_STATES, frozenset({"ON", "OFF"}))

    def test_successful_echoed_command(self) -> None:
        """Happy path — service accepts and device echoes within timeout."""
        body: dict[str, Any] = {
            "device": "MBTV", "desired": "ON", "echoed": True,
            "current_state": "ON", "power_w": 12.5,
        }
        with patch("urllib.request.urlopen",
                   return_value=_fake_response(body)) as m:
            result = self._client().set_state("MBTV", "on")
            # Request body must be the normalized uppercase state.
            self.assertEqual(m.call_count, 1)
            req = m.call_args[0][0]
            self.assertEqual(req.method, "POST")
            self.assertIn("/devices/MBTV/state", req.full_url)
            self.assertEqual(
                json.loads(req.data.decode("utf-8")), {"state": "ON"},
            )
        self.assertTrue(result.ok)
        self.assertTrue(result.echoed)
        self.assertEqual(result.state, "ON")
        self.assertEqual(result.power_w, 12.5)
        self.assertIsNone(result.error)

    def test_command_accepted_but_device_did_not_echo(self) -> None:
        """Service 504 shape — command sent, device silent.

        ``ok=True`` (service accepted) but ``echoed=False`` (device
        never acknowledged).  Per Perry's positive-handoff rule, this
        is a distinct outcome — callers must not treat it as success.
        """
        body: dict[str, Any] = {
            "device": "LRTV", "desired": "OFF", "echoed": False,
            "error": "timed out waiting for device to acknowledge",
        }
        # A real service 504 arrives via HTTPError with the body above.
        fp = io.BytesIO(json.dumps(body).encode("utf-8"))
        err = urllib.error.HTTPError(
            "http://127.0.0.1:8422/devices/LRTV/state", 504, "Gateway Timeout",
            {}, fp,  # type: ignore[arg-type]
        )
        with patch("urllib.request.urlopen", side_effect=err):
            result = self._client().set_state("LRTV", "OFF")
        self.assertFalse(result.ok)
        assert result.error is not None
        self.assertIn("acknowledge", result.error)

    def test_host_unreachable(self) -> None:
        """URLError (broker-2 down / network dead) is a hard failure."""
        err = urllib.error.URLError("Connection refused")
        with patch("urllib.request.urlopen", side_effect=err):
            result = self._client().set_state("LRTV", "ON")
        self.assertFalse(result.ok)
        assert result.error is not None
        self.assertIn("unreachable", result.error)

    def test_timeout_is_a_hard_failure(self) -> None:
        """Request-level timeout surfaces as an error, not a crash."""
        with patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
            result = self._client().set_state("LRTV", "ON")
        self.assertFalse(result.ok)
        assert result.error is not None
        self.assertIn("timed out", result.error)

    def test_malformed_response_shape_is_rejected(self) -> None:
        """A 200 with a string body (not a dict) must not pass as success."""
        with patch("urllib.request.urlopen",
                   return_value=_fake_response("oops")):
            result = self._client().set_state("LRTV", "ON")
        self.assertFalse(result.ok)
        assert result.error is not None
        self.assertIn("unexpected response shape", result.error)


class ListDevicesTests(unittest.TestCase):
    """``list_devices`` and its optional type filter."""

    def _client(self) -> ZigbeeControlClient:
        return ZigbeeControlClient("http://127.0.0.1:8422")

    def test_list_all(self) -> None:
        """No filter → request /devices, unwrap envelope to plain list."""
        body: dict[str, Any] = {
            "devices": [
                {"name": "LRTV", "type": "plug"},
                {"name": "SBYRD", "type": "soil"},
            ],
        }
        with patch("urllib.request.urlopen",
                   return_value=_fake_response(body)) as m:
            ok, devices = self._client().list_devices()
            req = m.call_args[0][0]
            self.assertTrue(req.full_url.endswith("/devices"))
        self.assertTrue(ok)
        self.assertEqual(len(devices), 2)

    def test_list_with_type_filter(self) -> None:
        """Type filter appears as ``?type=plug`` in the URL."""
        body: dict[str, Any] = {"devices": [{"name": "LRTV", "type": "plug"}]}
        with patch("urllib.request.urlopen",
                   return_value=_fake_response(body)) as m:
            ok, devices = self._client().list_devices(type_filter="plug")
            req = m.call_args[0][0]
            self.assertIn("type=plug", req.full_url)
        self.assertTrue(ok)
        self.assertEqual(devices[0]["name"], "LRTV")

    def test_missing_devices_envelope_is_an_error(self) -> None:
        """A 200 without ``devices`` key is an upstream contract break."""
        with patch("urllib.request.urlopen",
                   return_value=_fake_response({"unexpected": True})):
            ok, result = self._client().list_devices()
        self.assertFalse(ok)
        self.assertIn("unexpected response shape", str(result))


class GetDeviceTests(unittest.TestCase):
    """Single-device lookup."""

    def _client(self) -> ZigbeeControlClient:
        return ZigbeeControlClient("http://127.0.0.1:8422")

    def test_returns_device_dict(self) -> None:
        """200 body flows through verbatim."""
        body: dict[str, Any] = {"name": "MBTV", "type": "plug", "state": "OFF"}
        with patch("urllib.request.urlopen",
                   return_value=_fake_response(body)):
            ok, dev = self._client().get_device("MBTV")
        self.assertTrue(ok)
        self.assertEqual(dev["name"], "MBTV")

    def test_unknown_device_surfaces_service_error(self) -> None:
        """Service 404 ``{"error": "unknown device: X"}`` propagates."""
        fp = io.BytesIO(json.dumps(
            {"error": "unknown device: NOPE"},
        ).encode("utf-8"))
        err = urllib.error.HTTPError(
            "http://127.0.0.1:8422/devices/NOPE", 404, "Not Found",
            {}, fp,  # type: ignore[arg-type]
        )
        with patch("urllib.request.urlopen", side_effect=err):
            ok, detail = self._client().get_device("NOPE")
        self.assertFalse(ok)
        self.assertIn("unknown device", str(detail))


if __name__ == "__main__":
    unittest.main()
