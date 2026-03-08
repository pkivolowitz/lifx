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

__version__ = "2.0"

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

from solar import SunTimes, sun_times

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

    procs: list[subprocess.Popen] = field(default_factory=list)
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
    utc_offset: timedelta = now.utcoffset()
    today: date = now.date()
    yesterday: date = today - timedelta(days=1)

    # Resolve for today — handles entries starting today.
    today_resolved = _resolve_entries(
        specs, lat, lon, today, utc_offset, group_filter=group_name,
    )

    # Resolve for yesterday — handles overnight entries from yesterday.
    yesterday_resolved = _resolve_entries(
        specs, lat, lon, yesterday, utc_offset, group_filter=group_name,
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

    Args:
        spec:        Schedule entry dict with ``effect`` and ``params`` keys.
        ip:          Target device IP or hostname.
        main_script: Absolute path to ``glowup.py``.

    Returns:
        A list of strings suitable for :func:`subprocess.Popen`.
    """
    cmd: list[str] = [
        sys.executable, main_script, "play", spec["effect"], "--ip", ip,
    ]

    for key, value in spec.get("params", {}).items():
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


def _stop_group(state: GroupState) -> None:
    """Stop all subprocesses for a device group.

    Args:
        state: The group's runtime state.
    """
    for proc in state.procs:
        _stop_subprocess(proc)
    state.procs.clear()
    state.current_entry_name = None


def _start_group(
    state: GroupState,
    spec: dict[str, Any],
    ips: list[str],
    main_script: str,
) -> None:
    """Start effect player subprocesses for all devices in a group.

    One subprocess is spawned per device IP in the group.

    Args:
        state:       The group's runtime state (modified in place).
        spec:        The schedule entry dict to start.
        ips:         List of device IPs/hostnames in the group.
        main_script: Absolute path to ``glowup.py``.
    """
    entry_name: str = spec.get("name", "?")

    for ip in ips:
        cmd: list[str] = _build_command(spec, ip, main_script)
        logging.info(
            "Starting '%s' on %s: %s", entry_name, ip, " ".join(cmd),
        )
        proc: subprocess.Popen = subprocess.Popen(cmd)
        state.procs.append(proc)

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

    for i, proc in enumerate(state.procs):
        if proc.poll() is not None:
            ip: str = ips[i] if i < len(ips) else "?"
            logging.warning(
                "Effect '%s' on %s exited (code %d), restarting",
                entry_name, ip, proc.returncode,
            )
            cmd: list[str] = _build_command(spec, ip, main_script)
            state.procs[i] = subprocess.Popen(cmd)


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
    groups: dict[str, list[str]] = config["groups"]
    for group_name, ips in groups.items():
        if group_name.startswith("_"):
            # Allow "_comment" keys.
            continue
        if not isinstance(ips, list) or not ips:
            raise ValueError(
                f"Group '{group_name}' must be a non-empty list of IP addresses"
            )
        for ip in ips:
            if not isinstance(ip, str) or not ip:
                raise ValueError(
                    f"Group '{group_name}' contains invalid IP entry: {ip!r}"
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
    groups: dict[str, list[str]] = _get_groups(config)
    specs: list[dict[str, Any]] = config["schedule"]

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
            params_str: str = ""
            if spec.get("params"):
                params_str = f"  params: {spec['params']}"
            print(
                f"  {spec.get('name', '?'):20s}  "
                f"{start.strftime('%H:%M')} -> {stop_str}  "
                f"[{spec['effect']}]{params_str}{status}"
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
    groups: dict[str, list[str]] = _get_groups(config)
    specs: list[dict[str, Any]] = config["schedule"]

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
                    _stop_group(state)

                if active is not None:
                    _start_group(state, active, ips, main_script)
                else:
                    logging.info(
                        "[%s] No active schedule entry — lights off",
                        group_name,
                    )
                    state.current_entry_name = None

            elif active is not None and state.procs:
                # Same entry still active — check for crashed subprocesses.
                any_dead: bool = any(
                    p.poll() is not None for p in state.procs
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
            _stop_group(state)

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
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        logging.error("Configuration error: %s", exc)
        sys.exit(1)

    if args.dry_run:
        _dry_run(config)
    else:
        _main_loop(config)


if __name__ == "__main__":
    main()
