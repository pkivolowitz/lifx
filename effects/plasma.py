"""Plasma ball effect — electric tendrils reaching from a central point.

Simulates a Van de Graaff / plasma globe where bright arcs extend from a
central hot core toward both ends of the strip.  Each tendril is a random
walk biased toward the endpoints, forking occasionally.  Brightness follows
a 1/r falloff from the core, with stochastic flicker on each tendril.

The core sits at the center of the strip and pulses slowly.  Tendrils are
regenerated frequently — they flash into existence, crackle outward, then
die — giving the characteristic plasma ball look of constantly shifting
discharge paths.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
import random
from dataclasses import dataclass, field
from typing import Optional

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TWO_PI: float = 2.0 * math.pi

# Default zones per bulb for polychrome-aware rendering.
DEFAULT_ZPB: int = 1

# Core glow radius as fraction of strip length.
CORE_RADIUS_FRAC: float = 0.08

# Core pulse frequency multiplier (pulses per speed cycle).
CORE_PULSE_FREQ: float = 3.0

# Core brightness range (fraction of max).
CORE_BRI_MIN: float = 0.6
CORE_BRI_MAX: float = 1.0

# Tendril brightness decay exponent (higher = faster falloff from core).
TENDRIL_DECAY_EXP: float = 1.5

# Probability of a tendril crackling bright at any given frame.
CRACKLE_PROB: float = 0.15

# Minimum and maximum tendril flicker multiplier.
FLICKER_MIN: float = 0.3
FLICKER_MAX: float = 1.0

# Tendril fork probability per regeneration.
FORK_PROB: float = 0.3

# Maximum number of simultaneous tendrils.
MAX_TENDRILS: int = 8

# Minimum tendril reach as fraction of half-strip.
MIN_REACH_FRAC: float = 0.3

# Maximum tendril reach as fraction of half-strip.
MAX_REACH_FRAC: float = 1.0

# Tendril lifetime range in seconds.
TENDRIL_LIFE_MIN: float = 0.08
TENDRIL_LIFE_MAX: float = 0.4

# Width of a tendril in zones (gaussian sigma).
TENDRIL_WIDTH: float = 1.5


@dataclass
class _Tendril:
    """State for one electric arc from core to endpoint.

    Attributes:
        zones:    List of zone indices this tendril passes through.
        birth_t:  Time of creation.
        lifetime: How long this tendril lives before dying.
        hue_off:  Hue offset from the base hue (adds variety).
    """
    zones: list[int] = field(default_factory=list)
    birth_t: float = 0.0
    lifetime: float = 0.2
    hue_off: float = 0.0

    def is_alive(self, t: float) -> bool:
        """Return True if this tendril is still active.

        Args:
            t: Current time.

        Returns:
            True if the tendril hasn't expired.
        """
        return (t - self.birth_t) < self.lifetime

    def age_frac(self, t: float) -> float:
        """Return normalized age [0, 1] where 1 = about to die.

        Args:
            t: Current time.

        Returns:
            Normalized age.
        """
        if self.lifetime <= 0:
            return 1.0
        return min(1.0, (t - self.birth_t) / self.lifetime)


class Plasma(Effect):
    """Plasma ball — electric tendrils from a pulsing central core.

    A bright core sits at the center of the strip, pulsing slowly.
    Electric tendrils crackle outward toward the ends, flickering and
    forking.  Brightness falls off with distance from the core.  The
    constantly regenerating tendrils give the characteristic look of
    a plasma globe.
    """

    name: str = "plasma"
    description: str = "Plasma ball — electric tendrils crackle from a pulsing core"

    speed = Param(2.0, min=0.3, max=10.0,
                  description="Core pulse period in seconds")
    tendril_rate = Param(8.0, min=1.0, max=30.0,
                         description="Average new tendrils spawned per second")
    hue = Param(270.0, min=0.0, max=360.0,
                description="Base tendril hue in degrees (270 = violet)")
    hue_spread = Param(40.0, min=0.0, max=180.0,
                       description="Random hue variation in degrees")
    saturation = Param(80, min=0, max=100,
                       description="Tendril saturation percent")
    brightness = Param(100, min=0, max=100,
                       description="Peak brightness percent")
    zones_per_bulb = Param(DEFAULT_ZPB, min=1, max=16,
                           description="Zones per physical bulb (3 for string lights)")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, **overrides: dict) -> None:
        """Initialize tendril tracking state.

        Args:
            **overrides: Parameter names mapped to override values.
        """
        super().__init__(**overrides)
        self._tendrils: list[_Tendril] = []
        self._next_spawn_t: float = 0.0
        self._last_t: Optional[float] = None

    def on_start(self, zone_count: int) -> None:
        """Reset state when effect becomes active.

        Args:
            zone_count: Number of zones on the target device.
        """
        self._tendrils.clear()
        self._next_spawn_t = 0.0
        self._last_t = None

    def period(self) -> None:
        """Plasma is stochastic — no loopable period."""
        return None

    # ------------------------------------------------------------------
    # Tendril management
    # ------------------------------------------------------------------

    def _spawn_tendril(self, t: float, bulb_count: int) -> None:
        """Create a new tendril reaching from center toward an end.

        The tendril path is a biased random walk from the core outward.
        It may fork once, creating a second branch.

        Args:
            t:          Current time.
            bulb_count: Number of bulbs (for positional math).
        """
        if bulb_count < 3:
            return

        center: int = bulb_count // 2
        half: int = bulb_count - center

        # Pick direction: left or right.
        direction: int = -1 if random.random() < 0.5 else 1

        # How far this tendril reaches (fraction of half-strip).
        reach_frac: float = random.uniform(MIN_REACH_FRAC, MAX_REACH_FRAC)
        reach: int = max(2, int(half * reach_frac))

        # Build path via biased random walk.
        zones: list[int] = [center]
        pos: float = float(center)
        for _ in range(reach):
            # Bias toward the endpoint, with random jitter.
            step: float = direction * random.uniform(0.5, 1.5)
            jitter: float = random.uniform(-0.3, 0.3)
            pos += step + jitter
            zone: int = max(0, min(bulb_count - 1, int(pos)))
            if zone not in zones:
                zones.append(zone)

        hue_off: float = random.uniform(-self.hue_spread, self.hue_spread)
        lifetime: float = random.uniform(TENDRIL_LIFE_MIN, TENDRIL_LIFE_MAX)

        self._tendrils.append(_Tendril(
            zones=zones,
            birth_t=t,
            lifetime=lifetime,
            hue_off=hue_off,
        ))

        # Possible fork — a shorter branch splitting off partway.
        if random.random() < FORK_PROB and len(zones) > 3:
            fork_start: int = random.randint(1, len(zones) // 2)
            fork_zones: list[int] = list(zones[:fork_start])
            pos = float(zones[fork_start - 1])
            fork_dir: int = direction * (-1 if random.random() < 0.5 else 1)
            fork_reach: int = max(1, reach // 3)
            for _ in range(fork_reach):
                pos += fork_dir * random.uniform(0.5, 1.5)
                zone = max(0, min(bulb_count - 1, int(pos)))
                if zone not in fork_zones:
                    fork_zones.append(zone)

            self._tendrils.append(_Tendril(
                zones=fork_zones,
                birth_t=t,
                lifetime=lifetime * 0.7,
                hue_off=hue_off + random.uniform(-10.0, 10.0),
            ))

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the plasma ball.

        Manages tendril lifecycle, computes core glow, and composites
        all active tendrils onto the zone array.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        zpb: int = self.zones_per_bulb
        bulb_count: int = max(1, zone_count // zpb)
        center: int = bulb_count // 2
        half: float = max(1.0, bulb_count / 2.0)

        max_bri: int = pct_to_u16(self.brightness)
        sat_u16: int = pct_to_u16(self.saturation)

        # First-frame initialization.
        if self._last_t is None:
            self._last_t = t
            self._next_spawn_t = t

        self._last_t = t

        # Spawn new tendrils.
        if t >= self._next_spawn_t and len(self._tendrils) < MAX_TENDRILS:
            self._spawn_tendril(t, bulb_count)
            self._next_spawn_t = t + random.expovariate(self.tendril_rate)

        # Expire dead tendrils.
        self._tendrils = [td for td in self._tendrils if td.is_alive(t)]

        # Per-bulb accumulators.
        bulb_bri: list[float] = [0.0] * bulb_count
        bulb_hue: list[float] = [self.hue] * bulb_count

        # Core glow — pulsing brightness at the center.
        core_pulse: float = (
            CORE_BRI_MIN
            + (CORE_BRI_MAX - CORE_BRI_MIN)
            * (0.5 + 0.5 * math.sin(TWO_PI * CORE_PULSE_FREQ * t / self.speed))
        )
        core_radius: int = max(1, int(bulb_count * CORE_RADIUS_FRAC))
        for b in range(bulb_count):
            dist: int = abs(b - center)
            if dist <= core_radius:
                # Gaussian core glow.
                sigma: float = max(0.5, core_radius / 2.0)
                glow: float = core_pulse * math.exp(-0.5 * (dist / sigma) ** 2)
                bulb_bri[b] = max(bulb_bri[b], glow)

        # Composite tendrils.
        for tendril in self._tendrils:
            age: float = tendril.age_frac(t)
            # Fade out as tendril dies.
            life_fade: float = 1.0 - age * age  # quadratic fade

            # Per-tendril flicker.
            if random.random() < CRACKLE_PROB:
                flicker: float = 1.0  # crackle = full brightness
            else:
                flicker = random.uniform(FLICKER_MIN, FLICKER_MAX)

            for idx, zone in enumerate(tendril.zones):
                if zone < 0 or zone >= bulb_count:
                    continue

                # Distance from core along the tendril path (normalized).
                path_frac: float = idx / max(1, len(tendril.zones) - 1)

                # Brightness decays with distance from core.
                dist_decay: float = (1.0 - path_frac) ** TENDRIL_DECAY_EXP

                bri_contrib: float = dist_decay * life_fade * flicker

                if bri_contrib > bulb_bri[zone]:
                    bulb_bri[zone] = bri_contrib
                    bulb_hue[zone] = self.hue + tendril.hue_off

                # Also illuminate neighboring zones (tendril width).
                for offset in (-1, 1):
                    neighbor: int = zone + offset
                    if 0 <= neighbor < bulb_count:
                        neighbor_bri: float = bri_contrib * 0.4
                        if neighbor_bri > bulb_bri[neighbor]:
                            bulb_bri[neighbor] = neighbor_bri
                            bulb_hue[neighbor] = self.hue + tendril.hue_off

        # Convert to HSBK.
        colors: list[HSBK] = []
        for i in range(zone_count):
            bulb_index: int = min(i // zpb, bulb_count - 1)
            bri_frac: float = min(1.0, bulb_bri[bulb_index])
            bri: int = int(max_bri * bri_frac) if bri_frac > 0.01 else 0
            h: float = bulb_hue[bulb_index] % 360.0
            colors.append((hue_to_u16(h), sat_u16, bri, self.kelvin))

        return colors
