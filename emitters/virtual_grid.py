"""Virtual grid emitter — wraps N emitters as a 2D spatial pixel canvas.

Unlike :class:`VirtualMultizoneEmitter` which concatenates zones linearly,
the grid emitter arranges members on a 2D cell grid.  Each cell occupies
a position in the grid and contributes its internal pixel matrix (e.g.,
8×8 for LIFX Tiles) or zone strip (e.g., 36×1 for a string light
"scanline").  The total logical canvas is ``cell_cols * member_w`` by
``cell_rows * member_h`` pixels.

Grids may be **sparse** — not every cell position needs a device.  Empty
cells are skipped during fan-out: the effect renders the full canvas but
only occupied cells receive data.

All members must have **identical geometry** (same internal pixel
dimensions).  The server enforces homogeneity at construction time.

Not registered in the emitter registry — created programmatically by
the server for multi-device grids.

Example: a 3×3 grid of 8×8 Tiles with 4 cells occupied produces a
24×24 logical canvas (576 pixels).  Effects compute all 576 pixels;
fan-out routes each cell's 64-pixel slice to the correct device.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import logging
import time
from typing import Any, Optional

from effects import HSBK, KELVIN_DEFAULT
from emitters import Emitter, EmitterCapabilities
from transport import SendMode, broadcast_wake

# Module logger.
logger: logging.Logger = logging.getLogger("glowup.emitters.virtual_grid")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Log fan-out timing stats every N frames.
FANOUT_STATS_INTERVAL: int = 500

# Warn if a single fan-out exceeds this threshold (seconds).
FANOUT_WARN_THRESHOLD_S: float = 0.010  # 10 ms

# Frame type identifier.
_FRAME_TYPE_MATRIX: str = "matrix"

# Grid prefix for emitter IDs.
GRID_PREFIX: str = "grid:"


class VirtualGridEmitter(Emitter):
    """Wrap N emitters as a 2D spatial grid.

    Members are placed at ``(col, row)`` positions in a cell grid.  Each
    member provides ``member_w × member_h`` pixels.  The total logical
    canvas is ``cell_cols * member_w × cell_rows * member_h``.

    Effects render the full canvas in row-major order.  :meth:`send_tile_zones`
    extracts each occupied cell's pixel slice and routes it to the correct
    member emitter.

    Not registered in the emitter registry (``emitter_type`` is ``None``).
    Created programmatically by the server for multi-device grids.

    Args:
        cell_emitters: Dict mapping ``(col, row)`` cell positions to
                       :class:`Emitter` instances.
        cell_cols:     Grid width in cell units.
        cell_rows:     Grid height in cell units.
        member_w:      Internal pixel width per member device.
        member_h:      Internal pixel height per member device.
        name:          Grid name for display and identification.
        owns_emitters: If ``True`` (default), :meth:`close` closes all
                       member emitters.
    """

    # Not registered — emitter_type stays None from the base class.

    def __init__(
        self,
        cell_emitters: dict[tuple[int, int], Emitter],
        cell_cols: int,
        cell_rows: int,
        member_w: int,
        member_h: int,
        name: str = "",
        owns_emitters: bool = True,
    ) -> None:
        """Initialize with a dict of spatially placed member emitters.

        Args:
            cell_emitters: Mapping of ``(col, row)`` → :class:`Emitter`.
            cell_cols:     Number of cell columns in the grid.
            cell_rows:     Number of cell rows in the grid.
            member_w:      Pixel width per member (e.g., 8 for Tiles).
            member_h:      Pixel height per member (e.g., 8 for Tiles).
            name:          Grid display name.
            owns_emitters: Whether :meth:`close` closes members.

        Raises:
            ValueError: If *cell_emitters* is empty or dimensions invalid.
        """
        if not cell_emitters:
            raise ValueError(
                "VirtualGridEmitter requires at least one emitter."
            )
        if cell_cols < 1 or cell_rows < 1:
            raise ValueError(
                f"Grid dimensions must be positive: {cell_cols}×{cell_rows}"
            )
        if member_w < 1 or member_h < 1:
            raise ValueError(
                f"Member dimensions must be positive: {member_w}×{member_h}"
            )

        super().__init__(name or "grid", {})

        self._cell_emitters: dict[tuple[int, int], Emitter] = dict(
            cell_emitters
        )
        self._cell_cols: int = cell_cols
        self._cell_rows: int = cell_rows
        self._member_w: int = member_w
        self._member_h: int = member_h
        self._name: str = name
        self._owns_emitters: bool = owns_emitters

        # Derived pixel-level dimensions.
        self._total_w: int = cell_cols * member_w
        self._total_h: int = cell_rows * member_h
        self._total_zones: int = self._total_w * self._total_h

        # Unique member emitters (for power/lifecycle operations).
        self._unique_emitters: list[Emitter] = list(
            {id(em): em for em in cell_emitters.values()}.values()
        )

        # Member pixel count for pre-allocating extraction buffers.
        self._member_pixels: int = member_w * member_h

        # Fan-out timing statistics.
        self._fanout_count: int = 0
        self._fanout_sum_s: float = 0.0
        self._fanout_min_s: float = float("inf")
        self._fanout_max_s: float = 0.0
        self._fanout_warns: int = 0
        self._fanout_last_t: float = 0.0
        self._interval_sum_s: float = 0.0
        self._interval_min_s: float = float("inf")
        self._interval_max_s: float = 0.0

        logger.info(
            "Grid '%s' — %d cell(s) in %d×%d grid, "
            "member %d×%d px, canvas %d×%d (%d px)",
            name, len(cell_emitters),
            cell_cols, cell_rows,
            member_w, member_h,
            self._total_w, self._total_h, self._total_zones,
        )

    # ------------------------------------------------------------------
    # Emitter interface
    # ------------------------------------------------------------------

    @property
    def zone_count(self) -> Optional[int]:
        """Total pixel count of the logical canvas."""
        return self._total_zones

    @property
    def is_multizone(self) -> bool:
        """Grids are always multi-pixel."""
        return True

    @property
    def is_matrix(self) -> bool:
        """Grids are 2D matrix surfaces."""
        return True

    @property
    def matrix_width(self) -> int:
        """Pixel-level width of the logical canvas."""
        return self._total_w

    @property
    def matrix_height(self) -> int:
        """Pixel-level height of the logical canvas."""
        return self._total_h

    @property
    def emitter_id(self) -> str:
        """Unique identifier: ``grid:Name``."""
        if self._name:
            return f"{GRID_PREFIX}{self._name}"
        return f"grid({len(self._cell_emitters)})"

    @property
    def label(self) -> str:
        """Human-readable display name."""
        return self._name or f"Grid ({len(self._cell_emitters)} cells)"

    @property
    def product_name(self) -> str:
        """Synthetic product description."""
        return (
            f"{self._total_w}×{self._total_h} virtual grid "
            f"({len(self._cell_emitters)} device(s))"
        )

    def capabilities(self) -> EmitterCapabilities:
        """Declare accepted frame types and topology."""
        return EmitterCapabilities(
            accepted_frame_types=[_FRAME_TYPE_MATRIX],
            zones=self._total_zones,
            width=self._total_w,
            height=self._total_h,
            variable_topology=False,
        )

    # ------------------------------------------------------------------
    # Frame dispatch — 2D pixel routing
    # ------------------------------------------------------------------

    def send_tile_zones(
        self,
        colors: list[HSBK],
        duration_ms: int = 0,
    ) -> None:
        """Route pixel data from the full canvas to member devices.

        Extracts each occupied cell's rectangular pixel region from the
        row-major ``colors`` list and dispatches it to the correct
        member emitter.  Empty cells are skipped — no data is sent for
        pixels that have no physical device.

        Args:
            colors:      Full canvas colors in row-major order
                         (length = ``total_w * total_h``).
            duration_ms: LIFX transition duration in milliseconds.
        """
        t_start: float = time.monotonic()

        mw: int = self._member_w
        mh: int = self._member_h
        tw: int = self._total_w

        for (cell_col, cell_row), em in self._cell_emitters.items():
            # Extract this cell's pixel rectangle from the full canvas.
            cell_colors: list[HSBK] = []
            base_col: int = cell_col * mw
            base_row: int = cell_row * mh

            for py in range(mh):
                row_start: int = (base_row + py) * tw + base_col
                cell_colors.extend(colors[row_start : row_start + mw])

            # Dispatch to member — use tile protocol for matrix devices,
            # zone protocol for strips.
            if hasattr(em, 'is_matrix') and em.is_matrix:
                em.send_tile_zones(cell_colors, duration_ms=duration_ms)
            elif em.is_multizone:
                em.send_zones(
                    cell_colors, duration_ms=duration_ms,
                    mode=SendMode.IMMEDIATE,
                )
            else:
                # Single-zone fallback (unlikely for grids, but safe).
                if cell_colors:
                    h, s, b, k = cell_colors[0]
                    em.send_color(h, s, b, k, duration_ms=duration_ms)

        # Update fan-out statistics.
        self._record_fanout(t_start)

    def send_zones(
        self,
        colors: list[HSBK],
        duration_ms: int = 0,
        mode: Optional[SendMode] = None,
    ) -> None:
        """Delegate to :meth:`send_tile_zones`.

        The engine may call ``send_zones`` if the emitter reports as
        multizone.  Route through the 2D pixel dispatch.

        Args:
            colors:      Full canvas colors in row-major order.
            duration_ms: LIFX transition duration.
            mode:        Ignored (grid always uses IMMEDIATE).
        """
        self.send_tile_zones(colors, duration_ms=duration_ms)

    def send_color(
        self,
        hue: int, sat: int, bri: int, kelvin: int,
        duration_ms: int = 0,
    ) -> None:
        """Set all member devices to a single color.

        Args:
            hue:         Hue (0-65535).
            sat:         Saturation (0-65535).
            bri:         Brightness (0-65535).
            kelvin:      Color temperature.
            duration_ms: Transition duration.
        """
        for em in self._unique_emitters:
            em.send_color(hue, sat, bri, kelvin, duration_ms=duration_ms)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_open(self) -> None:
        """Prepare all member emitters for rendering."""
        self.prepare_for_rendering()

    def on_emit(self, frame: Any, metadata: Optional[dict] = None) -> None:
        """Process one frame through the grid routing pipeline.

        Args:
            frame:    List of HSBK tuples (full canvas, row-major).
            metadata: Optional frame metadata (unused).
        """
        if isinstance(frame, list):
            self.send_tile_zones(frame)

    def on_close(self) -> None:
        """Close all member emitters if owned."""
        if self._owns_emitters:
            for em in self._unique_emitters:
                try:
                    em.close()
                except Exception:
                    pass

    def prepare_for_rendering(self) -> None:
        """Wake all member devices and prepare for rendering.

        Issues a single broadcast wake, then prepares each member
        individually with ``skip_wake=True`` to avoid redundant wakes.
        """
        broadcast_wake()
        for em in self._unique_emitters:
            try:
                em.prepare_for_rendering(skip_wake=True)
            except TypeError:
                # Emitter doesn't accept skip_wake.
                em.prepare_for_rendering()

    def power_on(self, duration_ms: int = 0) -> None:
        """Power on all member devices.

        Args:
            duration_ms: Transition duration.
        """
        for em in self._unique_emitters:
            em.power_on(duration_ms=duration_ms)

    def power_off(self, duration_ms: int = 0) -> None:
        """Power off all member devices.

        Args:
            duration_ms: Transition duration.
        """
        for em in self._unique_emitters:
            em.power_off(duration_ms=duration_ms)

    def close(self) -> None:
        """Release resources."""
        self.on_close()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_emitter_list(self) -> list[Emitter]:
        """Return a defensive copy of the unique member emitters.

        Returns:
            List of member :class:`Emitter` instances.
        """
        return list(self._unique_emitters)

    def get_info(self) -> dict[str, Any]:
        """Return a JSON-serializable status dict.

        Returns:
            Dict with grid identity, dimensions, and member info.
        """
        return {
            "id": self.emitter_id,
            "label": self.label,
            "product": self.product_name,
            "zones": self._total_zones,
            "width": self._total_w,
            "height": self._total_h,
            "cell_cols": self._cell_cols,
            "cell_rows": self._cell_rows,
            "member_w": self._member_w,
            "member_h": self._member_h,
            "is_grid": True,
            "is_matrix": True,
            "members": [
                {
                    "cell": f"{col},{row}",
                    "id": em.emitter_id,
                    "label": getattr(em, 'label', ''),
                }
                for (col, row), em in self._cell_emitters.items()
            ],
        }

    # ------------------------------------------------------------------
    # Fan-out timing
    # ------------------------------------------------------------------

    def _record_fanout(self, t_start: float) -> None:
        """Record fan-out duration and log periodic stats.

        Args:
            t_start: Monotonic timestamp from before the fan-out.
        """
        t_end: float = time.monotonic()
        elapsed: float = t_end - t_start

        self._fanout_count += 1
        self._fanout_sum_s += elapsed
        if elapsed < self._fanout_min_s:
            self._fanout_min_s = elapsed
        if elapsed > self._fanout_max_s:
            self._fanout_max_s = elapsed
        if elapsed > FANOUT_WARN_THRESHOLD_S:
            self._fanout_warns += 1

        # Inter-frame interval.
        if self._fanout_last_t > 0:
            interval: float = t_start - self._fanout_last_t
            self._interval_sum_s += interval
            if interval < self._interval_min_s:
                self._interval_min_s = interval
            if interval > self._interval_max_s:
                self._interval_max_s = interval
        self._fanout_last_t = t_start

        # Periodic log dump.
        if self._fanout_count % FANOUT_STATS_INTERVAL == 0:
            avg_ms: float = (
                self._fanout_sum_s / self._fanout_count * 1000
            )
            int_avg_ms: float = (
                self._interval_sum_s / max(1, self._fanout_count - 1) * 1000
            )
            msg: str = (
                f"{self._name} — {self._fanout_count} frames | "
                f"fanout avg {avg_ms:.2f} ms, "
                f"min {self._fanout_min_s * 1000:.2f} ms, "
                f"max {self._fanout_max_s * 1000:.2f} ms | "
                f"interval avg {int_avg_ms:.1f} ms | "
                f"slow (>{FANOUT_WARN_THRESHOLD_S * 1000:.0f} ms): "
                f"{self._fanout_warns}"
            )
            logger.info(msg)
