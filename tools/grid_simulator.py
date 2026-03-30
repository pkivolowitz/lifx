#!/usr/bin/env python3
"""Grid simulator — terminal preview of effects on virtual 2D device groups.

Renders effects onto a sparse 2D grid of virtual devices in the terminal
using 24-bit ANSI truecolor.  Each cell represents a physical device
(single-zone bulb or multizone strip) positioned in a grid layout.

The grid is defined by a JSON configuration file specifying dimensions,
a shared member template, and sparse cell-to-label mapping.  All devices
in a grid are homogeneous — same product, same zone count.

Effects receive the full grid dimensions and produce colors for every
position.  The renderer displays filled cells as colored blocks and
skips empty cells (dim dots), so effects can compute on a complete
rectangular grid without worrying about sparsity.

For single-zone grids (flush downlights, bulbs), each cell is one device
rendered as a colored rectangle.  For multizone grids (strips), each
device defines a row or partial row — zones are rendered contiguously
within a device, with visual gaps between devices.

Config format::

    {
        "name": "Kitchen Ceiling",
        "dimensions": [4, 3],
        "member": {
            "product": "SuperColour 800 Flush Downlight",
            "zones": 1
        },
        "cells": {
            "0,0": "DL NW",
            "1,0": "DL NC",
            "3,0": "DL NE",
            "0,1": "DL CW",
            "3,1": "DL CE",
            "0,2": "DL SW",
            "1,2": "DL SC",
            "3,2": "DL SE"
        }
    }

- ``dimensions``: [columns, rows] of the grid (in zone units).
- ``member``: shared device template (all devices are this type).
  ``zones`` is 1 for single-zone bulbs, N for multizone strips.
- ``cells``: sparse map of ``"col,row"`` → device label.
  For multizone, ``col`` is the starting column; the device extends
  rightward for ``member.zones`` columns.

Usage::

    python3 tools/grid_simulator.py config.json plasma2d
    python3 tools/grid_simulator.py config.json rainbow --fps 30
    python3 tools/grid_simulator.py config.json --info
    python3 tools/grid_simulator.py --list

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Project imports — add project root to sys.path.
# ---------------------------------------------------------------------------

_PROJECT_ROOT: str = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from effects import HSBK, get_effect_names
from emitters import Emitter, EmitterCapabilities
from emitters.screen import (
    _hsbk_to_rgb,
    ZONE_CHAR,
    CSI,
    RESET,
    CLEAR_SCREEN,
    HIDE_CURSOR,
    SHOW_CURSOR,
)
from engine import Controller

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default animation frame rate.
DEFAULT_FPS: int = 20

# Visual gap between device slots (characters).
CELL_GAP_CHARS: int = 1

# Border consumes one character on each side (left + right or top + bottom).
BORDER_TOTAL_CHARS: int = 2

# Minimum characters per zone (horizontal).
MIN_CHARS_PER_ZONE: int = 1

# Minimum cell height in character rows.
MIN_CELL_HEIGHT_CHARS: int = 1

# ANSI color for empty (unfilled) grid cells — dim gray.
EMPTY_CELL_RGB: tuple[int, int, int] = (40, 40, 40)

# Character used to render empty grid cells.
EMPTY_CELL_CHAR: str = "\u00b7"  # middle dot

# FPS averaging window in seconds.
FPS_WINDOW_S: float = 1.0

# Send time exponential moving average smoothing factor.
SEND_TIME_EMA_ALPHA: float = 0.2

# Frame type identifier.
FRAME_TYPE_STRIP: str = "strip"

# Box-drawing characters (Unicode).
BOX_TL: str = "\u250c"
BOX_TR: str = "\u2510"
BOX_BL: str = "\u2514"
BOX_BR: str = "\u2518"
BOX_H: str = "\u2500"
BOX_V: str = "\u2502"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _move(row: int, col: int) -> str:
    """Return ANSI cursor-position sequence (1-based row/col).

    Args:
        row: 1-based row number.
        col: 1-based column number.

    Returns:
        ANSI escape sequence to position the cursor.
    """
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
    """Format elapsed seconds as ``MM:SS`` or ``H:MM:SS``.

    Args:
        seconds: Elapsed time in seconds.

    Returns:
        Formatted time string.
    """
    t: int = int(seconds)
    h: int = t // 3600
    m: int = (t % 3600) // 60
    s: int = t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# GridConfig
# ---------------------------------------------------------------------------

class GridConfig:
    """Parsed grid configuration from a JSON file.

    Validates structure, computes filled zone set, and exposes
    computed properties for the effect engine and renderer.

    All devices in a grid are homogeneous — same zone count, same
    capabilities.  The ``member`` section defines the shared template.

    Two layout modes:

    - **Flat** (``member.matrix`` absent): ``dimensions`` are in zone
      units.  Each cell is a single zone (bulb) or horizontal strip.
    - **Matrix** (``member.matrix`` = ``[w, h]``): ``dimensions`` are
      in **cell** units.  Each cell contains a ``w × h`` pixel matrix
      (e.g. 8×8 for LIFX Tiles).  Effects compute at pixel resolution
      (``cell_cols * matrix_w`` × ``cell_rows * matrix_h``).

    Attributes:
        name:             Grid display name.
        cols:             Grid width in zone units (pixel-level for matrix).
        rows:             Grid height in zone units (pixel-level for matrix).
        cell_cols:        Grid width in cell units.
        cell_rows:        Grid height in cell units.
        matrix_w:         Internal pixel width per cell (1 for flat mode).
        matrix_h:         Internal pixel height per cell (1 for flat mode).
        is_matrix:        True if member defines a matrix layout.
        product:          Member device product name.
        zones_per_member: Zones per device (1 for bulbs, N for strips).
        has_color:        Whether devices support color.
        kelvin_range:     (min, max) kelvin range.
        cells:            Mapping of ``(col, row)`` → device label (cell units).
        filled_zones:     Set of zone indices that have a device (pixel-level).
        filled_count:     Number of devices placed in the grid.

    Args:
        path: Path to the JSON configuration file.

    Raises:
        FileNotFoundError: If the config file does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
        ValueError: If required fields are missing or invalid.
    """

    def __init__(self, path_or_dict: Any) -> None:
        """Load and validate a grid configuration.

        Args:
            path_or_dict: Path to a JSON config file, or a raw dict.
        """
        if isinstance(path_or_dict, dict):
            raw: dict[str, Any] = path_or_dict
        else:
            with open(path_or_dict) as f:
                raw = json.load(f)

        self.name: str = raw.get("name", "Untitled Grid")

        # Grid dimensions: [columns, rows] in cell units.
        dims: Any = raw.get("dimensions")
        if not isinstance(dims, list) or len(dims) != 2:
            raise ValueError("'dimensions' must be [columns, rows]")
        self.cell_cols: int = int(dims[0])
        self.cell_rows: int = int(dims[1])
        if self.cell_cols < 1 or self.cell_rows < 1:
            raise ValueError(
                f"Dimensions must be positive: {self.cell_cols}x{self.cell_rows}"
            )

        # Member template — shared device properties.
        member: dict[str, Any] = raw.get("member", {})
        self.product: str = member.get("product", "Generic Bulb")
        self.zones_per_member: int = int(member.get("zones", 1))
        if self.zones_per_member < 1:
            raise ValueError(
                f"member.zones must be >= 1, got {self.zones_per_member}"
            )
        self.has_color: bool = bool(member.get("color", True))
        kr: list[int] = member.get("kelvin_range", [1500, 9000])
        self.kelvin_range: tuple[int, int] = (int(kr[0]), int(kr[1]))

        # Matrix dimensions — internal pixel grid per cell.
        # Absent or [1,1] means flat mode (single-zone bulbs, strips).
        mat: Any = member.get("matrix")
        if mat and isinstance(mat, list) and len(mat) == 2:
            self.matrix_w: int = int(mat[0])
            self.matrix_h: int = int(mat[1])
            self.is_matrix: bool = True
        else:
            self.matrix_w = 1
            self.matrix_h = 1
            self.is_matrix = False

        # Pixel-level dimensions — what effects compute on.
        # Flat mode: cols/rows = cell_cols/cell_rows (1:1).
        # Matrix mode: cols/rows = cell_cols * matrix_w, cell_rows * matrix_h.
        self.cols: int = self.cell_cols * self.matrix_w
        self.rows: int = self.cell_rows * self.matrix_h

        # Parse cell placements: "col,row" → label (in cell units).
        self.cells: dict[tuple[int, int], str] = {}
        raw_cells: dict[str, str] = raw.get("cells", {})
        for key, label in raw_cells.items():
            parts: list[str] = key.split(",")
            if len(parts) != 2:
                raise ValueError(f"Cell key '{key}' must be 'col,row'")
            col: int = int(parts[0].strip())
            row: int = int(parts[1].strip())
            if col < 0 or col >= self.cell_cols:
                raise ValueError(
                    f"Cell column {col} outside grid width {self.cell_cols}"
                )
            if row < 0 or row >= self.cell_rows:
                raise ValueError(
                    f"Cell row {row} outside grid height {self.cell_rows}"
                )
            self.cells[(col, row)] = label

        if not self.cells:
            raise ValueError("Grid has no filled cells")

        # Build the set of filled pixel-level zone indices.
        # For flat mode: one index per cell (or zones_per_member consecutive).
        # For matrix mode: matrix_w * matrix_h indices per occupied cell.
        self.filled_zones: set[int] = set()
        for (cell_col, cell_row) in self.cells:
            if self.is_matrix:
                # Each cell occupies a matrix_w × matrix_h block of pixels.
                px_col: int = cell_col * self.matrix_w
                px_row: int = cell_row * self.matrix_h
                for dy in range(self.matrix_h):
                    for dx in range(self.matrix_w):
                        idx: int = (px_row + dy) * self.cols + (px_col + dx)
                        self.filled_zones.add(idx)
            else:
                for z in range(self.zones_per_member):
                    self.filled_zones.add(
                        cell_row * self.cols + cell_col + z
                    )

        self.filled_count: int = len(self.cells)

    @property
    def total_zones(self) -> int:
        """Total zone count for the full grid (cols * rows)."""
        return self.cols * self.rows


# ---------------------------------------------------------------------------
# GridEmitter
# ---------------------------------------------------------------------------

class GridEmitter(Emitter):
    """Terminal emitter rendering a 2D grid of virtual devices.

    Each zone position in the grid is rendered as a block of colored
    characters.  Filled zones (belonging to a device) show the effect
    color; empty zones show dim dots.  Visual gaps separate device
    slots so individual devices are visually distinct.

    For single-zone grids (``zones_per_member == 1``), every column
    is a device slot with a gap between each.  For multizone grids,
    gaps appear every ``zones_per_member`` columns, grouping zones
    that belong to the same physical strip.

    The grid auto-scales to fill the current terminal.  Enlarging the
    terminal or reducing font size produces larger cells.

    Not registered in the emitter registry — created programmatically.

    Args:
        config:      Parsed :class:`GridConfig`.
        effect_name: Effect name shown in the top border.
        fps:         Target FPS shown in the top border.
    """

    def __init__(
        self,
        config: GridConfig,
        effect_name: str = "",
        fps: int = DEFAULT_FPS,
    ) -> None:
        """Initialize the grid emitter.

        Queries terminal size and computes cell layout.  Raises
        :class:`RuntimeError` if the terminal is too small to render
        the grid.

        Args:
            config:      Parsed :class:`GridConfig`.
            effect_name: Effect name for the top border.
            fps:         Target FPS for the top border.
        """
        super().__init__("grid", {})
        self._config: GridConfig = config
        self._effect_name: str = effect_name
        self._fps_target: int = fps
        self._powered: bool = False

        # Terminal dimensions (fallback for non-TTY contexts).
        try:
            term = os.get_terminal_size()
        except OSError:
            term = os.terminal_size((120, 40))
        # Layout computation.
        # Gaps appear between cells (not between pixels within a cell).
        h_gaps: int = max(0, config.cell_cols - 1) * CELL_GAP_CHARS
        v_gaps: int = max(0, config.cell_rows - 1) * CELL_GAP_CHARS

        avail_w: int = term.columns - BORDER_TOTAL_CHARS
        avail_h: int = term.lines - BORDER_TOTAL_CHARS - 1  # cursor parking

        # Characters per pixel column and rows per pixel row.
        self._chars_per_zone: int = max(
            MIN_CHARS_PER_ZONE,
            (avail_w - h_gaps) // config.cols,
        )
        self._cell_h: int = max(
            MIN_CELL_HEIGHT_CHARS,
            (avail_h - v_gaps) // config.rows,
        )

        # Total frame dimensions (including border).
        self._frame_w: int = (
            config.cols * self._chars_per_zone
            + h_gaps
            + BORDER_TOTAL_CHARS
        )
        self._frame_h: int = (
            config.rows * self._cell_h
            + v_gaps
            + BORDER_TOTAL_CHARS
        )

        # Validate terminal can fit the frame.
        if self._frame_w > term.columns or self._frame_h > term.lines:
            raise RuntimeError(
                f"Terminal too small ({term.columns}x{term.lines}) for "
                f"grid frame ({self._frame_w}x{self._frame_h}).  "
                f"Enlarge terminal or reduce font size."
            )

        # FPS tracking.
        self._start_time: float = 0.0
        self._fps_actual: float = 0.0
        self._fps_times: list[float] = []
        self._send_time_ms: float = 0.0
        self._frame_count: int = 0

    # --- SOE lifecycle -----------------------------------------------------

    def on_open(self) -> None:
        """Clear the screen and draw the grid border frame."""
        self.prepare_for_rendering()

    def on_emit(self, frame: Any, metadata: dict[str, Any]) -> bool:
        """Render a grid frame to the terminal.

        Args:
            frame:    ``list[HSBK]`` with one color per grid zone.
            metadata: Per-frame context dict.

        Returns:
            ``True`` on success.
        """
        if isinstance(frame, list):
            self.send_zones(frame)
            return True
        return False

    def on_close(self) -> None:
        """Restore the terminal if still powered."""
        if self._powered:
            self.power_off()

    def capabilities(self) -> EmitterCapabilities:
        """Declare grid emitter capabilities.

        Returns:
            An :class:`EmitterCapabilities` for this grid emitter.
        """
        return EmitterCapabilities(
            accepted_frame_types=[FRAME_TYPE_STRIP],
            zones=self._config.total_zones,
            width=self._config.cols,   # pixel-level width
            height=self._config.rows,  # pixel-level height
        )

    # --- Engine-facing properties ------------------------------------------

    @property
    def zone_count(self) -> Optional[int]:
        """Total zone count (grid cols * rows)."""
        return self._config.total_zones

    @property
    def is_multizone(self) -> bool:
        """Always ``True`` — the grid is inherently multizone."""
        return True

    @property
    def emitter_id(self) -> str:
        """Identifier string."""
        return "screen:grid"

    @property
    def label(self) -> str:
        """Grid display name."""
        return self._config.name

    @property
    def product_name(self) -> str:
        """Description with grid dimensions and fill count."""
        cfg: GridConfig = self._config
        if cfg.is_matrix:
            return (
                f"Virtual Grid ({cfg.cell_cols}x{cfg.cell_rows} cells, "
                f"{cfg.cols}x{cfg.rows}px, "
                f"{cfg.filled_count} devices)"
            )
        return (
            f"Virtual Grid ({cfg.cols}x{cfg.rows}, "
            f"{cfg.filled_count} devices)"
        )

    # --- Engine-facing frame dispatch --------------------------------------

    def send_zones(
        self,
        colors: list[HSBK],
        duration_ms: int = 0,
        rapid: bool = True,
    ) -> None:
        """Render grid cells to the terminal.

        The flat color list is interpreted row-major: the first
        ``cols`` values are the top row, the next are the second row,
        and so on.  Filled zones render as colored blocks; empty zones
        render as dim dots.  Gaps are inserted at device slot boundaries.

        Args:
            colors:      Flat list of HSBK tuples (cols * rows).
            duration_ms: Ignored (terminal has no transition).
            rapid:       Ignored.
        """
        if not self._powered:
            return

        send_start: float = time.monotonic()

        # Track actual FPS.
        now: float = send_start
        self._fps_times.append(now)
        self._frame_count += 1
        cutoff: float = now - FPS_WINDOW_S
        while self._fps_times and self._fps_times[0] < cutoff:
            self._fps_times.pop(0)
        if len(self._fps_times) >= 2:
            span: float = self._fps_times[-1] - self._fps_times[0]
            if span > 0:
                self._fps_actual = (len(self._fps_times) - 1) / span

        config: GridConfig = self._config
        cpz: int = self._chars_per_zone
        out: list[str] = []
        mw: int = config.matrix_w
        mh: int = config.matrix_h

        er, eg, eb = EMPTY_CELL_RGB
        empty_seq: str = f"{CSI}38;2;{er};{eg};{eb}m"

        for px_row in range(config.rows):
            # Determine which cell row this pixel row belongs to.
            cell_row: int = px_row // mh

            # Screen Y: account for per-cell gaps (not per-pixel).
            sy: int = (
                2  # row 1 is top border, row 2 is first interior row
                + px_row * self._cell_h
                + cell_row * CELL_GAP_CHARS
            )

            for dy in range(self._cell_h):
                out.append(_move(sy + dy, 2))  # column 2 = inside left border

                for px_col in range(config.cols):
                    cell_col: int = px_col // mw

                    # Insert gap at cell boundaries (not pixel boundaries).
                    if px_col > 0 and px_col % mw == 0:
                        out.append(RESET)
                        out.append(" " * CELL_GAP_CHARS)

                    idx: int = px_row * config.cols + px_col
                    if idx in config.filled_zones and idx < len(colors):
                        r, g, b = _hsbk_to_rgb(*colors[idx])
                        out.append(f"{CSI}38;2;{r};{g};{b}m")
                        out.append(ZONE_CHAR * cpz)
                    else:
                        out.append(empty_seq)
                        out.append(EMPTY_CELL_CHAR * cpz)

                out.append(RESET)

        # Update bottom border with profiling stats.
        elapsed: float = now - self._start_time if self._start_time else 0.0
        grid_total: int = config.total_zones
        out.append(_move(self._frame_h, 1))
        out.append(_border_line(BOX_BL, BOX_BR, self._frame_w, [
            f"FPS: {self._fps_actual:.0f}/{self._fps_target}",
            f"Send: {self._send_time_ms:.1f}ms",
            f"Frames: {self._frame_count}",
            _elapsed_str(elapsed),
            f"{config.filled_count}/{grid_total} cells",
        ]))

        # Park cursor below the frame.
        out.append(_move(self._frame_h + 1, 1))
        sys.stdout.write("".join(out))
        sys.stdout.flush()

        # Measure send time (EMA smoothed).
        send_elapsed: float = (time.monotonic() - send_start) * 1000.0
        alpha: float = SEND_TIME_EMA_ALPHA
        self._send_time_ms = (
            alpha * self._send_time_ms
            + (1.0 - alpha) * send_elapsed
        )

    def send_color(
        self,
        hue: int,
        sat: int,
        bri: int,
        kelvin: int,
        duration_ms: int = 0,
    ) -> None:
        """Fill all zones with a single color.

        Args:
            hue:         Hue (0--65535).
            sat:         Saturation (0--65535).
            bri:         Brightness (0--65535).
            kelvin:      Color temperature (ignored for display).
            duration_ms: Ignored.
        """
        total: int = self._config.total_zones
        self.send_zones([(hue, sat, bri, kelvin)] * total)

    # --- Engine-facing lifecycle -------------------------------------------

    def prepare_for_rendering(self) -> None:
        """Clear screen and draw the border frame with grid info."""
        config: GridConfig = self._config
        out: list[str] = [CLEAR_SCREEN, HIDE_CURSOR]

        # Dimension display string.
        if config.is_matrix:
            dim_str: str = (
                f"{config.cell_cols}x{config.cell_rows} cells "
                f"({config.cols}x{config.rows}px)"
            )
        else:
            dim_str = f"{config.cols}x{config.rows}"
            if config.zones_per_member > 1:
                dev_cols: int = config.cols // config.zones_per_member
                dim_str += f" ({dev_cols}dev x{config.zones_per_member}z)"

        # Top border with grid info.
        out.append(_move(1, 1))
        out.append(_border_line(BOX_TL, BOX_TR, self._frame_w, [
            config.name,
            self._effect_name,
            dim_str,
            f"FPS: {self._fps_target}",
        ]))

        # Side borders for every interior row.
        interior_h: int = self._frame_h - BORDER_TOTAL_CHARS
        for r in range(interior_h):
            row_num: int = r + 2
            out.append(_move(row_num, 1))
            out.append(BOX_V)
            out.append(_move(row_num, self._frame_w))
            out.append(BOX_V)

        # Bottom border placeholder.
        out.append(_move(self._frame_h, 1))
        out.append(_border_line(BOX_BL, BOX_BR, self._frame_w, [
            f"FPS: --/{self._fps_target}",
            "Send: --",
            "Frames: 0",
            "00:00",
            "Ctrl+C to quit",
        ]))

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def power_on(self, duration_ms: int = 0) -> None:
        """Enable rendering and start the elapsed timer.

        Args:
            duration_ms: Ignored.
        """
        self._powered = True
        self._start_time = time.monotonic()

    def power_off(self, duration_ms: int = 0) -> None:
        """Disable rendering and restore the terminal.

        Args:
            duration_ms: Ignored.
        """
        self._powered = False
        sys.stdout.write(SHOW_CURSOR + RESET + "\n")
        sys.stdout.flush()

    def close(self) -> None:
        """Restore the terminal if still powered."""
        self.on_close()

    def get_info(self) -> dict[str, Any]:
        """Return grid emitter status information.

        Returns:
            JSON-serializable dict with emitter identity and dimensions.
        """
        cfg: GridConfig = self._config
        return {
            "id": self.emitter_id,
            "label": self.label,
            "product": self.product_name,
            "zones": self.zone_count,
            "grid": f"{cfg.cols}x{cfg.rows}",
            "filled": cfg.filled_count,
            "cell_display": f"{self._chars_per_zone}x{self._cell_h}",
        }


# ---------------------------------------------------------------------------
# Info display
# ---------------------------------------------------------------------------

def _print_info(config: GridConfig) -> None:
    """Print grid config summary and device layout.

    Args:
        config: Parsed :class:`GridConfig` to display.
    """
    print(f"Grid: {config.name}")
    print(f"  Cell grid: {config.cell_cols} x {config.cell_rows}")
    if config.is_matrix:
        print(f"  Matrix per cell: {config.matrix_w} x {config.matrix_h}")
        print(f"  Pixel resolution: {config.cols} x {config.rows}")
    else:
        zpb: int = config.zones_per_member
        if zpb > 1:
            dev_cols: int = config.cell_cols // zpb
            print(f"  Device slots: {dev_cols} x {config.cell_rows}")
    print(f"  Member: {config.product}")
    if not config.is_matrix:
        print(f"    Zones per device: {config.zones_per_member}")
    print(f"    Color: {config.has_color}")
    print(f"    Kelvin: {config.kelvin_range[0]}\u2013{config.kelvin_range[1]}")
    print(f"  Devices placed: {config.filled_count}")
    print(f"  Zone fill: {len(config.filled_zones)}/{config.total_zones}")
    print()

    # Visual grid map (always in cell units).
    print("  Layout:")
    max_label: int = 14
    for row in range(config.cell_rows):
        parts: list[str] = []
        for col in range(config.cell_cols):
            lbl: Optional[str] = config.cells.get((col, row))
            if lbl:
                parts.append(lbl[:max_label].center(max_label))
            else:
                parts.append("--".center(max_label))
        print(f"    {'|'.join(parts)}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_grid_from_server_json(path: str, grid_name: str) -> GridConfig:
    """Extract a named grid definition from a server.json file.

    The server.json ``grids`` section contains grid definitions keyed
    by name.  Each entry has the same structure as a standalone grid
    config file (dimensions, member, cells).

    Args:
        path:      Path to server.json.
        grid_name: Name of the grid to extract.

    Returns:
        A :class:`GridConfig` built from the extracted definition.

    Raises:
        ValueError: If the grid is not found or the file has no grids.
        FileNotFoundError: If the server.json file doesn't exist.
    """
    with open(path) as f:
        server_cfg: dict[str, Any] = json.load(f)

    grids: dict[str, Any] = server_cfg.get("grids", {})
    if not grids:
        raise ValueError(f"No 'grids' section in {path}")

    if grid_name not in grids:
        available: str = ", ".join(sorted(grids.keys()))
        raise ValueError(
            f"Grid '{grid_name}' not found.  Available: {available}"
        )

    grid_def: dict[str, Any] = dict(grids[grid_name])
    # Inject name if not present.
    if "name" not in grid_def:
        grid_def["name"] = grid_name

    return GridConfig(grid_def)


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="Grid simulator \u2014 terminal preview of 2D virtual device groups.",
        epilog=(
            "Press Ctrl+C to stop.  Resize terminal for larger cells.\n"
            "Run with --info to see the grid layout before animating."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "config", nargs="?", default=None,
        help="Path to grid configuration JSON file",
    )
    parser.add_argument(
        "effect", nargs="?", default="plasma2d",
        help="Effect name (default: plasma2d)",
    )
    parser.add_argument(
        "--fps", "-f", type=int, default=DEFAULT_FPS,
        help=f"Target FPS (default: {DEFAULT_FPS})",
    )
    parser.add_argument(
        "--info", "-i", action="store_true",
        help="Print grid config info and exit",
    )
    parser.add_argument(
        "--zpb", type=int, default=1,
        help="Zones per bulb — how many zones the engine groups into "
             "one logical bulb (default: 1, i.e. each zone is its own bulb)",
    )
    parser.add_argument(
        "--list", "-l", action="store_true", dest="list_effects",
        help="List available effects and exit",
    )
    parser.add_argument(
        "--grid", "-g", default=None, metavar="NAME",
        help="Load a named grid from a server.json file instead of a "
             "standalone config.  The positional config arg must point to "
             "the server.json (e.g., /etc/glowup/server.json).",
    )
    return parser.parse_args()


def main() -> int:
    """Entry point for the grid simulator tool.

    Returns:
        Exit code (0 on success, non-zero on error).
    """
    args: argparse.Namespace = _parse_args()

    # List mode — no config needed.
    if args.list_effects:
        names: list[str] = get_effect_names()
        print(f"Available effects ({len(names)}):")
        for name in names:
            print(f"  {name}")
        return 0

    # Everything else requires a config file.
    if not args.config:
        print("Error: config file required.  Use --list for effects.")
        return 1

    # Load and validate config — either standalone JSON or a named grid
    # extracted from a server.json file.
    try:
        if args.grid:
            config = _load_grid_from_server_json(args.config, args.grid)
        else:
            config = GridConfig(args.config)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        print(f"Config error: {exc}")
        return 1

    # Info mode — print config and exit.
    if args.info:
        _print_info(config)
        return 0

    # Validate effect name.
    effect_name: str = args.effect
    available: list[str] = get_effect_names()
    if effect_name not in available:
        print(f"Unknown effect: {effect_name}")
        print("Use --list to see available effects.")
        return 1

    # Create grid emitter.
    try:
        em: GridEmitter = GridEmitter(
            config=config,
            effect_name=effect_name,
            fps=args.fps,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}")
        return 1

    em.power_on()

    # Wire up controller and play the effect.
    ctrl: Controller = Controller([em], fps=args.fps,
                                  zones_per_bulb=args.zpb)
    ctrl.play(
        effect_name,
        width=config.cols,
        height=config.rows,
    )

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
