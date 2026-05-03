"""Unit tests for the MIDI sensor, emitter, and light bridge.

Tests the components that don't require live MQTT or LIFX hardware.
Focuses on the synth backend ABC, event dispatch logic, backend
factory, pitch bend offset, note on/off tracking, and multi-device
virtual strip.

Run::

    python3 -m pytest tests/test_midi_pipeline.py -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

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
    NOTE_SATURATION,
    BRI_MAX,
    KELVIN_DEFAULT,
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

    def test_pitch_bend_offset_applied(self) -> None:
        """FluidSynth backend subtracts 8192 from MIDI pitch bend value.

        pyfluidsynth expects -8192..+8191 (center=0), but MIDI wire
        format is 0..16383 (center=8192).  The backend must convert.
        """
        # We can't call FluidSynth directly in tests, but we can
        # verify the emitter passes the raw value and the backend
        # is responsible for conversion.
        event: dict = {
            "event_type": "pitch_bend",
            "channel": 0,
            "pitch_bend": 8192,  # Center — no bend.
        }
        self.emitter._on_midi_event(event)
        # The mock backend receives the raw MIDI value.
        # The FluidSynth backend would subtract 8192.
        self.assertEqual(self.backend.pitch_bends[0], (0, 8192))

    def test_pitch_bend_extremes(self) -> None:
        """Pitch bend at min (0) and max (16383) dispatch correctly."""
        self.emitter._on_midi_event({
            "event_type": "pitch_bend",
            "channel": 0,
            "pitch_bend": 0,
        })
        self.emitter._on_midi_event({
            "event_type": "pitch_bend",
            "channel": 0,
            "pitch_bend": 16383,
        })
        self.assertEqual(self.backend.pitch_bends[0], (0, 0))
        self.assertEqual(self.backend.pitch_bends[1], (0, 16383))

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


class TestLightBridgeNoteTracking(unittest.TestCase):
    """Test the MIDI light bridge note on/off tracking.

    The bridge now tracks active notes and only lights zones while
    a note is held — no decay timer.
    """

    def setUp(self) -> None:
        """Create a bridge with mock internals."""
        self.bridge: MidiLightBridge = MidiLightBridge(
            device_ips=["192.0.2.1"],
            broker="localhost",
        )
        # Manually set zone count (skip device discovery).
        self.bridge._zone_count = TEST_ZONE_COUNT

    def test_note_on_registers(self) -> None:
        """A note_on event adds to active_notes."""
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": 60,
            "velocity": 100,
        })
        self.assertIn((0, 60), self.bridge._active_notes)

    def test_note_off_removes(self) -> None:
        """A note_off event removes from active_notes."""
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": 60,
            "velocity": 100,
        })
        self.assertIn((0, 60), self.bridge._active_notes)

        self.bridge._on_midi_event({
            "event_type": "note_off",
            "channel": 0,
            "note": 60,
        })
        self.assertNotIn((0, 60), self.bridge._active_notes)

    def test_note_off_nonexistent_ok(self) -> None:
        """Note-off for a note that wasn't on doesn't crash."""
        self.bridge._on_midi_event({
            "event_type": "note_off",
            "channel": 5,
            "note": 99,
        })
        # Should not raise.

    def test_multiple_notes_tracked(self) -> None:
        """Multiple simultaneous notes are all tracked."""
        for note in [60, 64, 67]:
            self.bridge._on_midi_event({
                "event_type": "note_on",
                "channel": 0,
                "note": note,
                "velocity": 100,
            })
        self.assertEqual(len(self.bridge._active_notes), 3)

        # Release middle note.
        self.bridge._on_midi_event({
            "event_type": "note_off",
            "channel": 0,
            "note": 64,
        })
        self.assertEqual(len(self.bridge._active_notes), 2)
        self.assertNotIn((0, 64), self.bridge._active_notes)

    def test_same_note_different_channels(self) -> None:
        """Same note on different channels tracked independently."""
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": 60,
            "velocity": 100,
        })
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 1,
            "note": 60,
            "velocity": 80,
        })
        self.assertEqual(len(self.bridge._active_notes), 2)
        self.assertIn((0, 60), self.bridge._active_notes)
        self.assertIn((1, 60), self.bridge._active_notes)

    def test_note_maps_to_zone(self) -> None:
        """Active note stores the correct zone index."""
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": MIDI_NOTE_LOW,
            "velocity": 100,
        })
        info: dict = self.bridge._active_notes[(0, MIDI_NOTE_LOW)]
        self.assertEqual(info["zone"], 0)  # Low note → zone 0.

    def test_high_note_maps_to_end(self) -> None:
        """High note maps to the last zone."""
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": MIDI_NOTE_HIGH,
            "velocity": 100,
        })
        info: dict = self.bridge._active_notes[(0, MIDI_NOTE_HIGH)]
        self.assertEqual(info["zone"], TEST_ZONE_COUNT - 1)

    def test_channel_sets_hue(self) -> None:
        """Channel number determines the hue in active_notes."""
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 2,
            "note": 60,
            "velocity": 100,
        })
        info: dict = self.bridge._active_notes[(2, 60)]
        self.assertEqual(info["hue"], CHANNEL_HUES[2])

    def test_velocity_sets_brightness(self) -> None:
        """Velocity determines brightness in active_notes."""
        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 0,
            "note": 60,
            "velocity": 127,
        })
        info: dict = self.bridge._active_notes[(0, 60)]
        self.assertAlmostEqual(info["brightness"], 1.0, places=2)

        self.bridge._on_midi_event({
            "event_type": "note_on",
            "channel": 1,
            "note": 64,
            "velocity": 64,
        })
        info2: dict = self.bridge._active_notes[(1, 64)]
        self.assertAlmostEqual(info2["brightness"], 64 / 127.0, places=2)

    def test_all_notes_off_clears(self) -> None:
        """Releasing all notes empties active_notes."""
        for note in [60, 64, 67]:
            self.bridge._on_midi_event({
                "event_type": "note_on",
                "channel": 0,
                "note": note,
                "velocity": 100,
            })
        for note in [60, 64, 67]:
            self.bridge._on_midi_event({
                "event_type": "note_off",
                "channel": 0,
                "note": note,
            })
        self.assertEqual(len(self.bridge._active_notes), 0)


class TestLightBridgeMultiDevice(unittest.TestCase):
    """Test multi-device virtual strip construction."""

    def test_multiple_ips_accepted(self) -> None:
        """Bridge accepts a list of IPs."""
        bridge: MidiLightBridge = MidiLightBridge(
            device_ips=["192.0.2.1", "192.0.2.2", "192.0.2.3"],
            broker="localhost",
        )
        self.assertEqual(len(bridge._device_ips), 3)

    def test_single_ip_accepted(self) -> None:
        """Bridge works with a single IP."""
        bridge: MidiLightBridge = MidiLightBridge(
            device_ips=["192.0.2.1"],
            broker="localhost",
        )
        self.assertEqual(len(bridge._device_ips), 1)

    def test_devices_list_starts_empty(self) -> None:
        """Discovered devices list is empty before discovery."""
        bridge: MidiLightBridge = MidiLightBridge(
            device_ips=["192.0.2.1"],
            broker="localhost",
        )
        self.assertEqual(len(bridge._devices), 0)
        self.assertEqual(bridge._zone_count, 0)


class TestFluidSynthPitchBendOffset(unittest.TestCase):
    """FluidSynthBackend.pitch_bend converts MIDI 0..16383 (center 8192)
    to pyfluidsynth's -8192..+8191 (center 0).  Test the actual method
    against a mock FluidSynth so the offset isn't merely re-implemented
    in the test body.
    """

    def _backend_with_mock_fs(self) -> tuple[Any, Any]:
        from emitters.midi_out import FluidSynthBackend
        backend = FluidSynthBackend.__new__(FluidSynthBackend)
        backend._fs = MagicMock()
        return backend, backend._fs

    def test_pitch_bend_offset_applied(self) -> None:
        """Backend forwards (value - 8192) to FluidSynth across the range."""
        cases: list[tuple[int, int]] = [
            (0, -8192),       # MIDI min
            (8192, 0),        # center
            (16383, 8191),    # MIDI max
            (8192 + 1024, 1024),
            (8192 - 1024, -1024),
        ]
        for midi_value, expected in cases:
            with self.subTest(midi_value=midi_value):
                backend, fs = self._backend_with_mock_fs()
                backend.pitch_bend(channel=3, value=midi_value)
                fs.pitch_bend.assert_called_once_with(3, expected)

    def test_pitch_bend_noop_when_fs_absent(self) -> None:
        """No exception when the FluidSynth client failed to initialize."""
        from emitters.midi_out import FluidSynthBackend
        backend = FluidSynthBackend.__new__(FluidSynthBackend)
        backend._fs = None
        backend.pitch_bend(channel=0, value=8192)  # Must not raise.


if __name__ == "__main__":
    unittest.main()
