#!/usr/bin/env python3
"""LIFX Effect Engine -- command-line interface.

Usage::

    python3 glowup.py discover                    # find all LIFX devices
    python3 glowup.py effects                     # list available effects
    python3 glowup.py identify --ip <device-ip>   # pulse a device to locate it
    python3 glowup.py monitor --ip <device-ip>           # monitor device in real time
    python3 glowup.py play cylon --ip <device-ip>    # run an effect on one device
    python3 glowup.py play cylon --config conf.json --group office  # virtual multizone
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
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from transport import LifxDevice, discover_devices
from emitters import Emitter
from emitters.lifx import LifxEmitter
from emitters.virtual import VirtualMultizoneEmitter
from engine import Controller
from effects import get_registry, get_effect_names, create_effect, HSBK, HSBK_MAX, KELVIN_DEFAULT
from colorspace import set_lerp_method

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

DEFAULT_REPLAY_BROKER: str = "10.0.0.48"
"""Default MQTT broker for replay (Pi)."""

DEFAULT_REPLAY_PORT: int = 1883
"""Default MQTT broker port."""

DEFAULT_REPLAY_SIGNAL: str = "sensor:midi:events"
"""Default signal name for MIDI replay events on the bus."""

# Minimum column widths for the discovery table display.
# These prevent columns from collapsing when device labels are short.
_COL_MIN_LABEL: int = 12
_COL_MIN_PRODUCT: int = 14
_COL_MIN_GROUP: int = 8
_COL_MIN_IP: int = 13
_COL_MIN_MAC: int = 17
_COL_MIN_ZONES: int = 5

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
                   rapid: bool = True) -> None:
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

        {"groups": {"office": ["10.0.0.25", "10.0.0.26"]}}

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

def cmd_discover(args: argparse.Namespace) -> None:
    """Discover and display all LIFX devices on the local network.

    Sends a UDP broadcast and collects responses, then prints a
    formatted table of discovered devices.  Optionally emits JSON
    output when ``--json`` is passed.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.  Expected attributes:
        ``timeout`` (float) and ``json`` (bool).
    """
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
        _print("    packets between wireless nodes. Try --ip <device-ip> to")
        _print("    query a specific device directly.")
        _print("  • Devices may be powered off or unreachable on a different")
        _print("    subnet or VLAN.")
        _print("  • Increasing --timeout (default 5s) can help on congested")
        _print("    networks.")
        _print("  • The LIFX app on your phone can confirm whether devices")
        _print("    are online.")
        _print("  • Check your router's admin page for connected devices and")
        _print("    their IP addresses, then add them to server.json groups.")
        return

    # Build a list of plain-dict rows for tabular display
    rows: List[Dict[str, str]] = []
    for dev in devices:
        rows.append({
            "label": dev.label or "?",
            "product": dev.product_name or "?",
            "group": dev.group or "",
            "ip": dev.ip,
            "mac": dev.mac_str,
            "zones": str(dev.zone_count or "-"),
        })

    # Column definitions: (header text, row-dict key, minimum width)
    cols: List[Tuple[str, str, int]] = [
        ("Label",       "label",   _COL_MIN_LABEL),
        ("Product",     "product", _COL_MIN_PRODUCT),
        ("Group",       "group",   _COL_MIN_GROUP),
        ("IP Address",  "ip",      _COL_MIN_IP),
        ("MAC Address", "mac",     _COL_MIN_MAC),
        ("Zones",       "zones",   _COL_MIN_ZONES),
    ]

    # Compute actual widths: max of (minimum, header length, longest value)
    widths: List[int] = []
    for header, key, min_w in cols:
        w: int = max(min_w, len(header),
                     max((len(r[key]) for r in rows), default=0))
        widths.append(w)

    # Print header row, separator line, and data rows
    header_line: str = _COL_SEP.join(
        cols[i][0].ljust(widths[i]) for i in range(len(cols))
    )
    _print(header_line)
    _print(_COL_SEP.join("-" * widths[i] for i in range(len(cols))))
    for r in rows:
        line: str = _COL_SEP.join(
            str(r[cols[i][1]]).ljust(widths[i]) for i in range(len(cols))
        )
        _print(line)
    _print(f"\n{len(rows)} device(s) found.")

    if args.json:
        _print("\n" + json.dumps(
            [
                {
                    "label": r["label"], "product": r["product"],
                    "group": r["group"], "ip": r["ip"],
                    "mac": r["mac"], "zones": r["zones"],
                }
                for r in rows
            ],
            indent=2,
        ))


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
        _print(f"\n  {name}: {cls.description}")
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
    """Slowly pulse a device's brightness so the user can locate it.

    Connects to the device, powers it on, then smoothly ramps
    brightness up and down in a sine-wave cycle until Ctrl+C.
    On stop, the device is powered off.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.  Expected attributes: ``ip`` (str).
    """
    if not args.ip:
        _print("ERROR: --ip is required for identify command.", file=sys.stderr)
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
    _print(f"\nPulsing brightness on {args.ip}.")
    _print("Press Ctrl+C to stop.\n")

    # --- Power on and pulse ---------------------------------------------------
    dev.set_power(on=True, duration_ms=0)

    stop_requested: threading.Event = threading.Event()

    def _handle_signal(sig: int, frame: Any) -> None:
        """Signal handler that unblocks the main thread."""
        stop_requested.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Warm white, zero saturation — works on both color and monochrome bulbs.
    start_time: float = time.monotonic()
    while not stop_requested.is_set():
        elapsed: float = time.monotonic() - start_time

        # Sine wave from 0..1, mapped to IDENTIFY_MIN_BRI..1.0
        phase: float = (math.sin(2.0 * math.pi * elapsed / IDENTIFY_CYCLE_SECONDS) + 1.0) / 2.0
        bri_frac: float = IDENTIFY_MIN_BRI + phase * (1.0 - IDENTIFY_MIN_BRI)
        bri: int = int(bri_frac * HSBK_MAX)

        if dev.is_multizone:
            color = (0, 0, bri, KELVIN_DEFAULT)
            colors = [color] * dev.zone_count
            dev.set_zones(colors, duration_ms=0, rapid=True)
        else:
            dev.set_color(0, 0, bri, KELVIN_DEFAULT, duration_ms=0)

        stop_requested.wait(timeout=IDENTIFY_FRAME_INTERVAL)

    # --- Cleanup --------------------------------------------------------------
    _print("\nStopping...")
    dev.set_power(on=False, duration_ms=DEFAULT_FADE_MS)
    dev.close()
    _print("Done.")


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
    from simulator import create_simulator

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
    def _handle_signal(sig: int, frame: Any) -> None:
        """Signal handler that unblocks the main thread."""
        stop_requested.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

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


def cmd_play(args: argparse.Namespace) -> None:
    """Connect to a LIFX device (or group) and run the named effect.

    Supports two modes:

    * **Single device** — ``--ip <address>`` targets one device.
    * **Virtual multizone** — ``--config <file> --group <name>`` loads
      a device group from the config file and treats every device in
      the group as one zone in a virtual multizone strip.

    This function blocks until SIGINT or SIGTERM is received, then
    gracefully fades the device(s) to black and disconnects.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.  Expected attributes: ``ip`` (str or None),
        ``config`` (str or None), ``group`` (str or None),
        ``effect`` (str), ``fps`` (int), plus any effect-specific
        parameters.
    """
    has_ip: bool = bool(getattr(args, "ip", None))
    has_group: bool = bool(getattr(args, "config", None) and
                           getattr(args, "group", None))
    sim_only: bool = bool(getattr(args, "sim_only", False))
    virtual_zones: int = getattr(args, "zones", None) or 0

    # --zones implies --sim-only (no device needed).
    if virtual_zones > 0:
        sim_only = True
        args.sim_only = True

    if not has_ip and not has_group and virtual_zones <= 0:
        _print(
            "ERROR: Specify either --ip or both --config and --group.\n"
            "       For device-free simulator mode, use --zones <count>.",
            file=sys.stderr,
        )
        sys.exit(1)

    if has_ip and has_group:
        _print(
            "ERROR: --ip and --config/--group are mutually exclusive.",
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
                "do not combine with --ip or --config/--group.",
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
        ips: list[str] = _load_group(args.config, args.group)
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
        from simulator import create_simulator
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

    def _handle_signal(sig: int, frame: Any) -> None:
        """Signal handler that unblocks the main thread."""
        stop_requested.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

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
    print(f"  python3 glowup.py play {effect_name} --ip <device-ip> [parameters]")
    print(f"  python3 glowup.py play {effect_name} "
          f"--config <file> --group <name> [parameters]")
    print(f"  python3 glowup.py play {effect_name} "
          f"--ip <device-ip> --sim-only [parameters]")
    print(f"  python3 glowup.py play {effect_name} "
          f"--zones 36 --zpb 3 [parameters]")
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
        help="Target device IP address or hostname",
    )
    p_play.add_argument(
        "--config", default=None,
        help="Path to config file containing device groups",
    )
    p_play.add_argument(
        "--group", default=None,
        help="Device group name (requires --config)",
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
    global _quiet

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

    _quiet = getattr(args, "quiet", False)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Print the startup banner (suppressed by -q/--quiet).
    _print(_BANNER)

    # Dispatch table -- maps subcommand names to handler functions
    commands: Dict[str, Callable[[argparse.Namespace], None]] = {
        "discover": cmd_discover,
        "effects": cmd_effects,
        "identify": cmd_identify,
        "monitor": cmd_monitor,
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
    main()
