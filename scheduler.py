"""LIFX effect scheduler — daemon that runs effects on a timed schedule.

Reads a JSON configuration file defining device groups and when effects
should play on each group, using symbolic times (sunrise, sunset, dawn,
dusk, noon, midnight) with optional offsets.  Each group is managed
independently — multiple groups can run different effects concurrently.

Spawns one LIFX effect player subprocess per device and manages their
lifecycles.  Designed to run as a systemd service on a Raspberry Pi.

Usage::

    python3 scheduler.py /etc/glowup/schedule.json
    python3 scheduler.py --dry-run schedule.json   # preview resolved times
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.2"

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time as time_mod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from effects import get_registry
from solar import SunTimes, sun_times

# Optional imports for label/MAC resolution.  When available, group
# entries can be registry labels or MAC addresses (not just raw IPs).
# Falls back gracefully if the registry/keepalive modules aren't present.
try:
    from device_registry import DeviceRegistry
    from infrastructure.bulb_keepalive import _read_arp, LIFX_OUI
    _HAS_RESOLUTION: bool = True
except ImportError:
    _HAS_RESOLUTION = False

try:
    from state_store import StateStore as _StateStore
    _HAS_STATE_STORE: bool = True
except ImportError:
    _HAS_STATE_STORE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How often to check if the active schedule entry has changed (seconds).
POLL_INTERVAL_SECONDS: int = 30

# Delay before restarting a subprocess that exited unexpectedly (seconds).
RESTART_DELAY_SECONDS: int = 10

# Timeout for subprocess to exit after SIGTERM before escalating to SIGKILL.
TERMINATE_TIMEOUT_SECONDS: int = 10

# Default configuration file path.
DEFAULT_CONFIG_PATH: str = "/etc/glowup/schedule.json"

# Regex for symbolic time specifications.
# Matches: "sunrise", "sunset+30m", "noon-1h30m", "midnight+2h", etc.
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

# Day-of-week letter to weekday index (Monday=0 .. Sunday=6).
# Matches Python's date.weekday() convention.
DAY_LETTER_TO_WEEKDAY: dict[str, int] = {
    "M": 0, "T": 1, "W": 2, "R": 3, "F": 4, "S": 5, "U": 6,
}

# All valid day letters (for validation).
VALID_DAY_LETTERS: str = "MTWRFSU"

# Logging format.
LOG_FORMAT: str = "%(asctime)s %(levelname)s %(message)s"
LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GroupState:
    """Runtime state for a single device group.

    Tracks the currently running effect player subprocesses (one per
    device IP in the group) and the name of the active schedule entry.
    """

    # IP → subprocess mapping.  Dict (not list) so that process-to-device
    # identity survives IP list reordering or changes during operation.
    procs: dict[str, subprocess.Popen] = field(default_factory=dict)
    current_entry_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

def _parse_time_spec(
    spec: str,
    sun: SunTimes,
    d: date,
    utc_offset: timedelta,
) -> Optional[datetime]:
    """Parse a time specification into a timezone-aware datetime.

    Supports three formats:

    - **Fixed times**: ``"14:30"``, ``"06:00"``
    - **Symbolic times**: ``"sunrise"``, ``"sunset"``, ``"dawn"``,
      ``"dusk"``, ``"noon"``, ``"midnight"``
    - **Symbolic with offsets**: ``"sunset+30m"``, ``"sunrise-1h"``,
      ``"dawn+1h30m"``

    Args:
        spec:       The time specification string.
        sun:        Precomputed solar event times for date *d*.
        d:          Calendar date for resolving the time.
        utc_offset: Local UTC offset as a timedelta.

    Returns:
        A timezone-aware datetime, or ``None`` if the symbolic sun event
        does not occur on this date (polar day/night).
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


# ---------------------------------------------------------------------------
# Day-of-week filtering
# ---------------------------------------------------------------------------

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

    Args:
        days_str: Day letter string (e.g. ``"MTWRF"``).

    Returns:
        A display string like ``"Weekdays"``, ``"Weekends"``, ``"Daily"``,
        or the sorted letter string.
    """
    if not days_str:
        return "Daily"
    upper: str = days_str.upper()
    canonical: str = "".join(ch for ch in VALID_DAY_LETTERS if ch in upper)
    if canonical == VALID_DAY_LETTERS:
        return "Daily"
    if canonical == "MTWRF":
        return "Weekdays"
    if canonical == "SU":
        return "Weekends"
    return canonical


# ---------------------------------------------------------------------------
# Schedule resolution
# ---------------------------------------------------------------------------

def _resolve_entries(
    specs: list[dict[str, Any]],
    lat: float,
    lon: float,
    d: date,
    utc_offset: timedelta,
    group_filter: Optional[str] = None,
) -> list[tuple[datetime, datetime, dict[str, Any]]]:
    """Resolve schedule entries for a specific date.

    Each entry in *specs* has ``start``, ``stop``, ``effect``, ``group``,
    and optional ``params`` keys.  Symbolic times are resolved against
    sun positions for date *d*.

    Args:
        specs:        List of raw schedule entry dicts from the config file.
        lat:          Observer latitude in degrees.
        lon:          Observer longitude in degrees.
        d:            Calendar date for sun time resolution.
        utc_offset:   Local UTC offset.
        group_filter: If set, only include entries matching this group name.

    Returns:
        A list of ``(start_datetime, stop_datetime, spec_dict)`` tuples.
        Entries where start or stop could not be resolved are omitted.
    """
    sun: SunTimes = sun_times(lat, lon, d, utc_offset)
    resolved: list[tuple[datetime, datetime, dict[str, Any]]] = []

    for spec in specs:
        # Filter by group if requested.
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
        # Strict less-than: stop == start means a zero-duration entry
        # (skip), not an overnight entry spanning 24 hours.
        if stop < start:
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

    Checks resolved schedules for both today and yesterday to correctly
    handle overnight entries that started yesterday and extend past midnight.
    First matching entry wins (config file order is priority order).

    Args:
        specs:      Raw schedule entry dicts from the config file.
        lat:        Observer latitude in degrees.
        lon:        Observer longitude in degrees.
        now:        Current timezone-aware datetime.
        group_name: Only consider entries targeting this group.

    Returns:
        The matching spec dict, or ``None`` if no entry is active.
    """
    today: date = now.date()
    yesterday: date = today - timedelta(days=1)

    # Compute UTC offset per date — DST transitions can change the
    # offset between yesterday and today.
    today_offset: timedelta = now.utcoffset()
    # Build a datetime for yesterday in the same timezone to get
    # yesterday's UTC offset (handles spring-forward / fall-back).
    yesterday_dt: datetime = now - timedelta(days=1)
    yesterday_offset: timedelta = yesterday_dt.utcoffset()

    # Resolve for today — handles entries starting today.
    today_resolved = _resolve_entries(
        specs, lat, lon, today, today_offset, group_filter=group_name,
    )

    # Resolve for yesterday — handles overnight entries from yesterday.
    yesterday_resolved = _resolve_entries(
        specs, lat, lon, yesterday, yesterday_offset, group_filter=group_name,
    )

    # Check today's entries first (higher priority for same-day matches).
    for start, stop, spec in today_resolved:
        if start <= now < stop:
            return spec

    # Then check yesterday's overnight entries.
    for start, stop, spec in yesterday_resolved:
        if start <= now < stop:
            return spec

    return None


# ---------------------------------------------------------------------------
# Subprocess management
# ---------------------------------------------------------------------------

def _build_command(
    spec: dict[str, Any],
    ip: str,
    main_script: str,
) -> list[str]:
    """Build the subprocess command list for running an effect on one device.

    Validates that the effect name is registered and that all parameter
    keys are declared by the effect's ``Param`` definitions, preventing
    injection of arbitrary CLI flags.

    Args:
        spec:        Schedule entry dict with ``effect`` and ``params`` keys.
        ip:          Target device IP or hostname.
        main_script: Absolute path to ``glowup.py``.

    Returns:
        A list of strings suitable for :func:`subprocess.Popen`.

    Raises:
        ValueError: If the effect name is unknown or a parameter key
                    is not declared by the effect.
    """
    effect_name: str = spec["effect"]

    # Validate effect name against the registry.
    registry: dict[str, Any] = get_registry()
    if effect_name not in registry:
        raise ValueError(f"Unknown effect: {effect_name!r}")

    # Validate parameter keys against the effect's declared Params.
    effect_cls = registry[effect_name]
    declared_params: set[str] = {
        name for name, attr in vars(effect_cls).items()
        if hasattr(attr, "default") and hasattr(attr, "validate")
    }

    cmd: list[str] = [
        sys.executable, main_script, "play", effect_name, "--ip", ip,
    ]

    for key, value in spec.get("params", {}).items():
        if key not in declared_params:
            raise ValueError(
                f"Effect {effect_name!r} has no parameter {key!r}"
            )
        # Convert param names from config (underscores) to CLI flags (hyphens).
        flag: str = f"--{key.replace('_', '-')}"
        cmd.extend([flag, str(value)])

    return cmd


def _stop_subprocess(proc: subprocess.Popen) -> None:
    """Gracefully stop a subprocess with SIGTERM, escalating to SIGKILL.

    Args:
        proc: The subprocess to stop.
    """
    if proc.poll() is not None:
        return

    logging.info("Sending SIGTERM to effect player (pid %d)", proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logging.warning(
            "Effect player did not exit after %ds, sending SIGKILL",
            TERMINATE_TIMEOUT_SECONDS,
        )
        proc.kill()
        proc.wait()


def _stop_group(
    state: GroupState,
    store: Optional["_StateStore"] = None,
) -> None:
    """Stop all subprocesses for a device group.

    Args:
        state: The group's runtime state.
        store: Optional state store — records devices as idle when provided.
    """
    # Capture IPs before clearing procs so the state store update has them.
    ips: list[str] = list(state.procs.keys())
    for proc in state.procs.values():
        _stop_subprocess(proc)
    state.procs.clear()
    state.current_entry_name = None

    if store is not None:
        for ip in ips:
            store.upsert(ip=ip, power=False, effect=None, source="scheduler")


def _start_group(
    state: GroupState,
    spec: dict[str, Any],
    ips: list[str],
    main_script: str,
    store: Optional["_StateStore"] = None,
) -> None:
    """Start effect player subprocesses for all devices in a group.

    One subprocess is spawned per device IP in the group.

    Args:
        state:       The group's runtime state (modified in place).
        spec:        The schedule entry dict to start.
        ips:         List of device IPs/hostnames in the group.
        main_script: Absolute path to ``glowup.py``.
        store:       Optional state store — records ownership when provided.
    """
    entry_name: str = spec.get("name", "?")
    effect_name: str = spec.get("effect", "")

    for ip in ips:
        cmd: list[str] = _build_command(spec, ip, main_script)
        logging.info(
            "Starting '%s' on %s: %s", entry_name, ip, " ".join(cmd),
        )
        proc: subprocess.Popen = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        state.procs[ip] = proc
        if store is not None:
            store.upsert(
                ip=ip,
                power=True,
                effect=effect_name,
                source="scheduler",
                entry=entry_name,
            )

    state.current_entry_name = entry_name


def _restart_dead_procs(
    state: GroupState,
    spec: dict[str, Any],
    ips: list[str],
    main_script: str,
) -> None:
    """Restart any subprocesses that exited unexpectedly.

    Matches processes to IPs by position in the list, so the group's
    IP list must not change while the scheduler is running.

    Args:
        state:       The group's runtime state (modified in place).
        spec:        The currently active schedule entry.
        ips:         List of device IPs/hostnames in the group.
        main_script: Absolute path to ``glowup.py``.
    """
    entry_name: str = spec.get("name", "?")

    for ip, proc in list(state.procs.items()):
        if proc.poll() is not None:
            logging.warning(
                "Effect '%s' on %s exited (code %d), restarting",
                entry_name, ip, proc.returncode,
            )
            cmd: list[str] = _build_command(spec, ip, main_script)
            state.procs[ip] = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_config(config_path: str) -> dict[str, Any]:
    """Load and validate the JSON configuration file.

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

    # Validate required top-level sections.
    if "location" not in config:
        raise ValueError("Config missing 'location' section")
    loc: dict = config["location"]
    if "latitude" not in loc or "longitude" not in loc:
        raise ValueError("Config location must have 'latitude' and 'longitude'")

    if "groups" not in config or not config["groups"]:
        raise ValueError("Config missing or empty 'groups' section")

    # Validate groups: each must be a non-empty list of strings.
    # Entries can be IP addresses, MAC addresses, or registry labels —
    # resolved to IPs at runtime via _resolve_groups().
    groups: dict[str, list[str]] = config["groups"]
    for group_name, entries in groups.items():
        if group_name.startswith("_"):
            # Allow "_comment" keys.
            continue
        if not isinstance(entries, list) or not entries:
            raise ValueError(
                f"Group '{group_name}' must be a non-empty list of "
                f"device identifiers (IPs, MACs, or registry labels)"
            )
        for entry in entries:
            if not isinstance(entry, str) or not entry:
                raise ValueError(
                    f"Group '{group_name}' contains invalid entry: {entry!r}"
                )

    if "schedule" not in config or not config["schedule"]:
        raise ValueError("Config missing or empty 'schedule' list")

    # Validate each schedule entry.
    known_groups: set[str] = {
        k for k in groups if not k.startswith("_")
    }
    for i, entry in enumerate(config["schedule"]):
        label: str = entry.get("name", f"entry_{i}")
        for req_field in ("start", "stop", "effect", "group"):
            if req_field not in entry:
                raise ValueError(
                    f"Schedule entry '{label}' missing required field "
                    f"'{req_field}'"
                )
        if entry["group"] not in known_groups:
            raise ValueError(
                f"Schedule entry '{label}' references unknown group "
                f"'{entry['group']}'"
            )

    return config


def _get_groups(config: dict[str, Any]) -> dict[str, list[str]]:
    """Extract the device groups from config, excluding comment keys.

    Args:
        config: Parsed configuration dictionary.

    Returns:
        A dict mapping group names to lists of IP addresses/hostnames.
    """
    return {
        name: ips
        for name, ips in config["groups"].items()
        if not name.startswith("_")
    }


# ---------------------------------------------------------------------------
# Device identifier resolution (label/MAC → IP)
# ---------------------------------------------------------------------------

# Regex matching an IPv4 address.
_IP_PATTERN: re.Pattern[str] = re.compile(
    r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$"
)

# Regex matching a MAC address (colon-separated hex).
_MAC_PATTERN: re.Pattern[str] = re.compile(
    r"^[\da-fA-F]{2}(:[\da-fA-F]{2}){5}$"
)

# Default registry path — same as the server's default.
DEFAULT_REGISTRY_PATH: str = "/etc/glowup/device_registry.json"


def _resolve_groups(
    groups: dict[str, list[str]],
    registry_path: Optional[str] = None,
) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """Resolve label/MAC identifiers to live IPs in group definitions.

    Uses the device registry (label → MAC) and the system ARP table
    (MAC → IP) to resolve non-IP identifiers.  Raw IPs pass through
    unchanged.

    This is the standalone equivalent of the server's
    ``_resolve_config_groups()`` — it does not require the server or
    BulbKeepAlive daemon to be running; it reads the ARP table directly.

    Args:
        groups:        Dict mapping group names to identifier lists.
        registry_path: Path to device_registry.json (default:
                       ``/etc/glowup/device_registry.json``).

    Returns:
        A tuple of (resolved_groups, unresolved) where resolved_groups
        has the same structure but with IPs, and unresolved is a list
        of (group_name, identifier) pairs that could not be resolved.
    """
    if not _HAS_RESOLUTION:
        # No resolution modules available — assume all entries are IPs.
        return dict(groups), []

    # Load the registry if available.
    registry: Optional[DeviceRegistry] = None
    reg_path: str = registry_path or DEFAULT_REGISTRY_PATH
    if os.path.exists(reg_path):
        registry = DeviceRegistry()
        registry.load(reg_path)
        logging.info("Loaded device registry from %s", reg_path)

    # Read the current ARP table for MAC → IP resolution.
    arp_table: dict[str, str] = _read_arp()  # {IP: MAC}
    # Build reverse map: {MAC: IP}.
    mac_to_ip: dict[str, str] = {
        mac.lower(): ip for ip, mac in arp_table.items()
    }

    resolved: dict[str, list[str]] = {}
    unresolved: list[tuple[str, str]] = []

    for gname, identifiers in groups.items():
        resolved_ips: list[str] = []
        for ident in identifiers:
            ip: Optional[str] = _resolve_identifier(
                ident, registry, mac_to_ip,
            )
            if ip is not None:
                resolved_ips.append(ip)
            else:
                unresolved.append((gname, ident))
                logging.warning(
                    "Could not resolve '%s' in group '%s' to an IP",
                    ident, gname,
                )
        resolved[gname] = resolved_ips

    return resolved, unresolved


def _resolve_identifier(
    ident: str,
    registry: Optional["DeviceRegistry"],
    mac_to_ip: dict[str, str],
) -> Optional[str]:
    """Resolve a single device identifier to an IP address.

    Resolution chain:
    1. If it looks like an IP → return as-is.
    2. If it looks like a MAC → look up in ARP table.
    3. Otherwise → treat as label, look up in registry → MAC → ARP.

    Args:
        ident:     Device identifier (IP, MAC, or label).
        registry:  DeviceRegistry instance (or None).
        mac_to_ip: Reverse ARP table {MAC: IP}.

    Returns:
        Resolved IP address, or None if resolution failed.
    """
    ident_stripped: str = ident.strip()

    # 1. Already an IP?
    if _IP_PATTERN.match(ident_stripped):
        return ident_stripped

    # 2. MAC address?
    if _MAC_PATTERN.match(ident_stripped):
        return mac_to_ip.get(ident_stripped.lower())

    # 3. Registry label → MAC → IP.
    if registry is not None:
        mac: Optional[str] = registry.label_to_mac(ident_stripped)
        if mac:
            return mac_to_ip.get(mac.lower())

    return None


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


def _log_resolved_schedule(
    specs: list[dict[str, Any]],
    groups: dict[str, list[str]],
    lat: float,
    lon: float,
    d: date,
    utc_offset: timedelta,
) -> None:
    """Log the resolved schedule for all groups on a date.

    Args:
        specs:      Raw schedule entry dicts.
        groups:     Device groups mapping.
        lat:        Latitude.
        lon:        Longitude.
        d:          Calendar date.
        utc_offset: UTC offset.
    """
    for group_name in sorted(groups):
        resolved = _resolve_entries(
            specs, lat, lon, d, utc_offset, group_filter=group_name,
        )
        if not resolved:
            logging.info(
                "  Group '%s' (%s): no schedule entries",
                group_name, ", ".join(groups[group_name]),
            )
            continue

        ips_str: str = ", ".join(groups[group_name])
        logging.info("  Group '%s' (%s):", group_name, ips_str)
        for start, stop, spec in resolved:
            stop_fmt: str = (
                stop.strftime("%m/%d %H:%M")
                if stop.date() != start.date()
                else stop.strftime("%H:%M")
            )
            logging.info(
                "    %s: %s -> %s  [%s]",
                spec.get("name", "?"),
                start.strftime("%H:%M"),
                stop_fmt,
                spec["effect"],
            )


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

def _dry_run(config: dict[str, Any]) -> None:
    """Print the resolved schedule without running any effects.

    Shows solar events, device groups, and the resolved schedule for
    each group.  Useful for verifying that time specifications resolve
    correctly before deploying as a daemon.

    Args:
        config: Parsed configuration dictionary.
    """
    lat: float = config["location"]["latitude"]
    lon: float = config["location"]["longitude"]
    raw_groups: dict[str, list[str]] = _get_groups(config)
    specs: list[dict[str, Any]] = config["schedule"]

    # Resolve labels/MACs to IPs for display.
    groups, unresolved = _resolve_groups(raw_groups)

    now: datetime = datetime.now(timezone.utc).astimezone()
    utc_offset: timedelta = now.utcoffset()
    today: date = now.date()

    sun: SunTimes = sun_times(lat, lon, today, utc_offset)

    print(f"Location:   {lat}°N, {lon}°E")
    print(f"Date:       {today}")
    print(f"UTC offset: {utc_offset}")
    print()

    print("Solar events:")
    fmt: str = "%H:%M:%S"
    print(f"  Dawn:    {sun.dawn.strftime(fmt) if sun.dawn else 'N/A'}")
    print(f"  Sunrise: {sun.sunrise.strftime(fmt) if sun.sunrise else 'N/A'}")
    print(f"  Noon:    {sun.noon.strftime(fmt)}")
    print(f"  Sunset:  {sun.sunset.strftime(fmt) if sun.sunset else 'N/A'}")
    print(f"  Dusk:    {sun.dusk.strftime(fmt) if sun.dusk else 'N/A'}")
    print()

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
# Main loop
# ---------------------------------------------------------------------------

def _main_loop(config: dict[str, Any]) -> None:
    """Run the scheduler main loop.

    Manages one independent set of subprocesses per device group.
    Polls periodically to check which schedule entry should be active
    for each group, starting and stopping effect player subprocesses
    as needed.

    Args:
        config: Parsed configuration dictionary.
    """
    lat: float = config["location"]["latitude"]
    lon: float = config["location"]["longitude"]
    raw_groups: dict[str, list[str]] = _get_groups(config)
    specs: list[dict[str, Any]] = config["schedule"]

    # Optional state store — records which effect is running on each device
    # so the server dashboard can show scheduler-owned devices accurately.
    # Path from config key 'state_db'; defaults to state.db alongside config.
    store: Optional["_StateStore"] = None
    if _HAS_STATE_STORE:
        default_db: str = os.path.join(
            os.path.dirname(os.path.abspath(
                config.get("_config_path", "schedule.json")
            )),
            "state.db",
        )
        db_path: str = config.get("state_db", default_db)
        store = _StateStore.open(db_path)

    # Resolve labels/MACs to IPs.  If resolution modules aren't
    # available, identifiers are assumed to be raw IPs (backward compat).
    groups, unresolved = _resolve_groups(raw_groups)
    if unresolved:
        logging.warning(
            "%d device identifier(s) could not be resolved: %s",
            len(unresolved),
            ", ".join(f"{g}:{ident}" for g, ident in unresolved),
        )

    # Locate glowup.py relative to this script.
    script_dir: str = os.path.dirname(os.path.abspath(__file__))
    main_script: str = os.path.join(script_dir, "glowup.py")

    if not os.path.isfile(main_script):
        logging.error("Cannot find glowup.py at %s", main_script)
        sys.exit(1)

    # Per-group runtime state.
    group_states: dict[str, GroupState] = {
        name: GroupState() for name in groups
    }
    last_logged_date: Optional[date] = None

    # Shutdown flag set by signal handler.
    running: bool = True

    def _handle_signal(signum: int, frame: Any) -> None:
        """Set the shutdown flag on SIGTERM or SIGINT."""
        nonlocal running
        logging.info("Received signal %d, shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    total_devices: int = sum(len(ips) for ips in groups.values())
    logging.info(
        "Scheduler started — %d groups, %d devices, %d schedule entries",
        len(groups), total_devices, len(specs),
    )
    for name, ips in sorted(groups.items()):
        logging.info("  Group '%s': %s", name, ", ".join(ips))

    while running:
        now: datetime = datetime.now(timezone.utc).astimezone()
        today: date = now.date()

        # Log sun times and resolved schedule once per day.
        if today != last_logged_date:
            utc_offset: timedelta = now.utcoffset()
            sun: SunTimes = sun_times(lat, lon, today, utc_offset)
            _log_sun_times(sun, today)
            _log_resolved_schedule(
                specs, groups, lat, lon, today, utc_offset,
            )
            last_logged_date = today

        # --- Per-group scheduling ---
        for group_name, ips in groups.items():
            state: GroupState = group_states[group_name]

            # Find which entry (if any) should be active for this group.
            active: Optional[dict[str, Any]] = _find_active_entry(
                specs, lat, lon, now, group_name,
            )
            active_name: Optional[str] = (
                active.get("name") if active else None
            )

            if active_name != state.current_entry_name:
                # --- Transition: stop current, start new ---
                if state.procs:
                    logging.info(
                        "[%s] Stopping '%s'",
                        group_name, state.current_entry_name,
                    )
                    _stop_group(state, store)

                if active is not None:
                    _start_group(state, active, ips, main_script, store)
                else:
                    logging.info(
                        "[%s] No active schedule entry — lights off",
                        group_name,
                    )
                    state.current_entry_name = None

            elif active is not None and state.procs:
                # Same entry still active — check for crashed subprocesses.
                any_dead: bool = any(
                    p.poll() is not None for p in state.procs.values()
                )
                if any_dead:
                    _restart_dead_procs(state, active, ips, main_script)

        # Sleep until next poll, checking for shutdown every second.
        poll_deadline: float = time_mod.time() + POLL_INTERVAL_SECONDS
        while running and time_mod.time() < poll_deadline:
            time_mod.sleep(1)

    # --- Cleanup on shutdown ---
    for group_name, state in group_states.items():
        if state.procs:
            logging.info(
                "Shutting down — stopping group '%s' ('%s')",
                group_name, state.current_entry_name,
            )
            _stop_group(state, store)

    logging.info("Scheduler stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser.

    Returns:
        A configured :class:`argparse.ArgumentParser`.
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="glowup-scheduler",
        description="GlowUp effect scheduler — runs effects on a timed "
                    "schedule with device groups",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to schedule config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resolved schedule and exit without running effects",
    )
    return parser


def main() -> None:
    """Entry point for the scheduler daemon."""
    parser: argparse.ArgumentParser = _build_parser()
    args: argparse.Namespace = parser.parse_args()

    # Set up logging to stderr (systemd journal captures this).
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )

    try:
        config: dict[str, Any] = _load_config(args.config)
        # Inject the config path so _main_loop can derive the default
        # state.db path without needing a separate parameter.
        config["_config_path"] = os.path.abspath(args.config)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        logging.error("Configuration error: %s", exc)
        sys.exit(1)

    if args.dry_run:
        _dry_run(config)
    else:
        _main_loop(config)


if __name__ == "__main__":
    main()
