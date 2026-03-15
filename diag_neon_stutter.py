#!/usr/bin/env python3
"""Neon stutter diagnostic — tests different sending strategies.

Runs a simple cylon-style eye sweep using various combinations of
protocol type, FPS, transition time, and pacing to find which
strategy the Neon handles smoothly.

Usage::

    python3 diag_neon_stutter.py <ip>
    python3 diag_neon_stutter.py 10.0.0.34

Each test runs for a fixed duration.  Watch the strip and rate
smoothness when prompted.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
import struct
import sys
import time
from typing import Optional

from transport import (
    LifxDevice,
    HSBK_FMT,
    HSBK_SIZE,
    ZONES_PER_PACKET,
    APPLY_NO,
    APPLY_YES,
    MSG_SET_COLOR_ZONES,
    MSG_SET_EXTENDED_COLOR_ZONES,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HSBK_MAX: int = 65535
KELVIN_DEFAULT: int = 3500
TEST_DURATION: float = 10.0
"""Seconds each test runs."""

EYE_WIDTH: float = 3.0
"""Half-width of the cylon eye in zones."""

SPEED: float = 4.0
"""Seconds for one full sweep cycle."""

# Colors
RED: tuple[int, int, int, int] = (0, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT)
BLACK: tuple[int, int, int, int] = (0, 0, 0, KELVIN_DEFAULT)
DIM_RED: tuple[int, int, int, int] = (0, HSBK_MAX, int(HSBK_MAX * 0.05), KELVIN_DEFAULT)

# Test configurations: (label, fps, transition_ms, use_501, pacing_ms)
TESTS: list[tuple[str, int, int, bool, float]] = [
    # Round 3: chasing the 5 — higher FPS with long transitions
    ("510,  5fps, trans=1000ms",                5, 1000, False, 0.0),
    ("510,  5fps, trans=1500ms",                5, 1500, False, 0.0),
    ("510,  5fps, trans=2000ms",                5, 2000, False, 0.0),
    ("510,  8fps, trans=1000ms",                8, 1000, False, 0.0),
    ("510,  8fps, trans=1500ms",                8, 1500, False, 0.0),
    ("510,  8fps, trans=2000ms",                8, 2000, False, 0.0),
    ("510, 10fps, trans=1000ms",               10, 1000, False, 0.0),
    ("510, 10fps, trans=1500ms",               10, 1500, False, 0.0),
    ("510, 10fps, trans=2000ms",               10, 2000, False, 0.0),
    ("510, 15fps, trans=2000ms",               15, 2000, False, 0.0),
    ("510, 20fps, trans=2000ms",               20, 2000, False, 0.0),
]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_cylon(t: float, zone_count: int) -> list[tuple[int, int, int, int]]:
    """Produce one cylon frame — cosine-eased eye sweep.

    Args:
        t:          Elapsed seconds.
        zone_count: Number of zones on the device.

    Returns:
        List of HSBK tuples, one per zone.
    """
    travel: float = zone_count - 1.0
    phase: float = (t % SPEED) / SPEED
    position: float = travel * (1.0 - math.cos(phase * 2.0 * math.pi)) / 2.0

    colors: list[tuple[int, int, int, int]] = []
    for i in range(zone_count):
        dist: float = abs(i - position)
        if dist < EYE_WIDTH:
            t_norm: float = dist / EYE_WIDTH
            bri: int = int(HSBK_MAX * (math.cos(t_norm * math.pi) + 1.0) / 2.0)
            colors.append((0, HSBK_MAX, max(bri, DIM_RED[2]), KELVIN_DEFAULT))
        else:
            colors.append(DIM_RED)
    return colors


# ---------------------------------------------------------------------------
# Sending strategies
# ---------------------------------------------------------------------------

def send_extended(dev: LifxDevice, colors: list[tuple[int, int, int, int]],
                  duration_ms: int, pacing_ms: float) -> None:
    """Send zones using extended multizone protocol (type 510).

    Args:
        dev:         Target device.
        colors:      HSBK list, one per zone.
        duration_ms: Firmware transition time.
        pacing_ms:   Delay between chunk packets (0 = no pacing).
    """
    total: int = len(colors)
    num_packets: int = math.ceil(total / ZONES_PER_PACKET)

    for i in range(num_packets):
        start: int = i * ZONES_PER_PACKET
        chunk: list[tuple[int, int, int, int]] = colors[start:start + ZONES_PER_PACKET]
        is_last: bool = (i == num_packets - 1)
        apply_flag: int = APPLY_YES if is_last else APPLY_NO

        payload: bytes = struct.pack(
            "<IBH B", duration_ms, apply_flag, start, len(chunk),
        )
        for h, s, b, k in chunk:
            payload += struct.pack(HSBK_FMT, h, s, b, k)
        for _ in range(ZONES_PER_PACKET - len(chunk)):
            payload += b'\x00' * HSBK_SIZE

        dev.fire_and_forget(MSG_SET_EXTENDED_COLOR_ZONES, payload)

        if pacing_ms > 0 and not is_last:
            time.sleep(pacing_ms / 1000.0)


def send_per_zone(dev: LifxDevice, colors: list[tuple[int, int, int, int]],
                  duration_ms: int) -> None:
    """Send zones using legacy per-zone protocol (type 501).

    SetColorZones (501) payload:
        start_index: u8
        end_index:   u8
        hue:         u16
        saturation:  u16
        brightness:  u16
        kelvin:      u16
        duration:    u32
        apply:       u8

    Args:
        dev:         Target device.
        colors:      HSBK list, one per zone.
        duration_ms: Firmware transition time.
    """
    total: int = len(colors)
    for i, (h, s, b, k) in enumerate(colors):
        apply_flag: int = APPLY_YES if i == total - 1 else APPLY_NO
        payload: bytes = struct.pack(
            "<BB HHHH IB",
            i, i,           # start_index, end_index (same zone)
            h, s, b, k,
            duration_ms,
            apply_flag,
        )
        dev.fire_and_forget(MSG_SET_COLOR_ZONES, payload)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_test(dev: LifxDevice, zone_count: int, label: str,
             fps: int, transition_ms: int, use_501: bool,
             pacing_ms: float) -> Optional[str]:
    """Run a single stutter test and prompt for a rating.

    Args:
        dev:           Target device.
        zone_count:    Number of zones.
        label:         Human-readable test name.
        fps:           Frames per second.
        transition_ms: Firmware transition time in ms.
        use_501:       Use legacy per-zone protocol if True.
        pacing_ms:     Inter-chunk delay in ms (510 only).

    Returns:
        User's rating string, or None to quit.
    """
    interval: float = 1.0 / fps
    print(f"\n{'=' * 60}")
    print(f"  TEST: {label}")
    print(f"  fps={fps}  transition={transition_ms}ms  "
          f"proto={'501' if use_501 else '510'}  pacing={pacing_ms}ms")
    print(f"{'=' * 60}")
    input("  Press Enter to start...")

    start: float = time.monotonic()
    frame_num: int = 0

    while True:
        t: float = time.monotonic() - start
        if t >= TEST_DURATION:
            break

        colors: list[tuple[int, int, int, int]] = render_cylon(t, zone_count)

        if use_501:
            send_per_zone(dev, colors, transition_ms)
        else:
            send_extended(dev, colors, transition_ms, pacing_ms)

        frame_num += 1
        # Maintain frame timing
        next_time: float = start + frame_num * interval
        now: float = time.monotonic()
        if next_time > now:
            time.sleep(next_time - now)

    sys.stdout.write(f"\r  {frame_num} frames sent in {TEST_DURATION:.0f}s "
                     f"({frame_num / TEST_DURATION:.1f} actual fps)\n")

    # Blank strip between tests
    dev.set_zones([BLACK] * zone_count, duration_ms=0, rapid=True)
    time.sleep(0.3)

    rating: str = input("  Rate smoothness (1=terrible 5=perfect, q=quit): ").strip()
    if rating.lower() == 'q':
        return None
    return rating


def main() -> None:
    """Run all stutter tests against the specified device."""
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <ip>")
        sys.exit(1)

    ip: str = sys.argv[1]

    print(f"Querying {ip}...", flush=True)
    dev: LifxDevice = LifxDevice(ip)
    dev.query_all()

    if dev.zone_count is None:
        print("Failed to query device.")
        sys.exit(1)

    zone_count: int = dev.zone_count
    print(f"Device: {dev.label} ({dev.product_name}), {zone_count} zones")
    print(f"\nRunning {len(TESTS)} tests, {TEST_DURATION:.0f}s each.")
    print("Watch the strip and rate each test.\n")

    dev.set_power(on=True, duration_ms=0)
    dev.clear_firmware_effect()
    time.sleep(0.2)

    results: list[tuple[str, str]] = []

    for label, fps, trans, use_501, pacing in TESTS:
        rating: Optional[str] = run_test(
            dev, zone_count, label, fps, trans, use_501, pacing,
        )
        if rating is None:
            break
        results.append((label, rating))

    # Summary
    print(f"\n{'=' * 60}")
    print("  RESULTS")
    print(f"{'=' * 60}")
    for label, rating in results:
        print(f"  [{rating}] {label}")
    print()

    dev.set_zones([BLACK] * zone_count, duration_ms=0, rapid=True)
    dev.close()


if __name__ == "__main__":
    main()
