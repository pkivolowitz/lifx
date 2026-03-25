#!/usr/bin/env python3
"""LIFX Effect Engine -- command-line interface.

Usage::

    python3 glowup.py discover                    # find all LIFX devices
    python3 glowup.py effects                     # list available effects
    python3 glowup.py identify --ip <device-ip>   # pulse a device to locate it
    python3 glowup.py monitor --ip <device-ip>           # monitor device in real time
    python3 glowup.py play cylon --ip <device-ip>    # run an effect on one device
    python3 glowup.py play cylon --group office               # virtual multizone (from server)
    python3 glowup.py play cylon --group office --config c.json  # virtual multizone (local file)
    python3 glowup.py replay --file song.mid             # replay MIDI at real-time tempo
    python3 glowup.py replay --file song.mid --speed 0   # bulk ingest (fast as possible)

All effect parameters are auto-generated from each effect's :class:`Param`
declarations.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.0"

import argparse
import json
import math
import platform
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from transport import LifxDevice, discover_devices
from emitters import Emitter
from emitters.lifx import LifxEmitter
from emitters.virtual import VirtualMultizoneEmitter
from engine import Controller
from effects import (
    get_registry, get_effect_names, create_effect,
    HSBK, HSBK_MAX, KELVIN_DEFAULT, ALL_DEVICE_TYPES,
)
from colorspace import set_lerp_method
from network_config import net
from simulator import create_simulator

# ---------------------------------------------------------------------------
# Named constants -- no magic numbers
# ---------------------------------------------------------------------------

DEFAULT_DISCOVERY_TIMEOUT: float = 5.0
"""Seconds to wait for LIFX UDP discovery responses."""

DEFAULT_FPS: int = 20
"""Frames per second for the effect render loop."""

DEFAULT_FADE_MS: int = 500
"""Milliseconds for the fade-to-black when stopping an effect."""

IDENTIFY_CYCLE_SECONDS: float = 3.0
"""Seconds per full brightness pulse during identify."""

IDENTIFY_FRAME_INTERVAL: float = 0.05
"""Seconds between brightness updates during identify (20 fps)."""

IDENTIFY_MIN_BRI: float = 0.05
"""Minimum brightness fraction during identify pulse (5%)."""

SIM_STOP_CHECK_MS: int = 100
"""Interval in milliseconds between stop-event checks in simulator mode."""

# Record subcommand defaults.
DEFAULT_RECORD_ZONES: int = 108
"""Default zone count for recordings (matches a 36-bulb string light)."""

DEFAULT_RECORD_ZPB: int = 3
"""Default zones-per-bulb for recordings (LIFX string lights)."""

DEFAULT_RECORD_DURATION: float = 5.0
"""Default recording duration in seconds when no period is available."""

DEFAULT_RECORD_WIDTH: int = 600
"""Default output width in pixels."""

DEFAULT_RECORD_HEIGHT: int = 80
"""Default output height in pixels."""

DEFAULT_RECORD_FORMAT: str = "gif"
"""Default output format."""

RECORD_BG_COLOR: tuple[int, int, int] = (26, 26, 26)
"""Background RGB color for the strip image (dark grey, matches simulator)."""

RECORD_ZONE_GAP: int = 1
"""Gap in pixels between bulbs in the recording."""

RECORD_SUPPORTED_FORMATS: list[str] = ["gif", "mp4", "webm"]
"""Output formats supported by the record subcommand."""

DEFAULT_MONITOR_POLL_HZ: float = 4.0
"""Default polling rate in Hz for monitor mode."""

MIN_MONITOR_POLL_HZ: float = 0.5
"""Minimum polling rate for monitor mode (once every 2 seconds)."""

MAX_MONITOR_POLL_HZ: float = 20.0
"""Maximum polling rate for monitor mode."""

# Replay subcommand defaults.
DEFAULT_REPLAY_SPEED: float = 1.0
"""Replay speed multiplier.  1.0 = real-time, 0 = as fast as possible."""

DEFAULT_REPLAY_BROKER: str = net.broker
"""Default MQTT broker for replay (Pi)."""

DEFAULT_REPLAY_PORT: int = 1883
"""Default MQTT broker port."""

DEFAULT_REPLAY_SIGNAL: str = "sensor:midi:events"
"""Default signal name for MIDI replay events on the bus."""

# Server connection defaults (for fetching groups remotely).
DEFAULT_SERVER_HOST: str = net.server
"""Default GlowUp server hostname (the Pi)."""

DEFAULT_SERVER_PORT: int = 8420
"""Default GlowUp server port."""

TOKEN_PATH: Path = Path.home() / ".glowup_token"
"""Path to the bearer-token file for server authentication."""

SERVER_TIMEOUT_SECONDS: float = 5.0
"""HTTP timeout for server API requests."""

COMMAND_DISCOVER_TIMEOUT_SECONDS: float = 15.0
"""HTTP timeout for /api/command/discover (waits for all device UDP queries)."""

IDENTIFY_DEFAULT_DURATION: float = 60.0
"""Default identify pulse duration when routing via server (seconds)."""

# Timeout for the server probe that runs at startup to decide routing mode.
SERVER_PROBE_TIMEOUT: float = 1.5
"""Seconds to wait for /api/status probe before falling back to direct UDP."""


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def _install_stop_signal(
    stop_event: threading.Event,
) -> threading.Event:
    """Install SIGINT/SIGTERM handlers that set *stop_event*.

    This is the standard pattern for interruptible CLI commands:
    create a :class:`threading.Event`, wire signal handlers to set it,
    then wait or poll via ``stop_event.wait()`` / ``stop_event.is_set()``.

    Args:
        stop_event: The event to set when a signal is received.

    Returns:
        The same *stop_event* for convenience (allows one-liner init).
    """

    def _handler(signum: int, frame: Any) -> None:
        """Signal handler — sets the stop event."""
        stop_event.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    return stop_event

# Minimum column widths for the discovery table display.
# These prevent columns from collapsing when device labels are short.
_COL_MIN_LABEL: int = 12
_COL_MIN_PRODUCT: int = 14
_COL_MIN_GROUP: int = 8
_COL_MIN_IP: int = 13
_COL_MIN_MAC: int = 17
_COL_MIN_ZONES: int = 5
_COL_MIN_REGISTRY: int = 10

# Column separator used in the discovery table
_COL_SEP: str = "  "

# Startup banner — printed unless -q/--quiet is given.
_BANNER: str = (
    f"GlowUp v{__version__} — LIFX Effect Engine\n"
    "Copyright (c) 2026 Perry Kivolowitz. All rights reserved.\n"
    "Licensed under the MIT License.\n"
)

# Module-level quiet flag — set by main() before any subcommand runs.
_quiet: bool = False

# Active server URL (``host:port``) used for routing commands.  Set in
# ``main()`` after probing for the server.  ``None`` means direct UDP.
_server_url: Optional[str] = None


def _print(*args: Any, **kwargs: Any) -> None:
    """Print unless quiet mode is active.

    Drop-in replacement for :func:`print` that respects the global
    ``_quiet`` flag.  Error output (``file=sys.stderr``) is never
    suppressed.

    Args:
        *args:   Positional arguments forwarded to :func:`print`.
        **kwargs: Keyword arguments forwarded to :func:`print`.
    """
    if _quiet and kwargs.get("file") is not sys.stderr:
        return
    print(*args, **kwargs)


# ---------------------------------------------------------------------------
# Null emitter — geometry-only stub for --sim-only mode
# ---------------------------------------------------------------------------

class _NullEmitter(Emitter):
    """Geometry-only emitter stub used by ``--sim-only``.

    Implements the :class:`Emitter` ABC with no-op write methods.
    This guarantees that ``--sim-only`` never sends a single UDP packet
    to physical hardware after the initial query.

    Attributes:
        _pre_poly_map: Pre-computed per-zone polychrome list extracted
                       from the real device before it was closed.  Used
                       by :func:`_build_polychrome_map` to produce an
                       accurate simulator colour map.
    """

    def __init__(
        self,
        zone_count: int,
        label: str,
        product_name: str,
        ip: str,
        pre_poly_map: list[bool],
    ) -> None:
        """Initialise with geometry copied from the real device.

        Args:
            zone_count:    Number of zones.
            label:         Device / group label.
            product_name:  Human-readable product string.
            ip:            IP or group description (display only).
            pre_poly_map:  Per-zone polychrome flags from the real
                           device, extracted before it was closed.
        """
        self._zone_count: int = zone_count
        self._label: str = label
        self._product_name: str = product_name
        self._ip: str = ip
        self._pre_poly_map: list[bool] = pre_poly_map

    # --- Emitter properties ---

    @property
    def zone_count(self) -> Optional[int]:
        """Number of zones copied from the real device."""
        return self._zone_count

    @property
    def is_multizone(self) -> bool:
        """Always ``True`` so the engine uses the zone-accurate path."""
        return True

    @property
    def emitter_id(self) -> str:
        """Display-only address string."""
        return self._ip

    @property
    def label(self) -> str:
        """Device or group label for display."""
        return self._label

    @property
    def product_name(self) -> str:
        """Human-readable product string."""
        return self._product_name

    # --- Frame dispatch (all no-ops) ---

    def send_zones(self, colors: list[HSBK], duration_ms: int = 0,
                   rapid: bool = True, **kwargs: Any) -> None:
        """No-op — sim-only mode never writes to physical devices."""

    def send_color(self, hue: int, sat: int, bri: int, kelvin: int,
                   duration_ms: int = 0) -> None:
        """No-op — sim-only mode never writes to physical devices."""

    # --- Lifecycle (all no-ops) ---

    def prepare_for_rendering(self) -> None:
        """No-op — no hardware to prepare."""

    def power_on(self, duration_ms: int = 0) -> None:
        """No-op — sim-only mode never writes to physical devices."""

    def power_off(self, duration_ms: int = 0) -> None:
        """No-op — sim-only mode never writes to physical devices."""

    def close(self) -> None:
        """No-op — the real socket was closed before this stub was created."""

    def get_info(self) -> dict:
        """Return device info for status reporting."""
        return {
            "id": self._ip,
            "label": self._label,
            "product": self._product_name,
            "zones": self._zone_count,
        }


# ---------------------------------------------------------------------------
# Config file helpers
# ---------------------------------------------------------------------------

def _load_group(config_path: str, group_name: str) -> list[str]:
    """Load a device group from a JSON config file.

    The config file must have a ``groups`` section mapping group names
    to lists of IP addresses or hostnames, e.g.::

        {"groups": {"office": ["192.0.2.25", "192.0.2.26"]}}

    Args:
        config_path: Path to the JSON config file.
        group_name:  Name of the group to load.

    Returns:
        A list of IP addresses / hostnames in the group.

    Raises:
        SystemExit: If the file cannot be read, parsed, or the group
                    is not found.
    """
    try:
        with open(config_path, "r") as f:
            config: dict = json.load(f)
    except FileNotFoundError:
        _print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        _print(f"ERROR: Invalid JSON in {config_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    groups: dict = config.get("groups", {})
    if group_name not in groups:
        available: str = ", ".join(sorted(groups.keys())) if groups else "(none)"
        _print(
            f"ERROR: Group '{group_name}' not found. "
            f"Available: {available}",
            file=sys.stderr,
        )
        sys.exit(1)

    ips: list = groups[group_name]
    if not ips:
        _print(f"ERROR: Group '{group_name}' is empty.", file=sys.stderr)
        sys.exit(1)

    return ips


def _probe_server(server: str) -> bool:
    """Check whether the GlowUp server is reachable.

    Sends a quick authenticated GET to ``/api/status``.  Uses a short
    timeout (:data:`SERVER_PROBE_TIMEOUT`) so startup is not noticeably
    delayed when the server is offline.

    Args:
        server: ``host:port`` of the server to probe.

    Returns:
        ``True`` if the server responded with HTTP 200, ``False``
        otherwise (unreachable, bad token, any error).
    """
    if not TOKEN_PATH.is_file():
        return False
    token: str = TOKEN_PATH.read_text().strip()
    if not token:
        return False
    url: str = f"http://{server}/api/status"
    req: urllib.request.Request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=SERVER_PROBE_TIMEOUT) as resp:
            return resp.status == 200
    except Exception:
        return False


def _fetch_group_from_server(
    group_name: str,
    server: str,
) -> list[str]:
    """Fetch a device group from the GlowUp server via its REST API.

    Reads the bearer token from :data:`TOKEN_PATH`, calls
    ``GET /api/groups`` on the server, and extracts the requested
    group.

    Args:
        group_name: Name of the group to retrieve.
        server:     ``host:port`` of the GlowUp server.

    Returns:
        A list of IP addresses / hostnames in the group.

    Raises:
        SystemExit: If the token file is missing, the server is
                    unreachable, or the group does not exist.
    """
    # --- Read auth token -----------------------------------------------------
    if not TOKEN_PATH.is_file():
        _print(
            f"ERROR: Token file not found: {TOKEN_PATH}\n"
            "       Create it with the server's auth_token value "
            "(chmod 600).",
            file=sys.stderr,
        )
        sys.exit(1)

    token: str = TOKEN_PATH.read_text().strip()
    if not token:
        _print(
            f"ERROR: Token file is empty: {TOKEN_PATH}",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Fetch groups from the server ----------------------------------------
    url: str = f"http://{server}/api/groups"
    req: urllib.request.Request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )

    try:
        with urllib.request.urlopen(req, timeout=SERVER_TIMEOUT_SECONDS) as resp:
            body: dict = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        _print(
            f"ERROR: Server returned {exc.code} for {url}: {exc.reason}",
            file=sys.stderr,
        )
        sys.exit(1)
    except (urllib.error.URLError, OSError) as exc:
        _print(
            f"ERROR: Cannot reach server at {server}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Extract the requested group -----------------------------------------
    groups: dict = body.get("groups", {})
    if group_name not in groups:
        available: str = (
            ", ".join(sorted(groups.keys())) if groups else "(none)"
        )
        _print(
            f"ERROR: Group '{group_name}' not found on server. "
            f"Available: {available}",
            file=sys.stderr,
        )
        sys.exit(1)

    ips: list = groups[group_name]
    if not ips:
        _print(
            f"ERROR: Group '{group_name}' is empty on server.",
            file=sys.stderr,
        )
        sys.exit(1)

    return ips


def _read_token() -> str:
    """Read the bearer token from :data:`TOKEN_PATH`.

    Returns:
        The token string, stripped of whitespace.

    Raises:
        SystemExit: If the file is missing or empty.
    """
    if not TOKEN_PATH.is_file():
        _print(
            f"ERROR: Token file not found: {TOKEN_PATH}\n"
            "       Create it with the server's auth_token value "
            "(chmod 600).",
            file=sys.stderr,
        )
        sys.exit(1)
    token: str = TOKEN_PATH.read_text().strip()
    if not token:
        _print(f"ERROR: Token file is empty: {TOKEN_PATH}", file=sys.stderr)
        sys.exit(1)
    return token


def _server_request(
    server: str,
    path: str,
    *,
    method: str = "GET",
    body: Optional[dict] = None,
    timeout: float = SERVER_TIMEOUT_SECONDS,
) -> dict:
    """Perform an authenticated HTTP request against the GlowUp server.

    Handles token loading, JSON serialization, error formatting, and
    ``sys.exit`` on failure — so callers stay concise.

    Args:
        server:  ``host:port`` of the server.
        path:    URL path (e.g. ``/api/status``).
        method:  HTTP method (``"GET"``, ``"POST"``, ``"DELETE"``).
        body:    Optional dict to serialize as a JSON request body.
                 Only meaningful for POST; ignored for other methods.
        timeout: HTTP request timeout in seconds.

    Returns:
        Parsed JSON response body as a dict.

    Raises:
        SystemExit: On HTTP error, network failure, or auth failure.
    """
    token: str = _read_token()
    url: str = f"http://{server}{path}"

    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    data: Optional[bytes] = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()

    req: urllib.request.Request = urllib.request.Request(
        url, data=data, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        err_detail: str = exc.read().decode(errors="replace")
        _print(
            f"ERROR: Server returned {exc.code} for {url}: {err_detail}",
            file=sys.stderr,
        )
        sys.exit(1)
    except (urllib.error.URLError, OSError) as exc:
        _print(
            f"ERROR: Cannot reach server at {server}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


def _server_get(
    server: str, path: str, *, timeout: float = SERVER_TIMEOUT_SECONDS,
) -> dict:
    """Authenticated GET.  See :func:`_server_request`."""
    return _server_request(server, path, method="GET", timeout=timeout)


def _server_post(
    server: str, path: str, body: dict,
    *, timeout: float = SERVER_TIMEOUT_SECONDS,
) -> dict:
    """Authenticated POST with JSON body.  See :func:`_server_request`."""
    return _server_request(
        server, path, method="POST", body=body, timeout=timeout,
    )


def _server_delete(
    server: str, path: str, *, timeout: float = SERVER_TIMEOUT_SECONDS,
) -> dict:
    """Authenticated DELETE.  See :func:`_server_request`."""
    return _server_request(server, path, method="DELETE", timeout=timeout)


def _connect_group(ips: list[str]) -> list[LifxDevice]:
    """Connect to and query a list of devices.

    Args:
        ips: List of IP addresses or hostnames.

    Returns:
        A list of connected, queried :class:`LifxDevice` instances.

    Raises:
        SystemExit: If any device fails to connect or respond.
    """
    devices: list[LifxDevice] = []
    for ip in ips:
        _print(f"  Connecting to {ip}...", flush=True)
        try:
            dev: LifxDevice = LifxDevice(ip)
        except ValueError as exc:
            # Close any already-connected devices before exiting.
            for d in devices:
                d.close()
            _print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        dev.query_all()

        if dev.product is None:
            for d in devices:
                d.close()
            dev.close()
            _print(f"ERROR: No response from {ip}.", file=sys.stderr)
            sys.exit(1)

        kind: str = dev.product_name or "?"
        if not dev.is_polychrome:
            kind += " (monochrome)"
        _print(f"    {dev.label or '?'} — {kind}", flush=True)
        devices.append(dev)

    return devices


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _print_discover_table(
    rows: List[Dict[str, str]], emit_json: bool = False
) -> None:
    """Print a formatted device table from a list of row dicts.

    Each dict must contain keys: ``label``, ``product``, ``group``,
    ``ip``, ``mac``, ``zones``.  The optional ``registry`` key shows
    the user-assigned registry label (from the device registry) when
    available.  Used by both the direct-UDP and server-routed discover
    paths so output is identical regardless of transport.

    Args:
        rows:      List of device info dicts.
        emit_json: If ``True``, also print a JSON representation.
    """
    # Only include the Registry column if any row has a non-empty value.
    has_registry: bool = any(r.get("registry", "") for r in rows)

    cols: List[Tuple[str, str, int]] = [
        ("Label",       "label",    _COL_MIN_LABEL),
        ("Product",     "product",  _COL_MIN_PRODUCT),
        ("Group",       "group",    _COL_MIN_GROUP),
        ("IP Address",  "ip",       _COL_MIN_IP),
        ("MAC Address", "mac",      _COL_MIN_MAC),
        ("Zones",       "zones",    _COL_MIN_ZONES),
    ]
    if has_registry:
        cols.append(("Registry", "registry", _COL_MIN_REGISTRY))

    widths: List[int] = []
    for header, key, min_w in cols:
        w: int = max(min_w, len(header),
                     max((len(r.get(key, "")) for r in rows), default=0))
        widths.append(w)

    header_line: str = _COL_SEP.join(
        cols[i][0].ljust(widths[i]) for i in range(len(cols))
    )
    _print(header_line)
    _print(_COL_SEP.join("-" * widths[i] for i in range(len(cols))))
    for r in rows:
        line: str = _COL_SEP.join(
            str(r.get(cols[i][1], "")).ljust(widths[i])
            for i in range(len(cols))
        )
        _print(line)
    _print(f"\n{len(rows)} device(s) found.")

    if emit_json:
        _print("\n" + json.dumps(
            [
                {
                    "label": r["label"], "product": r["product"],
                    "group": r["group"], "ip": r["ip"],
                    "mac": r["mac"], "zones": r["zones"],
                    **({"registry": r["registry"]}
                       if r.get("registry") else {}),
                }
                for r in rows
            ],
            indent=2,
        ))


def cmd_discover(args: argparse.Namespace) -> None:
    """Discover and display all LIFX devices on the local network.

    When the GlowUp server is reachable the query is executed on the
    server (which has unobstructed UDP access to every bulb on the LAN)
    and results are returned over HTTP.  When running locally (``--local``
    or server unreachable) a UDP broadcast or directed query is used.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.  Expected attributes:
        ``timeout`` (float), ``ip`` (str | None), ``json`` (bool).
    """
    if _server_url:
        # --- Server path: query all known bulbs (or a specific IP) from Pi ---
        path: str = (
            f"/api/command/discover?ip={args.ip}"
            if args.ip else "/api/command/discover"
        )
        if args.ip:
            _print(f"Querying {args.ip} via server...", flush=True)
        else:
            _print("Querying all known devices via server...", flush=True)
        body: dict = _server_get(
            _server_url, path,
            timeout=COMMAND_DISCOVER_TIMEOUT_SECONDS,
        )
        devices_raw: list = body.get("devices", [])
        if not devices_raw:
            _print("No devices responded.")
            return
        rows: List[Dict[str, str]] = [
            {
                "label":    d.get("label") or "?",
                "product":  d.get("product") or "?",
                "group":    d.get("group") or "",
                "ip":       d.get("ip", "?"),
                "mac":      d.get("mac", "?"),
                "zones":    str(d.get("zones") or "-"),
                "registry": d.get("registry_label") or "",
            }
            for d in devices_raw
        ]
        _print_discover_table(rows, emit_json=args.json)
        return

    # --- Direct UDP path (server unreachable or --local) ---------------------
    if args.ip:
        _print(f"Querying {args.ip}...", flush=True)
    else:
        _print("Scanning for LIFX devices...", flush=True)
    devices: list = discover_devices(timeout=args.timeout, target_ip=args.ip)

    if not devices:
        _print("No LIFX devices found.\n")
        _print("This does not necessarily mean your lights are absent — LIFX")
        _print("UDP discovery can be unreliable depending on your network.")
        _print("Common causes:\n")
        _print("  • Mesh routers (e.g. TP-Link Deco) may filter broadcast")
        _print("    packets between wireless nodes.")
        _print("  • Devices may be powered off or unreachable on a different")
        _print("    subnet or VLAN.")
        _print("  • Increasing --timeout (default 5s) can help on congested")
        _print("    networks.")
        _print("  • The LIFX app on your phone can confirm whether devices")
        _print("    are online.")
        _print("  • Check your router's admin page for connected devices and")
        _print("    their IP addresses, then add them to server.json groups.")
        return

    udp_rows: List[Dict[str, str]] = [
        {
            "label":   dev.label or "?",
            "product": dev.product_name or "?",
            "group":   dev.group or "",
            "ip":      dev.ip,
            "mac":     dev.mac_str,
            "zones":   str(dev.zone_count or "-"),
        }
        for dev in devices
    ]
    _print_discover_table(udp_rows, emit_json=args.json)


def cmd_effects(args: argparse.Namespace) -> None:
    """List every registered effect and its tunable parameters.

    Iterates the effect registry and prints each effect's name,
    description, and parameter definitions including defaults,
    ranges, and allowed choices.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments (none currently used, but kept for
        dispatcher uniformity).
    """
    registry: Dict[str, Any] = get_registry()
    if not registry:
        _print("No effects registered.")
        return

    for name in sorted(registry):
        cls = registry[name]
        # Show affinity as [all] for universal effects, else sorted list
        aff_tag: str = (
            "[all]" if cls.affinity == ALL_DEVICE_TYPES
            else "[" + ", ".join(sorted(cls.affinity)) + "]"
        )
        _print(f"\n  {name} {aff_tag}: {cls.description}")
        params = cls.get_param_defs()
        if params:
            for pname, pdef in sorted(params.items()):
                # Build an optional range/choices suffix for display
                range_str: str = ""
                if pdef.min is not None and pdef.max is not None:
                    range_str = f" [{pdef.min}..{pdef.max}]"
                elif pdef.choices:
                    range_str = f" {pdef.choices}"
                _print(
                    f"    --{pname:16s} {pdef.description} "
                    f"(default: {pdef.default}){range_str}"
                )
    _print()


def cmd_identify(args: argparse.Namespace) -> None:
    """Pulse a device's brightness so the user can locate it physically.

    When the GlowUp server is reachable, the pulse is executed on the
    server (bypassing Deco mesh UDP filtering) for a fixed
    ``--duration`` (default :data:`IDENTIFY_DEFAULT_DURATION` seconds).
    The HTTP response returns immediately and the bulb flashes
    asynchronously on the server side.

    When running locally (``--local`` or server unreachable), the pulse
    runs on the CLI host as an interactive loop until Ctrl+C.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.  Expected attributes: ``ip`` (str),
        ``duration`` (float).
    """
    if not args.ip:
        _print("ERROR: --ip is required for identify command.", file=sys.stderr)
        sys.exit(1)

    if _server_url:
        # --- Server path: execute pulse from Pi, cancel on Ctrl+C -----------
        duration: float = getattr(args, "duration", None) or IDENTIFY_DEFAULT_DURATION
        _print(
            f"Identifying {args.ip} via server "
            f"(pulsing for {duration:.0f}s — Ctrl+C to cancel early)...",
            flush=True,
        )
        resp: dict = _server_post(
            _server_url,
            "/api/command/identify",
            {"ip": args.ip, "duration": duration},
            timeout=SERVER_TIMEOUT_SECONDS,
        )
        dev_info: dict = resp.get("device", {})
        label: str = dev_info.get("label") or "?"
        product: str = dev_info.get("product") or "?"
        zones: Any = dev_info.get("zones")
        mac: str = dev_info.get("mac") or "?"
        _print(f"  {label} — {product}  |  MAC {mac}  |  zones: {zones}")

        # Wait for the pulse to finish; cancel via DELETE on Ctrl+C.
        cancel_event: threading.Event = threading.Event()
        _install_stop_signal(cancel_event)

        cancel_event.wait(timeout=duration)

        if cancel_event.is_set():
            _print("\nCancelling pulse on server...", flush=True)
            _server_delete(_server_url,
                           f"/api/command/identify/{args.ip}",
                           timeout=SERVER_TIMEOUT_SECONDS)

        _print("Done.")
        return

    # --- Direct UDP path (server unreachable or --local) ---------------------
    _print(f"Connecting to {args.ip}...", flush=True)
    try:
        dev: LifxDevice = LifxDevice(args.ip)
    except ValueError as exc:
        _print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    dev.query_all()

    if dev.product is None:
        _print(f"ERROR: No response from {args.ip}.", file=sys.stderr)
        dev.close()
        sys.exit(1)

    _print(f"  {dev.label or '?'} — {dev.product_name or '?'}", flush=True)
    _print(f"\nPulsing brightness on {args.ip}.")
    _print("Press Ctrl+C to stop.\n")

    dev.set_power(on=True, duration_ms=0)

    stop_requested: threading.Event = threading.Event()
    _install_stop_signal(stop_requested)

    start_time: float = time.monotonic()
    while not stop_requested.is_set():
        elapsed: float = time.monotonic() - start_time
        phase: float = (
            math.sin(2.0 * math.pi * elapsed / IDENTIFY_CYCLE_SECONDS) + 1.0
        ) / 2.0
        bri_frac: float = IDENTIFY_MIN_BRI + phase * (1.0 - IDENTIFY_MIN_BRI)
        bri: int = int(bri_frac * HSBK_MAX)

        if dev.is_multizone:
            color = (0, 0, bri, KELVIN_DEFAULT)
            colors = [color] * dev.zone_count
            dev.set_zones(colors, duration_ms=0)
        else:
            dev.set_color(0, 0, bri, KELVIN_DEFAULT, duration_ms=0)

        stop_requested.wait(timeout=IDENTIFY_FRAME_INTERVAL)

    _print("\nStopping...")
    dev.set_power(on=False, duration_ms=DEFAULT_FADE_MS)
    dev.close()
    _print("Done.")


def cmd_power(args: argparse.Namespace) -> None:
    """Turn a device or group on or off via the server.

    Usage::

        glowup power on  --device "group:main_bedroom"
        glowup power off --device "PORCH STRING LIGHTS"
        glowup power on  --device "group:all"

    Args:
        args: Parsed CLI arguments with ``state`` and ``device``.
    """
    device: str = args.device
    on: bool = args.state == "on"

    if not _server_url:
        _print("ERROR: Power command requires a reachable server.",
               file=sys.stderr)
        sys.exit(1)

    encoded: str = quote(device, safe="")
    try:
        _server_post(
            _server_url,
            f"/api/devices/{encoded}/power",
            {"on": on},
        )
        _print(f"{'On' if on else 'Off'}: {device}")
    except SystemExit:
        raise
    except Exception as exc:
        _print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_off(args: argparse.Namespace) -> None:
    """Emergency power-off: all LIFX devices off immediately.

    Safety command that powers off every reachable LIFX device with
    confirmation required.  Runs in parallel:

    1. Direct UDP broadcast GetService + SetPower(False) to all devices
       on the local subnet (independent of server).
    2. Server-side bulk power-off of all configured devices.
    3. Cancellation of any running identify/effect pulses on the server.

    Confirmation required: user must type "off" to execute.  This prevents
    accidental activation.
    """
    _print("\n⚠️  EMERGENCY POWER-OFF ⚠️")
    _print("This will immediately power off ALL LIFX devices on the network.")
    _print("Type 'off' to confirm, or press Ctrl+C to cancel.\n")

    try:
        confirmation: str = input("Confirm: ").strip()
    except (EOFError, KeyboardInterrupt):
        _print("\nCancelled.")
        return

    if confirmation != "off":
        _print("Confirmation mismatch. Cancelled.")
        return

    _print("\nPowering off all devices...\n")

    # --- Broadcast power-off to local subnet (fast, server-independent) -----
    try:
        from transport import broadcast_power_off
        broadcast_power_off()
        _print("✓ Broadcast power-off sent to local subnet")
    except Exception as exc:
        _print(f"⚠️  Broadcast failed: {exc}")

    # --- Server-side power-off (configured devices + cancel identify) -------
    if _server_url:
        try:
            # POST /api/server/power-off-all to turn off all configured devices
            resp: dict = _server_post(
                _server_url,
                "/api/server/power-off-all",
                {},
                timeout=5.0,
            )
            count: int = resp.get("devices_off", 0)
            _print(f"✓ Server powered off {count} configured device(s)")
        except Exception as exc:
            _print(f"⚠️  Server power-off failed: {exc}")

        try:
            # Cancel any running identify pulses (best-effort, fire-and-forget)
            resp: dict = _server_get(
                _server_url,
                "/api/command/identify/cancel-all",
                timeout=2.0,
            )
            cancelled: int = resp.get("cancelled", 0)
            if cancelled > 0:
                _print(f"✓ Cancelled {cancelled} identify pulse(s) on server")
        except Exception as exc:
            _print(f"⚠️  Pulse cancellation failed: {exc}")
    else:
        _print("⚠️  Server unreachable — configured devices may still be on")

    _print("\n✓ Emergency power-off complete")


def cmd_monitor(args: argparse.Namespace) -> None:
    """Monitor a LIFX device in real time by polling its zone colors.

    Connects to a multizone device, repeatedly queries its current
    zone colors, and displays them in a live simulator window.  This
    lets you watch what the lights are actually doing — whether driven
    by the scheduler, a phone app, or anything else on the network.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.  Expected attributes: ``ip`` (str),
        ``hz`` (float), ``zpb`` (int).
    """
    if not args.ip:
        _print("ERROR: --ip is required for monitor command.", file=sys.stderr)
        sys.exit(1)

    # --- Connect to device ---------------------------------------------------
    _print(f"Connecting to {args.ip}...", flush=True)
    try:
        dev: LifxDevice = LifxDevice(args.ip)
    except ValueError as exc:
        _print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    dev.query_all()

    if dev.product is None:
        _print(f"ERROR: No response from {args.ip}.", file=sys.stderr)
        dev.close()
        sys.exit(1)

    _print(f"  {dev.label or '?'} — {dev.product_name or '?'}", flush=True)

    if not dev.is_multizone:
        _print("ERROR: Monitor mode requires a multizone device.", file=sys.stderr)
        dev.close()
        sys.exit(1)

    _print(f"  {dev.zone_count} zones", flush=True)

    # --- Create simulator window ---------------------------------------------
    poly_map: list[bool] = _build_polychrome_map(dev)
    zpb: int = getattr(args, "zpb", 1)
    zoom_val: int = getattr(args, "zoom", 1)
    sim = create_simulator(
        dev.zone_count or 1, f"Monitor: {dev.label or args.ip}",
        polychrome_map=poly_map, zones_per_bulb=zpb,
        zoom=zoom_val,
    )
    if sim is None:
        _print("ERROR: Monitor mode requires tkinter.", file=sys.stderr)
        dev.close()
        sys.exit(1)

    poll_interval: float = 1.0 / args.hz
    _print(f"\nMonitoring at {args.hz:.1f} Hz (every {poll_interval:.2f}s)")
    _print("Press Ctrl+C or close the window to stop.\n")

    # --- Polling thread -------------------------------------------------------
    stop_requested: threading.Event = threading.Event()

    def _poll_loop() -> None:
        """Background thread that queries zone colors and pushes to simulator."""
        while not stop_requested.is_set():
            colors = dev.query_zone_colors()
            if colors is not None:
                sim.update(colors)
            stop_requested.wait(timeout=poll_interval)

    poll_thread: threading.Thread = threading.Thread(
        target=_poll_loop, daemon=True,
    )
    poll_thread.start()

    # --- Signal handling ------------------------------------------------------
    _install_stop_signal(stop_requested)

    # tkinter must run on the main thread (macOS requirement).
    sim._root.protocol("WM_DELETE_WINDOW", lambda: stop_requested.set())

    def _check_stop() -> None:
        """Poll the stop event from the tkinter event loop."""
        if stop_requested.is_set():
            sim.stop()
        else:
            sim._root.after(SIM_STOP_CHECK_MS, _check_stop)

    sim._root.after(SIM_STOP_CHECK_MS, _check_stop)
    sim.run()  # blocks on mainloop (main thread)

    # --- Cleanup --------------------------------------------------------------
    _print("\nStopping...")
    stop_requested.set()
    poll_thread.join(timeout=2.0)
    dev.close()
    _print("Done.")


def _build_polychrome_map(em: Any) -> list[bool]:
    """Build a per-zone list indicating color vs. monochrome capability.

    For a :class:`_NullEmitter` (``--sim-only``), uses the pre-computed
    map that was extracted from the real device before it was closed.

    For a :class:`VirtualMultizoneEmitter`, each zone inherits the
    polychrome status of its underlying physical emitter.  For a
    :class:`LifxEmitter`, all zones share the device's status.

    Args:
        em: An emitter, virtual multizone emitter, or null emitter stub.

    Returns:
        A list of booleans, one per zone.  ``True`` = color,
        ``False`` = monochrome (simulator renders in grayscale).
    """
    # _NullEmitter carries a pre-computed map extracted before real device
    # sockets were closed.
    if hasattr(em, "_pre_poly_map") and em._pre_poly_map is not None:
        return em._pre_poly_map

    # VirtualMultizoneEmitter exposes _zone_map with (emitter, zone_idx) tuples.
    if hasattr(em, "_zone_map"):
        result: list[bool] = []
        for member_em, _ in em._zone_map:
            if isinstance(member_em, LifxEmitter):
                result.append(bool(member_em.transport.is_polychrome))
            else:
                result.append(True)  # Non-LIFX: assume polychrome.
        return result

    # Single LifxEmitter: all zones share the same polychrome status.
    zones: int = em.zone_count if em.zone_count else 1
    if isinstance(em, LifxEmitter):
        poly: bool = bool(em.transport.is_polychrome)
    else:
        poly = True
    return [poly] * zones


def _calibration_request(
    server_url: str, path: str,
    body: Optional[dict] = None,
    timeout: float = 15.0,
) -> Optional[dict]:
    """Non-fatal HTTP request for calibration protocol.

    Unlike :func:`_server_request`, this returns ``None`` on failure
    instead of calling ``sys.exit``.  Used by the calibration system
    where individual request failures are expected and handled.

    Args:
        server_url: Server host:port string.
        path:       URL path.
        body:       Optional JSON body (POST if provided, GET if not).
        timeout:    Request timeout in seconds.

    Returns:
        Parsed JSON response, or ``None`` on any failure.
    """
    token: str = _read_token()
    url: str = f"http://{server_url}{path}"
    headers: Dict[str, str] = {"Authorization": f"Bearer {token}"}
    data: Optional[bytes] = None
    method: str = "GET"
    if body is not None:
        method = "POST"
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _measure_clock_offset(server_url: str, samples: int = 10) -> float:
    """Estimate clock offset between this machine and the server.

    Uses Cristian's algorithm: send rapid time-sync requests, pick
    the sample with the smallest round-trip (least asymmetry), and
    compute the offset as:
        offset = server_time - (t_send + t_recv) / 2

    After applying this offset, a server-side ``time.monotonic()``
    value can be translated to local time:
        t_local = t_server - offset

    Args:
        server_url: Server host:port string.
        samples:    Number of ping samples (default 10).

    Returns:
        Estimated clock offset in seconds (server - local).
    """
    best_rtt: float = float("inf")
    best_offset: float = 0.0

    for _ in range(samples):
        t_send: float = time.monotonic()
        resp: Optional[dict] = _calibration_request(
            server_url, "/api/calibrate/time_sync"
        )
        if resp is None:
            continue
        t_recv: float = time.monotonic()

        rtt: float = t_recv - t_send
        server_time: float = resp.get("server_time", 0.0)

        if rtt < best_rtt:
            best_rtt = rtt
            best_offset = server_time - (t_send + t_recv) / 2.0

    return best_offset


def _run_calibration(
    server_url: str,
    audio_port: int,
    device_label: str,
    encoded_device: str,
    server_host: str,
) -> Optional[float]:
    """Run standalone sonar calibration before music starts.

    Nothing else is running — no music, no effects.  The server opens
    a dedicated TCP socket, sends silence + calibration pulses, and
    appends emission timestamps at the end of the stream.  The CLI
    detects the pulses, reads the timestamps, and computes the
    one-way audio latency using Cristian's algorithm for clock sync.

    Args:
        server_url:     Server host:port string.
        audio_port:     Not used (server picks its own port).
        device_label:   Human-readable device name for messages.
        encoded_device: URL-encoded device identifier.
        server_host:    Server hostname/IP.

    Returns:
        Measured audio delay in seconds, or ``None`` on failure.
    """
    import socket as _socket

    _print("  Calibrating audio sync...", flush=True)

    # Step 1: Clock offset estimation (Cristian's algorithm).
    clock_offset: float = _measure_clock_offset(server_url)

    # Step 2: Ask the server to start calibration.  It opens a TCP
    # socket and returns the port.  Then it waits for us to connect.
    cal_resp: Optional[dict] = _calibration_request(
        server_url,
        f"/api/calibrate/start/{encoded_device}",
        body={},
        timeout=5,
    )
    if cal_resp is None:
        _print("  Calibration: server did not respond",
               file=sys.stderr)
        return None

    cal_port: int = cal_resp.get("port", 8421)

    # Step 3: Connect to the calibration TCP socket.
    try:
        sock: _socket.socket = _socket.socket(
            _socket.AF_INET, _socket.SOCK_STREAM
        )
        sock.settimeout(10.0)
        sock.connect((server_host, cal_port))
    except Exception as exc:
        _print(f"  Calibration: cannot connect to tcp port {cal_port}: {exc}",
               file=sys.stderr)
        return None

    # Step 4: Read the entire calibration stream — silence, pulses,
    # then a JSON line with emission timestamps at the end.
    from media.calibration import PulseDetector
    detector: PulseDetector = PulseDetector(sample_rate=44100)

    all_data: bytearray = bytearray()
    deadline: float = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            chunk: bytes = sock.recv(8820)
        except _socket.timeout:
            break
        if not chunk:
            break  # Server closed connection — stream complete.
        all_data.extend(chunk)
        detector.feed(chunk)

    sock.close()

    # Step 5: Extract emit timestamps from the end of the stream.
    MARKER: bytes = b"\n__EMIT_TIMES__:"
    emit_times: List[float] = []
    marker_pos: int = all_data.find(MARKER)
    if marker_pos >= 0:
        json_start: int = marker_pos + len(MARKER)
        json_end: int = all_data.find(b"\n", json_start)
        if json_end < 0:
            json_end = len(all_data)
        try:
            emit_times = json.loads(
                all_data[json_start:json_end].decode()
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    detect_times: List[float] = detector.detections

    if not detect_times or not emit_times:
        _print(f"  Calibration: detected {len(detect_times)} pulses, "
               f"server emitted {len(emit_times)}",
               file=sys.stderr)
        return None

    # Step 6: Compute one-way latency for each matched pulse.
    latencies: List[float] = []
    for i in range(min(len(emit_times), len(detect_times))):
        emit_local: float = emit_times[i] - clock_offset
        latency: float = detect_times[i] - emit_local
        if latency > 0:
            latencies.append(latency)

    if not latencies:
        _print("  Calibration: all latency measurements invalid",
               file=sys.stderr)
        return None

    # Step 7: Median + ffplay pipeline estimate.
    latencies.sort()
    median_latency: float = latencies[len(latencies) // 2]

    # ffplay's internal pipeline with low-latency flags is ~150ms.
    FFPLAY_PIPELINE_ESTIMATE: float = 0.15
    total: float = median_latency + FFPLAY_PIPELINE_ESTIMATE

    _print(f"  Calibration: TCP={median_latency*1000:.0f}ms "
           f"+ ffplay~{FFPLAY_PIPELINE_ESTIMATE*1000:.0f}ms "
           f"= {total*1000:.0f}ms ({len(latencies)} pulses)")

    return total


def _play_screen_reactive(args: argparse.Namespace) -> None:
    """Run screen-reactive lighting locally.

    Captures the local screen, runs VisionExtractor, and drives the
    screen_light effect on a device (via --ip or --device) or the
    simulator (via --sim-only or --zones).

    Args:
        args: Parsed CLI arguments.
    """
    print("Screen-reactive mode starting...", flush=True)

    # Use the same proven code path as the test harness: MovieDecoder
    # pointed at avfoundation for screen capture, direct pyramid +
    # extraction on the main thread, render to pygame or LIFX emitter.
    import signal
    import numpy as np
    from media.screen_source import build_pyramid
    from media.vision import VisionExtractor
    from media import SignalBus

    has_ip: bool = bool(getattr(args, "ip", None))
    has_device: bool = bool(getattr(args, "device", None))
    sim_only: bool = bool(getattr(args, "sim_only", False))
    virtual_zones: int = getattr(args, "zones", None) or 0
    use_sim: bool = sim_only or virtual_zones > 0

    # Determine zone count and emitter target.
    zone_count: int = 60
    emitter: Any = None

    if use_sim:
        zone_count = virtual_zones if virtual_zones > 0 else 60
    elif has_ip:
        ip: str = args.ip
        try:
            dev: LifxDevice = LifxDevice(ip)
            dev.query_all()
            zone_count = dev.zone_count or 1
            emitter = LifxEmitter.from_device(dev)
            _print(f"  Connected: {dev.label or ip} — {zone_count} zones")
        except Exception as exc:
            _print(f"ERROR: Cannot connect to {ip}: {exc}",
                   file=sys.stderr)
            sys.exit(1)
    elif has_device and _server_url:
        device: str = args.device
        encoded: str = quote(device, safe="")
        resp: dict = _server_get(
            _server_url, f"/api/devices/{encoded}/status"
        )
        dev_list: list = resp.get("devices", [])
        if not dev_list:
            _print("ERROR: Device not found on server.", file=sys.stderr)
            sys.exit(1)
        dev_info: dict = dev_list[0]
        zone_count = dev_info.get("zones", 1)
        dev_ip: str = dev_info.get("ip", "")
        _print(f"  {dev_info.get('label', device)} — {zone_count} zones")
        try:
            dev_obj: LifxDevice = LifxDevice(dev_ip)
            dev_obj.query_all()
            emitter = LifxEmitter.from_device(dev_obj)
        except Exception as exc:
            _print(f"ERROR: Cannot connect to {dev_ip}: {exc}",
                   file=sys.stderr)
            sys.exit(1)
    else:
        _print("ERROR: --screen requires --ip, --device, --sim-only, "
               "or --zones", file=sys.stderr)
        sys.exit(1)

    # Capture dimensions (what we decode frames at).
    cap_w: int = 640
    cap_h: int = 360

    # Sensitivity, contrast, blur, and fps params.
    sensitivity: float = getattr(args, "sensitivity", None) or 1.5
    contrast: float = getattr(args, "contrast", None) or 1.5
    no_blur: bool = bool(getattr(args, "no_blur", False))
    cap_fps: int = getattr(args, "fps", None) or 30

    # Vision pipeline (same as test harness).
    bus: SignalBus = SignalBus()
    extractor: VisionExtractor = VisionExtractor(
        source_name="screen", bus=bus,
        edge_regions=zone_count,
    )

    # Video input via ffmpeg — screen capture or URL (HDHomeRun, RTSP, etc.).
    import subprocess as _sp
    video_url: Optional[str] = getattr(args, "video_url", None)
    if video_url:
        # External video source (e.g. HDHomeRun).
        cap_cmd: list[str] = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", video_url,
            "-vf", f"scale={cap_w}:{cap_h}",
            "-r", str(cap_fps),
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "pipe:1",
        ]
        print(f"  Starting video capture from URL: {video_url}",
              flush=True)
        print(f"  Decoding at {cap_w}x{cap_h} @ {cap_fps} fps", flush=True)
    else:
        # Local screen capture via AVFoundation.
        cap_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation", "-framerate", str(cap_fps),
            "-capture_cursor", "0",
            "-i", "3:",
            "-vf", f"scale={cap_w}:{cap_h}",
            "-r", str(cap_fps),
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "pipe:1",
        ]
        print(f"  Starting screen capture: {cap_w}x{cap_h} @ {cap_fps} fps",
              flush=True)
    cap_proc: _sp.Popen = _sp.Popen(
        cap_cmd, stdout=_sp.PIPE, stderr=_sp.DEVNULL,
    )
    frame_size: int = cap_w * cap_h * 3

    # Set up pygame for simulator mode.
    BORDER_PX: int = 40
    if use_sim:
        try:
            import pygame
            from tools import screen_test_harness
            from tools.screen_test_harness import hsb_to_rgb, render_glow_border
            from colorspace import srgb_to_oklab, oklab_to_srgb
            pygame.init()
            # Sync harness globals with our layout dimensions.
            screen_test_harness.BORDER_PX = BORDER_PX
            screen_test_harness.TV_WIDTH = cap_w
            screen_test_harness.TV_HEIGHT = cap_h
            room_w: int = cap_w + BORDER_PX * 2
            room_h: int = cap_h + BORDER_PX * 2
            pg_screen: pygame.Surface = pygame.display.set_mode(
                (room_w, room_h),
            )
            pygame.display.set_caption("GlowUp Screen Reactive")
            clock: pygame.time.Clock = pygame.time.Clock()
        except ImportError as exc:
            _print(f"ERROR: Missing dependency for sim mode: {exc}",
                   file=sys.stderr)
            cap_proc.kill()
            sys.exit(1)

    # Signal handler for clean exit.
    running: bool = True

    def _sig_handler(signum: int, frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    print("Screen-reactive mode active. Press Ctrl+C to stop.\n",
          flush=True)

    # Main loop — same structure as the test harness.
    room_bg: tuple[int, int, int] = (15, 15, 20)
    src: str = "screen"

    # FPS measurement state.
    _fps_frame_count: int = 0
    _fps_last_time: float = time.time()
    _fps_display: float = 0.0

    while running:
        # Read one complete frame, accumulating partial reads.
        buf: bytearray = bytearray()
        while len(buf) < frame_size:
            chunk: bytes = cap_proc.stdout.read(frame_size - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        frame_bytes: bytes = bytes(buf)
        if len(frame_bytes) < frame_size:
            print("  Screen capture EOF.", flush=True)
            break

        # Build pyramid and extract vision signals.
        pyramid: list[Any] = build_pyramid(frame_bytes, cap_w, cap_h)
        extractor.process_pyramid(pyramid, cap_w, cap_h)

        # Read signals from the bus.
        brightness: float = float(bus.read(f"{src}:vision:brightness", 0.0))
        flash: float = float(bus.read(f"{src}:vision:flash", 0.0))
        dominant_hue: float = float(
            bus.read(f"{src}:vision:dominant_hue", 0.0)
        )
        dominant_sat: float = float(
            bus.read(f"{src}:vision:dominant_sat", 0.5)
        )
        edge_colors: Any = bus.read(
            f"{src}:vision:edge_colors", [0.0] * zone_count,
        )
        edge_brightness: Any = bus.read(
            f"{src}:vision:edge_brightness", [0.0] * zone_count,
        )

        # Apply sensitivity and contrast.
        if isinstance(edge_brightness, list):
            processed_bri: list[float] = []
            for b_val in edge_brightness:
                b_val = min(1.0, b_val * sensitivity)
                if contrast != 1.0 and b_val > 0.0:
                    b_val = b_val ** contrast
                b_val = min(1.0, b_val + flash * 0.4)
                processed_bri.append(b_val)
        else:
            processed_bri = [0.5] * zone_count

        if use_sim:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False

            pg_screen.fill(room_bg)

            if isinstance(edge_colors, list):
                if no_blur:
                    # Flat color rects — no scipy, fast baseline.
                    n_z: int = len(edge_colors)
                    tv_x: int = BORDER_PX
                    tv_y: int = BORDER_PX
                    peri_total: int = 2 * (cap_w + cap_h)
                    d_sat: float = min(1.0, dominant_sat * 0.7)
                    for iz in range(n_z):
                        h_z: float = edge_colors[iz]
                        b_z: float = processed_bri[iz] if iz < len(processed_bri) else 0.0
                        color: tuple[int, int, int] = hsb_to_rgb(h_z, d_sat, b_z)
                        frac_lo: float = iz / n_z
                        frac_hi: float = (iz + 1) / n_z
                        pos_lo: float = frac_lo * peri_total
                        pos_mid: float = (pos_lo + frac_hi * peri_total) / 2.0
                        seg_len: float = (frac_hi - frac_lo) * peri_total
                        if pos_mid < cap_w:
                            rect = pygame.Rect(
                                int(tv_x + pos_lo), 0,
                                max(1, int(seg_len)), BORDER_PX,
                            )
                        elif pos_mid < cap_w + cap_h:
                            local: float = pos_lo - cap_w
                            rect = pygame.Rect(
                                tv_x + cap_w, int(tv_y + local),
                                BORDER_PX, max(1, int(seg_len)),
                            )
                        elif pos_mid < 2 * cap_w + cap_h:
                            local = pos_lo - cap_w - cap_h
                            rect = pygame.Rect(
                                int(tv_x + cap_w - local - seg_len), tv_y + cap_h,
                                max(1, int(seg_len)), BORDER_PX,
                            )
                        else:
                            local = pos_lo - 2 * cap_w - cap_h
                            rect = pygame.Rect(
                                0, int(tv_y + cap_h - local - seg_len),
                                BORDER_PX, max(1, int(seg_len)),
                            )
                        rect = rect.clip(pg_screen.get_rect())
                        if rect.width > 0 and rect.height > 0:
                            pygame.draw.rect(pg_screen, color, rect)

                    # Fill corners with oklab midpoint of adjacent zones.
                    # Corners sit at perimeter positions where one edge
                    # ends and the next begins.  Each corner's two
                    # adjacent zone indices come from the perimeter
                    # fraction at that corner.
                    corner_positions: list[float] = [
                        0.0,                              # top-left
                        float(cap_w),                     # top-right
                        float(cap_w + cap_h),             # bottom-right
                        float(2 * cap_w + cap_h),         # bottom-left
                    ]
                    corner_rects: list[tuple[int, int]] = [
                        (0, 0),                           # top-left
                        (tv_x + cap_w, 0),                # top-right
                        (tv_x + cap_w, tv_y + cap_h),     # bottom-right
                        (0, tv_y + cap_h),                 # bottom-left
                    ]
                    for ci in range(4):
                        # Zone index just after this corner.
                        frac_c: float = corner_positions[ci] / peri_total
                        iz_after: int = int(frac_c * n_z) % n_z
                        iz_before: int = (iz_after - 1) % n_z
                        # Get RGB of both adjacent zones.
                        rgb_a: tuple[int, int, int] = hsb_to_rgb(
                            edge_colors[iz_before], d_sat,
                            processed_bri[iz_before] if iz_before < len(processed_bri) else 0.0,
                        )
                        rgb_b: tuple[int, int, int] = hsb_to_rgb(
                            edge_colors[iz_after], d_sat,
                            processed_bri[iz_after] if iz_after < len(processed_bri) else 0.0,
                        )
                        # Oklab midpoint.
                        L1, a1, b1 = srgb_to_oklab(rgb_a[0] / 255.0, rgb_a[1] / 255.0, rgb_a[2] / 255.0)
                        L2, a2, b2 = srgb_to_oklab(rgb_b[0] / 255.0, rgb_b[1] / 255.0, rgb_b[2] / 255.0)
                        rm, gm, bm = oklab_to_srgb(
                            (L1 + L2) * 0.5,
                            (a1 + a2) * 0.5,
                            (b1 + b2) * 0.5,
                        )
                        corner_color: tuple[int, int, int] = (
                            max(0, min(255, int(rm * 255))),
                            max(0, min(255, int(gm * 255))),
                            max(0, min(255, int(bm * 255))),
                        )
                        cx, cy = corner_rects[ci]
                        pygame.draw.rect(
                            pg_screen, corner_color,
                            pygame.Rect(cx, cy, BORDER_PX, BORDER_PX),
                        )
                else:
                    # 1D blur: paint zone color rects onto border strips,
                    # then blur each strip along the perpendicular axis
                    # (vertical for top/bottom, horizontal for left/right).
                    # Additive blit onto room background so glow fades.
                    from scipy.ndimage import gaussian_filter1d
                    _blur_sigma: float = BORDER_PX / 3.0
                    n_z: int = len(edge_colors)
                    peri_total: int = 2 * (cap_w + cap_h)
                    d_sat: float = min(1.0, dominant_sat * 0.7)

                    # Build 1-pixel-wide color lines for top/bottom
                    # (horizontal, cap_w pixels) and left/right (vertical,
                    # cap_h pixels).  Each zone paints its span into the
                    # line.  Then replicate to _inner_depth and embed at
                    # the inner edge of the border strip before blurring.
                    _inner_depth: int = max(4, BORDER_PX // 3)
                    top_line: np.ndarray = np.zeros((cap_w, 3), dtype=np.float32)
                    bot_line: np.ndarray = np.zeros((cap_w, 3), dtype=np.float32)
                    left_line: np.ndarray = np.zeros((cap_h, 3), dtype=np.float32)
                    right_line: np.ndarray = np.zeros((cap_h, 3), dtype=np.float32)

                    for iz in range(n_z):
                        h_z: float = edge_colors[iz]
                        b_z: float = processed_bri[iz] if iz < len(processed_bri) else 0.0
                        rgb: tuple[int, int, int] = hsb_to_rgb(h_z, d_sat, b_z)
                        frgb: np.ndarray = np.array([rgb[0], rgb[1], rgb[2]], dtype=np.float32)

                        frac_lo: float = iz / n_z
                        frac_hi: float = (iz + 1) / n_z
                        pos_lo: float = frac_lo * peri_total
                        pos_mid: float = (pos_lo + frac_hi * peri_total) / 2.0

                        if pos_mid < cap_w:
                            x0: int = max(0, int(pos_lo))
                            x1: int = min(cap_w, int(frac_hi * peri_total))
                            top_line[x0:x1, :] = frgb
                        elif pos_mid < cap_w + cap_h:
                            local: float = pos_lo - cap_w
                            y0: int = max(0, int(local))
                            y1: int = min(cap_h, int(frac_hi * peri_total - cap_w))
                            right_line[y0:y1, :] = frgb
                        elif pos_mid < 2 * cap_w + cap_h:
                            local = pos_lo - cap_w - cap_h
                            x1_b: int = min(cap_w, int(cap_w - local))
                            x0_b: int = max(0, int(cap_w - (frac_hi * peri_total - cap_w - cap_h)))
                            bot_line[x0_b:x1_b, :] = frgb
                        else:
                            local = pos_lo - 2 * cap_w - cap_h
                            y1_l: int = min(cap_h, int(cap_h - local))
                            y0_l: int = max(0, int(cap_h - (frac_hi * peri_total - 2 * cap_w - cap_h)))
                            left_line[y0_l:y1_l, :] = frgb

                    # Replicate each 1-pixel line to _inner_depth rows/cols
                    # and embed at the inner edge of a black border strip.
                    top_strip: np.ndarray = np.zeros((BORDER_PX, cap_w, 3), dtype=np.float32)
                    top_strip[BORDER_PX - _inner_depth:, :, :] = top_line[np.newaxis, :, :]

                    bot_strip: np.ndarray = np.zeros((BORDER_PX, cap_w, 3), dtype=np.float32)
                    bot_strip[:_inner_depth, :, :] = bot_line[np.newaxis, :, :]

                    right_strip: np.ndarray = np.zeros((cap_h, BORDER_PX, 3), dtype=np.float32)
                    right_strip[:, :_inner_depth, :] = right_line[:, np.newaxis, :]

                    left_strip: np.ndarray = np.zeros((cap_h, BORDER_PX, 3), dtype=np.float32)
                    left_strip[:, BORDER_PX - _inner_depth:, :] = left_line[:, np.newaxis, :]

                    # Pass 1 — outward blur: spread color from inner
                    # edge toward the wall (perpendicular to TV edge).
                    top_strip = gaussian_filter1d(top_strip, sigma=_blur_sigma, axis=0)
                    bot_strip = gaussian_filter1d(bot_strip, sigma=_blur_sigma, axis=0)
                    left_strip = gaussian_filter1d(left_strip, sigma=_blur_sigma, axis=1)
                    right_strip = gaussian_filter1d(right_strip, sigma=_blur_sigma, axis=1)

                    # Pass 2 — cross blur: soften zone seams along the
                    # TV edge (parallel to it).  Top/bottom get horizontal
                    # blur, left/right get vertical blur.
                    top_strip = gaussian_filter1d(top_strip, sigma=_blur_sigma, axis=1)
                    bot_strip = gaussian_filter1d(bot_strip, sigma=_blur_sigma, axis=1)
                    left_strip = gaussian_filter1d(left_strip, sigma=_blur_sigma, axis=0)
                    right_strip = gaussian_filter1d(right_strip, sigma=_blur_sigma, axis=0)

                    # Blit with additive blending so glow fades into wall.
                    def _blit_add(arr: np.ndarray, x: int, y: int) -> None:
                        s: pygame.Surface = pygame.surfarray.make_surface(
                            arr.clip(0, 255).astype(np.uint8).swapaxes(0, 1),
                        )
                        pg_screen.blit(s, (x, y), special_flags=pygame.BLEND_ADD)

                    _blit_add(top_strip, BORDER_PX, 0)
                    _blit_add(bot_strip, BORDER_PX, BORDER_PX + cap_h)
                    _blit_add(left_strip, 0, BORDER_PX)
                    _blit_add(right_strip, BORDER_PX + cap_w, BORDER_PX)

                    # Corners: oklab midpoint of two adjacent edge zones,
                    # filled into a BORDER_PX square and 2D blurred.
                    from scipy.ndimage import gaussian_filter
                    corner_peri: list[float] = [
                        0.0,                              # top-left
                        float(cap_w),                     # top-right
                        float(cap_w + cap_h),             # bottom-right
                        float(2 * cap_w + cap_h),         # bottom-left
                    ]
                    corner_xy: list[tuple[int, int]] = [
                        (0, 0),
                        (BORDER_PX + cap_w, 0),
                        (BORDER_PX + cap_w, BORDER_PX + cap_h),
                        (0, BORDER_PX + cap_h),
                    ]
                    for ci in range(4):
                        frac_c: float = corner_peri[ci] / peri_total
                        iz_after: int = int(frac_c * n_z) % n_z
                        iz_before: int = (iz_after - 1) % n_z
                        rgb_a: tuple[int, int, int] = hsb_to_rgb(
                            edge_colors[iz_before], d_sat,
                            processed_bri[iz_before] if iz_before < len(processed_bri) else 0.0,
                        )
                        rgb_b: tuple[int, int, int] = hsb_to_rgb(
                            edge_colors[iz_after], d_sat,
                            processed_bri[iz_after] if iz_after < len(processed_bri) else 0.0,
                        )
                        L1, a1, b1 = srgb_to_oklab(rgb_a[0] / 255.0, rgb_a[1] / 255.0, rgb_a[2] / 255.0)
                        L2, a2, b2 = srgb_to_oklab(rgb_b[0] / 255.0, rgb_b[1] / 255.0, rgb_b[2] / 255.0)
                        rm, gm, bm = oklab_to_srgb(
                            (L1 + L2) * 0.5, (a1 + a2) * 0.5, (b1 + b2) * 0.5,
                        )
                        # Paint the inner quadrant of the corner square,
                        # leave the outer portion black, then 2D blur so
                        # the color bleeds outward like the edge strips.
                        csq: np.ndarray = np.zeros(
                            (BORDER_PX, BORDER_PX, 3), dtype=np.float32,
                        )
                        c_rgb: list[float] = [
                            max(0.0, min(255.0, rm * 255.0)),
                            max(0.0, min(255.0, gm * 255.0)),
                            max(0.0, min(255.0, bm * 255.0)),
                        ]
                        # Inner quadrant is the quarter closest to the TV.
                        half: int = BORDER_PX // 2
                        # ci: 0=TL(inner=bottom-right), 1=TR(inner=bottom-left),
                        #     2=BR(inner=top-left), 3=BL(inner=top-right)
                        if ci == 0:
                            csq[half:, half:, :] = c_rgb
                        elif ci == 1:
                            csq[half:, :half, :] = c_rgb
                        elif ci == 2:
                            csq[:half, :half, :] = c_rgb
                        else:
                            csq[:half, half:, :] = c_rgb
                        csq = gaussian_filter(csq, sigma=(_blur_sigma, _blur_sigma, 0))
                        _blit_add(csq, corner_xy[ci][0], corner_xy[ci][1])

            # Composite the live video frame as the "TV".
            frame_arr: np.ndarray = np.frombuffer(
                frame_bytes, dtype=np.uint8,
            ).reshape(cap_h, cap_w, 3)
            tv_surf: pygame.Surface = pygame.surfarray.make_surface(
                frame_arr.swapaxes(0, 1),
            )
            pg_screen.blit(tv_surf, (BORDER_PX, BORDER_PX))

            # FPS overlay.
            _fps_frame_count += 1
            _fps_now: float = time.time()
            _fps_elapsed: float = _fps_now - _fps_last_time
            if _fps_elapsed >= 1.0:
                _fps_display = _fps_frame_count / _fps_elapsed
                _fps_frame_count = 0
                _fps_last_time = _fps_now
            fps_text: str = f"{_fps_display:.1f} fps"
            if no_blur:
                fps_text += "  [no blur]"
            pygame.display.set_caption(f"GlowUp Screen Reactive — {fps_text}")

            pygame.display.flip()
            clock.tick(cap_fps)
        elif emitter is not None:
            # Send to real LIFX device.
            if isinstance(edge_colors, list) and len(edge_colors) >= zone_count:
                colors: list[tuple[int, int, int, int]] = []
                for i in range(zone_count):
                    h_val: float = edge_colors[i] if i < len(edge_colors) else 0.0
                    b_val2: float = processed_bri[i] if i < len(processed_bri) else 0.0
                    s_val: float = min(1.0, dominant_sat * 0.7)
                    colors.append((
                        int(h_val * 65535) & 0xFFFF,
                        int(s_val * 65535) & 0xFFFF,
                        int(b_val2 * 65535) & 0xFFFF,
                        3500,
                    ))
                emitter.send_zones(colors)

    # Cleanup.
    _print("\nStopping...")
    cap_proc.kill()
    cap_proc.wait(timeout=3)
    if use_sim:
        pygame.quit()
    if emitter is not None:
        # Turn off the lights.
        off_colors: list[tuple[int, int, int, int]] = [
            (0, 0, 0, 3500)
        ] * zone_count
        emitter.send_zones(off_colors)


def _play_via_server(args: argparse.Namespace) -> None:
    """Run an effect on a device identified by label or MAC.

    Two modes:

    - **Normal** — the server resolves the identifier, runs the effect,
      and sends packets.  The CLI blocks until Ctrl+C, then tells the
      server to stop.
    - **Sim / sim-only** — the server is queried for the device's zone
      count, then the effect runs locally in the simulator.  No packets
      are sent to the device.

    Args:
        args: Parsed CLI arguments with ``device``, ``effect``, and
              any effect-specific parameters.
    """
    device: str = args.device
    effect_name: str = args.effect
    sim_mode: bool = getattr(args, "sim", False)
    sim_only: bool = getattr(args, "sim_only", False)

    # Validate effect name locally (fast feedback, no server round-trip).
    registry: Dict[str, Any] = get_registry()
    if effect_name not in registry:
        _print(
            f"ERROR: Unknown effect '{effect_name}'. "
            f"Available: {', '.join(get_effect_names())}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Collect effect params from CLI arguments.
    effect_cls = registry[effect_name]
    param_defs = effect_cls.get_param_defs()
    params: Dict[str, Any] = {}
    for pname in param_defs:
        val: Any = getattr(args, pname, None)
        if val is not None:
            params[pname] = val
    zpb_val: int = getattr(args, "zpb", 1)
    if "zones_per_bulb" in param_defs:
        params["zones_per_bulb"] = zpb_val

    # URL-encode the device identifier (labels may contain spaces).
    encoded_device: str = quote(device, safe="")

    # -- Sim mode: fetch geometry from server, run effect locally ----------
    if sim_mode or sim_only:
        status_path: str = f"/api/devices/{encoded_device}/status"
        _print(f"Fetching geometry for '{device}' from server...",
               flush=True)
        resp: dict = _server_get(_server_url, status_path)
        # Extract zone count from device info.
        dev_list: list = resp.get("devices", [])
        if not dev_list:
            _print("ERROR: Device not found on server.", file=sys.stderr)
            sys.exit(1)
        dev_info: dict = dev_list[0]
        zone_count: int = dev_info.get("zones", 1)
        dev_label: str = dev_info.get("label", device)
        dev_product: str = dev_info.get("product", "?")

        _print(f"  {dev_label} — {dev_product}, {zone_count} zones",
               flush=True)

        # Build a null emitter with the device's geometry.
        poly_map: list[bool] = [True] * zone_count
        em: Any = _NullEmitter(
            zone_count=zone_count,
            label=dev_label,
            product_name=dev_product,
            ip="sim-only",
            pre_poly_map=poly_map,
        )

        # Set color interpolation.
        from colorspace import set_lerp_method
        lerp_method: str = getattr(args, "lerp", "lab")
        set_lerp_method(lerp_method)

        # Create and start the simulator.
        zoom_val: int = getattr(args, "zoom", 1)
        sim = create_simulator(
            zone_count, effect_name,
            polychrome_map=poly_map,
            zones_per_bulb=zpb_val,
            zoom=zoom_val,
        )
        if sim is None:
            _print("ERROR: Simulator unavailable (pygame not installed?).",
                   file=sys.stderr)
            sys.exit(1)

        # Create engine controller and run.
        from engine import Controller, create_effect
        fps: int = getattr(args, "fps", None) or DEFAULT_FPS
        ctrl: Controller = Controller(
            [em], fps=fps,
            frame_callback=sim.update,
            zones_per_bulb=zpb_val,
        )
        ctrl.play(effect_name, **params)

        _print(f"\nSimulating '{effect_name}' — {zone_count} zones "
               f"(from '{dev_label}')")
        _print("Press Ctrl+C to stop.\n")

        stop_event: threading.Event = threading.Event()
        _install_stop_signal(stop_event)
        stop_event.wait()
        _print("\nStopping...")
        ctrl.stop(fade_ms=0)
        sim.stop()
        return

    # -- Normal mode: server runs everything --------------------------------
    play_path: str = f"/api/devices/{encoded_device}/play"

    music_dir: Optional[str] = getattr(args, "music_dir", None)
    if music_dir:
        _print(f"Playing '{effect_name}' on '{device}' with music "
               f"from '{music_dir}'...", flush=True)
    else:
        _print(f"Playing '{effect_name}' on '{device}' via server...",
               flush=True)

    play_body: Dict[str, Any] = {
        "effect": effect_name,
        "params": params,
        "source": platform.node().removesuffix(".local"),
    }
    if music_dir:
        play_body["music_dir"] = music_dir
        play_body["bands"] = getattr(args, "bands", 32)

    try:
        resp = _server_post(
            _server_url, play_path,
            play_body,
            timeout=SERVER_TIMEOUT_SECONDS,
        )
    except SystemExit:
        raise
    except Exception as exc:
        _print(f"ERROR: Server play failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Show what the server reported.
    if "effect" in resp:
        _print(f"  Effect: {resp['effect']}")
    if "params" in resp:
        _print(f"  Params: {json.dumps(resp['params'], indent=2)}")

    # If music_dir is active, calibrate audio sync and start streaming.
    ffplay_proc: Optional[subprocess.Popen] = None
    if music_dir:
        audio_port: Optional[int] = resp.get("audio_stream_port")
        if audio_port:
            server_host: str = _server_url.rsplit(":", 1)[0]
            stream_url: str = f"tcp://{server_host}:{audio_port}"

            # --- Automatic audio-light sync calibration ---
            audio_offset_ms: int = getattr(args, "audio_offset_ms", 0)
            skip_cal: bool = getattr(args, "skip_calibration", False)

            if not skip_cal:
                cal_delay: Optional[float] = _run_calibration(
                    _server_url, audio_port, device,
                    encoded_device, server_host,
                )
                if cal_delay is not None:
                    # Apply the measured delay plus any manual offset.
                    total_delay: float = cal_delay + (audio_offset_ms / 1000.0)
                    _print(f"  Sync: {cal_delay*1000:.0f}ms measured"
                           f"{f' + {audio_offset_ms}ms offset' if audio_offset_ms else ''}"
                           f" = {total_delay*1000:.0f}ms light delay")
                    result: Optional[dict] = _calibration_request(
                        _server_url,
                        f"/api/calibrate/result/{encoded_device}",
                        body={"delay_seconds": max(0.0, total_delay)},
                    )
                    if result is None:
                        _print("  WARNING: Could not apply sync delay",
                               file=sys.stderr)
                else:
                    _print("  Sync: calibration failed, playing without delay")

            # Start ffplay for audio output.
            try:
                ffplay_proc = subprocess.Popen(
                    [
                        "ffplay",
                        "-fflags", "nobuffer",
                        "-flags", "low_delay",
                        "-probesize", "32",
                        "-analyzeduration", "0",
                        "-f", "s16le",
                        "-ar", "44100",
                        "-ch_layout", "mono",
                        "-nodisp",
                        "-loglevel", "error",
                        stream_url,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                _print(f"  Audio: {stream_url}")
            except FileNotFoundError:
                _print("  WARNING: ffplay not found — no local audio playback",
                       file=sys.stderr)
            except Exception as exc:
                _print(f"  WARNING: Could not start audio: {exc}",
                       file=sys.stderr)

    _print("Press Ctrl+C to stop.\n")

    # Block until Ctrl+C, then tell the server to stop the effect.
    stop_event: threading.Event = threading.Event()
    _install_stop_signal(stop_event)
    stop_event.wait()
    _print("\nStopping...")

    # Kill local audio playback.
    if ffplay_proc is not None:
        try:
            ffplay_proc.kill()
            ffplay_proc.wait(timeout=3.0)
        except Exception:
            pass

    stop_path: str = f"/api/devices/{encoded_device}/stop"
    try:
        _server_post(
            _server_url, stop_path, {},
            timeout=SERVER_TIMEOUT_SECONDS,
        )
        _print("Stopped.")
    except Exception as exc:
        _print(f"WARNING: Stop request failed: {exc}", file=sys.stderr)


def cmd_play(args: argparse.Namespace) -> None:
    """Connect to a LIFX device (or group) and run the named effect.

    Supports three modes:

    * **Single device** — ``--ip <address>`` targets one device.
    * **Virtual multizone (server)** — ``--group <name>`` fetches the
      group's device list from the GlowUp server and treats every
      device in the group as one zone in a virtual multizone strip.
    * **Virtual multizone (local)** — ``--group <name> --config <file>``
      loads the group from a local JSON config file instead of the
      server.

    This function blocks until SIGINT or SIGTERM is received, then
    gracefully fades the device(s) to black and disconnects.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.  Expected attributes: ``ip`` (str or None),
        ``config`` (str or None), ``group`` (str or None),
        ``server`` (str or None), ``effect`` (str), ``fps`` (int),
        plus any effect-specific parameters.
    """
    has_ip: bool = bool(getattr(args, "ip", None))
    has_device: bool = bool(getattr(args, "device", None))
    has_group: bool = bool(getattr(args, "group", None))
    has_config: bool = bool(getattr(args, "config", None))
    sim_only: bool = bool(getattr(args, "sim_only", False))
    virtual_zones: int = getattr(args, "zones", None) or 0

    # -- Server-side play via --device ----------------------------------------
    # When --device is given, the server does all the work: resolve the
    # label/MAC to an IP, run the effect, send packets.  The CLI just
    # blocks until Ctrl+C, then tells the server to stop.
    if has_device:
        if not _server_url:
            _print(
                "ERROR: --device requires a reachable GlowUp server "
                "(the server resolves labels and runs effects).",
                file=sys.stderr,
            )
            sys.exit(1)
        if has_ip or has_group:
            _print(
                "ERROR: --device is mutually exclusive with "
                "--ip and --group.",
                file=sys.stderr,
            )
            sys.exit(1)
        _play_via_server(args)
        return

    # -- Screen-reactive mode --------------------------------------------------
    # --video-url implies --screen.
    has_video_url: bool = bool(getattr(args, "video_url", None))
    has_screen: bool = bool(getattr(args, "screen", False)) or has_video_url
    if has_screen:
        _play_screen_reactive(args)
        return

    # --config without --group is meaningless.
    if has_config and not has_group:
        _print(
            "ERROR: --config requires --group.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --zones implies --sim-only (no device needed).
    if virtual_zones > 0:
        sim_only = True
        args.sim_only = True

    if not has_ip and not has_group and virtual_zones <= 0:
        _print(
            "ERROR: Specify --ip, --device, --group, or --zones.\n"
            "       --device routes through the server by label/MAC\n"
            "       --group fetches device IPs from the server\n"
            "       (or from a local file with --config).",
            file=sys.stderr,
        )
        sys.exit(1)

    if has_ip and has_group:
        _print(
            "ERROR: --ip and --group are mutually exclusive.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Device-free simulator mode ------------------------------------------
    # When --zones is specified, skip all network I/O and create a
    # _NullEmitter directly with the requested geometry.
    if virtual_zones > 0:
        if has_ip or has_group:
            _print(
                "ERROR: --zones is for device-free mode; "
                "do not combine with --ip or --group.",
                file=sys.stderr,
            )
            sys.exit(1)
        poly_map: list[bool] = [True] * virtual_zones
        em: Emitter = _NullEmitter(
            zone_count=virtual_zones,
            label="virtual",
            product_name="Virtual Device",
            ip="sim-only",
            pre_poly_map=poly_map,
        )
        _print(f"Virtual device: {virtual_zones} zones (device-free mode)",
               flush=True)

    # --- Connect to device(s) ------------------------------------------------
    elif has_group:
        # Virtual multizone: load group, connect all devices, wrap in emitters.
        # If --config is provided, use the local file; otherwise ask the server.
        if has_config:
            ips: list[str] = _load_group(args.config, args.group)
        else:
            server: str = _server_url or (
                f"{DEFAULT_SERVER_HOST}:{DEFAULT_SERVER_PORT}"
            )
            _print(f"Fetching group '{args.group}' from server ({server})...",
                   flush=True)
            ips = _fetch_group_from_server(args.group, server)
        _print(f"Connecting to group '{args.group}' ({len(ips)} devices)...",
              flush=True)
        devices: list[LifxDevice] = _connect_group(ips)
        member_emitters: list[Emitter] = [LifxEmitter.from_device(d) for d in devices]
        em = VirtualMultizoneEmitter(member_emitters, name=args.group)
        _print(f"  Virtual multizone: {em.zone_count} zones", flush=True)
    else:
        # Single device mode.
        _print(f"Connecting to {args.ip}...", flush=True)
        try:
            dev: LifxDevice = LifxDevice(args.ip)
        except ValueError as exc:
            _print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        dev.query_all()

        if dev.product is None:
            _print(f"ERROR: No response from {args.ip}.", file=sys.stderr)
            dev.close()
            sys.exit(1)

        _print(f"  {dev.label or '?'} — {dev.product_name or '?'}",
              flush=True)

        if dev.is_multizone:
            _print(f"  {dev.zone_count} zones", flush=True)
        elif dev.is_polychrome:
            _print("  Single color bulb", flush=True)
        else:
            _print("  Monochrome bulb (BT.709 luma mode)", flush=True)

        em = LifxEmitter.from_device(dev)

    # --- Sim-only: extract geometry then close real sockets immediately --------
    # From this point on, if sim_only is active, em is a _NullEmitter and
    # no further packets will be sent to the physical lights.
    # Skip when --zones was used — em is already a _NullEmitter.
    if sim_only and virtual_zones <= 0:
        pre_poly: list[bool] = _build_polychrome_map(em)
        null_label: str = em.label or "?"
        null_product: str = em.product_name or "?"
        null_ip: str = em.emitter_id or "sim-only"
        null_zones: int = em.zone_count or 1
        # Close real sockets — the null stub takes over from here.
        em.close()
        em = _NullEmitter(
            zone_count=null_zones,
            label=null_label,
            product_name=null_product,
            ip=null_ip,
            pre_poly_map=pre_poly,
        )
        _print("  Sim-only mode: no commands will be sent to the lights.",
               flush=True)

    # --- Set color interpolation method ---------------------------------------
    lerp_method: str = getattr(args, "lerp", "lab")
    set_lerp_method(lerp_method)
    if not args.quiet:
        _print(f"Color interpolation: {lerp_method}", flush=True)

    # --- Validate effect name -------------------------------------------------
    effect_name: str = args.effect
    registry: Dict[str, Any] = get_registry()
    if effect_name not in registry:
        _print(
            f"ERROR: Unknown effect '{effect_name}'. "
            f"Available: {', '.join(get_effect_names())}",
            file=sys.stderr,
        )
        em.close()
        sys.exit(1)

    # Collect only the parameters the user explicitly provided on the CLI.
    # Parameters left at None were not supplied and will fall back to
    # the effect's declared defaults inside the engine.
    effect_cls = registry[effect_name]
    param_defs = effect_cls.get_param_defs()
    effect_params: Dict[str, Any] = {}
    for pname in param_defs:
        val: Any = getattr(args, pname, None)
        if val is not None:
            effect_params[pname] = val

    # Map the global --zpb flag to the effect's zones_per_bulb Param.
    # The CLI uses the short name for convenience; the effect declares
    # the full name for clarity.
    if "zones_per_bulb" in param_defs:
        zpb_val: int = getattr(args, "zpb", 1)
        effect_params["zones_per_bulb"] = zpb_val

    # --- Ensure emitter is powered on before sending colors --------------------
    # Skipped in sim-only mode: _NullEmitter methods are no-ops, but
    # being explicit avoids confusing log output about powering on.
    if not sim_only:
        em.power_on(duration_ms=0)

    # --- Optional simulator window (--sim or --sim-only) ----------------------
    sim = None
    if getattr(args, "sim", False) or sim_only:
        poly_map: list[bool] = _build_polychrome_map(em)
        zpb: int = getattr(args, "zpb", 1)
        zoom_val: int = getattr(args, "zoom", 1)
        sim = create_simulator(em.zone_count or 1, effect_name,
                               polychrome_map=poly_map,
                               zones_per_bulb=zpb,
                               zoom=zoom_val)
        if sim is None and sim_only:
            # No point continuing — sim-only has no other output channel.
            _print(
                "ERROR: --sim-only requires tkinter. "
                "Install it (e.g. brew install python-tk) and retry.",
                file=sys.stderr,
            )
            em.close()
            sys.exit(1)

    frame_cb = sim.update if sim is not None else None

    # --- Start the render engine ----------------------------------------------
    fps_explicit: bool = args.fps is not None
    fps: int = args.fps if fps_explicit else DEFAULT_FPS
    ctrl: Controller = Controller([em], fps=fps,
                                  frame_callback=frame_cb,
                                  transition_ms=getattr(args, 'transition', None),
                                  fps_explicit=fps_explicit,
                                  zones_per_bulb=getattr(args, 'zpb', 3))
    ctrl.play(effect_name, **effect_params)

    status: dict = ctrl.get_status()
    _print(f"\nPlaying '{effect_name}' at {status['fps']} fps")
    _print(f"Params: {json.dumps(status['params'], indent=2)}")
    _print("Press Ctrl+C to stop.\n")

    # --- Wait for interrupt (SIGINT / SIGTERM) --------------------------------
    stop_requested: threading.Event = threading.Event()
    _install_stop_signal(stop_requested)

    if sim is not None:
        # tkinter must run on the main thread (macOS requirement).
        # Wire window-close to the same stop path as Ctrl+C.
        sim._root.protocol("WM_DELETE_WINDOW",
                           lambda: stop_requested.set())

        def _check_stop() -> None:
            """Poll the stop event from the tkinter event loop."""
            if stop_requested.is_set():
                sim.stop()
            else:
                sim._root.after(SIM_STOP_CHECK_MS, _check_stop)

        sim._root.after(SIM_STOP_CHECK_MS, _check_stop)
        sim.run()  # blocks on mainloop (main thread)
    else:
        # No simulator — block the main thread until signal arrives.
        stop_requested.wait()

    _print("\nStopping...")

    # Print ack-pacing statistics for each emitter before shutdown.
    # VirtualMultizoneEmitter wraps member emitters — check both levels.
    emitters_to_check: list = list(ctrl.emitters)
    for e in ctrl.emitters:
        members = getattr(e, '_emitters', None)
        if members:
            emitters_to_check.extend(members)
    for e in emitters_to_check:
        info = e.get_info()
        ack = info.get("ack_stats")
        if ack and ack.get("sends", 0) > 0:
            _print(f"\n  Ack stats [{info.get('label', '?')}]:")
            _print(f"    Sends:   {ack['sends']}")
            _print(f"    Acked:   {ack['acked']}")
            _print(f"    Drops:   {ack['drops']}")
            _print(f"    RTT avg: {ack['rtt_avg_ms']:.1f} ms")
            _print(f"    RTT min: {ack['rtt_min_ms']:.1f} ms")
            _print(f"    RTT max: {ack['rtt_max_ms']:.1f} ms")

    # In sim-only mode em is a _NullEmitter; skip the fade and power-off
    # so the intent is clear even though the no-ops would be harmless.
    if sim_only:
        ctrl.stop(fade_ms=0)
    else:
        ctrl.stop(fade_ms=DEFAULT_FADE_MS)
        em.power_off(duration_ms=DEFAULT_FADE_MS)
    em.close()
    _print("Done.")


# ---------------------------------------------------------------------------
# Record subcommand — render to GIF/MP4/WebM via ffmpeg
# ---------------------------------------------------------------------------

def _hsbk_to_rgb_tuple(hue: int, sat: int, bri: int, kelvin: int) -> tuple[int, int, int]:
    """Convert an HSBK color to an (R, G, B) tuple of 0-255 ints.

    Args:
        hue:    LIFX hue (0-65535).
        sat:    LIFX saturation (0-65535).
        bri:    LIFX brightness (0-65535).
        kelvin: Color temperature (ignored for display).

    Returns:
        (R, G, B) with each component in 0-255.
    """
    h: float = (hue / HSBK_MAX) * 6.0
    s: float = sat / HSBK_MAX
    b: float = bri / HSBK_MAX

    c: float = b * s
    x: float = c * (1.0 - abs(h % 2.0 - 1.0))
    m: float = b - c

    sextant: int = int(h) % 6
    if sextant == 0:
        r, g, bl = c + m, x + m, m
    elif sextant == 1:
        r, g, bl = x + m, c + m, m
    elif sextant == 2:
        r, g, bl = m, c + m, x + m
    elif sextant == 3:
        r, g, bl = m, x + m, c + m
    elif sextant == 4:
        r, g, bl = x + m, m, c + m
    else:
        r, g, bl = c + m, m, x + m

    return (min(int(r * 255), 255),
            min(int(g * 255), 255),
            min(int(bl * 255), 255))


def _render_frame_pixels(
    colors: list,
    zones_per_bulb: int,
    width: int,
    height: int,
) -> bytes:
    """Render one frame of HSBK zone colors to raw RGB pixel bytes.

    Groups zones into bulbs (using the middle zone's color), then
    paints each bulb as a vertical column scaled to fill the output
    width.  Gaps between bulbs are rendered in the background color.

    Args:
        colors:         List of (H, S, B, K) tuples, one per zone.
        zones_per_bulb: Zones per physical bulb (e.g. 3).
        width:          Output image width in pixels.
        height:         Output image height in pixels.

    Returns:
        Raw RGB bytes (width * height * 3) suitable for ffmpeg rawvideo.
    """
    zpb: int = max(1, zones_per_bulb)
    zone_count: int = len(colors)
    bulb_count: int = max(1, (zone_count + zpb - 1) // zpb)

    # Sample the middle zone of each bulb group for its display color.
    bulb_colors: list[tuple[int, int, int]] = []
    for b_idx in range(bulb_count):
        mid: int = min(b_idx * zpb + zpb // 2, zone_count - 1)
        h, s, br, k = colors[mid]
        bulb_colors.append(_hsbk_to_rgb_tuple(h, s, br, k))

    # Compute pixel column assignments.
    total_gaps: int = (bulb_count - 1) * RECORD_ZONE_GAP
    usable: int = max(bulb_count, width - total_gaps)
    bulb_w: int = usable // bulb_count

    # Build one scanline.
    bg: tuple[int, int, int] = RECORD_BG_COLOR
    scanline: bytearray = bytearray()
    for b_idx in range(bulb_count):
        r, g, b = bulb_colors[b_idx]
        scanline.extend(bytes([r, g, b]) * bulb_w)
        if b_idx < bulb_count - 1:
            scanline.extend(bytes(bg) * RECORD_ZONE_GAP)

    # Pad or trim to exact width.
    row_bytes: int = width * 3
    if len(scanline) < row_bytes:
        scanline.extend(bytes(bg) * ((row_bytes - len(scanline)) // 3))
    scanline = scanline[:row_bytes]

    # Replicate scanline for the full height.
    row: bytes = bytes(scanline)
    return row * height


def cmd_record(args: argparse.Namespace) -> None:
    """Render an effect to a video file (GIF, MP4, or WebM) via ffmpeg.

    No device or network connection is needed.  The effect is rendered
    headlessly at deterministic timestamps.  If the effect has a known
    period and no explicit ``--duration`` was given, exactly one cycle
    is recorded for seamless looping.

    A JSON metadata sidecar is written alongside the output file
    containing effect name, parameters, and reproduction details.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.
    """
    import os
    import subprocess as sp
    from datetime import date

    # --- Validate ffmpeg is available ----------------------------------------
    try:
        sp.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (FileNotFoundError, sp.CalledProcessError):
        _print(
            "ERROR: ffmpeg is required for recording. "
            "Install with: brew install ffmpeg (macOS) or "
            "sudo apt install ffmpeg (Linux).",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Set color interpolation method --------------------------------------
    lerp_method: str = getattr(args, "lerp", "lab")
    set_lerp_method(lerp_method)

    # --- Resolve effect and parameters ---------------------------------------
    effect_name: str = args.effect
    registry: Dict[str, Any] = get_registry()
    if effect_name not in registry:
        _print(
            f"ERROR: Unknown effect '{effect_name}'. "
            f"Available: {', '.join(get_effect_names())}",
            file=sys.stderr,
        )
        sys.exit(1)

    effect_cls = registry[effect_name]
    param_defs = effect_cls.get_param_defs()
    effect_params: Dict[str, Any] = {}
    for pname in param_defs:
        val: Any = getattr(args, pname, None)
        if val is not None:
            effect_params[pname] = val

    # Map --zpb to zones_per_bulb if the effect declares it.
    zpb: int = getattr(args, "zpb", DEFAULT_RECORD_ZPB)
    if "zones_per_bulb" in param_defs:
        effect_params["zones_per_bulb"] = zpb

    effect = create_effect(effect_name, **effect_params)

    # --- Determine recording parameters -------------------------------------
    zones: int = getattr(args, "zones", DEFAULT_RECORD_ZONES)
    fps: int = getattr(args, "fps", DEFAULT_FPS)
    width: int = getattr(args, "width", DEFAULT_RECORD_WIDTH)
    height: int = getattr(args, "height", DEFAULT_RECORD_HEIGHT)
    fmt: str = getattr(args, "format", DEFAULT_RECORD_FORMAT)
    author: str = getattr(args, "author", None) or ""
    title: str = getattr(args, "title", None) or ""

    # Determine duration: explicit --duration, or one period, or default.
    explicit_duration: bool = getattr(args, "duration", None) is not None
    effect_period = effect.period()
    if explicit_duration:
        duration: float = args.duration
        looping: bool = False
    elif effect_period is not None and effect_period > 0:
        duration = effect_period
        looping = True
        _print(f"Effect period detected: {duration:.2f}s (recording one cycle for seamless loop)")
    else:
        duration = DEFAULT_RECORD_DURATION
        looping = False

    total_frames: int = max(1, int(duration * fps))
    dt: float = 1.0 / fps

    # --- Determine output path -----------------------------------------------
    output: str = getattr(args, "output", None) or f"{effect_name}.{fmt}"
    if not output.endswith(f".{fmt}"):
        output = f"{output}.{fmt}"
    json_path: str = os.path.splitext(output)[0] + ".json"

    _print(f"Recording '{effect_name}' → {output}")
    _print(f"  {zones} zones, {zpb} zpb, {fps} fps, {duration:.2f}s "
           f"({total_frames} frames), {width}×{height}px")
    if looping:
        _print(f"  Looping: one full cycle ({effect_period:.2f}s)")

    # --- Build ffmpeg command ------------------------------------------------
    ffmpeg_cmd: list[str] = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "pipe:0",
    ]

    if fmt == "gif":
        # Palette-optimized GIF: split input, generate a global palette
        # from one copy, apply it to the other.  Must use -filter_complex
        # (not -vf) because the graph has named streams.
        ffmpeg_cmd.extend([
            "-filter_complex",
            "[0:v]split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
            "-loop", "0",
        ])
    elif fmt == "mp4":
        ffmpeg_cmd.extend([
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
        ])
    elif fmt == "webm":
        ffmpeg_cmd.extend([
            "-c:v", "libvpx-vp9",
            "-b:v", "0",
            "-crf", "30",
        ])

    ffmpeg_cmd.append(output)

    # --- Render frames and pipe to ffmpeg ------------------------------------
    proc = sp.Popen(ffmpeg_cmd, stdin=sp.PIPE, stdout=sp.DEVNULL,
                    stderr=sp.PIPE)

    realtime: bool = getattr(args, "realtime", False)

    try:
        for frame_idx in range(total_frames):
            t: float = frame_idx * dt
            colors = effect.render(t, zones)
            pixels: bytes = _render_frame_pixels(colors, zpb, width, height)
            proc.stdin.write(pixels)

            # In realtime mode, sleep between frames so wall-clock-
            # dependent effects (e.g. binclock) advance naturally.
            if realtime:
                time.sleep(dt)

            # Progress indicator every 20%.
            if total_frames >= 10 and frame_idx % (total_frames // 5) == 0:
                pct: int = int(100 * frame_idx / total_frames)
                _print(f"  {pct}%...", end=" ", flush=True)

        proc.stdin.close()
        proc.wait()
        stderr_bytes: bytes = proc.stderr.read()

        if proc.returncode != 0:
            _print(f"\nERROR: ffmpeg failed:\n{stderr_bytes.decode()}", file=sys.stderr)
            sys.exit(1)

        _print(f"\n  Wrote {output}")

    except BrokenPipeError:
        proc.wait()
        stderr_bytes = proc.stderr.read()
        _print(f"\nERROR: ffmpeg pipe broke:\n{stderr_bytes.decode()}", file=sys.stderr)
        sys.exit(1)

    # --- Write JSON metadata sidecar -----------------------------------------
    all_params: Dict[str, Any] = effect.get_params()
    metadata: Dict[str, Any] = {
        "effect": effect_name,
        "description": effect_cls.description,
        "params": all_params,
        "zones": zones,
        "zpb": zpb,
        "duration": round(duration, 3),
        "looping": looping,
        "fps": fps,
        "width": width,
        "height": height,
        "format": fmt,
        "lerp": lerp_method,
        "file": os.path.basename(output),
        "created": str(date.today()),
    }
    if author:
        metadata["author"] = author
    if title:
        metadata["title"] = title

    # media_url: relative path for gallery JS (defaults to output filename).
    media_url: str = getattr(args, "media_url", None) or os.path.basename(output)
    metadata["media_url"] = media_url

    # Build a CLI command that reproduces this recording.
    cmd_parts: list[str] = [
        "python3 glowup.py play", effect_name,
        f"--zones {zones}", f"--zpb {zpb}",
    ]
    for pname, pval in all_params.items():
        if pname == "zones_per_bulb":
            continue  # already covered by --zpb
        cmd_parts.append(f"--{pname.replace('_', '-')} {pval}")
    metadata["command"] = " ".join(cmd_parts)

    with open(json_path, "w") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")

    _print(f"  Wrote {json_path}")
    _print("Done.")


# ---------------------------------------------------------------------------
# Layered effect help
# ---------------------------------------------------------------------------

def _print_effect_help(effect_name: str) -> None:
    """Print parameters and usage for a single named effect then return.

    Called when the user runs ``glowup.py play <effect> --help``.
    Intentionally bypasses the quiet flag — help is always useful.

    Args:
        effect_name: The effect name as typed on the command line.
    """
    registry = get_registry()
    if effect_name not in registry:
        available: str = ", ".join(get_effect_names())
        print(f"ERROR: Unknown effect '{effect_name}'.", file=sys.stderr)
        print(f"Available effects: {available}", file=sys.stderr)
        return

    cls = registry[effect_name]
    params = cls.get_param_defs()

    print(f"\nEffect: {effect_name}")
    print(f"  {cls.description}")

    if params:
        print("\nParameters:")
        col: int = 20   # left-column width for the flag names
        for pname, pdef in sorted(params.items()):
            flag: str = f"--{pname}"
            range_str: str = ""
            if pdef.min is not None and pdef.max is not None:
                range_str = f"  [{pdef.min}..{pdef.max}]"
            elif pdef.choices:
                range_str = f"  {pdef.choices}"
            # First line: flag + description
            print(f"  {flag:<{col}}  {pdef.description}")
            # Second line: default + range, indented to align
            print(f"  {'':>{col}}  default: {pdef.default}{range_str}")

    print(f"\nUsage:")
    print(f"  python3 glowup.py play {effect_name} --device <label> [parameters]")
    print(f"  python3 glowup.py play {effect_name} --ip <device-ip> [parameters]")
    print(f"  python3 glowup.py play {effect_name} "
          f"--group <name> [parameters]")
    print(f"  python3 glowup.py play {effect_name} "
          f"--group <name> --config <file> [parameters]")
    print(f"  python3 glowup.py play {effect_name} "
          f"--ip <device-ip> --sim-only [parameters]")
    print(f"  python3 glowup.py play {effect_name} "
          f"--zones 36 --zpb 3 [parameters]")
    print()
    print(f"  --device and --group run through the server (visible on dashboard).")
    print(f"  --ip sends UDP directly (standalone, not visible on dashboard).")
    print()


# ---------------------------------------------------------------------------
# replay subcommand
# ---------------------------------------------------------------------------

def cmd_replay(args: argparse.Namespace) -> None:
    """Replay a MIDI file onto the signal bus via MQTT.

    Parses the MIDI file and publishes structured events to the bus
    at the requested speed.  At real-time speed (default), events are
    timed to match the original tempo.  At speed 0, events are sent
    as fast as possible for bulk data loading via the persistence emitter.

    Args:
        args: Parsed CLI arguments (file, broker, port, speed, signal_name).
    """
    from distributed.midi_sensor import MidiSensor

    file_path: str = args.file
    if not Path(file_path).exists():
        _print(f"ERROR: MIDI file not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    sensor: MidiSensor = MidiSensor(
        file_path=file_path,
        broker=args.broker,
        port=args.port,
        signal_name=args.signal_name,
        speed=args.speed,
    )

    # Handle Ctrl+C gracefully.
    def _shutdown(signum: int, frame: object) -> None:
        """Signal handler for clean shutdown."""
        _print("\nStopping replay...")
        sensor.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    speed_label: str = (
        "unlimited (bulk)" if args.speed == 0.0
        else f"{args.speed}x"
    )
    _print(f"Replaying {file_path} at {speed_label} speed...")
    _print(f"  Broker: {args.broker}:{args.port}")
    _print(f"  Signal: {args.signal_name}")
    _print()

    sensor.start()

    _print("Done.")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with all subcommands.

    Each registered effect's :class:`Param` declarations are
    automatically added as ``--flag`` options on the ``play``
    subcommand so the user can tune any parameter from the CLI
    without code changes.

    Returns
    -------
    argparse.ArgumentParser
        A fully configured parser ready for ``parse_args()``.
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="glowup",
        description="GlowUp — drive animated effects on "
                    "LIFX devices",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress the startup banner and informational output",
    )
    parser.add_argument(
        "--server", default=None, metavar="HOST:PORT",
        help=(
            f"GlowUp server address for routing UDP commands "
            f"(default: {DEFAULT_SERVER_HOST}:{DEFAULT_SERVER_PORT}). "
            f"Auto-detected if omitted — the server is used when reachable."
        ),
    )
    parser.add_argument(
        "--local", action="store_true", default=False,
        help=(
            "Force direct UDP even if the server is reachable. "
            "Useful for testing or when running on the same machine as the server."
        ),
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # -- discover --------------------------------------------------------------
    p_disc = sub.add_parser("discover", help="Find LIFX devices on the LAN")
    p_disc.add_argument(
        "--timeout", type=float, default=DEFAULT_DISCOVERY_TIMEOUT,
        help=f"Discovery timeout in seconds (default: {DEFAULT_DISCOVERY_TIMEOUT})",
    )
    p_disc.add_argument(
        "--ip", type=str, default=None,
        help="Query a specific device IP or hostname instead of broadcasting",
    )
    p_disc.add_argument(
        "--json", action="store_true",
        help="Also output results as JSON",
    )

    # -- effects ---------------------------------------------------------------
    sub.add_parser(
        "effects", help="List available effects and their parameters",
    )

    # -- identify --------------------------------------------------------------
    p_ident = sub.add_parser(
        "identify", help="Pulse a device to visually locate it",
    )
    p_ident.add_argument(
        "--ip", required=True,
        help="Target device IP address or hostname",
    )
    p_ident.add_argument(
        "--duration", type=float, default=IDENTIFY_DEFAULT_DURATION,
        help=(
            f"Pulse duration in seconds when routing via server "
            f"(default: {IDENTIFY_DEFAULT_DURATION:.0f}s). "
            f"Ignored when running locally — use Ctrl+C to stop."
        ),
    )

    # -- power ----------------------------------------------------------------
    p_power = sub.add_parser(
        "power",
        help="Turn a device or group on or off",
    )
    p_power.add_argument(
        "state", choices=["on", "off"],
        help="Power state: 'on' or 'off'",
    )
    p_power.add_argument(
        "--device", required=True,
        help=(
            "Device label, MAC, IP, or group (e.g. 'group:main_bedroom', "
            "'PORCH STRING LIGHTS', 'group:all')"
        ),
    )

    # -- off ----------------------------------------------------------------
    sub.add_parser(
        "off",
        help="⚠️  EMERGENCY: Power off all LIFX devices on the network",
    )

    # -- monitor ---------------------------------------------------------------
    p_mon = sub.add_parser(
        "monitor",
        help="Monitor a multizone device in real time",
    )
    p_mon.add_argument(
        "--ip", required=True,
        help="Target device IP address or hostname",
    )
    p_mon.add_argument(
        "--hz", type=float, default=DEFAULT_MONITOR_POLL_HZ,
        help=f"Polling rate in Hz (default: {DEFAULT_MONITOR_POLL_HZ})",
    )
    p_mon.add_argument(
        "--zpb", type=int, default=3,
        help="Zones per bulb (default: 3 for LIFX string lights)",
    )
    p_mon.add_argument(
        "--zoom", type=int, default=1,
        help="Simulator zoom factor 1-10 (nearest-neighbor scaling, default: 1)",
    )

    # -- play ------------------------------------------------------------------
    p_play = sub.add_parser(
        "play",
        help="Run an effect on a device",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Effect-specific parameters are hidden from this view.\n"
            "To see all parameters for a specific effect:\n\n"
            "  python3 glowup.py play <effect> --help\n\n"
            "Example:\n"
            "  python3 glowup.py play fireworks --help\n\n"
            "To list all available effects:\n\n"
            "  python3 glowup.py effects"
        ),
    )
    p_play.add_argument(
        "effect", help="Effect name (run 'effects' to list, or 'play <effect> --help')",
    )
    p_play.add_argument(
        "--ip", default=None,
        help=(
            "Target device IP address or hostname (direct UDP, "
            "bypasses the server — effect will NOT appear on "
            "the dashboard)"
        ),
    )
    p_play.add_argument(
        "--device", default=None,
        help=(
            "Target device by registry label or MAC address. "
            "Requires the GlowUp server — the server resolves "
            "the identifier and runs the effect.  Visible on "
            "the dashboard."
        ),
    )
    p_play.add_argument(
        "--music-dir", default=None, metavar="PATH",
        help=(
            "Directory of audio files to play in shuffled order. "
            "The server decodes the files via ffmpeg and feeds the "
            "audio signal to the effect.  Requires --device and a "
            "media-reactive effect (waveform, soundlevel)."
        ),
    )
    p_play.add_argument(
        "--bands", type=int, default=32, metavar="N",
        help=(
            "Number of FFT frequency bands for audio-reactive effects "
            "(default 32).  More bands = finer spectral resolution on "
            "multizone devices."
        ),
    )
    p_play.add_argument(
        "--audio-offset-ms", type=int, default=0, metavar="MS",
        help=(
            "Manual audio sync offset in milliseconds (added to "
            "the automatic calibration result).  Positive values "
            "delay lights further; negative brings them forward."
        ),
    )
    p_play.add_argument(
        "--skip-calibration", action="store_true",
        help="Skip automatic audio sync calibration (debug only)",
    )
    p_play.add_argument(
        "--screen", action="store_true",
        help=(
            "Screen-reactive mode: capture the local screen and drive "
            "the effect from screen content.  Automatically selects the "
            "screen_light effect.  Works with --device, --ip, or --sim."
        ),
    )
    p_play.add_argument(
        "--video-url", default=None, metavar="URL",
        help=(
            "Video input URL for --screen mode (replaces screen capture). "
            "Use with HDHomeRun: http://<ip>:5004/auto/v<channel>  "
            "Any ffmpeg-compatible URL works (RTSP, HTTP, UDP, etc.)."
        ),
    )
    p_play.add_argument(
        "--no-blur", action="store_true",
        help=(
            "Disable Gaussian blur in screen-reactive sim mode.  "
            "Shows flat color rects instead of the blurred glow.  "
            "Useful for performance comparison and debugging."
        ),
    )
    p_play.add_argument(
        "--config", default=None,
        help="Path to local config file containing device groups",
    )
    p_play.add_argument(
        "--group", default=None,
        help=(
            "Device group name. Fetched from the server unless "
            "--config provides a local file"
        ),
    )
    p_play.add_argument(
        "--fps", type=int, default=None,
        help=f"Frames per second (default: {DEFAULT_FPS}, auto-tuned for Neon)",
    )
    p_play.add_argument(
        "--sim", action="store_true", default=False,
        help="Open a live simulator window showing the effect",
    )
    p_play.add_argument(
        "--sim-only", dest="sim_only", action="store_true", default=False,
        help=(
            "Query device geometry then show the effect in the simulator "
            "only — no color or power commands are sent to the lights"
        ),
    )
    p_play.add_argument(
        "--zones", type=int, default=None,
        help=(
            "Zone count for device-free simulator mode (implies --sim-only). "
            "Example: --zones 36 --zpb 3 for a 12-bulb string light"
        ),
    )
    p_play.add_argument(
        "--zpb", type=int, default=3,
        help="Zones per bulb (default: 3 for LIFX string lights)",
    )
    p_play.add_argument(
        "--zoom", type=int, default=1,
        help="Simulator zoom factor 1-10 (nearest-neighbor scaling, default: 1)",
    )
    p_play.add_argument(
        "--transition", type=int, default=None,
        help="Firmware transition time in ms per frame (default: 2000/fps). "
             "0=snap, higher=smoother but adds latency",
    )
    p_play.add_argument(
        "--lerp", type=str, default="oklab",
        choices=["oklab", "lab", "hsb"],
        help="Color interpolation method: oklab (best, default), "
             "lab (classic CIELAB), or hsb (cheap)",
    )

    # Auto-add every effect's Param declarations as CLI flags.
    # Help text is suppressed here — users run "play <effect> --help"
    # for the per-effect page.  A ``seen`` set prevents duplicate flags
    # when multiple effects share a parameter name (e.g. "speed").
    seen: set = set()
    for _effect_name, effect_cls in get_registry().items():
        for pname, pdef in effect_cls.get_param_defs().items():
            if pname in seen:
                continue
            seen.add(pname)

            kwargs: Dict[str, Any] = {
                "default": None,
                "help": argparse.SUPPRESS,  # shown via "play <effect> --help"
            }

            # Infer argparse type from the Param's default value type
            if isinstance(pdef.default, int):
                kwargs["type"] = int
            elif isinstance(pdef.default, float):
                kwargs["type"] = float
            elif isinstance(pdef.default, str):
                kwargs["type"] = str

            if pdef.choices:
                kwargs["choices"] = pdef.choices

            p_play.add_argument(
                f"--{pname.replace('_', '-')}",
                dest=pname,
                **kwargs,
            )

    # -- record ----------------------------------------------------------------
    p_record = sub.add_parser(
        "record",
        help="Render an effect to GIF/MP4/WebM via ffmpeg (no device needed)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Effect-specific parameters are hidden from this view.\n"
            "To see all parameters for a specific effect:\n\n"
            "  python3 glowup.py record <effect> --help\n\n"
            "If the effect has a known period and no --duration is given,\n"
            "exactly one cycle is recorded for seamless looping.\n\n"
            "A JSON metadata sidecar is written alongside every recording."
        ),
    )
    p_record.add_argument(
        "effect", help="Effect name (run 'effects' to list)",
    )
    p_record.add_argument(
        "--zones", type=int, default=DEFAULT_RECORD_ZONES,
        help=f"Number of zones to simulate (default: {DEFAULT_RECORD_ZONES})",
    )
    p_record.add_argument(
        "--zpb", type=int, default=DEFAULT_RECORD_ZPB,
        help=f"Zones per bulb (default: {DEFAULT_RECORD_ZPB})",
    )
    p_record.add_argument(
        "--fps", type=int, default=DEFAULT_FPS,
        help=f"Frames per second (default: {DEFAULT_FPS})",
    )
    p_record.add_argument(
        "--duration", type=float, default=None,
        help="Recording duration in seconds (auto-detected from period if omitted)",
    )
    p_record.add_argument(
        "--width", type=int, default=DEFAULT_RECORD_WIDTH,
        help=f"Output width in pixels (default: {DEFAULT_RECORD_WIDTH})",
    )
    p_record.add_argument(
        "--height", type=int, default=DEFAULT_RECORD_HEIGHT,
        help=f"Output height in pixels (default: {DEFAULT_RECORD_HEIGHT})",
    )
    p_record.add_argument(
        "--format", type=str, default=DEFAULT_RECORD_FORMAT,
        choices=RECORD_SUPPORTED_FORMATS,
        help=f"Output format (default: {DEFAULT_RECORD_FORMAT})",
    )
    p_record.add_argument(
        "--output", type=str, default=None,
        help="Output file path (default: <effect>.<format>)",
    )
    p_record.add_argument(
        "--lerp", type=str, default="oklab",
        choices=["oklab", "lab", "hsb"],
        help="Color interpolation method (default: oklab)",
    )
    p_record.add_argument(
        "--author", type=str, default=None,
        help="Author name for the metadata sidecar",
    )
    p_record.add_argument(
        "--title", type=str, default=None,
        help="Title / description for the metadata sidecar",
    )
    p_record.add_argument(
        "--media-url", dest="media_url", type=str, default=None,
        help="Relative URL path for gallery use (e.g. assets/previews/aurora.gif). "
             "Defaults to the output filename.",
    )
    p_record.add_argument(
        "--realtime", action="store_true", default=False,
        help="Sleep between frames so wall-clock-dependent effects "
             "(e.g. binclock) animate correctly. Recording takes real time.",
    )

    # Add effect params to record subcommand (same pattern as play).
    # Skip params whose names collide with record's own arguments.
    record_reserved: set = {
        "effect", "zones", "zpb", "fps", "duration", "width", "height",
        "format", "output", "lerp", "author", "title", "media_url",
    }
    seen_rec: set = set()
    for _effect_name, effect_cls in get_registry().items():
        for pname, pdef in effect_cls.get_param_defs().items():
            if pname in seen_rec or pname in record_reserved:
                continue
            seen_rec.add(pname)

            kwargs_rec: Dict[str, Any] = {
                "default": None,
                "help": argparse.SUPPRESS,
            }

            if isinstance(pdef.default, int):
                kwargs_rec["type"] = int
            elif isinstance(pdef.default, float):
                kwargs_rec["type"] = float
            elif isinstance(pdef.default, str):
                kwargs_rec["type"] = str

            if pdef.choices:
                kwargs_rec["choices"] = pdef.choices

            p_record.add_argument(
                f"--{pname.replace('_', '-')}",
                dest=pname,
                **kwargs_rec,
            )

    # -- replay ----------------------------------------------------------------
    p_replay = sub.add_parser(
        "replay",
        help="Replay a MIDI file onto the signal bus via MQTT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Replays MIDI events at the original tempo (real-time) or\n"
            "as fast as possible (--speed 0) for bulk data loading.\n\n"
            "Examples:\n"
            "  python3 glowup.py replay --file song.mid\n"
            "  python3 glowup.py replay --file song.mid --speed 0\n"
            "  python3 glowup.py replay --file song.mid --speed 2"
        ),
    )
    p_replay.add_argument(
        "--file", required=True,
        help="Path to a Standard MIDI File (.mid)",
    )
    p_replay.add_argument(
        "--broker", default=DEFAULT_REPLAY_BROKER,
        help=f"MQTT broker host (default: {DEFAULT_REPLAY_BROKER})",
    )
    p_replay.add_argument(
        "--port", type=int, default=DEFAULT_REPLAY_PORT,
        help=f"MQTT broker port (default: {DEFAULT_REPLAY_PORT})",
    )
    p_replay.add_argument(
        "--speed", type=float, default=DEFAULT_REPLAY_SPEED,
        help=(
            f"Replay speed multiplier (default: {DEFAULT_REPLAY_SPEED}).  "
            f"0 = as fast as possible (bulk ingest)."
        ),
    )
    p_replay.add_argument(
        "--signal-name", dest="signal_name",
        default=DEFAULT_REPLAY_SIGNAL,
        help=f"Signal name on the bus (default: '{DEFAULT_REPLAY_SIGNAL}')",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse arguments and dispatch to the appropriate subcommand handler.

    Help is layered:

    * ``glowup.py --help``               — top-level command list.
    * ``glowup.py play --help``          — play options (effect params hidden).
    * ``glowup.py play <effect> --help`` — full parameter reference for one effect.

    If no subcommand is given, prints help and exits cleanly.
    Prints a copyright/license banner on startup unless ``-q``/``--quiet``
    is given.
    """
    # --- Intercept "play/record <effect> --help" before argparse consumes -h --
    # sys.argv[1] is "play" or "record", sys.argv[2] is a non-flag token
    # (the effect name), and -h or --help appears anywhere after.
    argv = sys.argv[1:]
    if (
        len(argv) >= 2
        and argv[0] in ("play", "record")
        and not argv[1].startswith("-")
        and ("-h" in argv or "--help" in argv)
    ):
        _print_effect_help(argv[1])
        sys.exit(0)

    parser: argparse.ArgumentParser = build_parser()
    args: argparse.Namespace = parser.parse_args()

    global _quiet, _server_url
    _quiet = getattr(args, "quiet", False)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Print the startup banner (suppressed by -q/--quiet).
    _print(_BANNER)

    # --- Determine routing mode: via server or direct UDP --------------------
    # Commands that only need local computation skip the server probe.
    _LOCAL_ONLY_COMMANDS: set[str] = {"effects", "record", "replay"}
    if args.command not in _LOCAL_ONLY_COMMANDS and not args.local:
        server_addr: str = args.server or (
            f"{DEFAULT_SERVER_HOST}:{DEFAULT_SERVER_PORT}"
        )
        if _probe_server(server_addr):
            _server_url = server_addr
            _print(f"Routing via server at {server_addr}")
        else:
            _print(
                f"Server at {server_addr} unreachable — "
                f"running locally (direct UDP)"
            )
    elif args.local:
        _print("Running locally (--local flag set, direct UDP)")

    # Dispatch table -- maps subcommand names to handler functions
    commands: Dict[str, Callable[[argparse.Namespace], None]] = {
        "discover": cmd_discover,
        "effects": cmd_effects,
        "identify": cmd_identify,
        "monitor": cmd_monitor,
        "off": cmd_off,
        "power": cmd_power,
        "play": cmd_play,
        "record": cmd_record,
        "replay": cmd_replay,
    }

    handler: Optional[Callable[[argparse.Namespace], None]] = commands.get(
        args.command
    )
    if handler is None:
        # Defensive: argparse constrains choices, but guard anyway
        _print(f"ERROR: Unknown command '{args.command}'.", file=sys.stderr)
        sys.exit(1)

    handler(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Clean exit on Ctrl+C — no ugly traceback.
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except BrokenPipeError:
        # Piped output closed early (e.g. glowup.py effects | head).
        # Suppress the traceback and flush stderr quietly.
        sys.stderr.close()
        sys.exit(1)
