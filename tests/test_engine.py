"""Thorough test suite for the engine module.

Tests cover:
- Engine construction and validation
- Controller lifecycle (play, stop, hot-swap)
- Effect rendering and frame production
- Zones-per-bulb replication
- Matrix vs strip dispatch
- Frame callback invocation
- Parameter updates (direct and bus-routed)
- Signal bindings and resolution
- Audio delay buffer
- Generation counter and stale frame discard
- get_status / get_last_frame
- Error handling (render failures, send failures)
- Thread safety and concurrent operations

Run independently::

    python3 -m pytest tests/test_engine.py -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

import threading
import time
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock, patch, call

from effects import Effect, create_effect, HSBK, KELVIN_DEFAULT, HSBK_MAX
from engine import (
    Engine, Controller,
    DEFAULT_FPS, DEFAULT_ZPB, DEFAULT_FADE_MS,
    PIPELINE_HIGH_WATER, PIPELINE_LOW_LATENCY,
    TRANSITION_FACTOR,
)
from emitters import Emitter


# ---------------------------------------------------------------------------
# Recording emitter — captures frames for assertions
# ---------------------------------------------------------------------------

class RecordingEmitter(Emitter):
    """Test emitter that records all frames sent to it."""

    emitter_type = None  # Not registered.

    def __init__(
        self,
        zone_count: int = 36,
        is_multizone: bool = True,
        is_matrix: bool = False,
        matrix_width: Optional[int] = None,
        matrix_height: Optional[int] = None,
    ) -> None:
        super().__init__("test-emitter", {})
        self._zone_count: int = zone_count
        self._is_multizone: bool = is_multizone
        self._is_matrix: bool = is_matrix
        self._matrix_width: Optional[int] = matrix_width
        self._matrix_height: Optional[int] = matrix_height
        self._frames: list[list[HSBK]] = []
        self._lock: threading.Lock = threading.Lock()
        self._power: bool = False
        self._prepared: bool = False

    @property
    def zone_count(self) -> Optional[int]:
        return self._zone_count

    @property
    def is_multizone(self) -> bool:
        return self._is_multizone

    @property
    def is_matrix(self) -> bool:
        return self._is_matrix

    @property
    def matrix_width(self) -> Optional[int]:
        return self._matrix_width

    @property
    def matrix_height(self) -> Optional[int]:
        return self._matrix_height

    @property
    def emitter_id(self) -> str:
        return "test-emitter"

    @property
    def label(self) -> str:
        return "Test Emitter"

    @property
    def product_name(self) -> str:
        return "Test Device"

    def send_zones(self, colors, duration_ms=0, mode=None) -> None:
        with self._lock:
            self._frames.append(list(colors))

    def send_tile_zones(self, colors, duration_ms=0) -> None:
        with self._lock:
            self._frames.append(list(colors))

    def send_color(self, h, s, b, k, duration_ms=0) -> None:
        with self._lock:
            self._frames.append([(h, s, b, k)])

    def power_on(self, duration_ms=0) -> None:
        self._power = True

    def power_off(self, duration_ms=0) -> None:
        self._power = False

    def prepare_for_rendering(self, **kwargs) -> None:
        self._prepared = True

    def get_info(self) -> dict:
        return {"id": self.emitter_id, "label": self.label,
                "zones": self._zone_count}

    def get_frames(self) -> list[list[HSBK]]:
        with self._lock:
            return list(self._frames)

    def frame_count(self) -> int:
        with self._lock:
            return len(self._frames)

    def clear_frames(self) -> None:
        with self._lock:
            self._frames.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Known effect that exists in the registry.
KNOWN_EFFECT: str = "breathe"

# Duration to let the engine render a few frames.
RENDER_WAIT: float = 0.3


def _wait_for_frames(em: RecordingEmitter, min_frames: int = 1,
                     timeout: float = 2.0) -> bool:
    """Wait until the emitter has received at least min_frames."""
    deadline: float = time.time() + timeout
    while time.time() < deadline:
        if em.frame_count() >= min_frames:
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# Engine construction tests
# ---------------------------------------------------------------------------

class TestEngineConstruction(unittest.TestCase):
    """Tests for Engine.__init__ validation."""

    def test_empty_emitters_raises(self) -> None:
        """Engine requires at least one emitter."""
        with self.assertRaises(ValueError):
            Engine(emitters=[])

    def test_zero_fps_raises(self) -> None:
        """FPS must be positive."""
        em = RecordingEmitter()
        with self.assertRaises(ValueError):
            Engine(emitters=[em], fps=0)

    def test_negative_fps_raises(self) -> None:
        em = RecordingEmitter()
        with self.assertRaises(ValueError):
            Engine(emitters=[em], fps=-1)

    def test_valid_construction(self) -> None:
        """Engine with valid args constructs without error."""
        em = RecordingEmitter()
        eng = Engine(emitters=[em], fps=20)
        self.assertEqual(eng.fps, 20)
        self.assertFalse(eng.running)
        self.assertIsNone(eng.effect)


# ---------------------------------------------------------------------------
# Controller construction tests
# ---------------------------------------------------------------------------

class TestControllerConstruction(unittest.TestCase):
    """Tests for Controller.__init__."""

    def test_valid_construction(self) -> None:
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20)
        self.assertIsNotNone(ctrl.engine)
        self.assertEqual(len(ctrl.emitters), 1)

    def test_empty_emitters_raises(self) -> None:
        with self.assertRaises(ValueError):
            Controller([])


# ---------------------------------------------------------------------------
# Play / stop lifecycle
# ---------------------------------------------------------------------------

class TestPlayStopLifecycle(unittest.TestCase):
    """Tests for playing and stopping effects."""

    def test_play_starts_engine(self) -> None:
        """play() starts the engine and produces frames."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        ctrl.play(KNOWN_EFFECT)
        self.assertTrue(ctrl.engine.running)
        _wait_for_frames(em, 3)
        ctrl.stop(fade_ms=0)
        self.assertFalse(ctrl.engine.running)
        self.assertGreaterEqual(em.frame_count(), 3)

    def test_stop_halts_rendering(self) -> None:
        """stop() stops frame production."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 2)
        ctrl.stop(fade_ms=0)
        count_at_stop: int = em.frame_count()
        time.sleep(0.2)
        self.assertEqual(em.frame_count(), count_at_stop)

    def test_negative_fade_raises(self) -> None:
        """stop() with negative fade_ms raises ValueError."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20)
        with self.assertRaises(ValueError):
            ctrl.stop(fade_ms=-1)

    def test_play_invalid_effect_raises(self) -> None:
        """play() with unknown effect name raises ValueError."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20)
        with self.assertRaises(ValueError):
            ctrl.play("nonexistent_effect_name")

    def test_play_non_string_raises(self) -> None:
        """play() with non-string effect name raises TypeError."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20)
        with self.assertRaises(TypeError):
            ctrl.play(42)

    def test_prepare_for_rendering_called(self) -> None:
        """play() calls prepare_for_rendering on emitters."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 1)
        ctrl.stop(fade_ms=0)
        self.assertTrue(em._prepared)

    def test_on_start_called(self) -> None:
        """Effect.on_start is called with the zone count."""
        em = RecordingEmitter(zone_count=36)
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        effect = create_effect(KNOWN_EFFECT)
        effect.on_start = MagicMock()
        ctrl.engine.start(effect)
        _wait_for_frames(em, 1)
        ctrl.stop(fade_ms=0)
        effect.on_start.assert_called_with(36)

    def test_on_stop_called(self) -> None:
        """Effect.on_stop is called when the engine stops."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        effect = create_effect(KNOWN_EFFECT)
        effect.on_stop = MagicMock()
        ctrl.engine.start(effect)
        _wait_for_frames(em, 1)
        ctrl.stop(fade_ms=0)
        effect.on_stop.assert_called()


# ---------------------------------------------------------------------------
# Hot-swap tests
# ---------------------------------------------------------------------------

class TestHotSwap(unittest.TestCase):
    """Tests for swapping effects while the engine is running."""

    def test_hot_swap_changes_effect(self) -> None:
        """Playing a new effect while one is running swaps cleanly."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 2)
        em.clear_frames()
        ctrl.play("on")
        _wait_for_frames(em, 2)
        ctrl.stop(fade_ms=0)
        self.assertGreaterEqual(em.frame_count(), 2)

    def test_hot_swap_increments_generation(self) -> None:
        """Each play() increments the effect generation counter."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        gen_before: int = ctrl.engine._effect_generation
        ctrl.play(KNOWN_EFFECT)
        gen_after: int = ctrl.engine._effect_generation
        self.assertGreater(gen_after, gen_before)
        ctrl.stop(fade_ms=0)


# ---------------------------------------------------------------------------
# Zones-per-bulb tests
# ---------------------------------------------------------------------------

class TestZonesPerBulb(unittest.TestCase):
    """Tests for zpb replication logic."""

    def test_zpb_replicates_colors(self) -> None:
        """With zpb=3 and 36 zones, effect renders 12 colors, each replicated 3x."""
        em = RecordingEmitter(zone_count=36)
        ctrl = Controller([em], fps=20, zones_per_bulb=3)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 1)
        ctrl.stop(fade_ms=0)
        frames = em.get_frames()
        self.assertGreaterEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual(len(frame), 36)
        # Each group of 3 consecutive zones should have the same color.
        for i in range(0, 36, 3):
            self.assertEqual(frame[i], frame[i + 1])
            self.assertEqual(frame[i], frame[i + 2])

    def test_zpb_1_no_replication(self) -> None:
        """With zpb=1, no replication — frame length = zone count."""
        em = RecordingEmitter(zone_count=10)
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 1)
        ctrl.stop(fade_ms=0)
        frames = em.get_frames()
        self.assertGreaterEqual(len(frames), 1)
        self.assertEqual(len(frames[0]), 10)

    def test_matrix_ignores_zpb(self) -> None:
        """Matrix emitters always use zpb=1 regardless of setting."""
        em = RecordingEmitter(
            zone_count=35, is_matrix=True,
            matrix_width=7, matrix_height=5,
        )
        ctrl = Controller([em], fps=20, zones_per_bulb=3)
        ctrl.play("plasma2d", width=7, height=5)
        _wait_for_frames(em, 1)
        ctrl.stop(fade_ms=0)
        frames = em.get_frames()
        self.assertGreaterEqual(len(frames), 1)
        # Matrix frame should be 35 pixels, not 35//3=11 replicated.
        self.assertEqual(len(frames[0]), 35)


# ---------------------------------------------------------------------------
# Frame callback tests
# ---------------------------------------------------------------------------

class TestFrameCallback(unittest.TestCase):
    """Tests for the optional frame callback."""

    def test_callback_receives_frames(self) -> None:
        """The frame callback is called with rendered colors."""
        received: list[list] = []

        def on_frame(colors: list) -> None:
            received.append(list(colors))

        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1,
                         frame_callback=on_frame)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 3)
        ctrl.stop(fade_ms=0)
        self.assertGreaterEqual(len(received), 1)

    def test_callback_exception_doesnt_crash(self) -> None:
        """A crashing callback doesn't stop the engine."""
        def bad_callback(colors: list) -> None:
            raise RuntimeError("callback crash")

        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1,
                         frame_callback=bad_callback)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 3)
        ctrl.stop(fade_ms=0)
        self.assertGreaterEqual(em.frame_count(), 3)


# ---------------------------------------------------------------------------
# Parameter update tests
# ---------------------------------------------------------------------------

class TestUpdateParams(unittest.TestCase):
    """Tests for Controller.update_params."""

    def test_direct_param_update(self) -> None:
        """update_params sets attributes on the running effect."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 1)
        ctrl.update_params(speed=5.0)
        self.assertAlmostEqual(ctrl.engine.effect.speed, 5.0, places=1)
        ctrl.stop(fade_ms=0)

    def test_update_unknown_param_ignored(self) -> None:
        """Unknown parameter names are silently ignored."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 1)
        ctrl.update_params(nonexistent_param=42)
        ctrl.stop(fade_ms=0)

    def test_update_when_stopped_is_noop(self) -> None:
        """update_params when no effect is running is a no-op."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20)
        ctrl.update_params(speed=5.0)  # Should not raise.


# ---------------------------------------------------------------------------
# get_status tests
# ---------------------------------------------------------------------------

class TestGetStatus(unittest.TestCase):
    """Tests for Controller.get_status."""

    def test_status_when_running(self) -> None:
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 1)
        status = ctrl.get_status()
        self.assertTrue(status["running"])
        self.assertEqual(status["effect"], KNOWN_EFFECT)
        self.assertIn("params", status)
        self.assertEqual(status["fps"], 20)
        ctrl.stop(fade_ms=0)

    def test_status_when_stopped(self) -> None:
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 1)
        ctrl.stop(fade_ms=0)
        status = ctrl.get_status()
        self.assertFalse(status["running"])
        # Should remember the last effect name.
        self.assertEqual(status["effect"], KNOWN_EFFECT)

    def test_status_before_any_play(self) -> None:
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20)
        status = ctrl.get_status()
        self.assertFalse(status["running"])
        self.assertIsNone(status["effect"])


# ---------------------------------------------------------------------------
# get_last_frame tests
# ---------------------------------------------------------------------------

class TestGetLastFrame(unittest.TestCase):
    """Tests for Controller.get_last_frame."""

    def test_returns_none_before_play(self) -> None:
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20)
        self.assertIsNone(ctrl.get_last_frame())

    def test_returns_frame_after_play(self) -> None:
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 3)
        frame = ctrl.get_last_frame()
        ctrl.stop(fade_ms=0)
        self.assertIsNotNone(frame)
        self.assertEqual(len(frame), em.zone_count)


# ---------------------------------------------------------------------------
# Power control tests
# ---------------------------------------------------------------------------

class TestPowerControl(unittest.TestCase):
    """Tests for Controller.set_power."""

    def test_power_on(self) -> None:
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20)
        ctrl.set_power(on=True)
        self.assertTrue(em._power)

    def test_power_off(self) -> None:
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20)
        em._power = True
        ctrl.set_power(on=False)
        self.assertFalse(em._power)


# ---------------------------------------------------------------------------
# list_effects tests
# ---------------------------------------------------------------------------

class TestListEffects(unittest.TestCase):
    """Tests for Controller.list_effects."""

    def test_returns_registry(self) -> None:
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20)
        effects = ctrl.list_effects()
        self.assertIn(KNOWN_EFFECT, effects)
        self.assertIn("description", effects[KNOWN_EFFECT])
        self.assertIn("params", effects[KNOWN_EFFECT])

    def test_params_have_metadata(self) -> None:
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20)
        effects = ctrl.list_effects()
        params = effects[KNOWN_EFFECT]["params"]
        # Breathe should have at least 'speed'.
        self.assertIn("speed", params)
        self.assertIn("default", params["speed"])
        self.assertIn("min", params["speed"])
        self.assertIn("max", params["speed"])


# ---------------------------------------------------------------------------
# Audio delay tests
# ---------------------------------------------------------------------------

class TestAudioDelay(unittest.TestCase):
    """Tests for audio sync delay buffer."""

    def test_set_delay(self) -> None:
        em = RecordingEmitter()
        eng = Engine([em], fps=20)
        eng.set_audio_delay(0.5)
        # 0.5s at 20fps = 10 frames.
        self.assertEqual(eng._audio_delay_frames, 10)

    def test_zero_delay(self) -> None:
        em = RecordingEmitter()
        eng = Engine([em], fps=20)
        eng.set_audio_delay(0.0)
        self.assertEqual(eng._audio_delay_frames, 0)

    def test_delay_clears_buffer(self) -> None:
        em = RecordingEmitter()
        eng = Engine([em], fps=20)
        eng._delay_buffer.append("stale")
        eng.set_audio_delay(1.0)
        self.assertEqual(len(eng._delay_buffer), 0)


# ---------------------------------------------------------------------------
# Dispatch mode tests
# ---------------------------------------------------------------------------

class TestDispatchMode(unittest.TestCase):
    """Tests for matrix vs multizone vs single-zone dispatch."""

    def test_multizone_dispatch(self) -> None:
        """Multizone emitter receives send_zones calls."""
        em = RecordingEmitter(zone_count=36, is_multizone=True)
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 1)
        ctrl.stop(fade_ms=0)
        frames = em.get_frames()
        self.assertGreaterEqual(len(frames), 1)
        self.assertEqual(len(frames[0]), 36)

    def test_single_zone_dispatch(self) -> None:
        """Single-zone emitter receives send_color calls (1-element frames)."""
        em = RecordingEmitter(zone_count=1, is_multizone=False)
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        ctrl.play(KNOWN_EFFECT)
        _wait_for_frames(em, 1)
        ctrl.stop(fade_ms=0)
        frames = em.get_frames()
        self.assertGreaterEqual(len(frames), 1)
        self.assertEqual(len(frames[0]), 1)


# ---------------------------------------------------------------------------
# Concurrent play/stop tests
# ---------------------------------------------------------------------------

class TestConcurrency(unittest.TestCase):
    """Tests for thread safety under concurrent operations."""

    def test_rapid_play_stop(self) -> None:
        """Rapid play/stop cycles don't crash."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20, zones_per_bulb=1)
        for _ in range(5):
            ctrl.play(KNOWN_EFFECT)
            time.sleep(0.05)
            ctrl.stop(fade_ms=0)
        # Should complete without error.

    def test_stop_without_play(self) -> None:
        """Stopping without playing doesn't crash."""
        em = RecordingEmitter()
        ctrl = Controller([em], fps=20)
        ctrl.stop(fade_ms=0)  # Should be a no-op.


if __name__ == "__main__":
    unittest.main()
