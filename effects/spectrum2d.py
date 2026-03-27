"""2D audio spectrum visualizer — frequency bars on a pixel grid.

Reads frequency band data from the signal bus and renders vertical bars
across the 2D grid.  Each column maps to a frequency band (interpolated
to fill the width).  Bar height reflects energy.  Color follows a
warm-to-cool gradient from bass (left, red) to treble (right, blue).

A peak-hold indicator (bright pixel at the maximum) decays slowly,
giving a classic VU-meter look.  Beat detection pulses brightness.

Requires an audio source feeding the signal bus (e.g., MicSource).

Works on both 1D strips (single-row spectrum) and 2D matrix emitters.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

from . import (
    DEVICE_TYPE_MATRIX, DEVICE_TYPE_STRIP,
    MediaEffect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Peak hold decay per frame (0.98 = slow decay, 0.90 = fast).
# Per-frame peak decay.  At 20 FPS, 0.85 gives ~300ms half-life
# (responsive), 0.97 gives ~1.5s (sluggish).
PEAK_DECAY: float = 0.85

# Minimum energy to show a peak indicator.
PEAK_THRESHOLD: float = 0.05

# Hue range: 0.0 (red/bass) to 0.66 (blue/treble) of the color wheel.
HUE_RANGE: float = 0.66

# Bar brightness range (fraction of HSBK_MAX).
BAR_BRI_MIN: float = 0.25
BAR_BRI_MAX: float = 0.76

# Peak indicator brightness and desaturation.
PEAK_BRI: float = 0.90
PEAK_SAT: float = 0.50

# Default grid dimensions (overridden by viewer).
DEFAULT_WIDTH: int = 78
DEFAULT_HEIGHT: int = 22

# Default band count when signal bus returns empty list.
FALLBACK_BANDS: int = 8


class Spectrum2D(MediaEffect):
    """2D audio spectrum visualizer with peak hold.

    Vertical frequency bars fill the grid from bottom to top.
    Color maps bass (left) to warm hues and treble (right) to cool.
    A peak-hold indicator marks the maximum for each column.
    Beat detection modulates overall brightness.
    """

    name: str = "spectrum2d"
    description: str = "2D audio spectrum — frequency bars with peak hold"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX, DEVICE_TYPE_STRIP})

    source = Param("mic", description="Audio source name")
    width = Param(DEFAULT_WIDTH, min=4, max=500,
                  description="Grid width in pixels (set by viewer)")
    height = Param(DEFAULT_HEIGHT, min=4, max=300,
                   description="Grid height in pixels (set by viewer)")
    brightness = Param(76, min=10, max=100,
                       description="Peak bar brightness (percent)")
    peak_hold = Param(True, description="Show peak hold indicators")

    def on_start(self, zone_count: int) -> None:
        """Initialize peak tracking state.

        Args:
            zone_count: Total pixel count (width * height).
        """
        self._peaks: list[float] = [0.0] * int(self.width)

    def period(self) -> None:
        """Audio-reactive — no periodic loop."""
        return None

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the 2D spectrum.

        Reads frequency bands from the signal bus, interpolates them
        to fill the grid width, and renders vertical bars with peak
        indicators.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Total pixels (should equal width * height).

        Returns:
            A list of ``width * height`` HSBK tuples in row-major order.
        """
        w: int = int(self.width)
        h: int = int(self.height)
        src: str = self.source
        bri_scale: float = self.brightness / 100.0

        # Read audio signals from the bus.
        bands: list[float] = self.signal(
            f"{src}:audio:bands", [0.0] * FALLBACK_BANDS
        )
        beat: float = self.signal(f"{src}:audio:beat", 0.0)

        n_bands: int = len(bands) if bands else FALLBACK_BANDS
        if not bands or n_bands == 0:
            bands = [0.0] * FALLBACK_BANDS
            n_bands = FALLBACK_BANDS

        # Interpolate bands to fill the grid width.
        col_energy: list[float] = []
        for x in range(w):
            band_pos: float = x * (n_bands - 1) / max(1, w - 1)
            lo: int = int(band_pos)
            hi: int = min(lo + 1, n_bands - 1)
            frac: float = band_pos - lo
            energy: float = bands[lo] * (1.0 - frac) + bands[hi] * frac
            col_energy.append(energy)

        # Update peak hold.
        if not hasattr(self, '_peaks') or len(self._peaks) != w:
            self._peaks = [0.0] * w
        for x in range(w):
            self._peaks[x] *= PEAK_DECAY
            if col_energy[x] > self._peaks[x]:
                self._peaks[x] = col_energy[x]

        # Beat brightness boost (up to 1.5x on a beat).
        beat_mult: float = 1.0 + beat * 0.5

        # Render the pixel grid (row-major, y=0 is top row).
        colors: list[HSBK] = []
        for y in range(h):
            row_from_bottom: int = h - 1 - y

            for x in range(w):
                bar_height: float = col_energy[x] * h
                peak_row: int = int(self._peaks[x] * (h - 1))

                # Hue: warm (bass/left) to cool (treble/right).
                hue: int = int((x / max(1, w - 1)) * HUE_RANGE * HSBK_MAX)

                if (self.peak_hold
                        and row_from_bottom == peak_row
                        and self._peaks[x] > PEAK_THRESHOLD):
                    # Peak indicator — bright, desaturated.
                    bri: int = int(HSBK_MAX * PEAK_BRI * bri_scale)
                    sat: int = int(HSBK_MAX * PEAK_SAT)
                    colors.append((hue, sat, bri, KELVIN_DEFAULT))

                elif row_from_bottom < bar_height:
                    # Inside the bar — gradient brighter toward top.
                    fill_frac: float = (
                        row_from_bottom / max(1.0, bar_height)
                    )
                    bri_frac: float = (
                        BAR_BRI_MIN + (BAR_BRI_MAX - BAR_BRI_MIN) * fill_frac
                    )
                    bri = int(
                        HSBK_MAX * bri_frac * bri_scale
                        * min(1.5, beat_mult)
                    )
                    bri = min(bri, HSBK_MAX)
                    colors.append((hue, HSBK_MAX, bri, KELVIN_DEFAULT))

                else:
                    # Background — black.
                    colors.append((0, 0, 0, KELVIN_DEFAULT))

        return colors
