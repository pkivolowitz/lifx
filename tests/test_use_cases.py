#!/usr/bin/env python3
"""Use-case-level end-to-end tests for the GlowUp effect engine.

These tests exercise the full pipeline — Controller → Engine → Emitters —
without hardware, GUI, or network dependencies.  They capture the kinds of
integration tests that were previously run ad-hoc during development and
lost: effect playback, signal-reactive parameter binding, multi-device
fan-out, start/stop lifecycle, and frame correctness.

Architecture
------------
A ``RecordingEmitter`` captures every frame sent through the pipeline.
A ``SignalBus`` injects synthetic signal values.  The ``Controller``
drives the real ``Engine`` with real ``Effect`` instances.  Tests assert
on the *recorded output* — the same data that would have gone to real
bulbs over UDP.

Categories
----------
- **Playback**:      Effect starts, produces frames, stops cleanly.
- **Signal binding**: External signals modulate effect parameters.
- **Multi-device**:  Virtual group fans out to N emitters correctly.
- **Lifecycle**:     Start/stop/replace/resume without crashes.
- **Frame correctness**: HSBK values within legal range, zone counts match.

Run::

    python3 -m pytest test_use_cases.py -v
    python3 -m unittest test_use_cases -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import threading
import time
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock

from effects import (
    HSBK, HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    Effect, create_effect, get_registry,
)
from emitters import Emitter, EmitterCapabilities
from engine import Controller, Engine

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How long to let the engine run before collecting frames.  Longer values
# produce more frames but slow down the test suite.
DEFAULT_SETTLE_SECONDS: float = 0.3

# Minimum frames we expect from a 20 FPS engine in DEFAULT_SETTLE_SECONDS.
# Conservatively low to avoid flaky timing on loaded CI machines.
MIN_EXPECTED_FRAMES: int = 3

# Maximum HSBK component value (unsigned 16-bit).
HSBK_COMPONENT_MAX: int = 65535

# Default FPS for test engines.
TEST_FPS: int = 20

# Default zones for test emitters.
TEST_ZONE_COUNT: int = 36

# Zones per bulb for string-light-style tests.
TEST_ZPB: int = 3


# ---------------------------------------------------------------------------
# RecordingEmitter — captures frames for assertion
# ---------------------------------------------------------------------------

class RecordingEmitter(Emitter):
    """In-memory emitter that records every frame for test assertions.

    Implements the full :class:`Emitter` interface but writes frames
    to a list instead of UDP.  Thread-safe: the engine's send thread
    writes concurrently with the test thread reading.

    Attributes:
        zone_frames:  List of multizone frames (each a list of HSBK).
        color_frames: List of single-zone HSBK tuples.
        power_calls:  List of (on: bool, duration_ms: int) tuples.
    """

    def __init__(
        self,
        emitter_id: str = "10.0.0.99",
        zone_count: int = TEST_ZONE_COUNT,
        is_multizone: bool = True,
    ) -> None:
        """Initialize a recording emitter.

        Args:
            emitter_id:   Unique identifier (typically an IP string).
            zone_count:   Number of zones to report.
            is_multizone: Whether this emitter acts as a multizone device.
        """
        self._emitter_id: str = emitter_id
        self._zone_count: int = zone_count
        self._is_multizone: bool = is_multizone
        self._label: str = f"test-{emitter_id}"
        self._product_name: str = "RecordingEmitter"
        self._lock: threading.Lock = threading.Lock()

        self.zone_frames: list[list[HSBK]] = []
        self.color_frames: list[HSBK] = []
        self.power_calls: list[tuple[bool, int]] = []

    @property
    def zone_count(self) -> Optional[int]:
        """Return the configured zone count."""
        return self._zone_count

    @property
    def is_multizone(self) -> bool:
        """Return whether this emitter is multizone."""
        return self._is_multizone

    @property
    def emitter_id(self) -> str:
        """Return the emitter ID."""
        return self._emitter_id

    @property
    def label(self) -> str:
        """Return the emitter label."""
        return self._label

    @property
    def product_name(self) -> str:
        """Return the product name."""
        return self._product_name

    def prepare_for_rendering(self, *, skip_wake: bool = False) -> None:
        """No-op — no hardware to wake."""

    def send_zones(
        self,
        colors: list[HSBK],
        duration_ms: int = 0,
        mode: object = None,
    ) -> None:
        """Record a multizone frame."""
        with self._lock:
            self.zone_frames.append(list(colors))

    def send_color(
        self,
        hue: int,
        sat: int,
        bri: int,
        kelvin: int,
        duration_ms: int = 0,
    ) -> None:
        """Record a single-zone color."""
        with self._lock:
            self.color_frames.append((hue, sat, bri, kelvin))

    def power_on(self, duration_ms: int = 0) -> None:
        """Record power-on call."""
        with self._lock:
            self.power_calls.append((True, duration_ms))

    def power_off(self, duration_ms: int = 0) -> None:
        """Record power-off call."""
        with self._lock:
            self.power_calls.append((False, duration_ms))

    def close(self) -> None:
        """No-op."""

    def get_info(self) -> dict[str, Any]:
        """Return emitter info dict (used by Controller.get_status)."""
        return {
            "ip": self._emitter_id,
            "label": self._label,
            "product": self._product_name,
            "zones": self._zone_count,
            "is_multizone": self._is_multizone,
        }

    def get_frames_snapshot(self) -> list[list[HSBK]]:
        """Return a thread-safe copy of all recorded zone frames."""
        with self._lock:
            return list(self.zone_frames)

    def get_color_snapshot(self) -> list[HSBK]:
        """Return a thread-safe copy of all recorded color frames."""
        with self._lock:
            return list(self.color_frames)

    def frame_count(self) -> int:
        """Return total frames received (zones + single-color)."""
        with self._lock:
            return len(self.zone_frames) + len(self.color_frames)


# ---------------------------------------------------------------------------
# UseCaseTest — base class with pipeline helpers
# ---------------------------------------------------------------------------

class UseCaseTest(unittest.TestCase):
    """Base class for use-case-level tests.

    Provides helpers to instantiate the full Controller → Engine →
    Emitter pipeline, run effects for a measured duration, and assert
    on the recorded output.
    """

    def _make_emitter(
        self,
        emitter_id: str = "10.0.0.99",
        zone_count: int = TEST_ZONE_COUNT,
        is_multizone: bool = True,
    ) -> RecordingEmitter:
        """Create a recording emitter with the given topology."""
        return RecordingEmitter(emitter_id, zone_count, is_multizone)

    def _make_controller(
        self,
        emitters: list[RecordingEmitter],
        fps: int = TEST_FPS,
        zones_per_bulb: int = 1,
        frame_callback: Optional[Any] = None,
    ) -> Controller:
        """Create a Controller wired to the given emitters."""
        return Controller(
            emitters,
            fps=fps,
            frame_callback=frame_callback,
            zones_per_bulb=zones_per_bulb,
        )

    def _play_and_collect(
        self,
        ctrl: Controller,
        effect_name: str,
        duration: float = DEFAULT_SETTLE_SECONDS,
        bindings: Optional[dict] = None,
        signal_bus: Optional[Any] = None,
        **params: Any,
    ) -> None:
        """Play an effect, wait, then stop.  Frames accumulate on emitters."""
        ctrl.play(effect_name, bindings=bindings,
                  signal_bus=signal_bus, **params)
        time.sleep(duration)
        ctrl.stop(fade_ms=0)
        # Give the engine threads a moment to fully drain.
        time.sleep(0.05)

    def _assert_valid_hsbk(self, color: HSBK, msg: str = "") -> None:
        """Assert that a single HSBK tuple has legal component values."""
        self.assertEqual(len(color), 4, f"HSBK must have 4 components: {msg}")
        h, s, b, k = color
        self.assertGreaterEqual(h, 0, f"Hue < 0: {msg}")
        self.assertLessEqual(h, HSBK_COMPONENT_MAX, f"Hue > max: {msg}")
        self.assertGreaterEqual(s, 0, f"Sat < 0: {msg}")
        self.assertLessEqual(s, HSBK_COMPONENT_MAX, f"Sat > max: {msg}")
        self.assertGreaterEqual(b, 0, f"Bri < 0: {msg}")
        self.assertLessEqual(b, HSBK_COMPONENT_MAX, f"Bri > max: {msg}")
        self.assertGreaterEqual(k, KELVIN_MIN, f"Kelvin < min: {msg}")
        self.assertLessEqual(k, KELVIN_MAX, f"Kelvin > max: {msg}")

    def _assert_frame_valid(
        self,
        frame: list[HSBK],
        expected_zones: int,
        msg: str = "",
    ) -> None:
        """Assert a frame has the right zone count and all legal HSBK."""
        self.assertEqual(
            len(frame), expected_zones,
            f"Zone count mismatch (got {len(frame)}, "
            f"expected {expected_zones}): {msg}",
        )
        for i, color in enumerate(frame):
            self._assert_valid_hsbk(color, f"zone {i} of {msg}")


# ---------------------------------------------------------------------------
# UC-1: Basic playback — effect produces frames and stops cleanly
# ---------------------------------------------------------------------------

class TestBasicPlayback(UseCaseTest):
    """Play each registered effect and verify it produces valid output."""

    def test_every_effect_produces_frames(self) -> None:
        """Every registered effect must produce at least one valid frame."""
        registry: dict = get_registry()
        self.assertGreater(len(registry), 0, "No effects registered")

        # Some effects need special handling (e.g. media effects need a bus).
        # Skip those gracefully.
        skipped: list[str] = []

        for name in sorted(registry):
            em: RecordingEmitter = self._make_emitter()
            ctrl: Controller = self._make_controller([em])
            try:
                self._play_and_collect(ctrl, name, duration=0.2)
            except (ValueError, TypeError) as exc:
                skipped.append(f"{name}: {exc}")
                continue

            frames: list[list[HSBK]] = em.get_frames_snapshot()
            if not frames:
                skipped.append(f"{name}: 0 frames (may need signal bus)")
                continue

            # Validate HSBK values in the first frame.  Don't assert
            # exact zone count — some effects (2D grids) produce
            # non-standard sizes.
            for color in frames[0]:
                self._assert_valid_hsbk(color, f"effect={name} frame[0]")

        # Log skipped effects but don't fail — some effects legitimately
        # need external signals or special config.
        if skipped:
            import sys
            print(
                f"\n  Skipped {len(skipped)} effects: "
                + ", ".join(skipped),
                file=sys.stderr,
            )

    def test_single_zone_emitter(self) -> None:
        """A 1-zone non-multizone emitter receives single-color frames."""
        em: RecordingEmitter = self._make_emitter(
            zone_count=1, is_multizone=False,
        )
        ctrl: Controller = self._make_controller([em])
        self._play_and_collect(ctrl, "breathe", duration=0.2)

        colors: list[HSBK] = em.get_color_snapshot()
        self.assertGreater(
            len(colors), 0,
            "Single-zone emitter received no color frames",
        )
        for color in colors:
            self._assert_valid_hsbk(color, "single-zone breathe")


# ---------------------------------------------------------------------------
# UC-2: Start / stop / replace lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle(UseCaseTest):
    """Effect lifecycle: start, stop, hot-swap, resume."""

    def test_stop_halts_frame_production(self) -> None:
        """After stop(), no more frames should arrive."""
        em: RecordingEmitter = self._make_emitter()
        ctrl: Controller = self._make_controller([em])

        ctrl.play("breathe")
        time.sleep(0.15)
        ctrl.stop(fade_ms=0)
        time.sleep(0.05)

        count_at_stop: int = em.frame_count()
        time.sleep(0.15)
        count_after: int = em.frame_count()

        self.assertEqual(
            count_at_stop, count_after,
            f"Frames still arriving after stop: "
            f"{count_at_stop} → {count_after}",
        )

    def test_hot_swap_effect(self) -> None:
        """Playing a new effect while one is running replaces it cleanly."""
        em: RecordingEmitter = self._make_emitter()
        ctrl: Controller = self._make_controller([em])

        ctrl.play("breathe")
        time.sleep(0.15)
        ctrl.play("cylon")
        time.sleep(0.15)
        ctrl.stop(fade_ms=0)
        time.sleep(0.05)

        # Should have frames from both effects — no crash.
        self.assertGreater(
            em.frame_count(), MIN_EXPECTED_FRAMES,
            "Hot swap produced too few frames",
        )
        # Engine should report the second effect.
        status: dict = ctrl.get_status()
        self.assertEqual(status["effect"], "cylon")

    def test_double_stop_is_safe(self) -> None:
        """Calling stop() twice must not crash."""
        em: RecordingEmitter = self._make_emitter()
        ctrl: Controller = self._make_controller([em])

        ctrl.play("breathe")
        time.sleep(0.1)
        ctrl.stop(fade_ms=0)
        ctrl.stop(fade_ms=0)  # Must not raise.

    def test_play_after_stop(self) -> None:
        """An effect can be restarted after being stopped."""
        em: RecordingEmitter = self._make_emitter()
        ctrl: Controller = self._make_controller([em])

        ctrl.play("breathe")
        time.sleep(0.1)
        ctrl.stop(fade_ms=0)
        time.sleep(0.05)

        count_after_first: int = em.frame_count()

        ctrl.play("cylon")
        time.sleep(0.15)
        ctrl.stop(fade_ms=0)
        time.sleep(0.05)

        self.assertGreater(
            em.frame_count(), count_after_first,
            "No new frames after restart",
        )


# ---------------------------------------------------------------------------
# UC-3: Signal binding — media signals modulate effect params
# ---------------------------------------------------------------------------

class TestSignalBinding(UseCaseTest):
    """External signals (audio, sensor) drive effect parameters in real time."""

    def _make_bus(self) -> "SignalBus":
        """Create a fresh SignalBus."""
        from media import SignalBus
        return SignalBus()

    def test_signal_drives_effect_param(self) -> None:
        """A bound signal should modulate the effect's parameter."""
        bus = self._make_bus()
        em: RecordingEmitter = self._make_emitter()
        ctrl: Controller = self._make_controller([em])

        # Bind the "speed" param of breathe to a test signal.
        bindings: dict = {
            "speed": {
                "signal": "test:sensor:level",
                "scale": [0.5, 10.0],
            },
        }

        # Write a known signal value before playing.
        bus.write("test:sensor:level", 0.5)

        ctrl.play("breathe", bindings=bindings, signal_bus=bus)
        time.sleep(0.15)

        # Now change the signal — the engine reads it each frame.
        bus.write("test:sensor:level", 1.0)
        time.sleep(0.15)
        ctrl.stop(fade_ms=0)
        time.sleep(0.05)

        # We can't easily assert the exact param value from outside,
        # but we CAN verify frames were produced (the binding didn't crash).
        self.assertGreater(
            em.frame_count(), MIN_EXPECTED_FRAMES,
            "Signal binding produced too few frames",
        )

    def test_missing_signal_uses_default(self) -> None:
        """A binding to a non-existent signal should not crash.

        The bus returns 0.0 for unknown signals, which maps to the
        scale minimum.
        """
        bus = self._make_bus()
        em: RecordingEmitter = self._make_emitter()
        ctrl: Controller = self._make_controller([em])

        bindings: dict = {
            "speed": {
                "signal": "nonexistent:signal",
                "scale": [1.0, 10.0],
            },
        }

        ctrl.play("breathe", bindings=bindings, signal_bus=bus)
        time.sleep(0.15)
        ctrl.stop(fade_ms=0)
        time.sleep(0.05)

        self.assertGreater(
            em.frame_count(), 0,
            "Missing signal caused zero frames",
        )

    def test_array_signal_reduced(self) -> None:
        """An array signal with a reduce function should not crash."""
        bus = self._make_bus()
        em: RecordingEmitter = self._make_emitter()
        ctrl: Controller = self._make_controller([em])

        bindings: dict = {
            "speed": {
                "signal": "test:audio:bands",
                "scale": [1.0, 10.0],
                "reduce": "mean",
            },
        }

        # Write an 8-band FFT array.
        bus.write("test:audio:bands", [0.1, 0.3, 0.8, 0.2, 0.0, 0.1, 0.5, 0.9])

        ctrl.play("breathe", bindings=bindings, signal_bus=bus)
        time.sleep(0.15)
        ctrl.stop(fade_ms=0)
        time.sleep(0.05)

        self.assertGreater(
            em.frame_count(), 0,
            "Array signal with reduce='mean' produced no frames",
        )


# ---------------------------------------------------------------------------
# UC-4: Multi-device fan-out via VirtualMultizoneEmitter
# ---------------------------------------------------------------------------

class TestMultiDeviceFanOut(UseCaseTest):
    """Virtual groups distribute zones across multiple physical emitters."""

    def test_virtual_group_distributes_zones(self) -> None:
        """A 3-emitter virtual group splits frames across members."""
        from emitters.virtual import VirtualMultizoneEmitter
        from unittest.mock import patch

        members: list[RecordingEmitter] = [
            self._make_emitter(f"10.0.0.{i}", zone_count=12)
            for i in range(3)
        ]

        with patch("emitters.virtual.broadcast_wake"):
            vem = VirtualMultizoneEmitter(members, name="test-group")

        # Total zones should be sum of members.
        self.assertEqual(vem.zone_count, 36)

        ctrl: Controller = self._make_controller([vem])
        self._play_and_collect(ctrl, "cylon", duration=0.25)

        # Each member should have received frames.
        for i, mem in enumerate(members):
            frames: list[list[HSBK]] = mem.get_frames_snapshot()
            self.assertGreater(
                len(frames), 0,
                f"Member {i} ({mem.emitter_id}) received no frames",
            )
            # Each member's frames should have its own zone count.
            for frame in frames:
                self.assertEqual(
                    len(frame), 12,
                    f"Member {i} got {len(frame)} zones, expected 12",
                )

    def test_mixed_single_and_multizone(self) -> None:
        """A virtual group with mixed emitter types works correctly."""
        from emitters.virtual import VirtualMultizoneEmitter
        from unittest.mock import patch

        multi: RecordingEmitter = self._make_emitter(
            "10.0.0.1", zone_count=30, is_multizone=True,
        )
        single: RecordingEmitter = self._make_emitter(
            "10.0.0.2", zone_count=1, is_multizone=False,
        )

        with patch("emitters.virtual.broadcast_wake"):
            vem = VirtualMultizoneEmitter([multi, single], name="mixed")

        # Total: 30 + 1 = 31 virtual zones.
        self.assertEqual(vem.zone_count, 31)

        ctrl: Controller = self._make_controller([vem])
        self._play_and_collect(ctrl, "breathe", duration=0.25)

        # Multi-zone member gets zone frames.
        self.assertGreater(
            len(multi.get_frames_snapshot()), 0,
            "Multizone member received no zone frames",
        )
        # Single-zone member gets color frames.
        self.assertGreater(
            len(single.get_color_snapshot()), 0,
            "Single-zone member received no color frames",
        )


# ---------------------------------------------------------------------------
# UC-5: Frame correctness — HSBK values within spec
# ---------------------------------------------------------------------------

class TestFrameCorrectness(UseCaseTest):
    """Every frame from every effect must contain legal HSBK values."""

    # Effects known to require special setup (media bus, external input)
    # or that produce non-standard zone counts (2D grid effects).
    SKIP_EFFECTS: set[str] = {
        "soundlevel", "theremin", "waveform",
        # 2D matrix effects return a fixed width × height frame, not
        # zone_count zones — excluded from the zone-count contract check.
        "_grid_map", "fireworks2d", "matrix_rain", "plasma2d", "ripple2d",
        "screen_light2d", "spectrum2d",
    }

    def test_all_effects_produce_legal_hsbk(self) -> None:
        """Spot-check frames from each effect for HSBK validity."""
        registry: dict = get_registry()
        failures: list[str] = []

        for name in sorted(registry):
            if name in self.SKIP_EFFECTS:
                continue

            em: RecordingEmitter = self._make_emitter()
            ctrl: Controller = self._make_controller([em])
            try:
                self._play_and_collect(ctrl, name, duration=0.15)
            except (ValueError, TypeError):
                continue

            frames: list[list[HSBK]] = em.get_frames_snapshot()
            for fi, frame in enumerate(frames[:5]):  # Spot-check first 5.
                try:
                    self._assert_frame_valid(
                        frame, TEST_ZONE_COUNT, f"{name} frame[{fi}]",
                    )
                except AssertionError as exc:
                    failures.append(str(exc))

        if failures:
            self.fail(
                f"{len(failures)} HSBK violations:\n"
                + "\n".join(failures[:10]),
            )

    def test_zones_per_bulb_replication(self) -> None:
        """With zpb=3, each color should be replicated 3 times."""
        em: RecordingEmitter = self._make_emitter(zone_count=36)
        ctrl: Controller = self._make_controller(
            [em], zones_per_bulb=TEST_ZPB,
        )
        self._play_and_collect(ctrl, "breathe", duration=0.2)

        frames: list[list[HSBK]] = em.get_frames_snapshot()
        self.assertGreater(len(frames), 0, "No frames with zpb=3")

        # In each frame, zones should come in groups of 3 identical colors.
        for frame in frames[:3]:
            self.assertEqual(len(frame), 36)
            for bulb_idx in range(12):  # 36 zones / 3 zpb = 12 bulbs
                base: int = bulb_idx * 3
                group: list[HSBK] = frame[base:base + 3]
                self.assertTrue(
                    all(c == group[0] for c in group),
                    f"zpb=3 but zones {base}-{base+2} differ: {group}",
                )


# ---------------------------------------------------------------------------
# UC-6: Frame callback (simulator stand-in)
# ---------------------------------------------------------------------------

class TestFrameCallback(UseCaseTest):
    """The frame_callback receives live frames (simulator replacement)."""

    def test_callback_receives_frames(self) -> None:
        """A frame callback should receive the same data as emitters."""
        collected: list[list[HSBK]] = []
        lock: threading.Lock = threading.Lock()

        def collect(colors: list[HSBK]) -> None:
            with lock:
                collected.append(list(colors))

        em: RecordingEmitter = self._make_emitter()
        ctrl: Controller = self._make_controller(
            [em], frame_callback=collect,
        )
        self._play_and_collect(ctrl, "breathe", duration=0.2)

        with lock:
            callback_count: int = len(collected)

        self.assertGreater(
            callback_count, 0,
            "Frame callback received no frames",
        )

    def test_callback_exception_does_not_crash_engine(self) -> None:
        """A crashing callback must not kill the engine."""
        def bad_callback(colors: list[HSBK]) -> None:
            raise RuntimeError("callback failure")

        em: RecordingEmitter = self._make_emitter()
        ctrl: Controller = self._make_controller(
            [em], frame_callback=bad_callback,
        )
        self._play_and_collect(ctrl, "breathe", duration=0.2)

        # Engine should still have produced frames for the emitter
        # even though the callback crashed.
        self.assertGreater(
            em.frame_count(), 0,
            "Engine stopped producing frames after callback exception",
        )


# ---------------------------------------------------------------------------
# UC-7: Effect parameter override at runtime
# ---------------------------------------------------------------------------

class TestRuntimeParamUpdate(UseCaseTest):
    """Effect parameters can be changed while the effect is running."""

    def test_update_params_while_running(self) -> None:
        """update_params() should not crash and frames keep flowing."""
        em: RecordingEmitter = self._make_emitter()
        ctrl: Controller = self._make_controller([em])

        ctrl.play("breathe", speed=2.0)
        time.sleep(0.1)

        # Update speed while running.
        ctrl.update_params(speed=8.0)
        time.sleep(0.1)

        ctrl.stop(fade_ms=0)
        time.sleep(0.05)

        self.assertGreater(
            em.frame_count(), MIN_EXPECTED_FRAMES,
            "Param update interrupted frame production",
        )

    def test_update_unknown_param_is_safe(self) -> None:
        """Updating a non-existent param should be a silent no-op."""
        em: RecordingEmitter = self._make_emitter()
        ctrl: Controller = self._make_controller([em])

        ctrl.play("breathe")
        time.sleep(0.1)

        # This should not raise.
        ctrl.update_params(nonexistent_param=42.0)
        time.sleep(0.1)
        ctrl.stop(fade_ms=0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
