#!/usr/bin/env python3
"""Terminal matrix preview — render 2D effects as a pixel grid.

Renders effects onto a full-terminal 2D pixel grid inside a Unicode
box-drawing border.  Resolution is determined by terminal size — make
the window bigger or reduce font size for more pixels.

Any effect works: 1D effects produce a raster-scan pattern across rows,
while 2D effects (like ``plasma2d``) fill the grid natively.  2D effects
receive ``width`` and ``height`` parameters matching the pixel grid.

Uses 24-bit ANSI truecolor.  No curses dependency.

Usage:
    python3 demo_matrix_emitter.py plasma2d
    python3 demo_matrix_emitter.py plasma2d --fps 30
    python3 demo_matrix_emitter.py aurora
    python3 demo_matrix_emitter.py --list

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import signal
import sys
import threading

from effects import get_effect_names
from emitters.screen_matrix import ScreenMatrixEmitter
from engine import Controller

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default parameters.
DEFAULT_EFFECT: str = "plasma2d"
DEFAULT_FPS: int = 20


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="2D terminal matrix preview for LIFX effects.",
        epilog="Press Ctrl+C to stop.  Resize terminal for more pixels.",
    )
    parser.add_argument(
        "effect", nargs="?", default=DEFAULT_EFFECT,
        help=f"Effect name (default: {DEFAULT_EFFECT})",
    )
    parser.add_argument(
        "--fps", "-f", type=int, default=DEFAULT_FPS,
        help=f"Target FPS (default: {DEFAULT_FPS})",
    )
    parser.add_argument(
        "--list", "-l", action="store_true", dest="list_effects",
        help="List available effects and exit",
    )
    return parser.parse_args()


def main() -> int:
    """Entry point for the 2D matrix preview tool."""
    args: argparse.Namespace = _parse_args()

    # List mode.
    if args.list_effects:
        names: list[str] = get_effect_names()
        print(f"Available effects ({len(names)}):")
        for name in names:
            print(f"  {name}")
        return 0

    # Validate effect name.
    available: list[str] = get_effect_names()
    if args.effect not in available:
        print(f"Unknown effect: {args.effect}")
        print("Use --list to see available effects.")
        return 1

    # Create matrix emitter — auto-fits to terminal size.
    em: ScreenMatrixEmitter = ScreenMatrixEmitter(
        effect_name=args.effect,
        fps=args.fps,
    )
    em.power_on()

    # Pass pixel dimensions to the effect so 2D effects can use them.
    ctrl: Controller = Controller([em], fps=args.fps)
    ctrl.play(args.effect, width=em.pixel_width, height=em.pixel_height)

    # Block until Ctrl+C.
    stop: threading.Event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()

    ctrl.stop(fade_ms=0)
    em.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
