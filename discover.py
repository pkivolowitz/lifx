#!/usr/bin/env python3
"""Discover LIFX devices on the local LAN and print detailed info.

This module broadcasts LIFX GetService messages over UDP, collects
responses from devices on the network, queries each device for its
label, group, product, firmware, and light state, and prints a
formatted table of results sorted by group and label.

Usage::

    python discover.py [timeout_seconds]

The optional ``timeout_seconds`` argument controls how long to listen
for discovery responses (default 3.0 seconds).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import socket
import struct
import sys
import time
import random
from typing import Optional

# ---------------------------------------------------------------------------
# Network constants
# ---------------------------------------------------------------------------

LIFX_PORT: int = 56700
"""Standard LIFX LAN protocol UDP port."""

BROADCAST: str = "255.255.255.255"
"""Limited broadcast address — works on any subnet."""

# Source ID is a random 32-bit value used to match responses to our requests.
# Range starts at 2 to avoid reserved values 0 and 1.
SOURCE_ID_MIN: int = 2
SOURCE_ID_MAX: int = (1 << 32) - 1
SOURCE_ID: int = random.randint(SOURCE_ID_MIN, SOURCE_ID_MAX)

# ---------------------------------------------------------------------------
# LIFX protocol header constants
# ---------------------------------------------------------------------------

PROTOCOL: int = 1024
"""LIFX protocol number, always 1024."""

ADDRESSABLE: int = 1 << 12
"""Bit flag indicating the message includes a target address."""

TAGGED: int = 1 << 13
"""Bit flag indicating a broadcast (tagged) message."""

HEADER_SIZE: int = 36
"""Total size in bytes of the LIFX message header (frame + frame address
+ protocol header), excluding the payload."""

# ---------------------------------------------------------------------------
# LIFX message type identifiers
# ---------------------------------------------------------------------------

MSG_GET_SERVICE: int = 2
MSG_STATE_SERVICE: int = 3
MSG_GET_LABEL: int = 23
MSG_STATE_LABEL: int = 25
MSG_GET_VERSION: int = 32
MSG_STATE_VERSION: int = 33
MSG_GET_GROUP: int = 51
MSG_STATE_GROUP: int = 53
MSG_LIGHT_GET: int = 101
MSG_LIGHT_STATE: int = 107
MSG_GET_HOST_FIRMWARE: int = 14
MSG_STATE_HOST_FIRMWARE: int = 15

# ---------------------------------------------------------------------------
# Payload parsing constants
# ---------------------------------------------------------------------------

LABEL_SIZE: int = 32
"""Size in bytes of a device label field in the StateLabel payload."""

GROUP_PAYLOAD_MIN: int = 48
"""Minimum payload size for a valid StateGroup response
(16-byte UUID + 32-byte label)."""

GROUP_LABEL_OFFSET: int = 16
"""Byte offset where the group label begins within the StateGroup payload
(after the 16-byte group UUID)."""

VERSION_PAYLOAD_MIN: int = 12
"""Minimum payload size for a valid StateVersion response
(vendor + product + version, each 4 bytes)."""

FW_PAYLOAD_MIN: int = 20
"""Minimum payload size for a valid StateHostFirmware response
(8-byte build + 8-byte reserved + 4-byte version)."""

FW_VERSION_OFFSET: int = 16
"""Byte offset of the firmware version uint32 within the
StateHostFirmware payload (after build and reserved fields)."""

LIGHT_STATE_PAYLOAD_MIN: int = 20
"""Minimum payload size for a valid LightState response to extract
HSBK (8 bytes) plus power (at offset 10)."""

LIGHT_STATE_POWER_OFFSET: int = 10
"""Byte offset of the power field within the LightState payload."""

# ---------------------------------------------------------------------------
# HSBK and colour constants
# ---------------------------------------------------------------------------

HSBK_MAX: int = 65535
"""Maximum value for any HSBK component (16-bit unsigned)."""

LOW_SAT_THRESHOLD: int = 3000
"""Saturation values below this are treated as white / kelvin mode rather
than a chromatic colour."""

FW_MAJOR_SHIFT: int = 16
"""Bit shift to extract the major firmware version from the packed uint32."""

FW_MINOR_MASK: int = 0xFFFF
"""Bitmask to extract the minor firmware version from the packed uint32."""

# ---------------------------------------------------------------------------
# Hue-to-colour-name degree boundaries
# ---------------------------------------------------------------------------

HUE_DEG_RED_UPPER: float = 15.0
HUE_DEG_ORANGE_UPPER: float = 45.0
HUE_DEG_YELLOW_UPPER: float = 70.0
HUE_DEG_GREEN_UPPER: float = 150.0
HUE_DEG_CYAN_UPPER: float = 195.0
HUE_DEG_BLUE_UPPER: float = 260.0
HUE_DEG_PURPLE_UPPER: float = 290.0
HUE_DEG_PINK_UPPER: float = 340.0
HUE_DEG_MAX: float = 360.0

# ---------------------------------------------------------------------------
# Display / table formatting constants
# ---------------------------------------------------------------------------

MAX_COL_WIDTH: int = 120
"""Maximum total character width for the output table."""

COLUMN_SEPARATOR: str = "  "
"""String placed between table columns."""

COLUMN_SEPARATOR_WIDTH: int = len(COLUMN_SEPARATOR)
"""Character width of the column separator."""

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------

BROADCAST_INTERVAL: float = 0.5
"""Seconds between successive broadcast sends during discovery."""

SOCKET_TIMEOUT: float = 0.3
"""Default per-recv timeout in seconds for sockets."""

DEFAULT_DISCOVERY_TIMEOUT: float = 3.0
"""Default total discovery timeout in seconds."""

QUERY_TIMEOUT: float = 1.0
"""Default per-device query timeout in seconds."""

MAX_QUERY_RETRIES: int = 3
"""Maximum number of send attempts when querying a single device."""

RECV_BUFFER_SIZE: int = 1024
"""Buffer size in bytes for UDP recvfrom calls."""

# ---------------------------------------------------------------------------
# MAC address constants
# ---------------------------------------------------------------------------

MAC_BYTES_LEN: int = 6
"""Number of bytes in a MAC address."""

TARGET_SIZE: int = 8
"""Size in bytes of the LIFX target field."""

NULL_TARGET: bytes = b'\x00' * TARGET_SIZE
"""All-zeros target used for broadcast messages."""

NULL_MAC: str = "00:00:00:00:00:00"
"""MAC string representing an invalid / broadcast device."""

# ---------------------------------------------------------------------------
# Frame address constants
# ---------------------------------------------------------------------------

FRAME_ADDR_RESERVED_SIZE: int = 6
"""Number of reserved zero bytes in the frame address section."""

ACK_RES_RESPONSE: int = 0x01
"""ack_res byte value requesting a response."""

ACK_RES_NONE: int = 0x00
"""ack_res byte value requesting no response."""

# ---------------------------------------------------------------------------
# Product map — maps LIFX product IDs to human-readable names
# ---------------------------------------------------------------------------

PRODUCT_MAP: dict[int, str] = {
    1:   "Original 1000",
    3:   "Color 650",
    10:  "White 800 LV",
    11:  "White 800 HV",
    15:  "Color 1000",
    18:  "White 900 BR30 LV",
    20:  "Color 1000 BR30",
    22:  "Color 1000",
    27:  "A19",
    28:  "BR30",
    29:  "A19 Night Vision",
    30:  "BR30 Night Vision",
    31:  "Z",
    32:  "Z",
    36:  "Downlight",
    37:  "Downlight",
    38:  "Beam",
    43:  "A19",
    44:  "BR30",
    45:  "A19 Night Vision",
    46:  "BR30 Night Vision",
    49:  "Mini Color",
    50:  "Mini WW",
    51:  "Mini White",
    52:  "GU10",
    53:  "GU10",
    55:  "Tile",
    57:  "Candle",
    59:  "Mini Color",
    60:  "Mini WW",
    61:  "Mini White",
    62:  "A19",
    63:  "BR30",
    64:  "A19 Night Vision",
    65:  "BR30 Night Vision",
    68:  "Candle",
    70:  "Switch",
    71:  "Switch",
    81:  "Candle WW",
    82:  "Filament Clear",
    85:  "Filament Amber",
    87:  "Mini White",
    88:  "Mini White",
    89:  "Switch",
    90:  "Clean",
    91:  "Color A19",
    92:  "Color BR30",
    93:  "A19",
    94:  "BR30",
    96:  "Candle WW",
    97:  "A19",
    98:  "BR30",
    99:  "Clean",
    100: "Filament Clear",
    101: "Filament Amber",
    109: "A19 Night Vision",
    110: "BR30 Night Vision",
    111: "A19 NV Intl",
    112: "BR30 NV Intl",
    113: "Mini WW US",
    114: "Mini WW Intl",
    115: "Mini White US",
    116: "Mini White Intl",
    117: "GU10",
    118: "GU10",
    119: "Color A19",
    120: "Color BR30",
    123: "String Light",
    124: "String Light",
    125: "Neon",
}


# ===================================================================
# Core protocol functions
# ===================================================================


def build_message(
    tagged: bool,
    msg_type: int,
    source: int,
    target: bytes = NULL_TARGET,
    res: bool = True,
    payload: bytes = b'',
) -> bytes:
    """Build a complete LIFX LAN protocol message.

    Constructs a binary message conforming to the LIFX LAN protocol,
    including the frame, frame address, protocol header, and payload
    sections.

    Args:
        tagged: Whether to set the tagged flag, indicating a broadcast
            message destined for all devices.
        msg_type: The LIFX message type identifier (e.g.
            ``MSG_GET_SERVICE``).
        source: A unique 32-bit source identifier for matching
            responses to requests.
        target: The 8-byte target device address.  Defaults to
            ``NULL_TARGET`` (all zeros) for broadcast.
        res: Whether to request a response from the target device.
        payload: The raw payload bytes for the message body.

    Returns:
        The fully assembled LIFX protocol message as ``bytes``.

    Raises:
        ValueError: If *target* is not exactly ``TARGET_SIZE`` bytes.
    """
    if len(target) != TARGET_SIZE:
        raise ValueError(
            f"target must be exactly {TARGET_SIZE} bytes, got {len(target)}"
        )

    size = HEADER_SIZE + len(payload)

    # Combine protocol number with addressable bit, and optionally tagged
    flags = PROTOCOL | ADDRESSABLE
    if tagged:
        flags |= TAGGED
    frame = struct.pack("<HHI", size, flags, source)

    # Frame address: target + 6 reserved bytes + ack/res flag + sequence
    reserved = b'\x00' * FRAME_ADDR_RESERVED_SIZE
    ack_res = ACK_RES_RESPONSE if res else ACK_RES_NONE
    seq = 0  # Sequence number; unused for simple queries
    frame_addr = target + reserved + struct.pack("<BB", ack_res, seq)

    # Protocol header: 8-byte timestamp (zero) + msg type + 2 reserved bytes
    proto_header = struct.pack("<QHH", 0, msg_type, 0)

    return frame + frame_addr + proto_header + payload


def parse_message(data: bytes) -> Optional[dict]:
    """Parse a raw LIFX protocol message into its constituent parts.

    Extracts the header fields and payload from a raw LIFX protocol
    message received over UDP.

    Args:
        data: The raw bytes received from a LIFX device.

    Returns:
        A dictionary with keys ``source``, ``target``, ``type``, and
        ``payload``, or ``None`` if *data* is too short to contain a
        valid header.
    """
    if len(data) < HEADER_SIZE:
        return None

    size = struct.unpack_from("<H", data, 0)[0]
    source = struct.unpack_from("<I", data, 4)[0]
    # Target occupies bytes 8..15 (8 bytes)
    target = data[8:8 + TARGET_SIZE]
    # Message type is at byte offset 32
    msg_type = struct.unpack_from("<H", data, 32)[0]
    # Payload starts after the header; clamp to declared size
    payload = data[HEADER_SIZE:size] if size <= len(data) else data[HEADER_SIZE:]
    return {
        "source": source,
        "target": target,
        "type": msg_type,
        "payload": payload,
    }


def mac_bytes_to_str(mac_bytes: bytes) -> str:
    """Convert a MAC address from raw bytes to a colon-separated hex string.

    Args:
        mac_bytes: At least ``MAC_BYTES_LEN`` bytes representing the
            MAC address.

    Returns:
        A string in the form ``"aa:bb:cc:dd:ee:ff"``.

    Raises:
        ValueError: If *mac_bytes* contains fewer than
            ``MAC_BYTES_LEN`` bytes.
    """
    if len(mac_bytes) < MAC_BYTES_LEN:
        raise ValueError(
            f"mac_bytes must be at least {MAC_BYTES_LEN} bytes, "
            f"got {len(mac_bytes)}"
        )
    return ":".join(f"{b:02x}" for b in mac_bytes[:MAC_BYTES_LEN])


# ===================================================================
# Socket and network helpers
# ===================================================================


def create_socket(timeout: float = SOCKET_TIMEOUT) -> socket.socket:
    """Create and configure a UDP broadcast socket for LIFX communication.

    The socket is bound to an ephemeral port and configured with
    ``SO_BROADCAST`` and ``SO_REUSEADDR`` options so it can send
    and receive LIFX broadcast traffic.

    Args:
        timeout: The receive timeout in seconds.  Must be positive.

    Returns:
        A configured :class:`socket.socket` ready for sending and
        receiving LIFX messages.

    Raises:
        ValueError: If *timeout* is not positive.
    """
    if timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    # Bind to all interfaces on an ephemeral port
    sock.bind(("", 0))
    return sock


def discover(timeout: float = DEFAULT_DISCOVERY_TIMEOUT) -> dict[str, dict]:
    """Broadcast GetService and collect responding LIFX devices.

    Sends periodic broadcast discovery messages at
    ``BROADCAST_INTERVAL`` intervals and listens for StateService
    responses from LIFX devices on the local network.

    Args:
        timeout: Total time in seconds to spend discovering devices.
            Must be positive.

    Returns:
        A dictionary mapping MAC address strings to device info dicts,
        each containing ``ip``, ``mac``, and ``target`` keys.

    Raises:
        ValueError: If *timeout* is not positive.
    """
    if timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout}")

    sock = create_socket(timeout=SOCKET_TIMEOUT)
    msg = build_message(
        tagged=True, msg_type=MSG_GET_SERVICE, source=SOURCE_ID
    )

    devices: dict[str, dict] = {}
    deadline = time.time() + timeout
    sends = 0
    # next_send is initialised on first iteration (when sends == 0)
    next_send = 0.0

    while time.time() < deadline:
        # Send a broadcast every BROADCAST_INTERVAL seconds
        if sends == 0 or time.time() >= next_send:
            sock.sendto(msg, (BROADCAST, LIFX_PORT))
            sends += 1
            next_send = time.time() + BROADCAST_INTERVAL

        try:
            data, (ip, _port) = sock.recvfrom(RECV_BUFFER_SIZE)
            msg_parsed = parse_message(data)
            if msg_parsed and msg_parsed["type"] == MSG_STATE_SERVICE:
                mac = mac_bytes_to_str(msg_parsed["target"])
                # Ignore null MACs and duplicates
                if mac not in devices and mac != NULL_MAC:
                    devices[mac] = {
                        "ip": ip,
                        "mac": mac,
                        "target": msg_parsed["target"],
                    }
        except socket.timeout:
            continue

    sock.close()
    return devices


def query_device(
    device: dict,
    msg_type: int,
    resp_type: int,
    timeout: float = QUERY_TIMEOUT,
) -> Optional[bytes]:
    """Send a Get message to a single device and return its response payload.

    Retries the request up to ``MAX_QUERY_RETRIES`` times within the
    given timeout period.

    Args:
        device: A device info dict containing ``ip`` and ``target``
            keys.
        msg_type: The LIFX message type to send.
        resp_type: The expected response message type.
        timeout: Maximum time in seconds to wait for a response.

    Returns:
        The raw payload bytes from the response, or ``None`` if no
        valid response was received within the timeout.
    """
    sock = create_socket(timeout=SOCKET_TIMEOUT)
    msg = build_message(
        tagged=False,
        msg_type=msg_type,
        source=SOURCE_ID,
        target=device["target"],
    )

    deadline = time.time() + timeout
    sends = 0
    while time.time() < deadline:
        if sends < MAX_QUERY_RETRIES:
            sock.sendto(msg, (device["ip"], LIFX_PORT))
            sends += 1

        try:
            data, _ = sock.recvfrom(RECV_BUFFER_SIZE)
            msg_parsed = parse_message(data)
            if msg_parsed and msg_parsed["type"] == resp_type:
                sock.close()
                return msg_parsed["payload"]
        except socket.timeout:
            continue

    sock.close()
    return None


# ===================================================================
# Device attribute query helpers
# ===================================================================


def get_label(device: dict) -> str:
    """Query a device for its user-assigned label.

    Sends a GetLabel message and parses the StateLabel response to
    extract the null-terminated UTF-8 label string.

    Args:
        device: A device info dict containing ``ip`` and ``target``
            keys.

    Returns:
        The device label as a string, or ``"?"`` if the query failed
        or the payload was too short.
    """
    payload = query_device(device, MSG_GET_LABEL, MSG_STATE_LABEL)
    if payload and len(payload) >= LABEL_SIZE:
        # Label is a 32-byte null-terminated UTF-8 string
        return payload[:LABEL_SIZE].split(b'\x00', 1)[0].decode(
            "utf-8", errors="replace"
        )
    return "?"


def get_group(device: dict) -> str:
    """Query a device for its group name.

    Sends a GetGroup message and parses the StateGroup response.
    The group payload consists of a 16-byte UUID followed by a
    32-byte null-terminated label and an 8-byte updated_at timestamp.

    Args:
        device: A device info dict containing ``ip`` and ``target``
            keys.

    Returns:
        The group label as a string, or ``"?"`` if the query failed
        or the payload was too short.
    """
    payload = query_device(device, MSG_GET_GROUP, MSG_STATE_GROUP)
    if payload and len(payload) >= GROUP_PAYLOAD_MIN:
        # Skip the 16-byte group UUID to reach the 32-byte label
        label_end = GROUP_LABEL_OFFSET + LABEL_SIZE
        return payload[GROUP_LABEL_OFFSET:label_end].split(
            b'\x00', 1
        )[0].decode("utf-8", errors="replace")
    return "?"


def get_version(device: dict) -> tuple[Optional[int], Optional[int]]:
    """Query a device for its hardware vendor and product identifiers.

    Sends a GetVersion message and unpacks the vendor and product
    fields from the StateVersion response.

    Args:
        device: A device info dict containing ``ip`` and ``target``
            keys.

    Returns:
        A tuple of ``(vendor, product)`` integers, or
        ``(None, None)`` if the query failed or the payload was too
        short.
    """
    payload = query_device(device, MSG_GET_VERSION, MSG_STATE_VERSION)
    if payload and len(payload) >= VERSION_PAYLOAD_MIN:
        # StateVersion: vendor (4B) + product (4B) + version (4B)
        vendor, product, _version = struct.unpack_from("<III", payload, 0)
        return vendor, product
    return None, None


def get_host_firmware(device: dict) -> str:
    """Query a device for its firmware version string.

    Sends a GetHostFirmware message and extracts the major/minor
    version from the packed 32-bit version field in the
    StateHostFirmware response.

    Args:
        device: A device info dict containing ``ip`` and ``target``
            keys.

    Returns:
        A firmware version string in ``"major.minor"`` format, or
        ``"?"`` if the query failed or the payload was too short.
    """
    payload = query_device(
        device, MSG_GET_HOST_FIRMWARE, MSG_STATE_HOST_FIRMWARE
    )
    if payload and len(payload) >= FW_PAYLOAD_MIN:
        # Layout: build (8B) + reserved (8B) + version (4B)
        _build, _reserved, version = struct.unpack_from("<QQI", payload, 0)
        # Major version is in the upper 16 bits, minor in the lower 16
        major = version >> FW_MAJOR_SHIFT
        minor = version & FW_MINOR_MASK
        return f"{major}.{minor}"
    return "?"


def get_light_state(
    device: dict,
) -> Optional[tuple[int, int, int, int, int]]:
    """Query a device for its current light state (HSBK + power).

    Sends a LightGet message and unpacks the hue, saturation,
    brightness, kelvin, and power fields from the LightState response.

    Args:
        device: A device info dict containing ``ip`` and ``target``
            keys.

    Returns:
        A tuple of ``(hue, saturation, brightness, kelvin, power)``
        as 16-bit unsigned integers, or ``None`` if the query failed
        or the payload was too short.
    """
    payload = query_device(device, MSG_LIGHT_GET, MSG_LIGHT_STATE)
    if payload and len(payload) >= LIGHT_STATE_PAYLOAD_MIN:
        # HSBK occupies the first 8 bytes (4 x uint16)
        hue, sat, bri, kelvin = struct.unpack_from("<HHHH", payload, 0)
        # Power is a uint16 at a fixed offset after HSBK + reserved bytes
        power = struct.unpack_from(
            "<H", payload, LIGHT_STATE_POWER_OFFSET
        )[0]
        return hue, sat, bri, kelvin, power
    return None


# ===================================================================
# Display formatting helpers
# ===================================================================


def hue_to_name(hue_16: int) -> str:
    """Convert a LIFX 16-bit hue value to an approximate colour name.

    Maps the full 0--65535 hue range to one of eight named colours
    based on the equivalent degree position on the colour wheel.

    Args:
        hue_16: A 16-bit unsigned hue value (0 to ``HSBK_MAX``)
            from a LIFX device.

    Returns:
        A human-readable colour name such as ``"Red"``, ``"Green"``,
        or ``"Blue"``.
    """
    # Convert 16-bit hue to degrees (0..360)
    deg = hue_16 * HUE_DEG_MAX / HSBK_MAX
    if deg < HUE_DEG_RED_UPPER:
        return "Red"
    if deg < HUE_DEG_ORANGE_UPPER:
        return "Orange"
    if deg < HUE_DEG_YELLOW_UPPER:
        return "Yellow"
    if deg < HUE_DEG_GREEN_UPPER:
        return "Green"
    if deg < HUE_DEG_CYAN_UPPER:
        return "Cyan"
    if deg < HUE_DEG_BLUE_UPPER:
        return "Blue"
    if deg < HUE_DEG_PURPLE_UPPER:
        return "Purple"
    if deg < HUE_DEG_PINK_UPPER:
        return "Pink"
    return "Red"


def format_power(power: int) -> str:
    """Format a LIFX power value as a human-readable on/off string.

    Args:
        power: A 16-bit unsigned power value (0 means off, any
            nonzero value means on).

    Returns:
        ``"ON"`` if the device is powered on, ``"off"`` otherwise.
    """
    return "ON" if power > 0 else "off"


def format_brightness(bri_16: int) -> str:
    """Format a LIFX 16-bit brightness value as a percentage string.

    Args:
        bri_16: A 16-bit unsigned brightness value (0 to
            ``HSBK_MAX``).

    Returns:
        A percentage string such as ``"75%"``, computed via integer
        division.
    """
    return f"{bri_16 * 100 // HSBK_MAX}%"


def truncate(s: str, w: int) -> str:
    """Truncate a string to fit within a given column width.

    If the string exceeds the width, it is trimmed and a trailing
    tilde (``~``) is appended to indicate truncation.

    Args:
        s: The string to truncate.
        w: The maximum display width.  Must be at least 1.

    Returns:
        The original string if it fits, or a truncated version ending
        with ``"~"``.
    """
    s = str(s)
    if w < 1:
        return ""
    # When width is 1, the only option is the tilde itself
    return s[:w - 1] + "~" if len(s) > w else s


def main() -> None:
    """Run the LIFX device discovery and display results in a table.

    Accepts an optional command-line argument specifying the discovery
    timeout in seconds (default ``DEFAULT_DISCOVERY_TIMEOUT``).
    Discovers devices via UDP broadcast, queries each for its label,
    group, product, firmware, and light state, then prints a formatted
    table sorted by group and label.
    """
    timeout = DEFAULT_DISCOVERY_TIMEOUT
    if len(sys.argv) > 1:
        try:
            timeout = float(sys.argv[1])
            if timeout <= 0:
                raise ValueError("timeout must be positive")
        except ValueError:
            print(
                f"Usage: {sys.argv[0]} [timeout_seconds]",
                file=sys.stderr,
            )
            sys.exit(1)

    print("Scanning for LIFX devices...", flush=True)
    devices = discover(timeout)

    if not devices:
        print("No LIFX devices found.")
        sys.exit(0)

    print(
        f"Found {len(devices)} device(s). Querying details...\n",
        flush=True,
    )

    rows: list[dict[str, str]] = []
    for _mac, dev in devices.items():
        label = get_label(dev)
        group = get_group(dev)
        vendor, product = get_version(dev)
        firmware = get_host_firmware(dev)
        # Look up product name; fall back to "pid:<id>" if unknown
        product_name = PRODUCT_MAP.get(product, f"pid:{product}")
        light = get_light_state(dev)

        if light:
            hue, sat, bri, kelvin, power = light
            pwr = format_power(power)
            bright = format_brightness(bri)
            # Low saturation means the bulb is in white/kelvin mode
            if sat < LOW_SAT_THRESHOLD:
                color = f"{kelvin}K"
            else:
                color = hue_to_name(hue)
        else:
            pwr = "?"
            bright = "?"
            color = "?"

        rows.append({
            "label": label,
            "product": product_name,
            "mac": dev["mac"],
            "ip": dev["ip"],
            "group": group,
            "pwr": pwr,
            "bright": bright,
            "color": color,
            "fw": firmware,
        })

    # Sort by group first, then label within each group
    rows.sort(key=lambda r: (r["group"], r["label"]))

    # Column definitions: (header, dict key, minimum width)
    cols: list[tuple[str, str, int]] = [
        ("Label",       "label",   12),
        ("Product",     "product", 14),
        ("Group",       "group",    8),
        ("MAC Address", "mac",     17),
        ("IP Address",  "ip",      13),
        ("Pwr",         "pwr",      3),
        ("Bri",         "bright",   4),
        ("Color",       "color",    6),
        ("FW",          "fw",       5),
    ]

    # Compute column widths: max of minimum, header length, and data length
    widths: list[int] = []
    for header, key, min_w in cols:
        data_max = max((len(str(r[key])) for r in rows), default=0)
        w = max(min_w, len(header), data_max)
        widths.append(w)

    # Shrink columns if total width exceeds MAX_COL_WIDTH
    total_sep = (len(cols) - 1) * COLUMN_SEPARATOR_WIDTH
    available = MAX_COL_WIDTH - total_sep
    total_w = sum(widths)
    if total_w > available:
        # Shrink the widest columns proportionally, but never below minimum
        excess = total_w - available
        shrinkable = [
            (i, widths[i])
            for i in range(len(widths))
            if widths[i] > cols[i][2]
        ]
        # Shrink widest columns first
        shrinkable.sort(key=lambda x: -x[1])
        for i, w in shrinkable:
            cut = min(excess, w - cols[i][2])
            widths[i] -= cut
            excess -= cut
            if excess <= 0:
                break

    # Print header row
    header_line = COLUMN_SEPARATOR.join(
        truncate(cols[i][0], widths[i]).ljust(widths[i])
        for i in range(len(cols))
    )
    print(header_line)

    # Print separator line
    print(COLUMN_SEPARATOR.join("-" * widths[i] for i in range(len(cols))))

    # Print data rows
    for r in rows:
        line = COLUMN_SEPARATOR.join(
            truncate(r[cols[i][1]], widths[i]).ljust(widths[i])
            for i in range(len(cols))
        )
        print(line)

    print(f"\n{len(rows)} device(s) found.")


if __name__ == "__main__":
    main()
