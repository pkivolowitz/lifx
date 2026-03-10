"""Live visual simulator for LIFX effects.

Opens a tkinter window displaying colored rectangles — one per zone —
updated in real-time as the engine renders frames.  This lets you
preview effects without physical hardware or watch what the engine is
sending alongside real devices.

The simulator is **optional**.  If tkinter is not available (missing
``_tkinter`` C extension), :func:`create_simulator` prints a note and
returns ``None``.  The rest of the system continues unaffected.

Threading model
---------------
macOS requires all tkinter calls on the main thread.  The engine
renders in a background thread and posts frame data onto a
:class:`queue.Queue`.  The tkinter event loop polls that queue via
``root.after()`` — no cross-thread GUI calls.

Usage::

    from simulator import create_simulator

    sim = create_simulator(zone_count=108, effect_name="cylon")
    if sim is not None:
        # Wire engine's frame_callback to sim.update
        # Then run sim.run() on the main thread
        sim.run()
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.1"

import os
import platform
import queue
import subprocess
import time
from typing import Optional

# ---------------------------------------------------------------------------
# Graceful tkinter import — the entire module is a no-op if unavailable.
# ---------------------------------------------------------------------------

try:
    import tkinter as tk
    _TK_AVAILABLE: bool = True
except ImportError:
    _TK_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HSBK_MAX: int = 65535
"""Maximum value for any HSBK component (unsigned 16-bit)."""

HUE_SEXTANTS: int = 6
"""Number of sextants in the HSB color wheel."""

SIM_ZONE_WIDTH: int = 12
"""Default width in pixels of each zone rectangle."""

SIM_ZONE_HEIGHT: int = 60
"""Height in pixels of each zone rectangle."""

SIM_ZONE_GAP: int = 1
"""Gap in pixels between adjacent zone rectangles."""

SIM_PADDING: int = 10
"""Padding in pixels around the zone strip."""

SIM_HEADER_HEIGHT: int = 40
"""Height in pixels reserved for the header text area."""

SIM_POLL_INTERVAL_MS: int = 50
"""Interval in milliseconds between queue polls (20 Hz)."""

SIM_STOP_CHECK_MS: int = 100
"""Interval in milliseconds between stop-event checks."""

SIM_WINDOW_TITLE: str = "GlowUp Simulator"
"""Default window title."""

SIM_BG_COLOR: str = "#1a1a1a"
"""Window and canvas background color (dark grey)."""

SIM_HEADER_COLOR: str = "#cccccc"
"""Header text color."""

SIM_FPS_SMOOTHING: int = 10
"""Number of frames to average for FPS display."""

SIM_MAX_WINDOW_WIDTH: int = 1600
"""Maximum window width in pixels before zone widths shrink."""

SIM_MIN_ZONE_WIDTH: int = 3
"""Minimum zone width in pixels (avoids sub-pixel rendering)."""

SIM_MIN_WINDOW_WIDTH: int = 360
"""Minimum window width in pixels so the title bar and header text
are always readable, even with very few zones."""

DEFAULT_ZONES_PER_BULB: int = 1
"""Default zones-per-bulb grouping (1 = show every zone)."""

DEFAULT_ZOOM: int = 1
"""Default zoom factor (1 = no scaling)."""

MAX_ZOOM: int = 10
"""Maximum allowed zoom factor."""

# BT.709 luma coefficients — same as effects/__init__.py.
_LUMA_R: float = 0.2126
_LUMA_G: float = 0.7152
_LUMA_B: float = 0.0722


# ---------------------------------------------------------------------------
# HSBK → RGB conversion (display only)
# ---------------------------------------------------------------------------

def hsbk_to_rgb(hue: int, sat: int, bri: int, kelvin: int) -> str:
    """Convert a LIFX HSBK color to a tkinter-compatible hex string.

    Uses the standard HSB-to-RGB algorithm (same as
    ``effects.hsbk_to_luminance`` lines 333-354) but returns an
    ``"#RRGGBB"`` hex string instead of computing BT.709 luma.

    The *kelvin* parameter is accepted for API compatibility but
    ignored — color temperature tinting is not applied.

    Args:
        hue:    LIFX hue (0-65535, mapped to 0-360°).
        sat:    LIFX saturation (0-65535).
        bri:    LIFX brightness (0-65535).
        kelvin: Color temperature (ignored for display).

    Returns:
        A hex color string ``"#RRGGBB"`` suitable for tkinter.
    """
    # Normalize to [0, 1].
    h: float = (hue / HSBK_MAX) * HUE_SEXTANTS  # 0-6 for sextant math
    s: float = sat / HSBK_MAX
    b: float = bri / HSBK_MAX

    # HSB to RGB (standard algorithm).
    c: float = b * s           # chroma
    x: float = c * (1.0 - abs(h % 2.0 - 1.0))  # secondary component
    m: float = b - c           # brightness offset

    sextant: int = int(h) % HUE_SEXTANTS
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

    # Clamp and convert to 8-bit integers.
    ri: int = min(int(r * 255), 255)
    gi: int = min(int(g * 255), 255)
    bi: int = min(int(bl * 255), 255)

    return f"#{ri:02x}{gi:02x}{bi:02x}"


def hsbk_to_gray(hue: int, sat: int, bri: int, kelvin: int) -> str:
    """Convert a LIFX HSBK color to a grayscale hex string via BT.709 luma.

    Mirrors the conversion that monochrome bulbs actually receive:
    HSB→RGB, then BT.709 perceptual luminance (Y = 0.2126R + 0.7152G
    + 0.0722B).  The result is a neutral gray at the computed brightness.

    Args:
        hue:    LIFX hue (0-65535).
        sat:    LIFX saturation (0-65535).
        bri:    LIFX brightness (0-65535).
        kelvin: Color temperature (ignored for display).

    Returns:
        A hex color string ``"#RRGGBB"`` where R == G == B (grayscale).
    """
    # Normalize to [0, 1].
    h: float = (hue / HSBK_MAX) * HUE_SEXTANTS
    s: float = sat / HSBK_MAX
    b: float = bri / HSBK_MAX

    # HSB to RGB (standard algorithm).
    c: float = b * s
    x: float = c * (1.0 - abs(h % 2.0 - 1.0))
    m: float = b - c

    sextant: int = int(h) % HUE_SEXTANTS
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

    # BT.709 perceptual luminance.
    y: float = _LUMA_R * r + _LUMA_G * g + _LUMA_B * bl
    gray: int = min(int(y * 255), 255)

    return f"#{gray:02x}{gray:02x}{gray:02x}"


# ---------------------------------------------------------------------------
# Simulator window
# ---------------------------------------------------------------------------

if _TK_AVAILABLE:

    class SimulatorWindow:
        """Live preview window showing effect output as colored rectangles.

        Each zone is rendered as a filled rectangle on a tkinter canvas.
        The engine thread pushes frame data via :meth:`update`, and the
        tkinter event loop polls the queue to refresh the display.

        Attributes:
            zone_count:  Number of zones being displayed.
            effect_name: Name of the active effect (shown in header).
        """

        def __init__(
            self,
            zone_count: int,
            effect_name: str,
            polychrome_map: Optional[list[bool]] = None,
            zones_per_bulb: int = DEFAULT_ZONES_PER_BULB,
            zoom: int = DEFAULT_ZOOM,
        ) -> None:
            """Create the simulator window.

            Args:
                zone_count:     Number of zones to display.
                effect_name:    Effect name shown in the header.
                polychrome_map: Per-zone list of booleans.  ``True``
                    means the zone is on a color device (render in
                    full RGB); ``False`` means monochrome (render in
                    BT.709 grayscale).  If ``None``, all zones are
                    treated as color.
                zones_per_bulb: Number of zones per physical bulb.
                    When > 1, adjacent zones are grouped into one
                    displayed rectangle using the middle zone's color.
                    LIFX string lights use 3 zones per bulb.
                zoom: Integer scale factor (1-10).  All zone
                    dimensions are multiplied by this value.
                    Nearest-neighbor scaling (sharp pixel edges).
            """
            self.zone_count: int = zone_count
            self.effect_name: str = effect_name
            self._zones_per_bulb: int = max(1, zones_per_bulb)
            self._zoom: int = max(1, min(zoom, MAX_ZOOM))

            # Number of displayed bulbs (rectangles on screen).
            self._bulb_count: int = (
                (zone_count + self._zones_per_bulb - 1)
                // self._zones_per_bulb
            )

            # Build per-bulb polychrome map by sampling the middle zone
            # of each group.
            zone_poly: list[bool] = (
                polychrome_map if polychrome_map is not None
                else [True] * zone_count
            )
            self._polychrome_map: list[bool] = []
            for b in range(self._bulb_count):
                mid: int = b * self._zones_per_bulb + self._zones_per_bulb // 2
                mid = min(mid, zone_count - 1)
                self._polychrome_map.append(zone_poly[mid])

            self._queue: queue.Queue = queue.Queue()
            self._frame_times: list[float] = []

            # --- Apply zoom to dimensions ------------------------------------
            z: int = self._zoom
            zone_width_base: int = SIM_ZONE_WIDTH * z
            zone_height: int = SIM_ZONE_HEIGHT * z
            zone_gap: int = SIM_ZONE_GAP * z
            padding: int = SIM_PADDING * z
            header_height: int = SIM_HEADER_HEIGHT * z
            font_size: int = max(12, 12 * z)
            max_window: int = SIM_MAX_WINDOW_WIDTH * z
            min_zone_w: int = SIM_MIN_ZONE_WIDTH * z
            min_window: int = SIM_MIN_WINDOW_WIDTH * z

            # --- Compute adaptive zone width ---------------------------------
            display_count: int = self._bulb_count
            max_strip: int = max_window - 2 * padding
            zone_w: int = min(
                zone_width_base,
                max(min_zone_w,
                    (max_strip - (display_count - 1) * zone_gap)
                    // display_count),
            )

            strip_width: int = (
                display_count * zone_w + (display_count - 1) * zone_gap
            )
            canvas_width: int = max(
                strip_width + 2 * padding,
                min_window,
            )
            canvas_height: int = (
                header_height + zone_height + 2 * padding
            )

            # --- Build the window --------------------------------------------
            self._root: tk.Tk = tk.Tk()
            self._root.title(SIM_WINDOW_TITLE)
            self._root.configure(bg=SIM_BG_COLOR)
            self._root.resizable(False, False)

            # Raise window to front.  macOS ignores lift()/topmost for
            # non-activated apps, so we use osascript to ask System Events
            # to activate our process.  On first run, macOS will prompt
            # for Accessibility permission — this is normal and required
            # for any app that brings another process's window to front.
            # The permission is granted once and remembered.
            if platform.system() == "Darwin":
                subprocess.Popen(
                    ["osascript", "-e",
                     "tell application \"System Events\" to set "
                     "frontmost of the first process whose unix id is "
                     f"{os.getpid()} to true"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                self._root.lift()
                self._root.attributes("-topmost", True)
                self._root.after_idle(
                    lambda: self._root.attributes("-topmost", False),
                )

            self._canvas: tk.Canvas = tk.Canvas(
                self._root,
                width=canvas_width,
                height=canvas_height,
                bg=SIM_BG_COLOR,
                highlightthickness=0,
            )
            self._canvas.pack()

            # --- Header text -------------------------------------------------
            if self._zones_per_bulb > 1:
                header_text: str = (
                    f"{effect_name}  |  {self._bulb_count} bulbs "
                    f"({zone_count} zones, {self._zones_per_bulb} zpb)"
                )
            else:
                header_text = f"{effect_name}  |  {zone_count} zones"

            self._header_id = self._canvas.create_text(
                padding, padding,
                anchor="nw",
                text=header_text,
                fill=SIM_HEADER_COLOR,
                font=("Menlo", font_size),
            )
            self._fps_id = self._canvas.create_text(
                canvas_width - padding, padding,
                anchor="ne",
                text="-- fps",
                fill=SIM_HEADER_COLOR,
                font=("Menlo", font_size),
            )

            # --- Bulb rectangles (initially black) ---------------------------
            # Center the strip horizontally when the window is wider
            # than the strip (e.g., when SIM_MIN_WINDOW_WIDTH kicks in).
            strip_x_offset: int = (canvas_width - strip_width) // 2
            y_top: int = header_height + padding
            y_bot: int = y_top + zone_height
            self._rects: list[int] = []

            for i in range(display_count):
                x0: int = strip_x_offset + i * (zone_w + zone_gap)
                x1: int = x0 + zone_w
                rect_id: int = self._canvas.create_rectangle(
                    x0, y_top, x1, y_bot,
                    fill="#000000", outline="",
                )
                self._rects.append(rect_id)

            # --- Start polling -----------------------------------------------
            self._root.after(SIM_POLL_INTERVAL_MS, self._poll_queue)

        def update(self, colors: list[tuple[int, int, int, int]]) -> None:
            """Post a frame to the simulator (called from engine thread).

            This method is thread-safe.  It puts the color list onto an
            internal queue and returns immediately.  The tkinter event
            loop picks it up on the next poll cycle.

            Args:
                colors: List of HSBK tuples, one per zone.
            """
            # Non-blocking put; queue is unbounded so this never raises.
            self._queue.put(colors)

        def _poll_queue(self) -> None:
            """Drain the queue and display the most recent frame.

            Scheduled via ``root.after()`` — runs on the main thread.
            Skips stale frames (only the latest matters for display).
            """
            latest: Optional[list] = None

            # Drain all queued frames, keep only the newest.
            try:
                while True:
                    latest = self._queue.get_nowait()
            except queue.Empty:
                pass

            if latest is not None:
                now: float = time.monotonic()
                self._frame_times.append(now)

                # Trim to smoothing window.
                while (len(self._frame_times) > SIM_FPS_SMOOTHING
                       and self._frame_times):
                    self._frame_times.pop(0)

                # Update FPS display.
                if len(self._frame_times) >= 2:
                    span: float = (
                        self._frame_times[-1] - self._frame_times[0]
                    )
                    if span > 0:
                        fps: float = (len(self._frame_times) - 1) / span
                        self._canvas.itemconfig(
                            self._fps_id, text=f"{fps:.0f} fps",
                        )

                # Update bulb rectangles.  When zones_per_bulb > 1,
                # each rectangle shows the middle zone's color from its
                # group.  Monochrome bulbs get BT.709 grayscale.
                zpb: int = self._zones_per_bulb
                for b in range(len(self._rects)):
                    # Pick the middle zone of this bulb's group.
                    mid: int = b * zpb + zpb // 2
                    if mid >= len(latest):
                        break
                    if self._polychrome_map[b]:
                        hex_color: str = hsbk_to_rgb(*latest[mid])
                    else:
                        hex_color = hsbk_to_gray(*latest[mid])
                    self._canvas.itemconfig(self._rects[b], fill=hex_color)

            # Reschedule.
            self._root.after(SIM_POLL_INTERVAL_MS, self._poll_queue)

        def run(self) -> None:
            """Enter the tkinter main loop (blocks the calling thread).

            This must be called from the main thread on macOS.
            """
            self._root.mainloop()

        def stop(self) -> None:
            """Request the tkinter main loop to exit.

            Safe to call from any thread — uses ``root.after()`` to
            schedule the quit on the main thread.
            """
            try:
                self._root.after(0, self._root.quit)
            except Exception:
                # Window may already be destroyed.
                pass


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def create_simulator(
    zone_count: int,
    effect_name: str,
    polychrome_map: Optional[list[bool]] = None,
    zones_per_bulb: int = DEFAULT_ZONES_PER_BULB,
    zoom: int = DEFAULT_ZOOM,
) -> Optional["SimulatorWindow"]:
    """Create a simulator window if tkinter is available.

    If tkinter is not installed, prints a note to stderr and returns
    ``None``.  Callers should check the return value and fall back to
    the non-simulator code path.

    Args:
        zone_count:     Number of zones to display.
        effect_name:    Effect name shown in the header.
        polychrome_map: Per-zone booleans — ``True`` for color zones,
            ``False`` for monochrome zones (rendered in BT.709
            grayscale).  If ``None``, all zones default to color.
        zones_per_bulb: Number of zones per physical bulb.  When > 1,
            adjacent zones are grouped into one displayed rectangle.
            LIFX string lights use 3 zones per bulb.
        zoom: Integer scale factor (1-10).  Multiplies all zone
            dimensions for a larger window with sharp pixel edges
            (nearest-neighbor scaling).

    Returns:
        A :class:`SimulatorWindow` instance, or ``None`` if tkinter
        is unavailable.
    """
    if not _TK_AVAILABLE:
        import sys
        print(
            "Note: tkinter not available — simulator disabled.  "
            "Install with: brew install tcl-tk python-tk@3.10",
            file=sys.stderr,
        )
        return None

    return SimulatorWindow(zone_count, effect_name, polychrome_map,
                           zones_per_bulb, zoom)
