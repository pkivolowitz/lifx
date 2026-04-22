"""Leapfrog effect — modulo-class jumpers ring-rotate through a palette.

Every modulo-M zone of the string belongs to one of M "classes".  One
beat of the effect fires all zones of a single class simultaneously:
each such zone's color leaps forward (or backward) by one group of M
to land in the same modulo-slot of the next group.  After M beats
every class has leapt once and the whole palette has rotated by one
group on the ring.  After G beats-of-M the ring returns to its
starting state, and the cycle repeats forever.

**Color physics — additive compositing.**  The visual "sum of colors,
not overwriting" behaviour is done the same way :mod:`effects.fireworks`
does it: each color contribution is converted to linear RGB, summed
per channel, clamped to 1.0, then converted back to HSB.  This gives
the physically-correct brightening when a mover passes over an
underlying color, and it makes the fade-to-black / fade-in-from-behind
behaviour at every moving slot fall out of the linear-RGB arithmetic
for free — no explicit lerping between two separate contributions.

**Velocity curve — DC sine.**  Each beat's rate of motion follows
``sin(π·p)`` (ramp up, cruise, settle down), so the mover's integrated
position is ``0.5·(1 − cos(π·p))`` — ease-in-out.

**Palettes.**  Uses the 50-entry registry shared by rule_trio and
spin.  Each palette supplies three hues + saturation; the per-zone
initial fill samples one of those three colors uniformly at random
per zone.

**Virtual-zone padding.**  The ring is kept as a multiple of M
*internally* — when the physical ``zone_count`` isn't a multiple of M,
we pad up to ``ceil(zone_count / M) * M`` virtual zones, fill them
from the same palette, and let them participate in every rotation.
Only the first ``zone_count`` entries are returned to the device, so
the virtual zones never show on any bulb, but the rhythm stays clean.

**Degeneracy.**  When the virtual ring still ends up with fewer than
two groups (``M > zone_count``), the effect has nothing to leap over
and renders the static palette fill rather than pretending motion.

**State lives here.**  ``self._state`` holds the full virtual ring
as a list of HSBK tuples.  The bulbs are not a source of truth — we
never read color back from them.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "1.0"

import math
import random
from typing import Any

from . import (
    DEVICE_TYPE_STRIP,
    Effect,
    HSBK,
    HSBK_MAX,
    KELVIN_DEFAULT,
    KELVIN_MAX,
    KELVIN_MIN,
    Param,
    hue_to_u16,
    pct_to_u16,
)
from .rule_trio import PALETTES, PALETTE_NAMES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default modulo-class count (M).  12 is a multiple of 3 (Perry's cue) and
# divides 48 (the NEON group zone count) cleanly.
DEFAULT_M: int = 12

# Velocity-curve shaping constants.
_PI: float = math.pi
_HALF: float = 0.5

# Degrees per full hue circle — HSB sextant math uses degrees, not u16.
DEGREES_PER_TURN: float = 360.0

# Number of hues per palette entry (all palettes are 3-hue in the registry).
HUES_PER_PALETTE: int = 3


# ---------------------------------------------------------------------------
# Additive-compositing helpers — same pattern as effects.fireworks.
#
# Intentionally local (copied, not imported from fireworks) to keep the
# effect self-contained and because fireworks' helpers are underscore-
# prefixed internals.  If a third effect ever needs this, promote these
# to colorspace.py as a proper public module.
# ---------------------------------------------------------------------------


def _hsb_to_rgb(h_deg: float, s: float, b: float) -> tuple[float, float, float]:
    """Convert HSB (hue in degrees, sat/bri in 0–1) to RGB in 0–1.

    Standard sextant algorithm — same as fireworks.  No sRGB gamma
    transfer; additive compositing here is in device-linear light
    space approximated by the sextant mapping, matching the established
    fireworks pattern.
    """
    if s <= 0.0:
        return (b, b, b)
    h = (h_deg % DEGREES_PER_TURN) / 60.0
    i = int(h)
    f = h - i
    p = b * (1.0 - s)
    q = b * (1.0 - s * f)
    t = b * (1.0 - s * (1.0 - f))
    if i == 0:
        return (b, t, p)
    if i == 1:
        return (q, b, p)
    if i == 2:
        return (p, b, t)
    if i == 3:
        return (p, q, b)
    if i == 4:
        return (t, p, b)
    return (b, p, q)


def _rgb_to_hsb(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Convert RGB (0–1) to HSB (hue in degrees, sat/bri in 0–1)."""
    mx: float = max(r, g, b)
    mn: float = min(r, g, b)
    d: float = mx - mn
    if d <= 0.0:
        h_deg: float = 0.0
    elif mx == r:
        h_deg = 60.0 * (((g - b) / d) % 6.0)
    elif mx == g:
        h_deg = 60.0 * (((b - r) / d) + 2.0)
    else:
        h_deg = 60.0 * (((r - g) / d) + 4.0)
    if h_deg < 0.0:
        h_deg += DEGREES_PER_TURN
    s: float = 0.0 if mx <= 0.0 else d / mx
    return (h_deg, s, mx)


def _hsbk_to_rgb(c: HSBK) -> tuple[float, float, float]:
    """Convert an HSBK tuple to RGB (0–1).  Kelvin is dropped for compositing."""
    hue_u16, sat_u16, bri_u16, _kelvin = c
    h_deg: float = (hue_u16 / HSBK_MAX) * DEGREES_PER_TURN
    s: float = sat_u16 / HSBK_MAX
    b: float = bri_u16 / HSBK_MAX
    return _hsb_to_rgb(h_deg, s, b)


# ---------------------------------------------------------------------------
# Beat-ordering helpers
# ---------------------------------------------------------------------------


def _beat_class(beat_index: int, m: int, order: str) -> int:
    """Map a sequential beat number to which modulo class fires this beat.

    ``sequential``: 0, 1, 2, …, M-1.
    ``interleaved``: 0, M/2, 1, M/2+1, 2, M/2+2, …  (odd M leaves the
    last index as a tail at position M-1).  Guarantees every class
    fires exactly once per M-beat cycle regardless of order or M parity.
    """
    i: int = beat_index % m
    if order == "sequential":
        return i
    # Interleaved: pair (even index, odd index) = (j, j + M//2).
    half: int = m // 2
    if i < 2 * half:
        j: int = i // 2
        return j + (half if (i % 2) else 0)
    # Only reached when M is odd; the trailing beat fires class M-1.
    return i


# ---------------------------------------------------------------------------
# The effect
# ---------------------------------------------------------------------------


class Leapfrog(Effect):
    """Modulo-class jumpers ring-rotate through a 3-color palette."""

    name: str = "leapfrog"
    description: str = (
        "Palette colors ring-rotate: each beat, all modulo-M zones "
        "leap one group forward (or backward) with additive pass-over"
    )
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_STRIP})

    palette = Param(
        "neon",
        choices=sorted(PALETTE_NAMES.keys()),
        description="Named palette preset (3 hues + saturation)",
    )
    m = Param(
        DEFAULT_M, min=2, max=64,
        description="Modulo class count — group size.  M that divides "
                    "zone_count cleanly looks best.",
    )
    beat_duration = Param(
        1.2, min=0.15, max=10.0,
        description="Seconds per beat (one full jump from source to target)",
    )
    beat_order = Param(
        "sequential",
        choices=["sequential", "interleaved"],
        description="Order classes fire within a cycle",
    )
    direction = Param(
        "forward",
        choices=["forward", "backward"],
        description="Ring travel direction.  No mid-run flip.",
    )
    brightness = Param(
        100, min=0, max=100,
        description="Overall brightness percent",
    )
    kelvin = Param(
        KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
        description="Color temperature in Kelvin",
    )
    seed = Param(
        0, min=0, max=1_000_000,
        description="RNG seed for palette fill; 0 = nondeterministic",
    )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self, zone_count: int) -> None:
        """Build the palette-filled virtual ring and reset beat state.

        The ring is padded up to the next multiple of M so the rotation
        rhythm stays clean even when the physical zone count isn't a
        multiple of M.  The padded (virtual) zones are part of the
        same ring — they hold colors, rotate with every beat, and act
        as real targets/sources — but they are trimmed off before the
        result is handed to the device.
        """
        palette_key: str = str(self.palette)
        # The "custom" slot in PALETTE_NAMES is not in PALETTES — it's
        # a rule_trio-specific escape hatch.  For leapfrog we fall back
        # to the "neon" palette to keep the effect live rather than
        # failing silently.
        hues_and_sat: tuple[float, float, float, int] = PALETTES.get(
            palette_key, PALETTES["neon"],
        )
        ha, hb, hc, sat_pct = hues_and_sat
        sat_u16: int = pct_to_u16(sat_pct)
        bri_u16: int = pct_to_u16(int(self.brightness))
        kelvin: int = int(self.kelvin)

        anchors: list[HSBK] = [
            (hue_to_u16(ha), sat_u16, bri_u16, kelvin),
            (hue_to_u16(hb), sat_u16, bri_u16, kelvin),
            (hue_to_u16(hc), sat_u16, bri_u16, kelvin),
        ]

        m_val: int = int(self.m)
        # Pad up to the next multiple of M.  Ceiling division: when
        # zone_count is already a multiple of M the virtual ring equals
        # the physical count exactly.  Minimum of M so M > zone_count
        # still produces a single group (degenerate path handled in render).
        virtual_count: int = max(
            m_val,
            ((zone_count + m_val - 1) // m_val) * m_val,
        )

        seed_val: int = int(self.seed)
        rng: random.Random = random.Random(seed_val if seed_val > 0 else None)
        self._state: list[HSBK] = [
            rng.choice(anchors) for _ in range(virtual_count)
        ]

        # Beat counter — how many beats we've committed to state.  A
        # beat completes when the render loop observes t crossing its
        # right boundary.  Starts at 0; render() advances it lazily.
        self._completed_beats: int = 0

        # Cache shape — used by render to detect device/zone/M changes
        # and rebuild the ring if any of them drifts.
        self._real_count: int = zone_count
        self._virtual_count: int = virtual_count
        self._m_cached: int = m_val

    def on_stop(self) -> None:
        """Release state — nothing external to close, but keep the contract."""
        self._state = []
        self._completed_beats = 0
        self._virtual_count = 0
        self._real_count = 0
        self._m_cached = 0

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame by compositing movers additively in linear RGB.

        Args:
            t:          Seconds elapsed since effect start.
            zone_count: Target device zone count (expected to match on_start).

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        m_val: int = int(self.m)

        # Rebuild the ring if the physical zone count, M, or both have
        # changed since on_start.  This covers device reconnect to a
        # different zone count AND live `set_params` changes to M.
        if (
            zone_count != self._real_count
            or m_val != self._m_cached
        ):
            self.on_start(zone_count)

        virtual_count: int = self._virtual_count
        n_groups: int = virtual_count // m_val

        # Degenerate cases — nothing to leap over.  Return the static
        # fill (trimmed to the real zone count) rather than pretend motion.
        if m_val < 2 or n_groups < 2:
            return list(self._state[:zone_count])

        beat_dur: float = float(self.beat_duration)
        total_beats: float = t / beat_dur
        current_beat: int = int(total_beats)
        p_raw: float = total_beats - current_beat  # 0 ≤ p_raw < 1

        # DC-sine velocity curve → eased position in [0, 1].
        p_eased: float = _HALF * (1.0 - math.cos(_PI * p_raw))

        # Commit state for every beat that has fully completed since
        # the last call.  Renderers can be called at uneven intervals,
        # so we must apply ALL pending rotations, not just the latest.
        order_str: str = str(self.beat_order)
        while self._completed_beats < current_beat:
            done_class: int = _beat_class(
                self._completed_beats, m_val, order_str,
            )
            self._rotate_class(done_class, m_val, n_groups)
            self._completed_beats += 1

        # Which class is currently mid-flight?
        current_class: int = _beat_class(current_beat, m_val, order_str)

        # Per-channel linear-RGB accumulators sized to the FULL virtual
        # ring.  Stationary zones (not of current_class) seed from
        # self._state; zones of current_class start at 0 and receive
        # contributions from the movers below.
        zone_r: list[float] = [0.0] * virtual_count
        zone_g: list[float] = [0.0] * virtual_count
        zone_b: list[float] = [0.0] * virtual_count

        for z in range(virtual_count):
            if z % m_val == current_class:
                continue  # deposited by mover contributions below
            r, g, bl = _hsbk_to_rgb(self._state[z])
            zone_r[z] = r
            zone_g[z] = g
            zone_b[z] = bl

        # Direction sign for the mover step: forward = +M zones, backward = -M.
        step: int = m_val if str(self.direction) == "forward" else -m_val

        # Each group contributes one mover this beat.  Mover's color is
        # the pre-beat state at its source (state[source] updates only
        # on beat completion, so reading it now is correct).
        for g_idx in range(n_groups):
            source: int = g_idx * m_val + current_class
            # Mover's current float position on the virtual ring.
            raw_pos: float = source + p_eased * step
            x: float = raw_pos % virtual_count
            x_floor: int = int(math.floor(x))
            x_ceil: int = (x_floor + 1) % virtual_count
            frac: float = x - math.floor(x)

            r, g, bl = _hsbk_to_rgb(self._state[source])

            # Bilinear tent: mover deposits color split between the
            # floor and ceil zones by proximity.  This one rule gives
            # three behaviours — fade at source, brightening pass-over
            # in the middle, fill-in at target — because every phase
            # is just "mover is at float position x" with different
            # integer neighbours.
            w_floor: float = 1.0 - frac
            w_ceil: float = frac
            zone_r[x_floor] += w_floor * r
            zone_g[x_floor] += w_floor * g
            zone_b[x_floor] += w_floor * bl
            zone_r[x_ceil] += w_ceil * r
            zone_g[x_ceil] += w_ceil * g
            zone_b[x_ceil] += w_ceil * bl

        # Clip and convert back to HSBK.  Clipping at 1.0 turns
        # co-located collisions into visible brightening up to white,
        # the intended additive payoff when different-hued movers
        # momentarily share a zone.  Only the first zone_count entries
        # are returned — virtual zones never reach a bulb.
        kelvin_val: int = int(self.kelvin)
        out: list[HSBK] = []
        for z in range(zone_count):
            r = min(1.0, zone_r[z])
            g = min(1.0, zone_g[z])
            bl = min(1.0, zone_b[z])
            if r <= 0.0 and g <= 0.0 and bl <= 0.0:
                out.append((0, 0, 0, kelvin_val))
                continue
            h_deg, s01, b01 = _rgb_to_hsb(r, g, bl)
            out.append((
                hue_to_u16(h_deg),
                int(s01 * HSBK_MAX),
                int(b01 * HSBK_MAX),
                kelvin_val,
            ))
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rotate_class(self, class_k: int, m_val: int, n_groups: int) -> None:
        """Rotate the modulo-class-*k* slots on the virtual ring by one group.

        Forward direction: the color at group g's slot-k moves to
        group (g+1)'s slot-k; the last group wraps to the first.
        Backward is the mirror.  The full virtual ring participates —
        including any padded zones past the physical zone count — so
        the rhythm stays clean across cycles.
        """
        slots: list[int] = [g * m_val + class_k for g in range(n_groups)]
        colors: list[HSBK] = [self._state[s] for s in slots]
        if str(self.direction) == "forward":
            # slots[g] receives what was at slots[g-1] (wrap).
            new_colors: list[HSBK] = [
                colors[(g - 1) % n_groups] for g in range(n_groups)
            ]
        else:
            new_colors = [
                colors[(g + 1) % n_groups] for g in range(n_groups)
            ]
        for slot, c in zip(slots, new_colors):
            self._state[slot] = c
