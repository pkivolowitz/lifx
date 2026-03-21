"""Vision signal extractor — screen frames to LIFX-ready signals.

Processes RGB frames from the screen capture pyramid and produces
normalized signals on the SignalBus.  Hardware-agnostic — the
extractor doesn't know what lights exist.  Effects read the signals
and adapt to whatever devices are available.

Signals produced:
    {src}:vision:brightness        scalar  — overall screen luminance
    {src}:vision:energy            scalar  — brightness change rate
    {src}:vision:flash             scalar  — sudden brightness spike (pulse)
    {src}:vision:dominant_hue      scalar  — most common hue [0,1] → 360°
    {src}:vision:dominant_sat      scalar  — saturation of dominant color
    {src}:vision:edge_colors       array   — hue per edge region [0,1]
    {src}:vision:edge_brightness   array   — brightness per edge region [0,1]
    {src}:vision:motion_angle      scalar  — dominant motion direction [0,1]
    {src}:vision:motion_magnitude  scalar  — motion intensity [0,1]
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import math
import time
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.media.vision")

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------

try:
    import numpy as np
    _HAS_NUMPY: bool = True
except ImportError:
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

try:
    import cv2 as _cv2
    _HAS_OPENCV: bool = True
except ImportError:
    _cv2 = None  # type: ignore[assignment]
    _HAS_OPENCV = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Number of edge regions for spatial color mapping.
# The screen perimeter is divided into this many segments.
# For a 32x18 analysis frame: top(32) + right(18) + bottom(32) + left(18)
# = 100 pixels of perimeter, divided into N regions.
DEFAULT_EDGE_REGIONS: int = 24

# Hue histogram bins (0-360° mapped to N bins).
HUE_BINS: int = 36

# Smoothing factor for temporal signals (EMA alpha).
SMOOTH_ALPHA: float = 0.3

# Flash detection: brightness delta must exceed this threshold.
FLASH_THRESHOLD: float = 0.3

# Flash decay time (seconds).
FLASH_DECAY: float = 0.2

# Minimum saturation for a pixel to contribute to hue analysis.
# Desaturated pixels (near white/gray/black) have unstable hues.
MIN_SAT_FOR_HUE: float = 0.15

# Minimum brightness for a pixel to contribute to hue analysis.
MIN_BRI_FOR_HUE: float = 0.08


# ---------------------------------------------------------------------------
# Color utilities (pure Python fallbacks)
# ---------------------------------------------------------------------------

def _rgb_to_hsb(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Convert RGB [0,1] to HSB [0,1].

    Args:
        r, g, b: RGB components in [0, 1].

    Returns:
        (hue, saturation, brightness) each in [0, 1].
    """
    max_c: float = max(r, g, b)
    min_c: float = min(r, g, b)
    delta: float = max_c - min_c

    # Brightness.
    brightness: float = max_c

    # Saturation.
    if max_c == 0.0:
        saturation: float = 0.0
    else:
        saturation = delta / max_c

    # Hue.
    if delta == 0.0:
        hue: float = 0.0
    elif max_c == r:
        hue = ((g - b) / delta) % 6.0
    elif max_c == g:
        hue = (b - r) / delta + 2.0
    else:
        hue = (r - g) / delta + 4.0
    hue /= 6.0
    if hue < 0.0:
        hue += 1.0

    return hue, saturation, brightness


# ---------------------------------------------------------------------------
# VisionExtractor
# ---------------------------------------------------------------------------

class VisionExtractor:
    """Extract vision signals from screen capture pyramid frames.

    Registered as a callback on :class:`ScreenSource`.  Receives the
    Gaussian pyramid per frame, runs analysis on the lowest level
    for speed, and writes normalized signals to the SignalBus.

    Args:
        source_name:   Name prefix for signal naming.
        bus:           SignalBus instance for output.
        edge_regions:  Number of edge color regions (default 24).
    """

    def __init__(
        self,
        source_name: str,
        bus: Any,
        edge_regions: int = DEFAULT_EDGE_REGIONS,
    ) -> None:
        self._source_name: str = source_name
        self._bus: Any = bus
        self._edge_regions: int = edge_regions

        # Temporal state.
        self._prev_brightness: float = 0.0
        self._smooth_brightness: float = 0.0
        self._smooth_energy: float = 0.0
        self._flash_value: float = 0.0
        self._last_flash_time: float = 0.0
        self._smooth_dominant_hue: float = 0.0
        self._smooth_dominant_sat: float = 0.0
        self._smooth_motion_mag: float = 0.0
        self._smooth_motion_angle: float = 0.0
        self._smooth_edge_colors: list[float] = [0.0] * edge_regions
        self._smooth_edge_brightness: list[float] = [0.0] * edge_regions

        # Previous frame for motion detection (grayscale).
        self._prev_gray: Optional[Any] = None

        # Register signals on the bus.
        self._register_signals()

        logger.info(
            "VisionExtractor '%s': %d edge regions, "
            "numpy=%s, opencv=%s",
            source_name, edge_regions,
            _HAS_NUMPY, _HAS_OPENCV,
        )

    def _register_signals(self) -> None:
        """Register all vision signal names on the bus."""
        prefix: str = f"{self._source_name}:vision"
        try:
            from media import SignalMeta
            scalars: list[str] = [
                "brightness", "energy", "flash",
                "dominant_hue", "dominant_sat",
                "motion_angle", "motion_magnitude",
            ]
            arrays: list[str] = ["edge_colors", "edge_brightness"]
            for name in scalars:
                self._bus.register(f"{prefix}:{name}", SignalMeta(
                    signal_type="scalar",
                    description=name.replace("_", " ").title(),
                    source_name=self._source_name,
                ))
            for name in arrays:
                self._bus.register(f"{prefix}:{name}", SignalMeta(
                    signal_type="array",
                    description=name.replace("_", " ").title(),
                    source_name=self._source_name,
                ))
        except Exception:
            pass  # Bus may not support registration.

    def process_pyramid(
        self, pyramid: list[Any], width: int, height: int,
    ) -> None:
        """Process one frame's pyramid and update the signal bus.

        This is the callback registered on ScreenSource.

        Args:
            pyramid: List of frames from highest to lowest resolution.
            width:   Capture resolution width.
            height:  Capture resolution height.
        """
        if not pyramid:
            return

        # Use the lowest pyramid level for fast analysis.
        bottom = pyramid[-1]

        if _HAS_NUMPY and isinstance(bottom, np.ndarray):
            self._analyze_numpy(bottom, pyramid)
        else:
            self._analyze_python(bottom)

    # ------------------------------------------------------------------
    # Numpy analysis path (fast)
    # ------------------------------------------------------------------

    def _analyze_numpy(
        self, frame: "np.ndarray", pyramid: list[Any],
    ) -> None:
        """Analyze a frame using numpy vectorized operations.

        Args:
            frame:   Lowest pyramid level (H, W, 3) uint8.
            pyramid: Full pyramid for motion analysis at higher res.
        """
        h, w = frame.shape[:2]
        prefix: str = f"{self._source_name}:vision"

        # Convert to float [0, 1] once.
        fframe: np.ndarray = frame.astype(np.float32) / 255.0

        # --- Overall brightness ---
        brightness: float = float(fframe.mean())
        self._smooth_brightness += SMOOTH_ALPHA * (
            brightness - self._smooth_brightness
        )

        # --- Energy (brightness change rate) ---
        delta: float = abs(brightness - self._prev_brightness)
        self._smooth_energy += SMOOTH_ALPHA * (
            delta - self._smooth_energy
        )
        self._prev_brightness = brightness

        # --- Flash detection ---
        now: float = time.monotonic()
        if delta > FLASH_THRESHOLD:
            self._flash_value = 1.0
            self._last_flash_time = now
        if self._flash_value > 0.0:
            dt: float = now - self._last_flash_time
            self._flash_value = max(0.0, 1.0 - dt / FLASH_DECAY)

        # --- Convert to HSB per pixel ---
        r: np.ndarray = fframe[:, :, 0]
        g: np.ndarray = fframe[:, :, 1]
        b: np.ndarray = fframe[:, :, 2]

        max_c: np.ndarray = np.maximum(np.maximum(r, g), b)
        min_c: np.ndarray = np.minimum(np.minimum(r, g), b)
        delta_c: np.ndarray = max_c - min_c

        # Saturation.
        sat: np.ndarray = np.where(max_c > 0, delta_c / max_c, 0.0)

        # Hue (0-1).
        hue: np.ndarray = np.zeros_like(max_c)
        mask_r = (max_c == r) & (delta_c > 0)
        mask_g = (max_c == g) & (delta_c > 0) & ~mask_r
        mask_b = (delta_c > 0) & ~mask_r & ~mask_g

        hue[mask_r] = (((g[mask_r] - b[mask_r]) / delta_c[mask_r]) % 6.0) / 6.0
        hue[mask_g] = ((b[mask_g] - r[mask_g]) / delta_c[mask_g] + 2.0) / 6.0
        hue[mask_b] = ((r[mask_b] - g[mask_b]) / delta_c[mask_b] + 4.0) / 6.0

        # --- Dominant hue (weighted histogram) ---
        # Only count pixels with enough saturation and brightness.
        color_mask: np.ndarray = (
            (sat > MIN_SAT_FOR_HUE) & (max_c > MIN_BRI_FOR_HUE)
        )
        if color_mask.any():
            hue_values: np.ndarray = hue[color_mask]
            weights: np.ndarray = (sat[color_mask] * max_c[color_mask])
            hist, bin_edges = np.histogram(
                hue_values, bins=HUE_BINS, range=(0.0, 1.0),
                weights=weights,
            )
            dominant_bin: int = int(np.argmax(hist))
            dominant_hue: float = (dominant_bin + 0.5) / HUE_BINS
            # Weighted average saturation of the dominant bin.
            bin_mask: np.ndarray = (
                (hue_values >= dominant_bin / HUE_BINS)
                & (hue_values < (dominant_bin + 1) / HUE_BINS)
            )
            if bin_mask.any():
                dominant_sat: float = float(
                    sat[color_mask][bin_mask].mean()
                )
            else:
                dominant_sat = 0.5
        else:
            dominant_hue = 0.0
            dominant_sat = 0.0

        self._smooth_dominant_hue += SMOOTH_ALPHA * (
            dominant_hue - self._smooth_dominant_hue
        )
        self._smooth_dominant_sat += SMOOTH_ALPHA * (
            dominant_sat - self._smooth_dominant_sat
        )

        # --- Edge colors (perimeter sampling) ---
        edge_hues, edge_bris = self._extract_edges_numpy(
            hue, sat, max_c, h, w,
        )
        for i in range(self._edge_regions):
            self._smooth_edge_colors[i] += SMOOTH_ALPHA * (
                edge_hues[i] - self._smooth_edge_colors[i]
            )
            self._smooth_edge_brightness[i] += SMOOTH_ALPHA * (
                edge_bris[i] - self._smooth_edge_brightness[i]
            )

        # --- Motion detection ---
        motion_mag: float = 0.0
        motion_angle: float = 0.0

        # Use a higher pyramid level for motion (more detail).
        motion_frame = pyramid[-2] if len(pyramid) >= 2 else frame
        if isinstance(motion_frame, np.ndarray):
            gray: np.ndarray = (
                0.299 * motion_frame[:, :, 0].astype(np.float32)
                + 0.587 * motion_frame[:, :, 1].astype(np.float32)
                + 0.114 * motion_frame[:, :, 2].astype(np.float32)
            ).astype(np.uint8)

            if _HAS_OPENCV and self._prev_gray is not None:
                # Optical flow (Farneback).
                try:
                    flow = _cv2.calcOpticalFlowFarneback(
                        self._prev_gray, gray,
                        None,  # output
                        pyr_scale=0.5,
                        levels=3,
                        winsize=15,
                        iterations=3,
                        poly_n=5,
                        poly_sigma=1.2,
                        flags=0,
                    )
                    # Average flow vector.
                    avg_dx: float = float(flow[:, :, 0].mean())
                    avg_dy: float = float(flow[:, :, 1].mean())
                    motion_mag = min(1.0, math.sqrt(
                        avg_dx * avg_dx + avg_dy * avg_dy
                    ) / 10.0)
                    motion_angle = (
                        math.atan2(avg_dy, avg_dx) / (2 * math.pi)
                    ) % 1.0
                except Exception:
                    pass
            elif self._prev_gray is not None:
                # Fallback: frame differencing.
                diff: np.ndarray = np.abs(
                    gray.astype(np.float32)
                    - self._prev_gray.astype(np.float32)
                )
                motion_mag = min(1.0, float(diff.mean()) / 30.0)

            self._prev_gray = gray

        self._smooth_motion_mag += SMOOTH_ALPHA * (
            motion_mag - self._smooth_motion_mag
        )
        self._smooth_motion_angle += SMOOTH_ALPHA * (
            motion_angle - self._smooth_motion_angle
        )

        # --- Write to bus ---
        self._bus.write(f"{prefix}:brightness", self._smooth_brightness)
        self._bus.write(f"{prefix}:energy", self._smooth_energy)
        self._bus.write(f"{prefix}:flash", self._flash_value)
        self._bus.write(f"{prefix}:dominant_hue", self._smooth_dominant_hue)
        self._bus.write(f"{prefix}:dominant_sat", self._smooth_dominant_sat)
        self._bus.write(
            f"{prefix}:edge_colors",
            list(self._smooth_edge_colors),
        )
        self._bus.write(
            f"{prefix}:edge_brightness",
            list(self._smooth_edge_brightness),
        )
        self._bus.write(f"{prefix}:motion_angle", self._smooth_motion_angle)
        self._bus.write(
            f"{prefix}:motion_magnitude", self._smooth_motion_mag,
        )

    def _extract_edges_numpy(
        self,
        hue: "np.ndarray",
        sat: "np.ndarray",
        bri: "np.ndarray",
        h: int,
        w: int,
    ) -> tuple[list[float], list[float]]:
        """Extract dominant hue and brightness per edge region.

        Samples pixels from the frame's perimeter (edges) and divides
        them into N regions.  Each region gets the average hue
        (weighted by saturation) and average brightness.

        Args:
            hue: Hue array (H, W) in [0, 1].
            sat: Saturation array (H, W) in [0, 1].
            bri: Brightness array (H, W) in [0, 1].
            h:   Frame height.
            w:   Frame width.

        Returns:
            (edge_hues, edge_bris) — lists of N floats each.
        """
        n: int = self._edge_regions

        # Collect perimeter pixels: top → right → bottom → left.
        # This unwraps the screen border into a 1D strip.
        peri_hue: list[float] = []
        peri_sat: list[float] = []
        peri_bri: list[float] = []

        # Top row (left to right).
        for x in range(w):
            peri_hue.append(float(hue[0, x]))
            peri_sat.append(float(sat[0, x]))
            peri_bri.append(float(bri[0, x]))
        # Right column (top to bottom, skip corners).
        for y in range(1, h - 1):
            peri_hue.append(float(hue[y, w - 1]))
            peri_sat.append(float(sat[y, w - 1]))
            peri_bri.append(float(bri[y, w - 1]))
        # Bottom row (right to left).
        for x in range(w - 1, -1, -1):
            peri_hue.append(float(hue[h - 1, x]))
            peri_sat.append(float(sat[h - 1, x]))
            peri_bri.append(float(bri[h - 1, x]))
        # Left column (bottom to top, skip corners).
        for y in range(h - 2, 0, -1):
            peri_hue.append(float(hue[y, 0]))
            peri_sat.append(float(sat[y, 0]))
            peri_bri.append(float(bri[y, 0]))

        total: int = len(peri_hue)
        region_size: int = max(1, total // n)

        edge_hues: list[float] = []
        edge_bris: list[float] = []

        for i in range(n):
            start: int = i * region_size
            end: int = min(start + region_size, total)
            if start >= total:
                edge_hues.append(0.0)
                edge_bris.append(0.0)
                continue

            # Weighted average hue (weight = saturation × brightness).
            h_sum: float = 0.0
            w_sum: float = 0.0
            b_sum: float = 0.0
            for j in range(start, end):
                weight: float = peri_sat[j] * peri_bri[j]
                h_sum += peri_hue[j] * weight
                w_sum += weight
                b_sum += peri_bri[j]

            count: int = end - start
            edge_hues.append(h_sum / w_sum if w_sum > 0 else 0.0)
            edge_bris.append(b_sum / count if count > 0 else 0.0)

        return edge_hues, edge_bris

    # ------------------------------------------------------------------
    # Pure Python fallback
    # ------------------------------------------------------------------

    def _analyze_python(self, frame: dict) -> None:
        """Analyze a frame using pure Python (no numpy).

        Slower but functional.  Used when numpy is not available.

        Args:
            frame: Dict with 'pixels' (flat RGB list), 'w', 'h'.
        """
        pixels: list[int] = frame["pixels"]
        w: int = frame["w"]
        h: int = frame["h"]
        prefix: str = f"{self._source_name}:vision"

        # Overall brightness.
        total_bri: float = sum(pixels) / (len(pixels) * 255.0)
        self._smooth_brightness += SMOOTH_ALPHA * (
            total_bri - self._smooth_brightness
        )

        # Energy.
        delta: float = abs(total_bri - self._prev_brightness)
        self._smooth_energy += SMOOTH_ALPHA * (
            delta - self._smooth_energy
        )
        self._prev_brightness = total_bri

        # Flash.
        now: float = time.monotonic()
        if delta > FLASH_THRESHOLD:
            self._flash_value = 1.0
            self._last_flash_time = now
        if self._flash_value > 0.0:
            dt: float = now - self._last_flash_time
            self._flash_value = max(0.0, 1.0 - dt / FLASH_DECAY)

        # Dominant hue via histogram.
        hue_hist: list[float] = [0.0] * HUE_BINS
        for i in range(0, len(pixels), 3):
            r: float = pixels[i] / 255.0
            g: float = pixels[i + 1] / 255.0
            b: float = pixels[i + 2] / 255.0
            h_val, s_val, b_val = _rgb_to_hsb(r, g, b)
            if s_val > MIN_SAT_FOR_HUE and b_val > MIN_BRI_FOR_HUE:
                bin_idx: int = min(
                    int(h_val * HUE_BINS), HUE_BINS - 1
                )
                hue_hist[bin_idx] += s_val * b_val

        max_bin: int = max(range(HUE_BINS), key=lambda i: hue_hist[i])
        dominant_hue: float = (max_bin + 0.5) / HUE_BINS
        dominant_sat: float = 0.5  # Approximate.

        self._smooth_dominant_hue += SMOOTH_ALPHA * (
            dominant_hue - self._smooth_dominant_hue
        )
        self._smooth_dominant_sat += SMOOTH_ALPHA * (
            dominant_sat - self._smooth_dominant_sat
        )

        # Write to bus (simplified — no edge colors in pure Python path).
        self._bus.write(f"{prefix}:brightness", self._smooth_brightness)
        self._bus.write(f"{prefix}:energy", self._smooth_energy)
        self._bus.write(f"{prefix}:flash", self._flash_value)
        self._bus.write(f"{prefix}:dominant_hue", self._smooth_dominant_hue)
        self._bus.write(f"{prefix}:dominant_sat", self._smooth_dominant_sat)
        self._bus.write(
            f"{prefix}:edge_colors", self._smooth_edge_colors,
        )
        self._bus.write(
            f"{prefix}:edge_brightness", self._smooth_edge_brightness,
        )
        self._bus.write(f"{prefix}:motion_angle", 0.0)
        self._bus.write(f"{prefix}:motion_magnitude", 0.0)
