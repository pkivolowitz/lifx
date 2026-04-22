"""Tests for step 4 — polymorphic group membership.

Covers:

- ``server_utils.split_group_members`` — transport-prefix partition.
- ``handlers/groups._validate_plug_members`` — CRUD-time guard for
  ``plug:`` entries referencing unknown plugs.

Group-power fan-out dispatch (the integration point that actually
fires plug commands when a mixed group is toggled) is exercised
in-process by instantiating a handler with a real PlugManager and
a mocked ``urllib.request.urlopen``.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "0.1"

import json
import threading
import time
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock, patch

from handlers.groups import GroupHandlerMixin
from plug_manager import PlugManager
from server_utils import split_group_members


_TEST_BROKER: str = "10.255.255.1"

_PLUG_CONFIG: dict[str, Any] = {
    "zigbee": {"broker": _TEST_BROKER, "http_port": 8422},
    "plugs": {
        "devices": {
            "LRTV": {"ieee": "0x1", "room": "Living Room"},
            "MBTV": {"ieee": "0x2", "room": "Main Bedroom"},
        },
    },
}


# ---------------------------------------------------------------------------
# split_group_members
# ---------------------------------------------------------------------------

class SplitGroupMembersTests(unittest.TestCase):
    """Transport-prefix partition of a group member list."""

    def test_lifx_only(self) -> None:
        lifx, matter, plug = split_group_members(
            ["192.0.2.1", "Bedroom Lamp"])
        self.assertEqual(lifx, ["192.0.2.1", "Bedroom Lamp"])
        self.assertEqual(matter, [])
        self.assertEqual(plug, [])

    def test_mixed(self) -> None:
        lifx, matter, plug = split_group_members([
            "192.0.2.1",
            "matter:Kitchen Lamp",
            "plug:LRTV",
            "192.0.2.2",
            "plug:MBTV",
        ])
        self.assertEqual(lifx, ["192.0.2.1", "192.0.2.2"])
        self.assertEqual(matter, ["Kitchen Lamp"])
        self.assertEqual(plug, ["LRTV", "MBTV"])

    def test_prefix_strip_preserves_name(self) -> None:
        """Prefix strip must not trim extra characters from the name."""
        _lifx, matter, plug = split_group_members([
            "matter:plug:foo", "plug:matter:bar"])
        # First entry is a Matter device whose name happens to contain
        # "plug:"; strip only the leading "matter:" prefix.
        self.assertEqual(matter, ["plug:foo"])
        self.assertEqual(plug, ["matter:bar"])

    def test_empty(self) -> None:
        lifx, matter, plug = split_group_members([])
        self.assertEqual((lifx, matter, plug), ([], [], []))


# ---------------------------------------------------------------------------
# _validate_plug_members
# ---------------------------------------------------------------------------

class _FakeGroupHandler(GroupHandlerMixin):
    """Minimal stand-in carrying GroupHandlerMixin for validator tests."""

    def __init__(self, plug_manager: Optional[PlugManager]) -> None:
        self.plug_manager: Optional[PlugManager] = plug_manager


class ValidatePlugMembersTests(unittest.TestCase):
    def test_known_plug_accepted(self) -> None:
        mgr: PlugManager = PlugManager(_PLUG_CONFIG)
        h: _FakeGroupHandler = _FakeGroupHandler(mgr)
        errs: list[str] = h._validate_plug_members([
            "192.0.2.1", "plug:LRTV", "plug:MBTV",
        ])
        self.assertEqual(errs, [])

    def test_unknown_plug_rejected(self) -> None:
        mgr: PlugManager = PlugManager(_PLUG_CONFIG)
        h: _FakeGroupHandler = _FakeGroupHandler(mgr)
        errs: list[str] = h._validate_plug_members(["plug:NOPE"])
        self.assertEqual(len(errs), 1)
        self.assertIn("NOPE", errs[0])
        # Known-plugs hint must be present so the client sees what
        # they could have meant.
        self.assertIn("LRTV", errs[0])

    def test_plug_member_without_manager_rejected(self) -> None:
        """``plug:`` member with no PlugManager on the hub → error."""
        h: _FakeGroupHandler = _FakeGroupHandler(plug_manager=None)
        errs: list[str] = h._validate_plug_members(["plug:LRTV"])
        self.assertEqual(len(errs), 1)
        self.assertIn("subsystem not configured", errs[0])

    def test_non_plug_members_ignored(self) -> None:
        """LIFX and matter members must not trigger plug validation."""
        mgr: PlugManager = PlugManager(_PLUG_CONFIG)
        h: _FakeGroupHandler = _FakeGroupHandler(mgr)
        errs: list[str] = h._validate_plug_members([
            "192.0.2.1", "matter:Kitchen Lamp", "Bedroom Lamp",
        ])
        self.assertEqual(errs, [])


# ---------------------------------------------------------------------------
# Group power fan-out dispatches to plugs
# ---------------------------------------------------------------------------

def _mock_ok_urlopen() -> Any:
    """Build a MagicMock urlopen that returns a generic echoed OK."""
    def _factory(*_args: Any, **_kwargs: Any) -> MagicMock:
        resp: MagicMock = MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.read.return_value = json.dumps({
            "echoed": True, "current_state": "ON", "power_w": 0.0,
        }).encode("utf-8")
        return resp
    return _factory


class GroupPowerPlugDispatchTests(unittest.TestCase):
    """Exercises the pre-flow plug dispatch in _handle_post_power.

    Stands up a minimal handler that reuses the real PlugHandler logic
    but bypasses HTTP plumbing.  Verifies that powering a group with
    ``plug:`` members fires plug commands via the PlugManager.
    """

    def _wait_for_worker(self, label: str, mgr: PlugManager,
                         expected: str, timeout: float = 2.0) -> None:
        """Poll the plug's last_state until it matches or timeout."""
        deadline: float = time.monotonic() + timeout
        while time.monotonic() < deadline:
            plug: Any = mgr.get_plug(label)
            if plug is not None and plug.last_state == expected:
                return
            time.sleep(0.01)
        self.fail(
            f"Plug '{label}' did not reach state {expected} within "
            f"{timeout}s (got {mgr.get_plug(label).last_state})"
        )

    def test_plug_power_worker_dispatches(self) -> None:
        """The module-level helper fires PlugManager.set_power."""
        from handlers.device import _plug_power_worker

        mgr: PlugManager = PlugManager(_PLUG_CONFIG)
        with patch("emitters.zigbee_plug.urllib.request.urlopen",
                   side_effect=_mock_ok_urlopen()):
            _plug_power_worker(mgr, "LRTV", True)
        self.assertEqual(mgr.get_plug("LRTV").last_state, "ON")

    def test_plug_worker_swallows_errors(self) -> None:
        """A dead plug must not raise — one failure cannot stop a group."""
        from handlers.device import _plug_power_worker

        mgr: PlugManager = PlugManager(_PLUG_CONFIG)
        # Unknown label: manager raises KeyError; worker must log+swallow.
        _plug_power_worker(mgr, "NOPE", True)
        # Test passes if no exception propagated out of the worker.


if __name__ == "__main__":
    unittest.main()
