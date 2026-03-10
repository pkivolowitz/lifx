"""GlowUp REST API server — remote control daemon for LIFX devices.

Provides an HTTP API for discovering, querying, and controlling LIFX
devices from anywhere.  Designed to be the single daemon running on a
Raspberry Pi (or Mac), this server subsumes the role of the standalone
scheduler by managing effects directly through the :class:`Controller`
API instead of spawning subprocesses.

Architecture::

    iPhone App (SwiftUI)
        ↓ HTTPS
    Cloudflare Edge (TLS termination)
        ↓ encrypted tunnel
    cloudflared on Pi
        ↓ localhost
    server.py (this file)
        ↓ in-process
    Controller / Engine / Transport
        ↓ UDP
    LIFX Devices

Endpoints::

    GET  /api/devices                    List discovered devices
    GET  /api/effects                    List effects with param metadata
    GET  /api/devices/{ip}/status        Current effect and params
    GET  /api/devices/{ip}/colors        Snapshot of zone HSBK values
    GET  /api/devices/{ip}/colors/stream SSE stream at 4 Hz
    POST /api/devices/{ip}/play          Start an effect
    POST /api/devices/{ip}/stop          Stop current effect
    POST /api/devices/{ip}/power         Turn device on/off
    POST /api/devices/{ip}/identify      Pulse brightness to locate device
    POST /api/devices/{ip}/nickname      Set a custom display name
    GET  /api/schedule                   Schedule entries with resolved times
    POST /api/schedule/{index}/enabled   Enable or disable a schedule entry
    POST /api/discover                   Re-run device discovery

Usage::

    python3 server.py server.json
    python3 server.py --dry-run server.json   # preview schedule
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "1.4"

import hmac
import http.server
import ipaddress
import json
import logging
import math
import os
import re
import signal
import socketserver
import sys
import threading
import time as time_mod
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from effects import get_registry, create_effect, HSBK_MAX, KELVIN_DEFAULT
from engine import Controller
from solar import SunTimes, sun_times
from transport import LifxDevice, discover_devices

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default HTTP port for the REST API.
DEFAULT_PORT: int = 8420

# Server-Sent Events polling rate (Hz) for live color streaming.
SSE_POLL_HZ: float = 4.0

# Computed interval between SSE polls (seconds).
SSE_POLL_INTERVAL: float = 1.0 / SSE_POLL_HZ

# Device discovery timeout (seconds).
DISCOVERY_TIMEOUT: float = 5.0

# Number of broadcast discovery attempts before falling back to direct IPs.
DISCOVERY_RETRIES: int = 3

# Maximum allowed size of an HTTP request body (bytes).
MAX_REQUEST_BODY: int = 65536

# How often the scheduler thread checks for schedule transitions (seconds).
SCHEDULER_POLL_SECONDS: int = 30

# Default fade-to-black duration when stopping an effect (ms).
DEFAULT_FADE_MS: int = 500

# HTTP Authorization header name.
AUTH_HEADER: str = "Authorization"

# Bearer token prefix in the Authorization header.
BEARER_PREFIX: str = "Bearer "

# Default configuration file path (for Pi deployment).
DEFAULT_CONFIG_PATH: str = "/etc/glowup/server.json"

# Logging format matching scheduler.py.
LOG_FORMAT: str = "%(asctime)s %(levelname)s %(message)s"
LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"

# Regex for symbolic time specifications (reused from scheduler.py).
_SYMBOLIC_RE: re.Pattern[str] = re.compile(
    r"^(sunrise|sunset|dawn|dusk|noon|midnight)"
    r"(?:([+-])"
    r"(?:(\d+)h)?"
    r"(?:(\d+)m)?"
    r")?$"
)

# Regex for fixed HH:MM time specifications.
_FIXED_TIME_RE: re.Pattern[str] = re.compile(r"^(\d{1,2}):(\d{2})$")

# Valid hours range for fixed time specs.
MAX_HOUR: int = 23
MAX_MINUTE: int = 59

# API path prefix.
API_PREFIX: str = "/api"

# Maximum failed authentication attempts per IP before throttling.
AUTH_RATE_LIMIT: int = 10

# Time window for the auth rate limiter (seconds).
AUTH_RATE_WINDOW: int = 60

# SSE stream timeout — close idle connections after this many seconds.
SSE_TIMEOUT_SECONDS: float = 3600.0

# Identify pulse duration (seconds).
IDENTIFY_DURATION_SECONDS: float = 10.0

# Seconds per full brightness cycle during identify.
IDENTIFY_CYCLE_SECONDS: float = 3.0

# Seconds between brightness updates during identify (20 fps).
IDENTIFY_FRAME_INTERVAL: float = 0.05

# Minimum brightness fraction during identify pulse (5%).
IDENTIFY_MIN_BRI: float = 0.05

# Day-of-week letter to weekday index (Monday=0 .. Sunday=6).
# Matches Python's date.weekday() convention.
DAY_LETTER_TO_WEEKDAY: dict[str, int] = {
    "M": 0, "T": 1, "W": 2, "R": 3, "F": 4, "S": 5, "U": 6,
}

# All valid day letters (for validation).
VALID_DAY_LETTERS: str = "MTWRFSU"


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Simple per-IP rate limiter for authentication failures.

    Tracks failed auth attempts within a sliding time window.  When a
    client exceeds :data:`AUTH_RATE_LIMIT` failures within
    :data:`AUTH_RATE_WINDOW` seconds, further requests are rejected
    with HTTP 429 until the window expires.
    """

    def __init__(self, limit: int = AUTH_RATE_LIMIT,
                 window: int = AUTH_RATE_WINDOW) -> None:
        self._limit: int = limit
        self._window: int = window
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._lock: threading.Lock = threading.Lock()

    def record_failure(self, ip: str) -> None:
        """Record a failed authentication attempt from *ip*."""
        now: float = time_mod.time()
        with self._lock:
            self._failures[ip].append(now)

    def is_blocked(self, ip: str) -> bool:
        """Return ``True`` if *ip* has exceeded the failure limit."""
        now: float = time_mod.time()
        cutoff: float = now - self._window
        with self._lock:
            attempts: list[float] = self._failures.get(ip, [])
            # Prune old entries.
            attempts[:] = [t for t in attempts if t > cutoff]
            return len(attempts) >= self._limit

    def clear(self, ip: str) -> None:
        """Clear failure history for *ip* (e.g. after a successful auth)."""
        with self._lock:
            self._failures.pop(ip, None)


# Singleton rate limiter — shared across all handler threads.
_rate_limiter: _RateLimiter = _RateLimiter()


# ---------------------------------------------------------------------------
# IP validation
# ---------------------------------------------------------------------------

def _validate_ip(ip_str: str) -> bool:
    """Return ``True`` if *ip_str* is a valid IPv4 or IPv6 address.

    Args:
        ip_str: The string to validate.

    Returns:
        ``True`` if valid, ``False`` otherwise.
    """
    try:
        ipaddress.ip_address(ip_str)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Device manager
# ---------------------------------------------------------------------------

class DeviceManager:
    """Manage discovered LIFX devices and one Controller per device.

    Thread-safe: all public methods acquire the internal lock before
    modifying shared state.  The :class:`Controller` instances themselves
    are already thread-safe.

    Attributes:
        devices:     Dict mapping device IP to :class:`LifxDevice`.
        controllers: Dict mapping device IP to :class:`Controller`.
    """

    def __init__(self, known_ips: Optional[list[str]] = None,
                 nicknames: Optional[dict[str, str]] = None) -> None:
        """Initialize with empty device and controller maps.

        Args:
            known_ips:  Optional list of device IPs from config groups.
                        Used as fallback when broadcast discovery fails
                        (e.g. mesh routers that block broadcast).
            nicknames:  Optional mapping of device IP to user-assigned
                        display name, loaded from the config file.
        """
        self._devices: dict[str, LifxDevice] = {}
        self._controllers: dict[str, Controller] = {}
        self._lock: threading.Lock = threading.Lock()
        # Override tracking: maps device IP to the schedule entry name
        # that was active when the phone took over.  The scheduler clears
        # the override when the active entry changes from this value.
        self._overrides: dict[str, Optional[str]] = {}
        # Known IPs from config groups for fallback direct discovery.
        self._known_ips: list[str] = known_ips or []
        # User-assigned nicknames: IP → display name.
        self._nicknames: dict[str, str] = nicknames or {}

    def discover(self) -> list[dict[str, Any]]:
        """Run LIFX discovery and cache results.

        First attempts broadcast discovery.  If that finds no devices
        and known IPs are configured (from config groups), falls back
        to direct per-IP queries — necessary on networks where mesh
        routers block UDP broadcast between nodes.

        Existing controllers are preserved if their device is still
        present.  Devices that disappear have their controllers stopped
        and sockets closed.

        Returns:
            A list of JSON-serializable device info dicts.
        """
        # Try broadcast discovery with retries.  Mesh routers and
        # congested networks often need multiple attempts.
        new_devices: list[LifxDevice] = []
        for attempt in range(1, DISCOVERY_RETRIES + 1):
            new_devices = discover_devices(timeout=DISCOVERY_TIMEOUT)
            if new_devices:
                break
            if attempt < DISCOVERY_RETRIES:
                logging.info(
                    "Broadcast discovery attempt %d/%d found 0 devices, retrying...",
                    attempt, DISCOVERY_RETRIES,
                )

        # Supplement with direct per-IP queries for any known IPs not
        # already found.  This ensures devices hidden by mesh routers
        # are always reachable when their IPs are in the config.
        if self._known_ips:
            found_ips: set[str] = {d.ip for d in new_devices}
            missing_ips: list[str] = [
                ip for ip in self._known_ips if ip not in found_ips
            ]
            if missing_ips:
                logging.info(
                    "Querying %d known IP(s) not found by broadcast...",
                    len(missing_ips),
                )
                for ip in missing_ips:
                    # Retry direct queries — flaky mesh routers may
                    # drop the first UDP packet.
                    for attempt in range(1, DISCOVERY_RETRIES + 1):
                        try:
                            found: list[LifxDevice] = discover_devices(
                                timeout=DISCOVERY_TIMEOUT,
                                target_ip=ip,
                            )
                            if found:
                                new_devices.extend(found)
                                break
                            if attempt < DISCOVERY_RETRIES:
                                logging.info(
                                    "Direct query %s attempt %d/%d — no response, retrying...",
                                    ip, attempt, DISCOVERY_RETRIES,
                                )
                        except Exception as exc:
                            logging.warning(
                                "Direct discovery %s failed: %s", ip, exc,
                            )
                            break
                    else:
                        logging.warning(
                            "Device %s did not respond after %d attempts",
                            ip, DISCOVERY_RETRIES,
                        )
        new_map: dict[str, LifxDevice] = {d.ip: d for d in new_devices}

        with self._lock:
            # Close sockets for devices that are gone.
            gone_ips: set[str] = set(self._devices) - set(new_map)
            for ip in gone_ips:
                self._stop_and_remove(ip)

            # For newly discovered devices, close any duplicate sockets
            # from the old map (discovery creates new LifxDevice instances
            # with their own sockets).
            for ip, new_dev in new_map.items():
                old_dev: Optional[LifxDevice] = self._devices.get(ip)
                if old_dev is not None and old_dev is not new_dev:
                    # A controller may reference the old device — update it.
                    ctrl: Optional[Controller] = self._controllers.get(ip)
                    if ctrl is not None:
                        # Stop the old controller; the caller can re-play.
                        ctrl.stop(fade_ms=0)
                        del self._controllers[ip]
                    old_dev.close()

            self._devices = new_map

        return self._devices_as_list()

    def get_device(self, ip: str) -> Optional[LifxDevice]:
        """Look up a cached device by IP.

        Args:
            ip: Device IP address.

        Returns:
            The :class:`LifxDevice`, or ``None`` if not found.
        """
        with self._lock:
            return self._devices.get(ip)

    def get_controller(self, ip: str) -> Optional[Controller]:
        """Look up an existing Controller by IP (does not create one).

        Args:
            ip: Device IP address.

        Returns:
            The :class:`Controller`, or ``None`` if none exists.
        """
        with self._lock:
            return self._controllers.get(ip)

    def get_or_create_controller(self, ip: str) -> Optional[Controller]:
        """Get or lazily create a Controller for a device.

        Args:
            ip: Device IP address.

        Returns:
            A :class:`Controller`, or ``None`` if the device is unknown.
        """
        with self._lock:
            if ip not in self._devices:
                return None
            if ip not in self._controllers:
                dev: LifxDevice = self._devices[ip]
                self._controllers[ip] = Controller([dev])
            return self._controllers[ip]

    def play(
        self,
        ip: str,
        effect_name: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Start an effect on a device.

        Args:
            ip:          Device IP address.
            effect_name: Registered effect name.
            params:      Parameter overrides.

        Returns:
            A status dict for the device.

        Raises:
            KeyError: If the device IP is not discovered.
            ValueError: If the effect name is invalid.
        """
        ctrl: Optional[Controller] = self.get_or_create_controller(ip)
        if ctrl is None:
            raise KeyError(f"Unknown device: {ip}")
        # Power on the device before playing.
        dev: Optional[LifxDevice] = self.get_device(ip)
        if dev is not None:
            try:
                dev.set_power(on=True, duration_ms=0)
            except Exception:
                pass
        ctrl.play(effect_name, **params)
        return ctrl.get_status()

    def stop(self, ip: str) -> dict[str, Any]:
        """Stop the current effect on a device.

        Args:
            ip: Device IP address.

        Returns:
            A status dict for the device.

        Raises:
            KeyError: If the device IP is not discovered.
        """
        ctrl: Optional[Controller] = self.get_or_create_controller(ip)
        if ctrl is None:
            raise KeyError(f"Unknown device: {ip}")
        ctrl.stop(fade_ms=DEFAULT_FADE_MS)
        return ctrl.get_status()

    def get_status(self, ip: str) -> dict[str, Any]:
        """Get the current effect status for a device.

        Args:
            ip: Device IP address.

        Returns:
            A status dict, or a minimal dict if no controller exists.

        Raises:
            KeyError: If the device IP is not discovered.
        """
        with self._lock:
            if ip not in self._devices:
                raise KeyError(f"Unknown device: {ip}")
            ctrl: Optional[Controller] = self._controllers.get(ip)

        if ctrl is not None:
            return ctrl.get_status()

        # No controller yet — return idle status.
        dev: LifxDevice = self._devices[ip]
        return {
            "running": False,
            "effect": None,
            "params": {},
            "fps": 0,
            "devices": [{
                "ip": dev.ip,
                "mac": dev.mac_str,
                "label": dev.label,
                "product": dev.product_name,
                "zones": dev.zone_count,
            }],
        }

    def set_power(self, ip: str, on: bool) -> dict[str, Any]:
        """Turn a device on or off.

        Args:
            ip: Device IP address.
            on: ``True`` to power on, ``False`` to power off.

        Returns:
            A dict confirming the action.

        Raises:
            KeyError: If the device IP is not discovered.
        """
        dev: Optional[LifxDevice] = self.get_device(ip)
        if dev is None:
            raise KeyError(f"Unknown device: {ip}")
        dev.set_power(on=on, duration_ms=DEFAULT_FADE_MS)
        return {"ip": ip, "power": "on" if on else "off"}

    def identify(self, ip: str) -> None:
        """Pulse a device's brightness for a fixed duration to locate it.

        Runs in a background thread so the HTTP request returns immediately.
        Stops any running effect first, then pulses warm white brightness
        in a sine wave for :data:`IDENTIFY_DURATION_SECONDS`, then powers
        the device off.

        Args:
            ip: Device IP address.

        Raises:
            KeyError: If the device IP is not discovered.
        """
        dev: Optional[LifxDevice] = self.get_device(ip)
        if dev is None:
            raise KeyError(f"Unknown device: {ip}")

        # Stop any running effect so identify is visible.
        ctrl: Optional[Controller] = self.get_controller(ip)
        if ctrl is not None:
            ctrl.stop(fade_ms=0)

        def _pulse() -> None:
            """Background pulse loop."""
            try:
                dev.set_power(on=True, duration_ms=0)
                start: float = time_mod.monotonic()
                while time_mod.monotonic() - start < IDENTIFY_DURATION_SECONDS:
                    elapsed: float = time_mod.monotonic() - start
                    phase: float = (
                        math.sin(2.0 * math.pi * elapsed / IDENTIFY_CYCLE_SECONDS)
                        + 1.0
                    ) / 2.0
                    bri_frac: float = (
                        IDENTIFY_MIN_BRI + phase * (1.0 - IDENTIFY_MIN_BRI)
                    )
                    bri: int = int(bri_frac * HSBK_MAX)

                    if dev.is_multizone:
                        color = (0, 0, bri, KELVIN_DEFAULT)
                        colors = [color] * dev.zone_count
                        dev.set_zones(colors, duration_ms=0, rapid=True)
                    else:
                        dev.set_color(0, 0, bri, KELVIN_DEFAULT, duration_ms=0)

                    time_mod.sleep(IDENTIFY_FRAME_INTERVAL)

                dev.set_power(on=False, duration_ms=DEFAULT_FADE_MS)
            except Exception as exc:
                logging.warning("Identify pulse failed for %s: %s", ip, exc)

        thread: threading.Thread = threading.Thread(
            target=_pulse, daemon=True, name=f"identify-{ip}",
        )
        thread.start()

    def get_colors(self, ip: str) -> Optional[list[dict[str, int]]]:
        """Get a snapshot of the current zone colors.

        Creates a temporary :class:`LifxDevice` for the read-only query
        to avoid socket contention with the engine's device.

        Args:
            ip: Device IP address.

        Returns:
            A list of ``{h, s, b, k}`` dicts, or ``None`` on failure.

        Raises:
            KeyError: If the device IP is not discovered.
        """
        dev: Optional[LifxDevice] = self.get_device(ip)
        if dev is None:
            raise KeyError(f"Unknown device: {ip}")

        # Use a temporary device to avoid socket contention.
        tmp: LifxDevice = LifxDevice(ip)
        try:
            tmp.query_version()
            if tmp.is_multizone:
                tmp.query_zone_count()
            else:
                tmp.zone_count = 1
            colors = tmp.query_zone_colors() if tmp.is_multizone else None
            if colors is None:
                # Single bulb — query light state instead.
                state = tmp.query_light_state()
                if state is not None:
                    h, s, b, k, _power = state
                    colors = [(h, s, b, k)]
            if colors is not None:
                return [
                    {"h": h, "s": s, "b": b, "k": k}
                    for h, s, b, k in colors
                ]
            return None
        finally:
            tmp.close()

    def list_effects(self) -> dict[str, Any]:
        """Return available effects with parameter metadata.

        Delegates to :meth:`Controller.list_effects` (static data,
        does not require a device).

        Returns:
            A dict mapping effect names to descriptions and params.
        """
        # Use a throwaway controller-like approach — list_effects is
        # a class-level query that doesn't need a device.  We can call
        # the underlying function directly.
        result: dict[str, Any] = {}
        for name, cls in get_registry().items():
            params: dict[str, Any] = {}
            for pname, pdef in cls.get_param_defs().items():
                params[pname] = {
                    "default": pdef.default,
                    "min": pdef.min,
                    "max": pdef.max,
                    "description": pdef.description,
                    "type": type(pdef.default).__name__,
                }
                if pdef.choices:
                    params[pname]["choices"] = pdef.choices
            result[name] = {
                "description": cls.description,
                "params": params,
            }
        return result

    def devices_as_list(self) -> list[dict[str, Any]]:
        """Return discovered devices as a JSON-serializable list.

        Returns:
            A list of device info dicts.
        """
        return self._devices_as_list()

    # -- Override management ------------------------------------------------

    def mark_override(self, ip: str, entry_name: Optional[str]) -> None:
        """Mark a device as phone-overridden.

        Args:
            ip:         Device IP address.
            entry_name: The schedule entry name that was active when the
                        override began, or ``None`` if none was active.
        """
        with self._lock:
            self._overrides[ip] = entry_name

    def clear_override(self, ip: str) -> None:
        """Clear the phone override for a device.

        Args:
            ip: Device IP address.
        """
        with self._lock:
            self._overrides.pop(ip, None)

    def is_overridden(self, ip: str) -> bool:
        """Check if a device is under phone control.

        Args:
            ip: Device IP address.

        Returns:
            ``True`` if the device has an active phone override.
        """
        with self._lock:
            return ip in self._overrides

    def get_override_entry(self, ip: str) -> Optional[str]:
        """Get the schedule entry name that was active when override began.

        Args:
            ip: Device IP address.

        Returns:
            The entry name, or ``None``.
        """
        with self._lock:
            return self._overrides.get(ip)

    # -- Nickname management ------------------------------------------------

    def set_nickname(self, ip: str, nickname: str) -> None:
        """Assign a custom display name to a device.

        An empty nickname removes the override, reverting to the
        protocol label.

        Args:
            ip:       Device IP address.
            nickname: The custom name, or empty string to clear.
        """
        with self._lock:
            if nickname:
                self._nicknames[ip] = nickname
            else:
                self._nicknames.pop(ip, None)

    def get_nickname(self, ip: str) -> Optional[str]:
        """Look up a device's custom display name.

        Args:
            ip: Device IP address.

        Returns:
            The nickname, or ``None`` if none is set.
        """
        with self._lock:
            return self._nicknames.get(ip)

    def get_nicknames(self) -> dict[str, str]:
        """Return a copy of the full nickname mapping.

        Returns:
            A dict mapping device IP to nickname.
        """
        with self._lock:
            return dict(self._nicknames)

    # -- Internal helpers ---------------------------------------------------

    def _devices_as_list(self) -> list[dict[str, Any]]:
        """Build a JSON-safe list of device info dicts.

        Returns:
            A sorted list of device metadata dicts.
        """
        result: list[dict[str, Any]] = []
        for ip, dev in sorted(self._devices.items()):
            with self._lock:
                ctrl: Optional[Controller] = self._controllers.get(ip)
            current_effect: Optional[str] = None
            if ctrl is not None:
                status: dict[str, Any] = ctrl.get_status()
                current_effect = status.get("effect")
            nickname: Optional[str] = self._nicknames.get(dev.ip)
            result.append({
                "ip": dev.ip,
                "mac": dev.mac_str,
                "label": dev.label,
                "nickname": nickname,
                "product": dev.product_name,
                "group": dev.group,
                "zones": dev.zone_count,
                "is_multizone": dev.is_multizone,
                "current_effect": current_effect,
            })
        return result

    def _stop_and_remove(self, ip: str) -> None:
        """Stop controller and close device socket for a given IP.

        Must be called with ``_lock`` held.

        Args:
            ip: Device IP address.
        """
        ctrl: Optional[Controller] = self._controllers.pop(ip, None)
        if ctrl is not None:
            try:
                ctrl.stop(fade_ms=0)
            except Exception:
                pass
        dev: Optional[LifxDevice] = self._devices.pop(ip, None)
        if dev is not None:
            dev.close()
        self._overrides.pop(ip, None)

    def shutdown(self) -> None:
        """Stop all controllers and close all device sockets."""
        with self._lock:
            for ip in list(self._devices.keys()):
                self._stop_and_remove(ip)


# ---------------------------------------------------------------------------
# Schedule time parsing (ported from scheduler.py)
# ---------------------------------------------------------------------------

def _parse_time_spec(
    spec: str,
    sun: SunTimes,
    d: date,
    utc_offset: timedelta,
) -> Optional[datetime]:
    """Parse a time specification into a timezone-aware datetime.

    Supports fixed times (``"14:30"``), symbolic solar times
    (``"sunrise"``, ``"sunset"``), and offsets (``"sunset+30m"``).

    Args:
        spec:       The time specification string.
        sun:        Precomputed solar event times for date *d*.
        d:          Calendar date for resolving the time.
        utc_offset: Local UTC offset as a timedelta.

    Returns:
        A timezone-aware datetime, or ``None`` if the symbolic sun event
        does not occur on this date.
    """
    tz: timezone = timezone(utc_offset)

    # Try fixed time first (e.g., "14:30").
    match = _FIXED_TIME_RE.match(spec)
    if match:
        hours: int = int(match.group(1))
        mins: int = int(match.group(2))
        if hours > MAX_HOUR or mins > MAX_MINUTE:
            logging.error("Invalid fixed time: %s", spec)
            return None
        return datetime(d.year, d.month, d.day, hours, mins, 0, tzinfo=tz)

    # Try symbolic time (e.g., "sunset+30m").
    match = _SYMBOLIC_RE.match(spec)
    if not match:
        logging.error("Invalid time specification: %r", spec)
        return None

    symbol: str = match.group(1)
    sign: Optional[str] = match.group(2)
    offset_hours: int = int(match.group(3) or 0)
    offset_mins: int = int(match.group(4) or 0)

    # Resolve symbolic name to a datetime.
    base_time: Optional[datetime] = None
    if symbol == "midnight":
        base_time = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)
    elif symbol == "noon":
        base_time = sun.noon
    elif symbol == "sunrise":
        base_time = sun.sunrise
    elif symbol == "sunset":
        base_time = sun.sunset
    elif symbol == "dawn":
        base_time = sun.dawn
    elif symbol == "dusk":
        base_time = sun.dusk

    if base_time is None:
        logging.warning(
            "Sun event '%s' does not occur on %s (polar day/night?)",
            symbol, d,
        )
        return None

    # Apply offset.
    if sign:
        delta: timedelta = timedelta(hours=offset_hours, minutes=offset_mins)
        if sign == "-":
            delta = -delta
        base_time = base_time + delta

    return base_time


def _entry_runs_on_day(spec: dict[str, Any], d: date) -> bool:
    """Check whether a schedule entry runs on a given calendar date.

    If the ``days`` key is absent or empty, the entry runs every day.
    Otherwise it must be a string of day letters from ``MTWRFSU``
    (Monday through Sunday, academic convention).

    Args:
        spec: Schedule entry dict (may contain a ``days`` key).
        d:    Calendar date to check.

    Returns:
        ``True`` if the entry should run on date *d*.
    """
    days_str: str = spec.get("days", "")
    if not days_str:
        return True
    # Convert the date's weekday (0=Mon .. 6=Sun) to our letter set.
    weekday: int = d.weekday()
    for letter, idx in DAY_LETTER_TO_WEEKDAY.items():
        if idx == weekday:
            return letter in days_str.upper()
    return False


def _validate_days(days_str: str) -> bool:
    """Validate a day-of-week string.

    Args:
        days_str: String of day letters (e.g. ``"MTWRF"``).

    Returns:
        ``True`` if all characters are valid day letters with no repeats.
    """
    upper: str = days_str.upper()
    return (
        all(ch in VALID_DAY_LETTERS for ch in upper)
        and len(upper) == len(set(upper))
    )


def _days_display(days_str: str) -> str:
    """Format a days string for human display.

    Returns smart labels for common patterns, otherwise the
    letter string itself.

    Args:
        days_str: Day letter string (e.g. ``"MTWRF"``).

    Returns:
        A display string like ``"Weekdays"``, ``"Weekends"``, ``"Daily"``,
        or the sorted letter string.
    """
    if not days_str:
        return "Daily"
    upper: str = days_str.upper()
    # Sort into canonical order.
    canonical: str = "".join(ch for ch in VALID_DAY_LETTERS if ch in upper)
    if canonical == VALID_DAY_LETTERS:
        return "Daily"
    if canonical == "MTWRF":
        return "Weekdays"
    if canonical == "SU":
        return "Weekends"
    return canonical


def _resolve_entries(
    specs: list[dict[str, Any]],
    lat: float,
    lon: float,
    d: date,
    utc_offset: timedelta,
    group_filter: Optional[str] = None,
) -> list[tuple[datetime, datetime, dict[str, Any]]]:
    """Resolve schedule entries for a specific date.

    Args:
        specs:        List of raw schedule entry dicts.
        lat:          Observer latitude in degrees.
        lon:          Observer longitude in degrees.
        d:            Calendar date for sun time resolution.
        utc_offset:   Local UTC offset.
        group_filter: If set, only include entries matching this group.

    Returns:
        A list of ``(start_datetime, stop_datetime, spec_dict)`` tuples.
    """
    sun: SunTimes = sun_times(lat, lon, d, utc_offset)
    resolved: list[tuple[datetime, datetime, dict[str, Any]]] = []

    for spec in specs:
        if group_filter is not None and spec.get("group") != group_filter:
            continue

        # Enabled filter: skip disabled entries (default: enabled).
        if not spec.get("enabled", True):
            continue

        # Day-of-week filter: skip entries that don't run on this date.
        if not _entry_runs_on_day(spec, d):
            continue

        start: Optional[datetime] = _parse_time_spec(
            spec["start"], sun, d, utc_offset,
        )
        stop: Optional[datetime] = _parse_time_spec(
            spec["stop"], sun, d, utc_offset,
        )

        if start is None or stop is None:
            logging.warning(
                "Skipping entry '%s': could not resolve times",
                spec.get("name", "?"),
            )
            continue

        # Overnight entries: stop is before start → add a day to stop.
        if stop <= start:
            stop += timedelta(days=1)

        resolved.append((start, stop, spec))

    return resolved


def _find_active_entry(
    specs: list[dict[str, Any]],
    lat: float,
    lon: float,
    now: datetime,
    group_name: str,
) -> Optional[dict[str, Any]]:
    """Find the first schedule entry active for a group at time *now*.

    Args:
        specs:      Raw schedule entry dicts.
        lat:        Observer latitude in degrees.
        lon:        Observer longitude in degrees.
        now:        Current timezone-aware datetime.
        group_name: Only consider entries targeting this group.

    Returns:
        The matching spec dict, or ``None`` if no entry is active.
    """
    utc_offset: timedelta = now.utcoffset()
    today: date = now.date()
    yesterday: date = today - timedelta(days=1)

    today_resolved = _resolve_entries(
        specs, lat, lon, today, utc_offset, group_filter=group_name,
    )
    yesterday_resolved = _resolve_entries(
        specs, lat, lon, yesterday, utc_offset, group_filter=group_name,
    )

    for start, stop, spec in today_resolved:
        if start <= now < stop:
            return spec

    for start, stop, spec in yesterday_resolved:
        if start <= now < stop:
            return spec

    return None


# ---------------------------------------------------------------------------
# Scheduler thread
# ---------------------------------------------------------------------------

class SchedulerThread(threading.Thread):
    """Background thread that manages scheduled effects.

    Replaces scheduler.py's subprocess-based approach with direct
    :class:`Controller` calls through the :class:`DeviceManager`.
    Respects phone overrides: skips devices that have been overridden
    by the REST API, and clears overrides at schedule transitions.
    """

    def __init__(
        self,
        config: dict[str, Any],
        device_manager: DeviceManager,
    ) -> None:
        """Initialize the scheduler thread.

        Args:
            config:         Parsed server configuration dict.
            device_manager: Shared :class:`DeviceManager` instance.
        """
        super().__init__(daemon=True, name="scheduler")
        self._config: dict[str, Any] = config
        self._dm: DeviceManager = device_manager
        self._stop_event: threading.Event = threading.Event()

        # Per-group state: tracks which schedule entry is currently active.
        self._group_entries: dict[str, Optional[str]] = {}

    def run(self) -> None:
        """Scheduler main loop — poll for schedule transitions."""
        lat: float = self._config["location"]["latitude"]
        lon: float = self._config["location"]["longitude"]
        groups: dict[str, list[str]] = _get_groups(self._config)
        specs: list[dict[str, Any]] = self._config.get("schedule", [])

        if not specs:
            logging.info("No schedule entries — scheduler idle")
            return

        # Initialize per-group state.
        for group_name in groups:
            self._group_entries[group_name] = None

        last_logged_date: Optional[date] = None

        total_devices: int = sum(len(ips) for ips in groups.values())
        logging.info(
            "Scheduler started — %d groups, %d devices, %d entries",
            len(groups), total_devices, len(specs),
        )

        while not self._stop_event.is_set():
            now: datetime = datetime.now(timezone.utc).astimezone()
            today: date = now.date()

            # Log sun times once per day.
            if today != last_logged_date:
                utc_offset: timedelta = now.utcoffset()
                sun: SunTimes = sun_times(lat, lon, today, utc_offset)
                _log_sun_times(sun, today)
                last_logged_date = today

            # Per-group scheduling.
            for group_name, ips in groups.items():
                active: Optional[dict[str, Any]] = _find_active_entry(
                    specs, lat, lon, now, group_name,
                )
                active_name: Optional[str] = (
                    active.get("name") if active else None
                )
                prev_name: Optional[str] = self._group_entries.get(
                    group_name,
                )

                if active_name != prev_name:
                    # Schedule transition — clear overrides for all
                    # devices in this group so the scheduler resumes.
                    for ip in ips:
                        if self._dm.is_overridden(ip):
                            logging.info(
                                "[%s] Clearing phone override on %s "
                                "(schedule transition)",
                                group_name, ip,
                            )
                            self._dm.clear_override(ip)

                    # Stop previous effect on all devices in group.
                    if prev_name is not None:
                        logging.info(
                            "[%s] Stopping '%s'", group_name, prev_name,
                        )
                        for ip in ips:
                            try:
                                self._dm.stop(ip)
                            except (KeyError, Exception) as exc:
                                logging.warning(
                                    "[%s] Error stopping %s: %s",
                                    group_name, ip, exc,
                                )

                    # Start new effect.
                    if active is not None:
                        effect: str = active["effect"]
                        params: dict[str, Any] = active.get("params", {})
                        logging.info(
                            "[%s] Starting '%s' (%s)",
                            group_name, active_name, effect,
                        )
                        for ip in ips:
                            try:
                                self._dm.play(ip, effect, params)
                            except (KeyError, ValueError, Exception) as exc:
                                logging.warning(
                                    "[%s] Error starting %s on %s: %s",
                                    group_name, effect, ip, exc,
                                )
                    else:
                        logging.info(
                            "[%s] No active entry — idle", group_name,
                        )

                    self._group_entries[group_name] = active_name

                elif active is not None:
                    # Same entry still active — ensure running on
                    # non-overridden devices (restart if crashed).
                    for ip in ips:
                        if self._dm.is_overridden(ip):
                            continue
                        ctrl: Optional[Controller] = (
                            self._dm.get_or_create_controller(ip)
                        )
                        if ctrl is not None:
                            status: dict[str, Any] = ctrl.get_status()
                            if not status.get("running"):
                                effect_name: str = active["effect"]
                                params_restart: dict = active.get(
                                    "params", {},
                                )
                                logging.info(
                                    "[%s] Restarting '%s' on %s",
                                    group_name, active_name, ip,
                                )
                                try:
                                    self._dm.play(
                                        ip, effect_name, params_restart,
                                    )
                                except Exception as exc:
                                    logging.warning(
                                        "[%s] Restart error on %s: %s",
                                        group_name, ip, exc,
                                    )

            # Sleep until next poll, checking for stop every second.
            self._stop_event.wait(SCHEDULER_POLL_SECONDS)

        logging.info("Scheduler stopped")

    def stop(self) -> None:
        """Signal the scheduler to stop."""
        self._stop_event.set()


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class GlowUpRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for the GlowUp REST API.

    Class-level attributes are set before the server starts:

    Attributes:
        device_manager: Shared :class:`DeviceManager`.
        auth_token:     Expected bearer token for authentication.
        scheduler:      The :class:`SchedulerThread` (for override tracking).
        config:         Parsed server configuration dict.
    """

    # These are set by main() before the server starts.
    device_manager: DeviceManager
    auth_token: str
    scheduler: Optional[SchedulerThread] = None
    config: dict[str, Any] = {}
    config_path: Optional[str] = None

    # Silence per-request logging from BaseHTTPRequestHandler.
    def log_message(self, format: str, *args: Any) -> None:
        """Route HTTP access logs through the logging module.

        Args:
            format: Format string.
            *args:  Format arguments.
        """
        logging.debug("HTTP %s", format % args)

    # -- Authentication -----------------------------------------------------

    def _authenticate(self) -> bool:
        """Check the Authorization header for a valid bearer token.

        Sends a 401 or 429 response if authentication fails.
        Rate-limits clients that repeatedly fail authentication.

        Returns:
            ``True`` if the request is authenticated, ``False`` otherwise.
        """
        client_ip: str = self.client_address[0]

        # Check rate limit before processing the token.
        if _rate_limiter.is_blocked(client_ip):
            self._send_json(429, {"error": "Too many failed attempts"})
            return False

        auth: Optional[str] = self.headers.get(AUTH_HEADER)
        if auth is None or not auth.startswith(BEARER_PREFIX):
            _rate_limiter.record_failure(client_ip)
            self._send_json(401, {"error": "Missing or invalid token"})
            self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            return False

        token: str = auth[len(BEARER_PREFIX):]
        if not hmac.compare_digest(token, self.auth_token):
            _rate_limiter.record_failure(client_ip)
            self._send_json(401, {"error": "Invalid token"})
            return False

        # Successful auth — clear any prior failures.
        _rate_limiter.clear(client_ip)
        return True

    # -- JSON helpers -------------------------------------------------------

    def _read_json_body(self) -> Optional[dict[str, Any]]:
        """Read and parse the JSON request body.

        Sends a 400 response if the body is missing, too large, or
        not valid JSON.

        Returns:
            The parsed dict, or ``None`` on error.
        """
        length_str: Optional[str] = self.headers.get("Content-Length")
        if length_str is None:
            self._send_json(400, {"error": "Missing Content-Length"})
            return None

        try:
            length: int = int(length_str)
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length"})
            return None

        if length > MAX_REQUEST_BODY:
            self._send_json(413, {"error": "Request body too large"})
            return None

        raw: bytes = self.rfile.read(length)
        try:
            body: Any = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON"})
            return None

        if not isinstance(body, dict):
            self._send_json(400, {"error": "Expected JSON object"})
            return None

        return body

    def _send_json(self, code: int, data: Any) -> None:
        """Send a JSON response with security headers.

        Args:
            code: HTTP status code.
            data: JSON-serializable response data.
        """
        body: bytes = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_headers(self) -> None:
        """Send HTTP headers for a Server-Sent Events stream."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._send_security_headers()
        self.end_headers()

    # -- Security headers ---------------------------------------------------

    def _send_security_headers(self) -> None:
        """Append standard security headers to the current response.

        Called by :meth:`_send_json` and :meth:`_send_sse_headers` so
        every response carries a consistent security posture.
        """
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Strict-Transport-Security",
                         "max-age=31536000; includeSubDomains")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'")

    # -- Routing ------------------------------------------------------------

    def _extract_ip(self, parts: list[str]) -> Optional[str]:
        """Extract the device IP from URL path parts.

        Expects the IP at index 3 (``/api/devices/{ip}/...``).

        Args:
            parts: URL path split by ``/``.

        Returns:
            The IP string, or ``None`` if not present.
        """
        if len(parts) > 3:
            return parts[3]
        return None

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight requests.

        Requires authentication to prevent unauthenticated endpoint
        enumeration.  The iOS app does not use CORS (native HTTP), so
        this is only needed for browser-based clients.
        """
        if not self._authenticate():
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "3600")
        self._send_security_headers()
        self.end_headers()

    def do_GET(self) -> None:
        """Route GET requests to the appropriate handler."""
        if not self._authenticate():
            return

        path: str = self.path.split("?")[0]  # strip query string
        parts: list[str] = path.strip("/").split("/")

        # /api/devices
        if path == "/api/devices":
            self._handle_get_devices()
            return

        # /api/effects
        if path == "/api/effects":
            self._handle_get_effects()
            return

        # /api/devices/{ip}/status
        if (len(parts) == 4 and parts[0] == "api" and parts[1] == "devices"
                and parts[3] == "status"):
            ip: str = parts[2]
            if not _validate_ip(ip):
                self._send_json(400, {"error": "Invalid IP address"})
                return
            self._handle_get_device_status(ip)
            return

        # /api/devices/{ip}/colors
        if (len(parts) == 4 and parts[0] == "api" and parts[1] == "devices"
                and parts[3] == "colors"):
            ip = parts[2]
            if not _validate_ip(ip):
                self._send_json(400, {"error": "Invalid IP address"})
                return
            self._handle_get_device_colors(ip)
            return

        # /api/devices/{ip}/colors/stream
        if (len(parts) == 5 and parts[0] == "api" and parts[1] == "devices"
                and parts[3] == "colors" and parts[4] == "stream"):
            ip = parts[2]
            if not _validate_ip(ip):
                self._send_json(400, {"error": "Invalid IP address"})
                return
            self._handle_get_device_colors_stream(ip)
            return

        # /api/schedule
        if path == "/api/schedule":
            self._handle_get_schedule()
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        """Route POST requests to the appropriate handler."""
        if not self._authenticate():
            return

        path: str = self.path.split("?")[0]
        parts: list[str] = path.strip("/").split("/")

        # /api/discover
        if path == "/api/discover":
            self._handle_post_discover()
            return

        # /api/devices/{ip}/play
        if (len(parts) == 4 and parts[0] == "api" and parts[1] == "devices"
                and parts[3] == "play"):
            ip: str = parts[2]
            if not _validate_ip(ip):
                self._send_json(400, {"error": "Invalid IP address"})
                return
            self._handle_post_play(ip)
            return

        # /api/devices/{ip}/stop
        if (len(parts) == 4 and parts[0] == "api" and parts[1] == "devices"
                and parts[3] == "stop"):
            ip = parts[2]
            if not _validate_ip(ip):
                self._send_json(400, {"error": "Invalid IP address"})
                return
            self._handle_post_stop(ip)
            return

        # /api/devices/{ip}/power
        if (len(parts) == 4 and parts[0] == "api" and parts[1] == "devices"
                and parts[3] == "power"):
            ip = parts[2]
            if not _validate_ip(ip):
                self._send_json(400, {"error": "Invalid IP address"})
                return
            self._handle_post_power(ip)
            return

        # /api/devices/{ip}/identify
        if (len(parts) == 4 and parts[0] == "api" and parts[1] == "devices"
                and parts[3] == "identify"):
            ip = parts[2]
            if not _validate_ip(ip):
                self._send_json(400, {"error": "Invalid IP address"})
                return
            self._handle_post_identify(ip)
            return

        # /api/devices/{ip}/nickname
        if (len(parts) == 4 and parts[0] == "api" and parts[1] == "devices"
                and parts[3] == "nickname"):
            ip = parts[2]
            if not _validate_ip(ip):
                self._send_json(400, {"error": "Invalid IP address"})
                return
            self._handle_post_nickname(ip)
            return

        # /api/schedule/{index}/enabled
        if (len(parts) == 4 and parts[0] == "api" and parts[1] == "schedule"
                and parts[3] == "enabled"):
            try:
                index: int = int(parts[2])
            except ValueError:
                self._send_json(400, {"error": "Invalid schedule index"})
                return
            self._handle_post_schedule_enabled(index)
            return

        self._send_json(404, {"error": "Not found"})

    # -- GET handlers -------------------------------------------------------

    def _handle_get_devices(self) -> None:
        """GET /api/devices — list all discovered devices."""
        devices: list[dict[str, Any]] = self.device_manager.devices_as_list()
        self._send_json(200, {"devices": devices})

    def _handle_get_effects(self) -> None:
        """GET /api/effects — list effects with param metadata."""
        effects: dict[str, Any] = self.device_manager.list_effects()
        self._send_json(200, {"effects": effects})

    def _handle_get_schedule(self) -> None:
        """GET /api/schedule — schedule entries with resolved times.

        Returns the schedule entries from the config, each enriched
        with resolved start/stop times for today and an ``active``
        flag indicating whether the entry is running right now.
        """
        config: dict[str, Any] = self.config
        specs: list[dict[str, Any]] = config.get("schedule", [])
        if not specs:
            self._send_json(200, {"entries": []})
            return

        lat: float = config.get("location", {}).get("latitude", 0.0)
        lon: float = config.get("location", {}).get("longitude", 0.0)

        now: datetime = datetime.now(timezone.utc).astimezone()
        utc_offset: timedelta = now.utcoffset()
        today: date = now.date()

        # Resolve times for today to determine active status and
        # display times.  We resolve without group filter to get all.
        sun: SunTimes = sun_times(lat, lon, today, utc_offset)

        entries: list[dict[str, Any]] = []
        for i, spec in enumerate(specs):
            enabled: bool = spec.get("enabled", True)
            days_raw: str = spec.get("days", "")

            # Resolve start/stop for display.
            start_resolved: Optional[datetime] = _parse_time_spec(
                spec["start"], sun, today, utc_offset,
            )
            stop_resolved: Optional[datetime] = _parse_time_spec(
                spec["stop"], sun, today, utc_offset,
            )

            start_str: Optional[str] = None
            stop_str: Optional[str] = None
            active: bool = False

            if start_resolved is not None and stop_resolved is not None:
                # Handle overnight entries.
                if stop_resolved <= start_resolved:
                    stop_resolved += timedelta(days=1)
                start_str = start_resolved.strftime("%H:%M")
                stop_str = stop_resolved.strftime("%H:%M")
                if stop_resolved.date() != start_resolved.date():
                    stop_str = stop_resolved.strftime("%H:%M (+1)")

                # Active if enabled, runs today, and we're in the window.
                if (enabled
                        and _entry_runs_on_day(spec, today)
                        and start_resolved <= now < stop_resolved):
                    active = True

            entry: dict[str, Any] = {
                "index": i,
                "name": spec.get("name", f"entry_{i}"),
                "group": spec.get("group", ""),
                "effect": spec.get("effect", ""),
                "start": spec.get("start", ""),
                "stop": spec.get("stop", ""),
                "start_resolved": start_str,
                "stop_resolved": stop_str,
                "days": days_raw,
                "days_display": _days_display(days_raw),
                "enabled": enabled,
                "active": active,
            }
            entries.append(entry)

        self._send_json(200, {"entries": entries})

    def _handle_get_device_status(self, ip: str) -> None:
        """GET /api/devices/{ip}/status — device effect status."""
        try:
            status: dict[str, Any] = self.device_manager.get_status(ip)
            self._send_json(200, status)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})

    def _handle_get_device_colors(self, ip: str) -> None:
        """GET /api/devices/{ip}/colors — zone color snapshot."""
        try:
            colors = self.device_manager.get_colors(ip)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})
            return

        if colors is None:
            self._send_json(503, {"error": "Could not query device colors"})
            return

        self._send_json(200, {"zones": colors})

    def _handle_get_device_colors_stream(self, ip: str) -> None:
        """GET /api/devices/{ip}/colors/stream — SSE color stream at 4 Hz.

        Creates a temporary :class:`LifxDevice` for read-only zone color
        queries to avoid socket contention with the engine.  The stream
        runs until the client disconnects.
        """
        dev: Optional[LifxDevice] = self.device_manager.get_device(ip)
        if dev is None:
            self._send_json(404, {"error": "Device not found"})
            return

        self._send_sse_headers()

        # Send an initial padding comment to flush Cloudflare's response
        # buffer.  Cloudflare Tunnel buffers small chunks; a ~4KB initial
        # payload forces the proxy to begin streaming immediately.
        padding: str = ": " + " " * 4096 + "\n\n"
        try:
            self.wfile.write(padding.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        stream_start: float = time_mod.time()
        try:
            while True:
                # Enforce maximum stream lifetime to prevent resource
                # exhaustion from abandoned connections.
                if time_mod.time() - stream_start > SSE_TIMEOUT_SECONDS:
                    break

                # Read colors from the engine's in-memory frame buffer.
                # Zero UDP overhead, zero socket contention.  When no
                # effect is running the frame is None and we skip the
                # event — the app shows "Connecting..." which is accurate.
                ctrl: Optional[Controller] = self.device_manager.get_controller(ip)
                if ctrl is not None:
                    colors = ctrl.get_last_frame()
                    if colors is not None:
                        payload: str = json.dumps({
                            "zones": [
                                {"h": h, "s": s, "b": b, "k": k}
                                for h, s, b, k in colors
                            ],
                        })
                        self.wfile.write(
                            f"data: {payload}\n\n".encode("utf-8"),
                        )
                        self.wfile.flush()

                time_mod.sleep(SSE_POLL_INTERVAL)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client disconnected — clean exit.
            pass

    # -- POST handlers ------------------------------------------------------

    def _handle_post_discover(self) -> None:
        """POST /api/discover — re-run device discovery."""
        logging.info("API: re-running device discovery")
        devices: list[dict[str, Any]] = self.device_manager.discover()
        self._send_json(200, {"devices": devices})

    def _handle_post_play(self, ip: str) -> None:
        """POST /api/devices/{ip}/play — start an effect.

        Request body::

            {
                "effect": "cylon",
                "params": {"speed": 2.0, "hue": 120}
            }
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        effect_name: Optional[str] = body.get("effect")
        if not effect_name or not isinstance(effect_name, str):
            self._send_json(400, {"error": "Missing or invalid 'effect'"})
            return

        params: dict[str, Any] = body.get("params", {})
        if not isinstance(params, dict):
            self._send_json(400, {"error": "'params' must be an object"})
            return

        try:
            # Track override so the scheduler knows to back off.
            active_entry: Optional[str] = self._get_active_entry_for_ip(ip)
            self.device_manager.mark_override(ip, active_entry)

            status: dict[str, Any] = self.device_manager.play(
                ip, effect_name, params,
            )
            logging.info(
                "API: playing '%s' on %s (params: %s)",
                effect_name, ip, params,
            )
            self._send_json(200, status)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})
        except ValueError:
            self._send_json(400, {"error": "Invalid effect or parameters"})

    def _handle_post_stop(self, ip: str) -> None:
        """POST /api/devices/{ip}/stop — stop the current effect."""
        try:
            status: dict[str, Any] = self.device_manager.stop(ip)
            logging.info("API: stopped effect on %s", ip)
            # Keep the override active so the scheduler doesn't
            # immediately resume — it clears at the next transition.
            self._send_json(200, status)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})

    def _handle_post_power(self, ip: str) -> None:
        """POST /api/devices/{ip}/power — turn device on/off.

        Request body::

            {"on": true}
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        on: Any = body.get("on")
        if not isinstance(on, bool):
            self._send_json(400, {"error": "'on' must be a boolean"})
            return

        try:
            result: dict[str, Any] = self.device_manager.set_power(ip, on)
            logging.info("API: power %s on %s", "on" if on else "off", ip)
            self._send_json(200, result)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})

    def _handle_post_identify(self, ip: str) -> None:
        """POST /api/devices/{ip}/identify — pulse brightness to locate device.

        Starts a background thread that pulses the device's brightness
        in a sine wave for :data:`IDENTIFY_DURATION_SECONDS`.  The HTTP
        response returns immediately.
        """
        try:
            self.device_manager.identify(ip)
            logging.info("API: identifying %s", ip)
            self._send_json(200, {"ip": ip, "identifying": True})
        except KeyError:
            self._send_json(404, {"error": "Device not found"})

    def _handle_post_nickname(self, ip: str) -> None:
        """POST /api/devices/{ip}/nickname — set a custom display name.

        Request body::

            {"nickname": "Porch Lights"}

        An empty string or ``null`` clears the nickname.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        nickname: Any = body.get("nickname")
        if nickname is None:
            nickname = ""
        if not isinstance(nickname, str):
            self._send_json(400, {"error": "'nickname' must be a string"})
            return

        nickname = nickname.strip()

        self.device_manager.set_nickname(ip, nickname)

        # Persist to config file.
        self._save_nicknames()

        logging.info(
            "API: nickname for %s %s",
            ip, f"set to '{nickname}'" if nickname else "cleared",
        )
        self._send_json(200, {"ip": ip, "nickname": nickname or None})

    def _save_nicknames(self) -> None:
        """Persist current nicknames to the config file."""
        nicknames: dict[str, str] = self.device_manager.get_nicknames()
        self._save_config_field("nicknames", nicknames or {})

    def _handle_post_schedule_enabled(self, index: int) -> None:
        """POST /api/schedule/{index}/enabled — enable or disable an entry.

        Request body::

            {"enabled": false}

        Persists the change to the config file so it survives restarts.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        enabled: Any = body.get("enabled")
        if not isinstance(enabled, bool):
            self._send_json(400, {"error": "'enabled' must be a boolean"})
            return

        specs: list[dict[str, Any]] = self.config.get("schedule", [])
        if index < 0 or index >= len(specs):
            self._send_json(404, {"error": "Schedule entry not found"})
            return

        specs[index]["enabled"] = enabled
        self._save_config_field("schedule", specs)

        name: str = specs[index].get("name", f"entry_{index}")
        logging.info(
            "API: schedule entry '%s' %s",
            name, "enabled" if enabled else "disabled",
        )
        self._send_json(200, {
            "index": index,
            "name": name,
            "enabled": enabled,
        })

    def _save_config_field(self, key: str, value: Any) -> None:
        """Persist a single config field to the config file.

        Reads the config JSON, updates the given key, and writes back.

        Args:
            key:   Top-level config key to update.
            value: The new value.
        """
        config_path: Optional[str] = self.config_path
        if config_path is None:
            return
        try:
            with open(config_path, "r") as f:
                config: dict[str, Any] = json.load(f)
            config[key] = value
            with open(config_path, "w") as f:
                json.dump(config, f, indent=4)
                f.write("\n")
        except Exception as exc:
            logging.warning("Failed to save config field '%s': %s", key, exc)

    # -- Helpers ------------------------------------------------------------

    def _get_active_entry_for_ip(self, ip: str) -> Optional[str]:
        """Find the active schedule entry name for a device IP.

        Searches all groups in the config for one containing this IP,
        then checks which schedule entry is active for that group.

        Args:
            ip: Device IP address.

        Returns:
            The active entry name, or ``None``.
        """
        config: dict[str, Any] = self.config
        if not config:
            return None

        groups: dict[str, list[str]] = _get_groups(config)
        specs: list[dict[str, Any]] = config.get("schedule", [])
        if not specs:
            return None

        now: datetime = datetime.now(timezone.utc).astimezone()

        for group_name, ips in groups.items():
            if ip in ips:
                active: Optional[dict[str, Any]] = _find_active_entry(
                    specs,
                    config["location"]["latitude"],
                    config["location"]["longitude"],
                    now,
                    group_name,
                )
                if active is not None:
                    return active.get("name")
                return None

        return None


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """HTTP server that handles each request in a new thread.

    Required for SSE: long-lived streaming connections must not block
    other requests from being served.
    """

    daemon_threads: bool = True
    allow_reuse_address: bool = True


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_config(config_path: str) -> dict[str, Any]:
    """Load and validate the server configuration file.

    The config must contain ``port``, ``auth_token``, and ``location``
    sections.  The ``groups`` and ``schedule`` sections are optional
    (server works without a schedule — API-only mode).

    Args:
        config_path: Path to the JSON configuration file.

    Returns:
        The parsed configuration dictionary.

    Raises:
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
        ValueError: If required fields are missing or invalid.
    """
    with open(config_path, "r") as f:
        config: dict[str, Any] = json.load(f)

    # Validate auth token.
    token: Any = config.get("auth_token")
    if not token or not isinstance(token, str) or token == "CHANGE_ME":
        raise ValueError(
            "Config must contain a non-default 'auth_token' string.  "
            "Generate one with: python3 -c "
            "\"import secrets; print(secrets.token_urlsafe(32))\""
        )

    # Validate port.
    port: Any = config.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or port < 1 or port > 65535:
        raise ValueError(f"'port' must be 1-65535, got {port!r}")
    config["port"] = port

    # Location is required if schedule is present.
    if "schedule" in config and config["schedule"]:
        if "location" not in config:
            raise ValueError("Config missing 'location' (required for schedule)")
        loc: dict = config["location"]
        if "latitude" not in loc or "longitude" not in loc:
            raise ValueError(
                "Config location must have 'latitude' and 'longitude'"
            )

    # Validate groups and schedule if present.
    if "groups" in config:
        groups: dict[str, list[str]] = config["groups"]
        for group_name, ips in groups.items():
            if group_name.startswith("_"):
                continue
            if not isinstance(ips, list) or not ips:
                raise ValueError(
                    f"Group '{group_name}' must be a non-empty list"
                )

    if "schedule" in config:
        known_groups: set[str] = set()
        if "groups" in config:
            known_groups = {
                k for k in config["groups"] if not k.startswith("_")
            }
        for i, entry in enumerate(config["schedule"]):
            label: str = entry.get("name", f"entry_{i}")
            for req_field in ("start", "stop", "effect", "group"):
                if req_field not in entry:
                    raise ValueError(
                        f"Schedule entry '{label}' missing '{req_field}'"
                    )
            if known_groups and entry["group"] not in known_groups:
                raise ValueError(
                    f"Schedule entry '{label}' references unknown group "
                    f"'{entry['group']}'"
                )
            days: str = entry.get("days", "")
            if days and not _validate_days(days):
                raise ValueError(
                    f"Schedule entry '{label}' has invalid 'days' value "
                    f"'{days}' — use letters from MTWRFSU (no repeats)"
                )

    return config


def _get_groups(config: dict[str, Any]) -> dict[str, list[str]]:
    """Extract device groups from config, excluding comment keys.

    Args:
        config: Parsed configuration dictionary.

    Returns:
        A dict mapping group names to lists of IP addresses.
    """
    groups: dict = config.get("groups", {})
    return {
        name: ips
        for name, ips in groups.items()
        if not name.startswith("_")
    }


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _log_sun_times(sun: SunTimes, d: date) -> None:
    """Log computed solar event times for a date.

    Args:
        sun: Computed solar event times.
        d:   Calendar date.
    """
    fmt: str = "%H:%M"
    logging.info("Sun times for %s:", d)
    logging.info(
        "  Dawn:    %s", sun.dawn.strftime(fmt) if sun.dawn else "N/A",
    )
    logging.info(
        "  Sunrise: %s", sun.sunrise.strftime(fmt) if sun.sunrise else "N/A",
    )
    logging.info("  Noon:    %s", sun.noon.strftime(fmt))
    logging.info(
        "  Sunset:  %s", sun.sunset.strftime(fmt) if sun.sunset else "N/A",
    )
    logging.info(
        "  Dusk:    %s", sun.dusk.strftime(fmt) if sun.dusk else "N/A",
    )


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

def _dry_run(config: dict[str, Any]) -> None:
    """Print the resolved schedule without running any effects.

    Args:
        config: Parsed configuration dictionary.
    """
    if "location" not in config:
        print("No location configured — nothing to preview.")
        return
    if "schedule" not in config or not config["schedule"]:
        print("No schedule entries — nothing to preview.")
        return

    lat: float = config["location"]["latitude"]
    lon: float = config["location"]["longitude"]
    groups: dict[str, list[str]] = _get_groups(config)
    specs: list[dict[str, Any]] = config["schedule"]

    now: datetime = datetime.now(timezone.utc).astimezone()
    utc_offset: timedelta = now.utcoffset()
    today: date = now.date()

    sun: SunTimes = sun_times(lat, lon, today, utc_offset)

    print(f"Location:   {lat}°N, {lon}°E")
    print(f"Date:       {today}")
    print(f"UTC offset: {utc_offset}")
    print(f"Server:     localhost:{config.get('port', DEFAULT_PORT)}")
    print()

    print("Solar events:")
    fmt: str = "%H:%M:%S"
    print(f"  Dawn:    {sun.dawn.strftime(fmt) if sun.dawn else 'N/A'}")
    print(f"  Sunrise: {sun.sunrise.strftime(fmt) if sun.sunrise else 'N/A'}")
    print(f"  Noon:    {sun.noon.strftime(fmt)}")
    print(f"  Sunset:  {sun.sunset.strftime(fmt) if sun.sunset else 'N/A'}")
    print(f"  Dusk:    {sun.dusk.strftime(fmt) if sun.dusk else 'N/A'}")
    print()

    if groups:
        print("Device groups:")
        for group_name in sorted(groups):
            ips: list[str] = groups[group_name]
            print(f"  {group_name}: {', '.join(ips)}")
        print()

    for group_name in sorted(groups):
        resolved = _resolve_entries(
            specs, lat, lon, today, utc_offset, group_filter=group_name,
        )
        print(f"Schedule — {group_name}:")
        if not resolved:
            print("  (no entries)")
            print()
            continue

        for start, stop, spec in resolved:
            status: str = ""
            if start <= now < stop:
                status = " <-- ACTIVE NOW"
            stop_str: str = (
                stop.strftime("%m/%d %H:%M")
                if stop.date() != start.date()
                else stop.strftime("%H:%M")
            )
            days_str: str = ""
            days_raw: str = spec.get("days", "")
            if days_raw:
                days_str = f"  ({_days_display(days_raw)})"
            params_str: str = ""
            if spec.get("params"):
                params_str = f"  params: {spec['params']}"
            print(
                f"  {spec.get('name', '?'):20s}  "
                f"{start.strftime('%H:%M')} -> {stop_str}  "
                f"[{spec['effect']}]{days_str}{params_str}{status}"
            )
        print()


# ---------------------------------------------------------------------------
# CLI and main
# ---------------------------------------------------------------------------

def _build_parser() -> "argparse.ArgumentParser":
    """Build the command-line argument parser.

    Returns:
        A configured argument parser.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="glowup-server",
        description="GlowUp REST API server — remote control daemon "
                    "for LIFX devices with optional scheduling",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to server config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resolved schedule and exit without running",
    )
    parser.add_argument(
        "--lerp",
        type=str,
        default=None,
        choices=["lab", "hsb"],
        help="Color interpolation method: lab (perceptually uniform) "
             "or hsb (cheap). Overrides config file. Default: lab",
    )
    return parser


def main() -> None:
    """Entry point for the GlowUp REST API server."""
    import argparse

    parser: argparse.ArgumentParser = _build_parser()
    args: argparse.Namespace = parser.parse_args()

    # Set up logging to stderr (systemd journal captures this).
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )

    config_path: str = args.config
    try:
        config: dict[str, Any] = _load_config(config_path)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        logging.error("Configuration error: %s", exc)
        sys.exit(1)

    # -- Color interpolation method -----------------------------------------
    # CLI --lerp overrides config "lerp" key; default is "lab".
    from colorspace import set_lerp_method
    lerp_method: str = (
        args.lerp
        or config.get("lerp", "lab")
    )
    set_lerp_method(lerp_method)
    logging.info("Color interpolation: %s", lerp_method)

    if args.dry_run:
        _dry_run(config)
        sys.exit(0)

    # -- Device discovery ---------------------------------------------------
    # Collect all unique IPs from config groups for fallback discovery.
    groups: dict[str, list[str]] = config.get("groups", {})
    known_ips: list[str] = sorted(
        {ip for ips in groups.values() for ip in ips}
    )
    nicknames: dict[str, str] = config.get("nicknames", {})
    dm: DeviceManager = DeviceManager(
        known_ips=known_ips, nicknames=nicknames,
    )
    logging.info("Discovering LIFX devices...")
    devices: list[dict[str, Any]] = dm.discover()
    logging.info("Found %d device(s)", len(devices))
    for dev_info in devices:
        logging.info(
            "  %s — %s (%s) [%s zones]",
            dev_info.get("label", "?"),
            dev_info.get("product", "?"),
            dev_info.get("ip", "?"),
            dev_info.get("zones", "?"),
        )

    # -- Scheduler thread ---------------------------------------------------
    scheduler: Optional[SchedulerThread] = None
    if config.get("schedule"):
        scheduler = SchedulerThread(config, dm)
        scheduler.start()
    else:
        logging.info("No schedule configured — API-only mode")

    # -- HTTP server --------------------------------------------------------
    port: int = config.get("port", DEFAULT_PORT)

    GlowUpRequestHandler.device_manager = dm
    GlowUpRequestHandler.auth_token = config["auth_token"]
    GlowUpRequestHandler.scheduler = scheduler
    GlowUpRequestHandler.config = config
    GlowUpRequestHandler.config_path = config_path

    server: ThreadedHTTPServer = ThreadedHTTPServer(
        ("", port), GlowUpRequestHandler,
    )

    # Graceful shutdown on SIGINT / SIGTERM.
    # server.shutdown() must be called from a different thread than
    # serve_forever() to avoid deadlock, so the signal handler sets
    # an event and a watcher thread performs the actual shutdown.
    shutdown_event: threading.Event = threading.Event()

    def _handle_signal(signum: int, frame: Any) -> None:
        """Signal handler — just sets the event, avoids deadlock."""
        if not shutdown_event.is_set():
            logging.info("Received signal %d, shutting down...", signum)
            shutdown_event.set()

    def _shutdown_watcher() -> None:
        """Background thread that waits for shutdown signal."""
        shutdown_event.wait()
        server.shutdown()

    watcher: threading.Thread = threading.Thread(
        target=_shutdown_watcher, daemon=True,
    )
    watcher.start()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logging.info("GlowUp server v%s listening on port %d", __version__, port)
    logging.info(
        "API base: http://localhost:%d/api/", port,
    )

    try:
        server.serve_forever()
    finally:
        logging.info("Shutting down...")
        if scheduler is not None:
            scheduler.stop()
            scheduler.join(timeout=5.0)
        dm.shutdown()
        server.server_close()
        logging.info("Server stopped")


if __name__ == "__main__":
    main()
