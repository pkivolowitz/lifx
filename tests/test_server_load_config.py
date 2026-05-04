"""Tests for ``server._load_config`` — state-file split (groups_file).

Covers the public-release boot path where the canonical group registry
lives in ``/var/lib/glowup/groups.json`` (writable by the service),
referenced from ``/etc/glowup/server.json`` via the ``groups_file``
key.  Also asserts the legacy path (groups directly in server.json,
no ``groups_file``) still works — that's the existing fleet-host
shape and breaking it would mean breaking the production hub.

Tests use a temp directory for both files; nothing under /etc or
/var/lib is touched.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import secrets
import tempfile
import unittest
from pathlib import Path
from typing import Any

from server import _load_config


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _valid_token() -> str:
    """A non-CHANGE_ME token so the auth-token validator passes."""
    return secrets.token_urlsafe(32)


def _placeholder_groups() -> dict[str, Any]:
    """Match the shape install.py writes to /var/lib/glowup/groups.json.

    A leading ``_comment`` is preserved by the loader (entries whose
    name begins with ``_`` are skipped by group iteration but still
    pass through as part of the dict).
    """
    return {
        "_comment": "Placeholder group pointing at RFC 5737 TEST-NET-1.",
        "placeholder": ["192.0.2.1"],
    }


class _TempDirCase(unittest.TestCase):
    """Each test gets its own temp dir for server.json + groups.json."""

    def setUp(self) -> None:
        self._tmpdir: tempfile.TemporaryDirectory = tempfile.TemporaryDirectory()
        self.tmp: Path = Path(self._tmpdir.name)
        self.server_json: Path = self.tmp / "server.json"
        self.groups_json: Path = self.tmp / "groups.json"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()


# ---------------------------------------------------------------------------
# Happy path — groups_file referenced + populated
# ---------------------------------------------------------------------------


class TestGroupsFileLoad(_TempDirCase):
    """``groups_file`` populates ``config['groups']`` from the external file."""

    def test_loads_groups_from_file(self) -> None:
        """Groups dict in groups.json arrives in config["groups"]."""
        self.groups_json.write_text(json.dumps(_placeholder_groups()))
        self.server_json.write_text(json.dumps({
            "schema_version": 1,
            "port": 8420,
            "auth_token": _valid_token(),
            "groups_file": str(self.groups_json),
        }))
        config: dict[str, Any] = _load_config(str(self.server_json))
        self.assertIn("groups", config)
        self.assertIn("placeholder", config["groups"])
        self.assertEqual(config["groups"]["placeholder"], ["192.0.2.1"])

    def test_stores_resolved_groups_path(self) -> None:
        """The loader stamps ``_groups_path`` for the dashboard's write side."""
        self.groups_json.write_text(json.dumps(_placeholder_groups()))
        self.server_json.write_text(json.dumps({
            "schema_version": 1,
            "port": 8420,
            "auth_token": _valid_token(),
            "groups_file": str(self.groups_json),
        }))
        config: dict[str, Any] = _load_config(str(self.server_json))
        self.assertEqual(
            config.get("_groups_path"), str(self.groups_json),
            "_save_config_field reads this to route group writes",
        )

    def test_relative_groups_path_resolves_against_server_json(self) -> None:
        """A relative ``groups_file`` is resolved against server.json's dir."""
        self.groups_json.write_text(json.dumps(_placeholder_groups()))
        self.server_json.write_text(json.dumps({
            "schema_version": 1,
            "port": 8420,
            "auth_token": _valid_token(),
            "groups_file": self.groups_json.name,  # bare filename
        }))
        config: dict[str, Any] = _load_config(str(self.server_json))
        self.assertEqual(config["groups"]["placeholder"], ["192.0.2.1"])


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestGroupsFileFailures(_TempDirCase):
    """``groups_file`` errors loud — silent fall-through would mask broken installs."""

    def test_missing_file_raises_filenotfounderror(self) -> None:
        """Referenced file that doesn't exist → FileNotFoundError."""
        self.server_json.write_text(json.dumps({
            "schema_version": 1,
            "port": 8420,
            "auth_token": _valid_token(),
            "groups_file": str(self.tmp / "no_such.json"),
        }))
        with self.assertRaises(FileNotFoundError):
            _load_config(str(self.server_json))

    def test_non_object_groups_file_raises_valueerror(self) -> None:
        """Top-level JSON list (or anything not a dict) → ValueError."""
        self.groups_json.write_text(json.dumps(["not", "a", "dict"]))
        self.server_json.write_text(json.dumps({
            "schema_version": 1,
            "port": 8420,
            "auth_token": _valid_token(),
            "groups_file": str(self.groups_json),
        }))
        with self.assertRaises(ValueError):
            _load_config(str(self.server_json))


# ---------------------------------------------------------------------------
# Backwards compat — legacy groups-in-server.json path still works
# ---------------------------------------------------------------------------


class TestLegacyGroupsInServerJson(_TempDirCase):
    """Existing fleet-host shape (``groups`` directly in server.json) still loads."""

    def test_legacy_groups_load_unchanged(self) -> None:
        """No ``groups_file`` set → server.json's ``groups`` is authoritative."""
        legacy_groups: dict[str, Any] = {
            "Living Room": ["192.0.2.10", "192.0.2.11"],
        }
        self.server_json.write_text(json.dumps({
            "schema_version": 1,
            "port": 8420,
            "auth_token": _valid_token(),
            "groups": legacy_groups,
        }))
        config: dict[str, Any] = _load_config(str(self.server_json))
        self.assertEqual(config["groups"], legacy_groups)
        self.assertNotIn(
            "_groups_path", config,
            "_groups_path must be absent when groups_file is unset",
        )


# ---------------------------------------------------------------------------
# state_file resolution — the SQLite state store path
# ---------------------------------------------------------------------------


class TestStateFileResolution(_TempDirCase):
    """``state_file`` resolves to ``_state_path``; absent key falls back."""

    def _write_minimal_server_json(self, **extra: Any) -> None:
        """Write a minimal valid server.json plus any extra keys."""
        body: dict[str, Any] = {
            "schema_version": 1,
            "port": 8420,
            "auth_token": _valid_token(),
            "groups": {"placeholder": ["192.0.2.1"]},
        }
        body.update(extra)
        self.server_json.write_text(json.dumps(body))

    def test_absolute_state_file_passes_through(self) -> None:
        """An absolute ``state_file`` is stored verbatim under ``_state_path``."""
        target: Path = self.tmp / "state.db"
        self._write_minimal_server_json(state_file=str(target))
        config: dict[str, Any] = _load_config(str(self.server_json))
        self.assertEqual(config["_state_path"], str(target))

    def test_relative_state_file_resolves_against_server_json(self) -> None:
        """A relative ``state_file`` resolves against the server.json directory."""
        self._write_minimal_server_json(state_file="state.db")
        config: dict[str, Any] = _load_config(str(self.server_json))
        self.assertEqual(
            config["_state_path"], str(self.tmp / "state.db"),
        )

    def test_legacy_no_state_file_defaults_to_config_dir(self) -> None:
        """No ``state_file`` set → fallback to ``<config_dir>/state.db``.

        This is the byte-identical-on-fleet contract — master hosts that
        don't ship ``state_file`` must keep opening state.db right next
        to server.json, otherwise we silently move their existing
        SQLite state out from under them on first restart after pull.
        """
        self._write_minimal_server_json()
        config: dict[str, Any] = _load_config(str(self.server_json))
        self.assertEqual(
            config["_state_path"], str(self.tmp / "state.db"),
        )

    def test_state_file_does_not_require_existing_db(self) -> None:
        """A non-existent ``state_file`` is fine — SQLite creates it lazily.

        Unlike groups_file (must exist; loaded eagerly), state_file is
        only a path — the actual sqlite3 connect happens later in the
        consumers (DeviceManager / occupancy operator).
        """
        target: Path = self.tmp / "subdir" / "state.db"
        self._write_minimal_server_json(state_file=str(target))
        # Loader must not raise even though the target file is absent.
        config: dict[str, Any] = _load_config(str(self.server_json))
        self.assertEqual(config["_state_path"], str(target))


# ---------------------------------------------------------------------------
# device_registry_file resolution — Phase 2 of the state-file split
# ---------------------------------------------------------------------------


class TestDeviceRegistryFileResolution(_TempDirCase):
    """``device_registry_file`` resolves to ``_device_registry_path``."""

    def _write_minimal_server_json(self, **extra: Any) -> None:
        body: dict[str, Any] = {
            "schema_version": 1,
            "port": 8420,
            "auth_token": _valid_token(),
            "groups": {"placeholder": ["192.0.2.1"]},
        }
        body.update(extra)
        self.server_json.write_text(json.dumps(body))

    def test_absolute_device_registry_file_passes_through(self) -> None:
        """Absolute ``device_registry_file`` is stored verbatim."""
        target: Path = self.tmp / "devices.json"
        self._write_minimal_server_json(device_registry_file=str(target))
        config: dict[str, Any] = _load_config(str(self.server_json))
        self.assertEqual(config["_device_registry_path"], str(target))

    def test_relative_device_registry_file_resolves(self) -> None:
        """Relative ``device_registry_file`` resolves against server.json's dir."""
        self._write_minimal_server_json(device_registry_file="devices.json")
        config: dict[str, Any] = _load_config(str(self.server_json))
        self.assertEqual(
            config["_device_registry_path"],
            str(self.tmp / "devices.json"),
        )

    def test_legacy_no_key_leaves_path_unset(self) -> None:
        """No ``device_registry_file`` set → no ``_device_registry_path``.

        DeviceRegistry's own default chain (env var → DEFAULT_REGISTRY_PATH)
        is the legacy contract; this loader must not synthesise a path
        that would override it.
        """
        self._write_minimal_server_json()
        config: dict[str, Any] = _load_config(str(self.server_json))
        self.assertNotIn(
            "_device_registry_path", config,
            "_device_registry_path must be absent when device_registry_file unset",
        )

    def test_device_registry_file_does_not_require_existing(self) -> None:
        """Non-existent target is fine — first run before any registrations.

        The loader resolves the path; DeviceRegistry.load() handles
        missing-file by entering legacy IP-only mode, which is the
        first-run state.
        """
        target: Path = self.tmp / "subdir" / "devices.json"
        self._write_minimal_server_json(device_registry_file=str(target))
        config: dict[str, Any] = _load_config(str(self.server_json))
        self.assertEqual(config["_device_registry_path"], str(target))


if __name__ == "__main__":
    unittest.main()
