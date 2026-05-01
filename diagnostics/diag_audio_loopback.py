#!/usr/bin/env python3
"""Acoustic loopback diagnostic — speaker → room → mic.

Plays a synthesized sine tone out the configured ALSA playback device
and concurrently records via the configured ALSA capture device, then
FFTs the recording to confirm the tone arrived above the noise floor.

This bypasses the satellite stack entirely; only ALSA + arecord +
aplay are exercised.  Use it to isolate "is the physical path live?"
from "is the voice daemon healthy?".

Usage::

    python3 diag_audio_loopback.py
    python3 diag_audio_loopback.py --freq 2000 --duration 1.5
    python3 diag_audio_loopback.py --play-device plughw:3,0 \
                                   --record-device plughw:2,0

Run on the host with the audio hardware (broker-2 for the Living
Room satellite).  Exit 0 on pass, 1 on fail.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import math
import os
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Defaults match broker-2's hardware (Pi 5, Living Room satellite):
#   capture: Fifine USB PnP mic, downstream of the powered hub
#   playback: MZ-631 USB Speaker Bar (added 2026-04-30)
DEFAULT_PLAY_DEVICE: str = "plughw:CARD=Bar,DEV=0"
DEFAULT_RECORD_DEVICE: str = "plughw:CARD=Device,DEV=0"

# 1 kHz is well inside both the speaker passband and the typical
# voice-mic passband, comfortably above 50/60 Hz mains hum, and far
# from common wake-word energy bands.
DEFAULT_FREQ_HZ: float = 1000.0
DEFAULT_DURATION_SEC: float = 1.0

# 16 kHz mono S16_LE matches the satellite's capture format, so we
# exercise the same ALSA configuration the daemon uses.
SAMPLE_RATE_HZ: int = 16000
SAMPLE_WIDTH_BYTES: int = 2  # S16_LE
CHANNELS: int = 1

# Recording window pads the play duration on both sides so a small
# arecord/aplay launch skew does not clip the tone.
PRE_ROLL_SEC: float = 0.20
POST_ROLL_SEC: float = 0.30

# Lead time between starting arecord and starting aplay — lets
# arecord's ALSA buffer prime so the tone lands inside the capture
# window even on slow USB enumeration.
APLAY_LAUNCH_DELAY_SEC: float = 0.15

# Sine amplitude as a fraction of full scale (S16 max = 32767).
# 0.5 gives a clearly audible but non-clipping tone at typical
# speaker gain.
SINE_AMPLITUDE_SCALE: float = 0.5
S16_MAX: int = 32767

# Tolerance window around the requested frequency — the FFT bin
# resolution at 16 kHz / 1 s recording is ~1 Hz, so 50 Hz is loose
# enough to absorb device clock skew but tight enough to reject
# unrelated room noise.
FREQ_TOLERANCE_HZ: float = 50.0

# Pass criterion: peak FFT bin energy must exceed the median of all
# bins by at least this dB margin.  Median (not mean) is used because
# a strong tone shifts the mean upward and would mask a weak
# signal-to-noise ratio.  10 dB is the textbook "clearly above the
# noise floor" margin.
MIN_PEAK_OVER_MEDIAN_DB: float = 10.0

# Absolute floor for recording RMS — below this the mic is almost
# certainly dead or muted, regardless of FFT shape.  In S16 normalized
# units (range [-1, 1]).  -50 dBFS ≈ 3 / 32767, well below normal
# room ambient but above true silence.
MIN_RECORDING_RMS_DBFS: float = -50.0

EPSILON: float = 1.0e-12  # log/divide guard


# ---------------------------------------------------------------------------
# Tone generation
# ---------------------------------------------------------------------------

def write_sine_wav(path: str, freq_hz: float, duration_sec: float) -> None:
    """Write a mono S16_LE sine WAV at SAMPLE_RATE_HZ.

    Args:
        path: output path.
        freq_hz: tone frequency.
        duration_sec: tone duration.
    """
    n_samples: int = int(duration_sec * SAMPLE_RATE_HZ)
    t: np.ndarray = np.arange(n_samples, dtype=np.float64) / SAMPLE_RATE_HZ
    # Float sine in [-1, 1], scaled to S16 with headroom.
    samples: np.ndarray = (
        SINE_AMPLITUDE_SCALE * np.sin(2.0 * np.pi * freq_hz * t)
    )
    pcm: np.ndarray = (samples * S16_MAX).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(SAMPLE_RATE_HZ)
        wf.writeframes(pcm.tobytes())


# ---------------------------------------------------------------------------
# Capture + playback
# ---------------------------------------------------------------------------

def run_loopback(
    tone_wav: str,
    record_wav: str,
    play_device: str,
    record_device: str,
    duration_sec: float,
) -> None:
    """Start arecord, then start aplay, wait for both.

    arecord runs for the full padded window; aplay runs for exactly
    ``duration_sec``.  Skew between them is absorbed by PRE_ROLL_SEC
    / POST_ROLL_SEC.

    Args:
        tone_wav: path to the sine WAV to play.
        record_wav: path to write the captured WAV.
        play_device: ALSA device name for aplay -D.
        record_device: ALSA device name for arecord -D.
        duration_sec: tone duration.

    Raises:
        RuntimeError: if either subprocess returns non-zero.
    """
    # arecord on some builds (notably Debian's alsa-utils on the Pi)
    # rejects fractional values to -d, so round up to whole seconds.
    # Extra padding past POST_ROLL_SEC is harmless: the FFT windows
    # the whole recording and the tone is still the dominant bin.
    record_duration_sec: int = max(
        1, math.ceil(PRE_ROLL_SEC + duration_sec + POST_ROLL_SEC)
    )

    # arecord exits on its own after -d seconds; aplay exits when the
    # WAV is consumed.  Capture stderr so error messages survive.
    arecord_cmd: list[str] = [
        "arecord",
        "-q",
        "-D", record_device,
        "-f", "S16_LE",
        "-r", str(SAMPLE_RATE_HZ),
        "-c", str(CHANNELS),
        "-d", str(record_duration_sec),
        record_wav,
    ]
    aplay_cmd: list[str] = [
        "aplay",
        "-q",
        "-D", play_device,
        tone_wav,
    ]

    print(f"[diag] arecord: {' '.join(arecord_cmd)}", flush=True)
    rec: subprocess.Popen = subprocess.Popen(
        arecord_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Let arecord's ALSA buffer prime before we send audio.  Without
    # this, fast hardware can drop the leading edge of the tone
    # because arecord has not yet opened the capture stream.
    time.sleep(APLAY_LAUNCH_DELAY_SEC)

    print(f"[diag] aplay:   {' '.join(aplay_cmd)}", flush=True)
    play: subprocess.Popen = subprocess.Popen(
        aplay_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    play_rc: int = play.wait()
    rec_rc: int = rec.wait()

    if play_rc != 0:
        stderr: bytes = play.stderr.read() if play.stderr else b""
        raise RuntimeError(
            f"aplay exited {play_rc}: {stderr.decode(errors='replace')}"
        )
    if rec_rc != 0:
        stderr = rec.stderr.read() if rec.stderr else b""
        raise RuntimeError(
            f"arecord exited {rec_rc}: {stderr.decode(errors='replace')}"
        )


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_recording(
    record_wav: str,
    expected_freq_hz: float,
) -> tuple[bool, float, float, float]:
    """FFT the recording and report whether the tone is present.

    Returns (passed, peak_freq_hz, peak_over_median_db, rms_dbfs).
    """
    with wave.open(record_wav, "rb") as wf:
        if wf.getsampwidth() != SAMPLE_WIDTH_BYTES:
            raise RuntimeError(
                f"unexpected sample width: {wf.getsampwidth()} bytes"
            )
        if wf.getnchannels() != CHANNELS:
            raise RuntimeError(
                f"unexpected channels: {wf.getnchannels()}"
            )
        n_frames: int = wf.getnframes()
        rate: int = wf.getframerate()
        raw: bytes = wf.readframes(n_frames)

    pcm: np.ndarray = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    pcm_normalized: np.ndarray = pcm / float(S16_MAX)

    # RMS in dBFS — sanity gate for "is anything coming through the
    # mic at all?".  Independent of FFT shape.
    rms: float = float(np.sqrt(np.mean(pcm_normalized ** 2) + EPSILON))
    rms_dbfs: float = 20.0 * np.log10(rms + EPSILON)

    # Hann window suppresses FFT sidelobes so the tone bin reads
    # cleanly even when the recording length is not an exact integer
    # number of cycles.
    window: np.ndarray = np.hanning(len(pcm_normalized))
    spectrum: np.ndarray = np.abs(
        np.fft.rfft(pcm_normalized * window)
    )
    freqs: np.ndarray = np.fft.rfftfreq(len(pcm_normalized), d=1.0 / rate)

    peak_idx: int = int(np.argmax(spectrum))
    peak_freq_hz: float = float(freqs[peak_idx])
    peak_amp: float = float(spectrum[peak_idx])
    median_amp: float = float(np.median(spectrum) + EPSILON)
    peak_over_median_db: float = 20.0 * np.log10(peak_amp / median_amp)

    freq_ok: bool = (
        abs(peak_freq_hz - expected_freq_hz) <= FREQ_TOLERANCE_HZ
    )
    snr_ok: bool = peak_over_median_db >= MIN_PEAK_OVER_MEDIAN_DB
    rms_ok: bool = rms_dbfs >= MIN_RECORDING_RMS_DBFS

    passed: bool = freq_ok and snr_ok and rms_ok
    return passed, peak_freq_hz, peak_over_median_db, rms_dbfs


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    """Parse args, run the loopback, print the verdict."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Acoustic loopback test: speaker → mic.",
    )
    parser.add_argument(
        "--play-device", default=DEFAULT_PLAY_DEVICE,
        help=f"ALSA playback device (default: {DEFAULT_PLAY_DEVICE})",
    )
    parser.add_argument(
        "--record-device", default=DEFAULT_RECORD_DEVICE,
        help=f"ALSA capture device (default: {DEFAULT_RECORD_DEVICE})",
    )
    parser.add_argument(
        "--freq", type=float, default=DEFAULT_FREQ_HZ,
        help=f"Tone frequency Hz (default: {DEFAULT_FREQ_HZ})",
    )
    parser.add_argument(
        "--duration", type=float, default=DEFAULT_DURATION_SEC,
        help=f"Tone duration sec (default: {DEFAULT_DURATION_SEC})",
    )
    parser.add_argument(
        "--keep-files", action="store_true",
        help="Do not delete the temp tone/recording WAVs.",
    )
    args: argparse.Namespace = parser.parse_args()

    workdir: str = tempfile.mkdtemp(prefix="diag_audio_loop_")
    tone_wav: str = os.path.join(workdir, "tone.wav")
    record_wav: str = os.path.join(workdir, "record.wav")

    print(
        f"[diag] tone={args.freq:.1f} Hz dur={args.duration:.2f}s "
        f"rate={SAMPLE_RATE_HZ} workdir={workdir}",
        flush=True,
    )

    write_sine_wav(tone_wav, args.freq, args.duration)

    try:
        run_loopback(
            tone_wav=tone_wav,
            record_wav=record_wav,
            play_device=args.play_device,
            record_device=args.record_device,
            duration_sec=args.duration,
        )
    except RuntimeError as exc:
        print(f"[diag] FAIL — playback/capture error: {exc}", flush=True)
        return 1

    try:
        passed, peak_hz, snr_db, rms_dbfs = analyze_recording(
            record_wav, args.freq,
        )
    except RuntimeError as exc:
        print(f"[diag] FAIL — analysis error: {exc}", flush=True)
        return 1

    verdict: str = "PASS" if passed else "FAIL"
    print(
        f"[diag] {verdict}  peak={peak_hz:7.2f} Hz  "
        f"snr={snr_db:5.1f} dB  rms={rms_dbfs:6.1f} dBFS  "
        f"(target={args.freq:.1f}±{FREQ_TOLERANCE_HZ:.0f} Hz, "
        f"snr≥{MIN_PEAK_OVER_MEDIAN_DB:.0f} dB, "
        f"rms≥{MIN_RECORDING_RMS_DBFS:.0f} dBFS)",
        flush=True,
    )

    if not args.keep_files:
        # Best-effort cleanup; don't fail the verdict on leftover files.
        for p in (tone_wav, record_wav):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
