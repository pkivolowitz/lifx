"""Pure-Python MIDI file parser — no external dependencies.

Parses Standard MIDI Files (SMF) formats 0, 1, and 2 into a stream
of :class:`MidiEvent` dataclasses.  The parser is source-agnostic —
it works on file bytes, so the same code can be reused for database
replay or any other byte source.

The parser handles:

* Variable-length quantities (VLQ) for delta times.
* Running status (implicit status byte reuse).
* Meta events (tempo, time signature, key signature, track name, etc.).
* System exclusive (SysEx) events.
* All channel voice messages (note on/off, CC, program change,
  pitch bend, channel pressure, poly aftertouch).

Tempo mapping converts raw tick positions to wall-clock seconds,
enabling real-time replay at the original tempo.

Usage::

    from distributed.midi_parser import MidiParser

    parser = MidiParser("song.mid")
    for event in parser.events():
        print(event)

References:
    * Standard MIDI File 1.0 spec (MMA, 1996)
    * https://www.music.mcgill.ca/~ich/classes/mumt306/StandardMIDIfileformat.html
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Union

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Chunk identifiers (4-byte ASCII).
HEADER_CHUNK_ID: bytes = b"MThd"
TRACK_CHUNK_ID: bytes = b"MTrk"

# Header chunk data length (always 6 bytes).
HEADER_DATA_LENGTH: int = 6

# MIDI status byte ranges.
STATUS_NOTE_OFF: int = 0x80
STATUS_NOTE_ON: int = 0x90
STATUS_POLY_AFTERTOUCH: int = 0xA0
STATUS_CONTROL_CHANGE: int = 0xB0
STATUS_PROGRAM_CHANGE: int = 0xC0
STATUS_CHANNEL_PRESSURE: int = 0xD0
STATUS_PITCH_BEND: int = 0xE0
STATUS_SYSTEM: int = 0xF0

# System messages.
STATUS_SYSEX: int = 0xF0
STATUS_SYSEX_END: int = 0xF7
STATUS_META: int = 0xFF

# Meta event types.
META_SEQUENCE_NUMBER: int = 0x00
META_TEXT: int = 0x01
META_COPYRIGHT: int = 0x02
META_TRACK_NAME: int = 0x03
META_INSTRUMENT_NAME: int = 0x04
META_LYRIC: int = 0x05
META_MARKER: int = 0x06
META_CUE_POINT: int = 0x07
META_CHANNEL_PREFIX: int = 0x20
META_END_OF_TRACK: int = 0x2F
META_SET_TEMPO: int = 0x51
META_SMPTE_OFFSET: int = 0x54
META_TIME_SIGNATURE: int = 0x58
META_KEY_SIGNATURE: int = 0x59
META_SEQUENCER_SPECIFIC: int = 0x7F

# Default tempo: 120 BPM = 500000 microseconds per quarter note.
DEFAULT_TEMPO_US: int = 500000

# Number of data bytes per channel voice status (keyed by high nibble).
# Program Change and Channel Pressure take 1 data byte; all others take 2.
VOICE_DATA_BYTES: dict[int, int] = {
    0x80: 2,  # Note Off
    0x90: 2,  # Note On
    0xA0: 2,  # Poly Aftertouch
    0xB0: 2,  # Control Change
    0xC0: 1,  # Program Change
    0xD0: 1,  # Channel Pressure
    0xE0: 2,  # Pitch Bend
}

# Human-readable event type names.
EVENT_TYPE_NAMES: dict[int, str] = {
    0x80: "note_off",
    0x90: "note_on",
    0xA0: "poly_aftertouch",
    0xB0: "control_change",
    0xC0: "program_change",
    0xD0: "channel_pressure",
    0xE0: "pitch_bend",
}

# Human-readable meta event type names.
META_TYPE_NAMES: dict[int, str] = {
    META_SEQUENCE_NUMBER: "sequence_number",
    META_TEXT: "text",
    META_COPYRIGHT: "copyright",
    META_TRACK_NAME: "track_name",
    META_INSTRUMENT_NAME: "instrument_name",
    META_LYRIC: "lyric",
    META_MARKER: "marker",
    META_CUE_POINT: "cue_point",
    META_CHANNEL_PREFIX: "channel_prefix",
    META_END_OF_TRACK: "end_of_track",
    META_SET_TEMPO: "set_tempo",
    META_SMPTE_OFFSET: "smpte_offset",
    META_TIME_SIGNATURE: "time_signature",
    META_KEY_SIGNATURE: "key_signature",
    META_SEQUENCER_SPECIFIC: "sequencer_specific",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MidiHeader:
    """Parsed MIDI file header.

    Attributes:
        format_type: SMF format (0 = single track, 1 = multi-track
                     synchronous, 2 = multi-track asynchronous).
        num_tracks:  Number of track chunks in the file.
        ticks_per_quarter: Ticks per quarter note (if positive).
                           Negative values indicate SMPTE time division
                           (not yet supported).
    """
    format_type: int
    num_tracks: int
    ticks_per_quarter: int


@dataclass
class MidiEvent:
    """A single parsed MIDI event with absolute timing.

    Every event produced by the parser carries both the raw tick
    position and the computed wall-clock time in seconds.  This
    makes the event stream directly usable for replay without
    needing access to the tempo map.

    Attributes:
        track:       Track index (0-based).
        tick:        Absolute tick position within the track.
        time_s:      Wall-clock time in seconds (computed from tempo map).
        event_type:  Human-readable type string (e.g. ``"note_on"``).
        channel:     MIDI channel (0-15) for voice messages, -1 for meta/sysex.
        note:        Note number (0-127) for note on/off, -1 otherwise.
        velocity:    Velocity (0-127) for note on/off, -1 otherwise.
        cc_number:   Controller number for CC events, -1 otherwise.
        cc_value:    Controller value for CC events, -1 otherwise.
        program:     Program number for program change, -1 otherwise.
        pitch_bend:  14-bit pitch bend value (0-16383, center=8192), -1 otherwise.
        pressure:    Pressure value for aftertouch events, -1 otherwise.
        meta_type:   Meta event type byte, -1 for non-meta events.
        meta_value:  Meta event data as string (text events) or hex string.
        tempo_bpm:   Tempo in BPM (set only for set_tempo meta events).
        raw_status:  Original status byte.
    """
    track: int = 0
    tick: int = 0
    time_s: float = 0.0
    event_type: str = ""
    channel: int = -1
    note: int = -1
    velocity: int = -1
    cc_number: int = -1
    cc_value: int = -1
    program: int = -1
    pitch_bend: int = -1
    pressure: int = -1
    meta_type: int = -1
    meta_value: str = ""
    tempo_bpm: float = -1.0
    raw_status: int = 0

    def to_dict(self) -> dict:
        """Convert to a JSON-safe dictionary, omitting unused fields.

        Returns:
            Dict with only the fields relevant to this event type.
        """
        d: dict = {
            "track": self.track,
            "tick": self.tick,
            "time_s": round(self.time_s, 6),
            "event_type": self.event_type,
        }
        if self.channel >= 0:
            d["channel"] = self.channel
        if self.note >= 0:
            d["note"] = self.note
        if self.velocity >= 0:
            d["velocity"] = self.velocity
        if self.cc_number >= 0:
            d["cc_number"] = self.cc_number
            d["cc_value"] = self.cc_value
        if self.program >= 0:
            d["program"] = self.program
        if self.pitch_bend >= 0:
            d["pitch_bend"] = self.pitch_bend
        if self.pressure >= 0:
            d["pressure"] = self.pressure
        if self.meta_type >= 0:
            d["meta_type"] = self.meta_type
            if self.meta_value:
                d["meta_value"] = self.meta_value
        if self.tempo_bpm > 0:
            d["tempo_bpm"] = round(self.tempo_bpm, 2)
        return d


@dataclass
class TempoChange:
    """A tempo change at a specific tick position.

    Used to build the tempo map for tick-to-seconds conversion.

    Attributes:
        tick:     Absolute tick where the tempo changes.
        tempo_us: Microseconds per quarter note.
        time_s:   Wall-clock second at this tick (computed during mapping).
    """
    tick: int
    tempo_us: int
    time_s: float = 0.0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class MidiParser:
    """Parse a Standard MIDI File into a stream of :class:`MidiEvent`.

    The parser reads the entire file into memory (MIDI files are small —
    typically under 1 MB even for complex orchestral scores), parses
    the header and all track chunks, builds a tempo map, and yields
    events in chronological order with absolute tick and time_s values.

    Args:
        source: Path to a ``.mid`` file, or raw bytes.
    """

    def __init__(self, source: Union[str, Path, bytes]) -> None:
        """Initialize the parser from a file path or raw bytes.

        Args:
            source: File path (str or Path) or raw MIDI bytes.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError:        If the data is not a valid MIDI file.
        """
        if isinstance(source, bytes):
            self._data: bytes = source
        else:
            path: Path = Path(source)
            if not path.exists():
                raise FileNotFoundError(f"MIDI file not found: {path}")
            self._data = path.read_bytes()

        self._pos: int = 0
        self._header: Optional[MidiHeader] = None
        self._tracks_raw: list[bytes] = []

        self._parse_file()

    @property
    def header(self) -> MidiHeader:
        """The parsed MIDI file header.

        Returns:
            :class:`MidiHeader` with format, track count, and timing.

        Raises:
            ValueError: If the file was not parsed successfully.
        """
        if self._header is None:
            raise ValueError("MIDI file not parsed")
        return self._header

    @property
    def num_tracks(self) -> int:
        """Number of tracks in the file."""
        return len(self._tracks_raw)

    # -------------------------------------------------------------------
    # File-level parsing
    # -------------------------------------------------------------------

    def _parse_file(self) -> None:
        """Parse the header chunk and collect raw track data."""
        self._parse_header()
        for _ in range(self._header.num_tracks):
            self._parse_track_chunk()

    def _parse_header(self) -> None:
        """Parse the MThd header chunk.

        Raises:
            ValueError: If the chunk ID or data length is wrong.
        """
        chunk_id: bytes = self._read_bytes(4)
        if chunk_id != HEADER_CHUNK_ID:
            raise ValueError(
                f"Not a MIDI file: expected {HEADER_CHUNK_ID!r}, "
                f"got {chunk_id!r}"
            )

        data_len: int = self._read_uint32()
        if data_len < HEADER_DATA_LENGTH:
            raise ValueError(
                f"Invalid header length: {data_len} (expected >= {HEADER_DATA_LENGTH})"
            )

        format_type: int = self._read_uint16()
        num_tracks: int = self._read_uint16()
        division: int = self._read_uint16()

        # Skip any extra header bytes (spec allows > 6).
        extra: int = data_len - HEADER_DATA_LENGTH
        if extra > 0:
            self._read_bytes(extra)

        # Check for SMPTE time division (high bit set).
        if division & 0x8000:
            raise ValueError(
                "SMPTE time division is not supported.  "
                "Only ticks-per-quarter-note files are handled."
            )

        self._header = MidiHeader(
            format_type=format_type,
            num_tracks=num_tracks,
            ticks_per_quarter=division,
        )

    def _parse_track_chunk(self) -> None:
        """Read one MTrk chunk and store its raw event bytes.

        Raises:
            ValueError: If the chunk ID is not MTrk.
        """
        chunk_id: bytes = self._read_bytes(4)
        if chunk_id != TRACK_CHUNK_ID:
            raise ValueError(
                f"Expected track chunk {TRACK_CHUNK_ID!r}, "
                f"got {chunk_id!r}"
            )
        data_len: int = self._read_uint32()
        track_data: bytes = self._read_bytes(data_len)
        self._tracks_raw.append(track_data)

    # -------------------------------------------------------------------
    # Event parsing
    # -------------------------------------------------------------------

    def events(self) -> list[MidiEvent]:
        """Parse all tracks and return events sorted by time.

        For format 0 and 1 files, events from all tracks are merged
        and sorted by ``(time_s, track)``.  For format 2, tracks are
        independent (each has its own timeline), but are still merged
        for uniform output.

        Returns:
            List of :class:`MidiEvent` sorted chronologically.
        """
        all_events: list[MidiEvent] = []

        for track_idx, track_data in enumerate(self._tracks_raw):
            track_events: list[MidiEvent] = self._parse_track_events(
                track_idx, track_data,
            )
            all_events.extend(track_events)

        # Build tempo map from all tempo events, then apply times.
        tempo_map: list[TempoChange] = self._build_tempo_map(all_events)
        self._apply_tempo_map(all_events, tempo_map)

        # Sort by time, then track, then tick for stability.
        all_events.sort(key=lambda e: (e.time_s, e.track, e.tick))

        return all_events

    def _parse_track_events(self, track_idx: int,
                            data: bytes) -> list[MidiEvent]:
        """Parse raw track bytes into a list of MidiEvent.

        Args:
            track_idx: Zero-based track index.
            data:      Raw bytes from the MTrk chunk.

        Returns:
            List of events with absolute tick positions (time_s not yet set).
        """
        events: list[MidiEvent] = []
        pos: int = 0
        abs_tick: int = 0
        running_status: int = 0

        while pos < len(data):
            try:
                # Read delta time (variable-length quantity).
                delta, pos = self._read_vlq(data, pos)
            except ValueError:
                # Truncated or malformed VLQ — stop parsing this track
                # gracefully rather than crashing on bad input.
                logger.warning(
                    "Malformed VLQ in track %d at offset %d, "
                    "truncating", track_idx, pos,
                )
                break
            abs_tick += delta

            # Peek at the next byte to determine the event type.
            if pos >= len(data):
                break

            status_byte: int = data[pos]

            if status_byte == STATUS_META:
                # Meta event: FF <type> <length> <data>
                pos += 1  # Skip 0xFF.
                if pos >= len(data):
                    break
                meta_type: int = data[pos]
                pos += 1
                meta_len, pos = self._read_vlq(data, pos)
                meta_data: bytes = data[pos:pos + meta_len]
                pos += meta_len

                event: MidiEvent = self._make_meta_event(
                    track_idx, abs_tick, meta_type, meta_data,
                )
                events.append(event)

                if meta_type == META_END_OF_TRACK:
                    break

            elif status_byte == STATUS_SYSEX or status_byte == STATUS_SYSEX_END:
                # SysEx event: F0 <length> <data> or F7 <length> <data>.
                pos += 1
                sysex_len, pos = self._read_vlq(data, pos)
                pos += sysex_len  # Skip sysex data.
                running_status = 0  # SysEx clears running status.

            elif status_byte & 0x80:
                # Channel voice/mode message with explicit status byte.
                running_status = status_byte
                pos += 1
                event, pos = self._parse_voice_event(
                    track_idx, abs_tick, running_status, data, pos,
                )
                events.append(event)

            else:
                # Running status — this byte is data, not status.
                if running_status == 0:
                    # No running status established; skip this byte.
                    pos += 1
                    continue
                event, pos = self._parse_voice_event(
                    track_idx, abs_tick, running_status, data, pos,
                )
                events.append(event)

        return events

    def _parse_voice_event(self, track_idx: int, tick: int,
                           status: int, data: bytes,
                           pos: int) -> tuple[MidiEvent, int]:
        """Parse a channel voice message from the data stream.

        The status byte has already been consumed; ``pos`` points at
        the first data byte.

        Args:
            track_idx: Track index.
            tick:      Absolute tick position.
            status:    Status byte (with channel in low nibble).
            data:      Raw track bytes.
            pos:       Current position (first data byte).

        Returns:
            Tuple of (MidiEvent, new_pos).
        """
        high_nibble: int = status & 0xF0
        channel: int = status & 0x0F
        num_data: int = VOICE_DATA_BYTES.get(high_nibble, 2)

        # Read data bytes.
        data_bytes: list[int] = []
        for _ in range(num_data):
            if pos < len(data):
                data_bytes.append(data[pos])
                pos += 1
            else:
                data_bytes.append(0)

        event_type: str = EVENT_TYPE_NAMES.get(high_nibble, f"unknown_0x{high_nibble:02X}")

        event: MidiEvent = MidiEvent(
            track=track_idx,
            tick=tick,
            event_type=event_type,
            channel=channel,
            raw_status=status,
        )

        # Populate fields based on event type.
        if high_nibble == STATUS_NOTE_OFF:
            event.note = data_bytes[0]
            event.velocity = data_bytes[1]

        elif high_nibble == STATUS_NOTE_ON:
            event.note = data_bytes[0]
            event.velocity = data_bytes[1]
            # Velocity 0 on note_on is conventionally note_off.
            if event.velocity == 0:
                event.event_type = "note_off"

        elif high_nibble == STATUS_POLY_AFTERTOUCH:
            event.note = data_bytes[0]
            event.pressure = data_bytes[1]

        elif high_nibble == STATUS_CONTROL_CHANGE:
            event.cc_number = data_bytes[0]
            event.cc_value = data_bytes[1]

        elif high_nibble == STATUS_PROGRAM_CHANGE:
            event.program = data_bytes[0]

        elif high_nibble == STATUS_CHANNEL_PRESSURE:
            event.pressure = data_bytes[0]

        elif high_nibble == STATUS_PITCH_BEND:
            # 14-bit value: LSB first, then MSB.
            event.pitch_bend = data_bytes[0] | (data_bytes[1] << 7)

        return event, pos

    def _make_meta_event(self, track_idx: int, tick: int,
                         meta_type: int, meta_data: bytes) -> MidiEvent:
        """Create a MidiEvent for a meta event.

        Args:
            track_idx: Track index.
            tick:      Absolute tick position.
            meta_type: Meta event type byte.
            meta_data: Raw meta event data.

        Returns:
            Populated MidiEvent.
        """
        type_name: str = META_TYPE_NAMES.get(
            meta_type, f"meta_0x{meta_type:02X}",
        )

        event: MidiEvent = MidiEvent(
            track=track_idx,
            tick=tick,
            event_type=type_name,
            meta_type=meta_type,
            raw_status=STATUS_META,
        )

        # Extract human-readable values for common meta events.
        if meta_type in (META_TEXT, META_COPYRIGHT, META_TRACK_NAME,
                         META_INSTRUMENT_NAME, META_LYRIC, META_MARKER,
                         META_CUE_POINT):
            try:
                event.meta_value = meta_data.decode("utf-8", errors="replace")
            except Exception:
                event.meta_value = meta_data.hex()

        elif meta_type == META_SET_TEMPO:
            if len(meta_data) >= 3:
                tempo_us: int = (
                    (meta_data[0] << 16) | (meta_data[1] << 8) | meta_data[2]
                )
                event.tempo_bpm = 60_000_000.0 / tempo_us if tempo_us > 0 else 120.0
                event.meta_value = f"{event.tempo_bpm:.2f} BPM"

        elif meta_type == META_TIME_SIGNATURE:
            if len(meta_data) >= 4:
                numerator: int = meta_data[0]
                denominator: int = 2 ** meta_data[1]
                event.meta_value = f"{numerator}/{denominator}"

        elif meta_type == META_KEY_SIGNATURE:
            if len(meta_data) >= 2:
                sf: int = struct.unpack("b", meta_data[0:1])[0]  # Signed.
                mi: int = meta_data[1]
                mode: str = "minor" if mi else "major"
                event.meta_value = f"sf={sf} {mode}"

        else:
            if meta_data:
                event.meta_value = meta_data.hex()

        return event

    # -------------------------------------------------------------------
    # Tempo mapping
    # -------------------------------------------------------------------

    def _build_tempo_map(self, events: list[MidiEvent]) -> list[TempoChange]:
        """Extract tempo changes from events and compute their wall-clock times.

        Args:
            events: All parsed events (time_s not yet set).

        Returns:
            Sorted list of :class:`TempoChange` with ``time_s`` populated.
        """
        if self._header is None:
            raise ValueError("Header not parsed")

        tpq: int = self._header.ticks_per_quarter

        # Collect tempo changes.
        changes: list[TempoChange] = []
        for ev in events:
            if ev.meta_type == META_SET_TEMPO and ev.tempo_bpm > 0:
                tempo_us: int = int(60_000_000.0 / ev.tempo_bpm)
                changes.append(TempoChange(tick=ev.tick, tempo_us=tempo_us))

        # If no tempo events, assume default (120 BPM).
        if not changes:
            changes.append(TempoChange(tick=0, tempo_us=DEFAULT_TEMPO_US))

        # Sort by tick.
        changes.sort(key=lambda c: c.tick)

        # Ensure there's a tempo at tick 0.
        if changes[0].tick > 0:
            changes.insert(0, TempoChange(tick=0, tempo_us=DEFAULT_TEMPO_US))

        # Compute wall-clock time at each tempo change.
        for i in range(1, len(changes)):
            prev: TempoChange = changes[i - 1]
            curr: TempoChange = changes[i]
            delta_ticks: int = curr.tick - prev.tick
            # Time = ticks × (microseconds_per_tick) / 1e6
            us_per_tick: float = prev.tempo_us / tpq
            curr.time_s = prev.time_s + (delta_ticks * us_per_tick) / 1_000_000.0

        return changes

    def _apply_tempo_map(self, events: list[MidiEvent],
                         tempo_map: list[TempoChange]) -> None:
        """Set ``time_s`` on every event using the tempo map.

        Args:
            events:    All events to update (modified in place).
            tempo_map: Sorted list of tempo changes.
        """
        if self._header is None:
            raise ValueError("Header not parsed")

        tpq: int = self._header.ticks_per_quarter
        map_len: int = len(tempo_map)

        for ev in events:
            # Find the tempo region for this event's tick.
            # Linear scan is fine — tempo changes are rare (typically < 10).
            region_idx: int = 0
            for i in range(map_len - 1, -1, -1):
                if ev.tick >= tempo_map[i].tick:
                    region_idx = i
                    break

            region: TempoChange = tempo_map[region_idx]
            delta_ticks: int = ev.tick - region.tick
            us_per_tick: float = region.tempo_us / tpq
            ev.time_s = region.time_s + (delta_ticks * us_per_tick) / 1_000_000.0

    # -------------------------------------------------------------------
    # Low-level read helpers
    # -------------------------------------------------------------------

    def _read_bytes(self, count: int) -> bytes:
        """Read ``count`` bytes from the internal buffer.

        Args:
            count: Number of bytes to read.

        Returns:
            The bytes read.

        Raises:
            ValueError: If not enough data remains.
        """
        if self._pos + count > len(self._data):
            raise ValueError(
                f"Unexpected end of MIDI data at offset {self._pos} "
                f"(need {count} bytes, {len(self._data) - self._pos} remain)"
            )
        result: bytes = self._data[self._pos:self._pos + count]
        self._pos += count
        return result

    def _read_uint16(self) -> int:
        """Read a big-endian unsigned 16-bit integer.

        Returns:
            Integer value.
        """
        raw: bytes = self._read_bytes(2)
        return struct.unpack(">H", raw)[0]

    def _read_uint32(self) -> int:
        """Read a big-endian unsigned 32-bit integer.

        Returns:
            Integer value.
        """
        raw: bytes = self._read_bytes(4)
        return struct.unpack(">I", raw)[0]

    @staticmethod
    def _read_vlq(data: bytes, pos: int) -> tuple[int, int]:
        """Read a MIDI variable-length quantity (VLQ).

        VLQ encoding uses 7 bits per byte, with the high bit indicating
        continuation.  Maximum 4 bytes (28-bit value).

        Args:
            data: Raw byte buffer.
            pos:  Starting position.

        Returns:
            Tuple of (decoded_value, new_position).

        Raises:
            ValueError: If the VLQ exceeds 4 bytes.
        """
        value: int = 0
        max_bytes: int = 4

        for i in range(max_bytes):
            if pos >= len(data):
                raise ValueError(
                    f"Unexpected end of data reading VLQ at offset {pos}"
                )
            byte: int = data[pos]
            pos += 1
            value = (value << 7) | (byte & 0x7F)
            if not (byte & 0x80):
                return value, pos

        raise ValueError(
            f"VLQ exceeds maximum 4 bytes at offset {pos - max_bytes}"
        )

    # -------------------------------------------------------------------
    # Summary / info
    # -------------------------------------------------------------------

    def summary(self) -> dict:
        """Return a summary of the MIDI file.

        Parses all events to compute duration, event counts, etc.

        Returns:
            Dict with keys: format, tracks, ticks_per_quarter,
            duration_s, total_events, note_events, tempo_bpm.
        """
        events: list[MidiEvent] = self.events()

        note_count: int = sum(
            1 for e in events if e.event_type in ("note_on", "note_off")
        )

        # Find initial tempo.
        initial_tempo: float = 120.0
        for e in events:
            if e.event_type == "set_tempo":
                initial_tempo = e.tempo_bpm
                break

        duration: float = events[-1].time_s if events else 0.0

        return {
            "format": self._header.format_type,
            "tracks": self._header.num_tracks,
            "ticks_per_quarter": self._header.ticks_per_quarter,
            "duration_s": round(duration, 3),
            "total_events": len(events),
            "note_events": note_count,
            "tempo_bpm": round(initial_tempo, 2),
        }
