"""Persistent MAC-based device identity registry for LIFX devices.

Maps MAC addresses to user-defined labels that survive DHCP reassignment,
router swaps, and power cycles.  The registry file lives outside the git
repo (default ``/etc/glowup/device_registry.json``) so it is never
overwritten by code updates.

Runtime IP resolution is performed via the :class:`BulbKeepAlive` daemon's
ARP table, making IP addresses a transient implementation detail rather
than a configuration input.

Typical usage::

    from device_registry import DeviceRegistry
    from bulb_keepalive import BulbKeepAlive

    registry = DeviceRegistry()
    registry.load()  # reads /etc/glowup/device_registry.json

    label = registry.mac_to_label("d0:73:d5:69:70:db")
    mac   = registry.label_to_mac("porch-left")
    ip    = registry.resolve_to_ip("porch-left", keepalive_daemon)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from infrastructure.bulb_keepalive import BulbKeepAlive

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default path to the device registry file (outside the repo).
DEFAULT_REGISTRY_PATH: str = "/etc/glowup/device_registry.json"

#: Environment variable that overrides the default registry path.
ENV_REGISTRY_PATH: str = "GLOWUP_DEVICE_REGISTRY"

#: Maximum label length in bytes (LIFX firmware limit for SetLabel).
MAX_LABEL_BYTES: int = 32

#: Regex matching a valid lowercase colon-separated MAC address.
MAC_PATTERN: re.Pattern[str] = re.compile(
    r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$"
)

#: Regex matching an IPv4 address (simple — not a full RFC validator).
IP_PATTERN: re.Pattern[str] = re.compile(
    r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$"
)

#: LIFX OUI prefix — all LIFX devices share this.
LIFX_OUI: str = "d0:73:d5"

# Module logger.
logger: logging.Logger = logging.getLogger("glowup.device_registry")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class DeviceRegistry:
    """Persistent MAC-to-label device identity registry.

    Thread-safe.  Loaded once at startup, provides O(1) lookups in both
    directions (MAC→label, label→MAC).  Runtime IP resolution is delegated
    to the :class:`BulbKeepAlive` daemon's ARP table.

    The registry file format::

        {
            "devices": {
                "d0:73:d5:69:70:db": {
                    "label": "porch-left",
                    "notes": "optional human notes"
                }
            }
        }

    Attributes:
        path: Filesystem path to the loaded registry file.
    """

    def __init__(self) -> None:
        self.path: Optional[str] = None
        self._devices: dict[str, dict[str, Any]] = {}   # MAC → entry
        self._label_to_mac: dict[str, str] = {}          # label → MAC
        self._lock: threading.Lock = threading.Lock()
        # Serializes file I/O (load/save) to prevent concurrent
        # read-modify-write races.  Separate from _lock so lookups
        # don't block on disk I/O.
        self._io_lock: threading.Lock = threading.Lock()

    # -- Loading -----------------------------------------------------------

    def load(self, path: Optional[str] = None) -> bool:
        """Read and validate the registry file.

        Args:
            path: Explicit path, or ``None`` to use the env var /
                  default location.

        Returns:
            ``True`` if the file was loaded successfully, ``False`` if
            the file does not exist (first-run scenario).

        Raises:
            ValueError: If the file exists but contains invalid data
                        (duplicate labels, malformed MACs, etc.).
        """
        resolved: str = path or os.environ.get(
            ENV_REGISTRY_PATH, DEFAULT_REGISTRY_PATH
        )
        self.path = resolved

        if not Path(resolved).exists():
            logger.info(
                "No device registry at %s — running in legacy IP-only mode",
                resolved,
            )
            return False

        # Serialize all file I/O to prevent concurrent load/save races.
        with self._io_lock:
            with open(resolved, "r", encoding="utf-8") as fh:
                raw: dict[str, Any] = json.load(fh)

            devices_raw: dict[str, Any] = raw.get("devices", {})
            devices: dict[str, dict[str, Any]] = {}
            label_to_mac: dict[str, str] = {}

            for mac_raw, entry in devices_raw.items():
                mac: str = mac_raw.strip().lower()

                # Validate MAC format.
                if not MAC_PATTERN.match(mac):
                    raise ValueError(
                        f"Invalid MAC address in registry: {mac_raw!r}"
                    )

                # Note: OUI check removed — registry now tracks all
                # network devices (cameras, printers, receivers), not
                # only LIFX bulbs.

                # Validate label.
                label: str = entry.get("label", "").strip()
                if not label:
                    raise ValueError(
                        f"Device {mac} has no label in registry"
                    )

                # Enforce LIFX firmware label byte limit.
                label_bytes: int = len(label.encode("utf-8"))
                if label_bytes > MAX_LABEL_BYTES:
                    raise ValueError(
                        f"Label {label!r} for {mac} is {label_bytes} bytes "
                        f"(max {MAX_LABEL_BYTES})"
                    )

                # Enforce label uniqueness.
                label_lower: str = label.lower()
                if label_lower in label_to_mac:
                    existing_mac: str = label_to_mac[label_lower]
                    raise ValueError(
                        f"Duplicate label {label!r} for {mac} and "
                        f"{existing_mac}"
                    )

                devices[mac] = entry
                devices[mac]["label"] = label  # normalized
                label_to_mac[label_lower] = mac

            with self._lock:
                self._devices = devices
                self._label_to_mac = label_to_mac

        logger.info(
            "Loaded device registry: %d device(s) from %s",
            len(devices), resolved,
        )
        return True

    # -- Lookups -----------------------------------------------------------

    def mac_to_label(self, mac: str) -> Optional[str]:
        """Return the label for a MAC address, or ``None`` if not registered.

        Args:
            mac: Lowercase colon-separated MAC (e.g. ``d0:73:d5:69:70:db``).
        """
        with self._lock:
            entry = self._devices.get(mac.lower())
            return entry["label"] if entry else None

    def label_to_mac(self, label: str) -> Optional[str]:
        """Return the MAC for a label, or ``None`` if not registered.

        Args:
            label: Case-insensitive device label.
        """
        with self._lock:
            return self._label_to_mac.get(label.lower())

    def all_devices(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot of the full registry (MAC → entry dict)."""
        with self._lock:
            return dict(self._devices)

    def ip_to_label(
        self,
        ip: str,
        keepalive: Optional["BulbKeepAlive"] = None,
    ) -> Optional[str]:
        """Return the registered label for a device at a given IP.

        Uses the keepalive daemon's ARP table to resolve IP → MAC,
        then looks up the MAC in the registry.  This is the reverse
        lookup path for devices that are offline or query-silent.

        Args:
            ip:        Device IP address (e.g. ``10.0.0.164``).
            keepalive: Optional keepalive daemon for IP → MAC resolution.

        Returns:
            The registered label, or ``None`` if unresolvable.
        """
        if keepalive is not None:
            bulbs: dict[str, str] = keepalive.known_bulbs
            mac: Optional[str] = bulbs.get(ip)
            if mac is not None:
                return self.mac_to_label(mac)
        # Fallback: scan registry entries for a stored IP match.
        # Covers offline devices registered with --offline that
        # recorded their IP in the entry.
        with self._lock:
            for _mac, entry in self._devices.items():
                if entry.get("ip") == ip:
                    return entry.get("label")
        return None

    def is_known_mac(self, mac: str) -> bool:
        """Return ``True`` if the MAC is registered."""
        with self._lock:
            return mac.lower() in self._devices

    @property
    def device_count(self) -> int:
        """Number of devices in the registry."""
        with self._lock:
            return len(self._devices)

    @property
    def is_loaded(self) -> bool:
        """Whether the registry has been loaded (even if empty)."""
        return self.path is not None

    # -- Resolution --------------------------------------------------------

    def resolve_identifier(
        self, identifier: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Resolve any identifier (IP, MAC, or label) to a (mac, label) pair.

        Args:
            identifier: An IP address, MAC address, or label string.

        Returns:
            Tuple of (mac, label).  Either or both may be ``None`` if
            the identifier cannot be resolved through the registry.
        """
        ident: str = identifier.strip().lower()

        # Is it a MAC address?
        if MAC_PATTERN.match(ident):
            label = self.mac_to_label(ident)
            return (ident, label)

        # Is it a label?
        mac = self.label_to_mac(ident)
        if mac is not None:
            return (mac, self.mac_to_label(mac))

        # Is it an IP?  We can't resolve to MAC without the ARP table.
        if IP_PATTERN.match(ident):
            return (None, None)

        return (None, None)

    def resolve_to_ip(
        self,
        identifier: str,
        keepalive: "BulbKeepAlive",
    ) -> Optional[str]:
        """Resolve any identifier (IP, MAC, or label) to a live IP address.

        Uses the keepalive daemon's ARP table for MAC→IP resolution.

        Args:
            identifier: An IP address, MAC address, or label string.
            keepalive:  Running :class:`BulbKeepAlive` instance.

        Returns:
            The current IP address, or ``None`` if the device is
            offline or unresolvable.
        """
        ident: str = identifier.strip().lower()

        # Direct IP — pass through.
        if IP_PATTERN.match(ident):
            return identifier.strip()

        # Resolve to MAC first.
        mac: Optional[str] = None
        if MAC_PATTERN.match(ident):
            mac = ident
        else:
            mac = self.label_to_mac(ident)

        if mac is None:
            logger.debug("Cannot resolve identifier %r to MAC", identifier)
            return None

        # Look up MAC in the ARP table.
        return keepalive.ip_for_mac(mac)

    # -- Persistence -------------------------------------------------------

    def save(self, path: Optional[str] = None) -> None:
        """Write the current registry state to disk.

        Args:
            path: Target path, or ``None`` to use the loaded path.

        Raises:
            RuntimeError: If no path is available.
        """
        target: str = path or self.path or ""
        if not target:
            raise RuntimeError(
                "No registry path — call load() first or pass a path"
            )

        # Serialize all file I/O to prevent concurrent load/save races.
        with self._io_lock:
            with self._lock:
                data: dict[str, Any] = {
                    "_comment": (
                        "MAC-based device identity.  Survives DHCP changes "
                        "and git pulls.  Do not edit while server is running."
                    ),
                    "devices": dict(self._devices),
                }

            # Atomic write via temp file.
            tmp: str = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=4, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, target)

        logger.info("Saved device registry: %d device(s) to %s",
                     len(self._devices), target)

    def add_device(
        self,
        mac: str,
        label: str,
        notes: str = "",
        force: bool = False,
        ip: str = "",
    ) -> None:
        """Add or update a device in the registry.

        Args:
            mac:   Lowercase colon-separated MAC address.
            label: User-defined label (max 32 bytes UTF-8).
            notes: Optional human-readable notes.
            force: If ``True``, reassign the label from its current
                   MAC to the new one instead of raising on collision.
            ip:    Optional last-known IP address.  Stored so offline
                   devices can be resolved by IP without ARP.

        Raises:
            ValueError: If the MAC or label is invalid, or the label
                        is already in use by a different device (and
                        *force* is ``False``).
        """
        mac = mac.strip().lower()
        label = label.strip()

        if not MAC_PATTERN.match(mac):
            raise ValueError(f"Invalid MAC address: {mac!r}")

        if not label:
            raise ValueError("Label cannot be empty")

        label_bytes: int = len(label.encode("utf-8"))
        if label_bytes > MAX_LABEL_BYTES:
            raise ValueError(
                f"Label {label!r} is {label_bytes} bytes "
                f"(max {MAX_LABEL_BYTES})"
            )

        with self._lock:
            # Check for label collision with a different MAC.
            existing_mac: Optional[str] = self._label_to_mac.get(
                label.lower()
            )
            if existing_mac is not None and existing_mac != mac:
                if not force:
                    raise ValueError(
                        f"Label {label!r} is already assigned to {existing_mac}"
                    )
                # Force: remove the label from the old MAC.
                old_entry = self._devices.get(existing_mac)
                if old_entry:
                    self._devices.pop(existing_mac)
                self._label_to_mac.pop(label.lower(), None)
                logger.info(
                    "Force: reassigned label %r from %s to %s",
                    label, existing_mac, mac,
                )

            # Remove old label mapping if this MAC had a different label.
            old_entry = self._devices.get(mac)
            if old_entry:
                old_label: str = old_entry.get("label", "")
                if old_label and old_label.lower() != label.lower():
                    self._label_to_mac.pop(old_label.lower(), None)

            entry: dict[str, Any] = {"label": label}
            if notes:
                entry["notes"] = notes
            if ip:
                entry["ip"] = ip
            self._devices[mac] = entry
            self._label_to_mac[label.lower()] = mac

    def remove_device(self, identifier: str) -> bool:
        """Remove a device by MAC or label.

        Args:
            identifier: MAC address or label.

        Returns:
            ``True`` if a device was removed, ``False`` if not found.
        """
        ident: str = identifier.strip().lower()

        with self._lock:
            # Try as MAC.
            if ident in self._devices:
                entry = self._devices.pop(ident)
                label = entry.get("label", "")
                if label:
                    self._label_to_mac.pop(label.lower(), None)
                return True

            # Try as label.
            mac = self._label_to_mac.get(ident)
            if mac:
                self._devices.pop(mac, None)
                self._label_to_mac.pop(ident, None)
                return True

        return False

    # -- Display -----------------------------------------------------------

    def format_table(
        self,
        keepalive: Optional["BulbKeepAlive"] = None,
    ) -> str:
        """Format the registry as a human-readable table.

        If a keepalive daemon is provided, includes the current IP and
        online status for each device.

        Args:
            keepalive: Optional running :class:`BulbKeepAlive` instance.

        Returns:
            Multi-line string suitable for terminal display.
        """
        with self._lock:
            devices = dict(self._devices)

        if not devices:
            return "(empty registry)"

        # Build reverse ARP lookup if available.
        mac_to_ip: dict[str, str] = {}
        if keepalive is not None:
            mac_to_ip = keepalive.known_bulbs_by_mac

        lines: list[str] = []
        header: str = (
            f"{'MAC Address':19}  {'Label':24}  {'IP Address':15}  "
            f"{'Status':8}  {'Notes'}"
        )
        lines.append(header)
        lines.append("=" * len(header))

        for mac in sorted(devices.keys()):
            entry = devices[mac]
            label: str = entry.get("label", "?")
            notes: str = entry.get("notes", "")
            ip: str = mac_to_ip.get(mac, "-")
            status: str = "online" if ip != "-" else "offline"
            lines.append(
                f"{mac:19}  {label:24}  {ip:15}  {status:8}  {notes}"
            )

        return "\n".join(lines)
