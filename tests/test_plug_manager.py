"""Tests for PlugManager — config loader and label-keyed control surface."""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "0.1"

import json
import unittest
import urllib.error
from typing import Any
from unittest.mock import MagicMock, patch

from emitters.zigbee_plug import PlugCommandError
from plug_manager import PlugManager


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

_TEST_BROKER: str = "10.255.255.1"

_FULL_CONFIG: dict[str, Any] = {
    "zigbee": {
        "broker": _TEST_BROKER,
        "http_port": 8422,
    },
    "plugs": {
        "devices": {
            "LRTV": {"ieee": "0x4ce175525c6b0000", "room": "Living Room"},
            "MBTV": {"ieee": "0x4ce17552545b0000", "room": "Main Bedroom"},
            "ML_Power": {
                "ieee": "0x4ce17552549a0000", "room": "ML server",
            },
        },
    },
}


def _ok_body(state: str) -> dict[str, Any]:
    """Canonical successful POST-response body from zigbee_service."""
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
# Construction and manifest parsing
# ---------------------------------------------------------------------------

class ConstructionTests(unittest.TestCase):
    def test_loads_all_plugs(self) -> None:
        mgr = PlugManager(_FULL_CONFIG)
        self.assertEqual(mgr.list_labels(), ["LRTV", "MBTV", "ML_Power"])

    def test_empty_config_is_valid(self) -> None:
        """A hub with no plugs section must still construct cleanly."""
        mgr = PlugManager({})
        self.assertEqual(mgr.list_labels(), [])
        self.assertFalse(mgr.has_plug("LRTV"))

    def test_missing_devices_is_valid(self) -> None:
        """zigbee present, plugs absent → no plugs, no error."""
        mgr = PlugManager({"zigbee": {"broker": _TEST_BROKER}})
        self.assertEqual(mgr.list_labels(), [])

    def test_metadata_round_trip(self) -> None:
        mgr = PlugManager(_FULL_CONFIG)
        meta = mgr.get_metadata("LRTV")
        self.assertEqual(meta["ieee"], "0x4ce175525c6b0000")
        self.assertEqual(meta["room"], "Living Room")

    def test_metadata_copy_is_independent(self) -> None:
        """Returned metadata must be a copy — mutation cannot leak back."""
        mgr = PlugManager(_FULL_CONFIG)
        meta = mgr.get_metadata("LRTV")
        meta["room"] = "MUTATED"
        self.assertEqual(mgr.get_metadata("LRTV")["room"], "Living Room")

    def test_unknown_plug_metadata_is_empty(self) -> None:
        mgr = PlugManager(_FULL_CONFIG)
        self.assertEqual(mgr.get_metadata("NOPE"), {})

    def test_skips_non_string_names(self) -> None:
        """Malformed config (integer key from JSON edge cases) is skipped."""
        cfg: dict[str, Any] = {
            "zigbee": {"broker": _TEST_BROKER},
            "plugs": {"devices": {"": {"ieee": "x"}, "LRTV": {}}},
        }
        mgr = PlugManager(cfg)
        self.assertEqual(mgr.list_labels(), ["LRTV"])


# ---------------------------------------------------------------------------
# Control dispatch
# ---------------------------------------------------------------------------

class ControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr: PlugManager = PlugManager(_FULL_CONFIG)

    def test_power_on_dispatches(self) -> None:
        resp = _mock_response(_ok_body("ON"))
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp) as mock_open:
            self.mgr.power_on("LRTV")
            args, _ = mock_open.call_args
            self.assertIn("/devices/LRTV/state", args[0].full_url)
            self.assertEqual(json.loads(args[0].data), {"state": "ON"})

    def test_power_off_dispatches(self) -> None:
        resp = _mock_response(_ok_body("OFF"))
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp):
            self.mgr.power_off("MBTV")

    def test_set_power_dispatches(self) -> None:
        resp = _mock_response(_ok_body("ON"))
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp) as mock_open:
            self.mgr.set_power("ML_Power", on=True)
            args, _ = mock_open.call_args
            self.assertEqual(json.loads(args[0].data), {"state": "ON"})

    def test_unknown_label_raises_keyerror(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            self.mgr.power_on("NOPE")
        # Error message must list known labels to make typos obvious.
        self.assertIn("LRTV", str(ctx.exception))

    def test_transport_error_propagates(self) -> None:
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("down")):
            with self.assertRaises(PlugCommandError):
                self.mgr.power_on("LRTV")


# ---------------------------------------------------------------------------
# Introspection — get_status / query_state / refresh_all
# ---------------------------------------------------------------------------

class IntrospectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr: PlugManager = PlugManager(_FULL_CONFIG)

    def test_get_status_is_cache_only(self) -> None:
        """get_status() must NOT issue HTTP calls."""
        with patch("emitters.zigbee_plug.urllib.request.urlopen") as mock_open:
            status = self.mgr.get_status()
            mock_open.assert_not_called()
        self.assertEqual(status["count"], 3)
        self.assertIn("LRTV", status["plugs"])
        self.assertIsNone(status["plugs"]["LRTV"]["last_state"])

    def test_get_status_reflects_last_command(self) -> None:
        resp = _mock_response(_ok_body("ON"))
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp):
            self.mgr.power_on("LRTV")
        status = self.mgr.get_status()
        self.assertEqual(status["plugs"]["LRTV"]["last_state"], "ON")
        self.assertIsNone(status["plugs"]["MBTV"]["last_state"])

    def test_query_state_roundtrip(self) -> None:
        resp = _mock_response({
            "name": "LRTV", "state": "ON", "power_w": 5.4,
            "online": True, "age_sec": 2.1,
        })
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   return_value=resp):
            data = self.mgr.query_state("LRTV")
        self.assertEqual(data["state"], "ON")

    def test_refresh_all_reports_errors_per_plug(self) -> None:
        """One dead plug must not abort refresh of the others."""
        ok_resp = _mock_response({
            "state": "ON", "power_w": 1.0, "online": True,
        })

        def side_effect(req_or_url: Any, timeout: float = 0.0) -> Any:
            # Deliver the OK response for LRTV and MBTV; blow up for ML_Power.
            # req_or_url is a string URL for GETs.
            url: str = str(req_or_url)
            if "ML_Power" in url:
                raise urllib.error.URLError("unreachable")
            return ok_resp

        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   side_effect=side_effect):
            result = self.mgr.refresh_all()
        self.assertEqual(set(result.keys()), {"LRTV", "MBTV", "ML_Power"})
        self.assertEqual(result["LRTV"]["state"], "ON")
        self.assertEqual(result["MBTV"]["state"], "ON")
        self.assertIn("error", result["ML_Power"])


if __name__ == "__main__":
    unittest.main()
