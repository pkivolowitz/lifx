#!/usr/bin/env python3
"""Raw UDP send diagnostic — bypasses engine, tests device responsiveness.

Sends a single lit zone walking across the strip using the transport
layer directly.  No threads, no pipeline, no render loop — just
set_zones() in a sleep loop.

Usage::

    python3 diag_raw_send.py <ip> [hold_sec]

Examples::

    python3 diag_raw_send.py 10.0.0.34          # 1s hold
    python3 diag_raw_send.py 10.0.0.34 0.5      # 0.5s hold
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import sys
import time

from transport import LifxDevice

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HSBK_MAX: int = 65535
KELVIN_DEFAULT: int = 3500
DEFAULT_HOLD: float = 1.0

# Red at full brightness.
RED: tuple[int, int, int, int] = (0, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT)
BLACK: tuple[int, int, int, int] = (0, 0, 0, KELVIN_DEFAULT)


def main() -> None:
    """Walk a single red zone across the strip with raw sends."""
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <ip> [hold_sec]")
        sys.exit(1)

    ip: str = sys.argv[1]
    hold: float = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_HOLD

    print(f"Querying {ip}...", flush=True)
    dev: LifxDevice = LifxDevice(ip)
    dev.query_all()

    if dev.zone_count is None:
        print("Failed to query device.")
        sys.exit(1)

    zone_count: int = dev.zone_count
    print(f"Device: {dev.label} ({dev.product_name}), {zone_count} zones")
    print(f"MAC: {dev.mac_str}")
    print(f"Hold: {hold}s")

    # Match what the engine does before sending frames:
    # power on, clear firmware effect, set committed layer to black.
    dev.set_power(on=True, duration_ms=0)
    dev.clear_firmware_effect()
    dev.set_color(0, 0, 0, KELVIN_DEFAULT, duration_ms=0)
    time.sleep(0.2)

    print("Starting walk...", flush=True)
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            for z in range(zone_count):
                colors: list[tuple[int, int, int, int]] = [BLACK] * zone_count
                colors[z] = RED
                t0: float = time.monotonic()
                dev.set_zones(colors, duration_ms=100, rapid=True)
                elapsed: float = time.monotonic() - t0
                sys.stdout.write(f"\r  zone {z:3d}/{zone_count}  "
                                 f"send: {elapsed*1000:.1f}ms  ")
                sys.stdout.flush()
                time.sleep(hold)
    except KeyboardInterrupt:
        # Blank the strip on exit.
        dev.set_zones([BLACK] * zone_count, duration_ms=100, rapid=True)
        print("\nDone.")

    dev.close()


if __name__ == "__main__":
    main()
