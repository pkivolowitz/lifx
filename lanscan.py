#!/usr/bin/env python3
"""Scan the local LAN for all devices.

Reports MAC address, IP address, hardware vendor (via OUI lookup),
and reverse-DNS hostname for every device discovered on the local
subnet.  Uses an async ping sweep to populate the ARP table, then
enriches each entry with vendor and hostname information.

Usage::

    python3 lanscan.py

The script auto-detects the local network by parsing ``ifconfig``
output, so no arguments are required.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import asyncio
import ipaddress
import re
import socket
import subprocess
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# Named constants — no magic numbers
# ---------------------------------------------------------------------------

# Ping parameters
PING_COUNT: int = 1               # Number of ICMP echo requests per host
PING_TIMEOUT_MS: int = 500        # Milliseconds to wait for a reply (macOS -W)
PING_TTL: int = 2                 # Max hops — keeps probes on the local segment
PING_CONCURRENCY: int = 128       # Max simultaneous ping sub-processes
PROC_TIMEOUT: int = 5             # Seconds before killing a hung ping process

# Subprocess timeouts (seconds)
ARP_TIMEOUT: int = 30             # Timeout for `arp -an`
IFCONFIG_TIMEOUT: int = 5         # Timeout for `ifconfig`

# Display / formatting
MAX_COL_WIDTH: int = 120          # Target terminal width for table output
COL_SEP: str = "  "               # Separator between table columns

# MAC-address analysis
LOCALLY_ADMINISTERED_BIT: int = 0x02  # Bit-mask for the LA flag in octet 0
OUI_PREFIX_LEN: int = 8              # Length of "xx:yy:zz" prefix used for lookup

# Column definition indices (header, dict-key, minimum width)
COL_HDR: int = 0
COL_KEY: int = 1
COL_MIN: int = 2

# Minimum column widths for the output table
MIN_WIDTH_IP: int = 13
MIN_WIDTH_MAC: int = 17
MIN_WIDTH_VENDOR: int = 10
MIN_WIDTH_HOSTNAME: int = 8

# ARP sentinel values
BROADCAST_MAC: str = "ff:ff:ff:ff:ff:ff"
UNKNOWN_MAC: str = "?"

# Truncation indicator appended when a cell value exceeds column width
TRUNC_CHAR: str = "~"

# DNS suffixes stripped from reverse-lookup results for brevity
LOCAL_DNS_SUFFIXES: tuple[str, ...] = (".local", ".lan", ".home", ".localdomain")

# ---------------------------------------------------------------------------
# Common OUI prefixes (first 3 bytes of MAC) -> vendor name.
# Curated for home / IoT networks; unknown MACs show the raw prefix.
# ---------------------------------------------------------------------------
OUI_MAP: dict[str, str] = {
    "00:17:88": "Philips Hue",
    "00:1a:22": "eQ-3 (Homematic)",
    "00:1e:c2": "Apple",
    "00:50:56": "VMware",
    "00:80:92": "Silex Technology",
    "00:e0:4c": "Realtek",
    "04:d3:b0": "Intel",
    "08:00:27": "VirtualBox",
    "0c:47:c9": "Amazon",
    "0c:83:cc": "Alpha Networks",
    "10:0c:6b": "Netgear",
    "10:da:43": "Netgear",
    "14:91:82": "Belkin",
    "18:b4:30": "Nest",
    "1c:63:49": "Texas Instruments",
    "1c:69:7a": "Elgato",
    "20:df:b9": "Google",
    "24:0a:c4": "Espressif",
    "24:dc:c3": "Espressif",
    "28:6c:07": "Xiaomi",
    "2c:aa:8e": "Wyze",
    "30:05:5c": "Belkin",
    "30:52:cb": "Liteon",
    "34:ea:34": "HangZhou (Tuya)",
    "38:f7:3d": "Amazon",
    "3c:22:fb": "Apple",
    "3c:84:6a": "TP-Link",
    "3c:ec:ef": "Supermicro",
    "40:b0:76": "ASUSTek",
    "44:07:0b": "Google",
    "44:d9:e7": "Ubiquiti",
    "48:8f:5a": "Routerboard",
    "48:e1:5c": "Apple",
    "4c:57:ca": "Apple",
    "4c:6b:b8": "Gaoshengda",
    "50:14:79": "Liteon",
    "50:c7:bf": "TP-Link",
    "50:ed:3c": "Apple",
    "54:60:09": "Google",
    "58:ef:68": "Belkin",
    "5c:aa:fd": "Sonos",
    "5c:cf:7f": "Espressif",
    "60:01:94": "Espressif",
    "60:a4:4c": "ASUSTek",
    "60:d2:62": "Almond/Securifi",
    "64:57:25": "Gaoshengda",
    "68:54:fd": "Amazon",
    "68:a3:78": "Intel",
    "6c:5a:b5": "LG",
    "6c:c8:40": "Espressif",
    "70:3a:cb": "Google",
    "70:85:c2": "Apple",
    "70:ee:50": "Netatmo",
    "74:40:be": "LG",
    "74:a6:cd": "Apple",
    "74:ac:b9": "Ubiquiti",
    "74:da:38": "Edimax",
    "78:3f:4d": "Apple",
    "78:8a:20": "Ubiquiti",
    "7c:2e:bd": "Google",
    "80:71:7a": "Honeywell",
    "80:a9:97": "Apple",
    "84:0d:8e": "Roku",
    "84:d6:d0": "Amazon",
    "88:71:b1": "Amazon",
    "8c:85:90": "Apple",
    "90:dd:5d": "Apple",
    "94:10:3e": "Belkin",
    "94:9a:a9": "Sonos",
    "98:da:c4": "TP-Link",
    "9c:76:0e": "Apple",
    "9c:8e:cd": "Amcrest",
    "9e:ef:d5": "Pear/Random",
    "a0:20:a6": "Meross",
    "a4:08:ea": "Murata",
    "a4:34:d9": "Intel",
    "a4:77:33": "Google",
    "a4:cf:12": "Espressif",
    "a8:51:ab": "Apple",
    "ac:44:f2": "Yamaha",
    "ac:84:c6": "TP-Link",
    "ac:bc:b5": "Apple",
    "b0:81:84": "Espressif",
    "b0:be:76": "TP-Link",
    "b0:e4:d5": "Google",
    "b4:69:21": "Intel",
    "b4:e6:2d": "LG",
    "b8:01:1f": "Apple",
    "b8:27:eb": "Raspberry Pi",
    "b8:78:26": "Apple",
    "bc:32:b2": "Samsung",
    "c0:25:e9": "TP-Link",
    "c0:95:6d": "Apple",
    "c4:41:1e": "Belkin",
    "c4:ad:34": "Routerboard",
    "c8:2b:96": "Espressif",
    "c8:69:cd": "Apple",
    "cc:40:d0": "Netgear",
    "d0:03:4b": "Apple",
    "d0:12:55": "Gaoshengda",
    "d0:21:f9": "Ubiquiti",
    "d0:52:a8": "Amazon",
    "d0:73:d5": "LIFX",
    "d4:61:9d": "Apple",
    "d8:0d:17": "TP-Link",
    "dc:56:e7": "Apple",
    "dc:62:79": "TP-Link",
    "dc:a6:32": "Raspberry Pi",
    "e0:63:da": "Ubiquiti",
    "e4:5f:01": "Raspberry Pi",
    "e4:95:6e": "IEEE Reg (IoT)",
    "e8:48:b8": "Samsung",
    "e8:ff:1e": "IEEE Reg",
    "ec:08:6b": "TP-Link",
    "ec:71:db": "Reolink",
    "ec:fa:bc": "Espressif",
    "f0:03:8c": "AzureWave",
    "f0:27:2d": "Amazon",
    "f0:72:ea": "Samsung",
    "f4:12:fa": "Apple",
    "f4:cf:a2": "Espressif",
    "f8:1a:67": "TP-Link",
    "f8:73:df": "Apple",
    "fc:65:de": "Amazon",
    "fc:a1:83": "Amazon",
}


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def get_local_network() -> Optional[ipaddress.IPv4Network]:
    """Detect the local IP and subnet via ``ifconfig``.

    Parses the output of ``ifconfig`` to find the first non-loopback
    interface with a private IP address and its netmask.

    Returns:
        An ``ipaddress.IPv4Network`` representing the local subnet, or
        ``None`` if no suitable interface was found.

    Raises:
        No exceptions are raised; errors are caught internally and
        result in a ``None`` return value.
    """
    try:
        result = subprocess.run(
            ["ifconfig"],
            capture_output=True,
            text=True,
            timeout=IFCONFIG_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    # Match lines like: inet 192.0.2.38 netmask 0xfffffc00
    pattern = re.compile(
        r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)"
    )
    for match in pattern.finditer(result.stdout):
        ip_str: str = match.group(1)
        mask_hex: str = match.group(2)

        # Skip the loopback interface
        if ip_str.startswith("127."):
            continue

        # Convert hex netmask (e.g. 0xfffffc00) to a CIDR prefix length
        # by counting the number of set bits.
        mask_int: int = int(mask_hex, 16)
        prefix: int = bin(mask_int).count("1")
        network = ipaddress.IPv4Network(f"{ip_str}/{prefix}", strict=False)
        return network

    return None


async def ping_host(sem: asyncio.Semaphore, ip_str: str) -> Optional[str]:
    """Ping a single host asynchronously.

    Sends a single ICMP echo request with a short timeout to determine
    whether the host is reachable.  The semaphore limits how many pings
    run in parallel so the system does not exhaust file descriptors.

    Args:
        sem: An asyncio semaphore to limit concurrency.
        ip_str: The IPv4 address to ping as a dotted-quad string.

    Returns:
        The ``ip_str`` if the host responded, or ``None`` otherwise.
    """
    if not ip_str:
        return None

    async with sem:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping",
                "-c", str(PING_COUNT),
                "-W", str(PING_TIMEOUT_MS),
                "-t", str(PING_TTL),
                ip_str,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=PROC_TIMEOUT)
            return ip_str if proc.returncode == 0 else None
        except (asyncio.TimeoutError, OSError):
            # TimeoutError: process did not finish in time
            # OSError: system could not spawn the subprocess
            return None


async def ping_sweep(network: ipaddress.IPv4Network) -> list[str]:
    """Ping all hosts in the network concurrently.

    Launches parallel ping tasks for every host address in the given
    network, throttled by a semaphore to avoid resource exhaustion.

    Args:
        network: The IPv4 network whose hosts should be pinged.

    Returns:
        A sorted list of IP address strings for hosts that responded.
    """
    if network.num_addresses <= 2:
        # A /31 or /32 has no usable host range
        return []

    sem = asyncio.Semaphore(PING_CONCURRENCY)
    hosts: list[str] = [str(ip) for ip in network.hosts()]
    tasks = [ping_host(sem, ip) for ip in hosts]
    results = await asyncio.gather(*tasks)
    return [ip for ip in results if ip is not None]


def get_arp_table() -> dict[str, str]:
    """Parse the system ARP table into an IP-to-MAC mapping.

    Runs ``arp -an`` (the ``-n`` flag avoids slow reverse-DNS lookups
    that can hang on large subnets) and extracts IP-to-MAC mappings.
    MAC addresses are normalized to two-digit colon-separated hex pairs.
    Broadcast and incomplete entries are excluded.

    Returns:
        A dictionary mapping IP address strings to normalized MAC
        address strings (lowercase, zero-padded).
    """
    try:
        result = subprocess.run(
            ["arp", "-an"],
            capture_output=True,
            text=True,
            timeout=ARP_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}

    entries: dict[str, str] = {}
    # macOS arp -an format: ? (192.0.2.1) at aa:bb:cc:dd:ee:ff on en0 ...
    pattern = re.compile(
        r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-fA-F:]+)"
    )
    for match in pattern.finditer(result.stdout):
        ip: str = match.group(1)
        mac_raw: str = match.group(2).lower()

        # macOS sometimes omits leading zeros (e.g. "0:17:88" instead
        # of "00:17:88").  Zero-pad each octet to two hex digits.
        parts: list[str] = mac_raw.split(":")
        mac: str = ":".join(p.zfill(2) for p in parts)

        # Exclude broadcast and malformed entries
        if mac == BROADCAST_MAC or mac.startswith("("):
            continue

        entries[ip] = mac

    return entries


def lookup_vendor(mac: str) -> Optional[str]:
    """Look up the hardware vendor from a MAC address OUI prefix.

    Checks whether the MAC address uses the locally-administered bit
    (indicating a randomized address, common on Apple devices) and, if
    not, looks up the first three octets in the built-in OUI map.

    Args:
        mac: A colon-separated MAC address string (e.g. ``"d0:73:d5:aa:bb:cc"``).

    Returns:
        The vendor name if found, ``"(randomized)"`` for locally
        administered addresses, or ``None`` if the prefix is unknown.
    """
    if not mac or len(mac) < OUI_PREFIX_LEN:
        return None

    # The locally-administered bit (bit 1 of the first octet) indicates
    # the MAC was generated locally rather than assigned by a vendor.
    try:
        first_byte: int = int(mac[:2], 16)
        if first_byte & LOCALLY_ADMINISTERED_BIT:
            return "(randomized)"
    except ValueError:
        # Non-hex characters in the first octet — cannot determine
        return None

    prefix: str = mac[:OUI_PREFIX_LEN].lower()
    return OUI_MAP.get(prefix, None)


def reverse_dns(ip: str) -> str:
    """Resolve an IP address to a hostname via reverse DNS.

    Common local suffixes such as ``.local`` and ``.lan`` are stripped
    from the result for brevity.

    Args:
        ip: The IPv4 address to resolve as a dotted-quad string.

    Returns:
        The resolved hostname with local suffixes removed, or an empty
        string if resolution failed.
    """
    if not ip:
        return ""

    try:
        host: str
        host, _, _ = socket.gethostbyaddr(ip)

        # Strip common local-network suffixes for cleaner display
        for suffix in LOCAL_DNS_SUFFIXES:
            if host.endswith(suffix):
                host = host[: -len(suffix)]
                break
        return host
    except (socket.herror, socket.gaierror, OSError):
        return ""


def trunc(s: str, w: int) -> str:
    """Truncate a string to fit within a given column width.

    If the string exceeds the width, it is trimmed and a trailing tilde
    is appended to indicate truncation.

    Args:
        s: The string to truncate.
        w: The maximum display width (must be at least 1).

    Returns:
        The original string if it fits, or a truncated version ending
        with ``"~"``.

    Raises:
        ValueError: If *w* is less than 1.
    """
    if w < 1:
        raise ValueError(f"Column width must be >= 1, got {w}")

    s = str(s)
    if len(s) > w:
        # Leave room for the truncation indicator
        return s[: w - 1] + TRUNC_CHAR
    return s


def main() -> None:
    """Run the LAN scan and display discovered devices in a table.

    Detects the local network, performs a concurrent ping sweep to
    populate the ARP table, resolves hostnames and vendor names for
    each discovered device, then prints a formatted table sorted by
    IP address.  Exits with status 1 if the local network cannot be
    determined.
    """
    network: Optional[ipaddress.IPv4Network] = get_local_network()
    if network is None:
        print("Could not determine local network.", file=sys.stderr)
        sys.exit(1)

    # Exclude network and broadcast addresses from the count
    num_hosts: int = network.num_addresses - 2
    print(f"Network: {network} ({num_hosts} hosts)")
    print("Pinging...", flush=True)

    alive: list[str] = asyncio.run(ping_sweep(network))
    print(f"  {len(alive)} host(s) responded to ping.", flush=True)

    print("Reading ARP table...", flush=True)
    arp: dict[str, str] = get_arp_table()

    # Defensively include hosts that responded to ping but may not yet
    # appear in the ARP cache (unlikely but possible with fast expiry).
    for ip in alive:
        if ip not in arp:
            arp[ip] = UNKNOWN_MAC

    if not arp:
        print("No devices found in ARP table.")
        sys.exit(0)

    print(f"Resolving hostnames for {len(arp)} device(s)...\n", flush=True)

    rows: list[dict[str, str]] = []
    for ip, mac in arp.items():
        # Only include devices that belong to our detected subnet
        if ipaddress.IPv4Address(ip) not in network:
            continue

        vendor: str = lookup_vendor(mac) or ""
        hostname: str = reverse_dns(ip)

        rows.append({
            "ip": ip,
            "mac": mac,
            "vendor": vendor,
            "hostname": hostname,
        })

    # Sort by numeric IP for natural ordering
    rows.sort(key=lambda r: tuple(int(p) for p in r["ip"].split(".")))

    if not rows:
        print("No devices found on the local network.")
        sys.exit(0)

    # -----------------------------------------------------------------------
    # Build and print the formatted table
    # -----------------------------------------------------------------------

    # Column definitions: (header, dict key, minimum width)
    cols: list[tuple[str, str, int]] = [
        ("IP Address",  "ip",       MIN_WIDTH_IP),
        ("MAC Address", "mac",      MIN_WIDTH_MAC),
        ("Vendor",      "vendor",   MIN_WIDTH_VENDOR),
        ("Hostname",    "hostname", MIN_WIDTH_HOSTNAME),
    ]

    # Compute natural column widths (widest value or header, but not
    # narrower than the declared minimum).
    widths: list[int] = []
    for header, key, min_w in cols:
        w: int = max(
            min_w,
            len(header),
            max((len(str(r[key])) for r in rows), default=0),
        )
        widths.append(w)

    # Shrink columns if the total width exceeds the target terminal width.
    sep_len: int = len(COL_SEP)
    total_sep: int = (len(cols) - 1) * sep_len
    available: int = MAX_COL_WIDTH - total_sep
    total_w: int = sum(widths)

    if total_w > available:
        excess: int = total_w - available
        # Build a list of (column index, current width) for columns that
        # have room to shrink (i.e. wider than their minimum).
        shrinkable: list[tuple[int, int]] = [
            (i, widths[i])
            for i in range(len(widths))
            if widths[i] > cols[i][COL_MIN]
        ]
        # Shrink widest columns first to distribute the cut fairly
        shrinkable.sort(key=lambda x: -x[1])
        for i, w in shrinkable:
            cut: int = min(excess, w - cols[i][COL_MIN])
            widths[i] -= cut
            excess -= cut
            if excess <= 0:
                break

    # Print header row
    header_line: str = COL_SEP.join(
        trunc(cols[i][COL_HDR], widths[i]).ljust(widths[i])
        for i in range(len(cols))
    )
    print(header_line)

    # Print separator row
    print(COL_SEP.join("-" * widths[i] for i in range(len(cols))))

    # Print data rows and count devices with known vendors
    known: int = 0
    for r in rows:
        line: str = COL_SEP.join(
            trunc(r[cols[i][COL_KEY]], widths[i]).ljust(widths[i])
            for i in range(len(cols))
        )
        print(line)
        if r["vendor"]:
            known += 1

    print(f"\n{len(rows)} device(s) found ({known} with known vendor).")


if __name__ == "__main__":
    main()
