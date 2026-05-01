"""Tests for ``DeviceRegistry`` sub-device support (6a-mid).

Covers schema round-trip, label uniqueness across the parent + sub-
device namespace, force-evict semantics, and the parent-removal
cascade that drops sub-device labels too.

Uses a temp directory for the registry file — never touches
``/etc/glowup/device_registry.json``.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import os
import tempfile
import unittest
from pathlib import Path

from device_registry import DeviceRegistry


PARENT_MAC: str = "d0:73:d5:69:70:db"
PARENT_LABEL: str = "Living Room Ceiling"
SUB_COMP: str = "uplight"
SUB_LABEL: str = "Living Room Ceiling Uplight"


class _RegistryTempCase(unittest.TestCase):
    """Common setup — fresh registry pointing at a temp file."""

    def setUp(self) -> None:
        self._tmpdir: tempfile.TemporaryDirectory = tempfile.TemporaryDirectory()
        self.path: str = os.path.join(self._tmpdir.name, "registry.json")
        self.reg: DeviceRegistry = DeviceRegistry()
        self.reg.path = self.path
        # Pre-register a parent so sub-device tests have something to attach to.
        self.reg.add_device(PARENT_MAC, PARENT_LABEL)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()


class TestAddSubdevice(_RegistryTempCase):
    """``add_subdevice`` — happy paths and validation."""

    def test_basic_add(self) -> None:
        """Sub-device shows up in mac_subdevices and label resolves."""
        self.reg.add_subdevice(PARENT_MAC, SUB_COMP, SUB_LABEL)
        subs = self.reg.mac_subdevices(PARENT_MAC)
        self.assertIn(SUB_COMP, subs)
        self.assertEqual(subs[SUB_COMP]["label"], SUB_LABEL)
        self.assertEqual(
            self.reg.subdevice_label_to_address(SUB_LABEL),
            (PARENT_MAC, SUB_COMP),
        )

    def test_add_with_notes(self) -> None:
        """Notes are stored when provided."""
        self.reg.add_subdevice(
            PARENT_MAC, SUB_COMP, SUB_LABEL, notes="14 cells",
        )
        self.assertEqual(
            self.reg.mac_subdevices(PARENT_MAC)[SUB_COMP]["notes"],
            "14 cells",
        )

    def test_unknown_parent_rejected(self) -> None:
        """Adding under a parent that isn't registered → ValueError."""
        with self.assertRaises(ValueError):
            self.reg.add_subdevice(
                "d0:73:d5:00:00:00", SUB_COMP, SUB_LABEL,
            )

    def test_empty_component_id_rejected(self) -> None:
        """Empty / whitespace component_id → ValueError."""
        with self.assertRaises(ValueError):
            self.reg.add_subdevice(PARENT_MAC, "  ", SUB_LABEL)

    def test_empty_label_rejected(self) -> None:
        """Empty label → ValueError."""
        with self.assertRaises(ValueError):
            self.reg.add_subdevice(PARENT_MAC, SUB_COMP, "  ")

    def test_label_collides_with_parent(self) -> None:
        """Sub-device label can't shadow a parent label."""
        with self.assertRaises(ValueError) as cm:
            self.reg.add_subdevice(PARENT_MAC, SUB_COMP, PARENT_LABEL)
        self.assertIn("parent label", str(cm.exception))

    def test_label_collides_with_other_subdevice(self) -> None:
        """Two sub-devices can't share a label."""
        other_parent: str = "d0:73:d5:aa:bb:cc"
        self.reg.add_device(other_parent, "Other Parent")
        self.reg.add_subdevice(PARENT_MAC, SUB_COMP, SUB_LABEL)
        with self.assertRaises(ValueError) as cm:
            self.reg.add_subdevice(other_parent, "uplight2", SUB_LABEL)
        self.assertIn("sub-device label", str(cm.exception))

    def test_relabel_same_subdevice(self) -> None:
        """Re-adding (same parent, same component_id) updates the label."""
        self.reg.add_subdevice(PARENT_MAC, SUB_COMP, SUB_LABEL)
        new_label: str = "LR Ceiling Up"
        self.reg.add_subdevice(PARENT_MAC, SUB_COMP, new_label)
        # Old label gone from the index, new label in.
        self.assertIsNone(self.reg.subdevice_label_to_address(SUB_LABEL))
        self.assertEqual(
            self.reg.subdevice_label_to_address(new_label),
            (PARENT_MAC, SUB_COMP),
        )

    def test_force_steals_label_from_other_subdevice(self) -> None:
        """``force=True`` reassigns a sub-device label across owners."""
        other_parent: str = "d0:73:d5:aa:bb:cc"
        self.reg.add_device(other_parent, "Other Parent")
        self.reg.add_subdevice(other_parent, "uplight", SUB_LABEL)
        self.reg.add_subdevice(
            PARENT_MAC, SUB_COMP, SUB_LABEL, force=True,
        )
        self.assertEqual(
            self.reg.subdevice_label_to_address(SUB_LABEL),
            (PARENT_MAC, SUB_COMP),
        )
        # Original owner's sub-device entry is gone.
        self.assertEqual(self.reg.mac_subdevices(other_parent), {})


class TestRemoveSubdevice(_RegistryTempCase):
    """``remove_subdevice`` — single-entry removal, parent untouched."""

    def setUp(self) -> None:
        super().setUp()
        self.reg.add_subdevice(PARENT_MAC, SUB_COMP, SUB_LABEL)

    def test_remove_subdevice_returns_true(self) -> None:
        self.assertTrue(self.reg.remove_subdevice(PARENT_MAC, SUB_COMP))
        self.assertEqual(self.reg.mac_subdevices(PARENT_MAC), {})
        self.assertIsNone(self.reg.subdevice_label_to_address(SUB_LABEL))

    def test_remove_unknown_returns_false(self) -> None:
        self.assertFalse(self.reg.remove_subdevice(PARENT_MAC, "nope"))
        self.assertFalse(
            self.reg.remove_subdevice("d0:73:d5:00:00:00", SUB_COMP)
        )

    def test_remove_does_not_drop_parent(self) -> None:
        """Parent device must survive sub-device removal."""
        self.reg.remove_subdevice(PARENT_MAC, SUB_COMP)
        self.assertEqual(self.reg.mac_to_label(PARENT_MAC), PARENT_LABEL)


class TestParentRemovalCascade(_RegistryTempCase):
    """Removing a parent drops every sub-device label belonging to it."""

    def test_cascade(self) -> None:
        self.reg.add_subdevice(PARENT_MAC, SUB_COMP, SUB_LABEL)
        self.reg.add_subdevice(
            PARENT_MAC, "downlight", "Living Room Ceiling Downlight",
        )
        self.assertTrue(self.reg.remove_device(PARENT_MAC))
        self.assertIsNone(self.reg.subdevice_label_to_address(SUB_LABEL))
        self.assertIsNone(
            self.reg.subdevice_label_to_address(
                "Living Room Ceiling Downlight",
            )
        )

    def test_remove_by_parent_label_also_cascades(self) -> None:
        """Removing the parent by label drops sub-devices too."""
        self.reg.add_subdevice(PARENT_MAC, SUB_COMP, SUB_LABEL)
        self.assertTrue(self.reg.remove_device(PARENT_LABEL))
        self.assertIsNone(self.reg.subdevice_label_to_address(SUB_LABEL))


class TestPersistence(_RegistryTempCase):
    """save → load round-trips sub-devices with full validation."""

    def test_round_trip(self) -> None:
        self.reg.add_subdevice(
            PARENT_MAC, SUB_COMP, SUB_LABEL, notes="rim ring",
        )
        self.reg.save()

        # Re-load from disk into a fresh registry.
        fresh: DeviceRegistry = DeviceRegistry()
        self.assertTrue(fresh.load(self.path))
        self.assertEqual(
            fresh.subdevice_label_to_address(SUB_LABEL),
            (PARENT_MAC, SUB_COMP),
        )
        self.assertEqual(
            fresh.mac_subdevices(PARENT_MAC)[SUB_COMP]["notes"], "rim ring",
        )

    def test_load_rejects_collision_in_file(self) -> None:
        """A hand-edited file with duplicate sub-device labels → ValueError."""
        bad: dict = {
            "devices": {
                PARENT_MAC: {
                    "label": PARENT_LABEL,
                    "subdevices": {
                        "uplight": {"label": "X"},
                        "downlight": {"label": "X"},  # duplicate
                    },
                }
            }
        }
        Path(self.path).write_text(json.dumps(bad))
        fresh: DeviceRegistry = DeviceRegistry()
        with self.assertRaises(ValueError):
            fresh.load(self.path)

    def test_load_rejects_subdevice_shadowing_parent_label(self) -> None:
        """A sub-device label that matches a parent label → ValueError on load."""
        bad: dict = {
            "devices": {
                PARENT_MAC: {
                    "label": PARENT_LABEL,
                    "subdevices": {
                        "uplight": {"label": PARENT_LABEL},
                    },
                }
            }
        }
        Path(self.path).write_text(json.dumps(bad))
        fresh: DeviceRegistry = DeviceRegistry()
        with self.assertRaises(ValueError):
            fresh.load(self.path)

    def test_label_to_mac_does_not_resolve_subdevice(self) -> None:
        """Sub-device labels must NOT be returned by parent label_to_mac."""
        self.reg.add_subdevice(PARENT_MAC, SUB_COMP, SUB_LABEL)
        self.assertIsNone(self.reg.label_to_mac(SUB_LABEL))
        self.assertEqual(self.reg.label_to_mac(PARENT_LABEL), PARENT_MAC)


if __name__ == "__main__":
    unittest.main()
