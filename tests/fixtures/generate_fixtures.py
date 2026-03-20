#!/usr/bin/env python3
"""Generate deterministic test fixtures for the distributed test suite.

Creates known-value MIDI, WAV, and MP3 files that can be regenerated
at any time with identical content.  All values are chosen to make
test assertions straightforward.

Generated files
---------------
test_scale.mid
    A one-octave C major scale (C4-C5) on channel 0.  Each note is
    250ms at velocity 100, with 50ms gaps.  Total duration ~2.4s.
    Known event count: 16 (8 note_on + 8 note_off).

test_440hz.wav
    A 440 Hz sine wave, mono 16-bit PCM at 44100 Hz, exactly 1 second.
    Peak amplitude at int16 max (32767).  Known sample count: 44100.
    Known RMS: ~0.707 * 32767 ≈ 23170.

test_c_major_chord.wav
    Simultaneous C4+E4+G4 (261.6+329.6+392.0 Hz) for FFT validation.
    1 second, 44100 Hz, 16-bit mono.  The FFT should show energy peaks
    at these three frequencies and minimal energy elsewhere.

test_440hz.mp3
    The 440 Hz sine encoded as MP3 via ffmpeg (if available).  Used to
    test MP3 codec handling and graceful degradation when ffmpeg is
    absent.

test_corrupt.mp3
    Random bytes with an .mp3 extension.  Tests graceful handling of
    codec errors and corrupt files.

Run::

    python3 tests/fixtures/generate_fixtures.py
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
import os
import struct
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXTURES_DIR: Path = Path(__file__).parent

# MIDI constants.
MIDI_NOTES: list[int] = [60, 62, 64, 65, 67, 69, 71, 72]  # C4 major scale to C5
MIDI_VELOCITY: int = 100
MIDI_NOTE_DURATION_TICKS: int = 240  # At 480 ticks/beat, 120 BPM → 250ms
MIDI_GAP_TICKS: int = 48            # ~50ms gap between notes
MIDI_TICKS_PER_BEAT: int = 480
MIDI_TEMPO_BPM: int = 120
MIDI_TEMPO_US: int = 60_000_000 // MIDI_TEMPO_BPM  # Microseconds per beat

# WAV constants.
WAV_SAMPLE_RATE: int = 44100
WAV_DURATION_SECONDS: float = 1.0
WAV_FREQUENCY: float = 440.0
WAV_AMPLITUDE: int = 32767  # int16 max
WAV_NUM_SAMPLES: int = int(WAV_SAMPLE_RATE * WAV_DURATION_SECONDS)
WAV_CHANNELS: int = 1
WAV_BITS_PER_SAMPLE: int = 16


# ---------------------------------------------------------------------------
# MIDI file generation (Standard MIDI File format 0)
# ---------------------------------------------------------------------------

def _write_variable_length(value: int) -> bytes:
    """Encode an integer as MIDI variable-length quantity."""
    result: list[int] = []
    result.append(value & 0x7F)
    value >>= 7
    while value > 0:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.reverse()
    return bytes(result)


def generate_midi(output_path: Path) -> dict:
    """Generate a C major scale MIDI file with known values.

    Returns:
        Dict of fixture metadata for test assertions.
    """
    track_data: bytearray = bytearray()

    # Tempo meta event: FF 51 03 <tempo_us big-endian 3 bytes>
    track_data.extend(b'\x00')  # Delta time = 0
    track_data.extend(b'\xFF\x51\x03')
    track_data.extend(MIDI_TEMPO_US.to_bytes(3, "big"))

    # Note events.
    total_ticks: int = 0
    for i, note in enumerate(MIDI_NOTES):
        # Note on (delta = gap if not first, else 0).
        delta: int = MIDI_GAP_TICKS if i > 0 else 0
        track_data.extend(_write_variable_length(delta))
        track_data.extend(bytes([0x90, note, MIDI_VELOCITY]))  # Note on ch0
        total_ticks += delta

        # Note off after duration.
        track_data.extend(_write_variable_length(MIDI_NOTE_DURATION_TICKS))
        track_data.extend(bytes([0x80, note, 0]))  # Note off ch0
        total_ticks += MIDI_NOTE_DURATION_TICKS

    # End of track meta event.
    track_data.extend(b'\x00\xFF\x2F\x00')

    # Build the complete file.
    # MThd header.
    header: bytes = b"MThd"
    header += struct.pack(">I", 6)       # Header length
    header += struct.pack(">HHH", 0,     # Format 0 (single track)
                          1,              # One track
                          MIDI_TICKS_PER_BEAT)

    # MTrk chunk.
    track_chunk: bytes = b"MTrk"
    track_chunk += struct.pack(">I", len(track_data))
    track_chunk += bytes(track_data)

    with open(output_path, "wb") as f:
        f.write(header + track_chunk)

    # Compute expected duration.
    seconds_per_tick: float = (MIDI_TEMPO_US / 1_000_000) / MIDI_TICKS_PER_BEAT
    duration_s: float = total_ticks * seconds_per_tick

    return {
        "path": str(output_path),
        "format": 0,
        "tracks": 1,
        "ticks_per_beat": MIDI_TICKS_PER_BEAT,
        "tempo_bpm": MIDI_TEMPO_BPM,
        "notes": list(MIDI_NOTES),
        "velocity": MIDI_VELOCITY,
        "note_on_count": len(MIDI_NOTES),
        "note_off_count": len(MIDI_NOTES),
        "total_events": len(MIDI_NOTES) * 2,  # on + off
        "total_ticks": total_ticks,
        "duration_s": round(duration_s, 3),
    }


# ---------------------------------------------------------------------------
# WAV file generation (RIFF PCM format)
# ---------------------------------------------------------------------------

def generate_wav(output_path: Path) -> dict:
    """Generate a 440 Hz sine wave WAV file with known values.

    Returns:
        Dict of fixture metadata for test assertions.
    """
    # Generate samples.
    samples: list[int] = []
    rms_sum: float = 0.0
    for i in range(WAV_NUM_SAMPLES):
        t: float = i / WAV_SAMPLE_RATE
        value: float = WAV_AMPLITUDE * math.sin(2.0 * math.pi * WAV_FREQUENCY * t)
        sample: int = max(-32768, min(32767, int(value)))
        samples.append(sample)
        rms_sum += sample * sample

    rms: float = math.sqrt(rms_sum / WAV_NUM_SAMPLES)

    # Build WAV file.
    data_size: int = WAV_NUM_SAMPLES * WAV_CHANNELS * (WAV_BITS_PER_SAMPLE // 8)
    byte_rate: int = WAV_SAMPLE_RATE * WAV_CHANNELS * (WAV_BITS_PER_SAMPLE // 8)
    block_align: int = WAV_CHANNELS * (WAV_BITS_PER_SAMPLE // 8)

    with open(output_path, "wb") as f:
        # RIFF header.
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))  # File size - 8
        f.write(b"WAVE")

        # fmt subchunk.
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))               # Subchunk size
        f.write(struct.pack("<H", 1))                 # PCM format
        f.write(struct.pack("<H", WAV_CHANNELS))
        f.write(struct.pack("<I", WAV_SAMPLE_RATE))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", WAV_BITS_PER_SAMPLE))

        # data subchunk.
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        for s in samples:
            f.write(struct.pack("<h", s))

    return {
        "path": str(output_path),
        "sample_rate": WAV_SAMPLE_RATE,
        "channels": WAV_CHANNELS,
        "bits_per_sample": WAV_BITS_PER_SAMPLE,
        "num_samples": WAV_NUM_SAMPLES,
        "duration_s": WAV_DURATION_SECONDS,
        "frequency": WAV_FREQUENCY,
        "amplitude": WAV_AMPLITUDE,
        "rms": round(rms, 1),
        "data_size_bytes": data_size,
    }


# ---------------------------------------------------------------------------
# C major chord WAV (for FFT validation)
# ---------------------------------------------------------------------------

# Frequencies for C4, E4, G4 (equal temperament).
CHORD_FREQS: list[float] = [261.63, 329.63, 392.00]
CHORD_AMPLITUDE: int = 10922  # ~1/3 of 32767 so sum doesn't clip


def generate_chord_wav(output_path: Path) -> dict:
    """Generate a C major chord WAV for FFT frequency validation.

    Three simultaneous sine waves at C4, E4, G4. Each at 1/3 amplitude
    so the sum stays within int16 range.

    Returns:
        Dict of fixture metadata for test assertions.
    """
    samples: list[int] = []
    for i in range(WAV_NUM_SAMPLES):
        t: float = i / WAV_SAMPLE_RATE
        value: float = sum(
            CHORD_AMPLITUDE * math.sin(2.0 * math.pi * f * t)
            for f in CHORD_FREQS
        )
        samples.append(max(-32768, min(32767, int(value))))

    data_size: int = WAV_NUM_SAMPLES * WAV_CHANNELS * (WAV_BITS_PER_SAMPLE // 8)
    byte_rate: int = WAV_SAMPLE_RATE * WAV_CHANNELS * (WAV_BITS_PER_SAMPLE // 8)
    block_align: int = WAV_CHANNELS * (WAV_BITS_PER_SAMPLE // 8)

    with open(output_path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))
        f.write(struct.pack("<H", 1))
        f.write(struct.pack("<H", WAV_CHANNELS))
        f.write(struct.pack("<I", WAV_SAMPLE_RATE))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", WAV_BITS_PER_SAMPLE))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        for s in samples:
            f.write(struct.pack("<h", s))

    return {
        "path": str(output_path),
        "frequencies": list(CHORD_FREQS),
        "amplitude_per_voice": CHORD_AMPLITUDE,
        "num_samples": WAV_NUM_SAMPLES,
        "sample_rate": WAV_SAMPLE_RATE,
    }


# ---------------------------------------------------------------------------
# MP3 generation (requires ffmpeg) and corrupt MP3
# ---------------------------------------------------------------------------

def generate_mp3(wav_path: Path, mp3_path: Path) -> dict:
    """Convert a WAV to MP3 via ffmpeg.

    Returns:
        Metadata dict. Includes 'available': False if ffmpeg is missing.
    """
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        return {"path": str(mp3_path), "available": False,
                "reason": "ffmpeg not installed"}

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_path), "-codec:a", "libmp3lame",
             "-b:a", "128k", "-ar", "44100", "-ac", "1", str(mp3_path)],
            capture_output=True, timeout=10,
        )
        exists: bool = mp3_path.exists() and mp3_path.stat().st_size > 0
        return {
            "path": str(mp3_path),
            "available": exists,
            "source_wav": str(wav_path),
        }
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"path": str(mp3_path), "available": False,
                "reason": str(exc)}


def generate_corrupt_mp3(output_path: Path) -> dict:
    """Generate a file with .mp3 extension but random garbage content.

    Returns:
        Metadata dict.
    """
    import random
    rng: random.Random = random.Random(42)  # Deterministic.
    garbage: bytes = bytes(rng.getrandbits(8) for _ in range(4096))
    with open(output_path, "wb") as f:
        f.write(garbage)
    return {"path": str(output_path), "size_bytes": len(garbage)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_all() -> dict:
    """Generate all fixtures and return metadata.

    Returns:
        Dict mapping fixture names to their metadata.
    """
    midi_meta: dict = generate_midi(FIXTURES_DIR / "test_scale.mid")
    wav_meta: dict = generate_wav(FIXTURES_DIR / "test_440hz.wav")
    chord_meta: dict = generate_chord_wav(FIXTURES_DIR / "test_c_major_chord.wav")
    mp3_meta: dict = generate_mp3(
        FIXTURES_DIR / "test_440hz.wav",
        FIXTURES_DIR / "test_440hz.mp3",
    )
    corrupt_meta: dict = generate_corrupt_mp3(FIXTURES_DIR / "test_corrupt.mp3")
    return {
        "midi": midi_meta,
        "wav": wav_meta,
        "chord": chord_meta,
        "mp3": mp3_meta,
        "corrupt_mp3": corrupt_meta,
    }


if __name__ == "__main__":
    import json
    meta: dict = generate_all()
    print(json.dumps(meta, indent=2))
    print(f"\nGenerated {len(meta)} fixtures in {FIXTURES_DIR}")
