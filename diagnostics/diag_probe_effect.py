#!/usr/bin/env python3
"""Probe a LIFX multizone device for its current firmware effect state.

Polls the device repeatedly, printing the active firmware effect type
and zone colors.  Run this while triggering effects from the LIFX app
to see whether the app uses firmware effects (type 508) or streams
zone data (type 510).

Usage::

    python3 diag_probe_effect.py <ip> [poll_interval]

Examples::

    python3 diag_probe_effect.py 192.0.2.34
    python3 diag_probe_effect.py 192.0.2.34 0.5
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import struct
import sys
import time

from transport import (
    LifxDevice, _build_header, _parse_message,
    LIFX_PORT, MAX_UDP_PAYLOAD, HSBK_FMT, HSBK_SIZE,
    MSG_GET_EXTENDED_COLOR_ZONES, MSG_STATE_EXTENDED_COLOR_ZONES,
    HSBK_MAX,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# GetMultiZoneEffect (507) asks the device what firmware effect is running.
MSG_GET_MULTIZONE_EFFECT: int = 507
# StateMultiZoneEffect (509) is the response.
MSG_STATE_MULTIZONE_EFFECT: int = 509

DEFAULT_POLL_INTERVAL: float = 1.0

EFFECT_NAMES: dict[int, str] = {
    0: "OFF",
    1: "MOVE",
    2: "RESERVED_2",
    3: "RESERVED_3",
}


def query_firmware_effect(dev: LifxDevice) -> None:
    """Query and print the current firmware effect state."""
    payload = dev._send_and_recv(
        MSG_GET_MULTIZONE_EFFECT,
        MSG_STATE_MULTIZONE_EFFECT,
        timeout=1.0,
    )
    if payload is None:
        print("  firmware effect: no response")
        return

    if len(payload) >= 59:
        # StateMultiZoneEffect layout:
        #   instance_id: u32 (0)
        #   type:        u8  (4)
        #   reserved:    u16 (5)
        #   speed:       u32 (7)
        #   duration:    u64 (11)
        #   reserved:    u32 (19)
        #   reserved:    u32 (23)
        #   parameters:  32 bytes (27)
        instance_id: int = struct.unpack_from("<I", payload, 0)[0]
        effect_type: int = struct.unpack_from("<B", payload, 4)[0]
        speed: int = struct.unpack_from("<I", payload, 7)[0]
        duration: int = struct.unpack_from("<Q", payload, 11)[0]

        name: str = EFFECT_NAMES.get(effect_type, f"UNKNOWN({effect_type})")
        print(f"  firmware effect: {name} (type={effect_type}, "
              f"speed={speed}ms, duration={duration}ns, "
              f"instance={instance_id})")
    else:
        print(f"  firmware effect: short payload ({len(payload)} bytes)")


def query_zone_snapshot(dev: LifxDevice, zone_count: int) -> None:
    """Query and print a compact snapshot of current zone colors."""
    colors = dev.query_zone_colors()
    if colors is None:
        print("  zones: no response")
        return

    # Print a compact summary — first few zones' hue values.
    hues: list[str] = []
    for i, (h, s, b, k) in enumerate(colors[:8]):
        deg: float = h * 360.0 / HSBK_MAX
        bri_pct: int = b * 100 // HSBK_MAX
        hues.append(f"{i}:{deg:.0f}°/{bri_pct}%")
    suffix: str = f" ...+{len(colors)-8} more" if len(colors) > 8 else ""
    print(f"  zones: {', '.join(hues)}{suffix}")


def main() -> None:
    """Poll the device and print firmware effect + zone state."""
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <ip> [poll_interval]")
        sys.exit(1)

    ip: str = sys.argv[1]
    interval: float = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_POLL_INTERVAL

    print(f"Querying {ip}...", flush=True)
    dev: LifxDevice = LifxDevice(ip)
    dev.query_all()

    if dev.zone_count is None:
        print("Failed to query device.")
        sys.exit(1)

    print(f"Device: {dev.label} ({dev.product_name}), {dev.zone_count} zones")
    print(f"Polling every {interval}s — trigger effects from the LIFX app")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            ts: str = time.strftime("%H:%M:%S")
            print(f"[{ts}]")
            query_firmware_effect(dev)
            query_zone_snapshot(dev, dev.zone_count)
            print()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nDone.")

    dev.close()


if __name__ == "__main__":
    main()
