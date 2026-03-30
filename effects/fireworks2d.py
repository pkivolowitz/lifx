"""2D fireworks effect for LIFX matrix devices.

Shells launch from random positions along the bottom edge and rise with
ease-out deceleration (simulating drag / gravity).  At zenith they
detonate into a circular burst that expands as a 2D gaussian bloom and
fades through white-hot → chemical color → cooling orange.

Multiple simultaneous shells blend **additively in RGB space** — the
same physically correct compositing used by the 1D fireworks effect.

All ballistic and color-evolution lessons from the 1D fireworks are
preserved: ease-out ascent with white-hot head and trailing exhaust,
gaussian burst expansion, four-phase color evolution (white flash →
saturated chemical color → peak hold → warm orange cooldown), and
Poisson-process launch scheduling.

Designed for LIFX Tile matrices and the grid simulator.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

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

# Easing exponent applied to the ascent fraction.
# Higher = sharper deceleration as the shell approaches zenith.
EASE_EXPONENT: float = 2.0

# Quadratic decay exponent for the exhaust trail falloff.
TRAIL_EXPONENT: float = 2.0

# Fade exponent for burst brightness over time.
# 1.4 lets the burst linger near full brightness before fading —
# the hallmark of a real BOOM (tuned in 1D fireworks).
BURST_FADE_EXPONENT: float = 1.4

# Saturation of the shell head (low = white-hot).
HEAD_SATURATION: float = 0.10

# Saturation of the exhaust trail (slightly warmer than the head).
TRAIL_SATURATION: float = 0.25

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

# Fraction of grid height that is off-limits as zenith (from top).
# Shells always peak somewhere in the upper portion.
ZENITH_TOP_MARGIN: float = 0.10

# Minimum fraction of grid height that a shell must travel.
ZENITH_MIN_TRAVEL: float = 0.30

# HSB color space sextant count for RGB conversion.
HUE_SEXTANTS: int = 6

# Trail width perpendicular to ascent direction (pixels).
# Gives the exhaust trail a visible body instead of a single-pixel line.
TRAIL_HALF_WIDTH: float = 0.8


# ---------------------------------------------------------------------------
# Shell state
# ---------------------------------------------------------------------------

@dataclass
class _Shell:
    """State for one fireworks shell from launch through burst fade.

    Attributes:
        launch_x:   Horizontal pixel position at launch.
        launch_y:   Vertical pixel position at launch (bottom edge).
        zenith_x:   Horizontal position at detonation.
        zenith_y:   Vertical position at detonation.
        launch_t:   Global effect-time at moment of launch.
        ascent_dur: Seconds from launch to zenith.
        burst_hue:  Explosion hue in degrees (0-360).
        burst_dur:  Seconds for the burst to fade to black.
    """

    launch_x: float
    launch_y: float
    zenith_x: float
    zenith_y: float
    launch_t: float
    ascent_dur: float
    burst_hue: float
    burst_dur: float

    def is_done(self, t: float) -> bool:
        """Return True once the burst has fully faded.

        Args:
            t: Current global effect-time.

        Returns:
            ``True`` if this shell has no further contribution.
        """
        return (t - self.launch_t) >= (self.ascent_dur + self.burst_dur)


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------

class Fireworks2D(Effect):
    """2D fireworks — shells rise and burst into expanding circular halos.

    Each shell:

    1. Launches from a random position along the bottom edge.
    2. Rises with ease-out deceleration toward a random zenith in the
       upper portion of the grid, trailing white-hot exhaust.
    3. Detonates at zenith: a circular gaussian bloom expands outward
       and fades through white-hot → chemical color → warm orange.

    Multiple shells overlap additively in RGB space for physically
    correct color mixing.
    """

    name: str = "fireworks2d"
    description: str = "2D fireworks — shells rise and burst into circular halos"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    width = Param(DEFAULT_WIDTH, min=1, max=500,
                  description="Grid width in pixels (columns)")
    height = Param(DEFAULT_HEIGHT, min=1, max=300,
                   description="Grid height in pixels (rows)")
    max_shells = Param(
        3, min=1, max=20,
        description="Maximum simultaneous shells in flight",
    )
    launch_rate = Param(
        0.4, min=0.05, max=5.0,
        description="Average new shells launched per second",
    )
    ascent_speed = Param(
        6.0, min=1.0, max=40.0,
        description="Rise speed in pixels per second",
    )
    trail_length = Param(
        4.0, min=0.5, max=20.0,
        description="Exhaust trail length in pixels",
    )
    burst_spread = Param(
        6.0, min=1.0, max=30.0,
        description="Maximum burst radius in pixels from zenith",
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
        """Create and register a new shell from the bottom edge.

        The shell launches from a random X along the bottom row and
        rises to a zenith in the upper portion of the grid.  A slight
        horizontal drift gives the trajectory a natural arc.

        Args:
            t: Current global effect-time.
            w: Grid width in pixels.
            h: Grid height in pixels.
        """
        # Launch from a random X along the bottom edge.
        launch_x: float = random.uniform(0.0, w - 1.0)
        launch_y: float = float(h - 1)

        # Zenith: upper portion of the grid.
        min_y: int = max(0, int(h * ZENITH_TOP_MARGIN))
        max_y: int = max(min_y, int(h * (1.0 - ZENITH_MIN_TRAVEL)) - 1)
        zenith_y: float = float(random.randint(min_y, max(min_y, max_y)))

        # Slight horizontal drift — shells don't rise perfectly straight.
        drift: float = random.uniform(-w * 0.15, w * 0.15)
        zenith_x: float = max(0.0, min(w - 1.0, launch_x + drift))

        # Ascent duration = vertical distance / speed.
        vert_dist: float = launch_y - zenith_y
        ascent_dur: float = vert_dist / max(float(self.ascent_speed), 1.0)

        self._shells.append(_Shell(
            launch_x=launch_x,
            launch_y=launch_y,
            zenith_x=zenith_x,
            zenith_y=zenith_y,
            launch_t=t,
            ascent_dur=ascent_dur,
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

        age: float = t - shell.launch_t
        if age < 0:
            return contrib

        if age < shell.ascent_dur:
            # ----------------------------------------------------------
            # Ascent phase
            # ----------------------------------------------------------
            frac: float = age / shell.ascent_dur

            # Ease-out: fast start, slowing finish.
            eased: float = 1.0 - (1.0 - frac) ** EASE_EXPONENT

            # Current head position (interpolated along trajectory).
            head_x: float = shell.launch_x + (shell.zenith_x - shell.launch_x) * eased
            head_y: float = shell.launch_y + (shell.zenith_y - shell.launch_y) * eased

            # Direction vector (normalized) for trail computation.
            dx: float = shell.zenith_x - shell.launch_x
            dy: float = shell.zenith_y - shell.launch_y
            traj_len: float = math.sqrt(dx * dx + dy * dy)
            if traj_len < 0.01:
                return contrib
            nx: float = dx / traj_len
            ny: float = dy / traj_len

            trail_len: float = float(self.trail_length)

            for row in range(h):
                for col in range(w):
                    # Vector from head to this pixel.
                    px: float = col - head_x
                    py: float = row - head_y

                    # Project onto trajectory axis:
                    # behind > 0 means pixel is behind the head (in the trail).
                    behind: float = -(px * nx + py * ny)

                    # Perpendicular distance from trajectory line.
                    perp: float = abs(px * ny - py * nx)

                    if perp > TRAIL_HALF_WIDTH + 0.5:
                        # Too far from the trajectory line.
                        continue

                    # Width falloff — pixels off-axis are dimmer.
                    width_atten: float = max(
                        0.0, 1.0 - perp / (TRAIL_HALF_WIDTH + 0.5)
                    )

                    idx: int = row * w + col

                    if -0.7 <= behind <= 0.7:
                        # At the shell head — white-hot, full brightness.
                        contrib[idx] = (
                            shell.burst_hue,
                            HEAD_SATURATION,
                            1.0 * width_atten,
                        )
                    elif 0.7 < behind <= trail_len:
                        # In the exhaust trail — quadratic decay.
                        trail_frac: float = (behind - 0.7) / trail_len
                        bri: float = (
                            (1.0 - trail_frac) ** TRAIL_EXPONENT * width_atten
                        )
                        if bri > BURST_MIN_BRIGHTNESS:
                            contrib[idx] = (
                                shell.burst_hue, TRAIL_SATURATION, bri,
                            )

        else:
            burst_age: float = age - shell.ascent_dur
            if burst_age < shell.burst_dur:
                # ----------------------------------------------------------
                # Burst phase — circular 2D gaussian bloom
                # ----------------------------------------------------------
                burst_frac: float = burst_age / shell.burst_dur

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
                        dx: float = col - shell.zenith_x
                        dy: float = row - shell.zenith_y
                        dist_sq: float = dx * dx + dy * dy

                        # 2D gaussian falloff from zenith point.
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
