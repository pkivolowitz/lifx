"""BLE signal meter — passive advertisement scanner for diagnostics.

Continuously listens for BLE advertisements from a specific device
and reports RSSI signal strength on every received advertisement.
Tracks gap timing to identify when a device drops off.

**Requires the glowup-ble-sensor daemon to be stopped first.**
The daemon holds a persistent GATT connection and most BLE devices
stop advertising while connected.  Stop the daemon, run this tool,
then restart the daemon when done.

Usage::

    python3 -m ble.signal_meter --config ble_pairing.json --label onvis_motion
    python3 -m ble.signal_meter --address AA:BB:CC:DD:EE:FF
    python3 -m ble signal --label onvis_motion

Press Ctrl+C for summary and exit.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from typing import Optional

logger: logging.Logger = logging.getLogger("glowup.ble.signal_meter")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# RSSI range for the visual bar.  Typical BLE indoor range is
# -90 (barely detectable) to -50 (strong nearby signal).
RSSI_FLOOR: int = -95
RSSI_CEIL: int = -50

# Width of the visual signal bar in characters.
BAR_WIDTH: int = 20

# Seconds without an advertisement before declaring the device lost.
LOST_THRESHOLD_DEFAULT: float = 10.0

# Filled and empty bar characters for signal display.
BAR_FILLED: str = "\u2588"   # █
BAR_EMPTY: str = "\u2591"    # ░


# ---------------------------------------------------------------------------
# Statistics tracker
# ---------------------------------------------------------------------------

class SignalStats:
    """Running statistics for RSSI and advertisement gaps.

    Tracks min/max/sum for RSSI and gap timing, plus a count of
    'lost' events (gaps exceeding the lost threshold).

    Attributes:
        count: Total advertisements received.
        lost_count: Number of gaps exceeding the lost threshold.
    """

    def __init__(self, lost_threshold: float) -> None:
        """Initialize statistics.

        Args:
            lost_threshold: Seconds without an advertisement to declare lost.
        """
        self.lost_threshold: float = lost_threshold
        self.count: int = 0
        self.lost_count: int = 0
        self._rssi_min: int = 0
        self._rssi_max: int = -200
        self._rssi_sum: int = 0
        self._gap_min: float = float("inf")
        self._gap_max: float = 0.0
        self._gap_sum: float = 0.0
        self._gap_count: int = 0
        self._last_time: Optional[float] = None
        self._start_time: float = time.monotonic()

    def record(self, rssi: int, now: float) -> tuple[float, bool]:
        """Record one advertisement.

        Args:
            rssi: Signal strength in dBm.
            now: Monotonic timestamp of this advertisement.

        Returns:
            Tuple of (gap_seconds, was_lost).  gap_seconds is 0.0 for
            the first advertisement.
        """
        self.count += 1
        self._rssi_sum += rssi
        if rssi < self._rssi_min:
            self._rssi_min = rssi
        if rssi > self._rssi_max:
            self._rssi_max = rssi

        gap: float = 0.0
        was_lost: bool = False

        if self._last_time is not None:
            gap = now - self._last_time
            self._gap_count += 1
            self._gap_sum += gap
            if gap < self._gap_min:
                self._gap_min = gap
            if gap > self._gap_max:
                self._gap_max = gap
            if gap >= self.lost_threshold:
                self.lost_count += 1
                was_lost = True

        self._last_time = now
        return gap, was_lost

    @property
    def duration(self) -> float:
        """Elapsed seconds since tracking started."""
        return time.monotonic() - self._start_time

    @property
    def rssi_min(self) -> int:
        """Minimum RSSI seen (weakest signal)."""
        return self._rssi_min if self.count else 0

    @property
    def rssi_max(self) -> int:
        """Maximum RSSI seen (strongest signal)."""
        return self._rssi_max if self.count else 0

    @property
    def rssi_avg(self) -> float:
        """Mean RSSI across all advertisements."""
        return self._rssi_sum / self.count if self.count else 0.0

    @property
    def gap_min(self) -> float:
        """Shortest gap between advertisements."""
        return self._gap_min if self._gap_count else 0.0

    @property
    def gap_max(self) -> float:
        """Longest gap between advertisements."""
        return self._gap_max if self._gap_count else 0.0

    @property
    def gap_avg(self) -> float:
        """Mean gap between advertisements."""
        return self._gap_sum / self._gap_count if self._gap_count else 0.0


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _rssi_bar(rssi: int) -> str:
    """Render a visual bar for the given RSSI value.

    Maps RSSI from [RSSI_FLOOR, RSSI_CEIL] onto [0, BAR_WIDTH] filled
    characters, clamped at the endpoints.

    Args:
        rssi: Signal strength in dBm.

    Returns:
        String of BAR_WIDTH characters mixing filled and empty blocks.
    """
    clamped: int = max(RSSI_FLOOR, min(RSSI_CEIL, rssi))
    ratio: float = (clamped - RSSI_FLOOR) / (RSSI_CEIL - RSSI_FLOOR)
    filled: int = int(ratio * BAR_WIDTH)
    return BAR_FILLED * filled + BAR_EMPTY * (BAR_WIDTH - filled)


def _format_duration(seconds: float) -> str:
    """Format seconds as a human-readable duration string.

    Args:
        seconds: Duration in seconds.

    Returns:
        Formatted string like '5m 32s' or '1h 12m 5s'.
    """
    s: int = int(seconds)
    if s < 60:
        return f"{s}s"
    m: int = s // 60
    s = s % 60
    if m < 60:
        return f"{m}m {s}s"
    h: int = m // 60
    m = m % 60
    return f"{h}h {m}m {s}s"


# ---------------------------------------------------------------------------
# Scanner core
# ---------------------------------------------------------------------------

def _print_summary(label: str, stats: SignalStats, lost_threshold: float) -> None:
    """Print final signal statistics.

    Called from atexit so it runs even when the process is killed by
    SIGHUP (SSH disconnect) or SIGTERM.

    Args:
        label: Device label for the header.
        stats: Accumulated signal statistics.
        lost_threshold: Threshold used for lost-event detection.
    """
    try:
        print(flush=True)
        print(f"--- {label} signal summary ---")
        print(f"Duration:     {_format_duration(stats.duration)}")
        print(f"Seen:         {stats.count} advertisements")
        if stats.count:
            print(f"RSSI:         min={stats.rssi_min}  max={stats.rssi_max}  avg={stats.rssi_avg:.0f}")
        if stats.count > 1:
            print(f"Gap:          min={stats.gap_min:.1f}s  max={stats.gap_max:.1f}s  avg={stats.gap_avg:.1f}s")
        print(f"Lost events:  {stats.lost_count} (>{lost_threshold}s without advertisement)")
        sys.stdout.flush()
    except Exception as exc:
        logger.debug("Signal summary output failed (stdout may be closed): %s", exc)


async def run_signal_meter(
    address: str,
    label: str,
    lost_threshold: float = LOST_THRESHOLD_DEFAULT,
) -> None:
    """Run the passive BLE signal meter.

    Scans indefinitely for advertisements from the given address.
    Prints one line per received advertisement.  Prints a summary
    on exit via atexit (works over SSH where SIGHUP kills the process
    before finally blocks run).

    Args:
        address: BLE MAC address to monitor.
        label: Human-readable device label for display.
        lost_threshold: Seconds without an ad to flag as lost.

    Raises:
        ImportError: If bleak is not installed.
    """
    import atexit

    try:
        from bleak import BleakScanner
    except ImportError:
        raise ImportError(
            "BLE scanning requires bleak: pip install bleak"
        )

    stats: SignalStats = SignalStats(lost_threshold)
    # Register summary as atexit handler so it fires on any exit path
    # including SIGHUP (SSH disconnect), SIGTERM, and SIGINT.
    atexit.register(_print_summary, label, stats, lost_threshold)

    # Normalize address for comparison (BlueZ uses uppercase colons).
    target: str = address.upper()

    print(f"Monitoring {label} ({target})  lost_threshold={lost_threshold}s")
    print(f"{'Time':<15}  {'RSSI':>7}  {'Signal':<{BAR_WIDTH}}  Gap")
    print("-" * (15 + 2 + 7 + 2 + BAR_WIDTH + 2 + 20))

    def _on_advertisement(device, advertisement_data) -> None:
        """Process each BLE advertisement from the scanner."""
        if device.address.upper() != target:
            return

        now: float = time.monotonic()
        rssi: int = advertisement_data.rssi
        gap, was_lost = stats.record(rssi, now)

        ts: str = datetime.now().strftime("%H:%M:%S.%f")[:12]
        bar: str = _rssi_bar(rssi)
        gap_str: str = f"gap={gap:.1f}s" if gap > 0 else ""
        lost_str: str = f"  ** LOST {gap - lost_threshold:.1f}s **" if was_lost else ""

        print(f"{ts}  RSSI={rssi:>4}  {bar}  {gap_str}{lost_str}", flush=True)

    scanner = BleakScanner(detection_callback=_on_advertisement)
    await scanner.start()

    try:
        while True:
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass
    finally:
        await scanner.stop()


# ---------------------------------------------------------------------------
# Address resolution
# ---------------------------------------------------------------------------

def _resolve_address(
    label: Optional[str],
    address: Optional[str],
    config_path: str,
) -> tuple[str, str]:
    """Resolve a BLE address from --label or --address.

    Args:
        label: Device label to look up in pairing config.
        address: Direct BLE address (takes priority over label).
        config_path: Path to ble_pairing.json.

    Returns:
        Tuple of (address, label).

    Raises:
        SystemExit: If neither provided or label not found.
    """
    if address:
        return address, label or address

    if not label:
        print("Error: provide --label or --address", file=sys.stderr)
        sys.exit(1)

    try:
        with open(config_path, "r") as f:
            pairing: dict = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Error reading {config_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    device: Optional[dict] = pairing.get("devices", {}).get(label)
    if device is None:
        print(f"Error: label '{label}' not in {config_path}", file=sys.stderr)
        sys.exit(1)

    resolved: Optional[str] = device.get("address")
    if not resolved:
        print(f"Error: no address for '{label}' in {config_path}", file=sys.stderr)
        sys.exit(1)

    return resolved, label


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for the BLE signal meter."""
    parser = argparse.ArgumentParser(
        description="BLE signal meter — passive RSSI monitor for diagnostics. "
        "Stop glowup-ble-sensor first (sudo systemctl stop glowup-ble-sensor).",
    )
    parser.add_argument(
        "--label", "-l",
        help="Device label from ble_pairing.json",
    )
    parser.add_argument(
        "--address", "-a",
        help="Direct BLE MAC address (overrides --label)",
    )
    parser.add_argument(
        "--config", "-c",
        default="ble_pairing.json",
        help="Path to ble_pairing.json (default: ble_pairing.json)",
    )
    parser.add_argument(
        "--lost-threshold", "-t",
        type=float,
        default=LOST_THRESHOLD_DEFAULT,
        help=f"Seconds without ad to flag as LOST (default: {LOST_THRESHOLD_DEFAULT})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    address, label = _resolve_address(args.label, args.address, args.config)

    import signal as signal_mod

    # Convert SIGHUP (SSH disconnect) to SystemExit so atexit handlers
    # fire and the summary prints before the process dies.
    def _hup_handler(signum: int, frame) -> None:
        """Convert SIGHUP to SystemExit for clean atexit."""
        raise SystemExit(0)

    signal_mod.signal(signal_mod.SIGHUP, _hup_handler)

    loop = asyncio.new_event_loop()
    task = loop.create_task(
        run_signal_meter(address, label, args.lost_threshold)
    )

    def _shutdown() -> None:
        """Cancel the meter task on SIGINT/SIGTERM for clean summary."""
        task.cancel()

    try:
        loop.add_signal_handler(signal_mod.SIGINT, _shutdown)
        loop.add_signal_handler(signal_mod.SIGTERM, _shutdown)
    except NotImplementedError:
        pass  # Windows — KeyboardInterrupt will handle it.

    try:
        loop.run_until_complete(task)
    except KeyboardInterrupt:
        task.cancel()
        loop.run_until_complete(task)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
