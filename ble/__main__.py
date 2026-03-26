"""Entry point for ``python3 -m ble``.

Dispatches to sub-commands:

    python3 -m ble discover       — Scan for HAP-BLE accessories
    python3 -m ble pair <label>   — Pair with an accessory
    python3 -m ble sensor         — Run the sensor daemon
    python3 -m ble signal         — Passive RSSI signal meter

Each sub-command can also be run directly:

    python3 -m ble.sensor         — Same as ``python3 -m ble sensor``
    python3 -m ble.signal_meter   — Same as ``python3 -m ble signal``
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "0.1"

import argparse
import asyncio
import logging
import sys

logger: logging.Logger = logging.getLogger("glowup.ble")


def cmd_discover(args: argparse.Namespace) -> None:
    """Scan for nearby HAP-BLE accessories and print results."""
    from .scanner import discover_hap_devices

    async def _run() -> None:
        devices = await discover_hap_devices(timeout=args.timeout)
        if not devices:
            print("No HomeKit BLE accessories found.")
            return

        print(f"\nFound {len(devices)} HomeKit BLE accessory(ies):\n")
        for dev in devices:
            paired_str: str = "paired" if not dev.pairing_available else "UNPAIRED"
            print(f"  Address:    {dev.address}")
            print(f"  Name:       {dev.name or '(unnamed)'}")
            print(f"  Category:   {dev.category_name}")
            print(f"  RSSI:       {dev.rssi} dBm")
            print(f"  GSN:        {dev.state_number}")
            print(f"  Device ID:  {dev.device_id.hex(':')}")
            print(f"  Status:     {paired_str}")
            print()

    asyncio.run(_run())


def cmd_pair(args: argparse.Namespace) -> None:
    """Pair with a BLE accessory using its setup code."""
    from .registry import BleRegistry
    from .scanner import connect_and_wrap
    from .hap_session import HapSession

    async def _run() -> None:
        registry = BleRegistry(args.registry)
        device = registry.get_device(args.label)

        if device is None:
            print(f"Error: device '{args.label}' not in registry.")
            print("Add it first with the address and setup code.")
            sys.exit(1)

        if device.paired:
            print(f"Device '{args.label}' is already paired.")
            if not args.force:
                print("Use --force to re-pair.")
                sys.exit(1)

        if not device.address:
            print(f"Error: device '{args.label}' has no BLE address.")
            print("Run 'python3 -m ble discover' to find the address.")
            sys.exit(1)

        setup_code: str = args.code or device.setup_code
        if not setup_code:
            print(f"Error: no setup code for '{args.label}'.")
            print("Provide --code XXX-XX-XXX or set it in the registry.")
            sys.exit(1)

        print(f"Connecting to {device.address}...")
        gatt = await connect_and_wrap(device.address)

        print("Starting pair-setup...")
        session = HapSession(gatt)
        keys = await session.pair_setup(setup_code.encode("utf-8"))

        registry.mark_paired(args.label, keys)
        print(f"Pairing complete! Device '{args.label}' is now paired.")
        print(f"Keys saved to {args.registry}")

        await gatt.disconnect()

    asyncio.run(_run())


def cmd_sensor(args: argparse.Namespace) -> None:
    """Run the BLE sensor daemon."""
    # Delegate to the sensor module's CLI.
    sys.argv = ["ble.sensor"] + sys.argv[2:]
    from .sensor import main
    main()


def cmd_signal(args: argparse.Namespace) -> None:
    """Run the passive BLE signal meter."""
    # Delegate to the signal_meter module's CLI.
    sys.argv = ["ble.signal_meter"] + sys.argv[2:]
    from .signal_meter import main
    main()


def main() -> None:
    """Main CLI dispatcher."""
    parser = argparse.ArgumentParser(
        prog="python3 -m ble",
        description="GlowUp BLE tools — discover, pair, and monitor "
        "HomeKit BLE accessories.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    subparsers = parser.add_subparsers(dest="command", help="Sub-command")

    # --- discover ---
    p_discover = subparsers.add_parser(
        "discover",
        help="Scan for nearby HAP-BLE accessories",
    )
    p_discover.add_argument(
        "--timeout", "-t",
        type=float,
        default=10.0,
        help="Scan duration in seconds (default: 10)",
    )
    p_discover.set_defaults(func=cmd_discover)

    # --- pair ---
    p_pair = subparsers.add_parser(
        "pair",
        help="Pair with a BLE accessory",
    )
    p_pair.add_argument(
        "label",
        help="Device label from the registry",
    )
    p_pair.add_argument(
        "--code",
        help="Setup code (XXX-XX-XXX) — overrides registry value",
    )
    p_pair.add_argument(
        "--registry",
        default="ble_pairing.json",
        help="Path to ble_pairing.json",
    )
    p_pair.add_argument(
        "--force",
        action="store_true",
        help="Re-pair even if already paired",
    )
    p_pair.set_defaults(func=cmd_pair)

    # --- sensor ---
    p_sensor = subparsers.add_parser(
        "sensor",
        help="Run the BLE sensor daemon",
    )
    p_sensor.set_defaults(func=cmd_sensor)

    # --- signal ---
    p_signal = subparsers.add_parser(
        "signal",
        help="Passive RSSI signal meter (safe alongside daemon)",
    )
    p_signal.set_defaults(func=cmd_signal)

    # parse_known_args so delegating subcommands (sensor, signal)
    # can forward their own flags to the downstream CLI.
    args, _remaining = parser.parse_known_args()

    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
