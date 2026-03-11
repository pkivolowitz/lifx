"""Sonar / radar effect — pulsing wavefronts reflect off drifting obstacles.

Obstacles meander around the middle of the string.  Between obstacles
(and at each end), sonar sources emit wavefronts that travel outward.
When a wavefront hits an obstacle it reflects; when it returns to its
source it is absorbed (its tail continues to fade).

The wavefront is intense white.  The trailing tail fades linearly
from white to black over a configurable number of zones.

Obstacle count scales with string length: 1 obstacle per 24 bulbs
(72 zones with the default 3 zones-per-bulb), minimum 1.

Each source is limited to one live (non-absorbed) pulse at a time;
a new wavefront is emitted only after the previous one from that
source has been absorbed or has died.

This effect is stateful: wavefront positions, obstacle drift, and
pulse timing are tracked across frames.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.3"

import math
import random
from typing import Optional

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default zones per bulb for LIFX string lights.
DEFAULT_ZPB: int = 3

# Minimum bulbs before adding a second obstacle.
BULBS_PER_OBSTACLE: int = 24

# Obstacle width in bulbs (visual thickness).
OBSTACLE_WIDTH_BULBS: int = 1

# Obstacle color: bright red-orange.
OBSTACLE_HUE_DEG: float = 15.0
OBSTACLE_SAT_PCT: float = 100.0
OBSTACLE_BRI_PCT: float = 80.0

# Wavefront color: intense white.
WAVEFRONT_SAT: int = 0
WAVEFRONT_KELVIN: int = 6500

# Minimum gap in bulbs between obstacle and edge/other obstacle.
MIN_GAP_BULBS: int = 3

# Obstacle drift speed: fraction of string length per second.
OBSTACLE_DRIFT_SPEED: float = 0.02

# How often obstacle changes drift direction (seconds).
OBSTACLE_DIRECTION_INTERVAL: float = 4.0

# Minimum number of obstacles.
MIN_OBSTACLES: int = 1


class _Wavefront:
    """A single traveling wavefront with a fading tail.

    Attributes:
        pos:       Current position in fractional zone units.
        direction: +1 (rightward) or -1 (leftward).
        source:    Zone position of the source that emitted this wavefront.
        alive:     False once the wavefront has returned to source and
                   its tail has fully faded.
        born_t:    Time the wavefront was created (for tail length calc).
    """

    def __init__(self, source: float, direction: int, speed: float) -> None:
        self.pos: float = source
        self.direction: int = direction
        self.source: float = source
        self.speed: float = speed
        self.alive: bool = True
        self.reflected: bool = False
        self.absorbed: bool = False
        # Track positions for tail rendering.
        self.trail: list[float] = []


class _Obstacle:
    """A drifting obstacle that reflects wavefronts.

    Attributes:
        pos:       Current center position in bulb units.
        drift_dir: Current drift direction (+1 or -1).
        next_turn: Time at which drift direction changes.
    """

    def __init__(self, pos: float) -> None:
        self.pos: float = pos
        self.drift_dir: int = random.choice([-1, 1])
        self.next_turn: float = 0.0


class Sonar(Effect):
    """Sonar pulses bounce off drifting obstacles.

    Wavefronts emit from sources positioned at the string ends and
    between obstacles.  Each wavefront travels outward, reflects off
    the nearest obstacle, and is absorbed when it returns to its source.
    The wavefront head is bright white; the tail fades to black.
    """

    name: str = "sonar"
    description: str = "Sonar pulses reflect off drifting obstacles"

    speed = Param(1.5, min=0.3, max=10.0,
                  description="Wavefront travel speed in bulbs per second")
    tail = Param(8, min=1, max=50,
                 description="Tail length in zones (fades white to black)")
    pulse_interval = Param(2.0, min=0.5, max=15.0,
                           description="Seconds between pulse emissions")
    obstacle_speed = Param(0.5, min=0.0, max=3.0,
                           description="Obstacle drift speed in bulbs per second")
    obstacle_hue = Param(OBSTACLE_HUE_DEG, min=0.0, max=360.0,
                         description="Obstacle color hue in degrees")
    obstacle_brightness = Param(80, min=10, max=100,
                                description="Obstacle brightness as percent")
    brightness = Param(100, min=10, max=100,
                       description="Wavefront peak brightness as percent")
    kelvin = Param(WAVEFRONT_KELVIN, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Wavefront color temperature")
    zones_per_bulb = Param(DEFAULT_ZPB, min=1, max=10,
                           description="Zones per physical bulb")

    def __init__(self, **overrides: dict) -> None:
        """Initialize sonar state.

        Args:
            **overrides: Parameter names mapped to override values.
        """
        super().__init__(**overrides)
        self._obstacles: list[_Obstacle] = []
        self._wavefronts: list[_Wavefront] = []
        self._sources: list[float] = []
        self._last_pulse_t: float = -999.0
        self._initialized: bool = False
        self._prev_t: float = 0.0

    def _init_state(self, zone_count: int) -> None:
        """Set up obstacles and sources for the given zone count.

        Args:
            zone_count: Total number of zones across all devices.
        """
        zpb: int = max(1, int(self.zones_per_bulb))
        bulb_count: int = max(1, zone_count // zpb)

        # Determine obstacle count.
        num_obstacles: int = max(MIN_OBSTACLES, bulb_count // BULBS_PER_OBSTACLE)

        # Place obstacles evenly across the middle region.
        self._obstacles = []
        for i in range(num_obstacles):
            # Spread obstacles evenly across the center 60% of the string.
            frac: float = (i + 1) / (num_obstacles + 1)
            center: float = 0.2 + frac * 0.6
            pos: float = center * bulb_count
            obs: _Obstacle = _Obstacle(pos)
            obs.next_turn = random.uniform(
                OBSTACLE_DIRECTION_INTERVAL * 0.5,
                OBSTACLE_DIRECTION_INTERVAL * 1.5,
            )
            self._obstacles.append(obs)

        self._update_sources(bulb_count)
        self._initialized = True

    def _update_sources(self, bulb_count: int) -> None:
        """Recompute source positions based on current obstacle positions.

        Sources sit at each end and midway between adjacent obstacles.

        Args:
            bulb_count: Total number of bulbs.
        """
        # Sort obstacles by position.
        obs_positions: list[float] = sorted(o.pos for o in self._obstacles)

        self._sources = []
        # Source at the left end.
        self._sources.append(0.0)
        # Sources between adjacent obstacles.
        for i in range(len(obs_positions) - 1):
            mid: float = (obs_positions[i] + obs_positions[i + 1]) / 2.0
            self._sources.append(mid)
        # Source at the right end.
        self._sources.append(float(bulb_count - 1))

    def _drift_obstacles(self, t: float, dt: float, bulb_count: int) -> None:
        """Update obstacle positions with meandering drift.

        Args:
            t:          Current time in seconds.
            dt:         Time delta since last frame.
            bulb_count: Total number of bulbs.
        """
        zpb: int = max(1, int(self.zones_per_bulb))
        obs_sorted: list[_Obstacle] = sorted(
            self._obstacles, key=lambda o: o.pos,
        )

        for idx, obs in enumerate(obs_sorted):
            # Change direction periodically.
            if t >= obs.next_turn:
                obs.drift_dir = -obs.drift_dir
                obs.next_turn = t + random.uniform(
                    OBSTACLE_DIRECTION_INTERVAL * 0.5,
                    OBSTACLE_DIRECTION_INTERVAL * 1.5,
                )

            # Compute movement.
            move: float = obs.drift_dir * self.obstacle_speed * dt
            new_pos: float = obs.pos + move

            # Clamp to valid range, respecting min gap from edges and
            # other obstacles.
            left_limit: float = float(MIN_GAP_BULBS)
            right_limit: float = float(bulb_count - 1 - MIN_GAP_BULBS)

            # Respect gaps between adjacent obstacles.
            if idx > 0:
                left_limit = max(
                    left_limit,
                    obs_sorted[idx - 1].pos + MIN_GAP_BULBS,
                )
            if idx < len(obs_sorted) - 1:
                right_limit = min(
                    right_limit,
                    obs_sorted[idx + 1].pos - MIN_GAP_BULBS,
                )

            if new_pos < left_limit:
                new_pos = left_limit
                obs.drift_dir = 1
            elif new_pos > right_limit:
                new_pos = right_limit
                obs.drift_dir = -1

            obs.pos = new_pos

    def _source_has_live_pulse(self, src: float) -> bool:
        """Check whether a source already has a live (non-absorbed) wavefront.

        Args:
            src: Source position in bulb units.

        Returns:
            True if *src* already owns a non-absorbed wavefront.
        """
        for wf in self._wavefronts:
            if wf.alive and not wf.absorbed and wf.source == src:
                return True
        return False

    def _emit_pulses(self, t: float, bulb_count: int) -> None:
        """Emit new wavefronts from sources that have no live pulse.

        Each source is limited to one active (non-absorbed) wavefront at a
        time.  A new pulse is emitted only when the previous one from that
        source has been absorbed or has died, *and* the pulse interval has
        elapsed since the last global emission check.

        Args:
            t:          Current time in seconds.
            bulb_count: Total number of bulbs.
        """
        if t - self._last_pulse_t < self.pulse_interval:
            return

        self._last_pulse_t = t

        for src in self._sources:
            # Only one live pulse per source at a time.
            if self._source_has_live_pulse(src):
                continue

            # Determine which directions this source should emit.
            # End sources emit inward only; middle sources emit both ways.
            if src <= 0.5:
                # Left end — emit rightward only.
                self._wavefronts.append(
                    _Wavefront(src, +1, self.speed),
                )
            elif src >= bulb_count - 1.5:
                # Right end — emit leftward only.
                self._wavefronts.append(
                    _Wavefront(src, -1, self.speed),
                )
            else:
                # Middle source — emit both directions.
                self._wavefronts.append(
                    _Wavefront(src, +1, self.speed),
                )
                self._wavefronts.append(
                    _Wavefront(src, -1, self.speed),
                )

    def _update_wavefronts(self, dt: float, bulb_count: int) -> None:
        """Advance all wavefronts and handle reflection/absorption.

        Args:
            dt:         Time delta since last frame.
            bulb_count: Total number of bulbs.
        """
        obs_positions: list[float] = sorted(o.pos for o in self._obstacles)
        half_obs: float = OBSTACLE_WIDTH_BULBS / 2.0

        for wf in self._wavefronts:
            if not wf.alive:
                continue

            # Record current position in trail before moving.
            wf.trail.append(wf.pos)

            # Trim trail by spatial distance: keep only entries within
            # `tail` zones (converted to bulbs) of the current head.
            zpb: int = max(1, int(self.zones_per_bulb))
            max_dist: float = self.tail / zpb
            while (len(wf.trail) > 1
                   and abs(wf.trail[0] - wf.pos) > max_dist):
                wf.trail.pop(0)

            # Move.
            wf.pos += wf.direction * wf.speed * dt

            if wf.absorbed:
                # Already absorbed — just let the trail fade out.
                # Kill when trail is fully past.
                if len(wf.trail) <= 1:
                    wf.alive = False
                continue

            # Check reflection off obstacles.
            if not wf.reflected:
                for obs_pos in obs_positions:
                    if (wf.direction > 0
                            and wf.pos >= obs_pos - half_obs
                            and wf.source < obs_pos):
                        # Hit obstacle from the left.
                        wf.pos = obs_pos - half_obs
                        wf.direction = -1
                        wf.reflected = True
                        break
                    elif (wf.direction < 0
                            and wf.pos <= obs_pos + half_obs
                            and wf.source > obs_pos):
                        # Hit obstacle from the right.
                        wf.pos = obs_pos + half_obs
                        wf.direction = +1
                        wf.reflected = True
                        break

            # Check absorption at source (only after reflecting).
            if wf.reflected:
                if wf.direction < 0 and wf.pos <= wf.source:
                    wf.absorbed = True
                elif wf.direction > 0 and wf.pos >= wf.source:
                    wf.absorbed = True

            # Kill if off the string entirely.
            if wf.pos < -2 or wf.pos > bulb_count + 2:
                wf.alive = False

        # Prune dead wavefronts.
        self._wavefronts = [wf for wf in self._wavefronts if wf.alive]

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the sonar effect.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total number of zones across all devices.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        zpb: int = max(1, int(self.zones_per_bulb))
        bulb_count: int = max(1, zone_count // zpb)

        if not self._initialized:
            self._init_state(zone_count)
            self._prev_t = t

        dt: float = t - self._prev_t
        self._prev_t = t

        # Clamp dt to avoid huge jumps on first frame or lag spikes.
        if dt > 0.5:
            dt = 0.05

        # Drift obstacles.
        self._drift_obstacles(t, dt, bulb_count)

        # Recompute source positions after obstacle drift.
        self._update_sources(bulb_count)

        # Emit new pulses.
        self._emit_pulses(t, bulb_count)

        # Advance wavefronts.
        self._update_wavefronts(dt, bulb_count)

        # --- Render to zone buffer ---
        # Start with black.
        brightness_buf: list[float] = [0.0] * zone_count
        peak_bri: float = pct_to_u16(self.brightness) / float(HSBK_MAX)

        # Paint wavefronts and their tails.
        # Brightness fades linearly by spatial distance from the head,
        # not by trail-entry index (which is frame-rate dependent).
        tail_bulbs: float = max(0.01, self.tail / zpb)

        for wf in self._wavefronts:
            all_points: list[float] = wf.trail + [wf.pos]
            head: float = wf.pos
            for bulb_pos in all_points:
                # Fraction 1.0 at head, 0.0 at tail_bulbs away.
                dist: float = abs(bulb_pos - head)
                frac: float = max(0.0, 1.0 - dist / tail_bulbs)
                bri: float = frac * peak_bri

                # Map bulb position to zone range.
                zone_start: int = int(bulb_pos * zpb)
                zone_end: int = zone_start + zpb
                for z in range(max(0, zone_start), min(zone_count, zone_end)):
                    # Additive — multiple wavefronts can overlap.
                    brightness_buf[z] = min(1.0, brightness_buf[z] + bri)

        # Build the output color buffer.
        colors: list[HSBK] = []
        obs_hue: int = hue_to_u16(self.obstacle_hue)
        obs_bri: int = pct_to_u16(self.obstacle_brightness)
        half_obs_zones: int = max(1, (OBSTACLE_WIDTH_BULBS * zpb) // 2)

        # Pre-compute obstacle zone ranges for painting.
        obs_zones: set[int] = set()
        for obs in self._obstacles:
            center_zone: int = int(obs.pos * zpb)
            for z in range(center_zone - half_obs_zones,
                           center_zone + half_obs_zones + 1):
                if 0 <= z < zone_count:
                    obs_zones.add(z)

        for z in range(zone_count):
            if z in obs_zones:
                # Obstacle zone — colored marker.
                colors.append((obs_hue, HSBK_MAX, obs_bri, self.kelvin))
            else:
                # Wavefront / background zone.
                bri_val: int = int(brightness_buf[z] * HSBK_MAX)
                colors.append((0, WAVEFRONT_SAT, bri_val, self.kelvin))

        return colors
