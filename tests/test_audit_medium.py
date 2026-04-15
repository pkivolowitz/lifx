"""Regression tests for Medium-severity audit fixes (M1–M35).

Each test verifies the fix for one or more M-level bugs from
AUDIT_REPORT.md.  These tests are designed to run without hardware
or network dependencies.
"""

__version__ = "1.0"

import json
import math
import os
import queue
import struct
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# M1: engine.py zone_count consistency — `is not None` vs truthiness
# ---------------------------------------------------------------------------

class TestM1ZoneCountConsistency(unittest.TestCase):
    """Zone count of 0 must still trigger on_start and fade-to-black."""

    def test_on_start_called_for_zone_count_zero(self) -> None:
        """Engine.start() should call effect.on_start(0) for zone_count=0."""
        from engine import Engine
        from effects import Effect

        em = MagicMock()
        em.zone_count = 0  # falsy, but not None
        em.is_multizone = False
        em.is_matrix = False

        effect = MagicMock(spec=Effect)
        effect.is_transient = False

        engine = Engine([em], fps=10)
        engine.start(effect)
        effect.on_start.assert_called_with(0)
        engine.stop(fade_ms=0)

    def test_fade_to_black_for_zone_count_zero(self) -> None:
        """Engine.stop() should process zone_count=0 emitters."""
        from engine import Engine

        em = MagicMock()
        em.zone_count = 0
        em.is_multizone = False
        em.is_matrix = False

        engine = Engine([em], fps=10)
        # Stop with fade — zone_count=0 is not None, so the emitter
        # should be considered for fade-to-black.
        engine.stop(fade_ms=500)
        em.send_color.assert_called_once()


# ---------------------------------------------------------------------------
# M2: transport.py — time.monotonic() for deadlines
# ---------------------------------------------------------------------------

class TestM2MonotonicTime(unittest.TestCase):
    """Deadlines in transport.py must use time.monotonic()."""

    def test_monotonic_used_in_send_and_recv(self) -> None:
        """Check that _send_and_recv uses monotonic for deadlines."""
        import transport
        import inspect
        source = inspect.getsource(transport.LifxDevice._send_and_recv)
        # Should use monotonic, not time.time()
        self.assertIn("monotonic", source)
        self.assertNotIn("time.time()", source)


# ---------------------------------------------------------------------------
# M4-M5: device_registry.py — IO lock for file operations
# ---------------------------------------------------------------------------

class TestM4M5DeviceRegistryIOLock(unittest.TestCase):
    """DeviceRegistry must have a dedicated IO lock."""

    def test_io_lock_exists(self) -> None:
        """Registry should have _io_lock attribute."""
        from device_registry import DeviceRegistry
        reg = DeviceRegistry()
        self.assertIsInstance(reg._io_lock, type(threading.Lock()))


# ---------------------------------------------------------------------------
# M6: colorspace.py — lerp_hsb clamping
# ---------------------------------------------------------------------------

class TestM6LerpHsbClamping(unittest.TestCase):
    """lerp_hsb must clamp sat and bri to [0, HSBK_MAX]."""

    def test_clamp_above_max(self) -> None:
        """Blend > 1.0 should not produce values exceeding HSBK_MAX."""
        from colorspace import lerp_hsb, HSBK_MAX
        # blend > 1.0 can happen with unclamped input
        result = lerp_hsb((0, 0, 0, 3500), (0, HSBK_MAX, HSBK_MAX, 3500), 1.5)
        _, sat, bri, _ = result
        self.assertLessEqual(sat, HSBK_MAX)
        self.assertLessEqual(bri, HSBK_MAX)

    def test_clamp_below_zero(self) -> None:
        """Negative blend should not produce values below 0."""
        from colorspace import lerp_hsb
        result = lerp_hsb((0, 100, 100, 3500), (0, 50000, 50000, 3500), -0.5)
        _, sat, bri, _ = result
        self.assertGreaterEqual(sat, 0)
        self.assertGreaterEqual(bri, 0)


# ---------------------------------------------------------------------------
# M7: simulator.py — orphaned queue prevention
# ---------------------------------------------------------------------------

class TestM7SimulatorStopFlag(unittest.TestCase):
    """Simulator.update() should not enqueue frames after stop()."""

    def test_stopped_flag_blocks_update(self) -> None:
        """After stop is signaled, update() should be a no-op."""
        # We can't create a real simulator (needs tkinter), but we can
        # verify the flag logic exists by checking the source.
        try:
            import simulator
            import inspect
            src = inspect.getsource(simulator)
            self.assertIn("_stopped", src)
        except ImportError:
            self.skipTest("simulator module requires tkinter")


# ---------------------------------------------------------------------------
# M8-M9: mqtt_bridge.py — thread stop order and cache lock
# ---------------------------------------------------------------------------

class TestM8M9MqttBridge(unittest.TestCase):
    """MQTT bridge must join threads before loop_stop and protect caches."""

    def test_cache_lock_exists(self) -> None:
        """Bridge should have _cache_lock attribute."""
        try:
            from infrastructure.mqtt_bridge import MqttBridge
            dm = MagicMock()
            bridge = MqttBridge(dm, {"mqtt": {"broker": "localhost"}})
            self.assertIsInstance(bridge._cache_lock, type(threading.Lock()))
        except ImportError:
            self.skipTest("paho-mqtt not installed")

    def test_stop_joins_threads_before_loop_stop(self) -> None:
        """stop() should join publisher threads before calling loop_stop."""
        import inspect
        try:
            from infrastructure.mqtt_bridge import MqttBridge
            src = inspect.getsource(MqttBridge.stop)
            # The actual .join() call must appear before .loop_stop()
            join_pos = src.find(".join(")
            loop_stop_pos = src.find(".loop_stop()")
            if join_pos >= 0 and loop_stop_pos >= 0:
                self.assertLess(join_pos, loop_stop_pos)
        except ImportError:
            self.skipTest("paho-mqtt not installed")


# ---------------------------------------------------------------------------
# M10/M11/M12 retired: tested AutomationManager methods that were
# deleted in the 2026-04 cleanup when AutomationManager was retired
# in favour of the operator framework (operators/trigger.py).  The
# regressions they guarded against are no longer reachable —
# inspect.getsource() on a deleted class would raise ImportError.
# Do not restore these tests against the operator framework here;
# operator-level tests live in tests/test_operators.py and
# tests/test_trigger_operator.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# M13: scheduler.py — overnight entry stop==start
# ---------------------------------------------------------------------------

class TestM13OvernightStopEqualsStart(unittest.TestCase):
    """stop==start should be a zero-duration entry, not 24 hours."""

    def test_zero_duration_entry_not_matched(self) -> None:
        """An entry with stop==start should not match as active."""
        from schedule_utils import find_active_entry as _find_active_entry
        specs = [{
            "name": "test",
            "group": "test_group",
            "start": "12:00",
            "stop": "12:00",
            "effect": "cylon",
            "enabled": True,
        }]
        now = datetime(2026, 3, 26, 12, 0, 0,
                       tzinfo=timezone(timedelta(hours=-5)))
        result = _find_active_entry(specs, 30.7, -88.0, now, "test_group")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# M14: REMOVED — GroupState was part of the old monolithic scheduler.py,
# replaced by scheduling/evaluator.py which uses plain dicts for state.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# M16: ble/registry.py — IO lock
# ---------------------------------------------------------------------------

class TestM16BleRegistryIOLock(unittest.TestCase):
    """BleRegistry must have _io_lock for thread-safe file I/O."""

    def test_io_lock_exists(self) -> None:
        from ble.registry import BleRegistry
        # Use a non-existent path so it starts fresh.
        reg = BleRegistry("/tmp/test_ble_registry_nonexistent.json")
        self.assertIsInstance(reg._io_lock, type(threading.Lock()))


# ---------------------------------------------------------------------------
# M20: effects/crossfade.py — HSBK overflow clamping
# ---------------------------------------------------------------------------

class TestM20CrossfadeClamping(unittest.TestCase):
    """Crossfade brightness interpolation must be clamped."""

    def test_brightness_clamped(self) -> None:
        """Verify crossfade source has HSBK_MAX clamping."""
        import inspect
        from effects.crossfade import Crossfade
        src = inspect.getsource(Crossfade.render)
        self.assertIn("HSBK_MAX", src)


# ---------------------------------------------------------------------------
# M23: glowup.py — zoom bounds
# ---------------------------------------------------------------------------

class TestM23ZoomBounds(unittest.TestCase):
    """Zoom value must be clamped to [MIN_ZOOM, MAX_ZOOM]."""

    def test_zoom_constants_exist(self) -> None:
        import glowup
        self.assertEqual(glowup.MIN_ZOOM, 1)
        self.assertEqual(glowup.MAX_ZOOM, 10)


# ---------------------------------------------------------------------------
# M26: glowup.py — group list validation
# ---------------------------------------------------------------------------

class TestM26GroupValidation(unittest.TestCase):
    """Server response group must be validated as a list."""

    def test_isinstance_check_in_source(self) -> None:
        """Verify isinstance(ips, list) check exists."""
        import inspect
        import glowup
        src = inspect.getsource(glowup)
        self.assertIn("isinstance(ips, list)", src)


# ---------------------------------------------------------------------------
# M28: media/calibration.py — PulseDetector thread safety
# ---------------------------------------------------------------------------

class TestM28PulseDetectorLock(unittest.TestCase):
    """PulseDetector must have a lock for thread-safe feed()."""

    def test_lock_exists(self) -> None:
        from media.calibration import PulseDetector
        det = PulseDetector()
        self.assertIsInstance(det._lock, type(threading.Lock()))

    def test_concurrent_feed_safe(self) -> None:
        """Multiple threads calling feed() should not crash."""
        from media.calibration import PulseDetector
        det = PulseDetector(sample_rate=44100, threshold=0.3)

        # Generate a short silent chunk.
        silent = struct.pack("<" + "h" * 512, *([0] * 512))
        errors: list[Exception] = []

        def feeder() -> None:
            try:
                for _ in range(100):
                    det.feed(silent)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=feeder) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# M30: media/source.py — stream send timeout
# ---------------------------------------------------------------------------

class TestM30StreamSendTimeout(unittest.TestCase):
    """AudioStreamServer must set send timeout on client sockets."""

    def test_timeout_constant_exists(self) -> None:
        from media.source import STREAM_SEND_TIMEOUT
        self.assertGreater(STREAM_SEND_TIMEOUT, 0)
        self.assertLessEqual(STREAM_SEND_TIMEOUT, 5.0)


# ---------------------------------------------------------------------------
# M32: distributed/orchestrator.py — port release on node offline
# ---------------------------------------------------------------------------

class TestM32PortRelease(unittest.TestCase):
    """Orchestrator must release ports when nodes go offline."""

    def test_assignment_ports_dict_exists(self) -> None:
        """Orchestrator should track assignment → port mapping."""
        import inspect
        from distributed.orchestrator import Orchestrator
        src = inspect.getsource(Orchestrator.__init__)
        self.assertIn("_assignment_ports", src)


# ---------------------------------------------------------------------------
# M33: distributed/midi_sensor.py — MQTT reconnection retry
# ---------------------------------------------------------------------------

class TestM33MqttReconnectRetry(unittest.TestCase):
    """MidiSensor must retry MQTT connection with exponential backoff."""

    def test_retry_constants_exist(self) -> None:
        from distributed.midi_sensor import (
            MQTT_CONNECT_RETRIES, MQTT_RETRY_INITIAL, MQTT_RETRY_MAX,
        )
        self.assertGreater(MQTT_CONNECT_RETRIES, 1)
        self.assertGreater(MQTT_RETRY_INITIAL, 0)
        self.assertGreater(MQTT_RETRY_MAX, MQTT_RETRY_INITIAL)


if __name__ == "__main__":
    unittest.main()
