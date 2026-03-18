#!/usr/bin/env python3
"""End-to-end integration test: Reolink NVR audio → SignalBus.

Pulls ~3 seconds of live audio from the backyard camera via RTSP,
feeds it through the AudioExtractor pipeline, and verifies that
meaningful signals appear on the SignalBus.

Requirements:
    - NVR reachable (set RTSP_URL env var)
    - ffmpeg installed
    - Run from the project root

Usage:
    python3 test_e2e_audio.py
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import os
import subprocess
import sys
import time

from media import SignalBus, SignalMeta
from media.extractors import AudioExtractor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# RTSP URL for camera audio (channel 02 = sub stream).
# Set via environment variable to avoid leaking credentials.
RTSP_URL: str = os.environ.get(
    "RTSP_URL",
    "rtsp://user:pass@camera:554/Preview_02_main",
)

# Sample rate matching the Reolink AAC audio stream.
SAMPLE_RATE: int = 16000

# Duration to capture (seconds).
CAPTURE_DURATION: float = 3.0

# Chunk size for reading from ffmpeg pipe (bytes).
# 1024 samples × 2 bytes/sample = 2048 bytes.
CHUNK_SIZE: int = 2048

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the end-to-end audio pipeline test."""
    print("=" * 60)
    print("End-to-End Audio Pipeline Test")
    print("=" * 60)

    # --- Set up signal bus and extractor ---
    bus: SignalBus = SignalBus()
    ext: AudioExtractor = AudioExtractor(
        source_name="backyard",
        sample_rate=SAMPLE_RATE,
        bus=bus,
        window_size=1024,
        band_count=8,
        smoothing=0.2,
    )

    print(f"\nRegistered signals: {ext.get_signal_names()}")

    # --- Start ffmpeg to extract raw PCM from RTSP ---
    cmd: list[str] = [
        "ffmpeg",
        "-loglevel", "warning",
        "-rtsp_transport", "tcp",
        "-i", RTSP_URL,
        "-vn",                  # no video
        "-acodec", "pcm_s16le", # 16-bit signed PCM
        "-ar", str(SAMPLE_RATE),
        "-ac", "1",             # mono
        "-f", "s16le",          # raw PCM output
        "-t", str(CAPTURE_DURATION),
        "pipe:1",
    ]

    print(f"\nStarting ffmpeg (capturing {CAPTURE_DURATION}s of audio)...")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        print("ERROR: ffmpeg not found")
        sys.exit(1)

    # --- Feed chunks to extractor ---
    total_bytes: int = 0
    windows_processed: int = 0
    start_time: float = time.monotonic()

    while True:
        chunk: bytes = proc.stdout.read(CHUNK_SIZE)
        if not chunk:
            break
        total_bytes += len(chunk)
        ext.process(chunk)
        windows_processed += 1

    elapsed: float = time.monotonic() - start_time
    proc.wait()

    stderr_output: str = proc.stderr.read().decode("utf-8", errors="replace")
    if proc.returncode != 0:
        print(f"\nffmpeg exited with code {proc.returncode}")
        if stderr_output:
            print(f"stderr: {stderr_output[:500]}")
        sys.exit(1)

    total_samples: int = total_bytes // 2
    print(f"\nCapture complete:")
    print(f"  Bytes received:    {total_bytes:,}")
    print(f"  Samples:           {total_samples:,}")
    print(f"  Duration:          {total_samples / SAMPLE_RATE:.2f}s")
    print(f"  Chunks processed:  {windows_processed}")
    print(f"  Wall time:         {elapsed:.2f}s")

    # --- Read signals from bus ---
    print(f"\n{'Signal':<30} {'Value':<15} {'Status'}")
    print("-" * 60)

    all_ok: bool = True
    signal_names: list[str] = ext.get_signal_names()

    for name in signal_names:
        value = bus.read(name)
        if isinstance(value, list):
            display: str = f"[{len(value)} bands]"
            non_zero: int = sum(1 for v in value if v > 0.001)
            status: str = f"OK ({non_zero}/{len(value)} active)" if non_zero > 0 else "WARN: all zero"
        else:
            display = f"{value:.6f}"
            # For beat, 0 is fine (might not have beat in ambient audio).
            if "beat" in name:
                status = "OK (beat detection active)"
            elif value > 0.0001:
                status = "OK"
            else:
                status = "WARN: near zero"

        print(f"  {name:<28} {display:<15} {status}")

    # --- Verify critical signals ---
    print(f"\n{'Verification'}")
    print("-" * 60)

    bands = bus.read("backyard:audio:bands")
    if isinstance(bands, list) and len(bands) == 8:
        print(f"  Bands length:  PASS (8 bands)")
    else:
        print(f"  Bands length:  FAIL (got {type(bands)})")
        all_ok = False

    rms = bus.read("backyard:audio:rms")
    if isinstance(rms, float) and rms >= 0.0:
        print(f"  RMS range:     PASS ({rms:.6f})")
    else:
        print(f"  RMS range:     FAIL ({rms})")
        all_ok = False

    centroid = bus.read("backyard:audio:centroid")
    if isinstance(centroid, float) and 0.0 <= centroid <= 1.0:
        print(f"  Centroid range: PASS ({centroid:.6f})")
    else:
        print(f"  Centroid range: FAIL ({centroid})")
        all_ok = False

    energy = bus.read("backyard:audio:energy")
    if isinstance(energy, float) and energy >= 0.0:
        print(f"  Energy range:  PASS ({energy:.6f})")
    else:
        print(f"  Energy range:  FAIL ({energy})")
        all_ok = False

    # Band values should be normalized [0, 1].
    if isinstance(bands, list):
        all_in_range: bool = all(0.0 <= b <= 1.0 for b in bands)
        if all_in_range:
            print(f"  Band normalize: PASS (all in [0, 1])")
        else:
            print(f"  Band normalize: FAIL (out of range)")
            all_ok = False

    print()
    if all_ok:
        print("RESULT: ALL CHECKS PASSED")
    else:
        print("RESULT: SOME CHECKS FAILED")
    print("=" * 60)

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
