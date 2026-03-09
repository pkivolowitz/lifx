"""Fireworks effect for LIFX string lights.

Rockets launch from random ends of the strip, trail bright exhaust as
they decelerate toward their zenith, then detonate into an expanding halo
of color that fades to black.

Multiple simultaneous rockets blend **additively**: where two rockets
illuminate the same zone their brightnesses sum (clamped to maximum).
Hue is taken from whichever rocket contributes the most brightness to
that zone, so overlapping bursts paint the strip in layers of light
rather than fighting for a single color slot.

This effect is designed for multizone (string-light) LIFX devices.
On single-zone bulbs the effect degenerates to simple on-off flashes.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

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

# Total hue variation across a burst, in degrees.
# Zones to the left of zenith shift negatively; right shifts positively.
BURST_HUE_RANGE: float = 120.0

# Brightness threshold below which we skip writing a zone (performance).
BURST_MIN_BRIGHTNESS: float = 0.005

# Fraction of string length that is off-limits as zenith (from each end).
# Rockets always peak somewhere in the middle section of the strip.
ZENITH_MARGIN: float = 0.25

# Absolute minimum zenith travel distance in zones (guards tiny zone counts).
MIN_ZENITH_ZONES: int = 3


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
        z_order:     Monotonically increasing launch counter; higher value
                     wins when two rockets contribute equal brightness to
                     the same zone.
    """

    origin: int
    direction: int
    zenith: int
    launch_t: float
    ascent_dur: float
    burst_hue: float
    burst_dur: float
    z_order: int

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

    Multiple rockets overlap additively: brightness contributions are
    summed per zone (clamped to the LIFX maximum) so bright overlaps look
    even brighter rather than fighting for color priority.
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
        self._next_z_order: int = 0
        self._last_t: Optional[float] = None

    def on_start(self, zone_count: int) -> None:
        """Reset all rocket state when the effect becomes active.

        Args:
            zone_count: Number of zones on the target device.
        """
        self._rockets.clear()
        self._next_launch_t = 0.0
        self._next_z_order = 0
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
            z_order=self._next_z_order,
        ))
        self._next_z_order += 1

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
        expands outward while fading quadratically.  Zone hue varies
        linearly with signed distance from zenith so left/right sparks
        appear in complementary colors.

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

                for z in range(zone_count):
                    signed_dist: float = float(z - rocket.zenith)
                    dist_sq: float = signed_dist * signed_dist

                    # Gaussian falloff from zenith; boosted so fringe zones
                    # are pushed well above the visibility threshold.
                    gaussian: float = math.exp(-dist_sq / two_sigma_sq)
                    bri: float = min(1.0, fade * gaussian * BURST_BRIGHTNESS_BOOST)

                    if bri < BURST_MIN_BRIGHTNESS:
                        continue

                    # Hue shifts left/right of zenith in opposite directions
                    # so each arm of the burst shows complementary colors.
                    hue_shift: float = (
                        signed_dist * BURST_HUE_RANGE / max(self.burst_spread, 1)
                    )
                    zone_hue: float = (rocket.burst_hue + hue_shift) % 360.0

                    contrib[z] = (zone_hue, BURST_SATURATION, bri)

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

        # Per-zone accumulators (floating-point for headroom before clamping).
        zone_bri: list[float] = [0.0] * zone_count
        zone_hue: list[float] = [0.0] * zone_count
        zone_sat: list[float] = [0.0] * zone_count
        # Track which rocket contributes the dominant brightness per zone.
        zone_dom: list[float] = [0.0] * zone_count

        # Higher z_order was launched later; when two rockets contribute the
        # same brightness to a zone the newer one wins the hue slot.
        for rocket in sorted(self._rockets, key=lambda r: r.z_order):
            for z, (h, s, b) in enumerate(
                self._contribution(rocket, t, zone_count)
            ):
                if b <= 0.0:
                    continue
                # Additive brightness — sum, then clamp when converting to HSBK.
                zone_bri[z] = min(1.0, zone_bri[z] + b)
                # Hue/saturation: the strongest contributor wins.
                if b > zone_dom[z]:
                    zone_dom[z] = b
                    zone_hue[z] = h
                    zone_sat[z] = s

        # Convert accumulated floats to HSBK tuples.
        colors: list[HSBK] = []
        for z in range(zone_count):
            if zone_bri[z] > 0.0:
                h: int = hue_to_u16(zone_hue[z])
                s: int = int(zone_sat[z] * HSBK_MAX)
                b: int = int(zone_bri[z] * HSBK_MAX)
                colors.append((h, s, b, self.kelvin))
            else:
                colors.append((0, 0, 0, self.kelvin))

        return colors
