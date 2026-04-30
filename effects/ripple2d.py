"""Ripple — physics-motivated expanding wave train from random pebbles.

Each pebble produces a pulse train of N concentric crests separated by
one wavelength.  This is the cheap stand-in for surface-wave dispersion:
a real point-impulse deposits a broadband spectrum which separates into
a packet of crests under the gravity-capillary dispersion relation.
Crest k is born the instant the leading edge has advanced k
wavelengths, so the packet builds outward rather than appearing all at
once.

Two damping axes operate simultaneously:

- **Spatial — 1/sqrt(r)**: 2D cylindrical spreading.  Energy is
  conserved over an expanding ring of circumference 2*pi*r, so
  amplitude falls as 1/sqrt(r).  Clamped at SPREADING_FLOOR to avoid
  the singularity at the impact point.

- **Temporal — exp(-age/tau)**: bulk viscous dissipation, independent
  of position.  Captures the fact that even sitting at fixed radius
  watching the packet pass by, the crests dim over time as the system
  loses energy to viscosity.

A pebble retires when its temporal envelope drops below
DECAY_THRESHOLD (~1/255 — invisible at 8-bit brightness).  This single
physically-meaningful timescale replaces the older "lifetime by corner
distance" heuristic that v3.x used.

Spawn model is a Poisson process: each tick has a probability of
launching a new pebble.  At ``on_start`` the initial cohort is
staggered with random ages already in progress so the first frame
already shows ripples mid-flight rather than synchronized fresh
impacts.

Computes on a full rectangular grid.  ``--luna`` is legacy — for
non-Luna fixtures with their own dead-cell geometry, the per-fixture
mask in :class:`transport.LifxDevice` handles it transparently and
``--luna`` should stay 0.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "4.2"

import math
import random
from dataclasses import dataclass

from . import (
    DEVICE_TYPE_MATRIX,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Black — dead pixels and empty space.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Default grid dimensions — Luna protocol grid.
DEFAULT_WIDTH: int = 7
DEFAULT_HEIGHT: int = 5

# Luna dead corners — (row, col) positions with no physical LED.
# Kept here for the legacy ``--luna`` path; per-fixture masking in
# transport handles arbitrary dead-cell geometry.
LUNA_DEAD_ZONES: frozenset[tuple[int, int]] = frozenset({
    (0, 0), (0, 6), (4, 0), (4, 6),
})

# Minimum effective Gaussian sigma — below this the band becomes a
# single-pixel ring and aliases badly on coarse grids.
MIN_BAND_SIGMA: float = 0.35

# How many sigmas of the Gaussian to include before clipping to zero.
# Past 3-sigma the contribution is < 1.1% — invisible at int-quantized
# brightness, not worth computing.
GAUSSIAN_CUTOFF_SIGMAS: float = 3.0

# Per-pebble amplitude variance — the "weight" of each drop.  A
# pebble's peak brightness is uniform in this range, scaled by
# ``--brightness``.  0.35 = light pebble, 1.0 = heavy.
AMPLITUDE_MIN: float = 0.35
AMPLITUDE_MAX: float = 1.0

# Per-pebble speed variance — multiplier on the ``--speed`` param,
# uniform in this range.  Slight variance breaks visual periodicity
# without making rings noticeably faster/slower than commanded.
SPEED_VARIANCE_MIN: float = 0.7
SPEED_VARIANCE_MAX: float = 1.3

# Hard cap on simultaneous crests per pebble — allocation safety only.
# In practice the temporal envelope retires the trailing crests well
# before this cap is hit; the cap exists so a pathologically small
# wavelength on a very large grid cannot allocate unbounded crests.
NUM_CRESTS_MAX: int = 8

# Spatial-spreading floor — radius below which 1/sqrt(r) is clamped
# to 1.0 to avoid the singularity at the impact point.  Physically
# the point-impulse approximation breaks down at scales smaller than
# the pebble itself, so a sub-grid-cell floor is reasonable.
SPREADING_FLOOR: float = 0.5

# Temporal envelope value below which a pebble is considered dead.
# 1/255 is the smallest 8-bit visible step at full brightness; below
# that the contribution is rounding noise.
DECAY_THRESHOLD: float = 1.0 / 255.0


@dataclass
class _Pebble:
    """State for one in-flight pebble (impact + radiating wave packet).

    Attributes:
        x:          Drop point x (column, may be fractional).
        y:          Drop point y (row, may be fractional).
        birth_t:    Effect-time at moment of drop.
        speed:      This pebble's own ring expansion speed.
        amplitude:  Peak brightness multiplier in [0, 1] — the
            "weight" of the drop.
        hue_offset: Random hue jitter in degrees, applied to the
            base hue.
    """
    x: float
    y: float
    birth_t: float
    speed: float
    amplitude: float
    hue_offset: float


class Ripple2D(Effect):
    """Physics-motivated expanding ripples from randomly dropped pebbles.

    Each pebble emits a pulse train of concentric crests separated by
    one wavelength, with both spatial (1/sqrt(r)) and temporal
    (exp(-age/tau)) damping.  Pebbles drop at random times (Poisson
    spawn) and random positions, each with its own random amplitude,
    speed, and hue.  Multiple pulse trains overlap additively;
    overlapping crests of different colors blend toward the dominant.
    """

    name: str = "ripple2d"
    description: str = (
        "Physics-motivated wave-packet ripples from randomly dropped pebbles"
    )
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    width = Param(DEFAULT_WIDTH, min=1, max=500,
                  description="Grid width in pixels (columns)")
    height = Param(DEFAULT_HEIGHT, min=1, max=300,
                   description="Grid height in pixels (rows)")
    max_pebbles = Param(4, min=1, max=12,
                        description="Soft cap on simultaneous live pebbles")
    spawn_rate = Param(1.5, min=0.05, max=10.0,
                       description="Average new pebbles per second (Poisson)")
    speed = Param(4.0, min=0.1, max=30.0,
                  description="Base ring expansion speed (grid units per second)")
    wavelength = Param(2.0, min=0.5, max=20.0,
                       description="Inter-crest spacing in grid units (pulse-train wavelength)")
    decay_time = Param(2.0, min=0.1, max=20.0,
                       description="Temporal decay time tau in seconds (exp envelope)")
    thickness = Param(1.0, min=0.4, max=8.0,
                      description="Crest band thickness in grid units (Gaussian sigma)")
    hue = Param(200.0, min=0.0, max=360.0,
                description="Base hue in degrees (0-360)")
    hue_spread = Param(60.0, min=0.0, max=360.0,
                       description="Per-pebble random hue jitter in degrees")
    brightness = Param(100, min=1, max=100,
                       description="Peak ring brightness (percent of device max for a single non-overlapping crest; "
                                   "constructive interference where crests overlap saturates toward full white)")
    luna = Param(0, min=0, max=1,
                 description="Black out Luna dead corners (1=yes; usually unnecessary "
                             "since per-fixture mask in transport handles it)")

    def on_start(self, zone_count: int) -> None:
        """Reset state and seed a staggered initial cohort.

        The full pebble lifetime is solved from the temporal-decay
        threshold: tau * ln(1/DECAY_THRESHOLD).  Initial pebbles are
        back-dated by random fractions of that span so the first
        rendered frame already contains pulse trains at every stage
        of decay rather than a synchronized fresh cohort that would
        all expire on the same frame later.

        Args:
            zone_count: Number of zones on the target device.
        """
        self._last_t: float = 0.0
        self._next_spawn_t: float = 0.0
        self._pebbles: list[_Pebble] = []

        tau: float = max(float(self.decay_time), 1e-3)
        full_lifetime: float = tau * math.log(1.0 / DECAY_THRESHOLD)

        seed_n: int = max(1, int(self.max_pebbles) // 2)
        for _ in range(seed_n):
            peb: _Pebble = self._new_pebble(0.0)
            past_age: float = random.uniform(0.0, full_lifetime * 0.8)
            peb.birth_t = -past_age
            self._pebbles.append(peb)

        self._next_spawn_t = random.expovariate(
            max(float(self.spawn_rate), 0.01),
        )

    def _new_pebble(self, t: float) -> _Pebble:
        """Drop a fresh pebble at a random position with random parameters.

        Each pebble gets its own amplitude (variable drop weight), a
        speed (slight variance around the global ``--speed``), and a
        hue offset (within ``+/- hue_spread/2`` of the base hue).
        Pulse-train geometry and damping are computed in
        :meth:`render` from the global wavelength/decay-time params;
        per-pebble state is just the impact and the pebble's
        variations.

        Args:
            t: Effect-time at moment of drop.

        Returns:
            A new ``_Pebble`` initialized with random parameters.
        """
        w: int = int(self.width)
        h: int = int(self.height)
        x: float = random.uniform(0.0, max(w - 1, 0))
        y: float = random.uniform(0.0, max(h - 1, 0))

        speed: float = float(self.speed) * random.uniform(
            SPEED_VARIANCE_MIN, SPEED_VARIANCE_MAX,
        )
        amplitude: float = random.uniform(AMPLITUDE_MIN, AMPLITUDE_MAX)

        spread: float = float(self.hue_spread)
        hue_offset: float = random.uniform(-spread * 0.5, spread * 0.5)

        return _Pebble(
            x=x, y=y, birth_t=t, speed=speed,
            amplitude=amplitude, hue_offset=hue_offset,
        )

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame: sum of pulse-train rings from all live pebbles.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels (ignored — uses width * height).

        Returns:
            A list of ``width * height`` HSBK tuples in row-major order.
        """
        w: int = int(self.width)
        h: int = int(self.height)
        total: int = w * h

        sigma: float = max(MIN_BAND_SIGMA, float(self.thickness))
        bri_max: int = pct_to_u16(self.brightness)
        sat: int = HSBK_MAX
        base_hue_deg: float = float(self.hue)
        wavelength: float = max(float(self.wavelength), 1e-3)
        tau: float = max(float(self.decay_time), 1e-3)

        # ----- Lifecycle: retire dead pebbles, possibly spawn new ones -----

        # Pebble dies when its temporal envelope drops below the
        # 8-bit visible threshold.  This single physics-rooted check
        # subsumes the v3.x corner-distance and ring-radius heuristics.
        self._pebbles = [
            p for p in self._pebbles
            if math.exp(-(t - p.birth_t) / tau) > DECAY_THRESHOLD
        ]

        # Poisson spawn: when t crosses _next_spawn_t and we're under
        # the cap, drop a new pebble and schedule the next event.
        # The while loop handles bursts when spawn_rate is high
        # without compressing every event into one frame.
        while t >= self._next_spawn_t:
            if len(self._pebbles) < int(self.max_pebbles):
                self._pebbles.append(self._new_pebble(t))
            self._next_spawn_t += random.expovariate(
                max(float(self.spawn_rate), 0.01),
            )

        self._last_t = t

        # ----- Per-pebble derived state, computed once per frame ----------

        gaussian_norm: float = 1.0 / (2.0 * sigma * sigma)
        cutoff_dist: float = GAUSSIAN_CUTOFF_SIGMAS * sigma

        # Each entry: (px, py, hue_off, [(r_k, amp_k), ...]).  Crest k
        # has radius r_k = leading_r - k * wavelength; alive iff > 0.
        # Crest amplitude folds together the per-pebble peak weight,
        # the temporal envelope (same for all crests of one pebble at
        # one instant), and the per-crest 1/sqrt(r) spatial spreading.
        active: list[tuple[float, float, float,
                           list[tuple[float, float]]]] = []
        for peb in self._pebbles:
            age: float = t - peb.birth_t
            if age < 0:
                continue
            envelope: float = math.exp(-age / tau)
            leading_r: float = peb.speed * age
            crests: list[tuple[float, float]] = []
            # Walk crests from leading edge inward.  Stop at the first
            # k with r_k <= 0: that crest hasn't been born yet (its
            # wavelength has not been traversed by the leading edge),
            # so all higher k are also unborn.
            for k in range(NUM_CRESTS_MAX):
                r_k: float = leading_r - k * wavelength
                if r_k <= 0.0:
                    break
                spreading: float = math.sqrt(
                    SPREADING_FLOOR / max(r_k, SPREADING_FLOOR),
                )
                amp_k: float = peb.amplitude * envelope * spreading
                crests.append((r_k, amp_k))
            if not crests:
                continue
            active.append((peb.x, peb.y, peb.hue_offset, crests))

        # ----- Rasterize: per-pixel sum of Gaussian-band contributions ----

        colors: list[HSBK] = [BLACK] * total

        for row in range(h):
            for col in range(w):
                bri_accum: float = 0.0
                hue_accum: float = 0.0
                weight_total: float = 0.0
                for sx, sy, hue_off, crests in active:
                    dx: float = col - sx
                    dy: float = row - sy
                    dist: float = math.sqrt(dx * dx + dy * dy)
                    for r_k, amp_k in crests:
                        delta: float = abs(dist - r_k)
                        if delta > cutoff_dist:
                            continue
                        g: float = math.exp(-(delta * delta) * gaussian_norm)
                        contribution: float = amp_k * g
                        bri_accum += contribution
                        hue_accum += hue_off * contribution
                        weight_total += contribution

                if bri_accum <= 0.0:
                    continue

                # bri_accum is the summed amplitude of every
                # overlapping crest at this pixel.  Single-crest peak
                # multiplied by bri_max reaches the user's --brightness
                # ceiling; constructive interference where multiple
                # crests overlap pushes the product past bri_max and
                # saturates to HSBK_MAX (full white).  This is the
                # physics: waves add, and bright peaks at intersections
                # is exactly what real ripples do.
                bri: int = min(int(bri_max * bri_accum), HSBK_MAX)
                if bri < 1:
                    continue

                hue_deg: float = base_hue_deg
                if weight_total > 0.0:
                    hue_deg = base_hue_deg + (hue_accum / weight_total)
                hue_u16: int = hue_to_u16(hue_deg % 360.0)

                idx: int = row * w + col
                colors[idx] = (hue_u16, sat, bri, KELVIN_DEFAULT)

        # Legacy Luna dead corner mask — superseded by per-fixture
        # masking in transport for known fixtures.
        if int(self.luna):
            for r, c in LUNA_DEAD_ZONES:
                idx = r * w + c
                if idx < total:
                    colors[idx] = BLACK

        return colors
