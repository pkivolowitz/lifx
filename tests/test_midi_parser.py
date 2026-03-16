"""Unit tests for the MIDI file parser.

Tests the pure-Python MIDI parser against synthetic MIDI data and
the real BWV 565 file (if present).  Covers header parsing,
variable-length quantities, event types, tempo mapping, and edge
cases.

Run::

    python3 -m pytest tests/test_midi_parser.py -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import struct
import unittest
from pathlib import Path

from distributed.midi_parser import (
    MidiParser,
    MidiEvent,
    MidiHeader,
    TempoChange,
    HEADER_CHUNK_ID,
    TRACK_CHUNK_ID,
    HEADER_DATA_LENGTH,
    DEFAULT_TEMPO_US,
    STATUS_NOTE_ON,
    STATUS_NOTE_OFF,
    STATUS_CONTROL_CHANGE,
    STATUS_PROGRAM_CHANGE,
    STATUS_PITCH_BEND,
    STATUS_META,
    META_SET_TEMPO,
    META_END_OF_TRACK,
    META_TRACK_NAME,
    META_TIME_SIGNATURE,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Path to the real BWV 565 test file (optional — tests skip if absent).
BWV565_PATH: str = str(
    Path.home() / "Downloads" / "organ_major_works_bwv-565_(c)unknown1.mid"
)

# Ticks per quarter note used in synthetic test files.
TEST_TPQ: int = 480


# ---------------------------------------------------------------------------
# Helpers — build synthetic MIDI data
# ---------------------------------------------------------------------------

def _build_header(format_type: int = 1, num_tracks: int = 1,
                  tpq: int = TEST_TPQ) -> bytes:
    """Build a minimal MThd header chunk.

    Args:
        format_type: SMF format (0, 1, or 2).
        num_tracks:  Number of tracks.
        tpq:         Ticks per quarter note.

    Returns:
        Raw bytes for the header chunk.
    """
    data: bytes = struct.pack(">HHH", format_type, num_tracks, tpq)
    return HEADER_CHUNK_ID + struct.pack(">I", len(data)) + data


def _encode_vlq(value: int) -> bytes:
    """Encode an integer as a MIDI variable-length quantity.

    Args:
        value: Non-negative integer to encode.

    Returns:
        VLQ-encoded bytes (1-4 bytes).
    """
    if value < 0:
        raise ValueError("VLQ value must be non-negative")
    result: list[int] = []
    result.append(value & 0x7F)
    value >>= 7
    while value > 0:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.reverse()
    return bytes(result)


def _build_track(events: bytes) -> bytes:
    """Wrap raw event bytes in an MTrk chunk.

    Args:
        events: Raw event bytes (delta times + event data).

    Returns:
        Complete MTrk chunk bytes.
    """
    return TRACK_CHUNK_ID + struct.pack(">I", len(events)) + events


def _meta_event(delta: int, meta_type: int, data: bytes) -> bytes:
    """Build a meta event.

    Args:
        delta:     Delta time.
        meta_type: Meta event type byte.
        data:      Meta event data.

    Returns:
        Raw bytes for the meta event.
    """
    return (
        _encode_vlq(delta)
        + bytes([0xFF, meta_type])
        + _encode_vlq(len(data))
        + data
    )


def _note_on(delta: int, channel: int, note: int,
             velocity: int) -> bytes:
    """Build a note-on event.

    Args:
        delta:    Delta time.
        channel:  MIDI channel (0-15).
        note:     Note number (0-127).
        velocity: Velocity (0-127).

    Returns:
        Raw event bytes.
    """
    return _encode_vlq(delta) + bytes([0x90 | channel, note, velocity])


def _note_off(delta: int, channel: int, note: int) -> bytes:
    """Build a note-off event.

    Args:
        delta:   Delta time.
        channel: MIDI channel (0-15).
        note:    Note number (0-127).

    Returns:
        Raw event bytes.
    """
    return _encode_vlq(delta) + bytes([0x80 | channel, note, 0])


def _end_of_track(delta: int = 0) -> bytes:
    """Build an end-of-track meta event.

    Args:
        delta: Delta time.

    Returns:
        Raw event bytes.
    """
    return _meta_event(delta, META_END_OF_TRACK, b"")


def _set_tempo(delta: int, bpm: float) -> bytes:
    """Build a set-tempo meta event.

    Args:
        delta: Delta time.
        bpm:   Tempo in beats per minute.

    Returns:
        Raw event bytes.
    """
    us_per_beat: int = int(60_000_000 / bpm)
    data: bytes = bytes([
        (us_per_beat >> 16) & 0xFF,
        (us_per_beat >> 8) & 0xFF,
        us_per_beat & 0xFF,
    ])
    return _meta_event(delta, META_SET_TEMPO, data)


def _build_simple_midi(bpm: float = 120.0, notes: int = 4) -> bytes:
    """Build a complete single-track MIDI file with simple notes.

    Creates a format-0 file with a tempo event followed by
    alternating note-on / note-off events at quarter-note intervals.

    Args:
        bpm:   Tempo in BPM.
        notes: Number of notes to generate.

    Returns:
        Complete MIDI file bytes.
    """
    track_data: bytes = b""

    # Tempo event at tick 0.
    track_data += _set_tempo(0, bpm)

    # Notes: C4 through C4+notes, each one quarter note long.
    for i in range(notes):
        track_data += _note_on(0 if i == 0 else TEST_TPQ, 0, 60 + i, 100)
        track_data += _note_off(TEST_TPQ, 0, 60 + i)

    track_data += _end_of_track(0)

    header: bytes = _build_header(format_type=0, num_tracks=1, tpq=TEST_TPQ)
    track: bytes = _build_track(track_data)
    return header + track


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

class TestVLQ(unittest.TestCase):
    """Test variable-length quantity encoding/decoding."""

    def test_single_byte(self) -> None:
        """VLQ values 0-127 encode as a single byte."""
        for value in [0, 1, 63, 127]:
            encoded: bytes = _encode_vlq(value)
            self.assertEqual(len(encoded), 1)
            result, pos = MidiParser._read_vlq(encoded, 0)
            self.assertEqual(result, value)
            self.assertEqual(pos, 1)

    def test_two_bytes(self) -> None:
        """VLQ values 128-16383 encode as two bytes."""
        for value in [128, 255, 480, 16383]:
            encoded: bytes = _encode_vlq(value)
            self.assertEqual(len(encoded), 2)
            result, pos = MidiParser._read_vlq(encoded, 0)
            self.assertEqual(result, value)
            self.assertEqual(pos, 2)

    def test_three_bytes(self) -> None:
        """VLQ values 16384+ encode as three bytes."""
        value: int = 100000
        encoded: bytes = _encode_vlq(value)
        result, pos = MidiParser._read_vlq(encoded, 0)
        self.assertEqual(result, value)

    def test_zero(self) -> None:
        """VLQ of zero is a single 0x00 byte."""
        encoded: bytes = _encode_vlq(0)
        self.assertEqual(encoded, b"\x00")
        result, pos = MidiParser._read_vlq(encoded, 0)
        self.assertEqual(result, 0)

    def test_max_four_byte(self) -> None:
        """VLQ maximum value (0x0FFFFFFF) encodes as four bytes."""
        value: int = 0x0FFFFFFF
        encoded: bytes = _encode_vlq(value)
        self.assertEqual(len(encoded), 4)
        result, pos = MidiParser._read_vlq(encoded, 0)
        self.assertEqual(result, value)


class TestHeaderParsing(unittest.TestCase):
    """Test MIDI file header parsing."""

    def test_format_0(self) -> None:
        """Format 0 single-track file parses correctly."""
        data: bytes = _build_simple_midi()
        parser: MidiParser = MidiParser(data)
        self.assertEqual(parser.header.format_type, 0)
        self.assertEqual(parser.header.num_tracks, 1)
        self.assertEqual(parser.header.ticks_per_quarter, TEST_TPQ)

    def test_format_1(self) -> None:
        """Format 1 multi-track header parses correctly."""
        track1: bytes = _build_track(_set_tempo(0, 120) + _end_of_track(0))
        track2: bytes = _build_track(
            _note_on(0, 0, 60, 100) + _note_off(TEST_TPQ, 0, 60)
            + _end_of_track(0)
        )
        header: bytes = _build_header(format_type=1, num_tracks=2)
        data: bytes = header + track1 + track2
        parser: MidiParser = MidiParser(data)
        self.assertEqual(parser.header.format_type, 1)
        self.assertEqual(parser.header.num_tracks, 2)
        self.assertEqual(parser.num_tracks, 2)

    def test_invalid_magic(self) -> None:
        """Non-MIDI data raises ValueError."""
        with self.assertRaises(ValueError):
            MidiParser(b"NOT A MIDI FILE")

    def test_truncated_header(self) -> None:
        """Truncated header raises ValueError."""
        with self.assertRaises(ValueError):
            MidiParser(HEADER_CHUNK_ID + b"\x00")


class TestNoteEvents(unittest.TestCase):
    """Test parsing of note on/off events."""

    def test_note_on_off(self) -> None:
        """Note on and off events parse with correct fields."""
        data: bytes = _build_simple_midi(bpm=120, notes=1)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        # Find note events.
        note_ons = [e for e in events if e.event_type == "note_on"]
        note_offs = [e for e in events if e.event_type == "note_off"]

        self.assertEqual(len(note_ons), 1)
        self.assertEqual(len(note_offs), 1)

        # Check note-on fields.
        on: MidiEvent = note_ons[0]
        self.assertEqual(on.channel, 0)
        self.assertEqual(on.note, 60)
        self.assertEqual(on.velocity, 100)

        # Check note-off fields.
        off: MidiEvent = note_offs[0]
        self.assertEqual(off.channel, 0)
        self.assertEqual(off.note, 60)

    def test_velocity_zero_is_note_off(self) -> None:
        """Note-on with velocity 0 is treated as note-off."""
        track_data: bytes = (
            _set_tempo(0, 120)
            + _note_on(0, 0, 60, 100)
            + _note_on(TEST_TPQ, 0, 60, 0)  # Velocity 0 = note off.
            + _end_of_track(0)
        )
        data: bytes = _build_header(0, 1) + _build_track(track_data)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        note_offs = [e for e in events if e.event_type == "note_off"]
        self.assertEqual(len(note_offs), 1,
                         "velocity-0 note_on should become note_off")

    def test_multiple_channels(self) -> None:
        """Events on different channels parse with correct channel numbers."""
        track_data: bytes = (
            _set_tempo(0, 120)
            + _note_on(0, 0, 60, 100)
            + _note_on(0, 1, 64, 80)
            + _note_on(0, 9, 36, 127)
            + _note_off(TEST_TPQ, 0, 60)
            + _note_off(0, 1, 64)
            + _note_off(0, 9, 36)
            + _end_of_track(0)
        )
        data: bytes = _build_header(0, 1) + _build_track(track_data)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        note_ons = [e for e in events if e.event_type == "note_on"]
        channels: set[int] = {e.channel for e in note_ons}
        self.assertEqual(channels, {0, 1, 9})

    def test_note_count(self) -> None:
        """Correct number of note events for a multi-note file."""
        data: bytes = _build_simple_midi(notes=8)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        note_ons = [e for e in events if e.event_type == "note_on"]
        note_offs = [e for e in events if e.event_type == "note_off"]
        self.assertEqual(len(note_ons), 8)
        self.assertEqual(len(note_offs), 8)


class TestControlEvents(unittest.TestCase):
    """Test parsing of control change, program change, and pitch bend."""

    def test_control_change(self) -> None:
        """CC events parse with correct controller and value."""
        track_data: bytes = (
            _set_tempo(0, 120)
            + _encode_vlq(0) + bytes([0xB0, 7, 127])   # CC 7 = volume.
            + _encode_vlq(0) + bytes([0xB0, 10, 64])    # CC 10 = pan.
            + _end_of_track(0)
        )
        data: bytes = _build_header(0, 1) + _build_track(track_data)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        ccs = [e for e in events if e.event_type == "control_change"]
        self.assertEqual(len(ccs), 2)
        self.assertEqual(ccs[0].cc_number, 7)
        self.assertEqual(ccs[0].cc_value, 127)
        self.assertEqual(ccs[1].cc_number, 10)
        self.assertEqual(ccs[1].cc_value, 64)

    def test_program_change(self) -> None:
        """Program change events parse with correct program number."""
        track_data: bytes = (
            _set_tempo(0, 120)
            + _encode_vlq(0) + bytes([0xC0, 19])  # Church organ.
            + _end_of_track(0)
        )
        data: bytes = _build_header(0, 1) + _build_track(track_data)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        pcs = [e for e in events if e.event_type == "program_change"]
        self.assertEqual(len(pcs), 1)
        self.assertEqual(pcs[0].program, 19)
        self.assertEqual(pcs[0].channel, 0)

    def test_pitch_bend(self) -> None:
        """Pitch bend events parse with correct 14-bit value."""
        # Center pitch bend = 8192 = LSB 0, MSB 64.
        track_data: bytes = (
            _set_tempo(0, 120)
            + _encode_vlq(0) + bytes([0xE0, 0, 64])  # Center.
            + _end_of_track(0)
        )
        data: bytes = _build_header(0, 1) + _build_track(track_data)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        pbs = [e for e in events if e.event_type == "pitch_bend"]
        self.assertEqual(len(pbs), 1)
        self.assertEqual(pbs[0].pitch_bend, 8192)


class TestMetaEvents(unittest.TestCase):
    """Test parsing of meta events."""

    def test_tempo(self) -> None:
        """Set-tempo meta events parse with correct BPM."""
        data: bytes = _build_simple_midi(bpm=140, notes=1)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        tempos = [e for e in events if e.event_type == "set_tempo"]
        self.assertEqual(len(tempos), 1)
        self.assertAlmostEqual(tempos[0].tempo_bpm, 140.0, places=0)

    def test_track_name(self) -> None:
        """Track name meta events parse correctly."""
        name_bytes: bytes = b"Piano"
        track_data: bytes = (
            _meta_event(0, META_TRACK_NAME, name_bytes)
            + _end_of_track(0)
        )
        data: bytes = _build_header(0, 1) + _build_track(track_data)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        names = [e for e in events if e.event_type == "track_name"]
        self.assertEqual(len(names), 1)
        self.assertEqual(names[0].meta_value, "Piano")

    def test_time_signature(self) -> None:
        """Time signature meta events parse correctly."""
        # 3/4 time: numerator=3, denominator=2 (2^2=4), clocks=24, 32nds=8
        ts_data: bytes = bytes([3, 2, 24, 8])
        track_data: bytes = (
            _meta_event(0, META_TIME_SIGNATURE, ts_data)
            + _end_of_track(0)
        )
        data: bytes = _build_header(0, 1) + _build_track(track_data)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        sigs = [e for e in events if e.event_type == "time_signature"]
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].meta_value, "3/4")

    def test_end_of_track(self) -> None:
        """End-of-track meta event is present."""
        data: bytes = _build_simple_midi(notes=1)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        eots = [e for e in events if e.event_type == "end_of_track"]
        self.assertGreaterEqual(len(eots), 1)


class TestTempoMapping(unittest.TestCase):
    """Test tick-to-seconds conversion via the tempo map."""

    def test_default_tempo(self) -> None:
        """Files with no tempo event default to 120 BPM."""
        track_data: bytes = (
            _note_on(0, 0, 60, 100)
            + _note_off(TEST_TPQ, 0, 60)
            + _end_of_track(0)
        )
        data: bytes = _build_header(0, 1) + _build_track(track_data)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        note_off = [e for e in events if e.event_type == "note_off"][0]
        # 480 ticks at 120 BPM, 480 TPQ = exactly 0.5 seconds.
        self.assertAlmostEqual(note_off.time_s, 0.5, places=3)

    def test_120_bpm(self) -> None:
        """At 120 BPM, one quarter note = 0.5 seconds."""
        data: bytes = _build_simple_midi(bpm=120, notes=1)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        note_off = [e for e in events if e.event_type == "note_off"][0]
        self.assertAlmostEqual(note_off.time_s, 0.5, places=3)

    def test_60_bpm(self) -> None:
        """At 60 BPM, one quarter note = 1.0 seconds."""
        data: bytes = _build_simple_midi(bpm=60, notes=1)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        note_off = [e for e in events if e.event_type == "note_off"][0]
        self.assertAlmostEqual(note_off.time_s, 1.0, places=3)

    def test_240_bpm(self) -> None:
        """At 240 BPM, one quarter note = 0.25 seconds."""
        data: bytes = _build_simple_midi(bpm=240, notes=1)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        note_off = [e for e in events if e.event_type == "note_off"][0]
        self.assertAlmostEqual(note_off.time_s, 0.25, places=3)

    def test_events_sorted_by_time(self) -> None:
        """Events are returned sorted by time_s."""
        data: bytes = _build_simple_midi(bpm=120, notes=8)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        times: list[float] = [e.time_s for e in events]
        self.assertEqual(times, sorted(times))

    def test_tempo_change_mid_track(self) -> None:
        """Tempo change mid-track adjusts timing of subsequent events."""
        # Start at 120 BPM, change to 60 BPM after one quarter note.
        track_data: bytes = (
            _set_tempo(0, 120)
            + _note_on(0, 0, 60, 100)
            + _note_off(TEST_TPQ, 0, 60)       # 0.5s at 120 BPM
            + _set_tempo(0, 60)                  # Tempo change
            + _note_on(0, 0, 64, 100)
            + _note_off(TEST_TPQ, 0, 64)        # 1.0s at 60 BPM
            + _end_of_track(0)
        )
        data: bytes = _build_header(0, 1) + _build_track(track_data)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        note_offs = [e for e in events if e.event_type == "note_off"]
        self.assertEqual(len(note_offs), 2)

        # First note off at 0.5s (120 BPM).
        self.assertAlmostEqual(note_offs[0].time_s, 0.5, places=2)

        # Second note off at 0.5 + 1.0 = 1.5s (60 BPM after the change).
        self.assertAlmostEqual(note_offs[1].time_s, 1.5, places=2)


class TestEventToDict(unittest.TestCase):
    """Test MidiEvent.to_dict() serialization."""

    def test_note_on_dict(self) -> None:
        """Note-on to_dict includes expected keys and omits unused ones."""
        event: MidiEvent = MidiEvent(
            track=0, tick=480, time_s=0.5,
            event_type="note_on", channel=1, note=64, velocity=100,
        )
        d: dict = event.to_dict()
        self.assertEqual(d["event_type"], "note_on")
        self.assertEqual(d["channel"], 1)
        self.assertEqual(d["note"], 64)
        self.assertEqual(d["velocity"], 100)
        # Unused fields should be absent.
        self.assertNotIn("cc_number", d)
        self.assertNotIn("program", d)
        self.assertNotIn("pitch_bend", d)

    def test_cc_dict(self) -> None:
        """Control change to_dict includes cc_number and cc_value."""
        event: MidiEvent = MidiEvent(
            track=0, tick=0, time_s=0.0,
            event_type="control_change", channel=0,
            cc_number=7, cc_value=127,
        )
        d: dict = event.to_dict()
        self.assertEqual(d["cc_number"], 7)
        self.assertEqual(d["cc_value"], 127)
        self.assertNotIn("note", d)

    def test_meta_dict(self) -> None:
        """Meta event to_dict includes meta_type and meta_value."""
        event: MidiEvent = MidiEvent(
            track=0, tick=0, time_s=0.0,
            event_type="set_tempo", meta_type=0x51,
            meta_value="120.00 BPM", tempo_bpm=120.0,
        )
        d: dict = event.to_dict()
        self.assertEqual(d["meta_type"], 0x51)
        self.assertEqual(d["meta_value"], "120.00 BPM")
        self.assertEqual(d["tempo_bpm"], 120.0)


class TestSummary(unittest.TestCase):
    """Test the parser summary method."""

    def test_summary_fields(self) -> None:
        """Summary returns all expected keys."""
        data: bytes = _build_simple_midi(bpm=120, notes=4)
        parser: MidiParser = MidiParser(data)
        summary: dict = parser.summary()

        self.assertIn("format", summary)
        self.assertIn("tracks", summary)
        self.assertIn("ticks_per_quarter", summary)
        self.assertIn("duration_s", summary)
        self.assertIn("total_events", summary)
        self.assertIn("note_events", summary)
        self.assertIn("tempo_bpm", summary)

    def test_summary_note_count(self) -> None:
        """Summary note_events matches expected count."""
        data: bytes = _build_simple_midi(bpm=120, notes=6)
        parser: MidiParser = MidiParser(data)
        summary: dict = parser.summary()
        # 6 note-on + 6 note-off = 12 note events.
        self.assertEqual(summary["note_events"], 12)


class TestRunningStatus(unittest.TestCase):
    """Test running status (implicit status byte reuse)."""

    def test_running_status_notes(self) -> None:
        """Sequential notes using running status parse correctly."""
        # First note has explicit status, second omits it.
        track_data: bytes = (
            _set_tempo(0, 120)
            + _encode_vlq(0) + bytes([0x90, 60, 100])  # Explicit note on.
            + _encode_vlq(TEST_TPQ) + bytes([64, 80])   # Running status.
            + _encode_vlq(TEST_TPQ) + bytes([0x80, 60, 0])  # Explicit off.
            + _encode_vlq(0) + bytes([64, 0])            # Running status off.
            + _end_of_track(0)
        )
        data: bytes = _build_header(0, 1) + _build_track(track_data)
        parser: MidiParser = MidiParser(data)
        events: list[MidiEvent] = parser.events()

        note_ons = [e for e in events if e.event_type == "note_on"]
        note_offs = [e for e in events if e.event_type == "note_off"]
        self.assertEqual(len(note_ons), 2)
        self.assertEqual(len(note_offs), 2)
        self.assertEqual(note_ons[0].note, 60)
        self.assertEqual(note_ons[1].note, 64)


class TestRealFile(unittest.TestCase):
    """Tests against the real BWV 565 MIDI file.

    These tests are skipped if the file is not present on this
    machine.  They validate the parser against real-world data
    produced by an actual MIDI editor.
    """

    @unittest.skipUnless(
        Path(BWV565_PATH).exists(),
        f"BWV 565 file not found at {BWV565_PATH}",
    )
    def test_bwv565_parses(self) -> None:
        """BWV 565 parses without errors."""
        parser: MidiParser = MidiParser(BWV565_PATH)
        events: list[MidiEvent] = parser.events()
        self.assertGreater(len(events), 1000)

    @unittest.skipUnless(
        Path(BWV565_PATH).exists(),
        f"BWV 565 file not found at {BWV565_PATH}",
    )
    def test_bwv565_summary(self) -> None:
        """BWV 565 summary has expected structure."""
        parser: MidiParser = MidiParser(BWV565_PATH)
        summary: dict = parser.summary()
        self.assertEqual(summary["format"], 1)
        self.assertGreater(summary["tracks"], 1)
        self.assertGreater(summary["duration_s"], 400)
        self.assertGreater(summary["note_events"], 5000)

    @unittest.skipUnless(
        Path(BWV565_PATH).exists(),
        f"BWV 565 file not found at {BWV565_PATH}",
    )
    def test_bwv565_has_organ_tracks(self) -> None:
        """BWV 565 contains Swell, Great, and Pedal track names."""
        parser: MidiParser = MidiParser(BWV565_PATH)
        events: list[MidiEvent] = parser.events()

        track_names: list[str] = [
            e.meta_value for e in events if e.event_type == "track_name"
        ]
        self.assertIn("Swell", track_names)
        self.assertIn("Great", track_names)
        self.assertIn("Pedal", track_names)

    @unittest.skipUnless(
        Path(BWV565_PATH).exists(),
        f"BWV 565 file not found at {BWV565_PATH}",
    )
    def test_bwv565_three_channels(self) -> None:
        """BWV 565 uses channels 0, 1, and 2 (three organ manuals)."""
        parser: MidiParser = MidiParser(BWV565_PATH)
        events: list[MidiEvent] = parser.events()

        channels: set[int] = {
            e.channel for e in events
            if e.event_type in ("note_on", "note_off")
        }
        self.assertIn(0, channels)
        self.assertIn(1, channels)
        self.assertIn(2, channels)


if __name__ == "__main__":
    unittest.main()
