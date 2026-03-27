"""Colorspace conversions for perceptually uniform color interpolation.

Provides pipelines between LIFX HSBK and perceptual color spaces,
enabling smooth interpolation that avoids the artifacts of naive HSB
hue interpolation (uneven perceptual steps, brightness dips, muddy
intermediate colors).

Three interpolation methods are available:

* **oklab** (default) — Björn Ottosson's Oklab (2020).  More
  perceptually uniform than CIELAB, especially in blues and purples.
  Simpler math (one matrix multiply + cube root per direction).
  The modern choice for color blending.

* **lab** — CIELAB via CIEXYZ (D65).  The classic perceptual space.
  Still accurate, but has known non-uniformities that Oklab fixes.

* **hsb** — Naive shortest-path HSB blending.  Cheap but produces
  brightness dips and muddy intermediates on large hue jumps.

All conversions are pure math with no external dependencies.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.0"

import math

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# LIFX HSBK maximum value.
HSBK_MAX: int = 65535

# sRGB gamma threshold and constants (IEC 61966-2-1).
SRGB_LINEAR_THRESHOLD: float = 0.04045
SRGB_LINEAR_SCALE: float = 12.92
SRGB_GAMMA: float = 2.4
SRGB_OFFSET: float = 0.055
SRGB_DIVISOR: float = 1.055

# sRGB → CIEXYZ D65 matrix (row-major).
# Source: IEC 61966-2-1 / sRGB specification.
SRGB_TO_XYZ: list[list[float]] = [
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
]

# CIEXYZ D65 → sRGB matrix (inverse of above).
XYZ_TO_SRGB: list[list[float]] = [
    [ 3.2404542, -1.5371385, -0.4985314],
    [-0.9692660,  1.8760108,  0.0415560],
    [ 0.0556434, -0.2040259,  1.0572252],
]

# D65 reference white point (standard illuminant for sRGB).
D65_XN: float = 0.950456
D65_YN: float = 1.000000
D65_ZN: float = 1.088754

# CIELAB constants.
LAB_EPSILON: float = 216.0 / 24389.0   # 0.008856...
LAB_KAPPA: float = 24389.0 / 27.0      # 903.3...
LAB_ONE_THIRD: float = 1.0 / 3.0
LAB_OFFSET: float = 16.0 / 116.0

# HSB sextant count (divides 0–1 hue into 6 segments).
_HUE_SEXTANTS: int = 6


# ---------------------------------------------------------------------------
# sRGB gamma
# ---------------------------------------------------------------------------

def srgb_to_linear(c: float) -> float:
    """Remove sRGB gamma curve from a single channel.

    Args:
        c: sRGB channel value in [0.0, 1.0].

    Returns:
        Linear-light value in [0.0, 1.0].
    """
    if c <= SRGB_LINEAR_THRESHOLD:
        return c / SRGB_LINEAR_SCALE
    return ((c + SRGB_OFFSET) / SRGB_DIVISOR) ** SRGB_GAMMA


def linear_to_srgb(c: float) -> float:
    """Apply sRGB gamma curve to a single channel.

    Args:
        c: Linear-light value in [0.0, 1.0].

    Returns:
        sRGB channel value in [0.0, 1.0].
    """
    if c <= 0.0031308:
        return c * SRGB_LINEAR_SCALE
    return SRGB_DIVISOR * (c ** (1.0 / SRGB_GAMMA)) - SRGB_OFFSET


# ---------------------------------------------------------------------------
# HSB ↔ sRGB
# ---------------------------------------------------------------------------

def hsb_to_srgb(h: float, s: float, b: float) -> tuple[float, float, float]:
    """Convert HSB to sRGB.

    Args:
        h: Hue in [0.0, 1.0) (fractional turns, not degrees).
        s: Saturation in [0.0, 1.0].
        b: Brightness in [0.0, 1.0].

    Returns:
        Tuple of (r, g, b) each in [0.0, 1.0].
    """
    if s <= 0.0:
        return (b, b, b)

    hh: float = h * _HUE_SEXTANTS
    sextant: int = int(hh) % _HUE_SEXTANTS
    f: float = hh - int(hh)

    p: float = b * (1.0 - s)
    q: float = b * (1.0 - s * f)
    t: float = b * (1.0 - s * (1.0 - f))

    if sextant == 0:
        return (b, t, p)
    elif sextant == 1:
        return (q, b, p)
    elif sextant == 2:
        return (p, b, t)
    elif sextant == 3:
        return (p, q, b)
    elif sextant == 4:
        return (t, p, b)
    else:
        return (b, p, q)


def srgb_to_hsb(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Convert sRGB to HSB.

    Args:
        r: Red channel in [0.0, 1.0].
        g: Green channel in [0.0, 1.0].
        b: Blue channel in [0.0, 1.0].

    Returns:
        Tuple of (h, s, bri) — hue in [0.0, 1.0), saturation and
        brightness in [0.0, 1.0].
    """
    max_c: float = max(r, g, b)
    min_c: float = min(r, g, b)
    delta: float = max_c - min_c

    # Brightness.
    bri: float = max_c

    # Saturation.
    if max_c <= 0.0:
        return (0.0, 0.0, 0.0)
    sat: float = delta / max_c

    # Hue.
    if delta <= 0.0:
        return (0.0, 0.0, bri)

    if max_c == r:
        hue: float = (g - b) / delta
    elif max_c == g:
        hue = 2.0 + (b - r) / delta
    else:
        hue = 4.0 + (r - g) / delta

    hue /= _HUE_SEXTANTS
    if hue < 0.0:
        hue += 1.0

    return (hue, sat, bri)


# ---------------------------------------------------------------------------
# sRGB ↔ CIEXYZ (D65)
# ---------------------------------------------------------------------------

def srgb_to_xyz(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Convert sRGB to CIEXYZ using the D65 illuminant.

    Applies inverse sRGB gamma first, then the standard 3×3 matrix.

    Args:
        r, g, b: sRGB channels in [0.0, 1.0].

    Returns:
        Tuple of (X, Y, Z) in CIEXYZ space.
    """
    rl: float = srgb_to_linear(r)
    gl: float = srgb_to_linear(g)
    bl: float = srgb_to_linear(b)

    x: float = SRGB_TO_XYZ[0][0] * rl + SRGB_TO_XYZ[0][1] * gl + SRGB_TO_XYZ[0][2] * bl
    y: float = SRGB_TO_XYZ[1][0] * rl + SRGB_TO_XYZ[1][1] * gl + SRGB_TO_XYZ[1][2] * bl
    z: float = SRGB_TO_XYZ[2][0] * rl + SRGB_TO_XYZ[2][1] * gl + SRGB_TO_XYZ[2][2] * bl

    return (x, y, z)


def xyz_to_srgb(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Convert CIEXYZ (D65) to sRGB.

    Applies the inverse 3×3 matrix, clamps, then applies sRGB gamma.

    Args:
        x, y, z: CIEXYZ coordinates.

    Returns:
        Tuple of (r, g, b) each in [0.0, 1.0], clamped to gamut.
    """
    rl: float = XYZ_TO_SRGB[0][0] * x + XYZ_TO_SRGB[0][1] * y + XYZ_TO_SRGB[0][2] * z
    gl: float = XYZ_TO_SRGB[1][0] * x + XYZ_TO_SRGB[1][1] * y + XYZ_TO_SRGB[1][2] * z
    bl: float = XYZ_TO_SRGB[2][0] * x + XYZ_TO_SRGB[2][1] * y + XYZ_TO_SRGB[2][2] * z

    # Clamp to [0, 1] — out-of-gamut values can occur during
    # interpolation in Lab space.
    rl = max(0.0, min(1.0, rl))
    gl = max(0.0, min(1.0, gl))
    bl = max(0.0, min(1.0, bl))

    return (linear_to_srgb(rl), linear_to_srgb(gl), linear_to_srgb(bl))


# ---------------------------------------------------------------------------
# CIEXYZ ↔ CIELAB
# ---------------------------------------------------------------------------

def _lab_f(t: float) -> float:
    """CIELAB forward transfer function.

    Args:
        t: Normalized XYZ component (divided by reference white).

    Returns:
        Transformed value for L*, a*, b* computation.
    """
    if t > LAB_EPSILON:
        return t ** LAB_ONE_THIRD
    return (LAB_KAPPA * t + 16.0) / 116.0


def _lab_f_inv(t: float) -> float:
    """CIELAB inverse transfer function.

    Args:
        t: Transformed Lab intermediate value.

    Returns:
        Normalized XYZ component.
    """
    t3: float = t * t * t
    if t3 > LAB_EPSILON:
        return t3
    return (116.0 * t - 16.0) / LAB_KAPPA


def xyz_to_lab(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Convert CIEXYZ (D65) to CIELAB.

    Args:
        x, y, z: CIEXYZ coordinates (D65 illuminant).

    Returns:
        Tuple of (L*, a*, b*).  L* ranges 0–100, a* and b* are
        unbounded but typically ±128 for sRGB colors.
    """
    fx: float = _lab_f(x / D65_XN)
    fy: float = _lab_f(y / D65_YN)
    fz: float = _lab_f(z / D65_ZN)

    L: float = 116.0 * fy - 16.0
    a: float = 500.0 * (fx - fy)
    b: float = 200.0 * (fy - fz)

    return (L, a, b)


def lab_to_xyz(L: float, a: float, b: float) -> tuple[float, float, float]:
    """Convert CIELAB to CIEXYZ (D65).

    Args:
        L: Lightness (0–100).
        a: Green–red axis.
        b: Blue–yellow axis.

    Returns:
        Tuple of (X, Y, Z) in CIEXYZ space.
    """
    fy: float = (L + 16.0) / 116.0
    fx: float = a / 500.0 + fy
    fz: float = fy - b / 200.0

    x: float = _lab_f_inv(fx) * D65_XN
    y: float = _lab_f_inv(fy) * D65_YN
    z: float = _lab_f_inv(fz) * D65_ZN

    return (x, y, z)


# ---------------------------------------------------------------------------
# sRGB ↔ Oklab  (Björn Ottosson, 2020)
# ---------------------------------------------------------------------------
#
# Oklab is a perceptual color space designed for uniform color blending.
# It fixes the non-uniformities in CIELAB (especially blues/purples)
# and uses simpler math: one 3×3 matrix, cube root, second 3×3 matrix.
#
# Conversion chain:
#     sRGB → linear RGB → LMS (via M1) → cube root → Oklab (via M2)
#     Oklab → cube (via M2⁻¹) → LMS → linear RGB (via M1⁻¹) → sRGB
#
# Reference: https://bottosson.github.io/posts/oklab/

# sRGB linear → LMS cone responses (M1).
_OK_M1: list[list[float]] = [
    [0.4122214708, 0.5363325363, 0.0514459929],
    [0.2119034982, 0.6806995451, 0.1073969566],
    [0.0883024619, 0.2817188376, 0.6299787005],
]

# LMS cube root → Oklab (M2).
_OK_M2: list[list[float]] = [
    [0.2104542553, 0.7936177850, -0.0040720468],
    [1.9779984951, -2.4285922050, 0.4505937099],
    [0.0259040371, 0.7827717662, -0.8086757660],
]

# Oklab → LMS cube root (M2 inverse).
_OK_M2_INV: list[list[float]] = [
    [1.0000000000, 0.3963377774, 0.2158037573],
    [1.0000000000, -0.1055613458, -0.0638541728],
    [1.0000000000, -0.0894841775, -1.2914855480],
]

# LMS → sRGB linear (M1 inverse).
_OK_M1_INV: list[list[float]] = [
    [ 4.0767416621, -3.3077115913, 0.2309699292],
    [-1.2684380046,  2.6097574011, -0.3413193965],
    [-0.0041960863, -0.7034186147, 1.7076147010],
]


def srgb_to_oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Convert sRGB to Oklab.

    Args:
        r, g, b: sRGB channels in [0.0, 1.0].

    Returns:
        Tuple of (L, a, b) in Oklab space.  L is 0–1 (not 0–100).
    """
    # sRGB → linear.
    rl: float = srgb_to_linear(r)
    gl: float = srgb_to_linear(g)
    bl: float = srgb_to_linear(b)

    # Linear RGB → LMS.
    l: float = _OK_M1[0][0] * rl + _OK_M1[0][1] * gl + _OK_M1[0][2] * bl
    m: float = _OK_M1[1][0] * rl + _OK_M1[1][1] * gl + _OK_M1[1][2] * bl
    s: float = _OK_M1[2][0] * rl + _OK_M1[2][1] * gl + _OK_M1[2][2] * bl

    # Cube root.
    l_ = math.copysign(abs(l) ** LAB_ONE_THIRD, l) if l != 0.0 else 0.0
    m_ = math.copysign(abs(m) ** LAB_ONE_THIRD, m) if m != 0.0 else 0.0
    s_ = math.copysign(abs(s) ** LAB_ONE_THIRD, s) if s != 0.0 else 0.0

    # LMS cube root → Oklab.
    L: float = _OK_M2[0][0] * l_ + _OK_M2[0][1] * m_ + _OK_M2[0][2] * s_
    a: float = _OK_M2[1][0] * l_ + _OK_M2[1][1] * m_ + _OK_M2[1][2] * s_
    ob: float = _OK_M2[2][0] * l_ + _OK_M2[2][1] * m_ + _OK_M2[2][2] * s_

    return (L, a, ob)


def oklab_to_srgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    """Convert Oklab to sRGB.

    Args:
        L: Lightness (0–1).
        a: Green–red axis.
        b: Blue–yellow axis.

    Returns:
        Tuple of (r, g, b) each in [0.0, 1.0], clamped to gamut.
    """
    # Oklab → LMS cube root.
    l_: float = _OK_M2_INV[0][0] * L + _OK_M2_INV[0][1] * a + _OK_M2_INV[0][2] * b
    m_: float = _OK_M2_INV[1][0] * L + _OK_M2_INV[1][1] * a + _OK_M2_INV[1][2] * b
    s_: float = _OK_M2_INV[2][0] * L + _OK_M2_INV[2][1] * a + _OK_M2_INV[2][2] * b

    # Cube (inverse of cube root).
    l: float = l_ * l_ * l_
    m: float = m_ * m_ * m_
    s: float = s_ * s_ * s_

    # LMS → linear RGB.
    rl: float = _OK_M1_INV[0][0] * l + _OK_M1_INV[0][1] * m + _OK_M1_INV[0][2] * s
    gl: float = _OK_M1_INV[1][0] * l + _OK_M1_INV[1][1] * m + _OK_M1_INV[1][2] * s
    bl: float = _OK_M1_INV[2][0] * l + _OK_M1_INV[2][1] * m + _OK_M1_INV[2][2] * s

    # Clamp and apply sRGB gamma.
    rl = max(0.0, min(1.0, rl))
    gl = max(0.0, min(1.0, gl))
    bl = max(0.0, min(1.0, bl))

    return (linear_to_srgb(rl), linear_to_srgb(gl), linear_to_srgb(bl))


# ---------------------------------------------------------------------------
# HSBK ↔ Oklab  (convenience: the full pipeline)
# ---------------------------------------------------------------------------

def hsbk_to_oklab(hue: int, sat: int, bri: int) -> tuple[float, float, float]:
    """Convert LIFX HSBK to Oklab in one call.

    Args:
        hue: LIFX hue (0–65535).
        sat: LIFX saturation (0–65535).
        bri: LIFX brightness (0–65535).

    Returns:
        Tuple of (L, a, b) in Oklab space.
    """
    h: float = hue / HSBK_MAX
    s: float = sat / HSBK_MAX
    b: float = bri / HSBK_MAX

    r, g, bl = hsb_to_srgb(h, s, b)
    return srgb_to_oklab(r, g, bl)


def oklab_to_hsbk(L: float, a: float, b: float, kelvin: int) -> tuple[int, int, int, int]:
    """Convert Oklab back to LIFX HSBK in one call.

    Args:
        L:      Lightness (0–1).
        a:      Green–red axis.
        b:      Blue–yellow axis.
        kelvin: Color temperature to preserve.

    Returns:
        An HSBK tuple (hue, sat, bri, kelvin), each channel 0–65535.
    """
    r, g, bl = oklab_to_srgb(L, a, b)
    h, s, bri = srgb_to_hsb(r, g, bl)

    return (
        int(h * HSBK_MAX) % (HSBK_MAX + 1),
        int(s * HSBK_MAX),
        int(bri * HSBK_MAX),
        kelvin,
    )


# ---------------------------------------------------------------------------
# HSBK ↔ CIELAB  (convenience: the full pipeline)
# ---------------------------------------------------------------------------

def hsbk_to_lab(hue: int, sat: int, bri: int) -> tuple[float, float, float]:
    """Convert LIFX HSBK to CIELAB in one call.

    Kelvin is ignored — it affects white point but not chromatic hue.

    Args:
        hue: LIFX hue (0–65535).
        sat: LIFX saturation (0–65535).
        bri: LIFX brightness (0–65535).

    Returns:
        Tuple of (L*, a*, b*) in CIELAB space.
    """
    h: float = hue / HSBK_MAX
    s: float = sat / HSBK_MAX
    b: float = bri / HSBK_MAX

    r, g, bl = hsb_to_srgb(h, s, b)
    x, y, z = srgb_to_xyz(r, g, bl)
    return xyz_to_lab(x, y, z)


def lab_to_hsbk(L: float, a: float, b: float, kelvin: int) -> tuple[int, int, int, int]:
    """Convert CIELAB back to LIFX HSBK in one call.

    Args:
        L:      Lightness (0–100).
        a:      Green–red axis.
        b:      Blue–yellow axis.
        kelvin: Color temperature to preserve in the output.

    Returns:
        An HSBK tuple (hue, sat, bri, kelvin), each channel 0–65535.
    """
    x, y, z = lab_to_xyz(L, a, b)
    r, g, bl = xyz_to_srgb(x, y, z)
    h, s, bri = srgb_to_hsb(r, g, bl)

    return (
        int(h * HSBK_MAX) % (HSBK_MAX + 1),
        int(s * HSBK_MAX),
        int(bri * HSBK_MAX),
        kelvin,
    )


# ---------------------------------------------------------------------------
# Interpolation in CIELAB
# ---------------------------------------------------------------------------

def lerp_lab(
    hsbk1: tuple[int, int, int, int],
    hsbk2: tuple[int, int, int, int],
    blend: float,
) -> tuple[int, int, int, int]:
    """Interpolate between two HSBK colors through CIELAB space.

    This produces perceptually uniform transitions — equal blend steps
    result in equal perceived color differences.  No brightness dips,
    no muddy intermediates, no uneven hue velocity.

    Args:
        hsbk1:  Start color as (hue, sat, bri, kelvin).
        hsbk2:  End color as (hue, sat, bri, kelvin).
        blend:  Blend factor 0.0 (pure hsbk1) to 1.0 (pure hsbk2).

    Returns:
        Interpolated HSBK tuple.  Kelvin is taken from hsbk1.
    """
    # Convert both endpoints to Lab.
    L1, a1, b1 = hsbk_to_lab(hsbk1[0], hsbk1[1], hsbk1[2])
    L2, a2, b2 = hsbk_to_lab(hsbk2[0], hsbk2[1], hsbk2[2])

    # Linear interpolation in Lab space.
    L: float = L1 + (L2 - L1) * blend
    a: float = a1 + (a2 - a1) * blend
    b: float = b1 + (b2 - b1) * blend

    return lab_to_hsbk(L, a, b, hsbk1[3])


# ---------------------------------------------------------------------------
# Interpolation in HSB (cheap fallback)
# ---------------------------------------------------------------------------

# Halfway point for shortest-path hue interpolation.
_HUE_HALF: int = (HSBK_MAX + 1) // 2


def lerp_hsb(
    hsbk1: tuple[int, int, int, int],
    hsbk2: tuple[int, int, int, int],
    blend: float,
) -> tuple[int, int, int, int]:
    """Interpolate between two HSBK colors via shortest-path HSB blending.

    Much cheaper than CIELAB — just linear interpolation on the raw HSBK
    channels with hue wrapping.  Produces adequate results for small color
    differences but exhibits brightness dips and muddy intermediates for
    large hue jumps.

    Args:
        hsbk1:  Start color as (hue, sat, bri, kelvin).
        hsbk2:  End color as (hue, sat, bri, kelvin).
        blend:  Blend factor 0.0 (pure hsbk1) to 1.0 (pure hsbk2).

    Returns:
        Interpolated HSBK tuple.  Kelvin is taken from hsbk1.
    """
    # Shortest-path hue interpolation.
    diff: int = hsbk2[0] - hsbk1[0]
    if diff > _HUE_HALF:
        diff -= (HSBK_MAX + 1)
    elif diff < -_HUE_HALF:
        diff += (HSBK_MAX + 1)
    hue: int = int(hsbk1[0] + diff * blend) % (HSBK_MAX + 1)

    sat: int = max(0, min(HSBK_MAX, int(hsbk1[1] + (hsbk2[1] - hsbk1[1]) * blend)))
    bri: int = max(0, min(HSBK_MAX, int(hsbk1[2] + (hsbk2[2] - hsbk1[2]) * blend)))

    return (hue, sat, bri, hsbk1[3])


# ---------------------------------------------------------------------------
# Interpolation in Oklab
# ---------------------------------------------------------------------------

def lerp_oklab(
    hsbk1: tuple[int, int, int, int],
    hsbk2: tuple[int, int, int, int],
    blend: float,
) -> tuple[int, int, int, int]:
    """Interpolate between two HSBK colors through Oklab space.

    More perceptually uniform than CIELAB, especially in blues and
    purples.  Simpler and faster math.  The recommended default.

    Args:
        hsbk1:  Start color as (hue, sat, bri, kelvin).
        hsbk2:  End color as (hue, sat, bri, kelvin).
        blend:  Blend factor 0.0 (pure hsbk1) to 1.0 (pure hsbk2).

    Returns:
        Interpolated HSBK tuple.  Kelvin is taken from hsbk1.
    """
    L1, a1, b1 = hsbk_to_oklab(hsbk1[0], hsbk1[1], hsbk1[2])
    L2, a2, b2 = hsbk_to_oklab(hsbk2[0], hsbk2[1], hsbk2[2])

    L: float = L1 + (L2 - L1) * blend
    a: float = a1 + (a2 - a1) * blend
    b: float = b1 + (b2 - b1) * blend

    return oklab_to_hsbk(L, a, b, hsbk1[3])


# ---------------------------------------------------------------------------
# Interpolation dispatch — global method switch
# ---------------------------------------------------------------------------

# Available interpolation methods, keyed by name.
LERP_METHODS: dict[str, callable] = {
    "oklab": lerp_oklab,
    "lab": lerp_lab,
    "hsb": lerp_hsb,
}

# Module-level switch — the active interpolation method name.
_lerp_method: str = "oklab"


def set_lerp_method(method: str) -> None:
    """Set the global color interpolation method.

    Args:
        method: One of ``"lab"`` (perceptually uniform, ~13x slower on Pi)
                or ``"hsb"`` (naive shortest-path, cheap).

    Raises:
        ValueError: If *method* is not a recognized name.
    """
    global _lerp_method
    if method not in LERP_METHODS:
        raise ValueError(
            f"Unknown lerp method '{method}'. "
            f"Available: {', '.join(sorted(LERP_METHODS))}"
        )
    _lerp_method = method


def get_lerp_method() -> str:
    """Return the name of the active interpolation method."""
    return _lerp_method


def lerp_color(
    hsbk1: tuple[int, int, int, int],
    hsbk2: tuple[int, int, int, int],
    blend: float,
) -> tuple[int, int, int, int]:
    """Interpolate between two HSBK colors using the active method.

    This is the standard entry point for all effect code.  The actual
    algorithm (CIELAB or HSB) is selected by :func:`set_lerp_method`
    and can be changed at runtime without touching any effect code.

    Args:
        hsbk1:  Start color as (hue, sat, bri, kelvin).
        hsbk2:  End color as (hue, sat, bri, kelvin).
        blend:  Blend factor 0.0 (pure hsbk1) to 1.0 (pure hsbk2).

    Returns:
        Interpolated HSBK tuple.  Kelvin is taken from hsbk1.
    """
    return LERP_METHODS[_lerp_method](hsbk1, hsbk2, blend)
