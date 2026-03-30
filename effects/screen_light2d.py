"""2D screen-reactive lighting for matrix/tile grids.

Maps captured video frames directly to a 2D pixel grid — each tile
pixel shows the average color of the corresponding screen region.
This turns a wall of LIFX Tiles into a low-resolution display of
whatever is on TV.

Unlike :class:`ScreenLight` (which extracts 1D edge colors for
ambilight-style backlighting), this effect maps the **full frame**
to a 2D surface.

Uses the same media pipeline as screen_light: ScreenSource captures
frames, VisionExtractor publishes grid signals on the bus, this
effect reads and renders them.

Requires ``numpy`` for frame processing.

Usage::

    # HDHomeRun live TV on a tile grid
    python3 glowup.py play screen_light2d --device grid:Staircase \\
        --video-url http://hdhomerun/auto/v5.1

    # Local screen capture on tiles
    python3 glowup.py play screen_light2d --device grid:Staircase
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

from typing import Optional

from effects import (
    DEVICE_TYPE_MATRIX,
    MediaEffect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Black pixel.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Default grid dimensions — Luna.
DEFAULT_WIDTH: int = 7
DEFAULT_HEIGHT: int = 5

# Temporal smoothing alpha per pixel — prevents boil on noisy sources.
# Lower = heavier smoothing.  0.3 at 20 FPS ≈ 150ms settling time.
SMOOTH_ALPHA: float = 0.3

# Minimum brightness below which a pixel is treated as black.
# Prevents dim color noise from appearing on the tiles.
MIN_BRIGHTNESS: float = 0.02


class ScreenLight2D(MediaEffect):
    """2D screen-reactive lighting — maps full video frames to a tile grid.

    Each pixel in the grid corresponds to a rectangular region of the
    captured screen.  The average color of that region drives the pixel.
    """

    name: str = "screen_light2d"
    description: str = "Full-frame screen content mapped to a 2D tile grid"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    width = Param(DEFAULT_WIDTH, min=1, max=500,
                  description="Grid width in pixels (auto-set from device)")
    height = Param(DEFAULT_HEIGHT, min=1, max=300,
                   description="Grid height in pixels (auto-set from device)")
    source = Param(
        "screen", description="Vision source name (matches screen config)",
    )
    sensitivity = Param(
        1.2, min=0.1, max=5.0,
        description="Brightness multiplier",
    )
    saturation_boost = Param(
        120, min=0, max=200,
        description="Saturation adjustment (100 = natural, 200 = vivid)",
    )
    min_brightness = Param(
        2, min=0, max=50,
        description="Minimum pixel brightness (percent)",
    )
    max_brightness = Param(
        100, min=20, max=100,
        description="Maximum pixel brightness (percent)",
    )
    kelvin = Param(
        KELVIN_DEFAULT, min=1500, max=9000,
        description="Color temperature in Kelvin",
    )

    def __init__(self, **overrides) -> None:
        """Initialize per-pixel smoothing state.

        Args:
            **overrides: Parameter overrides.
        """
        super().__init__(**overrides)
        # Per-pixel smoothed HSB: (hue_01, sat_01, bri_01).
        self._smooth: list[tuple[float, float, float]] = []

    def on_start(self, zone_count: int) -> None:
        """Reset smoothing state.

        Args:
            zone_count: Total pixel count (width * height).
        """
        w: int = int(self.width)
        h: int = int(self.height)
        self._smooth = [(0.0, 0.0, 0.0)] * (w * h)

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame from grid vision signals.

        Reads ``grid_hues``, ``grid_sats``, and ``grid_bris`` arrays
        from the signal bus — each is a flat row-major array of float
        values in [0, 1].  Maps to HSBK with smoothing.

        Args:
            t:          Seconds since effect started.
            zone_count: Total pixels (ignored — uses width * height).

        Returns:
            A list of ``width * height`` HSBK tuples in row-major order.
        """
        w: int = int(self.width)
        h: int = int(self.height)
        total: int = w * h
        src: str = str(self.source)

        # Read grid signals from the vision extractor.
        grid_hues: list = self.signal(
            f"{src}:vision:grid_hues", [],
        )
        grid_sats: list = self.signal(
            f"{src}:vision:grid_sats", [],
        )
        grid_bris: list = self.signal(
            f"{src}:vision:grid_bris", [],
        )

        # If no grid data yet, return black.
        if not grid_hues or not grid_bris:
            return [BLACK] * total

        # Resize smoothing buffer if needed.
        if len(self._smooth) != total:
            self._smooth = [(0.0, 0.0, 0.0)] * total

        # Precompute param values.
        min_bri: float = float(self.min_brightness) / 100.0
        max_bri: float = float(self.max_brightness) / 100.0
        sat_scale: float = float(self.saturation_boost) / 100.0
        sens: float = float(self.sensitivity)
        kelvin_val: int = int(self.kelvin)

        # The grid signals may be a different resolution than our
        # output grid.  Use nearest-neighbor sampling.
        src_len: int = len(grid_hues)
        src_w: int = int(self.signal(f"{src}:vision:grid_w", w))
        src_h: int = int(self.signal(f"{src}:vision:grid_h", h))

        colors: list[HSBK] = []
        for y in range(h):
            for x in range(w):
                # Map output pixel to source grid position.
                sx: int = min(int(x * src_w / w), src_w - 1)
                sy: int = min(int(y * src_h / h), src_h - 1)
                si: int = sy * src_w + sx

                if si >= src_len:
                    colors.append(BLACK)
                    continue

                # Raw values from vision extractor.
                raw_h: float = float(grid_hues[si]) if si < len(grid_hues) else 0.0
                raw_s: float = float(grid_sats[si]) if si < len(grid_sats) else 0.0
                raw_b: float = float(grid_bris[si]) if si < len(grid_bris) else 0.0

                # Apply sensitivity and saturation boost.
                raw_b = min(1.0, raw_b * sens)
                raw_s = min(1.0, raw_s * sat_scale)

                # Temporal smoothing.
                idx: int = y * w + x
                prev_h, prev_s, prev_b = self._smooth[idx]
                sm_h: float = prev_h + SMOOTH_ALPHA * (raw_h - prev_h)
                sm_s: float = prev_s + SMOOTH_ALPHA * (raw_s - prev_s)
                sm_b: float = prev_b + SMOOTH_ALPHA * (raw_b - prev_b)
                self._smooth[idx] = (sm_h, sm_s, sm_b)

                # Clamp brightness.
                bri: float = max(min_bri, min(max_bri, sm_b))
                if bri < MIN_BRIGHTNESS:
                    colors.append(BLACK)
                    continue

                # Convert to LIFX HSBK.
                hue_u16: int = int((sm_h % 1.0) * HSBK_MAX)
                sat_u16: int = int(min(1.0, sm_s) * HSBK_MAX)
                bri_u16: int = int(bri * HSBK_MAX)
                colors.append((hue_u16, sat_u16, bri_u16, kelvin_val))

        return colors
