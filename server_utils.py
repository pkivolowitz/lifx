"""Server utility functions — device ID helpers, IP validation, rate limiter.

Pure functions and small classes with no dependencies on server state.
Extracted from server.py for modularity.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import ipaddress
import threading
import time
from typing import Any

from server_constants import GROUP_PREFIX, GRID_PREFIX

# ---------------------------------------------------------------------------
# IP validation
# ---------------------------------------------------------------------------


def validate_ip(addr: str) -> bool:
    """Return True if *addr* is a valid IPv4 address string.

    Args:
        addr: String to validate.

    Returns:
        ``True`` if the string is a syntactically valid IPv4 address.
    """
    try:
        ipaddress.IPv4Address(addr)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Group ID helpers
# ---------------------------------------------------------------------------


def is_group_id(device_id: str) -> bool:
    """Return ``True`` if *device_id* is a group identifier."""
    return device_id.startswith(GROUP_PREFIX)


def group_name_from_id(device_id: str) -> str:
    """Extract the group name from a ``group:name`` identifier."""
    return device_id[len(GROUP_PREFIX):]


def group_id_from_name(group_name: str) -> str:
    """Build a ``group:name`` identifier from a group name."""
    return GROUP_PREFIX + group_name


# ---------------------------------------------------------------------------
# Grid ID helpers
# ---------------------------------------------------------------------------


def is_grid_id(device_id: str) -> bool:
    """Return ``True`` if *device_id* is a grid identifier."""
    return device_id.startswith(GRID_PREFIX)


def grid_name_from_id(device_id: str) -> str:
    """Extract the grid name from a ``grid:name`` identifier."""
    return device_id[len(GRID_PREFIX):]


def grid_id_from_name(grid_name: str) -> str:
    """Build a ``grid:name`` identifier from a grid name."""
    return GRID_PREFIX + grid_name


# ---------------------------------------------------------------------------
# Device ID validation
# ---------------------------------------------------------------------------


def validate_device_id(device_id: str) -> bool:
    """Check whether a device identifier is structurally valid.

    Accepts raw IPv4 addresses, ``group:name``, and ``grid:name``
    identifiers.

    Args:
        device_id: Identifier string from the API path.

    Returns:
        ``True`` if the identifier is a valid IP, group, or grid key.
    """
    if is_group_id(device_id):
        return len(device_id) > len(GROUP_PREFIX)
    if is_grid_id(device_id):
        return len(device_id) > len(GRID_PREFIX)
    return validate_ip(device_id)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Track failed authentication attempts per IP.

    Thread-safe.  Records failure timestamps and blocks IPs that
    exceed the configured threshold within the time window.

    Args:
        max_failures: Maximum failures before blocking.
        window_seconds: Rolling time window in seconds.
    """

    def __init__(
        self, max_failures: int = 10, window_seconds: int = 60,
    ) -> None:
        self._max: int = max_failures
        self._window: int = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock: threading.Lock = threading.Lock()

    def record_failure(self, ip: str) -> None:
        """Record a failed authentication attempt.

        Args:
            ip: Client IP address.
        """
        now: float = time.time()
        with self._lock:
            times: list[float] = self._failures.setdefault(ip, [])
            times.append(now)
            # Prune old entries outside the window.
            cutoff: float = now - self._window
            self._failures[ip] = [t for t in times if t > cutoff]

    def is_blocked(self, ip: str) -> bool:
        """Check whether an IP is currently rate-limited.

        Args:
            ip: Client IP address.

        Returns:
            ``True`` if the IP has exceeded the failure threshold.
        """
        now: float = time.time()
        with self._lock:
            times: list[float] = self._failures.get(ip, [])
            cutoff: float = now - self._window
            recent: list[float] = [t for t in times if t > cutoff]
            self._failures[ip] = recent
            return len(recent) >= self._max

    def clear(self, ip: str) -> None:
        """Clear all failure records for an IP.

        Args:
            ip: Client IP address.
        """
        with self._lock:
            self._failures.pop(ip, None)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def get_groups(config: dict[str, Any]) -> dict[str, list[str]]:
    """Extract device groups from config, excluding comment keys.

    Args:
        config: Parsed configuration dictionary.

    Returns:
        A dict mapping group names to lists of identifiers (labels,
        MACs, or IPs).
    """
    groups: dict = config.get("groups", {})
    return {
        name: entries
        for name, entries in groups.items()
        if not name.startswith("_")
    }
