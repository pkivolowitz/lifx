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

__version__ = "1.6"

import enum
import logging
import math
import random
import re
import socket
import struct
import threading
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

# Ack worker constants — per-IP acked send pacing
ACK_TIMEOUT: float = 0.2       # Seconds to wait for ack (observed avg ~5ms, spikes ~200ms)
ACK_RETRIES: int = 0           # No retries — move on to the next frame immediately
ACK_STATS_LOG_INTERVAL: int = 500  # Log RTT summary every N acked frames
ACK_SEQ_WRAP: int = 256        # Sequence number wraps at 0-255 (u8)


# ---------------------------------------------------------------------------
# Send mode
# ---------------------------------------------------------------------------

class SendMode(enum.Enum):
    """How :meth:`LifxDevice.set_zones` delivers packets to the device.

    ``ACK_PACED``
        Submit to the per-device :class:`_AckWorker` for back-pressure
        pacing.  Used for solo devices during animation rendering.

    ``IMMEDIATE``
        Fire-and-forget — bypass the ack worker, send the UDP packet
        directly with no acknowledgement.  Used by
        :class:`VirtualMultizoneEmitter` for synchronized group fan-out
        so all devices receive their frames simultaneously.

    ``GUARANTEED``
        Send with ack and block until acknowledged (or timeout).  Used
        for stop/fade commands where delivery must be confirmed.
    """

    ACK_PACED = "ack_paced"
    IMMEDIATE = "immediate"
    GUARANTEED = "guaranteed"


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
MSG_ACKNOWLEDGEMENT: int = 45
MSG_GET_LABEL: int = 23
MSG_SET_LABEL: int = 24
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
MSG_SET_MULTIZONE_EFFECT: int = 508
MSG_SET_EXTENDED_COLOR_ZONES: int = 510
MSG_GET_EXTENDED_COLOR_ZONES: int = 511
MSG_STATE_EXTENDED_COLOR_ZONES: int = 512

# Multizone firmware effect types (for SetMultiZoneEffect, type 508).
MULTIZONE_EFFECT_OFF: int = 0
MULTIZONE_EFFECT_MOVE: int = 1

# Apply flags for extended multizone set
APPLY_NO: int = 0     # Stage colors, don't render yet
APPLY_YES: int = 1    # Stage and render atomically
APPLY_ONLY: int = 2   # Render previously staged colors

# --- Matrix / tile protocol (700-series) ---
MSG_GET_DEVICE_CHAIN: int = 701
MSG_STATE_DEVICE_CHAIN: int = 702
MSG_GET64: int = 707
MSG_STATE64: int = 711
MSG_SET64: int = 715
MSG_COPY_FRAME_BUFFER: int = 716
MSG_GET_TILE_EFFECT: int = 718
MSG_SET_TILE_EFFECT: int = 719
MSG_STATE_TILE_EFFECT: int = 720

# Tile struct size in StateDeviceChain response (55 bytes per tile entry).
TILE_STRUCT_SIZE: int = 55
# Maximum tile entries in a StateDeviceChain response (always 16 slots).
MAX_TILES_IN_CHAIN: int = 16
# StateDeviceChain payload: start_index(u8) + 16*Tile(55B) + count(u8).
STATE_DEVICE_CHAIN_PAYLOAD_SIZE: int = 1 + MAX_TILES_IN_CHAIN * TILE_STRUCT_SIZE + 1
# Maximum HSBK entries in a single Set64/State64 packet.
TILE_PIXELS_PER_PACKET: int = 64
# Frame buffer indices for Set64 / CopyFrameBuffer.
FB_DISPLAY: int = 0   # Write directly to visible display buffer
FB_TEMP: int = 1      # Write to temp buffer (for >64-zone devices)

# Tile firmware effect types (for SetTileEffect, type 719).
TILE_EFFECT_OFF: int = 0
TILE_EFFECT_MORPH: int = 2
TILE_EFFECT_FLAME: int = 3
TILE_EFFECT_SKY: int = 5

# HSBK black at default kelvin — padding for tile pixel arrays.
HSBK_BLACK_DEFAULT: tuple[int, int, int, int] = (0, 0, 0, 3500)

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
#: Source: https://github.com/LIFX/products/blob/master/products.json
MULTIZONE_PRODUCTS: set[int] = {
    31,             # LIFX Z
    32,             # LIFX Z
    38,             # LIFX Beam
    125,            # LIFX Neon
    117,            # LIFX Z US
    118,            # LIFX Z Intl
    119,            # LIFX Beam US
    120,            # LIFX Beam Intl
    141,            # LIFX Neon US
    142,            # LIFX Neon Intl
    143,            # LIFX String US
    144,            # LIFX String Intl
    161,            # LIFX Outdoor Neon US
    162,            # LIFX Outdoor Neon Intl
    203,            # LIFX String US (gen2)
    204,            # LIFX String Intl (gen2)
    205,            # LIFX Indoor Neon US
    206,            # LIFX Indoor Neon Intl
    213,            # LIFX Permanent Outdoor US
    214,            # LIFX Permanent Outdoor Intl
}

#: Product IDs for Neon-class strips (firmware needs slow FPS + long transitions).
NEON_PRODUCTS: set[int] = {
    125,            # LIFX Neon
    141,            # LIFX Neon US
    142,            # LIFX Neon Intl
    161,            # LIFX Outdoor Neon US
    162,            # LIFX Outdoor Neon Intl
    205,            # LIFX Indoor Neon US
    206,            # LIFX Indoor Neon Intl
}

#: Product IDs for monochrome-only bulbs (brightness + kelvin, no hue/saturation).
MONOCHROME_PRODUCTS: set[int] = {
    10, 11, 18, 50, 51, 60, 61, 87, 88, 113, 114, 115, 116,
}

#: Product IDs for matrix/tile devices (2D addressable grid).
#: Source: https://github.com/LIFX/products/blob/master/products.json
#: All products with the "matrix" capability flag.
MATRIX_PRODUCTS: set[int] = {
    55,             # LIFX Tile (8x8, chainable)
    57,             # LIFX Candle (5x6)
    68,             # LIFX Candle (5x6)
    137,            # LIFX Candle Color US
    138,            # LIFX Candle Colour Intl
    171,            # LIFX Round Spot US
    173,            # LIFX Round Path US
    174,            # LIFX Square Path US
    176,            # LIFX Ceiling US (16x8)
    177,            # LIFX Ceiling Intl (16x8)
    185,            # LIFX Candle Color US
    186,            # LIFX Candle Colour Intl
    201,            # LIFX Ceiling 13x26" US (16x8)
    202,            # LIFX Ceiling 13x26" Intl (16x8)
    215,            # LIFX Candle Color US
    216,            # LIFX Candle Colour Intl
    217,            # LIFX Tube US
    218,            # LIFX Tube Intl
    219,            # LIFX Luna US
    220,            # LIFX Luna Intl
    221,            # LIFX Round Spot Intl
    222,            # LIFX Round Path Intl
}

#: Matrix products with >64 zones that require frame-buffer double-buffering.
#: These devices need: write batches to fb=1, then CopyFrameBuffer to fb=0.
MATRIX_DOUBLE_BUFFER_PRODUCTS: set[int] = {
    176,            # LIFX Ceiling US (16x8 = 128 zones)
    177,            # LIFX Ceiling Intl
    201,            # LIFX Ceiling 13x26" US
    202,            # LIFX Ceiling 13x26" Intl
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
    141: "Neon US", 142: "Neon Intl",
    143: "String Light US", 144: "String Light Intl",
    161: "Outdoor Neon US", 162: "Outdoor Neon Intl",
    203: "String Light US", 204: "String Light Intl",
    205: "Indoor Neon US", 206: "Indoor Neon Intl",
    137: "Candle Color US", 138: "Candle Colour Intl",
    171: "Round Spot US", 173: "Round Path US", 174: "Square Path US",
    176: "Ceiling US", 177: "Ceiling Intl",
    185: "Candle Color US", 186: "Candle Colour Intl",
    201: "Ceiling US", 202: "Ceiling Intl",
    205: "Indoor Neon US", 206: "Indoor Neon Intl",
    213: "Permanent Outdoor US", 214: "Permanent Outdoor Intl",
    215: "Candle Color US", 216: "Candle Colour Intl",
    217: "Tube US", 218: "Tube Intl",
    219: "Luna US", 220: "Luna Intl",
    221: "Round Spot Intl", 222: "Round Path Intl",
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

    Works on macOS and Linux.  On macOS, parses ``ifconfig`` output for
    the broadcast address.  On Linux, uses ``fcntl``/ioctl to read the IP
    and netmask from the default-route interface, then computes the
    broadcast address as ``IP | ~netmask``.

    On Windows and other platforms, broadcast detection is not available.
    The function returns the global fallback ``"255.255.255.255"`` and
    logs a warning.  Use ``--ip`` to address devices directly on these
    platforms.

    Returns:
        A dotted-quad broadcast address string (e.g. ``"192.0.2.255"``
        on a /22 or ``"192.0.2.255"`` on a /24).  Falls back to
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
                # Look for: "inet 192.0.2.38 netmask 0xfffffc00 broadcast 192.0.2.255"
                if "broadcast" in line:
                    tokens = line.split()
                    bcast_idx: int = tokens.index("broadcast")
                    return tokens[bcast_idx + 1]

            return FALLBACK_BROADCAST

        elif platform.system() == "Linux":
            # Linux: use ioctl to compute broadcast from IP and netmask.
            import fcntl
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

        else:
            # Windows or other unsupported platform — broadcast detection
            # is not available.  Discovery will use the global fallback
            # (255.255.255.255) which may work on simple networks.  For
            # reliable operation, use --ip to address devices directly.
            import logging
            logging.getLogger(__name__).warning(
                "Broadcast auto-detection is not supported on %s. "
                "Use --ip to address devices directly.",
                platform.system(),
            )
            return FALLBACK_BROADCAST

    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return FALLBACK_BROADCAST


def _resolve_host(host: str) -> str:
    """Resolve a hostname or IPv4 address to a dotted-quad IPv4 string.

    Accepts either a raw IPv4 address (``"192.0.2.62"``) or a DNS
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
    seq: int = 0,
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
        seq:          Sequence number (0--255) for ack correlation.

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
    frame_addr = target + reserved + struct.pack("<BB", ack_res, seq & 0xFF)

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
# _AckWorker — per-device ack-paced send thread
# ---------------------------------------------------------------------------

# Module _log (used by _AckWorker and elsewhere).
_log: logging.Logger = logging.getLogger("glowup.transport")


class _AckWorker:
    """Per-device daemon thread that sends frames with ack-pacing.

    Maintains a size-1 frame slot: the engine overwrites it each frame
    (non-blocking), and the worker sends the latest frame, waits for
    the LIFX Acknowledgement (msg type 45), then grabs the next.

    This provides natural back-pressure: slow devices pace themselves
    while fast devices run at full speed.  Multiple devices run in
    parallel (each has its own worker).

    Attributes:
        stats: Dict of RTT and delivery statistics.
    """

    def __init__(self, device: "LifxDevice") -> None:
        """Initialize the ack worker for a specific device.

        The worker thread is not started until the first :meth:`submit`
        call (lazy start), avoiding socket contention during device
        queries in :meth:`LifxDevice.query_all`.

        Args:
            device: The :class:`LifxDevice` whose socket we send on.
        """
        self._device: "LifxDevice" = device
        self._slot_lock: threading.Lock = threading.Lock()
        self._slot: Optional[list[tuple[int, bytes]]] = None
        self._slot_event: threading.Event = threading.Event()
        self._stop_event: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started: bool = False
        self._seq: int = 0

        # Cumulative statistics.
        self._stats_lock: threading.Lock = threading.Lock()
        self._rtt_count: int = 0
        self._rtt_sum: float = 0.0
        self._rtt_min: float = float('inf')
        self._rtt_max: float = 0.0
        self._drops: int = 0
        self._sends: int = 0

    def submit(self, packets: list[tuple[int, bytes]]) -> None:
        """Submit a frame for acked delivery (non-blocking).

        Replaces any pending frame in the slot.  The worker thread
        picks up the latest frame on its next iteration, ensuring
        latest-frame semantics (no stale playback).

        Args:
            packets: List of ``(msg_type, payload)`` tuples.  For
                     multizone devices with >82 zones this may contain
                     multiple staging packets followed by a final
                     apply packet.
        """
        with self._slot_lock:
            self._slot = packets
        self._slot_event.set()
        if not self._started:
            self._start()

    def _start(self) -> None:
        """Spawn the worker thread on first submit (lazy start)."""
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"ack-{self._device.ip}",
        )
        self._thread.start()

    def _run(self) -> None:
        """Worker loop: wait for frame, send with ack, repeat."""
        sock: socket.socket = self._device.sock
        # Save the original timeout and switch to ack timeout.
        original_timeout: Optional[float] = sock.gettimeout()
        sock.settimeout(ACK_TIMEOUT)

        try:
            while not self._stop_event.is_set():
                # Wait for a new frame (or stop signal).
                self._slot_event.wait(timeout=ACK_TIMEOUT)
                if self._stop_event.is_set():
                    break

                # Atomically read-and-clear the slot.
                with self._slot_lock:
                    packets = self._slot
                    self._slot = None
                self._slot_event.clear()

                if packets is None:
                    continue

                self._send_with_ack(sock, packets)
        finally:
            # Restore the original socket timeout.
            try:
                sock.settimeout(original_timeout)
            except OSError:
                pass

    def _send_with_ack(
        self,
        sock: socket.socket,
        packets: list[tuple[int, bytes]],
    ) -> None:
        """Send all packets in a frame, ack-waiting on the last one.

        Intermediate packets (APPLY_NO staging) are sent fire-and-forget.
        Only the final packet (APPLY_YES) sets ack_required and waits
        for Acknowledgement (msg type 45).

        Args:
            sock:    The device's UDP socket.
            packets: Ordered list of ``(msg_type, payload)`` tuples.
        """
        if not packets:
            return

        dest: tuple[str, int] = (self._device.ip, LIFX_PORT)
        seq: int = self._seq
        self._seq = (self._seq + 1) % ACK_SEQ_WRAP

        # Fire-and-forget all but the last packet (staging).
        for msg_type, payload in packets[:-1]:
            header = _build_header(
                msg_type, len(payload), self._device.source_id,
                target=self._device.mac, seq=seq,
            )
            try:
                sock.sendto(header + payload, dest)
            except OSError:
                pass

        # Final packet: send with ack_required and wait.
        final_type, final_payload = packets[-1]
        header = _build_header(
            final_type, len(final_payload), self._device.source_id,
            target=self._device.mac, ack=True, seq=seq,
        )
        pkt: bytes = header + final_payload

        for attempt in range(1 + ACK_RETRIES):
            t_send: float = time.monotonic()
            try:
                sock.sendto(pkt, dest)
            except OSError:
                with self._stats_lock:
                    self._drops += 1
                return

            with self._stats_lock:
                self._sends += 1

            # Wait for ack.
            acked: bool = self._wait_for_ack(sock, t_send)
            if acked:
                return

        # All attempts exhausted — record drop.
        with self._stats_lock:
            self._drops += 1

    def _wait_for_ack(self, sock: socket.socket, t_send: float) -> bool:
        """Block until an Acknowledgement arrives or timeout expires.

        Args:
            sock:   The device's UDP socket.
            t_send: Monotonic timestamp of the send for RTT calculation.

        Returns:
            ``True`` if an ack was received, ``False`` on timeout.
        """
        deadline: float = time.monotonic() + ACK_TIMEOUT
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(MAX_UDP_PAYLOAD)
                msg = _parse_message(data)
                if msg and msg["type"] == MSG_ACKNOWLEDGEMENT:
                    rtt_ms: float = (time.monotonic() - t_send) * 1000.0
                    with self._stats_lock:
                        self._rtt_count += 1
                        self._rtt_sum += rtt_ms
                        if rtt_ms < self._rtt_min:
                            self._rtt_min = rtt_ms
                        if rtt_ms > self._rtt_max:
                            self._rtt_max = rtt_ms
                        count = self._rtt_count
                    if count % ACK_STATS_LOG_INTERVAL == 0:
                        stats = self.get_stats()
                        _log.info(
                            "Ack stats [%s]: %d acked, "
                            "rtt avg %.1fms min %.1fms max %.1fms, "
                            "%d drops",
                            self._device.ip, stats["acked"],
                            stats["rtt_avg_ms"], stats["rtt_min_ms"],
                            stats["rtt_max_ms"], stats["drops"],
                        )
                    return True
            except socket.timeout:
                return False
            except OSError:
                return False
        return False

    def get_stats(self) -> dict:
        """Return a snapshot of cumulative ack statistics.

        Returns:
            Dict with keys: ``acked``, ``sends``, ``drops``,
            ``rtt_avg_ms``, ``rtt_min_ms``, ``rtt_max_ms``.
        """
        with self._stats_lock:
            count = self._rtt_count
            return {
                "acked": count,
                "sends": self._sends,
                "drops": self._drops,
                "rtt_avg_ms": round(self._rtt_sum / count, 1) if count else 0.0,
                "rtt_min_ms": round(self._rtt_min, 1) if count else 0.0,
                "rtt_max_ms": round(self._rtt_max, 1) if count else 0.0,
            }

    def stop(self) -> None:
        """Signal the worker thread to exit and wait for it."""
        self._stop_event.set()
        self._slot_event.set()  # Wake the thread if it's waiting.
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


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
        acked: bool = True,
    ) -> None:
        """Create a device handle and open a persistent UDP socket.

        Args:
            ip:        Device IP address or hostname.  Hostnames are
                       resolved to IPv4 via the system resolver
                       (``/etc/hosts``, mDNS, DNS).
            mac_bytes: 8-byte MAC address (default: all zeros).
            source_id: Session identifier (default: random value in
                       [SOURCE_ID_MIN, SOURCE_ID_MAX]).
            acked:     If ``True`` (default), animation frames are sent
                       via a per-device :class:`_AckWorker` that waits
                       for LIFX Acknowledgement before sending the next
                       frame.  This provides natural back-pressure and
                       eliminates dropped frames.  Set to ``False`` for
                       legacy fire-and-forget behavior.

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

        # Matrix device fields (populated by query_device_chain).
        self.matrix_width: Optional[int] = None
        self.matrix_height: Optional[int] = None
        self.tile_count: Optional[int] = None

        # Per-device ack worker (lazy-started on first animation frame).
        self._ack_worker: Optional[_AckWorker] = (
            _AckWorker(self) if acked else None
        )

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
    def is_neon(self) -> Optional[bool]:
        """Whether this device is a Neon-class strip.

        Neon products have distinct firmware behavior (e.g., fewer
        zones, different LED driver timing).  With ack-paced sends
        the engine no longer needs special FPS tuning for Neons.

        Returns:
            ``True`` if the product ID is in :data:`NEON_PRODUCTS`,
            ``False`` if the product is known but not a Neon, or
            ``None`` if the product ID has not been queried yet.
        """
        if self.product is None:
            return None
        return self.product in NEON_PRODUCTS

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

    @property
    def is_matrix(self) -> Optional[bool]:
        """Whether this device supports the tile/matrix protocol (700-series).

        Matrix devices have a 2D grid of individually addressable HSBK
        zones and use Set64 (715) instead of SetExtendedColorZones (510).

        Returns:
            ``True`` if the product ID is in :data:`MATRIX_PRODUCTS`,
            ``False`` if the product is known but not a matrix device, or
            ``None`` if the product ID has not been queried yet.
        """
        if self.product is None:
            return None
        return self.product in MATRIX_PRODUCTS

    @property
    def needs_double_buffer(self) -> bool:
        """Whether this matrix device has >64 zones and needs frame-buffer staging.

        Ceiling devices (16x8 = 128 zones) must write to fb=1 in batches
        then CopyFrameBuffer to fb=0 for atomic display updates.
        """
        if self.product is None:
            return False
        return self.product in MATRIX_DOUBLE_BUFFER_PRODUCTS

    @property
    def ack_stats(self) -> dict:
        """Return cumulative ack-pacing statistics.

        Returns an empty dict when the device was created with
        ``acked=False``.

        Returns:
            Dict with keys: ``acked``, ``sends``, ``drops``,
            ``rtt_avg_ms``, ``rtt_min_ms``, ``rtt_max_ms``.
        """
        if self._ack_worker is None:
            return {}
        return self._ack_worker.get_stats()

    # -- Lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Stop the ack worker (if any) and close the UDP socket.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._ack_worker is not None:
            self._ack_worker.stop()
            self._ack_worker = None
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
            A string like ``<LifxDevice String 2 (192.0.2.62)>``.
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
        try:
            self.sock.sendto(header + payload, (self.ip, LIFX_PORT))
        except OSError as exc:
            logging.debug("Send to %s failed: %s", self.ip, exc)

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

        for _attempt in range(retries):
            self._send(msg_type, payload, res=True)
            # Each attempt gets a fresh deadline so retries are genuine.
            attempt_deadline = time.time() + timeout
            while time.time() < attempt_deadline:
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
                except OSError:
                    # Host unreachable, network down, etc.
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
        try:
            self.sock.sendto(header + payload, (self.ip, LIFX_PORT))
        except OSError as exc:
            logging.debug("Fire-and-forget to %s failed: %s", self.ip, exc)

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

    def set_label(self, label: str) -> bool:
        """Write a label to the device firmware via SetLabel (type 24).

        The label is a 32-byte UTF-8 field, null-padded.  Labels longer
        than 32 bytes are truncated at the last valid UTF-8 boundary.

        This is a metadata operation — not an animation command — so
        bricking risk from rapid-fire is not a concern.  The write is
        ack-required to confirm the device received it.

        Args:
            label: The new label (max 32 bytes UTF-8).

        Returns:
            ``True`` if the device acknowledged, ``False`` on timeout.
        """
        encoded: bytes = label.encode("utf-8")[:LABEL_FIELD_SIZE]
        payload: bytes = encoded.ljust(LABEL_FIELD_SIZE, b'\x00')

        # Send with ack_required to confirm the write.
        self._send(MSG_SET_LABEL, payload, ack=True)

        # Wait for acknowledgement.
        old_timeout: float = self.sock.gettimeout() or SOCKET_TIMEOUT
        try:
            self.sock.settimeout(SOCKET_TIMEOUT)
            deadline: float = time.time() + SOCKET_TIMEOUT
            while time.time() < deadline:
                try:
                    data, _ = self.sock.recvfrom(MAX_UDP_PAYLOAD)
                    msg = _parse_message(data)
                    if msg and msg["type"] == MSG_ACKNOWLEDGEMENT:
                        self.label = label
                        return True
                except socket.timeout:
                    break
                except OSError:
                    break
        except Exception as exc:
            _log.warning("set_label(%r) failed: %s: %s",
                         label, type(exc).__name__, exc)
        finally:
            self.sock.settimeout(old_timeout)

        return False

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

    def _flush_socket(self) -> None:
        """Drain any stale packets from the socket receive buffer.

        Sets a very short timeout to non-blocking-read until the
        buffer is empty, then restores the original timeout.
        """
        original_timeout: Optional[float] = self.sock.gettimeout()
        self.sock.settimeout(0.0)  # non-blocking
        try:
            while True:
                self.sock.recvfrom(MAX_UDP_PAYLOAD)
        except (BlockingIOError, OSError):
            pass
        self.sock.settimeout(original_timeout)

    def query_zone_colors(
        self,
    ) -> Optional[list[tuple[int, int, int, int]]]:
        """Query the current HSBK colors of all zones.

        Sends a ``GetExtendedColorZones`` (511) message and collects
        all ``StateExtendedColorZones`` (512) response packets.
        Devices with more than 82 zones respond with multiple packets
        (e.g. 108 zones → packet 1: zones 0-81, packet 2: zones 82-107).
        This method stitches them together into a single list.

        The response layout per packet is::

            zones_count  (u16)  — total number of zones on the device
            zone_index   (u16)  — first zone index in this packet
            colors_count (u8)   — number of HSBK entries following
            colors       (HSBK × colors_count)

        Returns:
            A list of ``(hue, saturation, brightness, kelvin)`` tuples,
            one per zone, or ``None`` on failure.
        """
        # Flush stale responses left by previous queries.
        self._flush_socket()

        self._send(MSG_GET_EXTENDED_COLOR_ZONES, res=True)

        # Collect responses until we have all zones or time out.
        total_zones: Optional[int] = None
        zone_data: dict[int, tuple[int, int, int, int]] = {}
        deadline: float = time.time() + SOCKET_TIMEOUT

        while time.time() < deadline:
            try:
                data, _ = self.sock.recvfrom(MAX_UDP_PAYLOAD)
            except socket.timeout:
                break

            msg = _parse_message(data)
            if not msg or msg["type"] != MSG_STATE_EXTENDED_COLOR_ZONES:
                continue

            payload: bytes = msg["payload"]  # type: ignore[assignment]
            if len(payload) < ZONE_COUNT_PAYLOAD_MIN:
                continue

            zones_count: int = struct.unpack_from("<H", payload, 0)[0]
            zone_index: int = struct.unpack_from("<H", payload, 2)[0]
            colors_count: int = struct.unpack_from("<B", payload, 4)[0]

            if total_zones is None:
                total_zones = zones_count

            # Parse HSBK entries from this packet.
            colors_offset: int = 5
            needed: int = colors_offset + colors_count * HSBK_SIZE
            if len(payload) < needed:
                continue

            for i in range(colors_count):
                idx: int = zone_index + i
                if idx >= zones_count:
                    break
                offset: int = colors_offset + i * HSBK_SIZE
                hue, sat, bri, kelvin = struct.unpack_from(
                    HSBK_FMT, payload, offset,
                )
                zone_data[idx] = (hue, sat, bri, kelvin)

            # Stop early if we have all zones.
            if len(zone_data) >= zones_count:
                break

        if total_zones is None or not zone_data:
            return None

        # Update cached zone_count.
        self.zone_count = total_zones

        # Build ordered list, filling any gaps with black.
        colors: list[tuple[int, int, int, int]] = []
        for i in range(total_zones):
            colors.append(zone_data.get(i, (0, 0, 0, 0)))

        return colors

    # -- Matrix / tile queries -----------------------------------------------

    def query_device_chain(self) -> Optional[tuple[int, int, int]]:
        """Query the tile device chain to get matrix dimensions.

        Sends ``GetDeviceChain`` (701) and parses tile entries from the
        ``StateDeviceChain`` (702) response.  Each tile struct is 55
        bytes.  The chain ends at the first tile with ``width == 0``.

        Returns:
            ``(width, height, tile_count)`` tuple, or ``None`` on
            failure.  On success, also populates :attr:`matrix_width`,
            :attr:`matrix_height`, :attr:`tile_count`, and
            :attr:`zone_count`.
        """
        payload = self._send_and_recv(
            MSG_GET_DEVICE_CHAIN, MSG_STATE_DEVICE_CHAIN,
            timeout=3.0,
        )
        if payload is None or len(payload) < STATE_DEVICE_CHAIN_PAYLOAD_SIZE:
            _log.debug(
                "query_device_chain(%s): no/short response, "
                "falling back to product defaults", self.ip,
            )
            return self._matrix_defaults()

        # Layout: start_index(u8) + 16 × Tile(55B) + tile_devices_count(u8)
        tile_count_raw: int = struct.unpack_from(
            "<B", payload,
            1 + MAX_TILES_IN_CHAIN * TILE_STRUCT_SIZE,
        )[0]

        actual_tiles: int = 0
        first_width: int = 0
        first_height: int = 0

        for i in range(min(tile_count_raw, MAX_TILES_IN_CHAIN)):
            offset: int = 1 + i * TILE_STRUCT_SIZE
            # Tile struct: accel(6B) + reserved(2B) + user_x(4B) +
            # user_y(4B) = 16 bytes before width/height fields.
            w: int = struct.unpack_from("<B", payload, offset + 16)[0]
            h: int = struct.unpack_from("<B", payload, offset + 17)[0]
            if w == 0:
                break  # End-of-chain marker
            actual_tiles += 1
            if actual_tiles == 1:
                first_width = w
                first_height = h

        if actual_tiles == 0:
            _log.debug(
                "query_device_chain(%s): chain empty, using defaults",
                self.ip,
            )
            return self._matrix_defaults()

        self.matrix_width = first_width
        self.matrix_height = first_height
        self.tile_count = actual_tiles
        self.zone_count = first_width * first_height
        _log.info(
            "query_device_chain(%s): %dx%d, %d tile(s), %d zones",
            self.ip, first_width, first_height,
            actual_tiles, self.zone_count,
        )
        return (first_width, first_height, actual_tiles)

    def _matrix_defaults(self) -> Optional[tuple[int, int, int]]:
        """Apply hardcoded matrix dimensions when the chain query fails.

        Falls back to 8×8 for unknown matrix products.  Returns
        ``None`` if the product is not a matrix device.
        """
        if self.product is None or self.product not in MATRIX_PRODUCTS:
            return None
        # Known dimensions from LIFX products.json.
        defaults: dict[int, tuple[int, int]] = {
            55: (8, 8),     # Tile
            57: (5, 6),     # Candle
            68: (5, 6),     # Candle
            137: (5, 6),    # Candle Color US
            138: (5, 6),    # Candle Colour Intl
            176: (16, 8),   # Ceiling US
            177: (16, 8),   # Ceiling Intl
            185: (5, 6),    # Candle Color US
            186: (5, 6),    # Candle Colour Intl
            201: (16, 8),   # Ceiling 13x26" US
            202: (16, 8),   # Ceiling 13x26" Intl
            215: (5, 6),    # Candle Color US
            216: (5, 6),    # Candle Colour Intl
        }
        w, h = defaults.get(self.product, (8, 8))
        self.matrix_width = w
        self.matrix_height = h
        self.tile_count = 1
        self.zone_count = w * h
        _log.info(
            "_matrix_defaults(%s): using %dx%d for product %d",
            self.ip, w, h, self.product,
        )
        return (w, h, 1)

    def query_tile_colors(
        self,
    ) -> Optional[list[tuple[int, int, int, int]]]:
        """Query the current HSBK colors from a matrix device via Get64.

        Sends ``Get64`` (707) for each tile and collects ``State64``
        (711) responses.  Returns colors in row-major order.

        Returns:
            A list of ``(hue, saturation, brightness, kelvin)`` tuples,
            one per pixel, or ``None`` on failure.
        """
        if self.tile_count is None or self.matrix_width is None:
            return None

        self._flush_socket()
        all_colors: list[tuple[int, int, int, int]] = []

        for tile_idx in range(self.tile_count):
            get_payload: bytes = struct.pack(
                "<BBBBBB",
                tile_idx,           # tile_index
                1,                  # length (one tile at a time)
                0,                  # reserved
                0,                  # x start
                0,                  # y start
                self.matrix_width,  # width
            )
            resp = self._send_and_recv(
                MSG_GET64, MSG_STATE64, payload=get_payload, timeout=2.0,
            )
            if resp is None:
                return None

            # State64: tile_index(u8) + reserved(u8) + x(u8) + y(u8)
            #          + width(u8) + 64×HSBK(8B) = 517 bytes minimum.
            min_size: int = 5 + TILE_PIXELS_PER_PACKET * HSBK_SIZE
            if len(resp) < min_size:
                _log.warning(
                    "query_tile_colors(%s): State64 too short "
                    "(%d bytes, need %d)", self.ip, len(resp), min_size,
                )
                continue

            pixel_count: int = min(
                TILE_PIXELS_PER_PACKET,
                (self.matrix_width or 8) * (self.matrix_height or 8),
            )
            for i in range(pixel_count):
                offset: int = 5 + i * HSBK_SIZE
                h, s, b, k = struct.unpack_from(HSBK_FMT, resp, offset)
                all_colors.append((h, s, b, k))

        return all_colors if all_colors else None

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

        Queries version, label, group, and zone count.  Matrix devices
        query the tile chain for dimensions.  Multizone devices query
        the extended zone count.  Single-zone bulbs are set to
        :data:`SINGLE_ZONE_COUNT` (1).  Each sub-query is tolerant of
        timeouts; cached fields remain ``None`` for any that fails.

        Returns:
            ``self``, for method chaining.
        """
        self.query_version()
        self.query_label()
        self.query_group()
        if self.is_matrix:
            self.query_device_chain()
        elif self.is_multizone:
            self.query_zone_count()
        else:
            # Non-multizone, non-matrix devices are a single zone.
            self.zone_count = SINGLE_ZONE_COUNT
        return self

    # -- Zone control (extended multizone) -----------------------------------

    def set_zones(
        self,
        colors: list[tuple[int, int, int, int]],
        duration_ms: int = 0,
        *,
        mode: SendMode = SendMode.ACK_PACED,
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
            mode:        Delivery strategy:

                         - :attr:`SendMode.ACK_PACED` — submit to the
                           per-device ack worker for back-pressure
                           pacing.  Falls back to fire-and-forget if
                           no worker is active.
                         - :attr:`SendMode.IMMEDIATE` — fire-and-forget,
                           bypass ack worker.  Used for synchronized
                           group fan-out.
                         - :attr:`SendMode.GUARANTEED` — send with ack,
                           block until acknowledged.  Used for
                           stop/fade commands.

        Raises:
            ValueError: If *colors* is empty or *duration_ms* is negative.
        """
        if not colors:
            raise ValueError("colors list must not be empty")
        if duration_ms < 0:
            raise ValueError(f"duration_ms must be >= 0, got {duration_ms}")

        total = len(colors)
        num_packets = math.ceil(total / ZONES_PER_PACKET)

        # Build all packet payloads.
        packets: list[tuple[int, bytes]] = []
        for i in range(num_packets):
            start = i * ZONES_PER_PACKET
            chunk = colors[start:start + ZONES_PER_PACKET]
            is_last = (i == num_packets - 1)
            apply_flag = APPLY_YES if is_last else APPLY_NO

            # Payload: duration(u32) + apply(u8) + index(u16) + count(u8)
            payload = struct.pack(
                "<IBH B", duration_ms, apply_flag, start, len(chunk),
            )
            for h, s, b, k in chunk:
                payload += struct.pack(HSBK_FMT, h, s, b, k)
            # Pad to ZONES_PER_PACKET (protocol requires fixed-size color array)
            for _ in range(ZONES_PER_PACKET - len(chunk)):
                payload += b'\x00' * HSBK_SIZE

            packets.append((MSG_SET_EXTENDED_COLOR_ZONES, payload))

        # Route based on send mode.
        if mode is SendMode.ACK_PACED and self._ack_worker is not None:
            self._ack_worker.submit(packets)
        elif mode is SendMode.GUARANTEED:
            for msg_type, payload in packets:
                self._send(msg_type, payload, ack=True)
        else:
            # IMMEDIATE, or ACK_PACED without a worker (legacy fallback).
            for msg_type, payload in packets:
                self.fire_and_forget(msg_type, payload)

    def clear_firmware_effect(self) -> None:
        """Disable any firmware-level multizone effect on this device.

        LIFX multizone devices can run an internal effect (e.g. MOVE)
        that cycles zone colors autonomously inside the firmware.  This
        persists through power cycles and fights with software-rendered
        zone writes, causing visible strobing.

        Sends ``SetMultiZoneEffect`` (type 508) with ``type=OFF`` to
        disable any active firmware effect.  Safe to call on devices
        that don't have a firmware effect running — it's a no-op.
        """
        # SetMultiZoneEffect payload (LIFX LAN protocol):
        #   instance_id: u32  (0 = any)
        #   type:        u8   (0 = OFF, 1 = MOVE)
        #   reserved:    u16
        #   speed:       u32  (ms per cycle, irrelevant for OFF)
        #   duration:    u64  (ns, 0 = forever, irrelevant for OFF)
        #   reserved:    u32
        #   reserved:    u32
        #   parameters:  32 bytes (all zeros for OFF)
        payload: bytes = struct.pack(
            "<I B HI Q II 32s",
            0,                  # instance_id
            MULTIZONE_EFFECT_OFF,  # type = OFF
            0,                  # reserved
            0,                  # speed
            0,                  # duration
            0,                  # reserved
            0,                  # reserved
            b'\x00' * 32,      # parameters
        )
        self._send(MSG_SET_MULTIZONE_EFFECT, payload, ack=True)

    # -- Matrix / tile control (700-series) ----------------------------------

    def set_tile_zones(
        self,
        colors: list[tuple[int, int, int, int]],
        duration_ms: int = 0,
    ) -> None:
        """Set all pixels on a matrix device using the tile protocol.

        For devices with <=64 zones (Luna, Candle, Tile): sends a single
        ``Set64`` (715) packet directly to the display buffer.

        For devices with >64 zones (Ceiling, 128 zones): writes batches
        to the temp buffer (fb=1) then ``CopyFrameBuffer`` (716) to
        atomically swap to the display buffer.

        ``Set64`` does not support acknowledgement — all sends are
        fire-and-forget.  This is consistent with the Djelibeybi
        lifx-async reference implementation.

        Args:
            colors:      Row-major HSBK list, one per pixel.
            duration_ms: Firmware transition duration in milliseconds.

        Raises:
            ValueError: If *colors* is empty or *duration_ms* < 0.
        """
        if not colors:
            raise ValueError("colors list must not be empty")
        if duration_ms < 0:
            raise ValueError(f"duration_ms must be >= 0, got {duration_ms}")

        width: int = self.matrix_width or 8
        height: int = self.matrix_height or 8
        total: int = width * height

        # Pad or trim to exact device pixel count.
        if len(colors) < total:
            colors = list(colors) + [HSBK_BLACK_DEFAULT] * (total - len(colors))
        elif len(colors) > total:
            colors = colors[:total]

        if total <= TILE_PIXELS_PER_PACKET:
            # Single-packet path (Luna, Candle, Tile — 64 or fewer zones).
            self._send_set64(
                tile_index=0, colors=colors, width=width,
                duration_ms=duration_ms, fb_index=FB_DISPLAY,
            )
        else:
            # Multi-packet path (Ceiling = 128 zones).
            # Write to temp buffer in row batches, then copy to display.
            rows_per_batch: int = TILE_PIXELS_PER_PACKET // width
            for row_start in range(0, height, rows_per_batch):
                row_end: int = min(row_start + rows_per_batch, height)
                pixel_start: int = row_start * width
                pixel_end: int = row_end * width
                chunk: list[tuple[int, int, int, int]] = colors[pixel_start:pixel_end]
                self._send_set64(
                    tile_index=0, colors=chunk, width=width,
                    duration_ms=duration_ms, fb_index=FB_TEMP,
                    x=0, y=row_start,
                )
            # Swap temp → display atomically.
            self._send_copy_frame_buffer(
                tile_index=0, width=width, height=height,
                duration_ms=duration_ms,
            )

    def _send_set64(
        self,
        tile_index: int,
        colors: list[tuple[int, int, int, int]],
        width: int,
        duration_ms: int = 0,
        fb_index: int = FB_DISPLAY,
        x: int = 0,
        y: int = 0,
    ) -> None:
        """Build and send a single ``Set64`` (715) packet.

        Payload layout: tile_index(u8) + length(u8) + fb_index(u8) +
        x(u8) + y(u8) + width(u8) + duration(u32) + 64×HSBK(8B).

        Args:
            tile_index:  Index of the tile in the chain (0 for single-tile).
            colors:      Up to 64 HSBK tuples for this packet.
            width:       Tile pixel width.
            duration_ms: Transition duration in milliseconds.
            fb_index:    Frame buffer target (0=display, 1=temp).
            x:           X offset within the tile.
            y:           Y offset within the tile.
        """
        header_bytes: bytes = struct.pack(
            "<BBBBB BI",
            tile_index,
            1,              # length: one tile
            fb_index,
            x,
            y,
            width,
            duration_ms,
        )
        # Pack colors, pad to 64 entries with black.
        color_bytes: bytes = b''
        for h, s, b, k in colors:
            color_bytes += struct.pack(HSBK_FMT, h, s, b, k)
        pad_count: int = TILE_PIXELS_PER_PACKET - len(colors)
        if pad_count > 0:
            black: bytes = struct.pack(HSBK_FMT, *HSBK_BLACK_DEFAULT)
            color_bytes += black * pad_count

        self.fire_and_forget(MSG_SET64, header_bytes + color_bytes)

    def _send_copy_frame_buffer(
        self,
        tile_index: int,
        width: int,
        height: int,
        duration_ms: int = 0,
    ) -> None:
        """Send ``CopyFrameBuffer`` (716) to swap temp → display buffer.

        Payload (15 bytes): tile_index(u8) + length(u8) + src_fb(u8) +
        dst_fb(u8) + src_x(u8) + src_y(u8) + dst_x(u8) + dst_y(u8) +
        width(u8) + height(u8) + duration(u32) + reserved(u8).

        Args:
            tile_index:  Tile index in the chain.
            width:       Copy region width (full tile).
            height:      Copy region height (full tile).
            duration_ms: Transition duration in milliseconds.
        """
        payload: bytes = struct.pack(
            "<10BIB",
            tile_index,
            1,              # length: one tile
            FB_TEMP,        # src frame buffer
            FB_DISPLAY,     # dst frame buffer
            0, 0,           # src_x, src_y
            0, 0,           # dst_x, dst_y
            width,
            height,
            duration_ms,
            0,              # reserved
        )
        self._send(MSG_COPY_FRAME_BUFFER, payload, ack=True)

    def set_tile_effect(
        self,
        effect_type: int,
        speed_ms: int = 3000,
        duration_ns: int = 0,
        palette: Optional[list[tuple[int, int, int, int]]] = None,
    ) -> None:
        """Set a firmware-level tile effect (MORPH, FLAME, SKY).

        Args:
            effect_type: One of :data:`TILE_EFFECT_OFF`,
                         :data:`TILE_EFFECT_MORPH`,
                         :data:`TILE_EFFECT_FLAME`, or
                         :data:`TILE_EFFECT_SKY`.
            speed_ms:    Effect speed in milliseconds per cycle.
            duration_ns: Effect duration in nanoseconds (0 = infinite).
            palette:     Up to 16 HSBK colors for the MORPH palette.
        """
        pal: list[tuple[int, int, int, int]] = palette or []
        pal_count: int = min(len(pal), 16)

        # Build palette: 16 HSBK entries × 8 bytes = 128 bytes.
        pal_bytes: bytes = b''
        for i in range(16):
            if i < pal_count:
                pal_bytes += struct.pack(HSBK_FMT, *pal[i])
            else:
                pal_bytes += struct.pack(HSBK_FMT, *HSBK_BLACK_DEFAULT)

        # SetTileEffect (719) payload (188 bytes):
        #   reserved(u8) + reserved(u8) + instanceid(u32) + type(u8) +
        #   speed(u32) + duration(u64) + reserved(u32) + reserved(u32) +
        #   sky_type(u8) + reserved(3B) + cloud_sat_min(u8) +
        #   reserved(3B) + reserved(24B) + palette_count(u8) +
        #   palette(128B)
        header: bytes = struct.pack(
            "<BB I B I Q II",
            0, 0,               # reserved
            0,                  # instance_id
            effect_type,
            speed_ms,
            duration_ns,
            0, 0,               # reserved
        )
        # sky_type(u8) + reserved(3B) + cloud_sat_min(u8) + reserved(3B)
        # + reserved(24B) + palette_count(u8)
        mid: bytes = struct.pack("<B3xB3x24xB", 0, 0, pal_count)

        payload: bytes = header + mid + pal_bytes
        self._send(MSG_SET_TILE_EFFECT, payload, ack=True)

    def clear_tile_effect(self) -> None:
        """Disable any firmware-level tile effect on this matrix device.

        Sends ``SetTileEffect`` (type 719) with ``type=OFF``.
        """
        self.set_tile_effect(TILE_EFFECT_OFF)

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
# Broadcast wake
# ---------------------------------------------------------------------------

# Number of GetService packets in a broadcast wake burst.
WAKE_BURST_COUNT: int = 3
# Delay between wake burst packets (seconds).
WAKE_BURST_DELAY: float = 0.1


def broadcast_wake() -> None:
    """Send a burst of broadcast GetService packets to wake sleeping bulbs.

    LIFX bulbs in power-save mode respond to broadcast frames but may
    ignore unicast.  This function fires a rapid burst of broadcast
    ``GetService`` (type 2) packets — the same wake pattern used at the
    start of :func:`discover_devices` — to prod their radios back into
    an active state before unicast commands are sent.

    No responses are collected; this is fire-and-forget.
    """
    broadcast_addr: str = _get_broadcast_address()
    source_id: int = random.randint(SOURCE_ID_MIN, SOURCE_ID_MAX)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    msg: bytes = _build_header(
        MSG_GET_SERVICE, 0, source_id, tagged=True, res=True,
    )
    try:
        for _ in range(WAKE_BURST_COUNT):
            sock.sendto(msg, (broadcast_addr, LIFX_PORT))
            time.sleep(WAKE_BURST_DELAY)
    except OSError as exc:
        _log.debug("Broadcast wake failed: %s", exc)
    finally:
        sock.close()


def broadcast_power_off() -> None:
    """Broadcast a SetPower(off) to all LIFX devices on the LAN.

    Emergency function that sends a tagged (broadcast) SetPower packet
    with level=0 and duration=0ms to every reachable LIFX device.
    No acknowledgement is requested — this is fire-and-forget.

    Uses the transport layer's :func:`_build_header` so protocol
    constants are not duplicated outside the module.
    """
    broadcast_addr: str = _get_broadcast_address()
    source_id: int = random.randint(SOURCE_ID_MIN, SOURCE_ID_MAX)
    # SetPower payload: level(u16) + duration(u32) = 6 bytes.
    payload: bytes = struct.pack("<HI", POWER_OFF, 0)
    header: bytes = _build_header(
        MSG_LIGHT_SET_POWER, len(payload), source_id, tagged=True,
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.sendto(header + payload, (broadcast_addr, LIFX_PORT))
    except OSError as exc:
        _log.debug("Broadcast power-off failed: %s", exc)
    finally:
        sock.close()


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
    try:
        for _ in range(DISCOVERY_WAKE_BURSTS):
            sock.sendto(msg, (broadcast_addr, LIFX_PORT))
            time.sleep(DISCOVERY_WAKE_DELAY)
    except OSError as exc:
        sock.close()
        _log.warning("Broadcast send failed: %s", exc)
        return []

    found: dict[str, tuple[str, bytes]] = {}  # mac_str -> (ip, mac_bytes)
    deadline = time.time() + timeout
    next_send: float = 0  # Send immediately on first iteration

    while time.time() < deadline:
        now = time.time()
        # Re-broadcast periodically to catch devices that missed earlier packets
        if now >= next_send:
            try:
                sock.sendto(msg, (broadcast_addr, LIFX_PORT))
            except OSError:
                break
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
