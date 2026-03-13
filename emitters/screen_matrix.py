"""2D terminal matrix emitter — renders HSBK grids as colored blocks.

Interprets a flat ``list[HSBK]`` as a row-major 2D pixel grid and renders
it inside a Unicode box-drawing border.  Each character cell is one pixel.
Status information (effect name, grid dimensions, FPS, elapsed time) is
embedded in the top and bottom border lines.

The available pixel resolution is ``(terminal_cols - 2) x (terminal_rows - 2)``
— the border consumes exactly one row/column on each side.  Increasing the
terminal window size or decreasing font size directly increases resolution.

Uses 24-bit ANSI truecolor escape sequences.  No curses dependency.

Usage::

    from emitters.screen_matrix import ScreenMatrixEmitter
    from engine import Controller

    em = ScreenMatrixEmitter(effect_name="plasma2d")
    ctrl = Controller([em], fps=20)
    ctrl.play("plasma2d", width=em.pixel_width, height=em.pixel_height)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import os
import sys
import time
from typing import Optional

from effects import HSBK
from emitters import Emitter
from emitters.screen import (
    _hsbk_to_rgb,
    ZONE_CHAR, CSI, RESET,
    CLEAR_SCREEN, HIDE_CURSOR, SHOW_CURSOR,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Box-drawing characters (Unicode).
BOX_TL: str = "\u250c"
BOX_TR: str = "\u2510"
BOX_BL: str = "\u2514"
BOX_BR: str = "\u2518"
BOX_H: str = "\u2500"
BOX_V: str = "\u2502"

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

    Spaces always flank each item so text never abuts the fill character.

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
# ScreenMatrixEmitter
# ---------------------------------------------------------------------------

class ScreenMatrixEmitter(Emitter):
    """2D terminal emitter — renders a pixel grid inside a bordered frame.

    The flat ``list[HSBK]`` received from the engine is interpreted as a
    row-major grid of ``pixel_width * pixel_height`` values.  Each HSBK
    value becomes a single colored block character in the terminal.

    The border reserves one row/column on each side, so the pixel grid
    fills ``(terminal_cols - 2) x (terminal_rows - 2)`` by default.
    Enlarging the terminal window or shrinking the font increases resolution.

    Args:
        effect_name:  Effect name shown in the top border.
        fps:          Target FPS shown in the top border.
        pixel_width:  Grid width in characters (default: auto-fit terminal).
        pixel_height: Grid height in characters (default: auto-fit terminal).
    """

    def __init__(
        self,
        effect_name: str = "",
        fps: int = 20,
        pixel_width: Optional[int] = None,
        pixel_height: Optional[int] = None,
    ) -> None:
        """Initialize the matrix emitter.

        Queries terminal size and computes the available pixel grid,
        reserving border rows and columns.

        Args:
            effect_name:  Effect name for the top border.
            fps:          Target FPS for the top border.
            pixel_width:  Override pixel width (default: terminal_cols - 2).
            pixel_height: Override pixel height (default: terminal_rows - 2).
        """
        term_size = os.get_terminal_size()
        self._pixel_width: int = pixel_width or (term_size.columns - 2)
        self._pixel_height: int = pixel_height or (term_size.lines - 2)
        self._effect_name: str = effect_name
        self._fps_target: int = fps
        self._powered: bool = False

        # Display dimensions (border included).
        self._frame_width: int = self._pixel_width + 2
        self._frame_height: int = self._pixel_height + 2

        # FPS tracking.
        self._start_time: float = 0.0
        self._fps_actual: float = 0.0
        self._fps_times: list[float] = []

    # --- Public pixel dimensions ---

    @property
    def pixel_width(self) -> int:
        """Width of the pixel grid in characters."""
        return self._pixel_width

    @property
    def pixel_height(self) -> int:
        """Height of the pixel grid in characters."""
        return self._pixel_height

    # --- Emitter properties ---

    @property
    def zone_count(self) -> Optional[int]:
        """Total pixel count (width * height)."""
        return self._pixel_width * self._pixel_height

    @property
    def is_multizone(self) -> bool:
        """Always True — the grid is inherently multizone."""
        return True

    @property
    def emitter_id(self) -> str:
        """Identifier string."""
        return "screen:matrix"

    @property
    def label(self) -> str:
        """Human-readable display name."""
        return "Matrix Preview"

    @property
    def product_name(self) -> str:
        """Description with grid dimensions."""
        return f"Terminal Matrix ({self._pixel_width}x{self._pixel_height})"

    # --- Frame dispatch ---

    def send_zones(self, colors: list[HSBK], duration_ms: int = 0,
                   rapid: bool = True) -> None:
        """Render a 2D frame inside the bordered display.

        The flat color list is interpreted as row-major: the first
        ``pixel_width`` values are the top row, the next are the second
        row, and so on.

        Args:
            colors:      Flat list of HSBK tuples (width * height).
            duration_ms: Ignored.
            rapid:       Ignored.
        """
        if not self._powered:
            return

        # Track actual FPS.
        now: float = time.monotonic()
        self._fps_times.append(now)
        cutoff: float = now - FPS_WINDOW
        while self._fps_times and self._fps_times[0] < cutoff:
            self._fps_times.pop(0)
        if len(self._fps_times) >= 2:
            span: float = self._fps_times[-1] - self._fps_times[0]
            if span > 0:
                self._fps_actual = (len(self._fps_times) - 1) / span

        w: int = self._pixel_width
        h: int = self._pixel_height
        out: list[str] = []

        # Render each row of the pixel grid.
        for row in range(h):
            # Position cursor: row+2 (row 1 is top border), column 2.
            out.append(_move(row + 2, 2))
            start: int = row * w
            for col in range(w):
                idx: int = start + col
                if idx < len(colors):
                    r, g, b = _hsbk_to_rgb(*colors[idx])
                    out.append(f"{CSI}38;2;{r};{g};{b}m{ZONE_CHAR}")
                else:
                    out.append(" ")
            out.append(RESET)

        # Update bottom border with live stats.
        elapsed: float = now - self._start_time if self._start_time else 0.0
        out.append(_move(self._frame_height, 1))
        out.append(_border_line(BOX_BL, BOX_BR, self._frame_width, [
            f"FPS: {self._fps_actual:.0f}",
            _elapsed_str(elapsed),
            f"{w}x{h} = {w * h} pixels",
            "Ctrl+C to quit",
        ]))

        # Park cursor below the frame.
        out.append(_move(self._frame_height + 1, 1))
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def send_color(self, hue: int, sat: int, bri: int, kelvin: int,
                   duration_ms: int = 0) -> None:
        """Fill the entire grid with a single color."""
        total: int = self._pixel_width * self._pixel_height
        self.send_zones([(hue, sat, bri, kelvin)] * total)

    # --- Lifecycle ---

    def prepare_for_rendering(self) -> None:
        """Clear the screen and draw the border frame."""
        out: list[str] = [CLEAR_SCREEN, HIDE_CURSOR]

        # Top border with effect info.
        out.append(_move(1, 1))
        out.append(_border_line(BOX_TL, BOX_TR, self._frame_width, [
            self._effect_name,
            f"{self._pixel_width}x{self._pixel_height}",
            f"FPS: {self._fps_target}",
        ]))

        # Side borders for each pixel row (content filled by send_zones).
        for row in range(self._pixel_height):
            r: int = row + 2
            out.append(_move(r, 1))
            out.append(BOX_V)
            out.append(_move(r, self._frame_width))
            out.append(BOX_V)

        # Bottom border with placeholder.
        out.append(_move(self._frame_height, 1))
        out.append(_border_line(BOX_BL, BOX_BR, self._frame_width, [
            "FPS: --", "00:00", "Ctrl+C to quit",
        ]))

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def power_on(self, duration_ms: int = 0) -> None:
        """Enable rendering and start the elapsed timer."""
        self._powered = True
        self._start_time = time.monotonic()

    def power_off(self, duration_ms: int = 0) -> None:
        """Disable rendering and restore the terminal."""
        self._powered = False
        sys.stdout.write(SHOW_CURSOR + RESET + "\n")
        sys.stdout.flush()

    def close(self) -> None:
        """Restore the terminal if still powered."""
        if self._powered:
            self.power_off()
