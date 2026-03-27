#!/usr/bin/env python3
"""Cylon (Larson scanner) effect using acked UDP — one frame at a time.

Sends each frame via SetExtendedColorZones with ack_required set,
waits for the Acknowledgement (message type 45) before sending the
next frame.  This tests whether ack-paced UDP eliminates the flicker
and stutter seen on Neon devices with fire-and-forget sends.

Usage:
    python3 test_cylon_ack.py [IP]
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
import socket
import struct
import sys
import time
from typing import Optional

from transport import (
    LifxDevice,
    MSG_SET_EXTENDED_COLOR_ZONES,
    HSBK_FMT,
    HSBK_SIZE,
    ZONES_PER_PACKET,
    APPLY_YES,
    HEADER_SIZE,
    _build_header,
    _parse_message,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Target device.
DEFAULT_IP: str = "192.0.2.10"

# Cylon eye color — saturated red at full brightness.
HUE_RED: int = 0
SAT_FULL: int = 65535
BRI_FULL: int = 65535
KELVIN: int = 3500

# Background — off.
BRI_OFF: int = 0

# Eye width — how many zones the bright core spans.
EYE_CORE: int = 1
# Glow falloff zones on each side of the core.
EYE_GLOW: int = 3
# Brightness falloff exponent (higher = tighter glow).
GLOW_GAMMA: float = 2.5

# Animation timing.
SWEEP_PERIOD: float = 2.0     # seconds for a full left-right-left cycle
TARGET_FPS: int = 30          # attempt this rate — ack pacing is the real governor

# Ack protocol.
MSG_ACKNOWLEDGEMENT: int = 45
ACK_TIMEOUT: float = 0.2      # seconds to wait for ack before retry
ACK_RETRIES: int = 3          # max retries per frame


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_cylon(t: float, zone_count: int) -> list[tuple[int, int, int, int]]:
    """Render one frame of the Cylon scanner.

    A bright red eye sweeps back and forth with an exponential glow
    falloff on either side.

    Args:
        t:          Seconds elapsed since start.
        zone_count: Number of zones on the device.

    Returns:
        List of (hue, sat, brightness, kelvin) tuples, one per zone.
    """
    # Triangle wave: 0 → 1 → 0 over SWEEP_PERIOD seconds.
    phase: float = (t % SWEEP_PERIOD) / SWEEP_PERIOD
    pos: float = 1.0 - abs(2.0 * phase - 1.0)  # 0→1→0

    # Map to zone index (float).
    center: float = pos * (zone_count - 1)

    colors: list[tuple[int, int, int, int]] = []
    for i in range(zone_count):
        dist: float = abs(i - center)
        if dist <= EYE_CORE * 0.5:
            # Core — full brightness.
            bri = BRI_FULL
        elif dist <= EYE_CORE * 0.5 + EYE_GLOW:
            # Glow falloff.
            glow_dist: float = dist - EYE_CORE * 0.5
            frac: float = 1.0 - (glow_dist / EYE_GLOW)
            bri = int(BRI_FULL * (frac ** GLOW_GAMMA))
        else:
            bri = BRI_OFF
        colors.append((HUE_RED, SAT_FULL, bri, KELVIN))

    return colors


# ---------------------------------------------------------------------------
# Acked send
# ---------------------------------------------------------------------------

def send_zones_acked(
    dev: LifxDevice,
    colors: list[tuple[int, int, int, int]],
) -> tuple[bool, float]:
    """Send a full zone frame with ack, waiting for confirmation.

    Args:
        dev:    The target LIFX device.
        colors: HSBK tuples, one per zone.

    Returns:
        (acked, rtt) — whether the ack arrived, and round-trip time in ms.
    """
    # Build the SetExtendedColorZones payload.
    total: int = len(colors)
    payload: bytes = struct.pack(
        "<IBH B", 0, APPLY_YES, 0, total,
    )
    for h, s, b, k in colors:
        payload += struct.pack(HSBK_FMT, h, s, b, k)
    # Pad to ZONES_PER_PACKET.
    for _ in range(ZONES_PER_PACKET - total):
        payload += b'\x00' * HSBK_SIZE

    for attempt in range(ACK_RETRIES):
        t_send: float = time.monotonic()
        dev._send(MSG_SET_EXTENDED_COLOR_ZONES, payload, ack=True)

        # Wait for ack.
        deadline: float = time.monotonic() + ACK_TIMEOUT
        while time.monotonic() < deadline:
            try:
                data, _ = dev.sock.recvfrom(256)
                msg = _parse_message(data)
                if msg and msg["type"] == MSG_ACKNOWLEDGEMENT:
                    rtt: float = (time.monotonic() - t_send) * 1000.0
                    return True, rtt
            except socket.timeout:
                break
            except OSError:
                break

    return False, 0.0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the Cylon scanner with ack-paced frames."""
    ip: str = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IP

    print(f"Connecting to {ip}...")
    dev = LifxDevice(ip)
    dev.query_zone_count()
    zone_count: int = dev.zone_count or 24
    print(f"Zone count: {zone_count}")
    print(f"Target FPS: {TARGET_FPS}  |  Ack timeout: {ACK_TIMEOUT*1000:.0f}ms")
    print(f"Sweep period: {SWEEP_PERIOD}s")
    print("Press Ctrl-C to stop.\n")

    # Set a short socket timeout so recvfrom doesn't block forever.
    dev.sock.settimeout(ACK_TIMEOUT)

    frame_interval: float = 1.0 / TARGET_FPS
    start: float = time.monotonic()
    frames: int = 0
    acked: int = 0
    dropped: int = 0
    rtt_sum: float = 0.0
    rtt_min: float = float('inf')
    rtt_max: float = 0.0

    try:
        while True:
            t: float = time.monotonic() - start
            colors = render_cylon(t, zone_count)
            ok, rtt = send_zones_acked(dev, colors)
            frames += 1

            if ok:
                acked += 1
                rtt_sum += rtt
                rtt_min = min(rtt_min, rtt)
                rtt_max = max(rtt_max, rtt)
            else:
                dropped += 1

            # Stats every 50 frames.
            if frames % 50 == 0:
                avg_rtt: float = rtt_sum / acked if acked else 0.0
                actual_fps: float = frames / t if t > 0 else 0.0
                print(
                    f"frame {frames:>5}  |  "
                    f"fps {actual_fps:5.1f}  |  "
                    f"acked {acked}/{frames}  |  "
                    f"rtt avg {avg_rtt:5.1f}ms  "
                    f"min {rtt_min:5.1f}ms  "
                    f"max {rtt_max:5.1f}ms  |  "
                    f"dropped {dropped}"
                )

            # Pace to target FPS (ack wait already ate some time).
            elapsed: float = time.monotonic() - start - t
            # Hmm, that's wrong. Let's just pace from frame start.
            frame_end: float = time.monotonic()
            sleep_time: float = frame_interval - (frame_end - (start + t))
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        t_total: float = time.monotonic() - start
        avg_rtt = rtt_sum / acked if acked else 0.0
        print(f"\n\nStopped after {t_total:.1f}s, {frames} frames.")
        print(f"  Acked:   {acked}/{frames} ({100*acked/frames:.1f}%)")
        print(f"  Dropped: {dropped}")
        if acked:
            print(f"  RTT avg: {avg_rtt:.1f}ms  min: {rtt_min:.1f}ms  max: {rtt_max:.1f}ms")
        print(f"  Avg FPS: {frames/t_total:.1f}")

    finally:
        # Turn off the device.
        off: list[tuple[int, int, int, int]] = [(0, 0, 0, KELVIN)] * zone_count
        dev._send(MSG_SET_EXTENDED_COLOR_ZONES, struct.pack(
            "<IBH B", 0, APPLY_YES, 0, zone_count,
        ) + b''.join(struct.pack(HSBK_FMT, *c) for c in off)
          + b'\x00' * HSBK_SIZE * (ZONES_PER_PACKET - zone_count),
          ack=True)
        dev.close()
        print("Device off. Done.")


if __name__ == "__main__":
    main()
