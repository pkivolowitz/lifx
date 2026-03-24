"""Flag effect -- display a waving national flag across the string lights.

Lays the flag's color stripes along a 1D line in space, applies fractal
Brownian motion (Perlin noise octaves) as depth displacement, then projects
through a perspective camera.  Stripes closer to the viewer expand and may
occlude stripes farther away.  Surface slope modulates brightness to simulate
the shading of fabric folds.

For single-color flags the effect displays a static solid color.

Usage::

    python3 glowup.py play flag --ip <device> --country france
    python3 glowup.py play flag --ip <device> --country japan --speed 3
    python3 glowup.py play flag --ip <device> --country us --direction right
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.4"

import math
import os
import sys
from typing import Any

from . import (
    DEVICE_TYPE_STRIP,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)
from .flag_data import StripeColor, get_flag, get_country_names

# Import colorspace module from project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from colorspace import lerp_color


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TWO_PI: float = 2.0 * math.pi

# Dense sampling resolution for the flag surface.
SAMPLE_COUNT: int = 1024

# --- Perlin noise ---------------------------------------------------------

# Ken Perlin's original permutation table -- provides the pseudo-random
# gradient selection for the noise function.  Doubled for index wrapping.
_PERM_BASE: list[int] = [
    151, 160, 137, 91, 90, 15, 131, 13, 201, 95, 96, 53, 194, 233, 7, 225,
    140, 36, 103, 30, 69, 142, 8, 99, 37, 240, 21, 10, 23, 190, 6, 148,
    247, 120, 234, 75, 0, 26, 197, 62, 94, 252, 219, 203, 117, 35, 11, 32,
    57, 177, 33, 88, 237, 149, 56, 87, 174, 20, 125, 136, 171, 168, 68, 175,
    74, 165, 71, 134, 139, 48, 27, 166, 77, 146, 158, 231, 83, 111, 229, 122,
    60, 211, 133, 230, 220, 105, 92, 41, 55, 46, 245, 40, 244, 102, 143, 54,
    65, 25, 63, 161, 1, 216, 80, 73, 209, 76, 132, 187, 208, 89, 18, 169,
    200, 196, 135, 130, 116, 188, 159, 86, 164, 100, 109, 198, 173, 186, 3, 64,
    52, 217, 226, 250, 124, 123, 5, 202, 38, 147, 118, 126, 255, 82, 85, 212,
    207, 206, 59, 227, 47, 16, 58, 17, 182, 189, 28, 42, 223, 183, 170, 213,
    119, 248, 152, 2, 44, 154, 163, 70, 221, 153, 101, 155, 167, 43, 172, 9,
    129, 22, 39, 253, 19, 98, 108, 110, 79, 113, 224, 232, 178, 185, 112, 104,
    218, 246, 97, 228, 251, 34, 242, 193, 238, 210, 144, 12, 191, 179, 162, 241,
    81, 51, 145, 235, 249, 14, 239, 107, 49, 192, 214, 31, 181, 199, 106, 157,
    184, 84, 204, 176, 115, 121, 50, 45, 127, 4, 150, 254, 138, 236, 205, 93,
    222, 114, 67, 29, 24, 72, 243, 141, 128, 195, 78, 66, 215, 61, 156, 180,
]
_PERM: list[int] = _PERM_BASE + _PERM_BASE

# --- Fractal Brownian motion ----------------------------------------------

FBM_OCTAVES: int = 5
"""Number of noise octaves stacked for the ripple texture."""

FBM_LACUNARITY: float = 2.0
"""Spatial frequency multiplier per octave."""

FBM_PERSISTENCE: float = 0.5
"""Amplitude multiplier per octave (higher = more fine detail)."""

BASE_FREQUENCY: float = 8.0
"""Spatial frequency of the fundamental noise layer (features per flag)."""

# Two noise layers moving in opposite directions create interference
# patterns similar to real fabric where waves reflect off the edges.
COUNTER_WAVE_FREQ_RATIO: float = 1.6
"""Spatial frequency ratio of the counter-wave to the main wave."""

COUNTER_WAVE_SPEED_RATIO: float = 0.7
"""Temporal speed ratio of the counter-wave (opposite direction)."""

COUNTER_WAVE_AMPLITUDE: float = 0.3
"""Relative amplitude of the counter-wave (main wave = 1.0)."""

MAIN_WAVE_AMPLITUDE: float = 0.7
"""Relative amplitude of the main wave (counter-wave = 0.3, total = 1.0)."""

# --- Perspective projection -----------------------------------------------

CAMERA_DISTANCE: float = 5.0
"""Virtual camera distance from the flag plane (in flag-length units).

With fine Perlin ripples and the chosen amplitude, the projection will
fold in steep regions.  The z-buffer handles occlusion correctly.
"""

WAVE_AMPLITUDE: float = 1.2
"""Maximum depth displacement of the flag surface.

Scales the raw noise output to control how dramatically stripes expand,
compress, and occlude one another.  Higher values produce deeper folds
with more pronounced occlusion.
"""

# --- Fold shading ---------------------------------------------------------

SHADE_MIN: float = 0.4
"""Minimum shade factor -- prevents folds from going completely dark."""

SHADE_SCALE: float = 0.001
"""Controls how quickly shading darkens with surface slope.

The shading formula is ``SHADE_MIN + (1 - SHADE_MIN) / (1 + slope² × SHADE_SCALE)``.
Smaller values produce gentler shading; larger values make folds more dramatic.
"""

# --- Temporal smoothing ----------------------------------------------------

SMOOTHING_ALPHA: float = 0.3
"""EMA blending factor for temporal smoothing (0-1).

Each frame is blended with the previous: ``new = alpha * current + (1 - alpha) * prev``.
Lower values = heavier smoothing (less flicker, more lag).  At 20 fps,
0.3 reaches 95 % of a new value in ~5 frames (250 ms) -- enough to
eliminate single-bulb flicker at stripe boundaries without visible lag.
"""

# --- Fallback -------------------------------------------------------------

FALLBACK_COUNTRY: str = "us"
"""Country used when the requested name is not in the database."""


# ---------------------------------------------------------------------------
# Internal helpers -- Perlin noise
# ---------------------------------------------------------------------------

def _perlin_1d(x: float) -> float:
    """Evaluate 1D Perlin noise at position *x*.

    Uses Ken Perlin's permutation table for gradient selection and a
    quintic smoothstep (``6t^5 - 15t^4 + 10t^3``) for interpolation.

    Args:
        x: Position along the noise domain (any float).

    Returns:
        Noise value in approximately ``[-1, +1]``.
    """
    # Integer and fractional parts.
    xi: int = int(math.floor(x)) & 255
    xf: float = x - math.floor(x)

    # Quintic smoothstep for C2-continuous interpolation.
    u: float = xf * xf * xf * (xf * (xf * 6.0 - 15.0) + 10.0)

    # 1D gradients: +1 or -1 selected by the low bit of the hash.
    g0: float = 1.0 if (_PERM[xi] & 1) else -1.0
    g1: float = 1.0 if (_PERM[xi + 1] & 1) else -1.0

    # Dot products (1D: gradient × distance).
    d0: float = g0 * xf
    d1: float = g1 * (xf - 1.0)

    # Interpolate.
    return d0 + u * (d1 - d0)


def _fbm(x: float) -> float:
    """Evaluate fractal Brownian motion (stacked Perlin octaves) at *x*.

    Each successive octave doubles in frequency (:data:`FBM_LACUNARITY`)
    and halves in amplitude (:data:`FBM_PERSISTENCE`), building up
    multi-scale detail from coarse folds to fine wrinkles.

    Args:
        x: Position in noise space (typically ``flag_x * freq + t * speed``).

    Returns:
        Summed noise value, roughly in ``[-2, +2]``.
    """
    total: float = 0.0
    amplitude: float = 1.0
    frequency: float = 1.0

    for _ in range(FBM_OCTAVES):
        total += amplitude * _perlin_1d(x * frequency)
        amplitude *= FBM_PERSISTENCE
        frequency *= FBM_LACUNARITY

    return total


# ---------------------------------------------------------------------------
# Internal helpers -- temporal smoothing
# ---------------------------------------------------------------------------

def _smooth_frame(
    prev: list[HSBK],
    curr: list[HSBK],
    alpha: float,
) -> list[HSBK]:
    """Blend the current frame toward the previous using an EMA.

    Interpolation passes through CIELAB perceptual color space to avoid
    the muddy intermediates and brightness dips of naive HSB blending.
    This is critical at stripe boundaries where the z-buffer jitters
    between two flag colors.

    Args:
        prev:  Previous frame's zone colors.
        curr:  Current frame's zone colors.
        alpha: Blend factor (0 = all prev, 1 = all curr).

    Returns:
        A new list of blended HSBK tuples with the same length as *curr*.
    """
    result: list[HSBK] = []
    for i in range(len(curr)):
        result.append(lerp_color(prev[i], curr[i], alpha))
    return result


# ---------------------------------------------------------------------------
# Internal helpers -- rendering
# ---------------------------------------------------------------------------

def _stripe_to_hsbk(
    stripe: StripeColor,
    brightness_pct: int,
    kelvin: int,
) -> HSBK:
    """Convert a flag stripe color to a LIFX HSBK tuple.

    The stripe's relative brightness is scaled by the effect's overall
    brightness parameter so that dark flag elements (navy, maroon)
    remain proportionally dimmer than bright ones (white, yellow).

    Args:
        stripe:         ``(hue_deg, sat_pct, bri_pct)`` from the database.
        brightness_pct: Effect-level brightness (0-100).
        kelvin:         Color temperature for the stripe.

    Returns:
        A 4-tuple ``(hue_u16, sat_u16, bri_u16, kelvin)``.
    """
    hue_deg: float = stripe[0]
    sat_pct: float = stripe[1]
    bri_pct: float = stripe[2]

    # Scale stripe brightness by the effect's master brightness.
    actual_bri: float = bri_pct * brightness_pct / 100.0

    return (
        hue_to_u16(hue_deg),
        pct_to_u16(sat_pct),
        pct_to_u16(actual_bri),
        kelvin,
    )


# ---------------------------------------------------------------------------
# Effect class
# ---------------------------------------------------------------------------

class Flag(Effect):
    """Display a waving national flag on the string lights.

    The flag's color stripes are laid out along a virtual 1D surface.
    Fractal Brownian motion (Perlin noise) displaces each point in
    depth, and a perspective projection maps the result onto zones.
    Two noise layers moving in opposite directions create natural
    interference.  Surface slope modulates brightness to simulate
    the shading of fabric folds.

    A z-buffer resolves occlusion when steep folds cause the
    projection to overlap.  Per-zone temporal smoothing (EMA) prevents
    single-bulb flicker at stripe boundaries.

    Use ``--country`` to select a flag by common name (e.g., ``us``,
    ``france``, ``japan``, ``germany``).  If the name is not found the
    effect falls back to the US flag and prints available names to
    stderr.
    """

    name: str = "flag"
    description: str = "Waving national flag with perspective ripple"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_STRIP})

    country = Param("us", description="Country name (e.g., us, france, japan)")
    speed = Param(1.5, min=0.1, max=20.0,
                  description="Wave propagation speed")
    brightness = Param(80, min=0, max=100,
                       description="Overall brightness percent")
    direction = Param("left", description="Stripe read direction: left or right",
                      choices=["left", "right"])
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def __init__(self, **overrides: Any) -> None:
        """Initialize the flag effect with temporal smoothing state.

        Args:
            **overrides: Parameter name/value overrides.
        """
        super().__init__(**overrides)
        # Per-zone EMA buffer, lazily sized on first render.
        self._prev_frame: list[HSBK] | None = None

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the waving flag.

        Steps:
            1. Sample N points on the flag surface.
            2. Compute depth via dual-layer Perlin fbm.
            3. Project through perspective camera.
            4. Compute fold shading from surface slope.
            5. Z-buffer render onto zone array.
            6. Fill any gaps from occlusion.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        # --- Resolve flag stripes ----------------------------------------
        stripes: list[StripeColor] | None = get_flag(self.country)
        if stripes is None:
            available: str = ", ".join(get_country_names())
            print(
                f"Unknown country '{self.country}'. "
                f"Available: {available}",
                file=sys.stderr,
            )
            stripes = get_flag(FALLBACK_COUNTRY)
            # Prevent repeated warnings by switching to fallback.
            self.country = FALLBACK_COUNTRY

        n_stripes: int = len(stripes)

        # --- Single-color flag: static solid ----------------------------
        if n_stripes == 1:
            color: HSBK = _stripe_to_hsbk(
                stripes[0], self.brightness, self.kelvin,
            )
            return [color] * zone_count

        # --- Multi-stripe: Perlin perspective wave ----------------------
        phase: float = t * self.speed

        # Dense sampling of the flag surface.
        sample_stripe: list[int] = []
        z_depth: list[float] = []
        screen_pos: list[float] = []

        n_minus_1: int = SAMPLE_COUNT - 1

        for i in range(SAMPLE_COUNT):
            # Normalized position on the flat flag [0, 1].
            flag_x: float = i / n_minus_1

            # Which stripe does this sample point belong to?
            idx: int = min(int(flag_x * n_stripes), n_stripes - 1)
            sample_stripe.append(idx)

            # Dual-layer Perlin fbm for depth displacement.
            # Main wave scrolls in the positive direction; counter-wave
            # moves in the opposite direction at a different frequency
            # to create natural interference patterns.
            noise_main: float = _fbm(flag_x * BASE_FREQUENCY - phase)
            noise_counter: float = _fbm(
                flag_x * BASE_FREQUENCY * COUNTER_WAVE_FREQ_RATIO + phase * COUNTER_WAVE_SPEED_RATIO
            )
            z: float = (
                noise_main * MAIN_WAVE_AMPLITUDE
                + noise_counter * COUNTER_WAVE_AMPLITUDE
            )
            z_depth.append(z)

            # Perspective projection: center the flag at x=0.5,
            # camera at z = -CAMERA_DISTANCE looking in +z direction.
            centered_x: float = flag_x - 0.5
            denominator: float = CAMERA_DISTANCE + z * WAVE_AMPLITUDE
            # Guard against division by zero / negative denominator
            # when very steep folds push z close to -CAMERA_DISTANCE.
            if denominator < 0.1:
                denominator = 0.1
            projected_x: float = CAMERA_DISTANCE * centered_x / denominator + 0.5
            screen_pos.append(projected_x)

        # --- Compute fold shading from surface slope --------------------
        # Numerical derivative of z with respect to flag position.
        # Slope-based Lambertian shading: flat surfaces are bright,
        # steep folds are dimmer.
        shade: list[float] = []
        dx_inv: float = float(n_minus_1)  # 1 / (1 / (N-1)) = N-1

        for i in range(SAMPLE_COUNT):
            if 0 < i < n_minus_1:
                slope: float = (z_depth[i + 1] - z_depth[i - 1]) * dx_inv * 0.5
            elif i == 0:
                slope = (z_depth[1] - z_depth[0]) * dx_inv
            else:
                slope = (z_depth[-1] - z_depth[-2]) * dx_inv

            # Smooth falloff: flat (slope=0) → shade=1.0,
            # steep (large |slope|) → shade approaches SHADE_MIN.
            s: float = SHADE_MIN + (1.0 - SHADE_MIN) / (
                1.0 + slope * slope * SHADE_SCALE
            )
            shade.append(s)

        # --- Normalize screen positions to [0, 1] ----------------------
        sp_min: float = min(screen_pos)
        sp_max: float = max(screen_pos)
        sp_range: float = sp_max - sp_min
        if sp_range <= 0.0:
            sp_range = 1.0

        normalized: list[float] = [
            (s - sp_min) / sp_range for s in screen_pos
        ]

        # --- Z-buffer rendering -----------------------------------------
        # For each zone, the closest sample (smallest z) wins.
        # This correctly handles occlusion when the projection folds.
        INF: float = float("inf")
        zone_z: list[float] = [INF] * zone_count
        zone_stripe: list[int] = [-1] * zone_count
        zone_shade: list[float] = [1.0] * zone_count
        last_zone: int = max(zone_count - 1, 1)

        for i in range(SAMPLE_COUNT):
            # Map normalized screen position to zone index.
            zi: int = int(normalized[i] * last_zone + 0.5)
            zi = max(0, min(zi, zone_count - 1))

            # Z-buffer test: closer samples overwrite farther ones.
            if z_depth[i] < zone_z[zi]:
                zone_z[zi] = z_depth[i]
                zone_stripe[zi] = sample_stripe[i]
                zone_shade[zi] = shade[i]

        # --- Fill gaps from occlusion -----------------------------------
        # Sweep left then right to propagate nearest filled zone into
        # any holes left by occluded regions.
        for i in range(1, zone_count):
            if zone_stripe[i] < 0 and zone_stripe[i - 1] >= 0:
                zone_stripe[i] = zone_stripe[i - 1]
                zone_shade[i] = zone_shade[i - 1]

        for i in range(zone_count - 2, -1, -1):
            if zone_stripe[i] < 0 and zone_stripe[i + 1] >= 0:
                zone_stripe[i] = zone_stripe[i + 1]
                zone_shade[i] = zone_shade[i + 1]

        # --- Build final zone colors with shading ----------------------
        zones: list[HSBK] = []
        for i in range(zone_count):
            si: int = zone_stripe[i] if zone_stripe[i] >= 0 else 0
            base: HSBK = _stripe_to_hsbk(
                stripes[si], self.brightness, self.kelvin,
            )
            # Apply fold shading to brightness only.
            shaded_bri: int = int(base[2] * zone_shade[i])
            zones.append((base[0], base[1], shaded_bri, base[3]))

        # --- Apply direction -------------------------------------------
        if self.direction == "right":
            zones.reverse()

        # --- Temporal smoothing (EMA) ----------------------------------
        # Blend with the previous frame to prevent single-bulb flicker
        # at stripe boundaries where the z-buffer assignment jitters.
        if self._prev_frame is not None and len(self._prev_frame) == zone_count:
            zones = _smooth_frame(self._prev_frame, zones, SMOOTHING_ALPHA)

        self._prev_frame = list(zones)
        return zones
