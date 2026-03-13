"""Sound level effect — bulb brightness tracks ambient audio energy.

Designed for white-only bulbs (LIFX Mini White) but works on any device.
Reads audio signals from the media bus and maps them to brightness,
with optional color temperature shift based on spectral centroid
(brighter sound = cooler white).  Beat pulses produce brief brightness
flashes above the ambient level.

Without a signal bus connected, the bulbs hold steady at the configured
base brightness — graceful degradation, never dark.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math

from . import (
    MediaEffect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Beat flash decay time in seconds.
BEAT_FLASH_DECAY: float = 0.3

# Minimum brightness floor (prevents bulbs from going fully dark).
MIN_BRIGHTNESS_PCT: float = 5.0

# Exponential smoothing factor for brightness transitions.
# Lower = smoother but more latent; higher = snappier but jittery.
SMOOTH_ALPHA: float = 0.4


class SoundLevel(MediaEffect):
    """Brightness tracks ambient sound level from a camera microphone.

    The effect reads the ``rms`` (overall loudness), ``centroid``
    (tonal brightness), and ``beat`` (transient pulse) signals from
    the configured audio source.  These are mapped to bulb brightness
    and color temperature so the lights breathe with the room's
    acoustic energy.

    Best on white-only bulbs (Mini White) but works on any LIFX device.
    Without a media bus, holds steady at *base_brightness*.
    """

    name = "soundlevel"
    description = "Brightness tracks ambient sound from a camera mic"

    # -- Tunable parameters --------------------------------------------------

    source = Param(
        "foyer", description="Media source name (matches server config)",
    )
    base_brightness = Param(
        30, min=5, max=100,
        description="Brightness floor when silent (percent)",
    )
    max_brightness = Param(
        100, min=20, max=100,
        description="Brightness ceiling at peak volume (percent)",
    )
    sensitivity = Param(
        1.0, min=0.1, max=5.0,
        description="Audio sensitivity multiplier",
    )
    beat_flash = Param(
        20, min=0, max=50,
        description="Extra brightness on beat (percent, 0 to disable)",
    )
    kelvin_shift = Param(
        True, description="Shift color temp with spectral centroid",
    )
    warm_kelvin = Param(
        2700, min=KELVIN_MIN, max=KELVIN_MAX,
        description="Color temp for bass-heavy audio",
    )
    cool_kelvin = Param(
        5000, min=KELVIN_MIN, max=KELVIN_MAX,
        description="Color temp for treble-heavy audio",
    )

    def __init__(self, **overrides) -> None:
        """Initialize with smoothing state.

        Args:
            **overrides: Parameter overrides.
        """
        super().__init__(**overrides)
        # Smoothed brightness value (avoids jitter).
        self._smooth_bri: float = float(self.base_brightness) / 100.0
        # Beat flash remaining intensity [0, 1].
        self._flash: float = 0.0
        self._last_beat: float = 0.0

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame — all zones get the same brightness.

        Args:
            t:          Seconds since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            List of *zone_count* identical HSBK tuples.
        """
        # Read signals from the bus (defaults if bus unavailable).
        rms: float = float(self.signal(
            f"{self.source}:audio:rms", 0.0,
        ))
        centroid: float = float(self.signal(
            f"{self.source}:audio:centroid", 0.5,
        ))
        beat: float = float(self.signal(
            f"{self.source}:audio:beat", 0.0,
        ))

        # --- Brightness from RMS ---
        # Scale RMS by sensitivity, clamp to [0, 1].
        scaled_rms: float = min(1.0, rms * self.sensitivity)

        # Map to brightness range.
        base: float = self.base_brightness / 100.0
        ceiling: float = self.max_brightness / 100.0
        target_bri: float = base + scaled_rms * (ceiling - base)

        # Smooth the transition.
        self._smooth_bri += SMOOTH_ALPHA * (target_bri - self._smooth_bri)

        # --- Beat flash ---
        if beat > 0.5 and self._flash < 0.1:
            self._flash = 1.0
            self._last_beat = t
        elif self._flash > 0.0:
            elapsed: float = t - self._last_beat
            self._flash = max(0.0, 1.0 - elapsed / BEAT_FLASH_DECAY)

        flash_add: float = self._flash * (self.beat_flash / 100.0)
        final_bri: float = min(1.0, self._smooth_bri + flash_add)

        # Enforce minimum brightness floor.
        final_bri = max(MIN_BRIGHTNESS_PCT / 100.0, final_bri)

        # --- Color temperature ---
        if self.kelvin_shift:
            # Centroid [0, 1]: 0 = bassy (warm), 1 = trebly (cool).
            kelvin: int = int(
                self.warm_kelvin
                + centroid * (self.cool_kelvin - self.warm_kelvin)
            )
            kelvin = max(KELVIN_MIN, min(KELVIN_MAX, kelvin))
        else:
            kelvin = KELVIN_DEFAULT

        # Build HSBK: saturation 0 for white light.
        brightness_u16: int = int(final_bri * HSBK_MAX)
        color: HSBK = (0, 0, brightness_u16, kelvin)

        return [color] * zone_count

    def period(self) -> None:
        """Sound level is aperiodic — no seamless loop point.

        Returns:
            ``None`` always.
        """
        return None
