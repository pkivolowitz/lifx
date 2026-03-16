"""Unit tests for the MIDI sensor, emitter, and light bridge.

Tests the components that don't require live MQTT or LIFX hardware.
Focuses on the synth backend ABC, event dispatch logic, backend
factory, and the light bridge zone mapping.

Run::

    python3 -m pytest tests/test_midi_pipeline.py -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import unittest
from typing import Any
from unittest.mock import MagicMock

from emitters.midi_out import (
    SynthBackend,
    FluidSynthBackend,
    RtMidiBackend,
    MidiOutEmitter,
    create_backend,
    BACKEND_FLUIDSYNTH,
    BACKEND_RTMIDI,
    VALID_BACKENDS,
    PITCH_BEND_CENTER,
    MIDI_MAX,
)
from distributed.midi_light_bridge import (
    MidiLightBridge,
    CHANNEL_HUES,
    MIDI_NOTE_LOW,
    MIDI_NOTE_HIGH,
    DEFAULT_DECAY,
    BRIGHTNESS_FLOOR,
    NOTE_SATURATION,
    BRI_MAX,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Number of zones on a test string light.
TEST_ZONE_COUNT: int = 108


# ---------------------------------------------------------------------------
# Mock synth backend for testing
# ---------------------------------------------------------------------------

class MockSynthBackend(SynthBackend):
    """Test backend that records all calls without producing sound."""

    def __init__(self) -> None:
        """Initialize call recording lists."""
        self.started: bool = False
        self.stopped: bool = False
        self.notes_on: list[tuple[int, int, int]] = []
        self.notes_off: list[tuple[int, int]] = []
        self.ccs: list[tuple[int, int, int]] = []
        self.programs: list[tuple[int, int]] = []
        self.pitch_bends: list[tuple[int, int]] = []

    def start(self) -> None:
        """Record start call."""
        self.started = True

    def stop(self) -> None:
        """Record stop call."""
        self.stopped = True

    def note_on(self, channel: int, note: int, velocity: int) -> None:
        """Record note-on."""
        self.notes_on.append((channel, note, velocity))

    def note_off(self, channel: int, note: int) -> None:
        """Record note-off."""
        self.notes_off.append((channel, note))

    def control_change(self, channel: int, cc: int, value: int) -> None:
        """Record control change."""
        self.ccs.append((channel, cc, value))

    def program_change(self, channel: int, program: int) -> None:
        """Record program change."""
        self.programs.append((channel, program))

    def pitch_bend(self, channel: int, value: int) -> None:
        """Record pitch bend."""
        self.pitch_bends.append((channel, value))


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

class TestSynthBackendABC(unittest.TestCase):
    """Test the SynthBackend abstract base class."""

    def test_cannot_instantiate_abc(self) -> None:
        """SynthBackend cannot be instantiated directly."""
        with self.assertRaises(TypeError):
            SynthBackend()

    def test_mock_backend_implements_interface(self) -> None:
        """MockSynthBackend satisfies the SynthBackend interface."""
        backend: MockSynthBackend = MockSynthBackend()
        backend.start()
        self.assertTrue(backend.started)
        backend.note_on(0, 60, 100)
        self.assertEqual(len(backend.notes_on), 1)
        backend.stop()
        self.assertTrue(backend.stopped)

    def test_all_notes_off_default(self) -> None:
        """Default all_notes_off sends CC 123 on all 16 channels."""
        backend: MockSynthBackend = MockSynthBackend()
        backend.all_notes_off()
        self.assertEqual(len(backend.ccs), 16)
        for i, (ch, cc, val) in enumerate(backend.ccs):
            self.assertEqual(ch, i)
            self.assertEqual(cc, 123)
            self.assertEqual(val, 0)


class TestBackendFactory(unittest.TestCase):
    """Test the create_backend factory function."""

    def test_unknown_backend_raises(self) -> None:
        """Unknown backend name raises ValueError."""
        with self.assertRaises(ValueError):
            create_backend("nonexistent")

    def test_fluidsynth_requires_soundfont(self) -> None:
        """FluidSynth backend raises if no soundfont given."""
        with self.assertRaises(ValueError):
            create_backend(BACKEND_FLUIDSYNTH)

    def test_fluidsynth_returns_correct_type(self) -> None:
        """FluidSynth factory returns FluidSynthBackend."""
        backend = create_backend(
            BACKEND_FLUIDSYNTH, soundfont="/fake/path.sf2",
        )
        self.assertIsInstance(backend, FluidSynthBackend)

    def test_rtmidi_returns_correct_type(self) -> None:
        """rtmidi factory returns RtMidiBackend."""
        backend = create_backend(BACKEND_RTMIDI)
        self.assertIsInstance(backend, RtMidiBackend)

    def test_valid_backends_list(self) -> None:
        """VALID_BACKENDS contains expected entries."""
        self.assertIn(BACKEND_FLUIDSYNTH, VALID_BACKENDS)
        self.assertIn(BACKEND_RTMIDI, VALID_BACKENDS)


class TestEmitterEventDispatch(unittest.TestCase):
    """Test the MidiOutEmitter event dispatch logic.

    Uses the mock backend to verify that incoming JSON events
    are routed to the correct backend methods.
    """

    def setUp(self) -> None:
        """Create an emitter with a mock backend."""
        self.backend: MockSynthBackend = MockSynthBackend()
        self.emitter: MidiOutEmitter = MidiOutEmitter(
            backend=self.backend,
            broker="localhost",
            port=1883,
        )

    def test_note_on_dispatch(self) -> None:
        """Note-on events dispatch to backend.note_on."""
        event: dict = {
            "event_type": "note_on",
            "channel": 1,
            "note": 64,
            "velocity": 100,
        }
        self.emitter._on_midi_event(event)
        self.assertEqual(len(self.backend.notes_on), 1)
        self.assertEqual(self.backend.notes_on[0], (1, 64, 100))

    def test_note_off_dispatch(self) -> None:
        """Note-off events dispatch to backend.note_off."""
        event: dict = {
            "event_type": "note_off",
            "channel": 0,
            "note": 60,
        }
        self.emitter._on_midi_event(event)
        self.assertEqual(len(self.backend.notes_off), 1)
        self.assertEqual(self.backend.notes_off[0], (0, 60))

    def test_cc_dispatch(self) -> None:
        """Control change events dispatch correctly."""
        event: dict = {
            "event_type": "control_change",
            "channel": 2,
            "cc_number": 7,
            "cc_value": 127,
        }
        self.emitter._on_midi_event(event)
        self.assertEqual(len(self.backend.ccs), 1)
        self.assertEqual(self.backend.ccs[0], (2, 7, 127))

    def test_program_change_dispatch(self) -> None:
        """Program change events dispatch correctly."""
        event: dict = {
            "event_type": "program_change",
            "channel": 0,
            "program": 19,
        }
        self.emitter._on_midi_event(event)
        self.assertEqual(len(self.backend.programs), 1)
        self.assertEqual(self.backend.programs[0], (0, 19))

    def test_pitch_bend_dispatch(self) -> None:
        """Pitch bend events dispatch correctly."""
        event: dict = {
            "event_type": "pitch_bend",
            "channel": 0,
            "pitch_bend": PITCH_BEND_CENTER,
        }
        self.emitter._on_midi_event(event)
        self.assertEqual(len(self.backend.pitch_bends), 1)
        self.assertEqual(self.backend.pitch_bends[0], (0, PITCH_BEND_CENTER))

    def test_stream_markers_ignored(self) -> None:
        """Stream start/end markers don't crash or trigger notes."""
        self.emitter._on_midi_event({"event_type": "stream_start",
                                      "source_file": "test.mid"})
        self.emitter._on_midi_event({"event_type": "stream_end",
                                      "events_sent": 100})
        self.assertEqual(len(self.backend.notes_on), 0)

    def test_meta_events_ignored(self) -> None:
        """Meta events are silently ignored."""
        self.emitter._on_midi_event({"event_type": "set_tempo",
                                      "tempo_bpm": 120.0})
        self.emitter._on_midi_event({"event_type": "track_name",
                                      "meta_value": "Piano"})
        self.assertEqual(len(self.backend.notes_on), 0)

    def test_velocity_scale(self) -> None:
        """Velocity scaling is applied to note-on events."""
        emitter: MidiOutEmitter = MidiOutEmitter(
            backend=self.backend,
            broker="localhost",
            velocity_scale=0.5,
        )
        event: dict = {
            "event_type": "note_on",
            "channel": 0,
            "note": 60,
            "velocity": 100,
        }
        emitter._on_midi_event(event)
        # 100 * 0.5 = 50.
        self.assertEqual(self.backend.notes_on[0][2], 50)

    def test_velocity_clamp(self) -> None:
        """Scaled velocity is clamped to MIDI_MAX (127)."""
        emitter: MidiOutEmitter = MidiOutEmitter(
            backend=self.backend,
            broker="localhost",
            velocity_scale=2.0,
        )
        event: dict = {
            "event_type": "note_on",
            "channel": 0,
            "note": 60,
            "velocity": 100,
        }
        emitter._on_midi_event(event)
        # 100 * 2.0 = 200, clamped to 127.
        self.assertEqual(self.backend.notes_on[0][2], MIDI_MAX)


class TestLightBridgeZoneMapping(unittest.TestCase):
    """Test the MIDI light bridge zone mapping logic.

    Creates a bridge with a mock device and verifies that note
    events produce correct zone brightness and hue assignments.
    """

    def setUp(self) -> None:
        """Create a bridge with mock internals."""
        self.bridge: MidiLightBridge = MidiLightBridge(
            device_ip="10.0.0.1",
            broker="localhost",
        )
        # Manually set zone count and buffers (skip device discovery).
        self.bridge._zone_count = TEST_ZONE_COUNT
        self.bridge._zone_brightness = [0.0] * TEST_ZONE_COUNT
        self.bridge._zone_hue = [0] * TEST_ZONE_COUNT

    def test_note_on_sets_brightness(self) -> None:
        """A note-on event sets nonzero brightness on at least one zone."""
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": 60,
            "velocity": 100,
        })
        max_bri: float = max(self.bridge._zone_brightness)
        self.assertGreater(max_bri, 0.0)

    def test_low_note_maps_left(self) -> None:
        """Low notes light up zones near the left end."""
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": MIDI_NOTE_LOW,
            "velocity": 100,
        })
        # The brightest zone should be near index 0.
        brightest_zone: int = self.bridge._zone_brightness.index(
            max(self.bridge._zone_brightness)
        )
        self.assertLess(brightest_zone, TEST_ZONE_COUNT // 4)

    def test_high_note_maps_right(self) -> None:
        """High notes light up zones near the right end."""
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": MIDI_NOTE_HIGH,
            "velocity": 100,
        })
        brightest_zone: int = self.bridge._zone_brightness.index(
            max(self.bridge._zone_brightness)
        )
        self.assertGreater(brightest_zone, TEST_ZONE_COUNT * 3 // 4)

    def test_channel_determines_hue(self) -> None:
        """Different channels produce different hues."""
        # Channel 0.
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": 60,
            "velocity": 100,
        })
        hue_ch0: int = self.bridge._zone_hue[
            self.bridge._zone_brightness.index(
                max(self.bridge._zone_brightness)
            )
        ]

        # Reset.
        self.bridge._zone_brightness = [0.0] * TEST_ZONE_COUNT
        self.bridge._zone_hue = [0] * TEST_ZONE_COUNT

        # Channel 1.
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 1,
            "note": 60,
            "velocity": 100,
        })
        hue_ch1: int = self.bridge._zone_hue[
            self.bridge._zone_brightness.index(
                max(self.bridge._zone_brightness)
            )
        ]

        self.assertNotEqual(hue_ch0, hue_ch1)
        self.assertEqual(hue_ch0, CHANNEL_HUES[0])
        self.assertEqual(hue_ch1, CHANNEL_HUES[1])

    def test_velocity_affects_brightness(self) -> None:
        """Higher velocity produces higher brightness."""
        # Low velocity.
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": 60,
            "velocity": 30,
        })
        bri_low: float = max(self.bridge._zone_brightness)

        self.bridge._zone_brightness = [0.0] * TEST_ZONE_COUNT

        # High velocity.
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": 60,
            "velocity": 127,
        })
        bri_high: float = max(self.bridge._zone_brightness)

        self.assertGreater(bri_high, bri_low)

    def test_brightness_minimum_floor(self) -> None:
        """Even low velocity produces at least 50% brightness."""
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": 60,
            "velocity": 1,
        })
        max_bri: float = max(self.bridge._zone_brightness)
        self.assertGreaterEqual(max_bri, 0.5)

    def test_note_off_does_not_crash(self) -> None:
        """Note-off events are handled without errors."""
        self.bridge._on_midi_event({
            "event_type": "note_off",
            "channel": 0,
            "note": 60,
        })
        # Should not raise; decay handles fade.

    def test_spread_lights_multiple_zones(self) -> None:
        """A single note lights up multiple adjacent zones (spread)."""
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": 60,
            "velocity": 100,
        })
        lit_zones: int = sum(
            1 for b in self.bridge._zone_brightness if b > 0.0
        )
        self.assertGreater(lit_zones, 1, "Note should light multiple zones")

    def test_out_of_range_note_clamped(self) -> None:
        """Notes outside MIDI_NOTE_LOW-HIGH are clamped, not crashed."""
        # Very low note.
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": 0,
            "velocity": 100,
        })
        # Very high note.
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": 127,
            "velocity": 100,
        })
        # Should not raise.
        max_bri: float = max(self.bridge._zone_brightness)
        self.assertGreater(max_bri, 0.0)


if __name__ == "__main__":
    unittest.main()
