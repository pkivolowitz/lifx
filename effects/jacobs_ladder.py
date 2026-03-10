"""Jacob's Ladder effect — rising electric arcs between electrode pairs.

Two glowing electrode nodes with a flickering blue-white arc between
them drift along the string, break off at the far end, and reform at
the start.  The gap between electrodes is modulated by smooth noise so
they breathe apart and together without ever collapsing or stretching
too far.

Multiple arc pairs can coexist.  At least one arcing pair is always
visible on the string.

Inspired by the classic Frankenstein laboratory prop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import math
import random

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Arc color: electric blue-white.
ARC_HUE_DEG: float = 220.0        # Blue.
ARC_SAT_FRAC: float = 0.45        # Partially desaturated toward white.
ELECTRODE_SAT_FRAC: float = 0.15  # Electrodes are nearly white.

# Noise: simple smooth random walk for gap modulation.
NOISE_STEP: float = 0.12          # Max change in gap per frame (in bulbs).

# Minimum gap between electrodes in bulbs.
GAP_MIN_BULBS: int = 2
# Maximum gap as fraction of string length.
GAP_MAX_FRAC: float = 0.4

# Flicker: per-frame random brightness variation for the arc.
# Wide range creates dramatic dips (nearly dead) and blazes.
FLICKER_MIN: float = 0.15
FLICKER_MAX: float = 0.85

# Surge: probability per arc per frame of a full-intensity blaze.
SURGE_PROBABILITY: float = 0.10
SURGE_INTENSITY: float = 1.0

# Crackle: probability per bulb per frame of a bright white spike.
CRACKLE_PROBABILITY: float = 0.12
CRACKLE_SAT_FRAC: float = 0.05   # Nearly pure white.

# Per-bulb flicker range within the arc body.
BULB_FLICKER_MIN: float = 0.25
BULB_FLICKER_MAX: float = 1.0

# Default zones per bulb.
DEFAULT_ZPB: int = 1


class _ArcPair:
    """State for one pair of electrodes with an arc between them."""

    __slots__ = ("position", "gap", "gap_target", "speed", "direction")

    def __init__(self, position: float, gap: float, speed: float,
                 direction: int, bulb_count: int) -> None:
        """Create an arc pair.

        Args:
            position:   Center position in bulb units.
            gap:        Current gap between electrodes in bulb units.
            speed:      Drift speed in bulbs per frame.
            direction:  +1 = drift toward high end, -1 = drift toward low end.
            bulb_count: Total bulbs on the string (for gap clamping).
        """
        self.position: float = position
        self.gap: float = gap
        self.gap_target: float = gap
        self.speed: float = speed
        self.direction: int = direction

    def step(self, bulb_count: int, gap_min: int, gap_max: float) -> None:
        """Advance the arc one frame.

        Args:
            bulb_count: Total bulbs on the string.
            gap_min:    Minimum electrode gap in bulbs.
            gap_max:    Maximum electrode gap in bulbs.
        """
        # Drift position.
        self.position += self.speed * self.direction

        # Smooth random walk for gap: pick a new target occasionally,
        # then ease toward it.
        if random.random() < 0.08:
            self.gap_target = random.uniform(gap_min, gap_max)
        self.gap += (self.gap_target - self.gap) * NOISE_STEP
        self.gap = max(gap_min, min(gap_max, self.gap))

    def is_off_string(self, bulb_count: int) -> bool:
        """Return True if the entire arc has scrolled off the string."""
        half: float = self.gap / 2.0
        if self.direction > 0:
            return (self.position - half) >= bulb_count
        else:
            return (self.position + half) < 0

    def left_edge(self) -> float:
        """Left electrode position in bulb units."""
        return self.position - self.gap / 2.0

    def right_edge(self) -> float:
        """Right electrode position in bulb units."""
        return self.position + self.gap / 2.0


class JacobsLadder(Effect):
    """Jacob's Ladder — rising electric arcs between electrode pairs.

    Arc pairs drift along the string, break off at the end, and reform.
    The electrode gap breathes with smooth noise.  Multiple arcs can
    coexist, and at least one is always visible.
    """

    name: str = "jacobs_ladder"
    description: str = "Rising electric arcs between electrode pairs (Frankenstein lab)"

    speed = Param(0.15, min=0.02, max=1.0,
                  description="Arc drift speed in bulbs per frame")
    arcs = Param(2, min=1, max=5,
                 description="Target number of simultaneous arc pairs")
    gap = Param(4, min=2, max=12,
                description="Base gap between electrodes in bulbs")
    reverse = Param(0, min=0, max=1,
                    description="Drift direction: 0 = forward, 1 = reverse")
    brightness = Param(100, min=0, max=100,
                       description="Overall brightness percent")
    zones_per_bulb = Param(DEFAULT_ZPB, min=1, max=16,
                           description="Zones per physical bulb (3 for string lights)")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def __init__(self, **overrides: dict) -> None:
        """Initialise the Jacob's Ladder effect."""
        super().__init__(**overrides)
        self._arcs: list[_ArcPair] = []
        self._frame: int = 0
        self._bulb_count: int = 0

    def _spawn_arc(self, bulb_count: int) -> _ArcPair:
        """Create a new arc pair at the entry end of the string.

        Args:
            bulb_count: Total bulbs on the string.

        Returns:
            A new _ArcPair positioned at the entry edge.
        """
        direction: int = -1 if self.reverse else 1
        gap_val: float = float(self.gap) + random.uniform(-1.0, 1.0)
        gap_val = max(GAP_MIN_BULBS, gap_val)
        half: float = gap_val / 2.0

        # Spawn just entering the string from the entry edge.
        if direction > 0:
            pos: float = -half + 1.0
        else:
            pos = bulb_count + half - 1.0

        return _ArcPair(pos, gap_val, self.speed, direction, bulb_count)

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the Jacob's Ladder effect.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        zpb: int = self.zones_per_bulb
        bulb_count: int = max(1, zone_count // zpb)
        self._frame += 1
        self._bulb_count = bulb_count

        max_bri: int = pct_to_u16(self.brightness)
        gap_max: float = min(float(bulb_count) * GAP_MAX_FRAC,
                             float(self.gap) * 2.0)

        # --- Ensure minimum arc count ------------------------------------
        while len(self._arcs) < self.arcs:
            if not self._arcs:
                # First arc: start partway onto the string so it's
                # immediately visible.
                arc = self._spawn_arc(bulb_count)
                arc.position = float(bulb_count) * 0.3
                self._arcs.append(arc)
            else:
                self._arcs.append(self._spawn_arc(bulb_count))

        # --- Step each arc and remove dead ones ---------------------------
        for arc in self._arcs:
            arc.step(bulb_count, GAP_MIN_BULBS, gap_max)

        self._arcs = [a for a in self._arcs
                      if not a.is_off_string(bulb_count)]

        # --- Guarantee at least one arc is always visible -----------------
        if not self._arcs:
            self._arcs.append(self._spawn_arc(bulb_count))

        # --- Render bulb brightness buffer --------------------------------
        # Start with black.  Each arc pair paints electrodes and arc.
        bulb_hue: list[float] = [0.0] * bulb_count
        bulb_sat: list[float] = [0.0] * bulb_count
        bulb_bri: list[float] = [0.0] * bulb_count

        arc_hue: int = hue_to_u16(ARC_HUE_DEG)

        for arc in self._arcs:
            left: float = arc.left_edge()
            right: float = arc.right_edge()

            # Per-arc flicker: random brightness multiplier.
            # Occasional surges blast the arc to full intensity.
            is_surge: bool = random.random() < SURGE_PROBABILITY
            if is_surge:
                flicker: float = SURGE_INTENSITY
            else:
                flicker = random.uniform(FLICKER_MIN, FLICKER_MAX)

            for b in range(bulb_count):
                fb: float = float(b)

                # Distance from left and right electrodes.
                d_left: float = abs(fb - left)
                d_right: float = abs(fb - right)

                # Electrode glow: bright within ~1 bulb of each electrode.
                # Electrodes pulse with the arc during surges.
                electrode_intensity: float = 0.0
                if d_left < 1.5:
                    electrode_intensity = max(electrode_intensity,
                                              1.0 - d_left / 1.5)
                if d_right < 1.5:
                    electrode_intensity = max(electrode_intensity,
                                              1.0 - d_right / 1.5)
                if electrode_intensity > 0 and is_surge:
                    electrode_intensity = 1.0

                # Arc glow: between the two electrodes.
                arc_intensity: float = 0.0
                is_crackle: bool = False
                if left <= fb <= right:
                    # Intensity peaks in the middle, tapers toward electrodes.
                    span: float = right - left
                    if span > 0:
                        normalized: float = (fb - left) / span
                        # Sine-shaped profile: bright in center, dim at edges.
                        arc_intensity = math.sin(normalized * math.pi)
                        # Apply per-arc flicker.
                        arc_intensity *= flicker
                        # Per-bulb flicker: wide range for organic look.
                        arc_intensity *= random.uniform(BULB_FLICKER_MIN,
                                                        BULB_FLICKER_MAX)
                        # Crackle: random bright white spike on individual bulbs.
                        if random.random() < CRACKLE_PROBABILITY:
                            arc_intensity = 1.0
                            is_crackle = True

                # Combine: electrodes are brighter and whiter than the arc.
                if electrode_intensity > 0:
                    intensity: float = electrode_intensity
                    sat: float = ELECTRODE_SAT_FRAC
                elif arc_intensity > 0:
                    intensity = arc_intensity * 0.85
                    sat = CRACKLE_SAT_FRAC if is_crackle else ARC_SAT_FRAC
                else:
                    continue

                # Additive blend with existing buffer.
                bulb_bri[b] = min(1.0, bulb_bri[b] + intensity)
                # Take the brighter arc's hue/sat (winner-take-all).
                if intensity > bulb_sat[b]:
                    bulb_hue[b] = ARC_HUE_DEG
                    bulb_sat[b] = sat

        # --- Map bulb buffer to zone colors -------------------------------
        colors: list[HSBK] = []
        for i in range(zone_count):
            b: int = i // zpb
            bri_val: float = bulb_bri[b]
            if bri_val < 0.01:
                colors.append((0, 0, 0, self.kelvin))
            else:
                hue: int = hue_to_u16(bulb_hue[b])
                sat: int = int(HSBK_MAX * bulb_sat[b])
                bri: int = int(max_bri * bri_val)
                colors.append((hue, sat, bri, self.kelvin))

        return colors
