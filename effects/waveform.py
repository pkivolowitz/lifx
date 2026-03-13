"""Waveform effect — audio frequency spectrum mapped to zones.

Each zone represents a frequency band.  Low frequencies (bass) on one
end, high frequencies (treble) on the other.  Band energy controls
brightness, and optionally hue shifts from warm (bass) to cool (treble).

Designed for multizone devices (string lights, light strips) where the
spatial layout makes the frequency decomposition visible.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math

from . import (
    MediaEffect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Beat flash decay time in seconds.
BEAT_FLASH_DECAY: float = 0.2

# Smoothing for brightness transitions per zone.
SMOOTH_ALPHA: float = 0.35


class Waveform(MediaEffect):
    """Audio frequency spectrum visualizer for multizone devices.

    Maps N frequency bands across the device's zones so that bass
    appears at one end and treble at the other.  Each zone's
    brightness tracks its frequency band's energy in real time.

    The hue gradient runs from warm colors (bass) to cool colors
    (treble), creating a natural visual mapping of the audio spectrum.
    """

    name = "waveform"
    description = "Audio spectrum visualizer across zones"

    # -- Tunable parameters --------------------------------------------------

    source = Param(
        "foyer", description="Media source name (matches server config)",
    )
    bass_hue = Param(
        0, min=0, max=360,
        description="Hue at the bass end (degrees)",
    )
    treble_hue = Param(
        240, min=0, max=360,
        description="Hue at the treble end (degrees)",
    )
    min_brightness = Param(
        5, min=0, max=50,
        description="Minimum zone brightness (percent)",
    )
    max_brightness = Param(
        100, min=20, max=100,
        description="Maximum zone brightness (percent)",
    )
    sensitivity = Param(
        1.5, min=0.1, max=5.0,
        description="Audio sensitivity multiplier",
    )
    saturation = Param(
        100, min=0, max=100,
        description="Color saturation (0 = white, 100 = full color)",
    )
    beat_flash = Param(
        30, min=0, max=100,
        description="Extra brightness on beat (percent, 0 to disable)",
    )

    def __init__(self, **overrides) -> None:
        """Initialize with per-zone smoothing state.

        Args:
            **overrides: Parameter overrides.
        """
        super().__init__(**overrides)
        # Per-zone smoothed brightness (allocated on first render).
        self._smooth: list[float] = []
        self._flash: float = 0.0
        self._last_beat: float = 0.0

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame — each zone maps to a frequency band.

        Args:
            t:          Seconds since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            List of *zone_count* HSBK tuples.
        """
        # Initialize smoothing array on first call or zone count change.
        if len(self._smooth) != zone_count:
            self._smooth = [0.0] * zone_count

        # Read signals.
        bands: list[float] = self.signal(
            f"{self.source}:audio:bands", [0.0] * 8,
        )
        beat: float = float(self.signal(
            f"{self.source}:audio:beat", 0.0,
        ))

        # Beat flash.
        if beat > 0.5 and self._flash < 0.1:
            self._flash = 1.0
            self._last_beat = t
        elif self._flash > 0.0:
            elapsed: float = t - self._last_beat
            self._flash = max(0.0, 1.0 - elapsed / BEAT_FLASH_DECAY)

        flash_add: float = self._flash * (self.beat_flash / 100.0)

        n_bands: int = len(bands) if isinstance(bands, list) else 8
        if n_bands == 0:
            n_bands = 8
            bands = [0.0] * 8

        # Precompute param values.
        min_bri: float = self.min_brightness / 100.0
        max_bri: float = self.max_brightness / 100.0
        sat_u16: int = pct_to_u16(self.saturation)

        colors: list[HSBK] = []
        for z in range(zone_count):
            # Map zone to a position in the band array [0, n_bands).
            # Interpolate between bands for smooth spatial mapping.
            pos: float = z / max(1, zone_count - 1) * (n_bands - 1)
            lo: int = int(pos)
            hi: int = min(lo + 1, n_bands - 1)
            frac: float = pos - lo

            # Linearly interpolate band energy.
            energy: float = bands[lo] * (1.0 - frac) + bands[hi] * frac

            # Apply sensitivity.
            energy = min(1.0, energy * self.sensitivity)

            # Smooth the transition.
            self._smooth[z] += SMOOTH_ALPHA * (energy - self._smooth[z])

            # Map to brightness range with beat flash.
            bri: float = min_bri + self._smooth[z] * (max_bri - min_bri)
            bri = min(1.0, bri + flash_add)
            bri_u16: int = int(bri * HSBK_MAX)

            # Hue: gradient from bass_hue to treble_hue across zones.
            zone_frac: float = z / max(1, zone_count - 1)
            hue_deg: float = (
                self.bass_hue + zone_frac * (self.treble_hue - self.bass_hue)
            )
            hue_u16: int = hue_to_u16(hue_deg % 360.0)

            colors.append((hue_u16, sat_u16, bri_u16, KELVIN_DEFAULT))

        return colors

    def period(self) -> None:
        """Waveform is aperiodic — driven by live audio.

        Returns:
            ``None`` always.
        """
        return None
