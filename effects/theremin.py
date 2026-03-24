"""Theremin effect — sensor-driven zone rendering via SignalBus.

Reads distance signals from the SignalBus (published by the sensor
simulator or ESP32 rangefinders), maps them to note frequency and
amplitude, renders antialiased zone colors, and writes computed
note data back to the SignalBus for the Mac synthesizer.

Pipeline role: **Operator + Emitter renderer**
    Input:  ``theremin:sensor:pitch``  (float, cm)
            ``theremin:sensor:volume`` (float, cm)
    Output: HSBK zone colors (via engine)
            ``theremin:note:frequency``  (float, Hz — via SignalBus)
            ``theremin:note:amplitude``  (float, 0-1 — via SignalBus)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
from typing import Optional

from . import (
    DEVICE_TYPE_STRIP,
    HSBK,
    HSBK_MAX,
    KELVIN_DEFAULT,
    Effect,
    MediaEffect,
    Param,
    hue_to_u16,
)

from theremin import (
    DISTANCE_MAX_CM,
    DISTANCE_MIN_CM,
    FREQ_MAX,
    FREQ_MIN,
    GLOW_HALF_WIDTH,
    HUE_DEGREES_MAX,
    HUE_DEGREES_MIN,
    OCTAVE_SPAN,
    SIGNAL_AMPLITUDE,
    SIGNAL_FREQUENCY,
    SIGNAL_PITCH,
    SIGNAL_VOLUME,
    distance_to_amplitude,
    distance_to_freq,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default distance when no sensor data available (mid-range).
DEFAULT_DISTANCE_CM: float = (DISTANCE_MIN_CM + DISTANCE_MAX_CM) / 2.0

# Minimum brightness to show (prevents invisible dim tails).
MIN_VISIBLE_BRIGHTNESS: int = 500


class Theremin(MediaEffect):
    """Theremin effect — maps rangefinder distances to zone colors.

    Reads pitch and volume distances from the SignalBus, computes
    the corresponding musical note and amplitude, and renders an
    antialiased glow on the target multizone device.  Also writes
    the computed note frequency and amplitude back to the SignalBus
    so the Mac synthesizer can generate the audio tone.
    """

    name: str = "theremin"
    description: str = "Laser rangefinder Theremin — sensor-driven zones"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_STRIP})

    glow_width = Param(
        2.5, min=0.5, max=10.0,
        description="Gaussian glow half-width in zones",
    )

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Render one frame of Theremin zone colors.

        Args:
            t:          Seconds since effect started (unused — sensor-driven).
            zone_count: Number of zones on the target device.

        Returns:
            List of zone_count HSBK tuples.
        """
        # --- Read sensor inputs from SignalBus ---
        pitch_cm: float = float(
            self.signal(SIGNAL_PITCH, DEFAULT_DISTANCE_CM)
        )
        volume_cm: float = float(
            self.signal(SIGNAL_VOLUME, DEFAULT_DISTANCE_CM)
        )

        # --- Operator logic: map distances to musical parameters ---
        frequency: float = distance_to_freq(pitch_cm)
        amplitude: float = distance_to_amplitude(volume_cm)

        # --- Write note output to SignalBus (→ MQTT → Mac synth) ---
        if self._signal_bus is not None:
            self._signal_bus.write(SIGNAL_FREQUENCY, round(frequency, 2))
            self._signal_bus.write(SIGNAL_AMPLITUDE, round(amplitude, 4))

        # --- Render antialiased zone colors ---
        # Zone position: log-scale mapping from frequency.
        t_freq: float = math.log2(frequency / FREQ_MIN) / OCTAVE_SPAN
        t_freq = max(0.0, min(1.0, t_freq))
        zone_pos: float = t_freq * (zone_count - 1)

        # Hue: low frequency = red, high frequency = violet.
        hue_degrees: float = (
            HUE_DEGREES_MIN + t_freq * (HUE_DEGREES_MAX - HUE_DEGREES_MIN)
        )
        hue: int = hue_to_u16(hue_degrees)

        # Build zone colors with Gaussian glow.
        max_bri: float = amplitude * HSBK_MAX
        half_w: float = self.glow_width
        colors: list[HSBK] = []

        for z in range(zone_count):
            dist: float = abs(z - zone_pos)

            if dist > half_w * 2.0:
                # Too far from center — dark.
                colors.append((0, 0, 0, KELVIN_DEFAULT))
            else:
                # Gaussian falloff.
                weight: float = math.exp(
                    -(dist * dist) / (2.0 * half_w * half_w)
                )
                bri: int = int(max_bri * weight)
                if bri < MIN_VISIBLE_BRIGHTNESS and amplitude > 0.05:
                    bri = 0  # Cut off dim tails cleanly.
                colors.append((hue, HSBK_MAX, bri, KELVIN_DEFAULT))

        return colors

    def period(self) -> Optional[float]:
        """Sensor-driven — no periodic cycle.

        Returns:
            None (aperiodic effect).
        """
        return None
