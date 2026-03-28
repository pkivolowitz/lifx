#!/usr/bin/env python3
"""Terminal preview — render LIFX effects in a bordered ANSI display.

The color strip (canvas) renders identically to ScreenEmitter.  This tool
wraps it in a Unicode box-drawing border (the picture frame) with live
status: effect name, zone count, FPS, and elapsed time.

Uses 24-bit ANSI truecolor escape sequences — no curses dependency.
Works with iTerm2, Terminal.app, GNOME Terminal, Windows Terminal, etc.

Usage:
    python3 demo_screen_emitter.py aurora
    python3 demo_screen_emitter.py cylon --zones 50
    python3 demo_screen_emitter.py spin --fps 30
    python3 demo_screen_emitter.py --list

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import os
import signal
import sys
import threading
import time

from effects import HSBK, get_effect_names
from emitters.screen import (
    ScreenEmitter, _hsbk_to_rgb,
    ZONE_CHAR, CHARS_PER_ZONE, CSI, RESET,
    CLEAR_SCREEN, HIDE_CURSOR, SHOW_CURSOR,
)
from engine import Controller

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default parameters.
DEFAULT_EFFECT: str = "aurora"
DEFAULT_FPS: int = 20
DEFAULT_STRIP_HEIGHT: int = 3

# Box-drawing characters (Unicode).
BOX_TL: str = "\u250c"
BOX_TR: str = "\u2510"
BOX_BL: str = "\u2514"
BOX_BR: str = "\u2518"
BOX_H: str = "\u2500"
BOX_V: str = "\u2502"

# Minimum usable terminal width.
MIN_WIDTH: int = 40

# FPS averaging window in seconds.
FPS_WINDOW: float = 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _move(row: int, col: int) -> str:
    """Return ANSI cursor-position sequence (1-based row/col)."""
    return f"{CSI}{row};{col}H"


def _border_line(left: str, right: str, width: int,
                 items: list[str]) -> str:
    """Build a border line with status items embedded.

    Format: ``left`` + ``BOX_H item BOX_H*3 item BOX_H*... `` + ``right``
    Spaces always flank each item so text never abuts the border fill.

    Args:
        left:  Left corner character.
        right: Right corner character.
        width: Total line width including corners.
        items: Status strings to embed in the border.

    Returns:
        The formatted border line.
    """
    inner: int = width - 2
    if not items:
        return left + BOX_H * inner + right

    segments: list[str] = [f" {s} " for s in items]
    content: str = BOX_H + (BOX_H * 3).join(segments)
    pad: int = inner - len(content)
    if pad > 0:
        content += BOX_H * pad
    else:
        content = content[:inner]
    return left + content + right


def _elapsed_str(seconds: float) -> str:
    """Format elapsed seconds as ``MM:SS`` or ``H:MM:SS``."""
    t: int = int(seconds)
    h: int = t // 3600
    m: int = (t % 3600) // 60
    s: int = t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Preview emitter — ScreenEmitter inside a picture frame
# ---------------------------------------------------------------------------

class PreviewEmitter(ScreenEmitter):
    """ScreenEmitter wrapped in a box-drawing border with live status.

    The canvas (colored block strip) is identical to ScreenEmitter.
    The border is the picture frame: top line shows effect name, zone
    count, and target FPS; bottom line shows measured FPS, elapsed time,
    and a quit hint.

    Args:
        zone_count:   Number of zones to display.
        effect_name:  Effect name shown in the top border.
        fps:          Target FPS shown in the top border.
        strip_height: Color strip height in terminal lines (2--5).
    """

    def __init__(
        self,
        zone_count: int,
        effect_name: str,
        fps: int = DEFAULT_FPS,
        strip_height: int = DEFAULT_STRIP_HEIGHT,
    ) -> None:
        """Initialize the bordered preview emitter.

        Auto-clamps zone count to fit the current terminal width.

        Args:
            zone_count:   Desired number of zones.
            effect_name:  Effect name for the top border.
            fps:          Target FPS for the top border.
            strip_height: Strip height in terminal lines.
        """
        # Clamp zones to terminal width.
        term_cols: int = os.get_terminal_size().columns
        display_w: int = min(zone_count * CHARS_PER_ZONE + 4, term_cols)
        max_zones: int = (display_w - 4) // CHARS_PER_ZONE
        actual_zones: int = min(zone_count, max_zones)

        super().__init__(zone_count=actual_zones, label="Preview")

        self._effect_name: str = effect_name
        self._fps_target: int = fps
        self._strip_height: int = strip_height
        self._display_width: int = actual_zones * CHARS_PER_ZONE + 4

        # FPS tracking.
        self._start_time: float = 0.0
        self._fps_actual: float = 0.0
        self._fps_times: list[float] = []

        # Layout rows (1-based ANSI coordinates).
        self._row_top: int = 1
        self._row_strip: int = 3                            # row 2 is padding
        self._row_bot: int = self._row_strip + strip_height + 1  # +1 padding

    # --- Overrides ---

    def prepare_for_rendering(self) -> None:
        """Clear the screen and draw the picture-frame border."""
        w: int = self._display_width
        out: list[str] = [CLEAR_SCREEN, HIDE_CURSOR]

        # Top border with effect info.
        out.append(_move(self._row_top, 1))
        out.append(_border_line(BOX_TL, BOX_TR, w, [
            self._effect_name,
            f"Zones: {self._zone_count}",
            f"FPS: {self._fps_target}",
        ]))

        # Padding and strip rows (initially empty).
        for r in range(self._row_top + 1, self._row_bot):
            out.append(_move(r, 1))
            out.append(BOX_V + " " * (w - 2) + BOX_V)

        # Bottom border with placeholder status.
        out.append(_move(self._row_bot, 1))
        out.append(_border_line(BOX_BL, BOX_BR, w, [
            "FPS: --", "00:00", "Ctrl+C to quit",
        ]))

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def send_zones(self, colors: list[HSBK], duration_ms: int = 0,
                   rapid: bool = True) -> None:
        """Render the color strip inside the border and update status."""
        if not self._powered:
            return

        # Track actual FPS over a sliding window.
        now: float = time.monotonic()
        self._fps_times.append(now)
        cutoff: float = now - FPS_WINDOW
        while self._fps_times and self._fps_times[0] < cutoff:
            self._fps_times.pop(0)
        if len(self._fps_times) >= 2:
            span: float = self._fps_times[-1] - self._fps_times[0]
            if span > 0:
                self._fps_actual = (len(self._fps_times) - 1) / span

        # Build the colored block string — same rendering as ScreenEmitter.
        parts: list[str] = []
        for hsbk in colors[:self._zone_count]:
            r, g, b = _hsbk_to_rgb(*hsbk)
            parts.append(f"{CSI}38;2;{r};{g};{b}m")
            parts.append(ZONE_CHAR * CHARS_PER_ZONE)
        parts.append(RESET)
        strip: str = "".join(parts)
        bordered: str = f"{BOX_V} {strip} {BOX_V}"

        # Write strip rows (same content repeated for thickness).
        out: list[str] = []
        for i in range(self._strip_height):
            out.append(_move(self._row_strip + i, 1))
            out.append(bordered)

        # Update bottom border with live stats.
        elapsed: float = now - self._start_time if self._start_time else 0.0
        out.append(_move(self._row_bot, 1))
        out.append(_border_line(BOX_BL, BOX_BR, self._display_width, [
            f"FPS: {self._fps_actual:.0f}",
            _elapsed_str(elapsed),
            "Ctrl+C to quit",
        ]))

        # Park cursor below the frame.
        out.append(_move(self._row_bot + 1, 1))
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def power_on(self, duration_ms: int = 0) -> None:
        """Enable output and start the elapsed timer."""
        super().power_on(duration_ms)
        self._start_time = time.monotonic()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Terminal preview for LIFX effects.",
        epilog="Press Ctrl+C to stop.",
    )
    parser.add_argument(
        "effect", nargs="?", default=DEFAULT_EFFECT,
        help=f"Effect name (default: {DEFAULT_EFFECT})",
    )
    parser.add_argument(
        "--zones", "-z", type=int, default=None,
        help="Zone count (default: auto-fit to terminal width)",
    )
    parser.add_argument(
        "--fps", "-f", type=int, default=DEFAULT_FPS,
        help=f"Target FPS (default: {DEFAULT_FPS})",
    )
    parser.add_argument(
        "--height", type=int, default=DEFAULT_STRIP_HEIGHT,
        choices=[2, 3, 4, 5],
        help=f"Strip height in lines (default: {DEFAULT_STRIP_HEIGHT})",
    )
    parser.add_argument(
        "--list", "-l", action="store_true", dest="list_effects",
        help="List available effects and exit",
    )
    return parser.parse_args()


def main() -> int:
    """Entry point for the terminal preview tool."""
    args: argparse.Namespace = _parse_args()

    # List mode — print effects and exit.
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

    # Calculate zone count — auto-fit to terminal or use user value.
    term_cols: int = os.get_terminal_size().columns
    max_zones: int = (term_cols - 4) // CHARS_PER_ZONE
    zones: int = min(args.zones, max_zones) if args.zones else max_zones

    if zones < 1:
        print("Terminal too narrow for preview.")
        return 1

    # Create emitter and controller.
    em: PreviewEmitter = PreviewEmitter(
        zone_count=zones,
        effect_name=args.effect,
        fps=args.fps,
        strip_height=args.height,
    )
    em.power_on()

    ctrl: Controller = Controller([em], fps=args.fps)
    ctrl.play(args.effect)

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
