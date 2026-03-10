#!/usr/bin/env python3
"""LIFX Effect Engine -- command-line interface.

Usage::

    python3 glowup.py discover                    # find all LIFX devices
    python3 glowup.py effects                     # list available effects
    python3 glowup.py identify --ip <device-ip>   # pulse a device to locate it
    python3 glowup.py monitor --ip <device-ip>           # monitor device in real time
    python3 glowup.py play cylon --ip <device-ip>    # run an effect on one device
    python3 glowup.py play cylon --config conf.json --group office  # virtual multizone

All effect parameters are auto-generated from each effect's :class:`Param`
declarations.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.9"

import argparse
import json
import math
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from transport import LifxDevice, discover_devices
from engine import Controller, VirtualMultizoneDevice
from effects import get_registry, get_effect_names, HSBK_MAX, KELVIN_DEFAULT
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

DEFAULT_MONITOR_POLL_HZ: float = 4.0
"""Default polling rate in Hz for monitor mode."""

MIN_MONITOR_POLL_HZ: float = 0.5
"""Minimum polling rate for monitor mode (once every 2 seconds)."""

MAX_MONITOR_POLL_HZ: float = 20.0
"""Maximum polling rate for monitor mode."""

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
# Null device — geometry-only stub for --sim-only mode
# ---------------------------------------------------------------------------

class _NullDevice:
    """Geometry-only device stub used by ``--sim-only``.

    Mirrors the properties that :class:`Engine` and :class:`Controller`
    read from a real device (zone count, label, etc.) but makes every
    write method a silent no-op.  This guarantees that ``--sim-only``
    never sends a single UDP packet to physical hardware after the
    initial query.

    Attributes:
        zone_count:   Number of zones copied from the real device.
        is_multizone: Always ``True`` so the engine uses the
                      ``set_zones`` path (zone-accurate rendering).
        is_polychrome: Always ``True`` (unused for the multizone path).
        label:        Device or group label for display.
        product_name: Human-readable product string.
        product:      Non-``None`` so engine readiness checks pass.
        ip:           Display-only address string.
        mac_str:      Fixed ``"sim-only"`` sentinel.
        group:        Empty string placeholder.
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
        self.zone_count: int = zone_count
        self.is_multizone: bool = True
        self.is_polychrome: bool = True
        self.label: str = label
        self.product_name: str = product_name
        self.product: int = 0       # non-None so engine checks pass
        self.ip: str = ip
        self.mac_str: str = "sim-only"
        self.group: str = ""
        self._pre_poly_map: list[bool] = pre_poly_map

    # All write methods are silent no-ops — no packets reach the lights.

    def set_zones(self, *args: Any, **kwargs: Any) -> None:
        """No-op — sim-only mode never writes to physical devices."""

    def set_color(self, *args: Any, **kwargs: Any) -> None:
        """No-op — sim-only mode never writes to physical devices."""

    def set_power(self, *args: Any, **kwargs: Any) -> None:
        """No-op — sim-only mode never writes to physical devices."""

    def close(self) -> None:
        """No-op — the real socket was closed before this stub was created."""

    def query_all(self) -> None:
        """No-op — geometry was already obtained from the real device."""


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
        _print("No LIFX devices found.")
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


def _build_polychrome_map(dev: Any) -> list[bool]:
    """Build a per-zone list indicating color vs. monochrome capability.

    For a :class:`_NullDevice` (``--sim-only``), uses the pre-computed
    map that was extracted from the real device before it was closed.

    For a :class:`VirtualMultizoneDevice`, each zone inherits the
    polychrome status of its underlying physical device.  For a plain
    :class:`LifxDevice`, all zones share the device's status.

    Args:
        dev: A device, virtual multizone device, or null device stub.

    Returns:
        A list of booleans, one per zone.  ``True`` = color,
        ``False`` = monochrome (simulator renders in grayscale).
    """
    # _NullDevice carries a pre-computed map extracted before real device
    # sockets were closed.
    if hasattr(dev, "_pre_poly_map") and dev._pre_poly_map is not None:
        return dev._pre_poly_map

    # VirtualMultizoneDevice exposes _zone_map with (device, zone_idx) tuples.
    if hasattr(dev, "_zone_map"):
        return [d.is_polychrome for d, _ in dev._zone_map]

    # Single device: all zones share the same polychrome status.
    zones: int = dev.zone_count if dev.zone_count else 1
    poly: bool = bool(getattr(dev, "is_polychrome", True))
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
    # _NullDevice directly with the requested geometry.
    if virtual_zones > 0:
        if has_ip or has_group:
            _print(
                "ERROR: --zones is for device-free mode; "
                "do not combine with --ip or --config/--group.",
                file=sys.stderr,
            )
            sys.exit(1)
        poly_map: list[bool] = [True] * virtual_zones
        dev = _NullDevice(
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
        # Virtual multizone: load group, connect all devices, wrap.
        ips: list[str] = _load_group(args.config, args.group)
        _print(f"Connecting to group '{args.group}' ({len(ips)} devices)...",
              flush=True)
        devices: list[LifxDevice] = _connect_group(ips)
        dev = VirtualMultizoneDevice(devices)
        _print(f"  Virtual multizone: {dev.zone_count} zones", flush=True)
    else:
        # Single device mode.
        _print(f"Connecting to {args.ip}...", flush=True)
        try:
            dev = LifxDevice(args.ip)
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

    # --- Sim-only: extract geometry then close real sockets immediately --------
    # From this point on, if sim_only is active, dev is a _NullDevice and
    # no further packets will be sent to the physical lights.
    # Skip when --zones was used — dev is already a _NullDevice.
    if sim_only and virtual_zones <= 0:
        pre_poly: list[bool] = _build_polychrome_map(dev)
        null_label: str = getattr(dev, "label", None) or "?"
        null_product: str = getattr(dev, "product_name", None) or "?"
        null_ip: str = getattr(dev, "ip", None) or "sim-only"
        null_zones: int = dev.zone_count or 1
        # Close real sockets — the null stub takes over from here.
        dev.close()
        dev = _NullDevice(
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
        dev.close()
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

    # --- Ensure device is powered on before sending colors --------------------
    # Skipped in sim-only mode: _NullDevice.set_power is a no-op, but
    # being explicit avoids confusing log output about powering on.
    if not sim_only:
        dev.set_power(on=True, duration_ms=0)

    # --- Optional simulator window (--sim or --sim-only) ----------------------
    sim = None
    if getattr(args, "sim", False) or sim_only:
        from simulator import create_simulator
        poly_map: list[bool] = _build_polychrome_map(dev)
        zpb: int = getattr(args, "zpb", 1)
        zoom_val: int = getattr(args, "zoom", 1)
        sim = create_simulator(dev.zone_count or 1, effect_name,
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
            dev.close()
            sys.exit(1)

    frame_cb = sim.update if sim is not None else None

    # --- Start the render engine ----------------------------------------------
    ctrl: Controller = Controller([dev], fps=args.fps,
                                  frame_callback=frame_cb)
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
    # In sim-only mode dev is a _NullDevice; skip the fade and power-off
    # so the intent is clear even though the no-ops would be harmless.
    if sim_only:
        ctrl.stop(fade_ms=0)
    else:
        ctrl.stop(fade_ms=DEFAULT_FADE_MS)
        dev.set_power(on=False, duration_ms=DEFAULT_FADE_MS)
    dev.close()
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
        "--zpb", type=int, default=1,
        help="Zones per bulb for the simulator display "
             "(3 for LIFX string lights, default: 1)",
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
        "--fps", type=int, default=DEFAULT_FPS,
        help=f"Frames per second (default: {DEFAULT_FPS})",
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
        "--zpb", type=int, default=1,
        help="Zones per bulb for the simulator display "
             "(3 for LIFX string lights, default: 1)",
    )
    p_play.add_argument(
        "--zoom", type=int, default=1,
        help="Simulator zoom factor 1-10 (nearest-neighbor scaling, default: 1)",
    )
    p_play.add_argument(
        "--lerp", type=str, default="lab",
        choices=["lab", "hsb"],
        help="Color interpolation method: lab (perceptually uniform, "
             "heavier CPU) or hsb (cheap, default: lab)",
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

    # --- Intercept "play <effect> --help" before argparse consumes -h ---------
    # sys.argv[1] == "play", sys.argv[2] is a non-flag token (the effect name),
    # and -h or --help appears anywhere in the remaining arguments.
    argv = sys.argv[1:]
    if (
        len(argv) >= 2
        and argv[0] == "play"
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
