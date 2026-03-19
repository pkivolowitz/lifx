#!/usr/bin/env python3
"""Unit tests for the DeviceManager override logic.

Tests the phone-override mechanism used by the scheduler and REST API
to prevent conflicts.  Covers:
  - Basic override set/clear/check
  - Group-level overrides
  - Individual member overrides within groups (the bug fixed in 8a27b45)
  - Override entry tracking for schedule transitions

No network or hardware dependencies — uses a minimal DeviceManager
with mocked devices.
"""

import unittest
from unittest.mock import MagicMock
from typing import Optional

from server import DeviceManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dm(
    device_ips: list[str],
    groups: Optional[dict[str, list[str]]] = None,
) -> DeviceManager:
    """Create a DeviceManager without loading real devices.

    Injects mock LifxDevice objects so the DeviceManager's internal
    dict is populated, but no network I/O occurs.

    Args:
        device_ips: List of fake device IPs.
        groups:     Group config (group name → IP list).

    Returns:
        A populated DeviceManager instance.
    """
    dm = DeviceManager(
        device_ips=device_ips,
        groups=groups or {},
    )
    # Inject mock devices so get_device() works.
    for ip in device_ips:
        mock_dev = MagicMock()
        mock_dev.ip = ip
        mock_dev.mac_str = "00:00:00:00:00:00"
        mock_dev.label = f"Mock {ip}"
        mock_dev.product_name = "Mock Light"
        mock_dev.zone_count = 1
        dm._devices[ip] = mock_dev
    # Inject virtual group devices for multi-IP groups.
    if groups:
        for name, ips in groups.items():
            if len(ips) >= 2:
                group_id = f"group:{name}"
                mock_vdev = MagicMock()
                mock_vdev.ip = group_id
                mock_vdev.mac_str = "virtual"
                mock_vdev.label = name
                mock_vdev.product_name = "Virtual"
                mock_vdev.zone_count = len(ips) * 3
                dm._devices[group_id] = mock_vdev
    return dm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBasicOverride(unittest.TestCase):
    """Tests for basic override set/clear/check."""

    def setUp(self) -> None:
        self.dm = _make_dm(["192.0.2.40", "192.0.2.41"])

    def test_not_overridden_by_default(self) -> None:
        """Devices are not overridden initially."""
        self.assertFalse(self.dm.is_overridden("192.0.2.40"))
        self.assertFalse(self.dm.is_overridden("192.0.2.41"))

    def test_mark_override(self) -> None:
        """mark_override makes is_overridden return True."""
        self.dm.mark_override("192.0.2.40", None)
        self.assertTrue(self.dm.is_overridden("192.0.2.40"))
        self.assertFalse(self.dm.is_overridden("192.0.2.41"))

    def test_clear_override(self) -> None:
        """clear_override reverses mark_override."""
        self.dm.mark_override("192.0.2.40", None)
        self.dm.clear_override("192.0.2.40")
        self.assertFalse(self.dm.is_overridden("192.0.2.40"))

    def test_clear_nonexistent_is_safe(self) -> None:
        """Clearing a device that was never overridden doesn't raise."""
        self.dm.clear_override("192.0.2.40")
        self.assertFalse(self.dm.is_overridden("192.0.2.40"))

    def test_override_entry_tracking(self) -> None:
        """get_override_entry returns the entry name set by mark_override."""
        self.dm.mark_override("192.0.2.40", "evening aurora")
        self.assertEqual(
            self.dm.get_override_entry("192.0.2.40"), "evening aurora",
        )

    def test_override_entry_none(self) -> None:
        """get_override_entry returns None for non-overridden devices."""
        self.assertIsNone(self.dm.get_override_entry("192.0.2.40"))

    def test_override_entry_with_none_name(self) -> None:
        """MQTT/external clients pass None as entry name."""
        self.dm.mark_override("192.0.2.40", None)
        self.assertTrue(self.dm.is_overridden("192.0.2.40"))
        self.assertIsNone(self.dm.get_override_entry("192.0.2.40"))


class TestGroupOverride(unittest.TestCase):
    """Tests for group-level overrides."""

    def setUp(self) -> None:
        self.dm = _make_dm(
            ["192.0.2.10", "192.0.2.20"],
            groups={"porch": ["192.0.2.10", "192.0.2.20"]},
        )

    def test_group_override(self) -> None:
        """Overriding the group ID works."""
        self.dm.mark_override("group:porch", "evening aurora")
        self.assertTrue(self.dm.is_overridden("group:porch"))

    def test_group_override_does_not_affect_members(self) -> None:
        """Overriding the group does not mark individual members."""
        self.dm.mark_override("group:porch", None)
        self.assertFalse(self.dm.is_overridden("192.0.2.10"))
        self.assertFalse(self.dm.is_overridden("192.0.2.20"))

    def test_member_override_does_not_affect_group(self) -> None:
        """Overriding a member does not mark the group (basic check)."""
        self.dm.mark_override("192.0.2.10", None)
        self.assertFalse(self.dm.is_overridden("group:porch"))


class TestGroupOrMemberOverride(unittest.TestCase):
    """Tests for is_overridden_or_member (the group member bug fix)."""

    def setUp(self) -> None:
        self.dm = _make_dm(
            ["192.0.2.10", "192.0.2.20", "192.0.2.30"],
            groups={
                "porch": ["192.0.2.10", "192.0.2.20"],
                "single": ["192.0.2.30"],
            },
        )

    def test_no_override_returns_false(self) -> None:
        """No overrides → is_overridden_or_member is False."""
        self.assertFalse(
            self.dm.is_overridden_or_member("group:porch"),
        )

    def test_group_override_detected(self) -> None:
        """Group-level override is detected by is_overridden_or_member."""
        self.dm.mark_override("group:porch", None)
        self.assertTrue(
            self.dm.is_overridden_or_member("group:porch"),
        )

    def test_member_override_detected_for_group(self) -> None:
        """Overriding one member makes is_overridden_or_member True
        for the whole group.  This is the core bug fix."""
        self.dm.mark_override("192.0.2.10", None)
        self.assertTrue(
            self.dm.is_overridden_or_member("group:porch"),
        )

    def test_other_member_override_detected(self) -> None:
        """Overriding the other member also triggers the group check."""
        self.dm.mark_override("192.0.2.20", None)
        self.assertTrue(
            self.dm.is_overridden_or_member("group:porch"),
        )

    def test_non_member_override_ignored(self) -> None:
        """Overriding a device NOT in the group does not trigger it."""
        self.dm.mark_override("192.0.2.30", None)
        self.assertFalse(
            self.dm.is_overridden_or_member("group:porch"),
        )

    def test_individual_device_passthrough(self) -> None:
        """For individual IPs, is_overridden_or_member matches
        is_overridden exactly."""
        self.assertFalse(
            self.dm.is_overridden_or_member("192.0.2.10"),
        )
        self.dm.mark_override("192.0.2.10", None)
        self.assertTrue(
            self.dm.is_overridden_or_member("192.0.2.10"),
        )

    def test_single_device_group(self) -> None:
        """Single-device groups work correctly (no member expansion)."""
        self.dm.mark_override("192.0.2.30", None)
        # "single" group has only one IP, so the scheduler would use
        # the IP directly, not group:single.  Test both paths.
        self.assertTrue(
            self.dm.is_overridden_or_member("192.0.2.30"),
        )

    def test_clear_member_clears_group_visibility(self) -> None:
        """Clearing the member override makes the group check return False."""
        self.dm.mark_override("192.0.2.10", None)
        self.assertTrue(
            self.dm.is_overridden_or_member("group:porch"),
        )
        self.dm.clear_override("192.0.2.10")
        self.assertFalse(
            self.dm.is_overridden_or_member("group:porch"),
        )

    def test_both_group_and_member_overridden(self) -> None:
        """Both group and member overridden — still True, no double-count."""
        self.dm.mark_override("group:porch", "evening")
        self.dm.mark_override("192.0.2.10", None)
        self.assertTrue(
            self.dm.is_overridden_or_member("group:porch"),
        )
        # Clear group but member still overridden.
        self.dm.clear_override("group:porch")
        self.assertTrue(
            self.dm.is_overridden_or_member("group:porch"),
        )


if __name__ == "__main__":
    unittest.main()
