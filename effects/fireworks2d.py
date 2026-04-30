"""2D fireworks effect for LIFX matrix devices.

Shells detonate at random positions across the grid and bloom outward
as a 2D gaussian halo, fading through white-hot → chemical color →
warm orange.

The earlier "rocket-up" preamble (shell rising from the bottom edge
with an ease-out trail to a zenith in the upper portion of the grid)
has been removed.  On a ceiling-mounted fixture there is no notion of
"up" — every direction looks the same — so a directional ascent reads
as wrong and adds latency before the visual payoff.  The burst is the
interesting part; we go straight to it.

Multiple simultaneous shells blend **additively in RGB space** — the
same physically correct compositing used by the 1D fireworks effect.

Designed for LIFX matrix fixtures (Luna, Tile chains, the SuperColor
Ceiling) and the grid simulator.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "2.0"

import math
import random
from dataclasses import dataclass
from typing import Optional

from . import (
    DEVICE_TYPE_MATRIX,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Black — unlit pixel.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Default grid dimensions — match Luna (7×5).
DEFAULT_WIDTH: int = 7
DEFAULT_HEIGHT: int = 5

# Fade exponent for burst brightness over time.
# 1.4 lets the burst linger near full brightness before fading —
# the hallmark of a real BOOM (tuned in 1D fireworks).
BURST_FADE_EXPONENT: float = 1.4

# Saturation at the white-hot core of the burst (briefly held during
# the initial flash, then transitions to BURST_SATURATION as the
# chemical color blooms).  Low = white-hot.
HEAD_SATURATION: float = 0.10

# Saturation of the burst — maximum vivid color.
BURST_SATURATION: float = 1.0

# Brightness multiplier for burst zones before clamping.
# Over-drives the gaussian so fringe pixels stay visible.
BURST_BRIGHTNESS_BOOST: float = 2.5

# Initial 2D gaussian sigma for burst bloom (in pixels).
BURST_SIGMA_START: float = 1.5

# Divisor applied to burst_spread to compute final sigma.
BURST_SIGMA_DIVISOR: float = 2.0

# Temporal color evolution thresholds (fraction of burst duration).
BURST_WHITE_PHASE: float = 0.08     # white-hot flash
BURST_COLOR_PEAK: float = 0.35      # peak chemical color
BURST_COOL_START: float = 0.6       # cooling begins
BURST_COOL_HUE: float = 25.0        # warm orange target hue

# Brightness below which we skip a pixel (performance).
BURST_MIN_BRIGHTNESS: float = 0.005

# HSB color space sextant count for RGB conversion.
HUE_SEXTANTS: int = 6


# ---------------------------------------------------------------------------
# Shell state
# ---------------------------------------------------------------------------

@dataclass
class _Shell:
    """State for one fireworks shell — burst-only, no ascent.

    Attributes:
        burst_x:   Horizontal pixel position of the detonation.
        burst_y:   Vertical pixel position of the detonation.
        burst_t:   Global effect-time at moment of detonation.
        burst_hue: Explosion hue in degrees (0-360).
        burst_dur: Seconds for the burst to fade to black.
    """

    burst_x: float
    burst_y: float
    burst_t: float
    burst_hue: float
    burst_dur: float

    def is_done(self, t: float) -> bool:
        """Return True once the burst has fully faded.

        Args:
            t: Current global effect-time.

        Returns:
            ``True`` if this shell has no further contribution.
        """
        return (t - self.burst_t) >= self.burst_dur


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------

class Fireworks2D(Effect):
    """2D fireworks — shells detonate at random points and bloom outward.

    Each shell detonates at a random position anywhere on the grid;
    a circular gaussian bloom expands from that point and fades
    through white-hot → chemical color → warm orange.  No rocket
    ascent — the previous "shell rises from the bottom edge to a
    zenith in the upper portion" preamble assumed a wall-mounted
    fixture with a clear up/down.  On a ceiling-mounted matrix every
    direction is equivalent and the ascent reads as wrong.

    Multiple shells overlap additively in RGB space for physically
    correct color mixing.
    """

    name: str = "fireworks2d"
    description: str = "2D fireworks — bursts bloom from random points across the grid"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    width = Param(DEFAULT_WIDTH, min=1, max=500,
                  description="Grid width in pixels (columns)")
    height = Param(DEFAULT_HEIGHT, min=1, max=300,
                   description="Grid height in pixels (rows)")
    max_shells = Param(
        3, min=1, max=20,
        description="Maximum simultaneous bursts on screen",
    )
    launch_rate = Param(
        0.4, min=0.05, max=5.0,
        description="Average new bursts per second",
    )
    burst_spread = Param(
        6.0, min=1.0, max=30.0,
        description="Maximum burst radius in pixels from detonation point",
    )
    burst_duration = Param(
        1.8, min=0.2, max=8.0,
        description="Seconds for the burst to fade to black",
    )
    kelvin = Param(
        KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
        description="Color temperature in Kelvin",
    )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, **overrides: dict) -> None:
        """Initialize shell-tracking state.

        Args:
            **overrides: Parameter names mapped to override values.
        """
        super().__init__(**overrides)
        self._shells: list[_Shell] = []
        self._next_launch_t: float = 0.0
        self._last_t: Optional[float] = None

    def period(self) -> None:
        """Shell launches are random — no loopable cycle."""
        return None

    def on_start(self, zone_count: int) -> None:
        """Reset all shell state when the effect becomes active.

        Args:
            zone_count: Number of zones on the target device.
        """
        self._shells.clear()
        self._next_launch_t = 0.0
        self._last_t = None

    # ------------------------------------------------------------------
    # Shell management
    # ------------------------------------------------------------------

    def _spawn_shell(self, t: float, w: int, h: int) -> None:
        """Create and register a new shell that detonates immediately.

        The detonation point is uniformly random across the entire
        grid — no upper-region bias, no horizontal drift, no ascent.
        On a ceiling-mounted fixture this reads as fireworks viewed
        from directly below: bursts blooming everywhere, no
        gravity-implied direction.

        Args:
            t: Current global effect-time.
            w: Grid width in pixels.
            h: Grid height in pixels.
        """
        burst_x: float = random.uniform(0.0, max(w - 1, 0))
        burst_y: float = random.uniform(0.0, max(h - 1, 0))

        self._shells.append(_Shell(
            burst_x=burst_x,
            burst_y=burst_y,
            burst_t=t,
            burst_hue=random.uniform(0.0, 360.0),
            burst_dur=float(self.burst_duration),
        ))

    # ------------------------------------------------------------------
    # Per-shell contribution
    # ------------------------------------------------------------------

    def _contribution(
        self,
        shell: _Shell,
        t: float,
        w: int,
        h: int,
    ) -> list[tuple[float, float, float]]:
        """Compute this shell's ``(hue°, sat_01, bri_01)`` for every pixel.

        Pixels not affected by this shell have brightness ``0.0``.

        Args:
            shell: The shell to evaluate.
            t:     Current global effect-time.
            w:     Grid width.
            h:     Grid height.

        Returns:
            List of ``(hue_deg, sat_01, bri_01)`` per pixel (row-major).
        """
        total: int = w * h
        contrib: list[tuple[float, float, float]] = [(0.0, 0.0, 0.0)] * total

        age: float = t - shell.burst_t
        if age < 0 or age >= shell.burst_dur:
            return contrib

        # ----------------------------------------------------------
        # Burst phase — circular 2D gaussian bloom expanding from
        # the detonation point.  No ascent phase (rocket-up was
        # removed in v2.0; see module docstring for rationale).
        # ----------------------------------------------------------
        burst_frac: float = age / shell.burst_dur

        # Quadratic fade: bright flash then long slow dimming.
        fade: float = (1.0 - burst_frac) ** BURST_FADE_EXPONENT

        # 2D gaussian sigma expands over the burst lifetime.
        sigma: float = (
            BURST_SIGMA_START
            + burst_frac * float(self.burst_spread) / BURST_SIGMA_DIVISOR
        )
        two_sigma_sq: float = 2.0 * sigma * sigma

        # Temporal color evolution — same four phases as 1D.
        if burst_frac < BURST_WHITE_PHASE:
            # Initial flash — white-hot.
            zone_hue: float = shell.burst_hue
            zone_sat: float = HEAD_SATURATION
        elif burst_frac < BURST_COLOR_PEAK:
            # Ramp to full chemical color.
            ramp: float = (
                (burst_frac - BURST_WHITE_PHASE)
                / (BURST_COLOR_PEAK - BURST_WHITE_PHASE)
            )
            zone_hue = shell.burst_hue
            zone_sat = HEAD_SATURATION + (
                BURST_SATURATION - HEAD_SATURATION
            ) * ramp
        elif burst_frac < BURST_COOL_START:
            # Peak chemical color.
            zone_hue = shell.burst_hue
            zone_sat = BURST_SATURATION
        else:
            # Cooling toward warm orange.
            cool_frac: float = (
                (burst_frac - BURST_COOL_START)
                / (1.0 - BURST_COOL_START)
            )
            diff: float = BURST_COOL_HUE - shell.burst_hue
            if diff > 180.0:
                diff -= 360.0
            elif diff < -180.0:
                diff += 360.0
            zone_hue = (shell.burst_hue + diff * cool_frac) % 360.0
            zone_sat = BURST_SATURATION * (1.0 - 0.5 * cool_frac)

        for row in range(h):
            for col in range(w):
                dx: float = col - shell.burst_x
                dy: float = row - shell.burst_y
                dist_sq: float = dx * dx + dy * dy

                # 2D gaussian falloff from detonation point.
                gaussian: float = math.exp(-dist_sq / two_sigma_sq)
                bri: float = min(
                    1.0, fade * gaussian * BURST_BRIGHTNESS_BOOST,
                )

                if bri < BURST_MIN_BRIGHTNESS:
                    continue

                idx: int = row * w + col
                contrib[idx] = (zone_hue, zone_sat, bri)

        return contrib

    # ------------------------------------------------------------------
    # Main render
    # ------------------------------------------------------------------

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of 2D fireworks.

        Manages the shell lifecycle (spawn / expire), then composites
        all active shells using additive RGB blending.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels (ignored — uses width * height).

        Returns:
            A list of ``width * height`` HSBK tuples in row-major order.
        """
        w: int = int(self.width)
        h: int = int(self.height)
        total: int = w * h

        # First-frame initialization.
        if self._last_t is None:
            self._last_t = t
            self._next_launch_t = t
        self._last_t = t

        # Spawn when schedule says so and capacity allows.
        if t >= self._next_launch_t and len(self._shells) < int(self.max_shells):
            self._spawn_shell(t, w, h)
            self._next_launch_t = t + random.expovariate(
                max(float(self.launch_rate), 0.01)
            )

        # Remove fully-faded shells.
        self._shells = [s for s in self._shells if not s.is_done(t)]

        # Per-pixel RGB accumulators for additive compositing.
        px_r: list[float] = [0.0] * total
        px_g: list[float] = [0.0] * total
        px_b: list[float] = [0.0] * total

        for shell in self._shells:
            for i, (h_deg, s_01, b_01) in enumerate(
                self._contribution(shell, t, w, h)
            ):
                if b_01 <= 0.0:
                    continue
                r, g, bl = _hsb_to_rgb(h_deg, s_01, b_01)
                px_r[i] += r
                px_g[i] += g
                px_b[i] += bl

        # Convert accumulated RGB back to HSBK.
        kelvin_val: int = int(self.kelvin)
        colors: list[HSBK] = []
        for i in range(total):
            r: float = min(1.0, px_r[i])
            g: float = min(1.0, px_g[i])
            bl: float = min(1.0, px_b[i])
            if r + g + bl <= 0.0:
                colors.append(BLACK)
            else:
                h_deg, s_01, b_01 = _rgb_to_hsb(r, g, bl)
                colors.append((
                    hue_to_u16(h_deg),
                    int(s_01 * HSBK_MAX),
                    int(b_01 * HSBK_MAX),
                    kelvin_val,
                ))

        return colors


# ---------------------------------------------------------------------------
# Color space helpers (identical to fireworks.py — shared physics)
# ---------------------------------------------------------------------------

def _hsb_to_rgb(h_deg: float, s: float, b: float) -> tuple[float, float, float]:
    """Convert HSB (hue in degrees, saturation and brightness 0-1) to RGB 0-1.

    Args:
        h_deg: Hue in degrees (0-360).
        s:     Saturation (0.0-1.0).
        b:     Brightness (0.0-1.0).

    Returns:
        Tuple of ``(r, g, b)`` each in 0.0-1.0.
    """
    h: float = (h_deg / 360.0) * HUE_SEXTANTS
    c: float = b * s
    x: float = c * (1.0 - abs(h % 2.0 - 1.0))
    m: float = b - c

    sextant: int = int(h) % HUE_SEXTANTS
    if sextant == 0:
        return (c + m, x + m, m)
    elif sextant == 1:
        return (x + m, c + m, m)
    elif sextant == 2:
        return (m, c + m, x + m)
    elif sextant == 3:
        return (m, x + m, c + m)
    elif sextant == 4:
        return (x + m, m, c + m)
    else:
        return (c + m, m, x + m)


def _rgb_to_hsb(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Convert RGB (0-1) to HSB (hue in degrees, saturation and brightness 0-1).

    Args:
        r: Red (0.0-1.0).
        g: Green (0.0-1.0).
        b: Blue (0.0-1.0).

    Returns:
        Tuple of ``(hue_degrees, saturation, brightness)``.
    """
    max_c: float = max(r, g, b)
    min_c: float = min(r, g, b)
    delta: float = max_c - min_c

    bri: float = max_c

    if delta == 0.0:
        return (0.0, 0.0, bri)

    sat: float = delta / max_c

    if max_c == r:
        hue: float = 60.0 * (((g - b) / delta) % 6.0)
    elif max_c == g:
        hue = 60.0 * (((b - r) / delta) + 2.0)
    else:
        hue = 60.0 * (((r - g) / delta) + 4.0)

    if hue < 0.0:
        hue += 360.0

    return (hue, sat, bri)
