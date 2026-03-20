#!/usr/bin/env python3
"""Regression tests for tech debt audit fixes — 2026-03-19/20.

Covers every change made during the audit sessions on Bed:

- server.py:3752   — logger.warning → logging.warning (undefined name fix)
- transport.py:1484 — logger.debug  → _log.debug     (undefined name fix)
- transport.py:1549 — logger.warning → _log.warning   (undefined name fix)
- transport.py:230  — duplicate PRODUCT_MAP keys 143/144 removed
- emitters/__init__.py — prepare_for_rendering() added to Emitter ABC
- emitters/lifx.py     — skip_wake parameter on prepare_for_rendering()
- emitters/virtual.py  — uses public API with skip_wake=True
- engine.py — bare except→logged warnings in render/send/callback/binding
- transport.py set_label — bare except→logged warning + finally timeout restore
- bulb_keepalive.py close — bare except→logged debug

No network or hardware dependencies.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import os
import tempfile
import threading
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock, patch

from effects import HSBK
from emitters import Emitter, EmitterCapabilities


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Product IDs that were duplicated before the fix.
PRODUCT_ID_STRING_LIGHT_US: int = 143
PRODUCT_ID_STRING_LIGHT_INTL: int = 144


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(config: dict[str, Any]) -> str:
    """Write a config dict to a temporary JSON file.

    Args:
        config: Configuration dictionary.

    Returns:
        Path to the temporary file (caller must clean up).
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
            "porch": ["192.0.2.10"],
        },
    }


class MockLifxEmitter(Emitter):
    """Mock emitter that records prepare_for_rendering calls.

    Tracks both whether the method was called and the value of
    ``skip_wake``, so tests can verify the virtual emitter passes
    the correct flag.
    """

    def __init__(
        self,
        emitter_id: str,
        zone_count: int,
        is_multizone: bool,
    ) -> None:
        """Initialize mock with configurable topology.

        Args:
            emitter_id:   Unique identifier (typically an IP string).
            zone_count:   Number of zones to report.
            is_multizone: Whether this mock acts as a multizone device.
        """
        self._emitter_id: str = emitter_id
        self._zone_count: int = zone_count
        self._is_multizone: bool = is_multizone
        self._label: str = f"mock-{emitter_id}"
        self._product_name: str = "Mock"

        # Call recording.
        self.prepare_calls: list[dict[str, Any]] = []
        self.send_zones_calls: list[tuple] = []
        self.send_color_calls: list[tuple] = []

    @property
    def zone_count(self) -> Optional[int]:
        """Return the configured zone count."""
        return self._zone_count

    @property
    def is_multizone(self) -> bool:
        """Return whether this mock is multizone."""
        return self._is_multizone

    @property
    def emitter_id(self) -> str:
        """Return the mock emitter ID."""
        return self._emitter_id

    @property
    def label(self) -> str:
        """Return the mock label."""
        return self._label

    @property
    def product_name(self) -> str:
        """Return the mock product name."""
        return self._product_name

    def prepare_for_rendering(self, *, skip_wake: bool = False) -> None:
        """Record the call and its skip_wake value."""
        self.prepare_calls.append({"skip_wake": skip_wake})

    def send_zones(
        self,
        colors: list[HSBK],
        duration_ms: int = 0,
        mode: object = None,
    ) -> None:
        """Record send_zones call."""
        self.send_zones_calls.append((list(colors), duration_ms, mode))

    def send_color(
        self,
        hue: int,
        sat: int,
        bri: int,
        kelvin: int,
        duration_ms: int = 0,
    ) -> None:
        """Record send_color call."""
        self.send_color_calls.append((hue, sat, bri, kelvin, duration_ms))

    def power_on(self, duration_ms: int = 0) -> None:
        """No-op for mock."""

    def power_off(self, duration_ms: int = 0) -> None:
        """No-op for mock."""

    def close(self) -> None:
        """No-op for mock."""


# ---------------------------------------------------------------------------
# Test: server.py — logging.warning (was undefined logger.warning)
# ---------------------------------------------------------------------------

class TestServerLoggerFix(unittest.TestCase):
    """Verify server._load_config uses ``logging``, not undefined ``logger``."""

    def test_empty_group_logs_warning_no_crash(self) -> None:
        """An empty group list should log a warning, not raise NameError.

        Before the fix, this code path crashed with ``NameError: name
        'logger' is not defined``.
        """
        config: dict[str, Any] = _minimal_valid_config()
        config["groups"]["empty_room"] = []

        path: str = _write_config(config)
        try:
            from server import _load_config
            with self.assertLogs(level=logging.WARNING) as cm:
                result: dict = _load_config(path)
            # The warning should mention the empty group name.
            found: bool = any("empty_room" in msg for msg in cm.output)
            self.assertTrue(
                found,
                f"Expected warning about 'empty_room', got: {cm.output}",
            )
            # The empty group should have been removed from config.
            self.assertNotIn(
                "empty_room", result["groups"],
                "Empty group should be pruned from config",
            )
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Test: transport.py — _log (was undefined logger)
# ---------------------------------------------------------------------------

class TestTransportLoggerFix(unittest.TestCase):
    """Verify transport module references ``_log``, not undefined ``logger``.

    These tests confirm the symbol resolution is correct by inspecting
    the source — calling the actual functions would require network
    access.
    """

    def test_broadcast_wake_uses_correct_logger(self) -> None:
        """broadcast_wake() error handler must reference _log, not logger."""
        import inspect
        from transport import broadcast_wake
        source: str = inspect.getsource(broadcast_wake)
        self.assertNotIn(
            "logger.",
            source,
            "broadcast_wake still references undefined 'logger' — "
            "should use '_log'",
        )
        self.assertIn(
            "_log.",
            source,
            "broadcast_wake should use '_log' for logging",
        )

    def test_discover_devices_uses_correct_logger(self) -> None:
        """discover_devices() error handler must reference _log, not logger."""
        import inspect
        from transport import discover_devices
        source: str = inspect.getsource(discover_devices)
        self.assertNotIn(
            "logger.",
            source,
            "discover_devices still references undefined 'logger' — "
            "should use '_log'",
        )
        self.assertIn(
            "_log.",
            source,
            "discover_devices should use '_log' for logging",
        )

    def test_log_symbol_exists_in_module(self) -> None:
        """The transport module must define ``_log`` as a Logger."""
        import transport
        self.assertTrue(
            hasattr(transport, "_log"),
            "transport module missing '_log' attribute",
        )
        self.assertIsInstance(
            transport._log,
            logging.Logger,
            "_log should be a logging.Logger instance",
        )


# ---------------------------------------------------------------------------
# Test: transport.py — PRODUCT_MAP duplicate keys removed
# ---------------------------------------------------------------------------

class TestProductMapNoDuplicates(unittest.TestCase):
    """Verify PRODUCT_MAP has no duplicate keys and correct values."""

    def test_string_light_us_has_regional_suffix(self) -> None:
        """Product 143 should be 'String Light US', not generic."""
        from transport import PRODUCT_MAP
        self.assertEqual(
            PRODUCT_MAP[PRODUCT_ID_STRING_LIGHT_US],
            "String Light US",
            f"Product {PRODUCT_ID_STRING_LIGHT_US} should be "
            f"'String Light US' (regional suffix)",
        )

    def test_string_light_intl_has_regional_suffix(self) -> None:
        """Product 144 should be 'String Light Intl', not generic."""
        from transport import PRODUCT_MAP
        self.assertEqual(
            PRODUCT_MAP[PRODUCT_ID_STRING_LIGHT_INTL],
            "String Light Intl",
            f"Product {PRODUCT_ID_STRING_LIGHT_INTL} should be "
            f"'String Light Intl' (regional suffix)",
        )

    def test_product_map_no_duplicate_keys_in_source(self) -> None:
        """Scan transport.py source for duplicate dict keys in PRODUCT_MAP.

        Python silently accepts duplicate keys — this catches regressions
        that the interpreter would not.
        """
        import ast

        transport_path: str = os.path.join(
            os.path.dirname(__file__), "transport.py",
        )
        with open(transport_path, "r") as f:
            source: str = f.read()
        tree: ast.Module = ast.parse(source)

        for node in ast.walk(tree):
            # Handle both plain assignment (Assign) and annotated
            # assignment (AnnAssign: ``PRODUCT_MAP: dict[...] = {...}``).
            target_name: Optional[str] = None
            value_node: Optional[ast.expr] = None

            if isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    target_name = node.target.id
                    value_node = node.value
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        target_name = target.id
                        value_node = node.value

            if target_name != "PRODUCT_MAP" or value_node is None:
                continue

            # Found the assignment — check for duplicate keys.
            self.assertIsInstance(value_node, ast.Dict)
            dict_node: ast.Dict = value_node
            keys: list[int] = []
            for key in dict_node.keys:
                if isinstance(key, ast.Constant):
                    val: int = key.value
                    self.assertNotIn(
                        val, keys,
                        f"Duplicate PRODUCT_MAP key: {val}",
                    )
                    keys.append(val)
            return

        self.fail("Could not find PRODUCT_MAP assignment in transport.py")


# ---------------------------------------------------------------------------
# Test: emitters/__init__.py — prepare_for_rendering on Emitter ABC
# ---------------------------------------------------------------------------

class TestEmitterABCPrepare(unittest.TestCase):
    """Verify prepare_for_rendering() exists on the Emitter base class."""

    def test_base_class_has_prepare_for_rendering(self) -> None:
        """Emitter ABC must define prepare_for_rendering as a no-op."""
        self.assertTrue(
            hasattr(Emitter, "prepare_for_rendering"),
            "Emitter base class missing prepare_for_rendering()",
        )

    def test_base_class_prepare_is_noop(self) -> None:
        """Default prepare_for_rendering() must not raise."""
        # Create a minimal concrete subclass to test the base method.
        class _Stub(Emitter):
            emitter_type = "stub"
            description = "test stub"

            def on_emit(self, frame: Any, metadata: dict) -> bool:
                return True

            def capabilities(self) -> EmitterCapabilities:
                return EmitterCapabilities()

        stub: _Stub = _Stub.__new__(_Stub)
        # Should not raise — it's a no-op.
        stub.prepare_for_rendering()


# ---------------------------------------------------------------------------
# Test: emitters/lifx.py — skip_wake parameter
# ---------------------------------------------------------------------------

class TestLifxEmitterSkipWake(unittest.TestCase):
    """Verify LifxEmitter.prepare_for_rendering() honors skip_wake."""

    def test_signature_accepts_skip_wake(self) -> None:
        """prepare_for_rendering must accept keyword-only skip_wake."""
        import inspect
        from emitters.lifx import LifxEmitter
        sig: inspect.Signature = inspect.signature(
            LifxEmitter.prepare_for_rendering,
        )
        self.assertIn(
            "skip_wake", sig.parameters,
            "LifxEmitter.prepare_for_rendering() missing skip_wake param",
        )
        param: inspect.Parameter = sig.parameters["skip_wake"]
        self.assertEqual(
            param.kind, inspect.Parameter.KEYWORD_ONLY,
            "skip_wake must be keyword-only",
        )
        self.assertFalse(
            param.default,
            "skip_wake default must be False",
        )

    @patch("emitters.lifx.broadcast_wake")
    def test_skip_wake_suppresses_broadcast(self, mock_wake: MagicMock) -> None:
        """When skip_wake=True, broadcast_wake must not be called."""
        from emitters.lifx import LifxEmitter

        em: LifxEmitter = LifxEmitter.__new__(LifxEmitter)
        em._device = None  # No device — just testing the wake logic.
        em.prepare_for_rendering(skip_wake=True)
        mock_wake.assert_not_called()

    @patch("emitters.lifx.broadcast_wake")
    def test_default_calls_broadcast(self, mock_wake: MagicMock) -> None:
        """Default (skip_wake=False) must call broadcast_wake."""
        from emitters.lifx import LifxEmitter

        em: LifxEmitter = LifxEmitter.__new__(LifxEmitter)
        # Provide a mock device so the guard (if _device is not None)
        # doesn't exit early before reaching broadcast_wake().
        em._device = MagicMock()
        em._device.is_multizone = False
        em.prepare_for_rendering()
        mock_wake.assert_called_once()


# ---------------------------------------------------------------------------
# Test: emitters/virtual.py — public API, no private access
# ---------------------------------------------------------------------------

class TestVirtualEmitterNoPrivateAccess(unittest.TestCase):
    """Verify VirtualMultizoneEmitter uses public API only."""

    def test_no_private_device_access_in_prepare(self) -> None:
        """prepare_for_rendering must not access em._device."""
        import inspect
        from emitters.virtual import VirtualMultizoneEmitter
        source: str = inspect.getsource(
            VirtualMultizoneEmitter.prepare_for_rendering,
        )
        self.assertNotIn(
            "_device",
            source,
            "VirtualMultizoneEmitter.prepare_for_rendering() still accesses "
            "private _device attribute — should use public API",
        )

    def test_members_receive_skip_wake_true(self) -> None:
        """Virtual emitter must pass skip_wake=True to each member."""
        from emitters.virtual import VirtualMultizoneEmitter

        emitters: list[MockLifxEmitter] = [
            MockLifxEmitter(f"10.0.0.{i}", zone_count=6, is_multizone=True)
            for i in range(3)
        ]
        # Patch broadcast_wake so we don't need a network.
        with patch("emitters.virtual.broadcast_wake"):
            vem = VirtualMultizoneEmitter(emitters)
            vem.prepare_for_rendering()

        for em in emitters:
            self.assertEqual(
                len(em.prepare_calls), 1,
                f"{em.emitter_id}: prepare not called",
            )
            self.assertTrue(
                em.prepare_calls[0]["skip_wake"],
                f"{em.emitter_id}: expected skip_wake=True, got False",
            )

    def test_virtual_does_one_broadcast_wake(self) -> None:
        """Virtual emitter should issue exactly one broadcast_wake."""
        from emitters.virtual import VirtualMultizoneEmitter

        emitters: list[MockLifxEmitter] = [
            MockLifxEmitter(f"10.0.0.{i}", zone_count=1, is_multizone=False)
            for i in range(5)
        ]
        with patch("emitters.virtual.broadcast_wake") as mock_wake:
            vem = VirtualMultizoneEmitter(emitters)
            vem.prepare_for_rendering()
            mock_wake.assert_called_once()


# ---------------------------------------------------------------------------
# Test: glowup.py — _install_stop_signal helper
# ---------------------------------------------------------------------------

class TestInstallStopSignal(unittest.TestCase):
    """Verify the shared signal-handler installer works correctly."""

    def test_function_exists(self) -> None:
        """_install_stop_signal must be importable from glowup."""
        from glowup import _install_stop_signal
        self.assertTrue(callable(_install_stop_signal))

    def test_returns_same_event(self) -> None:
        """_install_stop_signal must return the event it was given."""
        from glowup import _install_stop_signal
        evt: threading.Event = threading.Event()
        result: threading.Event = _install_stop_signal(evt)
        self.assertIs(result, evt)

    def test_installs_sigint_handler(self) -> None:
        """After calling, SIGINT handler should no longer be default."""
        import signal as sig_mod
        from glowup import _install_stop_signal
        evt: threading.Event = threading.Event()
        _install_stop_signal(evt)
        handler: Any = sig_mod.getsignal(sig_mod.SIGINT)
        # Must not be the default handler or SIG_DFL.
        self.assertNotEqual(handler, sig_mod.SIG_DFL)
        self.assertTrue(callable(handler))

    def test_installs_sigterm_handler(self) -> None:
        """After calling, SIGTERM handler should no longer be default."""
        import signal as sig_mod
        from glowup import _install_stop_signal
        evt: threading.Event = threading.Event()
        _install_stop_signal(evt)
        handler: Any = sig_mod.getsignal(sig_mod.SIGTERM)
        self.assertNotEqual(handler, sig_mod.SIG_DFL)
        self.assertTrue(callable(handler))

    def test_no_inline_signal_handlers_remain(self) -> None:
        """glowup.py should have no inline signal handler definitions
        except _install_stop_signal itself and the replay _shutdown."""
        import inspect
        import glowup
        source: str = inspect.getsource(glowup)

        # Count definitions of signal.signal(signal.SIGINT, ...).
        import re
        sigint_calls: list[str] = re.findall(
            r"signal\.signal\(signal\.SIGINT", source,
        )
        # Expected: 1 in _install_stop_signal + 1 in cmd_replay _shutdown = 2
        self.assertEqual(
            len(sigint_calls), 2,
            f"Expected exactly 2 signal.SIGINT installations "
            f"(helper + replay), found {len(sigint_calls)}",
        )


# ---------------------------------------------------------------------------
# Test: simulator.py — _hsbk_to_rgb_floats shared helper
# ---------------------------------------------------------------------------

class TestHsbkConversion(unittest.TestCase):
    """Verify the refactored HSBK→RGB conversion produces correct results."""

    def test_shared_helper_exists(self) -> None:
        """_hsbk_to_rgb_floats must be importable from simulator."""
        from simulator import _hsbk_to_rgb_floats
        self.assertTrue(callable(_hsbk_to_rgb_floats))

    def test_pure_red(self) -> None:
        """Hue 0, full sat/bri → pure red."""
        from simulator import _hsbk_to_rgb_floats
        r, g, b = _hsbk_to_rgb_floats(0, 65535, 65535)
        self.assertAlmostEqual(r, 1.0, places=3)
        self.assertAlmostEqual(g, 0.0, places=3)
        self.assertAlmostEqual(b, 0.0, places=3)

    def test_pure_green(self) -> None:
        """Hue 21845 (120°), full sat/bri → pure green."""
        from simulator import _hsbk_to_rgb_floats
        r, g, b = _hsbk_to_rgb_floats(21845, 65535, 65535)
        self.assertAlmostEqual(r, 0.0, places=3)
        self.assertAlmostEqual(g, 1.0, places=3)
        self.assertAlmostEqual(b, 0.0, places=3)

    def test_pure_blue(self) -> None:
        """Hue 43690 (240°), full sat/bri → pure blue."""
        from simulator import _hsbk_to_rgb_floats
        r, g, b = _hsbk_to_rgb_floats(43690, 65535, 65535)
        self.assertAlmostEqual(r, 0.0, places=3)
        self.assertAlmostEqual(g, 0.0, places=3)
        self.assertAlmostEqual(b, 1.0, places=3)

    def test_white(self) -> None:
        """Zero saturation, full brightness → white (1, 1, 1)."""
        from simulator import _hsbk_to_rgb_floats
        r, g, b = _hsbk_to_rgb_floats(0, 0, 65535)
        self.assertAlmostEqual(r, 1.0, places=3)
        self.assertAlmostEqual(g, 1.0, places=3)
        self.assertAlmostEqual(b, 1.0, places=3)

    def test_black(self) -> None:
        """Zero brightness → black (0, 0, 0)."""
        from simulator import _hsbk_to_rgb_floats
        r, g, b = _hsbk_to_rgb_floats(0, 0, 0)
        self.assertAlmostEqual(r, 0.0, places=3)
        self.assertAlmostEqual(g, 0.0, places=3)
        self.assertAlmostEqual(b, 0.0, places=3)

    def test_hsbk_to_rgb_uses_shared_helper(self) -> None:
        """hsbk_to_rgb must produce results consistent with the helper."""
        from simulator import hsbk_to_rgb, _hsbk_to_rgb_floats
        test_values: list[tuple[int, int, int]] = [
            (0, 65535, 65535),
            (21845, 65535, 65535),
            (43690, 65535, 65535),
            (0, 0, 65535),
            (10000, 32768, 49152),
        ]
        for hue, sat, bri in test_values:
            r, g, b = _hsbk_to_rgb_floats(hue, sat, bri)
            expected: str = (
                f"#{min(int(r * 255), 255):02x}"
                f"{min(int(g * 255), 255):02x}"
                f"{min(int(b * 255), 255):02x}"
            )
            actual: str = hsbk_to_rgb(hue, sat, bri, 3500)
            self.assertEqual(
                actual, expected,
                f"hsbk_to_rgb({hue},{sat},{bri}) = {actual}, "
                f"expected {expected}",
            )

    def test_hsbk_to_gray_uses_shared_helper(self) -> None:
        """hsbk_to_gray must produce BT.709 luma from the helper's RGB."""
        from simulator import hsbk_to_gray, _hsbk_to_rgb_floats

        # BT.709 luma coefficients (must match simulator.py).
        LUMA_R: float = 0.2126
        LUMA_G: float = 0.7152
        LUMA_B: float = 0.0722

        test_values: list[tuple[int, int, int]] = [
            (0, 65535, 65535),
            (21845, 65535, 65535),
            (0, 0, 65535),
            (10000, 32768, 49152),
        ]
        for hue, sat, bri in test_values:
            r, g, b = _hsbk_to_rgb_floats(hue, sat, bri)
            y: float = LUMA_R * r + LUMA_G * g + LUMA_B * b
            gray: int = min(int(y * 255), 255)
            expected: str = f"#{gray:02x}{gray:02x}{gray:02x}"
            actual: str = hsbk_to_gray(hue, sat, bri, 3500)
            self.assertEqual(
                actual, expected,
                f"hsbk_to_gray({hue},{sat},{bri}) = {actual}, "
                f"expected {expected}",
            )

    def test_no_duplicate_sextant_code(self) -> None:
        """hsbk_to_rgb and hsbk_to_gray must not contain sextant math."""
        import inspect
        from simulator import hsbk_to_rgb, hsbk_to_gray
        for fn in (hsbk_to_rgb, hsbk_to_gray):
            source: str = inspect.getsource(fn)
            self.assertNotIn(
                "sextant",
                source,
                f"{fn.__name__} still contains inline sextant math — "
                f"should delegate to _hsbk_to_rgb_floats",
            )


# ---------------------------------------------------------------------------
# Test: bulb_keepalive.py — _BulbDB._get_dsn() shared helper
# ---------------------------------------------------------------------------

class TestBulbDBGetDsn(unittest.TestCase):
    """Verify DSN resolution is consolidated in _get_dsn()."""

    def test_get_dsn_exists(self) -> None:
        """_BulbDB must have a _get_dsn static method."""
        from bulb_keepalive import _BulbDB
        self.assertTrue(hasattr(_BulbDB, "_get_dsn"))
        self.assertTrue(callable(_BulbDB._get_dsn))

    def test_env_var_overrides_default(self) -> None:
        """GLOWUP_DIAG_DSN env var must take precedence."""
        from bulb_keepalive import _BulbDB
        sentinel: str = "postgresql://test:test@testhost:5432/testdb"
        with patch.dict(os.environ, {"GLOWUP_DIAG_DSN": sentinel}):
            dsn: str = _BulbDB._get_dsn()
        self.assertEqual(dsn, sentinel)

    def test_default_dsn_uses_network_config(self) -> None:
        """Without env var, DSN must use network_config.net.db_host."""
        from bulb_keepalive import _BulbDB
        # Remove env var if set so the default path is taken.
        env: dict[str, str] = os.environ.copy()
        env.pop("GLOWUP_DIAG_DSN", None)
        with patch.dict(os.environ, env, clear=True):
            dsn: str = _BulbDB._get_dsn()
        # Must be a postgresql:// URL.
        self.assertTrue(
            dsn.startswith("postgresql://"),
            f"Default DSN should start with postgresql://, got: {dsn}",
        )

    def test_connect_and_reconnect_no_duplicate_dsn_logic(self) -> None:
        """connect() and _reconnect() must not contain inline DSN construction."""
        import inspect
        from bulb_keepalive import _BulbDB

        for method_name in ("connect", "_reconnect"):
            source: str = inspect.getsource(getattr(_BulbDB, method_name))
            self.assertNotIn(
                "changeme",
                source,
                f"_BulbDB.{method_name}() still contains hardcoded DSN "
                f"construction — should call _get_dsn()",
            )
            self.assertNotIn(
                "net.db_host",
                source,
                f"_BulbDB.{method_name}() still references net.db_host "
                f"directly — should call _get_dsn()",
            )

    def test_reconnect_attempts_is_named_constant(self) -> None:
        """The retry count in record() must use a named constant."""
        from bulb_keepalive import _BulbDB
        self.assertTrue(
            hasattr(_BulbDB, "_RECONNECT_ATTEMPTS"),
            "_BulbDB missing _RECONNECT_ATTEMPTS constant",
        )
        self.assertIsInstance(_BulbDB._RECONNECT_ATTEMPTS, int)
        self.assertGreaterEqual(_BulbDB._RECONNECT_ATTEMPTS, 1)


# ---------------------------------------------------------------------------
# Test: theremin — create_mqtt_client shared factory
# ---------------------------------------------------------------------------

try:
    import paho.mqtt.client  # noqa: F401
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False


@unittest.skipUnless(_HAS_PAHO, "paho-mqtt not installed")
class TestCreateMqttClient(unittest.TestCase):
    """Verify the paho v2 detection is consolidated in create_mqtt_client."""

    def test_factory_exists(self) -> None:
        """create_mqtt_client must be importable from theremin."""
        from theremin import create_mqtt_client
        self.assertTrue(callable(create_mqtt_client))

    def test_returns_mqtt_client(self) -> None:
        """Factory must return a paho mqtt.Client instance."""
        from theremin import create_mqtt_client
        import paho.mqtt.client as mqtt
        client: mqtt.Client = create_mqtt_client("test-client")
        self.assertIsInstance(client, mqtt.Client)

    def test_no_paho_v2_in_theremin_modules(self) -> None:
        """Theremin submodules must not define their own _PAHO_V2."""
        import importlib
        for mod_name in ("theremin.display", "theremin.synth",
                         "theremin.simulator"):
            mod = importlib.import_module(mod_name)
            self.assertFalse(
                hasattr(mod, "_PAHO_V2"),
                f"{mod_name} still defines _PAHO_V2 — "
                f"should use create_mqtt_client from theremin",
            )

    def test_no_inline_callbackapiversion_in_theremin_modules(self) -> None:
        """Theremin submodules must not reference CallbackAPIVersion."""
        import inspect
        import importlib
        for mod_name in ("theremin.display", "theremin.synth",
                         "theremin.simulator"):
            mod = importlib.import_module(mod_name)
            source: str = inspect.getsource(mod)
            self.assertNotIn(
                "CallbackAPIVersion",
                source,
                f"{mod_name} still references CallbackAPIVersion "
                f"directly — should use create_mqtt_client",
            )

    def test_no_paho_v2_in_source(self) -> None:  # noqa: E301
        """Theremin submodule source files must not contain _PAHO_V2."""
        for filename in ("display.py", "synth.py", "simulator.py"):
            filepath: str = os.path.join(
                os.path.dirname(__file__), "theremin", filename,
            )
            with open(filepath, "r") as f:
                source: str = f.read()
            # Exclude comments/docstrings mentioning _PAHO_V2 by
            # checking for the assignment pattern.
            self.assertNotIn(
                "_PAHO_V2:",
                source,
                f"theremin/{filename} still defines _PAHO_V2 variable",
            )


# ---------------------------------------------------------------------------
# Test: engine.py — bare exceptions replaced with logged warnings
# ---------------------------------------------------------------------------

class TestEngineLoggerExists(unittest.TestCase):
    """Verify engine.py has a proper logger for error reporting."""

    def test_engine_has_logger(self) -> None:
        """engine module must define _log as a Logger."""
        import engine
        self.assertTrue(
            hasattr(engine, "_log"),
            "engine module missing '_log' attribute",
        )
        self.assertIsInstance(engine._log, logging.Logger)

    def test_engine_has_exc_oneliner(self) -> None:
        """engine module must define _exc_oneliner helper."""
        import engine
        self.assertTrue(
            hasattr(engine, "_exc_oneliner"),
            "engine module missing '_exc_oneliner' helper",
        )
        self.assertTrue(callable(engine._exc_oneliner))


class TestEngineNoBareExcepts(unittest.TestCase):
    """Verify engine.py render loop no longer silently swallows exceptions."""

    def test_no_except_pass_in_render_loop(self) -> None:
        """_run_loop must not contain 'except Exception' followed by 'pass'."""
        import inspect
        from engine import Engine
        source: str = inspect.getsource(Engine._run_loop)
        # Find 'except Exception:' lines not followed by logging.
        import re
        bare_excepts: list[str] = re.findall(
            r"except Exception:\s*\n\s*pass",
            source,
        )
        self.assertEqual(
            len(bare_excepts), 0,
            f"Found {len(bare_excepts)} bare 'except Exception: pass' "
            f"blocks in _run_loop — should log warnings",
        )

    def test_no_except_pass_in_resolve_bindings(self) -> None:
        """_resolve_bindings must not contain 'except Exception' + 'pass'."""
        import inspect
        from engine import Engine
        source: str = inspect.getsource(Engine._resolve_bindings)
        import re
        bare_excepts: list[str] = re.findall(
            r"except Exception:\s*\n\s*pass",
            source,
        )
        self.assertEqual(
            len(bare_excepts), 0,
            f"Found {len(bare_excepts)} bare 'except Exception: pass' "
            f"blocks in _resolve_bindings — should log warnings",
        )

    def test_render_thread_logs_on_render_error(self) -> None:
        """_render_thread source must log on render exception."""
        import inspect
        from engine import Engine
        source: str = inspect.getsource(Engine._render_thread)
        self.assertIn(
            "Render failed",
            source,
            "_render_thread should log 'Render failed' on exception",
        )

    def test_run_loop_logs_on_send_error(self) -> None:
        """_run_loop source must log on send exception."""
        import inspect
        from engine import Engine
        source: str = inspect.getsource(Engine._run_loop)
        self.assertIn(
            "Send failed",
            source,
            "_run_loop should log 'Send failed' on exception",
        )

    def test_run_loop_logs_on_callback_error(self) -> None:
        """_run_loop source must log on frame callback exception."""
        import inspect
        from engine import Engine
        source: str = inspect.getsource(Engine._run_loop)
        self.assertIn(
            "Frame callback failed",
            source,
            "_run_loop should log 'Frame callback failed' on exception",
        )

    def test_resolve_bindings_logs_on_setattr_error(self) -> None:
        """_resolve_bindings must log when setattr fails."""
        from engine import Engine

        # Effect where setattr raises for a specific param.
        class _BrokenEffect:
            _param_defs = {}

            def __setattr__(self, name: str, value: Any) -> None:
                if name == "speed":
                    raise AttributeError("read-only")
                super().__setattr__(name, value)

        effect = _BrokenEffect()
        bindings: dict = {
            "speed": {"signal": "audio.rms", "mode": "latest"},
        }

        # Mock signal bus with a .read() method.
        mock_bus = MagicMock()
        mock_bus.read.return_value = 0.5

        with self.assertLogs("glowup.engine", level=logging.WARNING) as cm:
            Engine._resolve_bindings(effect, bindings, mock_bus)

        found: bool = any("Failed to set param" in msg for msg in cm.output)
        self.assertTrue(
            found,
            f"Expected 'Failed to set param' warning, got: {cm.output}",
        )


# ---------------------------------------------------------------------------
# Test: transport.py set_label — bare except replaced, timeout restored
# ---------------------------------------------------------------------------

class TestSetLabelExceptionHandling(unittest.TestCase):
    """Verify set_label logs errors and restores socket timeout."""

    def test_no_bare_except_in_set_label(self) -> None:
        """set_label must not contain 'except Exception' + 'pass'."""
        import inspect
        from transport import LifxDevice
        source: str = inspect.getsource(LifxDevice.set_label)
        import re
        bare_excepts: list[str] = re.findall(
            r"except Exception:\s*\n\s*pass",
            source,
        )
        self.assertEqual(
            len(bare_excepts), 0,
            "set_label still has bare 'except Exception: pass'",
        )

    def test_set_label_has_finally_block(self) -> None:
        """set_label must use finally to restore socket timeout."""
        import inspect
        from transport import LifxDevice
        source: str = inspect.getsource(LifxDevice.set_label)
        self.assertIn(
            "finally",
            source,
            "set_label missing 'finally' block for timeout restoration",
        )

    def test_set_label_logs_on_exception(self) -> None:
        """set_label must reference _log.warning for error reporting."""
        import inspect
        from transport import LifxDevice
        source: str = inspect.getsource(LifxDevice.set_label)
        self.assertIn(
            "_log.warning",
            source,
            "set_label should log warnings on exception",
        )


# ---------------------------------------------------------------------------
# Test: bulb_keepalive.py close — bare except replaced with debug log
# ---------------------------------------------------------------------------

class TestBulbDBCloseLogging(unittest.TestCase):
    """Verify BulbDB.close() logs instead of silently swallowing."""

    def test_close_no_bare_except(self) -> None:
        """close() must not contain 'except Exception' + 'pass'."""
        import inspect
        from bulb_keepalive import _BulbDB
        source: str = inspect.getsource(_BulbDB.close)
        import re
        bare_excepts: list[str] = re.findall(
            r"except Exception:\s*\n\s*pass",
            source,
        )
        self.assertEqual(
            len(bare_excepts), 0,
            "close() still has bare 'except Exception: pass'",
        )

    def test_close_logs_debug_on_failure(self) -> None:
        """close() should log at debug level if conn.close() fails."""
        import inspect
        from bulb_keepalive import _BulbDB
        source: str = inspect.getsource(_BulbDB.close)
        self.assertIn(
            "logger.debug",
            source,
            "close() should log at debug level on exception",
        )


# ---------------------------------------------------------------------------
# Test: server.py — consistent error handling in handlers
# ---------------------------------------------------------------------------

class TestServerHandlerErrorConsistency(unittest.TestCase):
    """Verify server handler error handling is consistent."""

    def test_power_on_before_play_logs_on_failure(self) -> None:
        """power_on() before play must log, not silently pass."""
        import inspect
        # DeviceManager.play is where power_on happens before effect start.
        from server import DeviceManager
        source: str = inspect.getsource(DeviceManager.play)
        # Should NOT have bare 'except Exception: pass'.
        import re
        bare: list[str] = re.findall(
            r"except Exception:\s*\n\s*pass", source,
        )
        self.assertEqual(
            len(bare), 0,
            "DeviceManager.play still has silent 'except Exception: pass' "
            "on power_on — should log warning",
        )

    def test_stop_and_remove_logs_on_failure(self) -> None:
        """_stop_and_remove must log, not silently pass."""
        import inspect
        from server import DeviceManager
        source: str = inspect.getsource(DeviceManager._stop_and_remove)
        import re
        bare: list[str] = re.findall(
            r"except Exception:\s*\n\s*pass", source,
        )
        self.assertEqual(
            len(bare), 0,
            "_stop_and_remove still has silent 'except Exception: pass' "
            "on ctrl.stop — should log warning",
        )

    def test_effect_defaults_returns_400_not_404(self) -> None:
        """save_effect_defaults ValueError should return 400, not 404."""
        import inspect
        from server import GlowUpRequestHandler
        source: str = inspect.getsource(
            GlowUpRequestHandler._handle_post_effect_defaults,
        )
        # Should NOT return 404 for a ValueError.
        self.assertNotIn(
            "404",
            source,
            "_handle_post_effect_defaults returns 404 for ValueError — "
            "should return 400 (bad request, not not-found)",
        )
        # Should return 400.
        self.assertIn(
            "400",
            source,
            "_handle_post_effect_defaults should return 400 for ValueError",
        )


# ---------------------------------------------------------------------------
# Test: glowup.py cmd_off — no raw protocol in client code
# ---------------------------------------------------------------------------

class TestCmdOffNoRawProtocol(unittest.TestCase):
    """Verify cmd_off delegates to transport instead of building raw frames."""

    def test_no_struct_pack_in_cmd_off(self) -> None:
        """cmd_off must not contain struct.pack — uses broadcast_power_off."""
        import inspect
        from glowup import cmd_off
        source: str = inspect.getsource(cmd_off)
        self.assertNotIn(
            "struct.pack",
            source,
            "cmd_off still builds raw LIFX frames with struct.pack — "
            "should call transport.broadcast_power_off()",
        )

    def test_no_magic_numbers_in_cmd_off(self) -> None:
        """cmd_off must not contain hardcoded protocol constants."""
        import inspect
        from glowup import cmd_off
        source: str = inspect.getsource(cmd_off)
        for magic in ("56700", "MSG_LIGHT_SET_POWER", "117"):
            self.assertNotIn(
                magic,
                source,
                f"cmd_off still contains magic number {magic} — "
                f"should delegate to transport layer",
            )

    def test_broadcast_power_off_exists(self) -> None:
        """transport.broadcast_power_off must be importable."""
        from transport import broadcast_power_off
        self.assertTrue(callable(broadcast_power_off))

    def test_broadcast_power_off_builds_correct_payload(self) -> None:
        """broadcast_power_off must send a SetPower(off) payload."""
        import inspect
        from transport import broadcast_power_off
        source: str = inspect.getsource(broadcast_power_off)
        self.assertIn("POWER_OFF", source)
        self.assertIn("MSG_LIGHT_SET_POWER", source)
        self.assertIn("_build_header", source)


# ---------------------------------------------------------------------------
# Test: _exc_oneliner helper
# ---------------------------------------------------------------------------

class TestExcOneliner(unittest.TestCase):
    """Verify _exc_oneliner produces correct exception summaries."""

    def test_returns_string_in_except_block(self) -> None:
        """Inside an except block, should return 'Type: message'."""
        from engine import _exc_oneliner
        try:
            raise ValueError("test message")
        except ValueError:
            result: str = _exc_oneliner()
        self.assertEqual(result, "ValueError: test message")

    def test_returns_unknown_outside_except(self) -> None:
        """Outside an except block, should return 'unknown error'."""
        from engine import _exc_oneliner
        result: str = _exc_oneliner()
        self.assertEqual(result, "unknown error")

    def test_handles_nested_exceptions(self) -> None:
        """Should report the innermost exception."""
        from engine import _exc_oneliner
        try:
            try:
                raise OSError("disk full")
            except OSError:
                raise RuntimeError("wrapped") from None
        except RuntimeError:
            result: str = _exc_oneliner()
        self.assertEqual(result, "RuntimeError: wrapped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
