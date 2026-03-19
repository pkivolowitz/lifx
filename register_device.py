#!/usr/bin/env python3
"""Quick device registration helper.

Registers a LIFX device by IP address.  Resolves the MAC from the
local ARP table, prompts for a label, optionally writes the label
to the bulb firmware, and saves to the device registry.

Usage::

    python3 register_device.py 10.0.0.44 "porch-left"
    python3 register_device.py 10.0.0.44          # prompts for label
    python3 register_device.py --list              # show registry
    python3 register_device.py --push-labels       # write all labels to bulbs

Designed for rapid use during a bulk identification session.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import os
import platform
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: LIFX OUI prefix.
LIFX_OUI: str = "d0:73:d5"

#: LIFX protocol port.
LIFX_PORT: int = 56700

#: Default registry path.
DEFAULT_REGISTRY_PATH: str = "/etc/glowup/device_registry.json"

#: Environment variable override for registry path.
ENV_REGISTRY_PATH: str = "GLOWUP_DEVICE_REGISTRY"

#: Maximum label length in bytes (LIFX firmware).
MAX_LABEL_BYTES: int = 32

#: Socket timeout for LIFX queries (seconds).
QUERY_TIMEOUT: float = 2.0

#: Regex matching a colon-separated MAC.
MAC_RE: re.Pattern[str] = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")

#: macOS ARP line regex.
ARP_RE: re.Pattern[str] = re.compile(
    r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([\da-fA-F:]+)"
)


# ---------------------------------------------------------------------------
# ARP resolution
# ---------------------------------------------------------------------------

def resolve_mac(ip: str) -> Optional[str]:
    """Resolve an IP to a LIFX MAC address via ARP.

    Pings the IP first to ensure it's in the ARP cache, then reads
    the system ARP table.

    Args:
        ip: IPv4 address string.

    Returns:
        Lowercase colon-separated MAC, or ``None`` if not found or
        not a LIFX device.
    """
    # Ping to populate ARP cache.
    ping_flag: str = "-c" if platform.system() != "Windows" else "-n"
    subprocess.run(
        ["ping", ping_flag, "1", "-W", "1", ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if platform.system() == "Linux":
        try:
            with open("/proc/net/arp", "r", encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 4 and parts[0] == ip:
                        mac: str = parts[3].lower()
                        if mac.startswith(LIFX_OUI):
                            return mac
        except OSError:
            pass
    else:
        try:
            output: str = subprocess.check_output(
                ["arp", "-an"], text=True, timeout=5.0,
            )
            for match in ARP_RE.finditer(output):
                if match.group(1) == ip:
                    mac = match.group(2).lower()
                    if mac.startswith(LIFX_OUI):
                        return mac
        except (subprocess.SubprocessError, OSError):
            pass

    return None


# ---------------------------------------------------------------------------
# Registry file I/O
# ---------------------------------------------------------------------------

def load_registry(path: str) -> Dict[str, dict]:
    """Load the registry file, returning the devices dict."""
    if not Path(path).exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("devices", {})


def save_registry(path: str, devices: Dict[str, dict]) -> None:
    """Save the registry file atomically.

    Creates parent directories if they do not exist.
    """
    data = {
        "_comment": (
            "MAC-based device identity.  Survives DHCP changes "
            "and git pulls.  Do not edit while server is running."
        ),
        "devices": devices,
    }
    parent: Path = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp: str = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# LIFX SetLabel
# ---------------------------------------------------------------------------

def push_label(ip: str, label: str) -> bool:
    """Write a label to a LIFX device via SetLabel (type 24).

    Args:
        ip:    Device IP address.
        label: Label string (max 32 bytes UTF-8).

    Returns:
        ``True`` if the device acknowledged, ``False`` on timeout.
    """
    encoded: bytes = label.encode("utf-8")[:MAX_LABEL_BYTES]
    payload: bytes = encoded.ljust(MAX_LABEL_BYTES, b'\x00')

    # Build LIFX header (36 bytes) + payload.
    msg_type: int = 24  # SetLabel
    size: int = 36 + len(payload)

    header: bytearray = bytearray(36)
    # Size
    header[0:2] = struct.pack("<H", size)
    # Protocol + addressable + tagged
    header[2:4] = struct.pack("<H", 1024 | (1 << 12))
    # Source
    header[4:8] = struct.pack("<I", 42)
    # Target (all zeros = broadcast to this IP)
    header[8:16] = bytes(8)
    # Reserved
    header[16:22] = bytes(6)
    # ack_required = 1
    header[22] = 0x02  # ack_required bit
    # Sequence
    header[23] = 1
    # Reserved
    header[24:32] = bytes(8)
    # Message type
    header[32:34] = struct.pack("<H", msg_type)
    # Reserved
    header[34:36] = bytes(2)

    sock: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(QUERY_TIMEOUT)

    try:
        sock.sendto(bytes(header) + payload, (ip, LIFX_PORT))

        # Wait for ack (type 45).
        deadline: float = time.time() + QUERY_TIMEOUT
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(1500)
                if len(data) >= 36:
                    resp_type: int = struct.unpack("<H", data[32:34])[0]
                    if resp_type == 45:  # Acknowledgement
                        return True
            except socket.timeout:
                break
            except OSError:
                break
    finally:
        sock.close()

    return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list(registry_path: str) -> None:
    """List all registered devices."""
    devices: Dict[str, dict] = load_registry(registry_path)
    if not devices:
        print("(empty registry)")
        return

    print(f"\n{'MAC Address':19}  {'Label':24}  {'Notes'}")
    print("=" * 60)
    for mac in sorted(devices.keys()):
        entry = devices[mac]
        label: str = entry.get("label", "?")
        notes: str = entry.get("notes", "")
        print(f"{mac:19}  {label:24}  {notes}")
    print(f"\n{len(devices)} device(s) registered.")


def cmd_add(
    registry_path: str,
    ip: str,
    label: str,
    write_to_bulb: bool = True,
) -> None:
    """Register a device by IP address.

    Resolves the MAC from ARP, adds to registry, optionally writes
    the label to the bulb firmware.

    Args:
        registry_path: Path to the registry JSON file.
        ip:            Device IP address.
        label:         User-defined label.
        write_to_bulb: If ``True``, also push label to firmware.
    """
    # Resolve MAC.
    mac: Optional[str] = resolve_mac(ip)
    if mac is None:
        print(f"ERROR: Cannot resolve MAC for {ip} — is the device online?",
              file=sys.stderr)
        sys.exit(1)

    # Validate label length.
    label_bytes: int = len(label.encode("utf-8"))
    if label_bytes > MAX_LABEL_BYTES:
        print(
            f"ERROR: Label {label!r} is {label_bytes} bytes "
            f"(max {MAX_LABEL_BYTES})",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load existing registry.
    devices: Dict[str, dict] = load_registry(registry_path)

    # Check for label collision.
    for existing_mac, entry in devices.items():
        if entry.get("label", "").lower() == label.lower():
            if existing_mac != mac:
                print(
                    f"ERROR: Label {label!r} already assigned to "
                    f"{existing_mac}",
                    file=sys.stderr,
                )
                sys.exit(1)

    # Add or update.
    devices[mac] = {"label": label}
    save_registry(registry_path, devices)
    print(f"Registered: {mac} → {label}")

    # Write to bulb firmware.
    if write_to_bulb:
        if push_label(ip, label):
            print(f"Label written to bulb firmware: {label}")
        else:
            print(f"WARNING: Could not write label to bulb (timeout)")


def cmd_push_labels(registry_path: str) -> None:
    """Write all registry labels to their respective bulbs.

    Resolves each MAC to IP via ARP, then sends SetLabel.
    """
    devices: Dict[str, dict] = load_registry(registry_path)
    if not devices:
        print("(empty registry — nothing to push)")
        return

    # Build current ARP table.
    arp: Dict[str, str] = {}  # IP → MAC
    if platform.system() == "Linux":
        try:
            with open("/proc/net/arp", "r", encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 4:
                        arp[parts[0]] = parts[3].lower()
        except OSError:
            pass
    else:
        try:
            output = subprocess.check_output(
                ["arp", "-an"], text=True, timeout=5.0,
            )
            for match in ARP_RE.finditer(output):
                arp[match.group(1)] = match.group(2).lower()
        except (subprocess.SubprocessError, OSError):
            pass

    # Reverse: MAC → IP.
    mac_to_ip: Dict[str, str] = {mac: ip for ip, mac in arp.items()}

    success: int = 0
    failed: int = 0
    offline: int = 0

    for mac, entry in sorted(devices.items()):
        label: str = entry.get("label", "")
        if not label:
            continue

        ip: Optional[str] = mac_to_ip.get(mac)
        if ip is None:
            print(f"  OFFLINE  {mac}  {label}")
            offline += 1
            continue

        if push_label(ip, label):
            print(f"  OK       {mac}  {label} → {ip}")
            success += 1
        else:
            print(f"  FAILED   {mac}  {label} → {ip}")
            failed += 1

    print(f"\nPushed: {success}  Failed: {failed}  Offline: {offline}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point — parse args and dispatch."""
    registry_path: str = os.environ.get(
        ENV_REGISTRY_PATH, DEFAULT_REGISTRY_PATH
    )

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    arg1: str = sys.argv[1]

    if arg1 == "--list":
        cmd_list(registry_path)
    elif arg1 == "--push-labels":
        cmd_push_labels(registry_path)
    else:
        # Assume: register_device.py <ip> [label]
        ip: str = arg1
        if len(sys.argv) >= 3:
            label: str = sys.argv[2]
        else:
            label = input(f"Label for {ip}: ").strip()
            if not label:
                print("ERROR: Label cannot be empty", file=sys.stderr)
                sys.exit(1)
        cmd_add(registry_path, ip, label)


if __name__ == "__main__":
    main()
