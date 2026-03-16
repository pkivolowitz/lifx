"""Fireworks effect for LIFX string lights.

Rockets launch from random ends of the strip, trail bright exhaust as
they decelerate toward their zenith, then detonate into an expanding halo
of color that fades to black.

Multiple simultaneous rockets blend **additively in RGB space**: each
rocket's HSB contribution is converted to linear RGB, the channels are
summed per zone, and the result is converted back to HSB.  This is
physically correct — overlapping red and green bursts produce yellow,
two reds produce brighter red, just like real light.

This effect is designed for multizone (string-light) LIFX devices.
On single-zone bulbs the effect degenerates to simple on-off flashes.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.3"

import math
import random
from dataclasses import dataclass
from typing import Optional

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Easing exponent applied to the ascent fraction.
# Higher = sharper deceleration as the rocket approaches zenith.
EASE_EXPONENT: float = 2.0

# Quadratic decay exponent for the exhaust trail falloff.
TRAIL_EXPONENT: float = 2.0

# Fade exponent for the burst brightness over time.
# Lowered from 2.0 to 1.4 so the burst lingers near full brightness
# for longer before dropping off — the hallmark of a real BOOM.
BURST_FADE_EXPONENT: float = 1.4

# Saturation of the rocket head (low = white-hot).
HEAD_SATURATION: float = 0.10

# Saturation of the exhaust trail (slightly warmer than the head).
TRAIL_SATURATION: float = 0.25

# Saturation of the burst — maximum vivid color.
BURST_SATURATION: float = 1.0

# Brightness multiplier applied to burst zones before clamping.
# Values > 1.0 over-drive the gaussian so even the fringes appear bright.
BURST_BRIGHTNESS_BOOST: float = 2.5

# Initial gaussian sigma for the burst bloom (in zones).
# The bloom expands over time toward burst_spread / BURST_SIGMA_DIVISOR.
BURST_SIGMA_START: float = 5.0
BURST_SIGMA_DIVISOR: float = 2.0

# Temporal color evolution of a burst (simulates star cooling).
# At ignition the stars are white-hot; they peak at their chemical color
# then cool through warm orange toward burnout.
BURST_WHITE_PHASE: float = 0.08    # fraction of burst spent white-hot
BURST_COLOR_PEAK: float = 0.35     # fraction at which chemical color is purest
BURST_COOL_HUE: float = 25.0      # hue degrees of the cooling tail (warm orange)
BURST_COOL_START: float = 0.6      # fraction at which cooling toward orange begins

# Brightness threshold below which we skip writing a zone (performance).
BURST_MIN_BRIGHTNESS: float = 0.005

# Fraction of string length that is off-limits as zenith (from each end).
# Rockets always peak somewhere in the middle section of the strip.
ZENITH_MARGIN: float = 0.25

# Absolute minimum zenith travel distance in zones (guards tiny zone counts).
MIN_ZENITH_ZONES: int = 3

# HSB color space has 6 sextants (60 degrees each).  Used by the RGB
# conversion helpers for additive compositing.
HUE_SEXTANTS: int = 6


# ---------------------------------------------------------------------------
# Rocket state
# ---------------------------------------------------------------------------

@dataclass
class _Rocket:
    """Complete state for one rocket from launch through burst-fade.

    Attributes:
        origin:      Zone index from which the rocket launches (0 or end).
        direction:   Travel direction: ``+1`` = rightward, ``-1`` = leftward.
        zenith:      Zone index at which the rocket peaks and bursts.
        launch_t:    Global effect-time at the moment of launch.
        ascent_dur:  Seconds from launch to zenith.
        burst_hue:   Hue of the explosion in degrees (0-360).
        burst_dur:   Seconds for the burst to fade completely to black.
    """

    origin: int
    direction: int
    zenith: int
    launch_t: float
    ascent_dur: float
    burst_hue: float
    burst_dur: float

    def is_done(self, t: float) -> bool:
        """Return True once the burst has fully faded.

        Args:
            t: Current global effect-time.

        Returns:
            ``True`` if this rocket has no further contribution.
        """
        return (t - self.launch_t) >= (self.ascent_dur + self.burst_dur)


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------

class Fireworks(Effect):
    """Rockets from both ends burst into spreading color halos.

    Each rocket:

    1. Launches from zone 0 or the last zone (chosen at random).
    2. Accelerates away, then decelerates as it approaches a random
       zenith in the middle third of the strip — producing the classic
       "slowing rocket" look with a bright head and fading exhaust trail.
    3. Detonates at zenith: a gaussian bloom of color expands outward in
       both directions and fades quadratically to black.

    Multiple rockets overlap additively in RGB space: each rocket's
    color contribution is summed as linear RGB per zone, producing
    physically correct color mixing (red + green = yellow, etc.).
    """

    name: str = "fireworks"
    description: str = "Rockets from both ends burst into spreading color halos"

    max_rockets = Param(
        3, min=1, max=20,
        description="Maximum number of simultaneous rockets in flight",
    )
    launch_rate = Param(
        0.5, min=0.05, max=5.0,
        description="Average new rockets launched per second",
    )
    ascent_speed = Param(
        10.0, min=1.0, max=60.0,
        description="Rocket travel speed in zones per second",
    )
    trail_length = Param(
        8, min=1, max=30,
        description="Exhaust trail length in zones",
    )
    burst_spread = Param(
        20, min=2, max=60,
        description="Maximum burst radius in zones from zenith",
    )
    burst_duration = Param(
        1.8, min=0.2, max=8.0,
        description="Seconds for the burst to fade completely to black",
    )
    kelvin = Param(
        KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
        description="Color temperature in Kelvin",
    )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, **overrides: dict) -> None:
        """Initialize rocket-tracking state.

        Args:
            **overrides: Parameter names mapped to override values.
        """
        super().__init__(**overrides)
        self._rockets: list[_Rocket] = []
        self._next_launch_t: float = 0.0
        self._last_t: Optional[float] = None

    def period(self) -> None:
        """Fireworks launches are random — no loopable cycle."""
        return None

    def on_start(self, zone_count: int) -> None:
        """Reset all rocket state when the effect becomes active.

        Args:
            zone_count: Number of zones on the target device.
        """
        self._rockets.clear()
        self._next_launch_t = 0.0
        self._last_t = None

    # ------------------------------------------------------------------
    # Rocket management
    # ------------------------------------------------------------------

    def _spawn_rocket(self, t: float, zone_count: int) -> None:
        """Create and register a new rocket from a randomly chosen end.

        The zenith is chosen randomly within the middle portion of the
        strip, keeping clear of the ZENITH_MARGIN fraction at each end
        so every rocket has a visible ascent phase.

        Args:
            t:          Current global effect-time.
            zone_count: Number of zones (used for bounds).
        """
        # Pick launch end at random.
        from_left: bool = random.random() < 0.5
        origin: int = 0 if from_left else zone_count - 1
        direction: int = 1 if from_left else -1

        # Zenith lives in the middle section of the strip.
        min_travel: int = max(MIN_ZENITH_ZONES, int(zone_count * ZENITH_MARGIN))
        max_travel: int = int(zone_count * (1.0 - ZENITH_MARGIN))

        # Guard against degenerate zone counts.
        if max_travel <= min_travel:
            max_travel = min_travel + 1

        zenith_dist: int = random.randint(min_travel, max_travel)
        zenith: int = max(0, min(zone_count - 1, origin + direction * zenith_dist))

        # Ascent duration = distance / speed.
        actual_dist: int = abs(zenith - origin)
        ascent_dur: float = actual_dist / max(self.ascent_speed, 1.0)

        self._rockets.append(_Rocket(
            origin=origin,
            direction=direction,
            zenith=zenith,
            launch_t=t,
            ascent_dur=ascent_dur,
            burst_hue=random.uniform(0.0, 360.0),
            burst_dur=self.burst_duration,
        ))

    # ------------------------------------------------------------------
    # Per-rocket rendering
    # ------------------------------------------------------------------

    def _contribution(
        self,
        rocket: _Rocket,
        t: float,
        zone_count: int,
    ) -> list[tuple[float, float, float]]:
        """Compute this rocket's ``(hue°, sat_01, bri_01)`` for every zone.

        Zones not affected by this rocket have brightness ``0.0``.

        The ascent phase uses ease-out motion so the rocket decelerates
        near the zenith.  The exhaust trail is a quadratic falloff behind
        the head.

        The burst phase uses a gaussian bloom centred on the zenith that
        expands outward while fading quadratically.  All stars in the
        shell share the same hue at any instant — color evolves temporally
        from white-hot flash through peak chemical color to warm orange
        cooldown, mimicking real pyrotechnic star combustion.

        Args:
            rocket:     The rocket to evaluate.
            t:          Current global effect-time.
            zone_count: Total number of zones.

        Returns:
            List of ``(hue_deg, sat_01, bri_01)`` per zone.
        """
        # Pre-fill with darkness.
        contrib: list[tuple[float, float, float]] = [(0.0, 0.0, 0.0)] * zone_count

        age: float = t - rocket.launch_t
        if age < 0:
            return contrib

        if age < rocket.ascent_dur:
            # ----------------------------------------------------------
            # Ascent phase
            # ----------------------------------------------------------
            frac: float = age / rocket.ascent_dur            # 0 → 1

            # Ease-out: fast start, slowing finish.
            eased: float = 1.0 - (1.0 - frac) ** EASE_EXPONENT

            dist_to_zenith: int = abs(rocket.zenith - rocket.origin)
            head_pos: float = rocket.origin + rocket.direction * dist_to_zenith * eased

            for z in range(zone_count):
                # Positive = zone is behind the head (in the trail direction).
                behind: float = rocket.direction * (head_pos - z)

                if -0.5 <= behind <= 0.5:
                    # Zone is at the rocket head — white-hot, full brightness.
                    contrib[z] = (rocket.burst_hue, HEAD_SATURATION, 1.0)

                elif 0.5 < behind <= self.trail_length:
                    # Zone is in the exhaust trail — quadratic decay.
                    trail_frac: float = (behind - 0.5) / self.trail_length
                    bri: float = (1.0 - trail_frac) ** TRAIL_EXPONENT
                    contrib[z] = (rocket.burst_hue, TRAIL_SATURATION, bri)

        else:
            burst_age: float = age - rocket.ascent_dur
            if burst_age < rocket.burst_dur:
                # ----------------------------------------------------------
                # Burst phase
                # ----------------------------------------------------------
                burst_frac: float = burst_age / rocket.burst_dur  # 0 → 1

                # Quadratic fade: bright flash then long slow dimming.
                fade: float = (1.0 - burst_frac) ** BURST_FADE_EXPONENT

                # Gaussian sigma expands over the burst lifetime.
                sigma: float = (
                    BURST_SIGMA_START
                    + burst_frac * self.burst_spread / BURST_SIGMA_DIVISOR
                )
                two_sigma_sq: float = 2.0 * sigma * sigma

                # Temporal color evolution — all stars in the shell share
                # the same hue at any given instant, evolving over time:
                #   1. White-hot flash (low saturation)
                #   2. Peak chemical color (full saturation)
                #   3. Cooling toward warm orange as stars burn out
                if burst_frac < BURST_WHITE_PHASE:
                    # Initial flash — white-hot.
                    zone_hue: float = rocket.burst_hue
                    zone_sat: float = HEAD_SATURATION
                elif burst_frac < BURST_COLOR_PEAK:
                    # Ramp up to full chemical color.
                    ramp: float = (
                        (burst_frac - BURST_WHITE_PHASE)
                        / (BURST_COLOR_PEAK - BURST_WHITE_PHASE)
                    )
                    zone_hue = rocket.burst_hue
                    zone_sat = HEAD_SATURATION + (BURST_SATURATION - HEAD_SATURATION) * ramp
                elif burst_frac < BURST_COOL_START:
                    # Holding at peak chemical color.
                    zone_hue = rocket.burst_hue
                    zone_sat = BURST_SATURATION
                else:
                    # Cooling: hue drifts toward warm orange, saturation drops.
                    cool_frac: float = (
                        (burst_frac - BURST_COOL_START)
                        / (1.0 - BURST_COOL_START)
                    )
                    # Shortest-path hue drift toward BURST_COOL_HUE.
                    diff: float = BURST_COOL_HUE - rocket.burst_hue
                    if diff > 180.0:
                        diff -= 360.0
                    elif diff < -180.0:
                        diff += 360.0
                    zone_hue = (rocket.burst_hue + diff * cool_frac) % 360.0
                    zone_sat = BURST_SATURATION * (1.0 - 0.5 * cool_frac)

                for z in range(zone_count):
                    dist_sq: float = float(z - rocket.zenith) ** 2

                    # Gaussian falloff from zenith; boosted so fringe zones
                    # are pushed well above the visibility threshold.
                    gaussian: float = math.exp(-dist_sq / two_sigma_sq)
                    bri: float = min(1.0, fade * gaussian * BURST_BRIGHTNESS_BOOST)

                    if bri < BURST_MIN_BRIGHTNESS:
                        continue

                    contrib[z] = (zone_hue, zone_sat, bri)

        return contrib

    # ------------------------------------------------------------------
    # Main render
    # ------------------------------------------------------------------

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of fireworks.

        Manages the rocket lifecycle (spawn / expire), then composites all
        active rockets onto the zone array using additive brightness blending.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        # First-frame initialization.
        if self._last_t is None:
            self._last_t = t
            self._next_launch_t = t
        self._last_t = t

        # Spawn a new rocket when the schedule says so and capacity allows.
        if t >= self._next_launch_t and len(self._rockets) < self.max_rockets:
            self._spawn_rocket(t, zone_count)
            # Exponential inter-arrival times approximate a Poisson process.
            self._next_launch_t = t + random.expovariate(self.launch_rate)

        # Remove fully-faded rockets to keep the active list short.
        self._rockets = [r for r in self._rockets if not r.is_done(t)]

        # Per-zone RGB accumulators.  Additive mixing is physically correct
        # in linear RGB: overlapping red + green bursts produce yellow, two
        # reds produce brighter red.  Accumulated values are clamped to 1.0
        # per channel before conversion back to HSB.
        zone_r: list[float] = [0.0] * zone_count
        zone_g: list[float] = [0.0] * zone_count
        zone_b: list[float] = [0.0] * zone_count

        for rocket in self._rockets:
            for z, (h_deg, s_01, b_01) in enumerate(
                self._contribution(rocket, t, zone_count)
            ):
                if b_01 <= 0.0:
                    continue
                # Convert this rocket's HSB contribution to linear RGB
                # so the addition is physically meaningful.
                r, g, bl = _hsb_to_rgb(h_deg, s_01, b_01)
                zone_r[z] += r
                zone_g[z] += g
                zone_b[z] += bl

        # Convert accumulated RGB back to HSBK tuples.
        colors: list[HSBK] = []
        for z in range(zone_count):
            r: float = min(1.0, zone_r[z])
            g: float = min(1.0, zone_g[z])
            bl: float = min(1.0, zone_b[z])
            if r + g + bl <= 0.0:
                colors.append((0, 0, 0, self.kelvin))
            else:
                h_deg, s_01, b_01 = _rgb_to_hsb(r, g, bl)
                h_u16: int = hue_to_u16(h_deg)
                s_u16: int = int(s_01 * HSBK_MAX)
                b_u16: int = int(b_01 * HSBK_MAX)
                colors.append((h_u16, s_u16, b_u16, self.kelvin))

        return colors


# ---------------------------------------------------------------------------
# Color space helpers for additive compositing
# ---------------------------------------------------------------------------

def _hsb_to_rgb(h_deg: float, s: float, b: float) -> tuple[float, float, float]:
    """Convert HSB (hue in degrees, saturation and brightness 0-1) to RGB 0-1.

    Uses the standard sextant algorithm.

    Args:
        h_deg: Hue in degrees (0-360).
        s:     Saturation (0.0-1.0).
        b:     Brightness (0.0-1.0).

    Returns:
        Tuple of ``(r, g, b)`` each in 0.0-1.0.
    """
    h: float = (h_deg / 360.0) * HUE_SEXTANTS  # 0.0 - 6.0
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

    # Brightness is the maximum channel.
    bri: float = max_c

    if delta == 0.0:
        return (0.0, 0.0, bri)

    # Saturation.
    sat: float = delta / max_c

    # Hue.
    if max_c == r:
        hue: float = 60.0 * (((g - b) / delta) % 6.0)
    elif max_c == g:
        hue = 60.0 * (((b - r) / delta) + 2.0)
    else:
        hue = 60.0 * (((r - g) / delta) + 4.0)

    if hue < 0.0:
        hue += 360.0

    return (hue, sat, bri)
