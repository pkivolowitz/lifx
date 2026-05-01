"""Old-fashioned radar scope — rotating sweep line with a fading phosphor tail.

A line rotates clockwise around the grid centre.  The leading tip is
fully bright; cells the sweep recently passed glow with a linearly
decaying tail that fades to dark over the configured trailing arc.
Cells just ahead of the sweep (about to be touched) are dark, then
flash on as the line crosses their angle.

Stateless rendering — every cell's brightness is a closed-form
function of (current sweep angle, cell angle, time-since-last-sweep).
No accumulated state means no frame-rate dependence and no drift
under skipped frames.

Hue / rate / brightness semantics match the rest of the matrix
effect family (omit ``--hue`` for the OkLab brownian-walk auto-cycle;
``--rate slow|medium|fast`` sets the rotation period).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import math
from typing import Optional

from . import (
    DEVICE_TYPE_MATRIX,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT,
    hue_to_u16, pct_to_u16,
)
from ._walkers import (
    ALLOWED_RATES, HUE_AUTO_SENTINEL, HueWalker,
    RATE_FAST, RATE_MEDIUM, RATE_SLOW,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default playfield geometry — matches the SuperColor Ceiling.
DEFAULT_WIDTH: int = 8
DEFAULT_HEIGHT: int = 8

# Default brightness — matches the rest of the matrix family.
DEFAULT_BRIGHTNESS_PCT: int = 80

# Black HSBK — emitted for unlit cells.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Tau — full rotation in radians.  math.tau exists since 3.6 but
# define a local alias so the per-cell math doesn't repeatedly read
# the math module attribute.
TAU: float = 2.0 * math.pi

# Length of the phosphor tail behind the sweep, in radians.  3π/2 =
# 270° leaves a small dark arc just ahead of the leading edge so the
# sweep direction reads clearly; a tail of TAU would cover the whole
# disc and erase the directional cue.  Tweak only if the dark gap
# feels too small (raise) or the trail looks too long (lower).
TAIL_ANGLE_RAD: float = 1.5 * math.pi

# Rotation period (seconds for one full sweep) per --rate choice.
# Old CRT radar scopes ran 1–3 RPM (60–20 s/rev); for an animated
# household effect the longer end is sleep-inducing, so the slow
# choice tightens that to 8 s.  Fast pushes to 2 s — a brisk sweep
# without becoming dizzying on a small grid.
RATE_ROTATION_PERIOD_SEC: dict[str, float] = {
    RATE_SLOW:   8.0,
    RATE_MEDIUM: 4.0,
    RATE_FAST:   2.0,
}

# Maximum radial extent of the sweep relative to the inscribed
# half-extent.  1.0 = inscribed circle (corners stay dark — a clean
# round scope shape); larger values flash the corners as the sweep
# crosses them.  1.0 reads more "radar" so it's the choice.
MAX_RADIUS_FRAC: float = 1.0

# Centre cell sentinel — cells closer than this to the geometric
# centre are always lit fully (their angular position is ill-defined
# and they should pulse with the sweep regardless).  Half a cell is
# the natural threshold.
CENTRE_R_THRESHOLD: float = 0.5


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------


class RadarScope(Effect):
    """Rotating sweep line with a linearly-fading phosphor tail."""

    name: str = "radar_scope"
    description: str = "Old-fashioned radar scope — rotating line with fading tail"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    width = Param(
        DEFAULT_WIDTH, min=4, max=64,
        description="Playfield width in cells",
    )
    height = Param(
        DEFAULT_HEIGHT, min=4, max=64,
        description="Playfield height in cells",
    )
    rate = Param(
        RATE_MEDIUM, choices=ALLOWED_RATES,
        description=(
            "Rotation period: slow=8s, medium=4s, fast=2s per "
            "full sweep."
        ),
    )
    hue = Param(
        HUE_AUTO_SENTINEL, min=HUE_AUTO_SENTINEL, max=360,
        description=(
            "Live-pixel hue in degrees.  Omit (or pass -1) to drift "
            "the global hue via brownian walk in OkLab — uniform "
            "20°-50° per leg, ~12 s per leg."
        ),
    )
    brightness = Param(
        DEFAULT_BRIGHTNESS_PCT, min=1, max=100,
        description="Live-pixel peak brightness (percent)",
    )

    def on_start(self, zone_count: int) -> None:
        """Cache geometry, rotation rate, and hue source."""
        self._w: int = int(self.width)
        self._h: int = int(self.height)
        self._cx: float = (self._w - 1) / 2.0
        self._cy: float = (self._h - 1) / 2.0
        # Inscribed half-extent — half the smaller dimension.  Use
        # this as the max sweep radius so a non-square grid still
        # gives a clean circular scope shape.
        self._max_radius: float = (
            min(self._w - 1, self._h - 1) / 2.0 * MAX_RADIUS_FRAC
        )
        self._rotation_period: float = RATE_ROTATION_PERIOD_SEC[str(self.rate)]
        # Angular velocity in rad/sec — positive = clockwise visually
        # (screen y-axis is inverted, so atan2 already returns the
        # visually-clockwise angle increment).
        self._omega: float = TAU / self._rotation_period

        # Hue source — same convention as the other matrix effects.
        if int(self.hue) < 0:
            self._hue_walker: Optional[HueWalker] = HueWalker()
            self._static_hue_u16: int = 0
        else:
            self._hue_walker = None
            self._static_hue_u16 = hue_to_u16(float(self.hue))

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the rotating sweep + fading tail.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels on the target device.

        Returns:
            ``zone_count`` HSBK tuples in row-major order.  Cells past
            the configured playfield are emitted black.
        """
        if not hasattr(self, "_w"):
            return [BLACK] * zone_count

        # Current sweep angle modulo a full revolution.
        theta: float = (self._omega * t) % TAU

        if self._hue_walker is not None:
            hue_u16: int = self._hue_walker.hue_u16(t)
        else:
            hue_u16 = self._static_hue_u16
        bri_max: int = pct_to_u16(int(self.brightness))

        colors: list[HSBK] = [BLACK] * zone_count
        for r in range(self._h):
            dy: float = r - self._cy
            for c in range(self._w):
                dx: float = c - self._cx
                radius: float = math.hypot(dx, dy)
                if radius > self._max_radius:
                    continue  # outside the scope's circle
                if radius < CENTRE_R_THRESHOLD:
                    # Centre always lit — its angle is meaningless and
                    # it should pulse with every sweep regardless.
                    bri: int = bri_max
                else:
                    cell_angle: float = math.atan2(dy, dx) % TAU
                    # Angular distance the sweep has rotated past this
                    # cell since it was last touched.  Mod TAU keeps
                    # the walk on the circle.
                    delta: float = (theta - cell_angle) % TAU
                    if delta > TAIL_ANGLE_RAD:
                        continue  # beyond the trail — dark
                    # Linear decay from full at the leading edge
                    # (delta=0) to zero at the trail end.
                    fade: float = 1.0 - delta / TAIL_ANGLE_RAD
                    bri = int(bri_max * fade)
                if bri <= 0:
                    continue
                idx: int = r * self._w + c
                if 0 <= idx < zone_count:
                    colors[idx] = (hue_u16, HSBK_MAX, bri, KELVIN_DEFAULT)
        return colors

    def period(self) -> Optional[float]:
        """One full sweep is the natural cycle."""
        return self._rotation_period if hasattr(self, "_rotation_period") else None
