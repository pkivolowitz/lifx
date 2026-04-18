#!/usr/bin/env python3
"""Authorized BLE control demo for Govee H7060 Aurora outdoor flood light.

Engagement scope: this tool is for hardware you own or for targets whose
owner has given explicit permission to test.  The Govee H70xx family has
no BLE-layer authentication, pairing, or encryption — anyone in radio
range can drive the light via standard GATT writes.  That is the whole
"vulnerability" being demonstrated; there is no exploit of a bug, just
use of the documented-by-reverse-engineering protocol.

Hard requirement: snapshot the light's original state on connect and
restore it on exit.  If the state cannot be faithfully restored (e.g.
the light is in a scene/effect mode whose identifier the query API does
not return), the script aborts before sending any set commands unless
--force-unrecoverable is passed.

Protocol reference: the H70xx family speaks 20-byte frames over a
write-without-response GATT characteristic, with a 1-byte XOR checksum
in byte 19.  Header 0x33 writes state, header 0xaa queries it; query
responses arrive on the notify characteristic.

Cross-platform: works on macOS (CoreBluetooth via bleak), Linux (BlueZ
via bleak), and Windows.  On macOS, Terminal.app or iTerm must be
granted Bluetooth permission in System Settings → Privacy & Security.
"""

from __future__ import annotations

__version__: str = "1.0.0"

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from typing import Optional

logger: logging.Logger = logging.getLogger("glowup.govee_h7060_pwn")

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
except ImportError:
    print("error: bleak is not installed.  pip install bleak", file=sys.stderr)
    sys.exit(1)


# GATT topology — same across the Govee H6xxx / H70xx BLE families.
SERVICE_UUID: str = "00010203-0405-0607-0809-0a0b0c0d1910"
WRITE_UUID: str = "00010203-0405-0607-0809-0a0b0c0d2b11"
NOTIFY_UUID: str = "00010203-0405-0607-0809-0a0b0c0d2b10"

# Frame layout.
FRAME_LEN: int = 20
HDR_SET: int = 0x33
HDR_QUERY: int = 0xAA

# Command bytes.
CMD_POWER: int = 0x01
CMD_BRIGHTNESS: int = 0x04
CMD_COLOR: int = 0x05

# Color/mode payload byte that means "manual RGB, not a scene".
MODE_MANUAL_COLOR: int = 0x02

# Defaults.
DEFAULT_RSSI_MIN: int = -85
DEFAULT_SCAN_SECS: float = 10.0
DEFAULT_HOLD_SECS: float = 0.8
DEFAULT_CONNECT_TIMEOUT: float = 20.0

# ROYGBIV for the demo — six distinct hues, unambiguously program-driven.
RAINBOW: list[tuple[int, int, int]] = [
    (0xFF, 0x00, 0x00),  # red
    (0xFF, 0x60, 0x00),  # orange
    (0xFF, 0xE0, 0x00),  # yellow
    (0x00, 0xFF, 0x00),  # green
    (0x00, 0x40, 0xFF),  # blue
    (0x90, 0x00, 0xFF),  # violet
]


@dataclass
class GoveeSnapshot:
    """State captured from the light at connect time."""

    power: Optional[int] = None
    brightness: Optional[int] = None
    mode: Optional[int] = None
    rgb: Optional[tuple[int, int, int]] = None

    def is_complete(self) -> bool:
        """True when power, brightness, and mode have all been queried."""
        return (
            self.power is not None
            and self.brightness is not None
            and self.mode is not None
        )

    def is_recoverable(self) -> bool:
        """True when we have enough info to put the light back exactly.

        The Govee BLE query API returns the current static RGB even when
        the light is running a scene effect, which means we cannot tell
        which scene was selected and therefore cannot restore it.  In
        that case we refuse to touch the light unless the operator
        explicitly overrides.
        """
        if not self.is_complete():
            return False
        if self.mode == MODE_MANUAL_COLOR:
            return self.rgb is not None
        return False

    def format(self) -> str:
        """Return a human-readable description of the captured state."""
        if not self.is_complete():
            return (
                f"INCOMPLETE: power={self.power} brightness={self.brightness} "
                f"mode={self.mode} rgb={self.rgb}"
            )
        power_s = "ON" if self.power else "OFF"
        if self.mode == MODE_MANUAL_COLOR:
            mode_s = "manual color"
        else:
            mode_s = f"scene/effect mode 0x{self.mode:02x}"
        rgb_s = f"rgb={self.rgb}" if self.rgb else "rgb=unknown"
        return f"{power_s}, brightness={self.brightness}/255, {mode_s}, {rgb_s}"


def build_frame(header: int, cmd: int, payload: list[int]) -> bytes:
    """Assemble a 20-byte Govee frame with trailing XOR checksum."""
    if len(payload) > FRAME_LEN - 3:
        raise ValueError(f"payload too long ({len(payload)} > {FRAME_LEN - 3})")
    buf = bytearray(FRAME_LEN)
    buf[0] = header
    buf[1] = cmd
    for i, b in enumerate(payload):
        buf[2 + i] = b & 0xFF
    checksum = 0
    for b in buf[: FRAME_LEN - 1]:
        checksum ^= b
    buf[FRAME_LEN - 1] = checksum
    return bytes(buf)


class GoveeController:
    """Thin wrapper around a connected BleakClient speaking Govee BLE."""

    def __init__(self, client: BleakClient) -> None:
        self._client: BleakClient = client
        self._responses: asyncio.Queue[bytes] = asyncio.Queue()

    async def start(self) -> None:
        """Subscribe to GATT notifications for query responses."""
        await self._client.start_notify(NOTIFY_UUID, self._on_notify)

    async def stop(self) -> None:
        """Unsubscribe from GATT notifications."""
        try:
            await self._client.stop_notify(NOTIFY_UUID)
        except Exception as exc:
            logger.debug("Error stopping GATT notifications: %s", exc)

    def _on_notify(self, _sender, data: bytearray) -> None:
        self._responses.put_nowait(bytes(data))

    async def _send(self, frame: bytes) -> None:
        await self._client.write_gatt_char(WRITE_UUID, frame, response=False)

    async def _drain_responses(self) -> None:
        while not self._responses.empty():
            try:
                self._responses.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _query(
        self,
        cmd: int,
        payload: Optional[list[int]] = None,
        timeout: float = 2.0,
    ) -> Optional[bytes]:
        """Send a query frame and wait for the matching response."""
        await self._drain_responses()
        await self._send(build_frame(HDR_QUERY, cmd, payload or []))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(
                    self._responses.get(), timeout=remaining
                )
            except asyncio.TimeoutError:
                return None
            if len(frame) >= 2 and frame[0] == HDR_QUERY and frame[1] == cmd:
                return frame
        return None

    async def snapshot(self) -> GoveeSnapshot:
        """Query power, brightness, and color to capture the current state."""
        snap = GoveeSnapshot()
        power_frame = await self._query(CMD_POWER)
        if power_frame and len(power_frame) >= 3:
            snap.power = power_frame[2]
        bright_frame = await self._query(CMD_BRIGHTNESS)
        if bright_frame and len(bright_frame) >= 3:
            snap.brightness = bright_frame[2]
        color_frame = await self._query(CMD_COLOR)
        if color_frame and len(color_frame) >= 3:
            snap.mode = color_frame[2]
            if snap.mode == MODE_MANUAL_COLOR and len(color_frame) >= 6:
                snap.rgb = (color_frame[3], color_frame[4], color_frame[5])
        return snap

    async def set_power(self, on: bool) -> None:
        """Turn the light on or off."""
        await self._send(build_frame(HDR_SET, CMD_POWER, [0x01 if on else 0x00]))

    async def set_brightness(self, level: int) -> None:
        """Set brightness (0-255, clamped)."""
        clamped = max(0, min(255, int(level)))
        await self._send(build_frame(HDR_SET, CMD_BRIGHTNESS, [clamped]))

    async def set_color(self, r: int, g: int, b: int) -> None:
        """Set the light to a static RGB color."""
        await self._send(
            build_frame(HDR_SET, CMD_COLOR, [MODE_MANUAL_COLOR, r, g, b])
        )

    async def restore(self, snap: GoveeSnapshot) -> bool:
        """Best-effort write-back of a previously captured snapshot."""
        if not snap.is_recoverable():
            return False
        # Order: brightness → color → power.  Setting brightness before
        # color avoids a full-white flash if the color write is applied
        # at the default 255.  Setting power last means the final visible
        # transition is exactly the original state, not an intermediate.
        assert snap.brightness is not None
        await self.set_brightness(snap.brightness)
        await asyncio.sleep(0.15)
        assert snap.rgb is not None
        await self.set_color(*snap.rgb)
        await asyncio.sleep(0.15)
        assert snap.power is not None
        await self.set_power(bool(snap.power))
        await asyncio.sleep(0.15)
        return True


async def run_engagement(
    target: BLEDevice,
    rssi: int,
    args: argparse.Namespace,
) -> int:
    """Connect to a target, run the rainbow demo, and restore original state."""
    print(f"\n[+] connecting to {target.address}  {target.name}  RSSI={rssi} dBm")
    async with BleakClient(target, timeout=args.connect_timeout) as client:
        if not client.is_connected:
            print("[-] connect failed")
            return 1
        print("[+] connected")

        controller = GoveeController(client)
        await controller.start()
        try:
            print("[+] querying original state...")
            snap = await controller.snapshot()
            print(f"[+] snapshot: {snap.format()}")

            if not snap.is_recoverable():
                print("[!] cannot faithfully restore this state.")
                if snap.mode is not None and snap.mode != MODE_MANUAL_COLOR:
                    print(
                        "[!] light is in a scene/effect mode; the BLE query API "
                        "does not return the scene identifier,"
                    )
                    print(
                        "[!] so restore would leave the light in a static color "
                        "(visibly wrong)."
                    )
                if not args.force_unrecoverable:
                    print("[-] aborting.  pass --force-unrecoverable to proceed anyway.")
                    return 2
                print("[!] proceeding under --force-unrecoverable")

            if args.dry_run:
                print("[+] --dry-run: snapshot complete, no set commands will be sent")
                return 0

            if args.pause_before_demo:
                try:
                    input("\n[?] snapshot captured.  press ENTER to start rainbow ")
                except (EOFError, KeyboardInterrupt):
                    print("\n[-] aborted before demo")
                    return 0

            print("[+] running rainbow demo...")
            await controller.set_power(True)
            await asyncio.sleep(0.2)
            await controller.set_brightness(255)
            await asyncio.sleep(0.2)
            for r, g, b in RAINBOW:
                await controller.set_color(r, g, b)
                print(f"    rgb=({r:3d},{g:3d},{b:3d})  hold={args.hold:.1f}s")
                await asyncio.sleep(args.hold)

            print("[+] restoring original state...")
            restored = await controller.restore(snap)
            if not restored:
                print("[-] restore failed (snapshot not recoverable)")
                return 3

            if args.verify:
                print("[+] re-querying to verify restore...")
                await asyncio.sleep(0.4)
                after = await controller.snapshot()
                print(f"[+] post-restore: {after.format()}")
                if (
                    after.power == snap.power
                    and after.brightness == snap.brightness
                    and after.mode == snap.mode
                    and after.rgb == snap.rgb
                ):
                    print("[+] VERIFIED: post-restore state matches snapshot")
                else:
                    print("[!] WARNING: post-restore state differs from snapshot")
                    return 4

            return 0
        finally:
            await controller.stop()


async def scan_and_select(
    args: argparse.Namespace,
) -> Optional[tuple[BLEDevice, int]]:
    """Scan for Govee H7060 devices and select the strongest signal."""
    print(f"[+] scanning {args.scan_secs:.0f}s for Govee_H7060_* ...")
    best: dict[str, tuple[BLEDevice, int]] = {}

    def on_detect(dev: BLEDevice, ad: "AdvertisementData") -> None:
        """Filter and track Govee H7060 advertisements by RSSI."""
        name = dev.name or getattr(ad, "local_name", None) or ""
        if not name.startswith("Govee_H7060_"):
            return
        rssi = getattr(ad, "rssi", None)
        if rssi is None:
            return
        prior = best.get(dev.address)
        if prior is None or rssi > prior[1]:
            best[dev.address] = (dev, rssi)

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    try:
        await asyncio.sleep(args.scan_secs)
    finally:
        await scanner.stop()

    if not best:
        print("[-] no Govee H7060 devices visible")
        print("    if you expect one in range, the usual suspects are:")
        print("      - Bluetooth permission not granted to Terminal (macOS)")
        print("      - signal too weak (try moving closer)")
        print("      - light is in WiFi-only mode (BLE disabled)")
        return None

    matches = sorted(best.values(), key=lambda x: x[1], reverse=True)
    print(f"[+] found {len(matches)} Govee H7060 device(s):")
    for i, (dev, rssi) in enumerate(matches):
        marker = "*" if i == 0 else " "
        print(f"  {marker} {dev.address}  {dev.name}  RSSI={rssi} dBm")

    chosen, rssi = matches[0]

    if rssi < args.rssi_min:
        print(
            f"[!] WARNING: best RSSI {rssi} dBm is below the floor "
            f"of {args.rssi_min} dBm"
        )
        print("[!] GATT connect will probably time out or drop mid-session.")
        print("[!] Move physically closer and re-scan.")
        if not args.force_weak:
            print("[-] aborting.  pass --force-weak to try anyway.")
            return None
        print("[!] proceeding under --force-weak")

    if not args.yes:
        prompt = (
            f"\nConnect to {chosen.address}  {chosen.name}  "
            f"and transmit? [y/N] "
        )
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            print("[-] aborted by operator")
            return None

    return chosen, rssi


async def main_async(args: argparse.Namespace) -> int:
    """Async entry point: scan, select, and run the engagement."""
    selection = await scan_and_select(args)
    if selection is None:
        return 1
    target, rssi = selection
    return await run_engagement(target, rssi, args)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(
        description=(
            "Authorized BLE control demo for Govee H7060 Aurora.  "
            "Use only against hardware you own or have explicit permission to test."
        ),
    )
    p.add_argument(
        "--scan-secs",
        type=float,
        default=DEFAULT_SCAN_SECS,
        help=f"scan duration in seconds (default {DEFAULT_SCAN_SECS})",
    )
    p.add_argument(
        "--rssi-min",
        type=int,
        default=DEFAULT_RSSI_MIN,
        help=(
            f"minimum acceptable RSSI in dBm (default {DEFAULT_RSSI_MIN}); "
            "below this, GATT connects typically fail"
        ),
    )
    p.add_argument(
        "--force-weak",
        action="store_true",
        help="proceed even if RSSI is below --rssi-min",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="skip the connect-confirmation prompt",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="connect and snapshot only; do not send any set commands",
    )
    p.add_argument(
        "--force-unrecoverable",
        action="store_true",
        help="proceed even if the original state cannot be faithfully restored",
    )
    p.add_argument(
        "--hold",
        type=float,
        default=DEFAULT_HOLD_SECS,
        help=f"seconds to hold each rainbow color (default {DEFAULT_HOLD_SECS})",
    )
    p.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT,
        help=f"GATT connect timeout in seconds (default {DEFAULT_CONNECT_TIMEOUT})",
    )
    p.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="skip the post-restore verification query",
    )
    p.add_argument(
        "--pause-before-demo",
        action="store_true",
        help=(
            "after the snapshot, wait for ENTER before starting the "
            "rainbow (useful when filming: confirm connect, start phone "
            "recording, then trigger transmission)"
        ),
    )
    return p.parse_args()


def main() -> int:
    """CLI entry point."""
    args = parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[-] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
