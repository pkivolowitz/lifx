"""Screen-reactive lighting effect.

Maps vision signals from screen capture to any LIFX device.  Adapts
automatically to the device's capabilities:

- **Single-zone bulbs:** dominant screen color + energy brightness.
- **Multizone devices:** edge colors mapped spatially across zones
  with energy-modulated brightness and motion influence.

The effect reads signals from the VisionExtractor and doesn't know
anything about the screen capture process — it just consumes
normalized [0, 1] signal values from the bus.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import math
from effects import MediaEffect, Param, HSBK, HSBK_MAX, KELVIN_DEFAULT

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Smoothing alpha for per-zone brightness (prevents flickering).
# Lower = heavier temporal smoothing = less boil.
ZONE_SMOOTH_ALPHA: float = 0.12

# Minimum perceptible brightness change (prevents micro-flicker).
MIN_BRIGHTNESS_DELTA: float = 0.005

# Maximum kernel half-width — caps memory and compute for large zone counts.
MAX_KERNEL_RADIUS: int = 20


def _gaussian_kernel(radius: int, sigma: float) -> list[float]:
    """Build a normalized 1D Gaussian kernel.

    Args:
        radius: Half-width of the kernel (full width = 2*radius + 1).
        sigma:  Standard deviation in zones.

    Returns:
        Normalized kernel weights (sum = 1.0).
    """
    r: int = min(radius, MAX_KERNEL_RADIUS)
    raw: list[float] = [
        math.exp(-0.5 * (i / sigma) ** 2) for i in range(-r, r + 1)
    ]
    total: float = sum(raw)
    return [w / total for w in raw]


def _blur_channel(values: list[float], kernel: list[float]) -> list[float]:
    """Apply a 1D Gaussian blur to a channel, wrapping at edges.

    The strip wraps around the TV perimeter, so zones at the start
    are spatially adjacent to zones at the end.  Circular convolution
    handles this correctly.

    Args:
        values: Per-zone float values.
        kernel: Normalized Gaussian kernel from :func:`_gaussian_kernel`.

    Returns:
        Blurred values, same length as input.
    """
    n: int = len(values)
    if n <= 1:
        return list(values)
    r: int = len(kernel) // 2
    result: list[float] = []
    for i in range(n):
        acc: float = 0.0
        for k, w in enumerate(kernel):
            j: int = (i + k - r) % n  # circular wrap
            acc += values[j] * w
        result.append(acc)
    return result


def hue_to_u16(hue_01: float) -> int:
    """Convert a [0, 1] hue to LIFX 16-bit hue.

    Args:
        hue_01: Hue in [0, 1] (0 = red, 0.33 = green, 0.66 = blue).

    Returns:
        LIFX hue value [0, 65535].
    """
    return int((hue_01 % 1.0) * HSBK_MAX)


def pct_to_u16(pct: float) -> int:
    """Convert a percentage [0, 100] to LIFX 16-bit value.

    Args:
        pct: Percentage value.

    Returns:
        LIFX value [0, 65535].
    """
    return int(max(0.0, min(100.0, pct)) / 100.0 * HSBK_MAX)


# ---------------------------------------------------------------------------
# ScreenLight effect
# ---------------------------------------------------------------------------

class ScreenLight(MediaEffect):
    """Screen-reactive lighting — drives any LIFX device from screen content.

    Reads vision signals and maps them to HSBK zone colors.  The
    mapping adapts to zone_count at render time, so the same effect
    works on a single bulb, a 102-zone string light, or a ceiling grid.
    """

    name = "screen_light"
    description = "Screen-reactive ambient lighting"

    # -- Tunable parameters --------------------------------------------------

    source = Param(
        "screen", description="Vision source name (matches screen config)",
    )
    sensitivity = Param(
        1.5, min=0.1,
        description="Brightness sensitivity multiplier",
    )
    contrast = Param(
        1.0, min=0.1, max=5.0,
        description=(
            "Dynamic range expansion (gamma). "
            "1.0 = linear, 2.0 = darks darker / brights brighter"
        ),
    )
    saturation_boost = Param(
        100, min=0, max=200,
        description="Saturation adjustment (100 = natural, 200 = vivid)",
    )
    min_brightness = Param(
        3, min=0, max=50,
        description="Minimum zone brightness (percent)",
    )
    max_brightness = Param(
        100, min=20, max=100,
        description="Maximum zone brightness (percent)",
    )
    flash_intensity = Param(
        40, min=0, max=100,
        description="Extra brightness on screen flash (percent, 0 to disable)",
    )
    motion_influence = Param(
        30, min=0, max=100,
        description=(
            "How much motion shifts the color mapping (percent). "
            "0 = static, 100 = full sweep with motion direction."
        ),
    )
    blur = Param(
        3.0, min=0.0, max=15.0,
        description=(
            "Spatial Gaussian blur radius in zones.  Softens color "
            "boundaries for a natural glow.  0 = sharp edges."
        ),
    )

    def __init__(self, **overrides) -> None:
        """Initialize with per-zone smoothing state.

        Args:
            **overrides: Parameter overrides.
        """
        super().__init__(**overrides)
        self._smooth_zones: list[tuple[float, float, float]] = []
        self._flash: float = 0.0
        # Cached Gaussian kernel — rebuilt when blur param or zone count changes.
        self._cached_kernel: list[float] = []
        self._cached_blur: float = -1.0
        self._cached_zone_count: int = -1

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame from vision signals.

        Args:
            t:          Seconds since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            List of *zone_count* HSBK tuples.
        """
        # Initialize smoothing on first call or zone count change.
        if len(self._smooth_zones) != zone_count:
            self._smooth_zones = [(0.0, 0.0, 0.0)] * zone_count

        # Read vision signals.
        src: str = self.source
        brightness: float = float(
            self.signal(f"{src}:vision:brightness", 0.0)
        )
        energy: float = float(
            self.signal(f"{src}:vision:energy", 0.0)
        )
        flash: float = float(
            self.signal(f"{src}:vision:flash", 0.0)
        )
        dominant_hue: float = float(
            self.signal(f"{src}:vision:dominant_hue", 0.0)
        )
        dominant_sat: float = float(
            self.signal(f"{src}:vision:dominant_sat", 0.5)
        )
        edge_colors: list = self.signal(
            f"{src}:vision:edge_colors", [0.0],
        )
        edge_brightness: list = self.signal(
            f"{src}:vision:edge_brightness", [0.0],
        )
        motion_angle: float = float(
            self.signal(f"{src}:vision:motion_angle", 0.0)
        )
        motion_mag: float = float(
            self.signal(f"{src}:vision:motion_magnitude", 0.0)
        )

        # Precompute param values.
        min_bri: float = self.min_brightness / 100.0
        max_bri: float = self.max_brightness / 100.0
        sat_scale: float = self.saturation_boost / 100.0
        flash_add: float = flash * (self.flash_intensity / 100.0)

        # Motion offset: shift the edge color mapping.
        motion_offset: float = (
            motion_angle * motion_mag * (self.motion_influence / 100.0)
        )

        n_edges: int = len(edge_colors) if isinstance(edge_colors, list) else 1
        if n_edges == 0:
            n_edges = 1
            edge_colors = [0.0]
        n_edge_bri: int = (
            len(edge_brightness) if isinstance(edge_brightness, list) else 1
        )
        if n_edge_bri == 0:
            n_edge_bri = 1
            edge_brightness = [0.0]

        # -- Pass 1: compute raw HSB per zone ----------------------------------
        raw_h: list[float] = []
        raw_s: list[float] = []
        raw_b: list[float] = []

        for z in range(zone_count):
            if zone_count == 1:
                # Single-zone: use dominant color.
                hue_val: float = dominant_hue
                sat_val: float = dominant_sat
                bri_val: float = brightness
            else:
                # Multizone: map zone position to edge color array.
                # Apply motion offset to shift the mapping.
                pos: float = (
                    z / max(1, zone_count - 1) + motion_offset
                ) % 1.0

                # Interpolate in the edge color array.
                edge_pos: float = pos * (n_edges - 1)
                lo: int = int(edge_pos) % n_edges
                hi: int = (lo + 1) % n_edges
                frac: float = edge_pos - int(edge_pos)

                hue_val = (
                    edge_colors[lo] * (1.0 - frac)
                    + edge_colors[hi] * frac
                ) if n_edges > 1 else dominant_hue

                # Edge brightness.
                if n_edge_bri > 1:
                    eb_pos: float = pos * (n_edge_bri - 1)
                    eb_lo: int = int(eb_pos) % n_edge_bri
                    eb_hi: int = (eb_lo + 1) % n_edge_bri
                    eb_frac: float = eb_pos - int(eb_pos)
                    bri_val = (
                        edge_brightness[eb_lo] * (1.0 - eb_frac)
                        + edge_brightness[eb_hi] * eb_frac
                    )
                else:
                    bri_val = brightness

                sat_val = dominant_sat

            # Apply sensitivity.
            bri_val = min(1.0, bri_val * self.sensitivity)

            # Apply contrast (gamma curve).
            if self.contrast != 1.0 and bri_val > 0.0:
                bri_val = bri_val ** self.contrast

            # Scale saturation.
            sat_val = min(1.0, sat_val * sat_scale)

            # Map to brightness range with flash.
            bri_final: float = min_bri + bri_val * (max_bri - min_bri)
            bri_final = min(1.0, bri_final + flash_add)

            raw_h.append(hue_val)
            raw_s.append(sat_val)
            raw_b.append(bri_final)

        # -- Pass 2: spatial Gaussian blur -------------------------------------
        # Softens color boundaries for a natural ambient glow.  The strip
        # wraps the TV perimeter so the blur uses circular convolution.
        blur_r: float = self.blur
        if blur_r > 0.5 and zone_count > 1:
            # Rebuild kernel only when blur param or zone count changes.
            if (blur_r != self._cached_blur
                    or zone_count != self._cached_zone_count):
                radius: int = max(1, int(math.ceil(blur_r)))
                sigma: float = blur_r / 2.0
                self._cached_kernel = _gaussian_kernel(radius, sigma)
                self._cached_blur = blur_r
                self._cached_zone_count = zone_count
            kernel: list[float] = self._cached_kernel
            raw_h = _blur_channel(raw_h, kernel)
            raw_s = _blur_channel(raw_s, kernel)
            raw_b = _blur_channel(raw_b, kernel)

        # -- Pass 3: temporal smoothing + HSBK conversion ----------------------
        colors: list[HSBK] = []
        for z in range(zone_count):
            prev_h, prev_s, prev_b = self._smooth_zones[z]
            new_h: float = prev_h + ZONE_SMOOTH_ALPHA * (raw_h[z] - prev_h)
            new_s: float = prev_s + ZONE_SMOOTH_ALPHA * (raw_s[z] - prev_s)
            new_b: float = prev_b + ZONE_SMOOTH_ALPHA * (raw_b[z] - prev_b)
            self._smooth_zones[z] = (new_h, new_s, new_b)

            colors.append((
                hue_to_u16(new_h),
                int(new_s * HSBK_MAX),
                int(new_b * HSBK_MAX),
                KELVIN_DEFAULT,
            ))

        return colors

    def period(self) -> None:
        """ScreenLight is aperiodic — driven by live screen capture.

        Returns:
            ``None`` always.
        """
        return None
