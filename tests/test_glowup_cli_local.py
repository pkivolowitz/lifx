"""Tests for glowup CLI standalone helpers — local registry + group CRUD.

Covers the BASIC standalone path documented in docs/BASIC.md: a brand-
new user with no GlowUp server reachable can name bulbs, group them,
and address them by label thereafter.  These tests do not touch a real
LIFX bulb (no UDP) and do not require a running server — they exercise
the JSON-file edge of the CLI in isolation.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import glowup as gp


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _TempHomeCase(unittest.TestCase):
    """Each test gets a private ``~/.glowup`` directory.

    The CLI's local-registry module-level paths are patched onto the
    temp dir so tests don't pollute the real ``~/.glowup``.
    """

    def setUp(self) -> None:
        self._tmpdir: tempfile.TemporaryDirectory = tempfile.TemporaryDirectory()
        self.tmp: Path = Path(self._tmpdir.name)
        self._patches: list = [
            patch.object(gp, "_LOCAL_HOME", self.tmp),
            patch.object(gp, "_LOCAL_DEVICES", self.tmp / "devices.json"),
            patch.object(gp, "_LOCAL_GROUPS", self.tmp / "groups.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()


# ---------------------------------------------------------------------------
# _load_local_json / _save_local_json — primitive JSON I/O
# ---------------------------------------------------------------------------


class TestLocalJsonIO(_TempHomeCase):
    """File doesn't exist → empty dict; round-trip preserves content."""

    def test_load_missing_file_returns_empty(self) -> None:
        path: Path = self.tmp / "missing.json"
        self.assertEqual(gp._load_local_json(path), {})

    def test_save_then_load_round_trip(self) -> None:
        path: Path = self.tmp / "x.json"
        gp._save_local_json(path, {"a": 1, "b": [2, 3]})
        self.assertEqual(gp._load_local_json(path), {"a": 1, "b": [2, 3]})

    def test_save_creates_parent_directory(self) -> None:
        target: Path = self.tmp / "subdir" / "x.json"
        gp._save_local_json(target, {"k": "v"})
        self.assertTrue(target.exists())
        self.assertEqual(gp._load_local_json(target), {"k": "v"})

    def test_save_is_atomic_via_temp_file(self) -> None:
        """A successful save leaves no .tmp scratch file behind."""
        target: Path = self.tmp / "x.json"
        gp._save_local_json(target, {"k": "v"})
        self.assertFalse((self.tmp / "x.json.tmp").exists())


# ---------------------------------------------------------------------------
# _resolve_ref_local — label / MAC / IP → IP
# ---------------------------------------------------------------------------


class TestResolveRefLocal(_TempHomeCase):
    """Resolution order: literal IP → MAC lookup → case-insensitive label."""

    def _seed(self, devices: dict) -> None:
        gp._save_local_devices(devices)

    def test_ip_literal_passes_through(self) -> None:
        self._seed({})
        self.assertEqual(
            gp._resolve_ref_local("192.0.2.1"), "192.0.2.1",
        )

    def test_mac_lookup(self) -> None:
        self._seed({
            "d0:73:d5:01:23:ab": {
                "label": "Kitchen Bulb", "ip": "192.0.2.41",
            },
        })
        self.assertEqual(
            gp._resolve_ref_local("d0:73:d5:01:23:ab"), "192.0.2.41",
        )

    def test_label_case_insensitive(self) -> None:
        self._seed({
            "d0:73:d5:01:23:ab": {
                "label": "Kitchen Bulb", "ip": "192.0.2.41",
            },
        })
        for variant in ("Kitchen Bulb", "kitchen bulb", "KITCHEN BULB"):
            with self.subTest(variant=variant):
                self.assertEqual(
                    gp._resolve_ref_local(variant), "192.0.2.41",
                )

    def test_skips_underscore_keys(self) -> None:
        """``_comment`` and other operator notes are never resolved."""
        self._seed({
            "_comment": "a hand-written note",
            "d0:73:d5:01:23:ab": {
                "label": "Kitchen Bulb", "ip": "192.0.2.41",
            },
        })
        self.assertIsNone(gp._resolve_ref_local("_comment"))

    def test_unknown_returns_none(self) -> None:
        self._seed({})
        self.assertIsNone(gp._resolve_ref_local("Nonexistent"))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(gp._resolve_ref_local(""))


# ---------------------------------------------------------------------------
# Group CRUD — _cmd_group_add / _list / _show / _rm via local files
# ---------------------------------------------------------------------------


class TestGroupCrudLocal(_TempHomeCase):
    """Group add/show/rm round-trip through ~/.glowup/groups.json."""

    def setUp(self) -> None:
        super().setUp()
        # Pin _server_url to None so group commands take the local path.
        self._url_patch = patch.object(gp, "_server_url", None)
        self._url_patch.start()

    def tearDown(self) -> None:
        self._url_patch.stop()
        super().tearDown()

    def test_add_creates_group(self) -> None:
        gp._cmd_group_add("bedroom", ["A", "B", "C"])
        groups: dict = gp._load_local_groups()
        self.assertEqual(groups["bedroom"], ["A", "B", "C"])

    def test_add_preserves_order(self) -> None:
        """The leftmost zone of the virtual strip is the first member."""
        gp._cmd_group_add("strip", ["Bulb 3", "Bulb 1", "Bulb 2"])
        groups: dict = gp._load_local_groups()
        self.assertEqual(groups["strip"], ["Bulb 3", "Bulb 1", "Bulb 2"])

    def test_add_overwrites_existing(self) -> None:
        gp._cmd_group_add("bedroom", ["A"])
        gp._cmd_group_add("bedroom", ["X", "Y"])
        groups: dict = gp._load_local_groups()
        self.assertEqual(groups["bedroom"], ["X", "Y"])

    def test_add_rejects_underscore_name(self) -> None:
        """``_``-prefixed names are reserved for operator notes."""
        with self.assertRaises(SystemExit):
            gp._cmd_group_add("_internal", ["A"])

    def test_add_requires_at_least_one_member(self) -> None:
        with self.assertRaises(SystemExit):
            gp._cmd_group_add("bedroom", [])

    def test_rm_deletes_group(self) -> None:
        gp._cmd_group_add("bedroom", ["A"])
        gp._cmd_group_rm("bedroom")
        groups: dict = gp._load_local_groups()
        self.assertNotIn("bedroom", groups)

    def test_rm_unknown_errors(self) -> None:
        with self.assertRaises(SystemExit):
            gp._cmd_group_rm("nonexistent")

    def test_underscore_keys_pass_through_save_round_trip(self) -> None:
        """Hand-written ``_`` notes survive an add/rm cycle."""
        groups_seed: dict = {
            "_note": "porch is the long one",
            "porch": ["P1", "P2"],
        }
        gp._save_local_groups(groups_seed)
        gp._cmd_group_add("kitchen", ["K1"])
        groups: dict = gp._load_local_groups()
        self.assertEqual(groups.get("_note"), "porch is the long one")
        self.assertEqual(groups.get("porch"), ["P1", "P2"])
        self.assertEqual(groups.get("kitchen"), ["K1"])


if __name__ == "__main__":
    unittest.main()
