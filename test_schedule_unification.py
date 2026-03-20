#!/usr/bin/env python3
"""Tests for the unified schedule configuration system.

Validates that both the server and standalone scheduler can read a
single ``schedule.json`` file, and that device identifiers (labels,
MACs, and IPs) are resolved correctly.

Test categories:
- Server schedule_file merge: server.json references schedule.json
- Scheduler label/MAC resolution: identifiers resolved via registry + ARP
- Round-trip: same schedule.json works for both consumers
- Backward compatibility: raw IPs still work without registry

Run::

    python3 -m unittest test_schedule_unification -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import os
import tempfile
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimal server.json content (no schedule, references external file).
MINIMAL_SERVER_CONFIG: dict[str, Any] = {
    "auth_token": "test-token-for-unit-tests-only-000",
    "port": 8420,
    "groups": {
        "all": ["10.0.0.1"],
    },
}

# Minimal schedule.json with label-based groups.
SCHEDULE_CONFIG: dict[str, Any] = {
    "location": {
        "latitude": 30.6954,
        "longitude": -88.0399,
    },
    "groups": {
        "porch": ["Porch Front", "PORCH STRING LIGHTS"],
    },
    "schedule": [
        {
            "name": "porch evening aurora",
            "group": "porch",
            "start": "sunset-30m",
            "stop": "23:00",
            "effect": "aurora",
            "params": {"speed": 10.0},
        },
    ],
}

# Schedule using raw IPs (backward compat).
IP_SCHEDULE_CONFIG: dict[str, Any] = {
    "location": {
        "latitude": 30.6954,
        "longitude": -88.0399,
    },
    "groups": {
        "porch": ["10.0.0.35", "10.0.0.45"],
    },
    "schedule": [
        {
            "name": "test entry",
            "group": "porch",
            "start": "18:00",
            "stop": "23:00",
            "effect": "breathe",
        },
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(data: dict, suffix: str = ".json") -> str:
    """Write a dict to a temp JSON file and return the path."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    return path


# ===================================================================
# 1. Server schedule_file merge
# ===================================================================

class TestServerScheduleFileMerge(unittest.TestCase):
    """Verify server _load_config merges an external schedule file."""

    def test_schedule_file_loads_entries(self) -> None:
        """schedule_file imports schedule entries into server config."""
        from server import _load_config

        sched_path: str = _write_json(SCHEDULE_CONFIG)
        server_cfg: dict = dict(MINIMAL_SERVER_CONFIG)
        server_cfg["schedule_file"] = sched_path
        server_path: str = _write_json(server_cfg)

        try:
            config: dict = _load_config(server_path)
            self.assertIn("schedule", config)
            self.assertEqual(len(config["schedule"]), 1)
            self.assertEqual(
                config["schedule"][0]["name"], "porch evening aurora",
            )
        finally:
            os.unlink(server_path)
            os.unlink(sched_path)

    def test_schedule_file_imports_location(self) -> None:
        """schedule_file imports location when server.json lacks it."""
        from server import _load_config

        sched_path: str = _write_json(SCHEDULE_CONFIG)
        server_cfg: dict = dict(MINIMAL_SERVER_CONFIG)
        server_cfg["schedule_file"] = sched_path
        server_path: str = _write_json(server_cfg)

        try:
            config: dict = _load_config(server_path)
            self.assertIn("location", config)
            self.assertAlmostEqual(
                config["location"]["latitude"], 30.6954,
            )
        finally:
            os.unlink(server_path)
            os.unlink(sched_path)

    def test_server_location_takes_precedence(self) -> None:
        """If server.json has location, schedule_file doesn't override."""
        from server import _load_config

        sched_path: str = _write_json(SCHEDULE_CONFIG)
        server_cfg: dict = dict(MINIMAL_SERVER_CONFIG)
        server_cfg["schedule_file"] = sched_path
        server_cfg["location"] = {"latitude": 43.0, "longitude": -89.0}
        server_path: str = _write_json(server_cfg)

        try:
            config: dict = _load_config(server_path)
            self.assertAlmostEqual(config["location"]["latitude"], 43.0)
        finally:
            os.unlink(server_path)
            os.unlink(sched_path)

    def test_schedule_file_merges_groups(self) -> None:
        """Groups from schedule file are merged with server groups."""
        from server import _load_config

        sched_path: str = _write_json(SCHEDULE_CONFIG)
        server_cfg: dict = dict(MINIMAL_SERVER_CONFIG)
        server_cfg["schedule_file"] = sched_path
        server_path: str = _write_json(server_cfg)

        try:
            config: dict = _load_config(server_path)
            # "all" from server + "porch" from schedule.
            self.assertIn("all", config["groups"])
            self.assertIn("porch", config["groups"])
        finally:
            os.unlink(server_path)
            os.unlink(sched_path)

    def test_schedule_file_no_group_duplicates(self) -> None:
        """Merging groups doesn't create duplicate device entries."""
        from server import _load_config

        sched_path: str = _write_json(SCHEDULE_CONFIG)
        server_cfg: dict = dict(MINIMAL_SERVER_CONFIG)
        server_cfg["schedule_file"] = sched_path
        # Server already has porch with one device.
        server_cfg["groups"]["porch"] = ["Porch Front"]
        server_path: str = _write_json(server_cfg)

        try:
            config: dict = _load_config(server_path)
            porch: list = config["groups"]["porch"]
            # "Porch Front" should appear only once.
            self.assertEqual(porch.count("Porch Front"), 1)
            # "PORCH STRING LIGHTS" should have been merged in.
            self.assertIn("PORCH STRING LIGHTS", porch)
        finally:
            os.unlink(server_path)
            os.unlink(sched_path)

    def test_missing_schedule_file_raises(self) -> None:
        """Referencing a nonexistent schedule file raises FileNotFoundError."""
        from server import _load_config

        server_cfg: dict = dict(MINIMAL_SERVER_CONFIG)
        server_cfg["schedule_file"] = "/nonexistent/schedule.json"
        server_path: str = _write_json(server_cfg)

        try:
            with self.assertRaises(FileNotFoundError):
                _load_config(server_path)
        finally:
            os.unlink(server_path)

    def test_no_schedule_file_works_normally(self) -> None:
        """Without schedule_file, config loads as before."""
        from server import _load_config

        server_path: str = _write_json(MINIMAL_SERVER_CONFIG)
        try:
            config: dict = _load_config(server_path)
            self.assertNotIn("schedule", config)
            self.assertIn("all", config["groups"])
        finally:
            os.unlink(server_path)


# ===================================================================
# 2. Standalone scheduler — label/MAC resolution
# ===================================================================

class TestSchedulerResolution(unittest.TestCase):
    """Verify scheduler resolves labels and MACs to IPs."""

    def test_ip_passes_through(self) -> None:
        """Raw IP addresses pass through unchanged."""
        from scheduler import _resolve_identifier
        ip: Optional[str] = _resolve_identifier(
            "10.0.0.35", None, {},
        )
        self.assertEqual(ip, "10.0.0.35")

    def test_mac_resolves_via_arp(self) -> None:
        """MAC address resolves to IP via ARP table."""
        from scheduler import _resolve_identifier
        mac_to_ip: dict = {"d0:73:d5:6b:be:3d": "10.0.0.35"}
        ip: Optional[str] = _resolve_identifier(
            "d0:73:d5:6b:be:3d", None, mac_to_ip,
        )
        self.assertEqual(ip, "10.0.0.35")

    def test_label_resolves_via_registry(self) -> None:
        """Label resolves to IP via registry → MAC → ARP chain."""
        from scheduler import _resolve_identifier
        mock_registry: MagicMock = MagicMock()
        mock_registry.label_to_mac.return_value = "d0:73:d5:6b:be:3d"
        mac_to_ip: dict = {"d0:73:d5:6b:be:3d": "10.0.0.35"}

        ip: Optional[str] = _resolve_identifier(
            "Porch Front", mock_registry, mac_to_ip,
        )
        self.assertEqual(ip, "10.0.0.35")

    def test_unknown_label_returns_none(self) -> None:
        """Unknown label returns None."""
        from scheduler import _resolve_identifier
        mock_registry: MagicMock = MagicMock()
        mock_registry.label_to_mac.return_value = None

        ip: Optional[str] = _resolve_identifier(
            "Nonexistent Light", mock_registry, {},
        )
        self.assertIsNone(ip)

    def test_unknown_mac_returns_none(self) -> None:
        """MAC not in ARP table returns None."""
        from scheduler import _resolve_identifier
        ip: Optional[str] = _resolve_identifier(
            "d0:73:d5:ff:ff:ff", None, {},
        )
        self.assertIsNone(ip)

    @patch("scheduler._read_arp")
    def test_resolve_groups_full_chain(self, mock_arp: MagicMock) -> None:
        """Full group resolution: label + MAC + IP mixed identifiers."""
        from scheduler import _resolve_groups

        mock_arp.return_value = {
            "10.0.0.35": "d0:73:d5:6b:be:3d",
            "10.0.0.45": "d0:73:d5:d4:79:9c",
        }

        # Mock the registry.
        mock_registry: MagicMock = MagicMock()
        mock_registry.label_to_mac.side_effect = lambda label: {
            "Porch Front": "d0:73:d5:6b:be:3d",
            "PORCH STRING LIGHTS": "d0:73:d5:d4:79:9c",
        }.get(label)

        groups: dict = {
            "porch": ["Porch Front", "PORCH STRING LIGHTS"],
            "single": ["10.0.0.99"],  # Raw IP.
        }

        with patch("scheduler.DeviceRegistry") as MockReg, \
             patch("scheduler.os.path.exists", return_value=True):
            MockReg.return_value = mock_registry
            mock_registry.load = MagicMock()
            resolved, unresolved = _resolve_groups(groups)

        self.assertEqual(len(resolved["porch"]), 2)
        self.assertIn("10.0.0.35", resolved["porch"])
        self.assertIn("10.0.0.45", resolved["porch"])
        self.assertEqual(resolved["single"], ["10.0.0.99"])
        self.assertEqual(len(unresolved), 0)


# ===================================================================
# 3. Backward compatibility — raw IPs without registry
# ===================================================================

class TestBackwardCompatibility(unittest.TestCase):
    """Raw IP configs work without registry or resolution modules."""

    def test_ip_only_schedule_loads(self) -> None:
        """A schedule.json with only IPs loads and validates."""
        from scheduler import _load_config
        path: str = _write_json(IP_SCHEDULE_CONFIG)
        try:
            config: dict = _load_config(path)
            self.assertEqual(len(config["schedule"]), 1)
            self.assertEqual(config["groups"]["porch"], ["10.0.0.35", "10.0.0.45"])
        finally:
            os.unlink(path)

    def test_resolve_groups_without_registry(self) -> None:
        """Resolution without registry passes IPs through."""
        from scheduler import _resolve_groups

        groups: dict = {"porch": ["10.0.0.35", "10.0.0.45"]}

        with patch("scheduler._HAS_RESOLUTION", False):
            resolved, unresolved = _resolve_groups(groups)

        self.assertEqual(resolved, groups)
        self.assertEqual(len(unresolved), 0)


# ===================================================================
# 4. Round-trip — same file works for both consumers
# ===================================================================

class TestRoundTrip(unittest.TestCase):
    """Same schedule.json is valid for both server and standalone scheduler."""

    def test_schedule_json_valid_for_both(self) -> None:
        """A label-based schedule.json passes both loaders' validation."""
        from scheduler import _load_config as sched_load
        from server import _load_config as server_load

        sched_path: str = _write_json(SCHEDULE_CONFIG)
        server_cfg: dict = dict(MINIMAL_SERVER_CONFIG)
        server_cfg["schedule_file"] = sched_path
        server_path: str = _write_json(server_cfg)

        try:
            # Standalone scheduler: loads schedule.json directly.
            sched_config: dict = sched_load(sched_path)
            self.assertEqual(len(sched_config["schedule"]), 1)
            self.assertEqual(
                sched_config["schedule"][0]["effect"], "aurora",
            )

            # Server: loads server.json which references schedule.json.
            server_config: dict = server_load(server_path)
            self.assertEqual(len(server_config["schedule"]), 1)
            self.assertEqual(
                server_config["schedule"][0]["effect"], "aurora",
            )

            # Both see the same schedule entry.
            self.assertEqual(
                sched_config["schedule"][0]["name"],
                server_config["schedule"][0]["name"],
            )
        finally:
            os.unlink(sched_path)
            os.unlink(server_path)

    def test_ip_schedule_valid_for_both(self) -> None:
        """A raw-IP schedule.json passes both loaders."""
        from scheduler import _load_config as sched_load
        from server import _load_config as server_load

        sched_path: str = _write_json(IP_SCHEDULE_CONFIG)
        server_cfg: dict = dict(MINIMAL_SERVER_CONFIG)
        server_cfg["schedule_file"] = sched_path
        server_path: str = _write_json(server_cfg)

        try:
            sched_config: dict = sched_load(sched_path)
            server_config: dict = server_load(server_path)

            self.assertEqual(
                sched_config["schedule"][0]["group"],
                server_config["schedule"][0]["group"],
            )
        finally:
            os.unlink(sched_path)
            os.unlink(server_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
