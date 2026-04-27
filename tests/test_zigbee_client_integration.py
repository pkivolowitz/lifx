"""Live integration tests for ``zigbee_service.client.ZigbeeControlClient``.

Exercises the client against the real glowup-zigbee-service running
on the operator's broker-2 host.  The service URL must be supplied via
the ``GLOWUP_TEST_ZIGBEE_SERVICE`` environment variable; no
site-specific default lives in the repo.  If the env var is unset or
the service is unreachable these tests are skipped, so a headless run
without fleet access still passes — same pattern as
test_pi_thermal_integration.

**Safety budget**:

- The only plug actuated is ``BYIR`` (nighttime IR flood — invisible
  flicker, nothing safety-critical).  Override with the env var
  ``GLOWUP_TEST_ZIGBEE_PLUG``.
- BYIR is actuated at most twice per run: once to the opposite of its
  current state, then once to restore it.  Per Perry: low number of
  cycles, real equipment is plugged into these.
- Every other device (ML_Power, LRTV, MBTV, soil sensors) is read-only.

Run::

    GLOWUP_TEST_ZIGBEE_SERVICE=http://broker-2.example.lan:8422 \\
        ~/venv/bin/python -m unittest tests.test_zigbee_client_integration -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.

__version__ = "1.0"

import os
import socket
import sys
import time
import unittest
from typing import Any
from urllib.parse import urlparse

_REPO_ROOT: str = os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."),
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from zigbee_service.client import ZigbeeControlClient
from zigbee_service.device_types import (
    TYPE_PLUG,
    TYPE_SOIL,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# No in-repo default service URL — site-specific addresses must come
# from the operator's environment via ``$GLOWUP_TEST_ZIGBEE_SERVICE``.
# When the variable is unset the suite skips wholesale; this keeps the
# public repo free of household network details.
_SERVICE_URL_ENV: str = "GLOWUP_TEST_ZIGBEE_SERVICE"
_DEFAULT_TEST_PLUG: str = "BYIR"

_SERVICE_URL: str = os.environ.get(_SERVICE_URL_ENV, "")
_TEST_PLUG: str = os.environ.get("GLOWUP_TEST_ZIGBEE_PLUG", _DEFAULT_TEST_PLUG)

# TCP reachability probe timeout.  1.5 s is plenty on the LAN and
# keeps the skip path snappy when the test host is off-fleet.
_PROBE_TIMEOUT_S: float = 1.5

# Maximum BYIR state transitions per run, inclusive of the restore
# transition.  Enforced by counter in the toggle test — tightening
# this budget is preferred to loosening it.
_MAX_BYIR_TRANSITIONS: int = 2


def _reachable(url: str, timeout_s: float = _PROBE_TIMEOUT_S) -> bool:
    """Return True iff the service accepts a TCP connection within *timeout_s*.

    We don't actually hit /health here — a bare TCP connect is the
    cheapest possible reachability probe and matches the pattern in
    test_pi_thermal_integration.  An empty URL means no service was
    configured for this run; treat as not reachable.
    """
    if not url:
        return False
    parsed = urlparse(url)
    host: str = parsed.hostname or ""
    port: int = parsed.port or 80
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except (OSError, socket.timeout):
        return False


_SERVICE_REACHABLE: bool = _reachable(_SERVICE_URL)


@unittest.skipUnless(
    _SERVICE_REACHABLE,
    (
        f"${_SERVICE_URL_ENV} not set — set it to the service URL to run this suite"
        if not _SERVICE_URL
        else f"glowup-zigbee-service unreachable at {_SERVICE_URL}"
    ),
)
class ReadOnlyIntegrationTests(unittest.TestCase):
    """Every endpoint that doesn't change state — hammer freely."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client: ZigbeeControlClient = ZigbeeControlClient(_SERVICE_URL)

    def test_list_devices_returns_nonempty(self) -> None:
        """Paired devices show up in /devices on any running fleet."""
        ok, devices = self.client.list_devices()
        self.assertTrue(ok, f"list_devices failed: {devices}")
        self.assertIsInstance(devices, list)
        self.assertGreater(
            len(devices), 0,
            "no devices returned — is Z2M running on broker-2?",
        )

    def test_list_devices_plug_filter(self) -> None:
        """type=plug restricts the response to plug-classified devices."""
        ok, devices = self.client.list_devices(type_filter=TYPE_PLUG)
        self.assertTrue(ok)
        self.assertIsInstance(devices, list)
        for dev in devices:
            self.assertEqual(
                dev.get("type"), TYPE_PLUG,
                f"non-plug leaked through filter: {dev}",
            )

    def test_list_devices_soil_filter(self) -> None:
        """type=soil restricts the response to soil-classified devices."""
        ok, devices = self.client.list_devices(type_filter=TYPE_SOIL)
        self.assertTrue(ok)
        for dev in devices:
            self.assertEqual(dev.get("type"), TYPE_SOIL)

    def test_list_devices_bogus_filter_returns_empty(self) -> None:
        """Rolling-upgrade safety — unknown type string must yield []."""
        ok, devices = self.client.list_devices(type_filter="zigbee_wyvern")
        self.assertTrue(ok)
        self.assertEqual(devices, [])

    def test_get_known_device(self) -> None:
        """Fetching the test plug by name must return a dict with type."""
        ok, dev = self.client.get_device(_TEST_PLUG)
        self.assertTrue(ok, f"get_device({_TEST_PLUG}) failed: {dev}")
        self.assertIsInstance(dev, dict)
        self.assertEqual(dev.get("name"), _TEST_PLUG)
        self.assertIn("type", dev)

    def test_get_unknown_device_surfaces_404(self) -> None:
        """Fabricated device name must surface the service's 404 error."""
        ok, err = self.client.get_device("ZZZ_does_not_exist_ZZZ")
        self.assertFalse(ok)
        self.assertIn("unknown device", str(err).lower())


@unittest.skipUnless(
    _SERVICE_REACHABLE,
    (
        f"${_SERVICE_URL_ENV} not set — set it to the service URL to run this suite"
        if not _SERVICE_URL
        else f"glowup-zigbee-service unreachable at {_SERVICE_URL}"
    ),
)
class ByirToggleIntegrationTest(unittest.TestCase):
    """Single-plug round-trip with strict transition accounting."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client: ZigbeeControlClient = ZigbeeControlClient(
            _SERVICE_URL, timeout_s=10.0,
        )
        cls._transitions: int = 0

    def _toggle(self, to_state: str) -> None:
        """Toggle BYIR, enforcing the session-wide transition budget."""
        if type(self)._transitions >= _MAX_BYIR_TRANSITIONS:
            self.fail(
                f"transition budget exceeded ({_MAX_BYIR_TRANSITIONS})",
            )
        type(self)._transitions += 1
        result = self.client.set_state(_TEST_PLUG, to_state)
        self.assertTrue(
            result.ok, f"set_state {to_state} not ok: {result.error}",
        )
        self.assertTrue(
            result.echoed,
            f"{_TEST_PLUG} did not echo {to_state}: {result.error}",
        )
        self.assertEqual(result.state, to_state)

    def test_round_trip_preserves_starting_state(self) -> None:
        """Flip BYIR to the opposite of its current state and back.

        Total radio transitions: exactly 2 (current→opposite→current).
        """
        ok, dev = self.client.get_device(_TEST_PLUG)
        self.assertTrue(ok, f"cannot read BYIR baseline: {dev}")
        starting: Any = dev.get("state")
        if starting not in ("ON", "OFF"):
            self.skipTest(
                f"{_TEST_PLUG} has no baseline ON/OFF state "
                f"(got {starting!r}); skipping toggle test",
            )

        opposite: str = "OFF" if starting == "ON" else "ON"

        # Flip away.
        self._toggle(opposite)
        # The service blocks on echo, but give Z2M a brief beat to let
        # the retained-state update propagate before we verify.
        time.sleep(0.5)
        ok, dev = self.client.get_device(_TEST_PLUG)
        self.assertTrue(ok)
        self.assertEqual(dev.get("state"), opposite)

        # Flip back — restores user-visible state regardless of
        # anything else that fails later in the run.
        self._toggle(starting)
        time.sleep(0.5)
        ok, dev = self.client.get_device(_TEST_PLUG)
        self.assertTrue(ok)
        self.assertEqual(dev.get("state"), starting)


if __name__ == "__main__":
    unittest.main()
