"""BLE device registry — persists labels, types, and long-term keys.

Maps BLE device addresses to human-readable labels and stores the
HAP long-term keys needed for pair-verify.  Backed by
``ble_pairing.json`` (gitignored — contains secrets).

Schema::

    {
        "controller": {
            "ltsk": "<hex>",
            "ltpk": "<hex>"
        },
        "devices": {
            "hallway_motion": {
                "address": "AA:BB:CC:DD:EE:FF",
                "type": "motion",
                "setup_code": "164-77-432",
                "accessory_ltpk": "<hex>",
                "accessory_pairing_id": "...",
                "paired": true
            }
        }
    }

The ``controller`` section holds GlowUp's own Ed25519 key pair,
generated once on first pair-setup and reused for all accessories.

Device labels are the dict keys.  The ``address`` field is the BLE
MAC (Linux) or CoreBluetooth UUID (macOS).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .hap_session import PairingKeys

logger: logging.Logger = logging.getLogger("glowup.ble.registry")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default path relative to the project root.
DEFAULT_REGISTRY_PATH: str = "ble_pairing.json"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BleDeviceEntry:
    """A single device in the registry.

    Attributes:
        label: Human-readable name (e.g., ``"hallway_motion"``).
        address: BLE address (MAC on Linux, UUID on macOS).
        device_type: Sensor type (``"motion"``, ``"contact"``, ``"button"``).
        setup_code: The HAP setup code (kept for re-pairing).
        accessory_ltpk: Accessory's Ed25519 public key (hex, or None if unpaired).
        accessory_pairing_id: Accessory's pairing identifier (or None).
        paired: Whether pair-setup has been completed.
    """

    label: str
    address: str = ""
    device_type: str = "motion"
    setup_code: str = ""
    accessory_ltpk: Optional[str] = None
    accessory_pairing_id: Optional[str] = None
    paired: bool = False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class BleRegistry:
    """Persistent BLE device registry backed by a JSON file.

    Thread-safe for reads.  Writes are atomic (write-to-temp + rename).
    """

    def __init__(self, path: Optional[str] = None) -> None:
        """Initialize the registry.

        Args:
            path: Path to the JSON file.  Defaults to
                ``ble_pairing.json`` in the current directory.
        """
        self._path: Path = Path(path or DEFAULT_REGISTRY_PATH)
        # Serializes file I/O to prevent concurrent load/save races.
        self._io_lock: threading.Lock = threading.Lock()
        self._data: dict = self._load()

    def _load(self) -> dict:
        """Load the registry from disk, or return an empty structure."""
        if not self._path.exists():
            logger.info("No BLE registry at %s — starting fresh", self._path)
            return {"controller": {}, "devices": {}}

        try:
            with open(self._path, "r") as f:
                data: dict = json.load(f)
            logger.info(
                "Loaded BLE registry: %d device(s)", len(data.get("devices", {}))
            )
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load BLE registry: %s", exc)
            return {"controller": {}, "devices": {}}

    def save(self) -> None:
        """Persist the registry to disk atomically."""
        with self._io_lock:
            tmp_path: Path = self._path.with_suffix(".tmp")
            try:
                with open(tmp_path, "w") as f:
                    json.dump(self._data, f, indent=4)
                    f.write("\n")
                tmp_path.replace(self._path)
                logger.debug("BLE registry saved to %s", self._path)
            except OSError as exc:
                logger.error("Failed to save BLE registry: %s", exc)
                raise

    # --- Controller keys ---

    def get_controller_keys(self) -> Optional[tuple[bytes, bytes]]:
        """Return the controller's (ltsk, ltpk) or None if not generated.

        Returns:
            Tuple of (private_key_bytes, public_key_bytes), or None.
        """
        ctrl: dict = self._data.get("controller", {})
        ltsk_hex: Optional[str] = ctrl.get("ltsk")
        ltpk_hex: Optional[str] = ctrl.get("ltpk")
        if ltsk_hex and ltpk_hex:
            return bytes.fromhex(ltsk_hex), bytes.fromhex(ltpk_hex)
        return None

    def set_controller_keys(self, ltsk: bytes, ltpk: bytes) -> None:
        """Store the controller's Ed25519 key pair.

        Args:
            ltsk: 32-byte private key.
            ltpk: 32-byte public key.
        """
        self._data["controller"] = {
            "ltsk": ltsk.hex(),
            "ltpk": ltpk.hex(),
        }
        self.save()
        logger.info("Controller Ed25519 key pair stored")

    # --- Device entries ---

    def get_device(self, label: str) -> Optional[BleDeviceEntry]:
        """Look up a device by label.

        Args:
            label: Human-readable device name.

        Returns:
            :class:`BleDeviceEntry` or None if not found.
        """
        devices: dict = self._data.get("devices", {})
        entry: Optional[dict] = devices.get(label)
        if entry is None:
            return None
        return BleDeviceEntry(
            label=label,
            address=entry.get("address", ""),
            device_type=entry.get("type", "motion"),
            setup_code=entry.get("setup_code", ""),
            accessory_ltpk=entry.get("accessory_ltpk"),
            accessory_pairing_id=entry.get("accessory_pairing_id"),
            paired=entry.get("paired", False),
        )

    def get_all_devices(self) -> list[BleDeviceEntry]:
        """Return all registered devices."""
        devices: dict = self._data.get("devices", {})
        return [
            BleDeviceEntry(
                label=label,
                address=entry.get("address", ""),
                device_type=entry.get("type", "motion"),
                setup_code=entry.get("setup_code", ""),
                accessory_ltpk=entry.get("accessory_ltpk"),
                accessory_pairing_id=entry.get("accessory_pairing_id"),
                paired=entry.get("paired", False),
            )
            for label, entry in devices.items()
        ]

    def get_paired_devices(self) -> list[BleDeviceEntry]:
        """Return only devices that have completed pair-setup."""
        return [d for d in self.get_all_devices() if d.paired]

    def add_device(
        self,
        label: str,
        address: str,
        device_type: str = "motion",
        setup_code: str = "",
    ) -> None:
        """Register a new device (pre-pairing).

        Args:
            label: Human-readable name.
            address: BLE address.
            device_type: Sensor type.
            setup_code: HAP setup code for pairing.
        """
        if "devices" not in self._data:
            self._data["devices"] = {}

        self._data["devices"][label] = {
            "address": address,
            "type": device_type,
            "setup_code": setup_code,
            "paired": False,
        }
        self.save()
        logger.info("Registered BLE device: %s (%s)", label, address)

    def mark_paired(
        self,
        label: str,
        keys: PairingKeys,
    ) -> None:
        """Update a device entry after successful pair-setup.

        Args:
            label: Device label.
            keys: Long-term keys from pair-setup.
        """
        entry: dict = self._data["devices"][label]
        entry["accessory_ltpk"] = keys.accessory_ltpk.hex()
        entry["accessory_pairing_id"] = keys.accessory_pairing_id.decode(
            "utf-8", errors="replace"
        )
        entry["paired"] = True

        # Also store/update the controller keys if not already present.
        if not self._data.get("controller", {}).get("ltsk"):
            self.set_controller_keys(keys.controller_ltsk, keys.controller_ltpk)

        self.save()
        logger.info("Device %s marked as paired", label)

    def get_pairing_keys(self, label: str) -> Optional[PairingKeys]:
        """Reconstruct PairingKeys for a paired device.

        Args:
            label: Device label.

        Returns:
            :class:`PairingKeys` or None if not paired or keys missing.
        """
        entry: Optional[BleDeviceEntry] = self.get_device(label)
        if entry is None or not entry.paired:
            return None

        ctrl_keys: Optional[tuple[bytes, bytes]] = self.get_controller_keys()
        if ctrl_keys is None:
            logger.error("Controller keys missing — cannot build PairingKeys")
            return None

        if entry.accessory_ltpk is None or entry.accessory_pairing_id is None:
            logger.error("Accessory keys missing for device %s", label)
            return None

        ltsk, ltpk = ctrl_keys
        return PairingKeys(
            controller_ltsk=ltsk,
            controller_ltpk=ltpk,
            accessory_ltpk=bytes.fromhex(entry.accessory_ltpk),
            accessory_pairing_id=entry.accessory_pairing_id.encode("utf-8"),
        )

    def find_by_address(self, address: str) -> Optional[BleDeviceEntry]:
        """Look up a device by BLE address.

        Args:
            address: BLE MAC or CoreBluetooth UUID.

        Returns:
            :class:`BleDeviceEntry` or None.
        """
        for device in self.get_all_devices():
            if device.address.upper() == address.upper():
                return device
        return None

    def remove_device(self, label: str) -> bool:
        """Remove a device from the registry.

        Args:
            label: Device label to remove.

        Returns:
            True if the device was found and removed.
        """
        if label in self._data.get("devices", {}):
            del self._data["devices"][label]
            self.save()
            logger.info("Removed BLE device: %s", label)
            return True
        return False
