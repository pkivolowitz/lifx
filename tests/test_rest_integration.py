#!/usr/bin/env python3
"""REST API integration tests for the GlowUp server.

This is an elevated-tier test suite — NOT for pre-commit hooks.
Intended for pre-release validation before deploying to the Pi.
Spins up a real HTTP server in a background thread using the actual
``GlowUpRequestHandler``, backed by a ``DeviceManager`` with no real
hardware, then fires real HTTP requests against every endpoint category.

Expected runtime: ~5-10 seconds on a modern Mac.

Test categories:
- Valid requests return correct status codes (200, 201)
- Missing auth token returns 401
- Invalid auth token returns 401
- Malformed JSON body returns 400
- Missing required fields return 400 with descriptive error
- Non-existent device/index returns 404
- XSS/injection strings in fields don't crash the server
- Concurrent requests don't deadlock (10 threads hitting endpoints)
- Empty body on POST returns 400
- Huge body (1MB) doesn't crash (returns 413)
- Wrong HTTP method returns 404 (no matching route)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "1.0"

import http.server
import json
import logging
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from server import (
    DeviceManager,
    GlowUpRequestHandler,
    MAX_REQUEST_BODY,
    _rate_limiter,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Authentication token used across all tests.
TEST_AUTH_TOKEN: str = "test-secret-token-42"

# Bind address for the test HTTP server — loopback only, OS-assigned port.
TEST_BIND_HOST: str = "127.0.0.1"

# Port zero tells the OS to pick a free ephemeral port.
TEST_BIND_PORT: int = 0

# A fake but structurally valid IPv4 address for mock devices.
FAKE_DEVICE_IP: str = "192.168.99.1"

# A second fake device IP for multi-device tests.
FAKE_DEVICE_IP_2: str = "192.168.99.2"

# Name for the test device group.
TEST_GROUP_NAME: str = "testgroup"

# An effect name known to exist in the GlowUp effect registry.
KNOWN_EFFECT_NAME: str = "cylon"

# An effect name that does not exist in any registry.
BOGUS_EFFECT_NAME: str = "nonexistent_effect_xyz"

# Number of concurrent threads for the deadlock stress test.
CONCURRENCY_THREAD_COUNT: int = 10

# Timeout (seconds) for individual HTTP requests during tests.
REQUEST_TIMEOUT_SECONDS: float = 5.0

# Body size that exceeds MAX_REQUEST_BODY (64 KB + 1 byte).
OVERSIZED_BODY_BYTES: int = MAX_REQUEST_BODY + 1

# Location coordinates for schedule time resolution (Mobile, AL).
TEST_LATITUDE: float = 30.6954
TEST_LONGITUDE: float = -88.0399

# HTTP status codes used in assertions — named for readability.
HTTP_OK: int = 200
HTTP_CREATED: int = 201
HTTP_BAD_REQUEST: int = 400
HTTP_UNAUTHORIZED: int = 401
HTTP_NOT_FOUND: int = 404
HTTP_ENTITY_TOO_LARGE: int = 413
HTTP_TOO_MANY_REQUESTS: int = 429


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(config_path: str) -> dict[str, Any]:
    """Build a minimal server config dict with all required keys.

    Args:
        config_path: Path to the JSON config file (for _save_config_field).

    Returns:
        A config dict suitable for GlowUpRequestHandler.config.
    """
    return {
        "groups": {
            TEST_GROUP_NAME: [FAKE_DEVICE_IP, FAKE_DEVICE_IP_2],
        },
        "schedule": [],
        "automations": [],
        "location": {
            "latitude": TEST_LATITUDE,
            "longitude": TEST_LONGITUDE,
        },
    }


def _write_config(path: str, config: dict[str, Any]) -> None:
    """Write config to disk so _save_config_field can read it back.

    Args:
        path:   Filesystem path.
        config: Config dict to serialize.
    """
    with open(path, "w") as f:
        json.dump(config, f, indent=4)
        f.write("\n")


class _RequestHelper:
    """Convenience methods for firing HTTP requests against the test server.

    Encapsulates base URL construction, auth header injection, and
    response parsing so individual test methods stay concise.
    """

    def __init__(self, base_url: str, token: str) -> None:
        """Initialize with the server base URL and auth token.

        Args:
            base_url: e.g. ``"http://127.0.0.1:54321"``.
            token:    Bearer token string.
        """
        self.base_url: str = base_url
        self.token: str = token

    def request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        *,
        raw_body: Optional[bytes] = None,
        auth: bool = True,
        token_override: Optional[str] = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> tuple[int, dict[str, Any]]:
        """Fire an HTTP request and return (status_code, parsed_json_body).

        Args:
            method:         HTTP method (GET, POST, PUT, DELETE).
            path:           URL path (e.g. ``"/api/status"``).
            body:           JSON-serializable dict for the request body.
            raw_body:       Raw bytes to send as the body (overrides body).
            auth:           Whether to include the Authorization header.
            token_override: Use this token instead of the default.
            timeout:        Request timeout in seconds.

        Returns:
            A tuple of (HTTP status code, parsed response JSON dict).
            If the response is not valid JSON, returns an empty dict.
        """
        url: str = self.base_url + path
        data: Optional[bytes] = None
        if raw_body is not None:
            data = raw_body
        elif body is not None:
            data = json.dumps(body).encode("utf-8")

        req: urllib.request.Request = urllib.request.Request(
            url, data=data, method=method,
        )

        if auth:
            tok: str = token_override if token_override is not None else self.token
            req.add_header("Authorization", f"Bearer {tok}")

        if data is not None:
            req.add_header("Content-Type", "application/json")
            req.add_header("Content-Length", str(len(data)))

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_resp: bytes = resp.read()
                try:
                    return (resp.status, json.loads(raw_resp))
                except json.JSONDecodeError:
                    return (resp.status, {})
        except urllib.error.HTTPError as exc:
            raw_resp = exc.read()
            try:
                return (exc.code, json.loads(raw_resp))
            except json.JSONDecodeError:
                return (exc.code, {})


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

class TestRESTIntegration(unittest.TestCase):
    """Integration tests for the GlowUp REST API.

    Spins up a real HTTPServer in a daemon thread with the actual
    ``GlowUpRequestHandler``, backed by a ``DeviceManager`` that has
    no real LIFX hardware.  All requests go through the full handler
    dispatch pipeline — routing, auth, JSON parsing, validation.
    """

    # Class-level server and helper — shared across all test methods.
    # setUp/tearDown would re-create per test; setUpClass is faster and
    # the handler is stateless enough that sharing is safe.
    server: http.server.HTTPServer
    server_thread: threading.Thread
    helper: _RequestHelper
    config_path: str
    config: dict[str, Any]
    _tmpdir: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls) -> None:
        """Start the test HTTP server and configure the handler."""
        # Suppress noisy log output during tests.
        logging.disable(logging.CRITICAL)

        # Create a temp directory and config file for persistence tests.
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.config_path = os.path.join(cls._tmpdir.name, "server.json")
        cls.config = _make_config(cls.config_path)
        _write_config(cls.config_path, cls.config)

        # Build a DeviceManager with no real devices — just empty lists.
        dm: DeviceManager = DeviceManager(
            device_ips=[],
            groups={TEST_GROUP_NAME: [FAKE_DEVICE_IP, FAKE_DEVICE_IP_2]},
            nicknames={},
        )
        # Mark ready so GET /api/status reports "ready".
        dm._ready = True

        # Wire up handler class-level attributes.
        GlowUpRequestHandler.device_manager = dm
        GlowUpRequestHandler.auth_token = TEST_AUTH_TOKEN
        GlowUpRequestHandler.config = cls.config
        GlowUpRequestHandler.config_path = cls.config_path
        GlowUpRequestHandler.scheduler = None
        GlowUpRequestHandler.media_manager = None
        # automation_manager class attribute was removed in
        # 2026-04-15 along with AutomationManager itself; the
        # operator framework owns trigger logic now.
        GlowUpRequestHandler.orchestrator = None
        GlowUpRequestHandler.keepalive = None
        GlowUpRequestHandler.registry = None

        # Start server on a random free port.
        cls.server = http.server.HTTPServer(
            (TEST_BIND_HOST, TEST_BIND_PORT),
            GlowUpRequestHandler,
        )
        port: int = cls.server.server_address[1]

        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.server_thread.start()

        cls.helper = _RequestHelper(
            f"http://{TEST_BIND_HOST}:{port}",
            TEST_AUTH_TOKEN,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        """Shut down the test server and clean up temp files."""
        cls.server.shutdown()
        cls.server_thread.join(timeout=REQUEST_TIMEOUT_SECONDS)
        cls._tmpdir.cleanup()
        logging.disable(logging.NOTSET)

    def setUp(self) -> None:
        """Reset rate limiter state before each test.

        Without this, auth failure tests can trip the rate limiter
        and cause subsequent tests to get 429 instead of 401.
        """
        # Clear all tracked failures so tests are independent.
        with _rate_limiter._lock:
            _rate_limiter._failures.clear()

        # Reset schedule and automations to empty for test isolation.
        self.config["schedule"] = []
        self.config["automations"] = []
        _write_config(self.config_path, self.config)

    # -- GET /api/status ---------------------------------------------------

    def test_get_status_returns_200(self) -> None:
        """GET /api/status should return 200 with ready=true."""
        code, data = self.helper.request("GET", "/api/status")
        self.assertEqual(code, HTTP_OK)
        self.assertTrue(data.get("ready"))
        self.assertIn("version", data)

    # -- GET /api/devices --------------------------------------------------

    def test_get_devices_returns_200(self) -> None:
        """GET /api/devices should return 200 with a devices list."""
        code, data = self.helper.request("GET", "/api/devices")
        self.assertEqual(code, HTTP_OK)
        self.assertIn("devices", data)
        self.assertIsInstance(data["devices"], list)

    # -- GET /api/effects --------------------------------------------------

    def test_get_effects_returns_200(self) -> None:
        """GET /api/effects should return 200 with an effects dict."""
        code, data = self.helper.request("GET", "/api/effects")
        self.assertEqual(code, HTTP_OK)
        self.assertIn("effects", data)
        # The registry should have at least a few effects.
        self.assertGreater(len(data["effects"]), 0)

    def test_get_effects_contains_known_effect(self) -> None:
        """GET /api/effects should include the 'cylon' effect."""
        code, data = self.helper.request("GET", "/api/effects")
        self.assertEqual(code, HTTP_OK)
        self.assertIn(KNOWN_EFFECT_NAME, data["effects"])

    # -- GET /api/groups ---------------------------------------------------

    def test_get_groups_returns_200(self) -> None:
        """GET /api/groups should return 200 with the configured groups."""
        code, data = self.helper.request("GET", "/api/groups")
        self.assertEqual(code, HTTP_OK)
        self.assertIn("groups", data)
        self.assertIn(TEST_GROUP_NAME, data["groups"])

    # -- GET /api/schedule -------------------------------------------------

    def test_get_schedule_empty_returns_200(self) -> None:
        """GET /api/schedule with no entries should return 200."""
        code, data = self.helper.request("GET", "/api/schedule")
        self.assertEqual(code, HTTP_OK)
        self.assertIn("entries", data)
        self.assertEqual(len(data["entries"]), 0)

    # -- GET /api/automations ----------------------------------------------

    def test_get_automations_empty_returns_200(self) -> None:
        """GET /api/automations with no entries should return 200."""
        code, data = self.helper.request("GET", "/api/automations")
        self.assertEqual(code, HTTP_OK)
        self.assertIn("automations", data)
        self.assertEqual(len(data["automations"]), 0)

    # -- Authentication ----------------------------------------------------

    def test_missing_auth_token_returns_401(self) -> None:
        """Request without Authorization header should get 401."""
        code, data = self.helper.request("GET", "/api/status", auth=False)
        self.assertEqual(code, HTTP_UNAUTHORIZED)
        self.assertIn("error", data)

    def test_invalid_auth_token_returns_401(self) -> None:
        """Request with wrong bearer token should get 401."""
        code, data = self.helper.request(
            "GET", "/api/status", token_override="wrong-token",
        )
        self.assertEqual(code, HTTP_UNAUTHORIZED)
        self.assertIn("error", data)

    def test_empty_bearer_token_returns_401(self) -> None:
        """Authorization header with empty token after 'Bearer ' gets 401."""
        code, data = self.helper.request(
            "GET", "/api/status", token_override="",
        )
        self.assertEqual(code, HTTP_UNAUTHORIZED)

    # -- POST /api/devices/{ip}/play ---------------------------------------

    def test_play_missing_effect_returns_400(self) -> None:
        """POST play without 'effect' field should return 400."""
        code, data = self.helper.request(
            "POST", f"/api/devices/{FAKE_DEVICE_IP}/play",
            body={"params": {}},
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("error", data)

    def test_play_nonexistent_device_returns_404(self) -> None:
        """POST play on an IP not in the device manager should return 404."""
        # FAKE_DEVICE_IP is in the group config but not loaded (no hardware).
        # DeviceManager.play() raises KeyError for unknown devices.
        code, data = self.helper.request(
            "POST", f"/api/devices/{FAKE_DEVICE_IP}/play",
            body={"effect": KNOWN_EFFECT_NAME, "params": {}},
        )
        # The device IP passes validation (valid IPv4) but play() raises
        # KeyError because no Controller exists — handler returns 404.
        self.assertEqual(code, HTTP_NOT_FOUND)

    def test_play_invalid_effect_returns_400_or_404(self) -> None:
        """POST play with a bogus effect name should return 400 or 404."""
        code, data = self.helper.request(
            "POST", f"/api/devices/{FAKE_DEVICE_IP}/play",
            body={"effect": BOGUS_EFFECT_NAME, "params": {}},
        )
        # 404 from KeyError (no device) or 400 from ValueError (bad effect).
        self.assertIn(code, (HTTP_BAD_REQUEST, HTTP_NOT_FOUND))

    def test_play_invalid_device_id_returns_400(self) -> None:
        """POST play with a non-IP, non-group device ID should return 400."""
        code, data = self.helper.request(
            "POST", "/api/devices/not-a-valid-ip/play",
            body={"effect": KNOWN_EFFECT_NAME},
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("error", data)

    # -- POST /api/devices/{ip}/stop ---------------------------------------

    def test_stop_nonexistent_device_returns_404(self) -> None:
        """POST stop on a device not in the manager should return 404."""
        code, data = self.helper.request(
            "POST", f"/api/devices/{FAKE_DEVICE_IP}/stop",
        )
        # The device is valid IPv4 but has no emitter — 404.
        self.assertEqual(code, HTTP_NOT_FOUND)

    # -- POST /api/schedule (create) ---------------------------------------

    def test_schedule_create_valid_returns_201(self) -> None:
        """POST /api/schedule with valid data should return 201."""
        body: dict[str, Any] = {
            "name": "test evening",
            "group": TEST_GROUP_NAME,
            "start": "18:00",
            "stop": "23:00",
            "effect": KNOWN_EFFECT_NAME,
            "params": {},
        }
        code, data = self.helper.request("POST", "/api/schedule", body=body)
        self.assertEqual(code, HTTP_CREATED)
        self.assertIn("index", data)
        self.assertTrue(data.get("created"))

    def test_schedule_create_missing_name_returns_400(self) -> None:
        """POST /api/schedule without name should return 400."""
        body: dict[str, Any] = {
            "group": TEST_GROUP_NAME,
            "start": "18:00",
            "stop": "23:00",
            "effect": KNOWN_EFFECT_NAME,
        }
        code, data = self.helper.request("POST", "/api/schedule", body=body)
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("Name is required", data.get("error", ""))

    def test_schedule_create_missing_all_fields_returns_400(self) -> None:
        """POST /api/schedule with empty body should list all missing fields."""
        code, data = self.helper.request("POST", "/api/schedule", body={})
        self.assertEqual(code, HTTP_BAD_REQUEST)
        error_msg: str = data.get("error", "")
        # All required fields should be mentioned.
        self.assertIn("Name is required", error_msg)
        self.assertIn("Group is required", error_msg)
        self.assertIn("Effect is required", error_msg)
        self.assertIn("Start time is required", error_msg)
        self.assertIn("Stop time is required", error_msg)

    def test_schedule_create_unknown_effect_returns_400(self) -> None:
        """POST /api/schedule with unknown effect should return 400."""
        body: dict[str, Any] = {
            "name": "test",
            "group": TEST_GROUP_NAME,
            "start": "18:00",
            "stop": "23:00",
            "effect": BOGUS_EFFECT_NAME,
        }
        code, data = self.helper.request("POST", "/api/schedule", body=body)
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("Unknown effect", data.get("error", ""))

    def test_schedule_create_unknown_group_returns_400(self) -> None:
        """POST /api/schedule with unknown group should return 400."""
        body: dict[str, Any] = {
            "name": "test",
            "group": "nonexistent_group_xyz",
            "start": "18:00",
            "stop": "23:00",
            "effect": KNOWN_EFFECT_NAME,
        }
        code, data = self.helper.request("POST", "/api/schedule", body=body)
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("Unknown group", data.get("error", ""))

    def test_schedule_create_invalid_time_returns_400(self) -> None:
        """POST /api/schedule with malformed time should return 400."""
        body: dict[str, Any] = {
            "name": "test",
            "group": TEST_GROUP_NAME,
            "start": "not-a-time",
            "stop": "23:00",
            "effect": KNOWN_EFFECT_NAME,
        }
        code, data = self.helper.request("POST", "/api/schedule", body=body)
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("Invalid start time", data.get("error", ""))

    # -- POST /api/schedule/{index}/enabled --------------------------------

    def test_schedule_enabled_toggle(self) -> None:
        """Create entry, then toggle enabled via POST .../enabled."""
        # Create an entry first.
        create_body: dict[str, Any] = {
            "name": "toggle test",
            "group": TEST_GROUP_NAME,
            "start": "18:00",
            "stop": "23:00",
            "effect": KNOWN_EFFECT_NAME,
        }
        code, data = self.helper.request("POST", "/api/schedule", body=create_body)
        self.assertEqual(code, HTTP_CREATED)
        idx: int = data["index"]

        # Disable it.
        code, data = self.helper.request(
            "POST", f"/api/schedule/{idx}/enabled",
            body={"enabled": False},
        )
        self.assertEqual(code, HTTP_OK)
        self.assertFalse(data.get("enabled"))

        # Enable it back.
        code, data = self.helper.request(
            "POST", f"/api/schedule/{idx}/enabled",
            body={"enabled": True},
        )
        self.assertEqual(code, HTTP_OK)
        self.assertTrue(data.get("enabled"))

    def test_schedule_enabled_nonexistent_returns_404(self) -> None:
        """POST .../999/enabled on empty schedule should return 404."""
        code, data = self.helper.request(
            "POST", "/api/schedule/999/enabled",
            body={"enabled": False},
        )
        self.assertEqual(code, HTTP_NOT_FOUND)

    def test_schedule_enabled_invalid_bool_returns_400(self) -> None:
        """POST .../0/enabled with non-bool should return 400."""
        # Create an entry so index 0 exists.
        self.helper.request("POST", "/api/schedule", body={
            "name": "x", "group": TEST_GROUP_NAME,
            "start": "18:00", "stop": "23:00", "effect": KNOWN_EFFECT_NAME,
        })
        code, data = self.helper.request(
            "POST", "/api/schedule/0/enabled",
            body={"enabled": "yes"},
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("boolean", data.get("error", "").lower())

    # -- PUT /api/schedule/{index} -----------------------------------------

    def test_schedule_update_valid(self) -> None:
        """Create then update a schedule entry via PUT."""
        # Create.
        self.helper.request("POST", "/api/schedule", body={
            "name": "orig", "group": TEST_GROUP_NAME,
            "start": "18:00", "stop": "23:00", "effect": KNOWN_EFFECT_NAME,
        })
        # Update.
        code, data = self.helper.request("PUT", "/api/schedule/0", body={
            "name": "updated", "group": TEST_GROUP_NAME,
            "start": "19:00", "stop": "23:30", "effect": KNOWN_EFFECT_NAME,
        })
        self.assertEqual(code, HTTP_OK)
        self.assertTrue(data.get("updated"))
        self.assertEqual(data.get("name"), "updated")

    def test_schedule_update_nonexistent_returns_404(self) -> None:
        """PUT /api/schedule/999 should return 404."""
        code, data = self.helper.request("PUT", "/api/schedule/999", body={
            "name": "x", "group": TEST_GROUP_NAME,
            "start": "18:00", "stop": "23:00", "effect": KNOWN_EFFECT_NAME,
        })
        self.assertEqual(code, HTTP_NOT_FOUND)

    # -- DELETE /api/schedule/{index} --------------------------------------

    def test_schedule_delete_valid(self) -> None:
        """Create then delete a schedule entry."""
        self.helper.request("POST", "/api/schedule", body={
            "name": "to-delete", "group": TEST_GROUP_NAME,
            "start": "18:00", "stop": "23:00", "effect": KNOWN_EFFECT_NAME,
        })
        code, data = self.helper.request("DELETE", "/api/schedule/0")
        self.assertEqual(code, HTTP_OK)
        self.assertIn("deleted", data)

    def test_schedule_delete_nonexistent_returns_404(self) -> None:
        """DELETE /api/schedule/999 should return 404."""
        code, data = self.helper.request("DELETE", "/api/schedule/999")
        self.assertEqual(code, HTTP_NOT_FOUND)

    # -- POST /api/groups (create) -----------------------------------------

    def test_group_create_valid_returns_201(self) -> None:
        """POST /api/groups with valid data should return 201."""
        body: dict[str, Any] = {
            "name": "newgroup",
            "members": [FAKE_DEVICE_IP],
        }
        code, data = self.helper.request("POST", "/api/groups", body=body)
        self.assertEqual(code, HTTP_CREATED)
        self.assertEqual(data.get("name"), "newgroup")

        # Clean up — remove so it doesn't affect other tests.
        self.helper.request("DELETE", "/api/groups/newgroup")

    def test_group_create_missing_name_returns_400(self) -> None:
        """POST /api/groups without name should return 400."""
        code, data = self.helper.request(
            "POST", "/api/groups",
            body={"members": [FAKE_DEVICE_IP]},
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("name", data.get("error", "").lower())

    def test_group_create_no_members_returns_400(self) -> None:
        """POST /api/groups with empty members should return 400."""
        code, data = self.helper.request(
            "POST", "/api/groups",
            body={"name": "empty", "members": []},
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("member", data.get("error", "").lower())

    def test_group_create_duplicate_returns_400(self) -> None:
        """POST /api/groups with existing group name should return 400."""
        code, data = self.helper.request(
            "POST", "/api/groups",
            body={"name": TEST_GROUP_NAME, "members": [FAKE_DEVICE_IP]},
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("already exists", data.get("error", ""))

    def test_group_create_underscore_name_returns_400(self) -> None:
        """POST /api/groups with underscore-prefixed name should return 400."""
        code, data = self.helper.request(
            "POST", "/api/groups",
            body={"name": "_hidden", "members": [FAKE_DEVICE_IP]},
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("_", data.get("error", ""))

    # -- PUT /api/groups/{name} --------------------------------------------

    def test_group_update_nonexistent_returns_404(self) -> None:
        """PUT /api/groups/nonexistent should return 404."""
        code, data = self.helper.request(
            "PUT", "/api/groups/nonexistent",
            body={"name": "x", "members": [FAKE_DEVICE_IP]},
        )
        self.assertEqual(code, HTTP_NOT_FOUND)

    # -- DELETE /api/groups/{name} -----------------------------------------

    def test_group_delete_nonexistent_returns_404(self) -> None:
        """DELETE /api/groups/nonexistent should return 404."""
        code, data = self.helper.request("DELETE", "/api/groups/nonexistent")
        self.assertEqual(code, HTTP_NOT_FOUND)

    # -- Malformed JSON body -----------------------------------------------

    def test_malformed_json_returns_400(self) -> None:
        """POST with invalid JSON should return 400."""
        code, data = self.helper.request(
            "POST", "/api/schedule",
            raw_body=b"{this is not valid json}",
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("Invalid JSON", data.get("error", ""))

    def test_non_object_json_returns_400(self) -> None:
        """POST with a JSON array instead of object should return 400."""
        code, data = self.helper.request(
            "POST", "/api/schedule",
            raw_body=b'[1, 2, 3]',
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("Expected JSON object", data.get("error", ""))

    # -- Empty body on POST ------------------------------------------------

    def test_empty_body_post_returns_400(self) -> None:
        """POST with Content-Length: 0 and no body should return 400."""
        code, data = self.helper.request(
            "POST", "/api/schedule",
            raw_body=b"",
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)

    # -- Oversized body ----------------------------------------------------

    def test_oversized_body_returns_413(self) -> None:
        """POST with body exceeding MAX_REQUEST_BODY should return 413."""
        # Build a valid-looking JSON body that's just way too big.
        # The padding string pushes past the size limit.
        padding: str = "x" * OVERSIZED_BODY_BYTES
        huge_body: bytes = json.dumps({"padding": padding}).encode("utf-8")
        code, data = self.helper.request(
            "POST", "/api/schedule",
            raw_body=huge_body,
        )
        self.assertEqual(code, HTTP_ENTITY_TOO_LARGE)
        self.assertIn("too large", data.get("error", "").lower())

    # -- Wrong HTTP method -------------------------------------------------

    def test_wrong_method_returns_404(self) -> None:
        """DELETE /api/status (not a defined route) should return 404."""
        code, data = self.helper.request("DELETE", "/api/status")
        self.assertEqual(code, HTTP_NOT_FOUND)

    def test_put_on_get_only_endpoint_returns_404(self) -> None:
        """PUT /api/devices (a GET-only endpoint) should return 404."""
        code, data = self.helper.request("PUT", "/api/devices", body={})
        self.assertEqual(code, HTTP_NOT_FOUND)

    # -- Non-existent path -------------------------------------------------

    def test_nonexistent_path_returns_404(self) -> None:
        """GET /api/nonexistent should return 404."""
        code, data = self.helper.request("GET", "/api/nonexistent")
        self.assertEqual(code, HTTP_NOT_FOUND)
        self.assertIn("Not found", data.get("error", ""))

    # -- XSS / injection strings -------------------------------------------

    def test_xss_in_group_name_does_not_crash(self) -> None:
        """Creating a group with XSS payload should not crash the server."""
        xss: str = '<script>alert("xss")</script>'
        code, data = self.helper.request(
            "POST", "/api/groups",
            body={"name": xss, "members": [FAKE_DEVICE_IP]},
        )
        # The server should respond (not crash). It may create the group
        # or reject it — either way, no 500.
        self.assertIn(code, (HTTP_OK, HTTP_CREATED, HTTP_BAD_REQUEST))

        # Clean up in case it was created — URL-encode special chars.
        try:
            encoded: str = urllib.parse.quote(xss, safe="")
            self.helper.request("DELETE", f"/api/groups/{encoded}")
        except Exception:
            pass  # Best-effort cleanup.

    def test_xss_in_schedule_name_does_not_crash(self) -> None:
        """Creating a schedule entry with XSS in name should not crash."""
        xss: str = '"><img src=x onerror=alert(1)>'
        code, data = self.helper.request("POST", "/api/schedule", body={
            "name": xss,
            "group": TEST_GROUP_NAME,
            "start": "18:00",
            "stop": "23:00",
            "effect": KNOWN_EFFECT_NAME,
        })
        # Should succeed (the name is just a label) or reject — not crash.
        self.assertIn(code, (HTTP_CREATED, HTTP_BAD_REQUEST))

    def test_sql_injection_in_group_name_does_not_crash(self) -> None:
        """SQL injection attempt in group name should not crash."""
        sqli: str = "'; DROP TABLE groups; --"
        code, data = self.helper.request(
            "POST", "/api/groups",
            body={"name": sqli, "members": [FAKE_DEVICE_IP]},
        )
        self.assertIn(code, (HTTP_OK, HTTP_CREATED, HTTP_BAD_REQUEST))
        # Clean up — URL-encode the name since it contains spaces.
        try:
            encoded: str = urllib.parse.quote(sqli, safe="")
            self.helper.request("DELETE", f"/api/groups/{encoded}")
        except Exception:
            pass  # Best-effort cleanup; failure here is acceptable.

    def test_null_bytes_in_body_does_not_crash(self) -> None:
        """Null bytes in JSON field values should not crash the server."""
        code, data = self.helper.request("POST", "/api/schedule", body={
            "name": "test\x00null",
            "group": TEST_GROUP_NAME,
            "start": "18:00",
            "stop": "23:00",
            "effect": KNOWN_EFFECT_NAME,
        })
        # Should respond, not hang or crash.
        self.assertIn(code, (HTTP_CREATED, HTTP_BAD_REQUEST))

    def test_unicode_bomb_does_not_crash(self) -> None:
        """Extreme Unicode strings should not crash the server."""
        # Mix of emoji, RTL override, zero-width joiners.
        bomb: str = "\U0001F4A3\u202E\u200D\u200B" * 100
        code, data = self.helper.request("POST", "/api/schedule", body={
            "name": bomb,
            "group": TEST_GROUP_NAME,
            "start": "18:00",
            "stop": "23:00",
            "effect": KNOWN_EFFECT_NAME,
        })
        self.assertIn(code, (HTTP_CREATED, HTTP_BAD_REQUEST))

    # -- Device status / colors on nonexistent device ----------------------

    def test_device_status_nonexistent_returns_404(self) -> None:
        """GET /api/devices/{ip}/status on unknown device should return 404."""
        code, data = self.helper.request(
            "GET", f"/api/devices/{FAKE_DEVICE_IP}/status",
        )
        self.assertEqual(code, HTTP_NOT_FOUND)

    def test_device_colors_nonexistent_returns_404(self) -> None:
        """GET /api/devices/{ip}/colors on unknown device should return 404."""
        code, data = self.helper.request(
            "GET", f"/api/devices/{FAKE_DEVICE_IP}/colors",
        )
        self.assertEqual(code, HTTP_NOT_FOUND)

    # -- POST /api/devices/{ip}/power --------------------------------------

    def test_power_missing_on_field_returns_400(self) -> None:
        """POST power without 'on' boolean should return 400."""
        code, data = self.helper.request(
            "POST", f"/api/devices/{FAKE_DEVICE_IP}/power",
            body={"power": True},  # Wrong key name.
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("boolean", data.get("error", "").lower())

    def test_power_string_on_field_returns_400(self) -> None:
        """POST power with on='true' (string) should return 400."""
        code, data = self.helper.request(
            "POST", f"/api/devices/{FAKE_DEVICE_IP}/power",
            body={"on": "true"},
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)

    # -- POST /api/devices/{ip}/nickname -----------------------------------

    def test_nickname_nonexistent_device(self) -> None:
        """POST nickname on a device with no emitter still sets nickname."""
        # DeviceManager.set_nickname doesn't check device existence — it
        # just updates the nickname map.  This is fine; the nickname
        # persists until the device appears.
        code, data = self.helper.request(
            "POST", f"/api/devices/{FAKE_DEVICE_IP}/nickname",
            body={"nickname": "Test Bulb"},
        )
        self.assertEqual(code, HTTP_OK)
        self.assertEqual(data.get("nickname"), "Test Bulb")

    # -- GET /api/schedule with entries ------------------------------------

    def test_get_schedule_with_entries(self) -> None:
        """GET /api/schedule after creating entries returns them."""
        # Create two entries.
        for i in range(2):
            self.helper.request("POST", "/api/schedule", body={
                "name": f"entry_{i}",
                "group": TEST_GROUP_NAME,
                "start": "18:00",
                "stop": "23:00",
                "effect": KNOWN_EFFECT_NAME,
            })
        code, data = self.helper.request("GET", "/api/schedule")
        self.assertEqual(code, HTTP_OK)
        self.assertEqual(len(data["entries"]), 2)
        self.assertEqual(data["entries"][0]["name"], "entry_0")
        self.assertEqual(data["entries"][1]["name"], "entry_1")

    # -- Concurrent requests (deadlock detection) --------------------------

    def test_concurrent_requests_no_deadlock(self) -> None:
        """Fire parallel requests to different endpoints without deadlock."""
        # Pre-create a schedule entry for the toggle test.
        self.helper.request("POST", "/api/schedule", body={
            "name": "concurrency-test",
            "group": TEST_GROUP_NAME,
            "start": "18:00",
            "stop": "23:00",
            "effect": KNOWN_EFFECT_NAME,
        })

        results: list[tuple[int, str]] = []
        lock: threading.Lock = threading.Lock()
        errors: list[str] = []

        # Each endpoint/method pair to hit concurrently.
        endpoints: list[tuple[str, str, Optional[dict]]] = [
            ("GET", "/api/status", None),
            ("GET", "/api/devices", None),
            ("GET", "/api/effects", None),
            ("GET", "/api/groups", None),
            ("GET", "/api/schedule", None),
            ("GET", "/api/automations", None),
            ("POST", "/api/schedule/0/enabled", {"enabled": True}),
            ("GET", "/api/status", None),
            ("GET", "/api/effects", None),
            ("GET", "/api/groups", None),
        ]

        def _fire(method: str, path: str, body: Optional[dict]) -> None:
            """Send one request and record the result."""
            try:
                code, data = self.helper.request(method, path, body=body)
                with lock:
                    results.append((code, path))
            except Exception as exc:
                with lock:
                    errors.append(f"{method} {path}: {exc}")

        threads: list[threading.Thread] = []
        for method, path, body in endpoints:
            t: threading.Thread = threading.Thread(
                target=_fire, args=(method, path, body),
            )
            threads.append(t)

        # Start all threads at once.
        for t in threads:
            t.start()

        # Wait for all to complete with a generous timeout.
        for t in threads:
            t.join(timeout=REQUEST_TIMEOUT_SECONDS * 2)

        # Verify no threads are still alive (would indicate deadlock).
        alive: list[threading.Thread] = [t for t in threads if t.is_alive()]
        self.assertEqual(
            len(alive), 0,
            f"{len(alive)} threads still alive — likely deadlocked",
        )

        # Verify no exceptions occurred.
        self.assertEqual(len(errors), 0, f"Concurrent request errors: {errors}")

        # All requests should have gotten valid responses.
        self.assertEqual(len(results), CONCURRENCY_THREAD_COUNT)
        for code, path in results:
            self.assertIn(
                code, (HTTP_OK, HTTP_CREATED, HTTP_NOT_FOUND),
                f"Unexpected status {code} from {path}",
            )

    # -- Group CRUD lifecycle ----------------------------------------------

    def test_group_crud_lifecycle(self) -> None:
        """Full create-read-update-delete lifecycle for a group."""
        group_name: str = "lifecycle_group"

        # Create.
        code, data = self.helper.request("POST", "/api/groups", body={
            "name": group_name,
            "members": [FAKE_DEVICE_IP],
        })
        self.assertEqual(code, HTTP_CREATED)

        # Read — verify it shows up.
        code, data = self.helper.request("GET", "/api/groups")
        self.assertEqual(code, HTTP_OK)
        self.assertIn(group_name, data["groups"])

        # Update — rename and change members.
        new_name: str = "lifecycle_group_v2"
        code, data = self.helper.request(
            "PUT", f"/api/groups/{group_name}",
            body={"name": new_name, "members": [FAKE_DEVICE_IP, FAKE_DEVICE_IP_2]},
        )
        self.assertEqual(code, HTTP_OK)
        self.assertEqual(data.get("name"), new_name)

        # Delete.
        code, data = self.helper.request("DELETE", f"/api/groups/{new_name}")
        self.assertEqual(code, HTTP_OK)
        self.assertIn("deleted", data)

        # Verify it's gone.
        code, data = self.helper.request("GET", "/api/groups")
        self.assertNotIn(new_name, data["groups"])

    # -- Schedule CRUD lifecycle -------------------------------------------

    def test_schedule_crud_lifecycle(self) -> None:
        """Full create-read-toggle-update-delete lifecycle for schedule."""
        # Create.
        code, data = self.helper.request("POST", "/api/schedule", body={
            "name": "lifecycle",
            "group": TEST_GROUP_NAME,
            "start": "18:00",
            "stop": "23:00",
            "effect": KNOWN_EFFECT_NAME,
        })
        self.assertEqual(code, HTTP_CREATED)
        idx: int = data["index"]

        # Read.
        code, data = self.helper.request("GET", "/api/schedule")
        self.assertEqual(code, HTTP_OK)
        self.assertEqual(len(data["entries"]), 1)
        self.assertEqual(data["entries"][0]["name"], "lifecycle")

        # Toggle enabled.
        code, data = self.helper.request(
            "POST", f"/api/schedule/{idx}/enabled",
            body={"enabled": False},
        )
        self.assertEqual(code, HTTP_OK)
        self.assertFalse(data["enabled"])

        # Update.
        code, data = self.helper.request(f"PUT", f"/api/schedule/{idx}", body={
            "name": "lifecycle-v2",
            "group": TEST_GROUP_NAME,
            "start": "19:00",
            "stop": "23:30",
            "effect": KNOWN_EFFECT_NAME,
        })
        self.assertEqual(code, HTTP_OK)

        # Delete.
        code, data = self.helper.request("DELETE", f"/api/schedule/{idx}")
        self.assertEqual(code, HTTP_OK)

        # Verify empty.
        code, data = self.helper.request("GET", "/api/schedule")
        self.assertEqual(len(data["entries"]), 0)

    # -- Config persistence ------------------------------------------------

    def test_schedule_create_persists_to_disk(self) -> None:
        """Schedule creation should write through to the config file."""
        self.helper.request("POST", "/api/schedule", body={
            "name": "persist-test",
            "group": TEST_GROUP_NAME,
            "start": "18:00",
            "stop": "23:00",
            "effect": KNOWN_EFFECT_NAME,
        })

        # Read the config file and verify the schedule was written.
        with open(self.config_path, "r") as f:
            disk_config: dict[str, Any] = json.load(f)
        self.assertEqual(len(disk_config.get("schedule", [])), 1)
        self.assertEqual(disk_config["schedule"][0]["name"], "persist-test")

    def test_group_create_persists_to_disk(self) -> None:
        """Group creation should write through to the config file."""
        self.helper.request("POST", "/api/groups", body={
            "name": "persist_grp",
            "members": [FAKE_DEVICE_IP],
        })

        with open(self.config_path, "r") as f:
            disk_config: dict[str, Any] = json.load(f)
        self.assertIn("persist_grp", disk_config.get("groups", {}))

        # Clean up.
        self.helper.request("DELETE", "/api/groups/persist_grp")

    # -- Security headers --------------------------------------------------

    def test_security_headers_present(self) -> None:
        """Responses should include standard security headers."""
        url: str = self.helper.base_url + "/api/status"
        req: urllib.request.Request = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {TEST_AUTH_TOKEN}")

        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            # Check for the key security headers set by _send_security_headers.
            self.assertEqual(
                resp.headers.get("X-Content-Type-Options"), "nosniff",
            )
            self.assertEqual(
                resp.headers.get("X-Frame-Options"), "DENY",
            )
            self.assertIn(
                "max-age", resp.headers.get("Strict-Transport-Security", ""),
            )

    # -- Content-Type validation -------------------------------------------

    def test_response_content_type_is_json(self) -> None:
        """All API responses should have Content-Type: application/json."""
        url: str = self.helper.base_url + "/api/status"
        req: urllib.request.Request = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {TEST_AUTH_TOKEN}")

        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            ct: str = resp.headers.get("Content-Type", "")
            self.assertIn("application/json", ct)

    # -- Schedule symbolic time specs --------------------------------------

    def test_schedule_create_symbolic_time(self) -> None:
        """Schedule with sunset+30m and midnight should be accepted."""
        code, data = self.helper.request("POST", "/api/schedule", body={
            "name": "symbolic",
            "group": TEST_GROUP_NAME,
            "start": "sunset+30m",
            "stop": "midnight",
            "effect": KNOWN_EFFECT_NAME,
        })
        self.assertEqual(code, HTTP_CREATED)

    # -- Multiple field errors in one response -----------------------------

    def test_schedule_multiple_errors_returned(self) -> None:
        """Validation should report all errors, not just the first."""
        code, data = self.helper.request("POST", "/api/schedule", body={
            "name": "",
            "group": "",
            "start": "",
            "stop": "",
            "effect": "",
        })
        self.assertEqual(code, HTTP_BAD_REQUEST)
        # The error string is semicolon-delimited — count the parts.
        parts: list[str] = data.get("error", "").split(";")
        # At least 5 errors: name, group, effect, start, stop.
        self.assertGreaterEqual(len(parts), 5)

    # -- Negative schedule index -------------------------------------------

    def test_schedule_negative_index_returns_404(self) -> None:
        """PUT /api/schedule/-1 should return 404 (out of bounds)."""
        code, data = self.helper.request("PUT", "/api/schedule/-1", body={
            "name": "x", "group": TEST_GROUP_NAME,
            "start": "18:00", "stop": "23:00", "effect": KNOWN_EFFECT_NAME,
        })
        self.assertEqual(code, HTTP_NOT_FOUND)

    # -- POST play with non-dict params ------------------------------------

    def test_play_non_dict_params_returns_400(self) -> None:
        """POST play with params as a list should return 400."""
        code, data = self.helper.request(
            "POST", f"/api/devices/{FAKE_DEVICE_IP}/play",
            body={"effect": KNOWN_EFFECT_NAME, "params": [1, 2, 3]},
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)
        self.assertIn("params", data.get("error", "").lower())

    # -- Device identifier edge cases --------------------------------------

    def test_group_colon_prefix_only_returns_400(self) -> None:
        """Device ID 'group:' (empty group name) should return 400."""
        code, data = self.helper.request(
            "GET", "/api/devices/group:/status",
        )
        self.assertEqual(code, HTTP_BAD_REQUEST)

    # -- Rapid sequential requests don't cause state corruption -----------

    def test_rapid_schedule_create_delete_cycle(self) -> None:
        """Rapidly creating and deleting schedule entries stays consistent."""
        cycle_count: int = 5
        for i in range(cycle_count):
            code, data = self.helper.request("POST", "/api/schedule", body={
                "name": f"rapid_{i}",
                "group": TEST_GROUP_NAME,
                "start": "18:00",
                "stop": "23:00",
                "effect": KNOWN_EFFECT_NAME,
            })
            self.assertEqual(code, HTTP_CREATED)
            idx: int = data["index"]
            code, _ = self.helper.request("DELETE", f"/api/schedule/{idx}")
            self.assertEqual(code, HTTP_OK)

        # After all cycles, schedule should be empty.
        code, data = self.helper.request("GET", "/api/schedule")
        self.assertEqual(code, HTTP_OK)
        self.assertEqual(len(data["entries"]), 0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
