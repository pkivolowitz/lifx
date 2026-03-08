#!/usr/bin/env python3
"""LIFX Effect Engine -- command-line interface.

Usage::

    python3 glowup.py discover                    # find all LIFX devices
    python3 glowup.py effects                     # list available effects
    python3 glowup.py identify --ip <device-ip>   # pulse a device to locate it
    python3 glowup.py play cylon --ip <device-ip>    # run an effect on one device
    python3 glowup.py play cylon --config conf.json --group office  # virtual multizone

All effect parameters are auto-generated from each effect's :class:`Param`
declarations.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.5"

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

# ---------------------------------------------------------------------------
# Named constants -- no magic numbers
# ---------------------------------------------------------------------------

DEFAULT_DISCOVERY_TIMEOUT: float = 3.0
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


def _build_polychrome_map(dev: Any) -> list[bool]:
    """Build a per-zone list indicating color vs. monochrome capability.

    For a :class:`VirtualMultizoneDevice`, each zone inherits the
    polychrome status of its underlying physical device.  For a plain
    :class:`LifxDevice`, all zones share the device's status.

    Args:
        dev: A device or virtual multizone device.

    Returns:
        A list of booleans, one per zone.  ``True`` = color,
        ``False`` = monochrome (simulator renders in grayscale).
    """
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

    if not has_ip and not has_group:
        _print(
            "ERROR: Specify either --ip or both --config and --group.",
            file=sys.stderr,
        )
        sys.exit(1)

    if has_ip and has_group:
        _print(
            "ERROR: --ip and --config/--group are mutually exclusive.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Connect to device(s) ------------------------------------------------
    if has_group:
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

    # --- Ensure device is powered on before sending colors --------------------
    dev.set_power(on=True, duration_ms=0)

    # --- Optional simulator window --------------------------------------------
    sim = None
    if getattr(args, "sim", False):
        from simulator import create_simulator
        poly_map: list[bool] = _build_polychrome_map(dev)
        sim = create_simulator(dev.zone_count or 1, effect_name,
                               polychrome_map=poly_map)

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
    ctrl.stop(fade_ms=DEFAULT_FADE_MS)
    dev.set_power(on=False, duration_ms=DEFAULT_FADE_MS)
    dev.close()
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

    # -- play ------------------------------------------------------------------
    p_play = sub.add_parser("play", help="Run an effect on a device")
    p_play.add_argument(
        "effect", help="Effect name (use 'effects' command to list)",
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

    # Auto-add every effect's Param declarations as CLI flags.
    # A ``seen`` set prevents duplicate flags when multiple effects
    # share a parameter name (e.g. "speed").
    seen: set = set()
    for _effect_name, effect_cls in get_registry().items():
        for pname, pdef in effect_cls.get_param_defs().items():
            if pname in seen:
                continue
            seen.add(pname)

            kwargs: Dict[str, Any] = {
                "default": None,
                "help": f"{pdef.description} (default: {pdef.default})",
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

            p_play.add_argument(f"--{pname}", **kwargs)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse arguments and dispatch to the appropriate subcommand handler.

    If no subcommand is given, prints help and exits cleanly.
    Prints a copyright/license banner on startup unless ``-q``/``--quiet``
    is given.
    """
    global _quiet

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
