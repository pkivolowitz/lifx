"""Shared per-effect helpers — hue auto-cycle walker and rate categories.

This module exists so that effects which expose the same ambient
"hue and rate" UX (conway2d, pong2d, ...) don't grow drifted copies
of the same logic.  It's underscore-prefixed because it is internal
to the effects package, not part of the user-facing surface.

The two pieces:

- ``HueWalker`` — OkLab-interpolated brownian-walk hue source.  Each
  leg picks a new target hue at ``current ± uniform(min, max)`` with
  random sign, then interpolates through OkLab so the path between
  hues stays perceptually continuous.  All consumer effects share the
  same auto-cycle behavior; only the per-effect param wiring differs.

- ``RATE_*`` string constants and ``ALLOWED_RATES`` — the canonical
  vocabulary for effects that expose ``--rate slow|medium|fast``.
  The mapping from rate category to physical units (seconds per
  generation, cells per second, etc.) stays in each effect — only
  the labels are shared.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import os
import random
import sys

from . import HSBK_MAX, KELVIN_DEFAULT, hue_to_u16

# ``lerp_color`` lives in the project-root ``colorspace`` module; effects
# normally don't reach outside the package, but ``colorspace`` is
# explicitly the shared color-math home and is imported the same way
# by ``effects/primary_cycle.py``.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from colorspace import lerp_color  # noqa: E402

# ---------------------------------------------------------------------------
# Rate vocabulary
# ---------------------------------------------------------------------------

# Effects that expose --rate use these strings as Param choices so the
# CLI accepts identical vocabulary across effects.  The numeric meaning
# (interval seconds, cells per second, ...) is per-effect.
RATE_SLOW: str = "slow"
RATE_MEDIUM: str = "medium"
RATE_FAST: str = "fast"
ALLOWED_RATES: list[str] = [RATE_SLOW, RATE_MEDIUM, RATE_FAST]

# Shared ball-speed mapping (cells per second) for effects that drive
# a bouncing ball at the --rate selected.  Used by pong2d, boing_ball,
# and any future bounce-style effect; consistent values keep the rate
# vocabulary feeling like a single household-wide knob rather than
# per-effect oddities.  Effects that don't move balls — e.g. conway2d
# with its discrete-step interval — pick a different mapping (see
# their own ``RATE_*`` dicts).
RATE_CELLS_PER_SEC: dict[str, float] = {
    RATE_SLOW:   1.5,
    RATE_MEDIUM: 3.0,
    RATE_FAST:   6.0,
}


# ---------------------------------------------------------------------------
# Hue auto-cycle
# ---------------------------------------------------------------------------

# Sentinel for ``--hue`` meaning "no hue given — auto-cycle in OkLab".
# Effect Param defaults that adopt the auto-cycle convention should set
# ``default=HUE_AUTO_SENTINEL, min=HUE_AUTO_SENTINEL, max=360``.
HUE_AUTO_SENTINEL: int = -1

# Default brownian-walk parameters for HueWalker; effects can override
# at construction time.
# - HUE_LEG_DURATION_SEC: seconds per OkLab interpolation leg.
# - HUE_DELTA_MIN_DEG / HUE_DELTA_MAX_DEG: per-leg step magnitude is
#   uniform in [MIN, MAX] with a random sign.  Nonzero MIN keeps the
#   walk from stalling near zero (a uniform-around-zero delta produces
#   frequent near-no-op legs that read as "the color isn't changing").
HUE_LEG_DURATION_SEC: float = 12.0
HUE_DELTA_MIN_DEG: float = 20.0
HUE_DELTA_MAX_DEG: float = 50.0
HUE_DEGREES_FULL_CIRCLE: float = 360.0


def _hue_deg_to_hsbk(hue_deg: float) -> tuple[int, int, int, int]:
    """Build a fully-saturated, full-brightness HSBK at *hue_deg*.

    Used as endpoints for OkLab interpolation — only the hue
    coordinate of the result matters; saturation and brightness are
    pinned to max so the lerp's chroma path stays at the gamut edge
    and the extracted hue is well-defined.
    """
    return (hue_to_u16(hue_deg), HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT)


class HueWalker:
    """OkLab-interpolated brownian-walk hue source.

    ``hue_u16(t)`` returns the current global hue (LIFX 16-bit) for
    wall-clock time *t*.  Internally:

    - The walker holds the current "from" and "to" hues in degrees.
    - Each leg lasts ``leg_duration_sec``.
    - When *t* crosses a leg boundary, "to" promotes to "from" and a
      new "to" is drawn at ``current ± uniform(min, max)`` with
      random sign, wrapped modulo 360.
    - Within a leg, the hue is the OkLab interpolation between the
      two endpoint HSBKs (full saturation, full brightness so the
      arc stays at the gamut edge).

    The walker carries no awareness of frame rate — it samples the
    current hue at any *t* the caller passes, so it survives skipped
    frames and out-of-order calls without drifting.

    Construct one walker per effect instance; never share across
    effects (the random walk is stateful).
    """

    def __init__(
        self,
        leg_duration_sec: float = HUE_LEG_DURATION_SEC,
        delta_min_deg: float = HUE_DELTA_MIN_DEG,
        delta_max_deg: float = HUE_DELTA_MAX_DEG,
        start_t: float = 0.0,
        seed_hue_deg: float | None = None,
    ) -> None:
        """Initialize the walker with its first leg.

        Args:
            leg_duration_sec: Seconds between target swaps.
            delta_min_deg:    Minimum |delta| per leg in degrees.
                              Nonzero floor avoids stall.
            delta_max_deg:    Maximum |delta| per leg in degrees.
            start_t:          Wall-clock t corresponding to the start
                              of the first leg.  Use the same t origin
                              the consumer effect uses so the leg
                              boundary math lines up.
            seed_hue_deg:     Optional starting hue; defaults to a
                              uniform random pick over [0, 360).
        """
        self._leg_seconds: float = float(leg_duration_sec)
        self._delta_min: float = float(delta_min_deg)
        self._delta_max: float = float(delta_max_deg)
        self._from_deg: float = (
            float(seed_hue_deg) % HUE_DEGREES_FULL_CIRCLE
            if seed_hue_deg is not None
            else random.uniform(0.0, HUE_DEGREES_FULL_CIRCLE)
        )
        self._to_deg: float = self._next_target(self._from_deg)
        self._leg_start_t: float = float(start_t)

    def _next_target(self, current_deg: float) -> float:
        """Pick the next leg's target hue (degrees, wrapped to [0, 360)).

        Magnitude uniform in ``[delta_min, delta_max]`` with 50/50
        random sign.
        """
        magnitude: float = random.uniform(self._delta_min, self._delta_max)
        sign: float = 1.0 if random.random() < 0.5 else -1.0
        return (current_deg + sign * magnitude) % HUE_DEGREES_FULL_CIRCLE

    def hue_u16(self, t: float) -> int:
        """Return the current global hue at wall-clock time *t*.

        Advances any number of completed legs in a single call (a
        slow render loop won't desync the walk) and returns the
        OkLab-interpolated hue for the current leg's progress.
        """
        # Catch up across any legs that elapsed since the last call.
        while t >= self._leg_start_t + self._leg_seconds:
            self._from_deg = self._to_deg
            self._to_deg = self._next_target(self._from_deg)
            self._leg_start_t += self._leg_seconds
        leg_phase: float = (t - self._leg_start_t) / self._leg_seconds
        leg_phase = max(0.0, min(1.0, leg_phase))
        from_hsbk: tuple[int, int, int, int] = _hue_deg_to_hsbk(self._from_deg)
        to_hsbk: tuple[int, int, int, int] = _hue_deg_to_hsbk(self._to_deg)
        return lerp_color(from_hsbk, to_hsbk, leg_phase)[0]
