"""LIFX LAN protocol transport layer.

Handles device discovery, persistent UDP sockets, and the extended multizone
protocol for string lights.  No effect logic lives here.

Typical usage::

    devices = discover_devices(timeout=3.0)
    dev = devices[0]
    dev.query_zone_count()
    dev.set_zones(colors)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.3"

import fcntl
import math
import random
import re
import socket
import struct
import time
from typing import Optional

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

LIFX_PORT: int = 56700

# Fallback broadcast address when auto-detection fails.
FALLBACK_BROADCAST: str = "255.255.255.255"

PROTOCOL: int = 1024
ADDRESSABLE: int = 1 << 12
TAGGED: int = 1 << 13

# Wire-format sizes
HEADER_SIZE: int = 36           # Every LIFX packet starts with a 36-byte header
MAC_TARGET_SIZE: int = 8        # Target field is 8 bytes (6 MAC + 2 padding)
MAC_DISPLAY_SIZE: int = 6       # Only 6 bytes are meaningful for display

# Source ID bounds — 0 and 1 are reserved by the protocol
SOURCE_ID_MIN: int = 2
SOURCE_ID_MAX: int = (1 << 32) - 1

# Socket defaults
SOCKET_TIMEOUT: float = 2.0    # Default recv timeout for device sockets (seconds)
DISCOVERY_INTERVAL: float = 0.5 # Seconds between broadcast discovery packets
DISCOVERY_RECV_TIMEOUT: float = 0.3  # Recv timeout during discovery (seconds)
DISCOVERY_WAKE_BURSTS: int = 3       # Rapid GetService packets before main loop
DISCOVERY_WAKE_DELAY: float = 0.1    # Seconds between wake burst packets
MAX_UDP_PAYLOAD: int = 1500     # Standard MTU-safe UDP receive buffer

# Label / group payload sizes (from LIFX protocol spec)
LABEL_FIELD_SIZE: int = 32
GROUP_PAYLOAD_MIN: int = 48
GROUP_LABEL_OFFSET: int = 16    # 16-byte UUID precedes the 32-byte label
VERSION_PAYLOAD_MIN: int = 12
ZONE_COUNT_PAYLOAD_MIN: int = 5
LIGHT_STATE_PAYLOAD_MIN: int = 20
LIGHT_STATE_POWER_OFFSET: int = 10  # Power field offset within LightState payload

# ---------------------------------------------------------------------------
# Message types
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
MSG_LIGHT_SET_COLOR: int = 102
MSG_LIGHT_STATE: int = 107
MSG_LIGHT_SET_POWER: int = 117
MSG_LIGHT_STATE_POWER: int = 118
MSG_SET_COLOR_ZONES: int = 501
MSG_GET_COLOR_ZONES: int = 502
MSG_STATE_ZONE: int = 503
MSG_STATE_MULTI_ZONE: int = 506
MSG_SET_EXTENDED_COLOR_ZONES: int = 510
MSG_GET_EXTENDED_COLOR_ZONES: int = 511
MSG_STATE_EXTENDED_COLOR_ZONES: int = 512

# Apply flags for extended multizone set
APPLY_NO: int = 0     # Stage colors, don't render yet
APPLY_YES: int = 1    # Stage and render atomically
APPLY_ONLY: int = 2   # Render previously staged colors

# HSBK wire format
HSBK_FMT: str = "<HHHH"
HSBK_SIZE: int = 8
ZONES_PER_PACKET: int = 82

# Power level constants
POWER_ON: int = 65535
POWER_OFF: int = 0

# HSBK value range limits
HSBK_MAX: int = 65535
KELVIN_MIN: int = 1500
KELVIN_MAX: int = 9000

# ---------------------------------------------------------------------------
# Product database
# ---------------------------------------------------------------------------

#: Product IDs that support the extended multizone protocol.
MULTIZONE_PRODUCTS: set[int] = {31, 32, 38, 123, 124, 125, 143, 144}

#: Product IDs for monochrome-only bulbs (brightness + kelvin, no hue/saturation).
MONOCHROME_PRODUCTS: set[int] = {
    10, 11, 18, 50, 51, 60, 61, 87, 88, 113, 114, 115, 116,
}

# Non-multizone devices are treated as a single zone.
SINGLE_ZONE_COUNT: int = 1

#: Mapping of LIFX product ID to human-readable name.
PRODUCT_MAP: dict[int, str] = {
    1: "Original 1000", 3: "Color 650", 10: "White 800 LV",
    11: "White 800 HV", 15: "Color 1000", 18: "White 900 BR30 LV",
    20: "Color 1000 BR30", 22: "Color 1000", 27: "A19", 28: "BR30",
    29: "A19 Night Vision", 30: "BR30 Night Vision", 31: "Z", 32: "Z",
    36: "Downlight", 37: "Downlight", 38: "Beam", 43: "A19", 44: "BR30",
    45: "A19 Night Vision", 46: "BR30 Night Vision", 49: "Mini Color",
    50: "Mini WW", 51: "Mini White", 52: "GU10", 53: "GU10", 55: "Tile",
    57: "Candle", 59: "Mini Color", 60: "Mini WW", 61: "Mini White",
    62: "A19", 63: "BR30", 64: "A19 Night Vision", 65: "BR30 Night Vision",
    68: "Candle", 70: "Switch", 71: "Switch", 81: "Candle WW",
    82: "Filament Clear", 85: "Filament Amber", 87: "Mini White",
    88: "Mini White", 89: "Switch", 90: "Clean", 91: "Color A19",
    92: "Color BR30", 93: "A19", 94: "BR30", 96: "Candle WW", 97: "A19",
    98: "BR30", 99: "Clean", 100: "Filament Clear", 101: "Filament Amber",
    109: "A19 Night Vision", 110: "BR30 Night Vision",
    111: "A19 NV Intl", 112: "BR30 NV Intl",
    113: "Mini WW US", 114: "Mini WW Intl", 115: "Mini White US",
    116: "Mini White Intl", 117: "GU10", 118: "GU10",
    119: "Color A19", 120: "Color BR30",
    123: "String Light", 124: "String Light", 125: "Neon",
    143: "String Light", 144: "String Light",
}

# Regex for basic IPv4 format validation
_IPV4_RE: re.Pattern[str] = re.compile(
    r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$"
)

# ioctl request codes for querying interface addresses (Linux).
SIOCGIFADDR: int = 0x8915
SIOCGIFNETMASK: int = 0x891B

# ioctl structure size for interface address requests.
IOCTL_STRUCT_SIZE: int = 256


def _get_broadcast_address() -> str:
    """Detect the subnet broadcast address from the default network interface.

    Works on both macOS and Linux.  On macOS, parses ``ifconfig`` output
    for the broadcast address.  On Linux, uses ioctl to read the IP and
    netmask from the interface that holds the default route, then computes
    the broadcast address as ``IP | ~netmask``.

    Returns:
        A dotted-quad broadcast address string (e.g. ``"10.0.3.255"``
        on a /22 or ``"10.0.0.255"`` on a /24).  Falls back to
        ``"255.255.255.255"`` if detection fails.
    """
    import platform
    import subprocess

    try:
        if platform.system() == "Darwin":
            # macOS: parse ifconfig for the broadcast address on the default route interface.
            route_out: str = subprocess.check_output(
                ["route", "-n", "get", "default"],
                stderr=subprocess.DEVNULL,
                text=True,
            )
            iface: Optional[str] = None
            for line in route_out.splitlines():
                parts = line.split(":")
                if len(parts) == 2 and parts[0].strip() == "interface":
                    iface = parts[1].strip()
                    break
            if not iface:
                return FALLBACK_BROADCAST

            ifconfig_out: str = subprocess.check_output(
                ["ifconfig", iface],
                stderr=subprocess.DEVNULL,
                text=True,
            )
            for line in ifconfig_out.splitlines():
                # Look for: "inet 10.0.0.38 netmask 0xfffffc00 broadcast 10.0.3.255"
                if "broadcast" in line:
                    tokens = line.split()
                    bcast_idx: int = tokens.index("broadcast")
                    return tokens[bcast_idx + 1]

            return FALLBACK_BROADCAST

        else:
            # Linux: use ioctl to compute broadcast from IP and netmask.
            route_out = subprocess.check_output(
                ["ip", "route", "show", "default"],
                stderr=subprocess.DEVNULL,
                text=True,
            )
            iface = None
            for token_idx, token in enumerate(route_out.split()):
                if token == "dev":
                    iface = route_out.split()[token_idx + 1]
                    break
            if not iface:
                return FALLBACK_BROADCAST

            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Get interface IP address.
            ip_bytes: bytes = fcntl.ioctl(
                s.fileno(), SIOCGIFADDR,
                struct.pack("256s", iface.encode("utf-8")[:15]),
            )[20:24]
            # Get interface netmask.
            mask_bytes: bytes = fcntl.ioctl(
                s.fileno(), SIOCGIFNETMASK,
                struct.pack("256s", iface.encode("utf-8")[:15]),
            )[20:24]
            s.close()

            ip_int: int = struct.unpack("!I", ip_bytes)[0]
            mask_int: int = struct.unpack("!I", mask_bytes)[0]
            bcast_int: int = ip_int | (~mask_int & 0xFFFFFFFF)
            return socket.inet_ntoa(struct.pack("!I", bcast_int))

    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return FALLBACK_BROADCAST


def _resolve_host(host: str) -> str:
    """Resolve a hostname or IPv4 address to a dotted-quad IPv4 string.

    Accepts either a raw IPv4 address (``"10.0.0.62"``) or a DNS
    hostname (``"string_lights"``).  Hostnames are resolved via
    :func:`socket.getaddrinfo` which consults ``/etc/hosts``, mDNS,
    and DNS in the platform's standard order.

    Args:
        host: An IPv4 address or hostname to resolve.

    Returns:
        The resolved IPv4 address as a dotted-quad string.

    Raises:
        ValueError: If *host* is empty, not a string, or cannot be
                    resolved to an IPv4 address.
    """
    if not isinstance(host, str) or not host:
        raise ValueError(f"Host must be a non-empty string, got {host!r}")

    # Fast path: if it's already a valid IPv4 address, validate and return.
    m = _IPV4_RE.match(host)
    if m:
        for octet_str in m.groups():
            if int(octet_str) > 255:
                raise ValueError(f"Invalid IPv4 address (octet > 255): {host!r}")
        return host

    # Slow path: resolve hostname to IPv4 via the system resolver.
    try:
        results = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
    except socket.gaierror as exc:
        raise ValueError(
            f"Cannot resolve hostname {host!r}: {exc}"
        ) from exc

    if not results:
        raise ValueError(f"No IPv4 address found for hostname {host!r}")

    # getaddrinfo returns (family, type, proto, canonname, (ip, port))
    resolved_ip: str = results[0][4][0]
    return resolved_ip


# ---------------------------------------------------------------------------
# Packet construction / parsing
# ---------------------------------------------------------------------------

def _build_header(
    msg_type: int,
    payload_size: int,
    source_id: int,
    target: bytes = b'\x00' * MAC_TARGET_SIZE,
    *,
    tagged: bool = False,
    ack: bool = False,
    res: bool = False,
) -> bytes:
    """Build a 36-byte LIFX protocol header.

    The LIFX header consists of three parts packed little-endian:

    - **Frame** (8 bytes): total size, protocol flags, source ID.
    - **Frame Address** (16 bytes): target MAC, reserved, ack/res flags.
    - **Protocol Header** (12 bytes): timestamp (zero), message type, reserved.

    Args:
        msg_type:     LIFX message type number.
        payload_size: Length of the payload that follows this header.
        source_id:    Unique source identifier for this session.
        target:       8-byte target MAC (all zeros for broadcast).
        tagged:       Whether this is a tagged (broadcast) message.
        ack:          Request an acknowledgement from the device.
        res:          Request a response from the device.

    Returns:
        A 36-byte ``bytes`` header ready to prepend to a payload.

    Raises:
        ValueError: If *payload_size* is negative or *source_id* is out of range.
    """
    if payload_size < 0:
        raise ValueError(f"payload_size must be >= 0, got {payload_size}")
    if not (SOURCE_ID_MIN <= source_id <= SOURCE_ID_MAX):
        raise ValueError(
            f"source_id must be in [{SOURCE_ID_MIN}, {SOURCE_ID_MAX}], "
            f"got {source_id}"
        )

    size = HEADER_SIZE + payload_size
    # Protocol field encodes protocol number plus addressable/tagged flags
    flags = PROTOCOL | ADDRESSABLE
    if tagged:
        flags |= TAGGED

    # Frame: total packet size (u16) + flags (u16) + source (u32)
    frame = struct.pack("<HHI", size, flags, source_id)

    # Frame Address: target (8 bytes) + reserved (6 bytes) + ack_res (u8) + seq (u8)
    reserved = b'\x00' * 6
    # Bit 1 = ack_required, bit 0 = res_required
    ack_res = (1 if ack else 0) << 1 | (1 if res else 0)
    frame_addr = target + reserved + struct.pack("<BB", ack_res, 0)

    # Protocol Header: timestamp (u64, always 0) + type (u16) + reserved (u16)
    proto_header = struct.pack("<QHH", 0, msg_type, 0)

    return frame + frame_addr + proto_header


def _parse_message(data: bytes) -> Optional[dict[str, object]]:
    """Parse a raw LIFX packet into its constituent fields.

    Args:
        data: Raw bytes received from the network.

    Returns:
        A dict with keys ``source`` (int), ``target`` (bytes), ``type`` (int),
        and ``payload`` (bytes), or ``None`` if *data* is too short to contain
        a valid LIFX header.
    """
    if len(data) < HEADER_SIZE:
        return None

    # Total packet size is in the first 2 bytes
    size = struct.unpack_from("<H", data, 0)[0]
    source = struct.unpack_from("<I", data, 4)[0]
    # Target MAC occupies bytes 8..16 of the header
    target = data[8:16]
    # Message type is at byte offset 32
    msg_type = struct.unpack_from("<H", data, 32)[0]
    # Payload starts right after the header; clamp to declared size
    payload = data[HEADER_SIZE:size] if size <= len(data) else data[HEADER_SIZE:]

    return {
        "source": source,
        "target": target,
        "type": msg_type,
        "payload": payload,
    }


def mac_bytes_to_str(mac_bytes: bytes) -> str:
    """Convert raw MAC bytes to a colon-separated hex string.

    Args:
        mac_bytes: At least 6 bytes of MAC address data.

    Returns:
        A string like ``"d0:73:d5:6a:cd:ba"``.

    Raises:
        ValueError: If *mac_bytes* has fewer than 6 bytes.
    """
    if len(mac_bytes) < MAC_DISPLAY_SIZE:
        raise ValueError(
            f"mac_bytes must be at least {MAC_DISPLAY_SIZE} bytes, "
            f"got {len(mac_bytes)}"
        )
    return ":".join(f"{b:02x}" for b in mac_bytes[:MAC_DISPLAY_SIZE])


# ---------------------------------------------------------------------------
# LifxDevice
# ---------------------------------------------------------------------------

class LifxDevice:
    """A single LIFX device with a persistent UDP socket.

    Supports both regular lights and multizone devices (string lights,
    beams, Z strips).  The persistent socket avoids the overhead of
    creating a new socket per command.

    Attributes:
        ip:           Device IP address on the LAN.
        mac:          8-byte MAC address (as received from the device).
        source_id:    Random 32-bit identifier for this session.
        label:        Human-readable device name (populated by :meth:`query_label`).
        group:        Device group name (populated by :meth:`query_group`).
        vendor:       LIFX vendor ID (populated by :meth:`query_version`).
        product:      LIFX product ID (populated by :meth:`query_version`).
        product_name: Friendly product name from :data:`PRODUCT_MAP`.
        zone_count:   Number of zones (populated by :meth:`query_zone_count`).
    """

    def __init__(
        self,
        ip: str,
        mac_bytes: Optional[bytes] = None,
        source_id: Optional[int] = None,
    ) -> None:
        """Create a device handle and open a persistent UDP socket.

        Args:
            ip:        Device IP address or hostname.  Hostnames are
                       resolved to IPv4 via the system resolver
                       (``/etc/hosts``, mDNS, DNS).
            mac_bytes: 8-byte MAC address (default: all zeros).
            source_id: Session identifier (default: random value in
                       [SOURCE_ID_MIN, SOURCE_ID_MAX]).

        Raises:
            ValueError: If *ip* cannot be resolved to a valid IPv4 address.
        """
        self.ip: str = _resolve_host(ip)
        self.mac: bytes = mac_bytes or b'\x00' * MAC_TARGET_SIZE
        self.source_id: int = (
            source_id
            if source_id is not None
            else random.randint(SOURCE_ID_MIN, SOURCE_ID_MAX)
        )

        # Persistent UDP socket — kept open for the device's lifetime to avoid
        # the overhead of socket creation on every command.
        self.sock: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM,
        )
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.settimeout(SOCKET_TIMEOUT)
        # Bind to any available port on all interfaces
        self.sock.bind(("", 0))

        # Cached device info (populated lazily by query methods)
        self.label: Optional[str] = None
        self.group: Optional[str] = None
        self.vendor: Optional[int] = None
        self.product: Optional[int] = None
        self.product_name: Optional[str] = None
        self.zone_count: Optional[int] = None

    # -- Properties ----------------------------------------------------------

    @property
    def mac_str(self) -> str:
        """Return the MAC address as a colon-separated hex string.

        Returns:
            A string like ``"d0:73:d5:6a:cd:ba"``.
        """
        return mac_bytes_to_str(self.mac)

    @property
    def is_multizone(self) -> Optional[bool]:
        """Whether this device supports the extended multizone protocol.

        Returns:
            ``True`` if the product ID is in :data:`MULTIZONE_PRODUCTS`,
            ``False`` if the product is known but not multizone, or ``None``
            if the product ID has not been queried yet.
        """
        if self.product is None:
            return None
        return self.product in MULTIZONE_PRODUCTS

    @property
    def is_polychrome(self) -> Optional[bool]:
        """Whether this device supports color (hue and saturation).

        Multizone devices are always polychrome.  Monochrome devices
        (product IDs in :data:`MONOCHROME_PRODUCTS`) support only
        brightness and color temperature.

        Returns:
            ``True`` if the device supports full HSBK color,
            ``False`` if it is monochrome (brightness + kelvin only),
            or ``None`` if the product ID has not been queried yet.
        """
        if self.product is None:
            return None
        return self.product not in MONOCHROME_PRODUCTS

    # -- Lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Close the persistent UDP socket.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        try:
            self.sock.close()
        except OSError:
            # Already closed or otherwise unavailable — nothing to do.
            pass

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Uses the device label if available, falling back to product name,
        then IP address.

        Returns:
            A string like ``<LifxDevice String 2 (10.0.0.62)>``.
        """
        name = self.label or self.product_name or self.ip
        return f"<LifxDevice {name} ({self.ip})>"

    # -- Low-level send / receive --------------------------------------------

    def _send(
        self,
        msg_type: int,
        payload: bytes = b'',
        *,
        tagged: bool = False,
        ack: bool = False,
        res: bool = False,
    ) -> None:
        """Send a LIFX message to this device.

        Constructs a full packet (header + payload) and transmits it via
        the persistent UDP socket.

        Args:
            msg_type: LIFX message type number.
            payload:  Raw payload bytes.
            tagged:   Whether to set the tagged flag.
            ack:      Request an acknowledgement.
            res:      Request a response.
        """
        header = _build_header(
            msg_type, len(payload), self.source_id,
            target=self.mac, tagged=tagged, ack=ack, res=res,
        )
        self.sock.sendto(header + payload, (self.ip, LIFX_PORT))

    def _send_and_recv(
        self,
        msg_type: int,
        resp_type: int,
        payload: bytes = b'',
        *,
        timeout: float = SOCKET_TIMEOUT,
        retries: int = 3,
    ) -> Optional[bytes]:
        """Send a message and wait for a specific response type.

        Retries the send up to *retries* times, listening for responses
        until the deadline expires.  On a successful match, updates
        ``self.mac`` and ``self.ip`` from the response (handles IP changes
        after DHCP renewal).

        Args:
            msg_type:  Outgoing LIFX message type.
            resp_type: Expected response message type.
            payload:   Raw payload bytes.
            timeout:   Total time to wait for a response (seconds).
            retries:   Number of send attempts before giving up.

        Returns:
            The response payload bytes, or ``None`` on timeout.
        """
        if retries < 1:
            raise ValueError(f"retries must be >= 1, got {retries}")
        if timeout <= 0:
            raise ValueError(f"timeout must be > 0, got {timeout}")

        deadline = time.time() + timeout
        for _attempt in range(retries):
            self._send(msg_type, payload, res=True)
            while time.time() < deadline:
                try:
                    data, (ip, _) = self.sock.recvfrom(MAX_UDP_PAYLOAD)
                    msg = _parse_message(data)
                    if msg and msg["type"] == resp_type:
                        # Update cached address — device may have changed IP
                        self.mac = msg["target"]  # type: ignore[assignment]
                        self.ip = ip
                        return msg["payload"]  # type: ignore[return-value]
                except socket.timeout:
                    break
        return None

    def fire_and_forget(self, msg_type: int, payload: bytes = b'') -> None:
        """Send a message without waiting for a response.

        Used for rapid animation frames where latency matters more than
        delivery guarantees.  The packet is sent exactly once with no
        ack/res flags.

        Args:
            msg_type: LIFX message type number.
            payload:  Raw payload bytes.
        """
        header = _build_header(
            msg_type, len(payload), self.source_id, target=self.mac,
        )
        self.sock.sendto(header + payload, (self.ip, LIFX_PORT))

    # -- Device info queries -------------------------------------------------

    def query_label(self) -> Optional[str]:
        """Query and cache the device label.

        Sends a ``GetLabel`` message and parses the 32-byte label field
        from the response.

        Returns:
            The device label string, or ``None`` on failure.
        """
        payload = self._send_and_recv(MSG_GET_LABEL, MSG_STATE_LABEL)
        if payload and len(payload) >= LABEL_FIELD_SIZE:
            # Label is a fixed 32-byte field, null-terminated
            self.label = payload[:LABEL_FIELD_SIZE].split(b'\x00', 1)[0].decode(
                "utf-8", errors="replace",
            )
        return self.label

    def query_group(self) -> Optional[str]:
        """Query and cache the device group name.

        The group payload contains a 16-byte UUID followed by a 32-byte
        label and an 8-byte updated-at timestamp.

        Returns:
            The group name string, or ``None`` on failure.
        """
        payload = self._send_and_recv(MSG_GET_GROUP, MSG_STATE_GROUP)
        if payload and len(payload) >= GROUP_PAYLOAD_MIN:
            # Skip 16-byte UUID, then read 32-byte label
            raw_label = payload[GROUP_LABEL_OFFSET:GROUP_LABEL_OFFSET + LABEL_FIELD_SIZE]
            self.group = raw_label.split(b'\x00', 1)[0].decode(
                "utf-8", errors="replace",
            )
        return self.group

    def query_version(self) -> tuple[Optional[int], Optional[int]]:
        """Query and cache the vendor and product IDs.

        Returns:
            A ``(vendor, product)`` tuple, or ``(None, None)`` on failure.
        """
        payload = self._send_and_recv(MSG_GET_VERSION, MSG_STATE_VERSION)
        if payload and len(payload) >= VERSION_PAYLOAD_MIN:
            # StateVersion: vendor (u32) + product (u32) + version (u32)
            self.vendor, self.product, _ = struct.unpack_from(
                "<III", payload, 0,
            )
            self.product_name = PRODUCT_MAP.get(
                self.product, f"Unknown({self.product})",
            )
        return self.vendor, self.product

    def query_zone_count(self) -> Optional[int]:
        """Query the extended color zones to get the total zone count.

        Sends a ``GetExtendedColorZones`` message.  The zone count is
        the first 16-bit field in the response payload.

        Returns:
            The number of zones, or ``None`` on failure.
        """
        payload = self._send_and_recv(
            MSG_GET_EXTENDED_COLOR_ZONES,
            MSG_STATE_EXTENDED_COLOR_ZONES,
            timeout=3.0,
        )
        if payload and len(payload) >= ZONE_COUNT_PAYLOAD_MIN:
            self.zone_count = struct.unpack_from("<H", payload, 0)[0]
        return self.zone_count

    def query_light_state(
        self,
    ) -> Optional[tuple[int, int, int, int, int]]:
        """Query the current light state.

        Returns:
            A ``(hue, saturation, brightness, kelvin, power)`` tuple,
            or ``None`` on failure.
        """
        payload = self._send_and_recv(MSG_LIGHT_GET, MSG_LIGHT_STATE)
        if payload and len(payload) >= LIGHT_STATE_PAYLOAD_MIN:
            hue, sat, bri, kelvin = struct.unpack_from("<HHHH", payload, 0)
            power = struct.unpack_from("<H", payload, LIGHT_STATE_POWER_OFFSET)[0]
            return hue, sat, bri, kelvin, power
        return None

    def query_all(self) -> "LifxDevice":
        """Populate all cached device info in one call.

        Queries version, label, group, and zone count.  Multizone
        devices query the actual zone count; single-bulb devices are
        set to :data:`SINGLE_ZONE_COUNT` (1).  Each sub-query is
        tolerant of timeouts; cached fields remain ``None`` for any
        query that fails.

        Returns:
            ``self``, for method chaining.
        """
        self.query_version()
        self.query_label()
        self.query_group()
        if self.is_multizone:
            self.query_zone_count()
        else:
            # Non-multizone devices are a single zone (one color for the bulb).
            self.zone_count = SINGLE_ZONE_COUNT
        return self

    # -- Zone control (extended multizone) -----------------------------------

    def set_zones(
        self,
        colors: list[tuple[int, int, int, int]],
        duration_ms: int = 0,
        *,
        rapid: bool = True,
    ) -> None:
        """Set all zones atomically using the extended multizone protocol.

        Colors are sent in chunks of up to :data:`ZONES_PER_PACKET` zones.
        All but the last chunk use ``APPLY_NO`` (stage only); the final
        chunk uses ``APPLY_YES`` to commit the entire frame atomically,
        preventing visible tearing.

        Args:
            colors:      List of ``(hue, sat, brightness, kelvin)`` tuples,
                         one per zone.
            duration_ms: Transition duration in milliseconds.
            rapid:       If ``True``, use fire-and-forget for speed.

        Raises:
            ValueError: If *colors* is empty or *duration_ms* is negative.
        """
        if not colors:
            raise ValueError("colors list must not be empty")
        if duration_ms < 0:
            raise ValueError(f"duration_ms must be >= 0, got {duration_ms}")

        total = len(colors)
        num_packets = math.ceil(total / ZONES_PER_PACKET)

        for i in range(num_packets):
            start = i * ZONES_PER_PACKET
            chunk = colors[start:start + ZONES_PER_PACKET]
            is_last = (i == num_packets - 1)
            # Stage all chunks silently; only the final one triggers rendering
            apply_flag = APPLY_YES if is_last else APPLY_NO

            # Payload: duration(u32) + apply(u8) + index(u16) + count(u8)
            payload = struct.pack(
                "<IBH B", duration_ms, apply_flag, start, len(chunk),
            )
            for h, s, b, k in chunk:
                payload += struct.pack(HSBK_FMT, h, s, b, k)
            # Pad to ZONES_PER_PACKET zones (protocol requires fixed-size color array)
            for _ in range(ZONES_PER_PACKET - len(chunk)):
                payload += b'\x00' * HSBK_SIZE

            if rapid:
                self.fire_and_forget(MSG_SET_EXTENDED_COLOR_ZONES, payload)
            else:
                self._send(MSG_SET_EXTENDED_COLOR_ZONES, payload, ack=True)

    def set_power(self, on: bool, duration_ms: int = 0) -> None:
        """Turn the device on or off.

        Args:
            on:          ``True`` to turn on, ``False`` to turn off.
            duration_ms: Transition duration in milliseconds.
        """
        level = POWER_ON if on else POWER_OFF
        payload = struct.pack("<HI", level, duration_ms)
        self._send(MSG_LIGHT_SET_POWER, payload, ack=True)

    def set_color(
        self,
        hue: int,
        sat: int,
        bri: int,
        kelvin: int,
        duration_ms: int = 0,
    ) -> None:
        """Set the whole device to a single color (non-multizone).

        Args:
            hue:         Hue (0--65535).
            sat:         Saturation (0--65535).
            bri:         Brightness (0--65535).
            kelvin:      Color temperature (1500--9000).
            duration_ms: Transition duration in milliseconds.

        Raises:
            ValueError: If any HSBK value is outside the valid range.
        """
        for name, val, lo, hi in [
            ("hue", hue, 0, HSBK_MAX),
            ("saturation", sat, 0, HSBK_MAX),
            ("brightness", bri, 0, HSBK_MAX),
            ("kelvin", kelvin, KELVIN_MIN, KELVIN_MAX),
        ]:
            if not (lo <= val <= hi):
                raise ValueError(f"{name} must be in [{lo}, {hi}], got {val}")

        # Payload: reserved(u8) + HSBK(4 x u16) + duration(u32)
        payload = struct.pack("<xHHHHI", hue, sat, bri, kelvin, duration_ms)
        self._send(MSG_LIGHT_SET_COLOR, payload, ack=True)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_devices(
    timeout: float = 3.0,
    target_ip: Optional[str] = None,
) -> list["LifxDevice"]:
    """Discover LIFX devices on the LAN.

    Sends periodic broadcast ``GetService`` messages and collects responses.
    Each discovered device is queried for version, label, group, and
    (for multizone devices) zone count.

    Args:
        timeout:   How long to listen for discovery responses (seconds).
                   Must be positive.
        target_ip: If set, skip broadcast and query only this IP directly.

    Returns:
        A list of :class:`LifxDevice` objects sorted by group, then label,
        then IP.  Returns an empty list if no devices respond.

    Raises:
        ValueError: If *timeout* is not positive, or *target_ip* is invalid.
    """
    if timeout <= 0:
        raise ValueError(f"timeout must be > 0, got {timeout}")

    # Single-device shortcut: skip broadcast, query directly
    if target_ip:
        # LifxDevice.__init__ handles resolution of hostnames to IPv4.
        dev = LifxDevice(target_ip)
        dev.query_all()
        if dev.product is not None:
            return [dev]
        dev.close()
        return []

    broadcast_addr: str = _get_broadcast_address()

    source_id = random.randint(SOURCE_ID_MIN, SOURCE_ID_MAX)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(DISCOVERY_RECV_TIMEOUT)
    sock.bind(("", 0))

    # Build a single broadcast GetService packet (reused for all sends)
    msg = _build_header(MSG_GET_SERVICE, 0, source_id, tagged=True, res=True)

    # Wake burst: rapid-fire a few GetService packets to prod sleeping bulbs
    # before the main discovery loop.  This helps on mesh routers (e.g.
    # TP-Link Deco) that delay or filter initial broadcast forwarding.
    for _ in range(DISCOVERY_WAKE_BURSTS):
        sock.sendto(msg, (broadcast_addr, LIFX_PORT))
        time.sleep(DISCOVERY_WAKE_DELAY)

    found: dict[str, tuple[str, bytes]] = {}  # mac_str -> (ip, mac_bytes)
    deadline = time.time() + timeout
    next_send: float = 0  # Send immediately on first iteration

    while time.time() < deadline:
        now = time.time()
        # Re-broadcast periodically to catch devices that missed earlier packets
        if now >= next_send:
            sock.sendto(msg, (broadcast_addr, LIFX_PORT))
            next_send = now + DISCOVERY_INTERVAL
        try:
            data, (ip, _) = sock.recvfrom(MAX_UDP_PAYLOAD)
            parsed = _parse_message(data)
            if parsed and parsed["type"] == MSG_STATE_SERVICE:
                mac = mac_bytes_to_str(parsed["target"])  # type: ignore[arg-type]
                # Ignore null MAC and deduplicate by MAC address
                if mac not in found and mac != "00:00:00:00:00:00":
                    found[mac] = (ip, parsed["target"])  # type: ignore[assignment]
        except socket.timeout:
            continue

    sock.close()

    # Query each discovered device for full metadata
    devices: list[LifxDevice] = []
    for _mac_str, (ip, mac_bytes) in found.items():
        dev = LifxDevice(ip, mac_bytes, source_id)
        dev.query_all()
        devices.append(dev)

    # Stable sort: group -> label -> IP for deterministic ordering
    devices.sort(key=lambda d: (d.group or "", d.label or "", d.ip))
    return devices
