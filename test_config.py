#!/usr/bin/env python3
"""Unit tests for server configuration validation.

Tests that _load_config correctly validates and rejects bad inputs:
  - Missing or default auth tokens
  - Invalid port numbers
  - Missing groups
  - Schedule entries with missing required fields
  - Invalid day-of-week strings
  - Invalid MQTT configuration
  - Valid configs pass without error

Uses temporary files to simulate real config loading.
No network or hardware dependencies.
"""

import json
import os
import tempfile
import unittest
from typing import Any

from server import _load_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(config: dict[str, Any]) -> str:
    """Write a config dict to a temporary JSON file.

    Args:
        config: Configuration dictionary.

    Returns:
        The path to the temporary file (caller should clean up).
    """
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path


def _minimal_valid_config() -> dict[str, Any]:
    """Return a minimal valid server configuration."""
    return {
        "auth_token": "test-token-for-unit-tests-only-000",
        "port": 8420,
        "groups": {
            "porch": ["10.0.0.62"],
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestValidConfig(unittest.TestCase):
    """Valid configs should load without error."""

    def test_minimal_config(self) -> None:
        """Minimal config (auth_token + groups) loads successfully."""
        config = _minimal_valid_config()
        path = _write_config(config)
        try:
            result = _load_config(path)
            self.assertEqual(result["auth_token"], config["auth_token"])
            self.assertIn("groups", result)
        finally:
            os.unlink(path)

    def test_config_with_schedule(self) -> None:
        """Config with schedule and location loads successfully."""
        config = _minimal_valid_config()
        config["location"] = {"latitude": 30.69, "longitude": -88.04}
        config["schedule"] = [{
            "name": "test",
            "group": "porch",
            "start": "18:00",
            "stop": "23:00",
            "effect": "aurora",
        }]
        path = _write_config(config)
        try:
            result = _load_config(path)
            self.assertEqual(len(result["schedule"]), 1)
        finally:
            os.unlink(path)

    def test_config_with_mqtt(self) -> None:
        """Config with MQTT section loads successfully."""
        config = _minimal_valid_config()
        config["mqtt"] = {"broker": "localhost", "port": 1883}
        path = _write_config(config)
        try:
            result = _load_config(path)
            self.assertEqual(result["mqtt"]["broker"], "localhost")
        finally:
            os.unlink(path)

    def test_default_port(self) -> None:
        """Port defaults to 8420 if omitted."""
        config = _minimal_valid_config()
        del config["port"]
        path = _write_config(config)
        try:
            result = _load_config(path)
            self.assertEqual(result["port"], 8420)
        finally:
            os.unlink(path)


class TestAuthTokenValidation(unittest.TestCase):
    """Auth token validation."""

    def test_missing_token(self) -> None:
        """Missing auth_token raises ValueError."""
        config = _minimal_valid_config()
        del config["auth_token"]
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_default_token_rejected(self) -> None:
        """The literal string 'CHANGE_ME' is rejected."""
        config = _minimal_valid_config()
        config["auth_token"] = "CHANGE_ME"
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_empty_token_rejected(self) -> None:
        """Empty string token is rejected."""
        config = _minimal_valid_config()
        config["auth_token"] = ""
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_non_string_token_rejected(self) -> None:
        """Numeric token is rejected."""
        config = _minimal_valid_config()
        config["auth_token"] = 12345
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)


class TestPortValidation(unittest.TestCase):
    """Port number validation."""

    def test_port_zero_rejected(self) -> None:
        config = _minimal_valid_config()
        config["port"] = 0
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_port_negative_rejected(self) -> None:
        config = _minimal_valid_config()
        config["port"] = -1
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_port_too_high_rejected(self) -> None:
        config = _minimal_valid_config()
        config["port"] = 70000
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_port_string_rejected(self) -> None:
        config = _minimal_valid_config()
        config["port"] = "8420"
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)


class TestGroupsValidation(unittest.TestCase):
    """Groups section validation."""

    def test_missing_groups_rejected(self) -> None:
        config = _minimal_valid_config()
        del config["groups"]
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_empty_groups_rejected(self) -> None:
        config = _minimal_valid_config()
        config["groups"] = {}
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_empty_group_list_rejected(self) -> None:
        config = _minimal_valid_config()
        config["groups"] = {"porch": []}
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_comment_keys_ignored(self) -> None:
        """Keys starting with '_' are treated as comments."""
        config = _minimal_valid_config()
        config["groups"]["_comment"] = "this is a comment"
        path = _write_config(config)
        try:
            result = _load_config(path)
            self.assertIn("_comment", result["groups"])
        finally:
            os.unlink(path)


class TestScheduleValidation(unittest.TestCase):
    """Schedule entry validation."""

    def test_schedule_requires_location(self) -> None:
        """Schedule entries require a location section."""
        config = _minimal_valid_config()
        config["schedule"] = [{
            "name": "test", "group": "porch",
            "start": "18:00", "stop": "23:00", "effect": "aurora",
        }]
        # No location section.
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_schedule_missing_required_field(self) -> None:
        """Schedule entry missing 'effect' raises ValueError."""
        config = _minimal_valid_config()
        config["location"] = {"latitude": 30.69, "longitude": -88.04}
        config["schedule"] = [{
            "name": "test", "group": "porch",
            "start": "18:00", "stop": "23:00",
            # Missing "effect".
        }]
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_schedule_unknown_group(self) -> None:
        """Schedule entry referencing unknown group raises ValueError."""
        config = _minimal_valid_config()
        config["location"] = {"latitude": 30.69, "longitude": -88.04}
        config["schedule"] = [{
            "name": "test", "group": "nonexistent",
            "start": "18:00", "stop": "23:00", "effect": "aurora",
        }]
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_schedule_invalid_days(self) -> None:
        """Schedule entry with invalid days string raises ValueError."""
        config = _minimal_valid_config()
        config["location"] = {"latitude": 30.69, "longitude": -88.04}
        config["schedule"] = [{
            "name": "test", "group": "porch",
            "start": "18:00", "stop": "23:00", "effect": "aurora",
            "days": "MXZ",  # Invalid day letters.
        }]
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_schedule_duplicate_days(self) -> None:
        """Schedule entry with repeated days raises ValueError."""
        config = _minimal_valid_config()
        config["location"] = {"latitude": 30.69, "longitude": -88.04}
        config["schedule"] = [{
            "name": "test", "group": "porch",
            "start": "18:00", "stop": "23:00", "effect": "aurora",
            "days": "MMT",  # Duplicate M.
        }]
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)


class TestMqttValidation(unittest.TestCase):
    """MQTT section validation."""

    def test_mqtt_not_a_dict_rejected(self) -> None:
        config = _minimal_valid_config()
        config["mqtt"] = "localhost"
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_mqtt_invalid_port(self) -> None:
        config = _minimal_valid_config()
        config["mqtt"] = {"broker": "localhost", "port": 0}
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_mqtt_empty_prefix_rejected(self) -> None:
        config = _minimal_valid_config()
        config["mqtt"] = {"broker": "localhost", "topic_prefix": ""}
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_mqtt_negative_color_interval(self) -> None:
        config = _minimal_valid_config()
        config["mqtt"] = {"broker": "localhost", "color_interval": -1.0}
        path = _write_config(config)
        try:
            with self.assertRaises(ValueError):
                _load_config(path)
        finally:
            os.unlink(path)

    def test_mqtt_valid_minimal(self) -> None:
        """Minimal mqtt section (just broker) is valid."""
        config = _minimal_valid_config()
        config["mqtt"] = {"broker": "localhost"}
        path = _write_config(config)
        try:
            result = _load_config(path)
            self.assertEqual(result["mqtt"]["broker"], "localhost")
        finally:
            os.unlink(path)


class TestFileErrors(unittest.TestCase):
    """File-level error handling."""

    def test_nonexistent_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            _load_config("/tmp/nonexistent_glowup_config_xyz.json")

    def test_invalid_json(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write("not valid json {{{")
        try:
            with self.assertRaises(json.JSONDecodeError):
                _load_config(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
