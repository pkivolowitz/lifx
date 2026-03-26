"""BLE scanner — discover and connect to HomeKit accessories via bleak.

Provides two capabilities:

1. **Discovery** — scan for nearby HAP-BLE accessories by looking for
   the Apple manufacturer data (0x004C) with HomeKit type byte (0x06).
   Returns device addresses, names, and HomeKit metadata (category,
   state number, flags).

2. **Connection** — connect to a discovered accessory and wrap the
   bleak ``BleakClient`` in the :class:`GattClient` protocol expected
   by :class:`HapSession`.

Requires ``bleak`` (``pip install bleak``).  Imported lazily so the
rest of the BLE stack can be tested without hardware.

Linux (Pi) notes:
    - BlueZ must be running (``systemctl status bluetooth``).
    - The user must be in the ``bluetooth`` group, or run as root.
    - Active scanning works out of the box for HAP discovery.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import Callable, Optional

logger: logging.Logger = logging.getLogger("glowup.ble.scanner")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Apple's BLE company identifier (little-endian in manufacturer data dict).
APPLE_COMPANY_ID: int = 0x004C

# HomeKit sub-type byte within Apple manufacturer data.
HOMEKIT_TYPE: int = 0x06

# Default scan duration in seconds.
DEFAULT_SCAN_TIMEOUT: float = 10.0

# Minimum RSSI to consider a device (filter distant noise).
MIN_RSSI: int = -90

# HAP-BLE device categories (Table 12-3).
CATEGORY_NAMES: dict[int, str] = {
    1: "Other",
    2: "Bridge",
    3: "Fan",
    5: "Garage Door Opener",
    6: "Lightbulb",
    7: "Door Lock",
    8: "Outlet",
    9: "Switch",
    10: "Thermostat",
    11: "Sensor",
    12: "Security System",
    13: "Door",
    14: "Window",
    15: "Window Covering",
    16: "Programmable Switch",
    17: "Range Extender",
    18: "IP Camera",
    19: "Video Doorbell",
    20: "Air Purifier",
    28: "Sprinkler",
    29: "Faucet",
    30: "Shower Head",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class HapAdvertisement:
    """Parsed HomeKit BLE advertisement.

    Attributes:
        address: BLE address (MAC on Linux, UUID on macOS).
        name: Local name from the advertisement (may be None).
        rssi: Signal strength in dBm.
        category: HAP device category code.
        category_name: Human-readable category.
        state_number: Global State Number (GSN) — increments on state change.
        status_flags: HAP status flags byte.
        device_id: 6-byte device identifier from the advertisement.
        config_number: Configuration number.
        pairing_available: True if the device is unpaired and accepting pairing.
    """

    address: str
    name: Optional[str]
    rssi: int
    category: int
    category_name: str
    state_number: int
    status_flags: int
    device_id: bytes
    config_number: int
    pairing_available: bool


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

async def discover_hap_devices(
    timeout: float = DEFAULT_SCAN_TIMEOUT,
    min_rssi: int = MIN_RSSI,
) -> list[HapAdvertisement]:
    """Scan for HomeKit BLE accessories.

    Args:
        timeout: Scan duration in seconds.
        min_rssi: Minimum RSSI threshold.

    Returns:
        List of discovered HAP-BLE accessories.

    Raises:
        ImportError: If bleak is not installed.
    """
    try:
        from bleak import BleakScanner
    except ImportError:
        raise ImportError(
            "BLE scanning requires bleak: pip install bleak"
        )

    found: dict[str, HapAdvertisement] = {}

    def _detection_callback(device, advertisement_data) -> None:
        """Process each BLE advertisement."""
        if advertisement_data.rssi < min_rssi:
            return

        apple_data: Optional[bytes] = advertisement_data.manufacturer_data.get(
            APPLE_COMPANY_ID
        )
        if apple_data is None:
            return

        parsed: Optional[HapAdvertisement] = _parse_homekit_advertisement(
            address=device.address,
            name=advertisement_data.local_name,
            rssi=advertisement_data.rssi,
            apple_data=apple_data,
        )
        if parsed is not None:
            found[device.address] = parsed

    scanner = BleakScanner(detection_callback=_detection_callback)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()

    results: list[HapAdvertisement] = list(found.values())
    logger.info("BLE scan complete: %d HAP device(s) found", len(results))
    for adv in results:
        logger.info(
            "  %s  %-20s  RSSI=%d  category=%s  GSN=%d  pairable=%s",
            adv.address,
            adv.name or "(unnamed)",
            adv.rssi,
            adv.category_name,
            adv.state_number,
            adv.pairing_available,
        )
    return results


async def connect_and_wrap(
    address: str,
    timeout: float = 30.0,
) -> "BleakGattClient":
    """Connect to a BLE device and return a GattClient wrapper.

    Args:
        address: BLE address (from discovery).
        timeout: Connection timeout in seconds.

    Returns:
        A :class:`BleakGattClient` that implements the
        :class:`GattClient` protocol.

    Raises:
        ImportError: If bleak is not installed.
        bleak.exc.BleakError: On connection failure.
    """
    try:
        from bleak import BleakClient
    except ImportError:
        raise ImportError(
            "BLE connection requires bleak: pip install bleak"
        )

    # HAP-BLE accessories can be aggressive about disconnecting during
    # GATT service discovery.  Retry with exponential backoff.  On the
    # second attempt, bleak uses its cached service table (if the first
    # attempt got far enough to populate it), making discovery instant.
    MAX_CONNECT_ATTEMPTS: int = 3
    RETRY_DELAY: float = 2.0

    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_CONNECT_ATTEMPTS + 1):
        try:
            client = BleakClient(address, timeout=timeout)
            await client.connect()
            logger.info(
                "Connected to %s (attempt %d/%d)",
                address, attempt, MAX_CONNECT_ATTEMPTS,
            )
            return BleakGattClient(client)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Connection attempt %d/%d to %s failed: %s",
                attempt, MAX_CONNECT_ATTEMPTS, address, exc,
            )
            if attempt < MAX_CONNECT_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY * attempt)

    raise last_error  # type: ignore[misc]


# ---------------------------------------------------------------------------
# GATT client wrapper
# ---------------------------------------------------------------------------

class BleakGattClient:
    """Wraps bleak's BleakClient to match the GattClient protocol.

    Provides the ``read_characteristic``, ``write_characteristic``,
    ``start_notify``, and ``stop_notify`` methods that
    :class:`HapSession` expects.
    """

    def __init__(self, client) -> None:
        """Initialize with a connected BleakClient.

        Args:
            client: A connected ``bleak.BleakClient`` instance.
        """
        self._client = client

    @property
    def is_connected(self) -> bool:
        """True if the underlying BLE connection is active."""
        return self._client.is_connected

    async def read_characteristic(self, uuid: str) -> bytes:
        """Read a GATT characteristic value.

        Args:
            uuid: Characteristic UUID string.

        Returns:
            Raw value bytes.
        """
        data: bytearray = await self._client.read_gatt_char(uuid)
        return bytes(data)

    async def write_characteristic(
        self,
        uuid: str,
        data: bytes,
        response: bool = True,
    ) -> None:
        """Write data to a GATT characteristic.

        Args:
            uuid: Characteristic UUID string.
            data: Bytes to write.
            response: If True, wait for a write response (default).
        """
        await self._client.write_gatt_char(uuid, data, response=response)

    async def start_notify(
        self,
        uuid: str,
        callback: Callable[[int, bytearray], None],
    ) -> None:
        """Subscribe to GATT notifications.

        Args:
            uuid: Characteristic UUID string.
            callback: Called with ``(handle, data)`` on each notification.
        """
        await self._client.start_notify(uuid, callback)

    async def stop_notify(self, uuid: str) -> None:
        """Unsubscribe from GATT notifications.

        Args:
            uuid: Characteristic UUID string.
        """
        await self._client.stop_notify(uuid)

    async def disconnect(self) -> None:
        """Disconnect the BLE connection."""
        await self._client.disconnect()
        logger.info("BLE disconnected")


# ---------------------------------------------------------------------------
# Internal: advertisement parsing
# ---------------------------------------------------------------------------

def _parse_homekit_advertisement(
    address: str,
    name: Optional[str],
    rssi: int,
    apple_data: bytes,
) -> Optional[HapAdvertisement]:
    """Parse Apple manufacturer data for HomeKit sub-type.

    Apple manufacturer data contains multiple sub-messages, each with
    a type byte and length.  We scan for type 0x06 (HomeKit).

    HomeKit sub-message format (13 bytes)::

        type(1) | length(1) | status_flags(1) | device_id(6) |
        category(2 LE) | gsn(2 LE) | config_number(1) | compat_version(1)

    Args:
        address: BLE device address.
        name: Advertised local name.
        rssi: Signal strength.
        apple_data: Raw bytes from manufacturer_data[0x004C].

    Returns:
        Parsed :class:`HapAdvertisement` or None if no HomeKit data.
    """
    # Scan for HomeKit sub-message within Apple data.
    pos: int = 0
    while pos < len(apple_data) - 1:
        sub_type: int = apple_data[pos]
        sub_len: int = apple_data[pos + 1]
        pos += 2

        if sub_type == HOMEKIT_TYPE:
            return _parse_homekit_payload(
                address, name, rssi, apple_data[pos:pos + sub_len]
            )

        pos += sub_len

    return None


def _parse_homekit_payload(
    address: str,
    name: Optional[str],
    rssi: int,
    payload: bytes,
) -> Optional[HapAdvertisement]:
    """Parse the 13-byte HomeKit sub-message payload.

    Args:
        address: BLE device address.
        name: Advertised local name.
        rssi: Signal strength.
        payload: HomeKit sub-message bytes (after type and length).

    Returns:
        Parsed :class:`HapAdvertisement` or None if payload is too short.
    """
    # Minimum HomeKit payload: status(1) + device_id(6) + category(2) +
    # gsn(2) + config(1) + compat(1) = 13 bytes.
    HOMEKIT_PAYLOAD_MIN_LEN: int = 13

    if len(payload) < HOMEKIT_PAYLOAD_MIN_LEN:
        logger.debug(
            "HomeKit payload too short (%d bytes) from %s",
            len(payload), address,
        )
        return None

    status_flags: int = payload[0]
    device_id: bytes = payload[1:7]
    category: int = struct.unpack_from("<H", payload, 7)[0]
    gsn: int = struct.unpack_from("<H", payload, 9)[0]
    config_number: int = payload[11]

    # Bit 0 of status_flags: 1 = paired, 0 = not paired (accepting pairing).
    pairing_available: bool = not bool(status_flags & 0x01)

    category_name: str = CATEGORY_NAMES.get(category, f"Unknown ({category})")

    return HapAdvertisement(
        address=address,
        name=name,
        rssi=rssi,
        category=category,
        category_name=category_name,
        state_number=gsn,
        status_flags=status_flags,
        device_id=device_id,
        config_number=config_number,
        pairing_available=pairing_available,
    )
