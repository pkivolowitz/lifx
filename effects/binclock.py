"""Binary clock effect — displays the current time in binary across the string.

***************************************************************************
**NOTE** DESIGNED FOR THREE 12 BULB STRING LIGHTS CHAINED TOGETHER **NOTE**
***************************************************************************

The full 108-zone string displays the time as binary digits:

    HH (4 bits × 6 zones = 24)  gap (6)  MM (6 bits × 6 zones = 36)  gap (6)  SS (6 bits × 6 zones = 36) = 108

Hours use 4 bits (0–23), minutes 6 bits (0–59), seconds 6 bits (0–59).
Each bit is represented by 6 zones (2 physical bulbs × 3 zones per bulb)
so that bit boundaries always align with physical bulb boundaries,
preventing the half-brightness blending that occurs when adjacent zones
within the same bulb have different colors.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.2"

import time

from . import (
    DEVICE_TYPE_STRIP,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Bit widths for each time component.
HOUR_BITS: int = 4
MINUTE_BITS: int = 6
SECOND_BITS: int = 6

# Total bits per time display.
TOTAL_BITS: int = HOUR_BITS + MINUTE_BITS + SECOND_BITS

# Number of zones per physical bulb on LIFX string lights.
ZONES_PER_BULB: int = 3

# Number of physical bulbs used to represent a single bit.
BULBS_PER_BIT: int = 2

# Number of zones per bit (must be a multiple of ZONES_PER_BULB).
ZONES_PER_BIT: int = BULBS_PER_BIT * ZONES_PER_BULB

# Number of physical bulbs used for gap separators.
BULBS_PER_GAP: int = 2

# Number of zones per gap separator.
ZONES_PER_GAP: int = BULBS_PER_GAP * ZONES_PER_BULB

# Number of gap sections (between HH/MM and MM/SS).
GAP_COUNT: int = 2

# Total zones for the full display:
#   16 bits × 6 zones/bit + 2 gaps × 6 zones/gap = 96 + 12 = 108
TOTAL_DISPLAY_ZONES: int = TOTAL_BITS * ZONES_PER_BIT + GAP_COUNT * ZONES_PER_GAP


def _time_to_bits(hours: int, minutes: int, seconds: int) -> list[bool]:
    """Convert h/m/s into a flat list of booleans, MSB first per group.

    The result is 16 bools: 4 for hours, 6 for minutes, 6 for seconds.

    Args:
        hours:   Hour value (0–23).
        minutes: Minute value (0–59).
        seconds: Second value (0–59).

    Returns:
        A list of 16 booleans representing the binary time.
    """
    bits: list[bool] = []

    # Hours: 4 bits, MSB first.
    for shift in range(HOUR_BITS - 1, -1, -1):
        bits.append(bool((hours >> shift) & 1))

    # Minutes: 6 bits, MSB first.
    for shift in range(MINUTE_BITS - 1, -1, -1):
        bits.append(bool((minutes >> shift) & 1))

    # Seconds: 6 bits, MSB first.
    for shift in range(SECOND_BITS - 1, -1, -1):
        bits.append(bool((seconds >> shift) & 1))

    return bits


def _bits_to_zones(
    bits: list[bool],
    hour_on: HSBK,
    hour_off: HSBK,
    min_on: HSBK,
    min_off: HSBK,
    sec_on: HSBK,
    sec_off: HSBK,
    gap_color: HSBK,
) -> list[HSBK]:
    """Expand 16 time bits into 108 zone colors for the full string.

    Layout:
        [HH 4 bits × 6 zones] [gap × 6] [MM 6 bits × 6 zones] [gap × 6] [SS 6 bits × 6 zones]

    Each bit spans 6 zones (2 physical bulbs × 3 zones/bulb) so that
    bit boundaries align with physical bulb boundaries.  Hours, minutes,
    and seconds each use a distinct color for easy visual identification.

    Args:
        bits:      16-element boolean list from :func:`_time_to_bits`.
        hour_on:   HSBK for a "1" hour bit.
        hour_off:  HSBK for a "0" hour bit.
        min_on:    HSBK for a "1" minute bit.
        min_off:   HSBK for a "0" minute bit.
        sec_on:    HSBK for a "1" second bit.
        sec_off:   HSBK for a "0" second bit.
        gap_color: HSBK for gap/separator zones.

    Returns:
        A list of 108 HSBK tuples for the full display.
    """
    zones: list[HSBK] = []
    bit_idx: int = 0

    # Hours: HOUR_BITS bits × ZONES_PER_BIT zones each.
    for _ in range(HOUR_BITS):
        color: HSBK = hour_on if bits[bit_idx] else hour_off
        zones.extend([color] * ZONES_PER_BIT)
        bit_idx += 1

    # Gap between hours and minutes.
    zones.extend([gap_color] * ZONES_PER_GAP)

    # Minutes: MINUTE_BITS bits × ZONES_PER_BIT zones each.
    for _ in range(MINUTE_BITS):
        color = min_on if bits[bit_idx] else min_off
        zones.extend([color] * ZONES_PER_BIT)
        bit_idx += 1

    # Gap between minutes and seconds.
    zones.extend([gap_color] * ZONES_PER_GAP)

    # Seconds: SECOND_BITS bits × ZONES_PER_BIT zones each.
    for _ in range(SECOND_BITS):
        color = sec_on if bits[bit_idx] else sec_off
        zones.extend([color] * ZONES_PER_BIT)
        bit_idx += 1

    return zones


class BinClock(Effect):
    """Display the current time in binary across the string lights.

    The full 108-zone string encodes hours (4 bits), minutes (6 bits),
    and seconds (6 bits).  Each bit spans 2 physical bulbs (6 zones)
    so boundaries align with bulb housings, preventing half-brightness
    blending.  Gaps of 2 physical bulbs separate the groups.
    """

    name: str = "binclock"
    description: str = "Display the current time in binary"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_STRIP})

    hour_hue = Param(0.0, min=0.0, max=360.0,
                     description="Hour hue in degrees (0=red)")
    min_hue = Param(120.0, min=0.0, max=360.0,
                    description="Minute hue in degrees (120=green)")
    sec_hue = Param(240.0, min=0.0, max=360.0,
                    description="Second hue in degrees (240=blue)")
    brightness = Param(80, min=0, max=100,
                       description="'On' bit brightness percent")
    off_bri = Param(0, min=0, max=100,
                    description="'Off' bit brightness percent")
    gap_bri = Param(0, min=0, max=100,
                    description="Gap brightness percent (0=dark)")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame showing the current wall-clock time in binary.

        The ``t`` parameter is unused — this effect reads the real clock
        so the display stays accurate regardless of when the effect started.

        Args:
            t:          Seconds elapsed since effect started (unused).
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        now = time.localtime()

        bits: list[bool] = _time_to_bits(now.tm_hour, now.tm_min, now.tm_sec)

        # Build color values from parameters.
        h_hue: int = hue_to_u16(self.hour_hue)
        m_hue: int = hue_to_u16(self.min_hue)
        s_hue: int = hue_to_u16(self.sec_hue)
        on_bri: int = pct_to_u16(self.brightness)
        off_b: int = pct_to_u16(self.off_bri)
        gap_b: int = pct_to_u16(self.gap_bri)

        # Each time group gets its own hue for visual distinction.
        hour_on: HSBK = (h_hue, HSBK_MAX, on_bri, self.kelvin)
        hour_off: HSBK = (h_hue, HSBK_MAX, off_b, self.kelvin)
        min_on: HSBK = (m_hue, HSBK_MAX, on_bri, self.kelvin)
        min_off: HSBK = (m_hue, HSBK_MAX, off_b, self.kelvin)
        sec_on: HSBK = (s_hue, HSBK_MAX, on_bri, self.kelvin)
        sec_off: HSBK = (s_hue, HSBK_MAX, off_b, self.kelvin)
        gap_color: HSBK = (0, 0, gap_b, self.kelvin)

        display: list[HSBK] = _bits_to_zones(
            bits, hour_on, hour_off, min_on, min_off, sec_on, sec_off, gap_color,
        )

        # Zone 0 is at the far end of the physical string, so reverse
        # the layout to read left-to-right from the viewer's perspective.
        display.reverse()

        # Truncate or pad to match the actual zone count.
        if zone_count <= TOTAL_DISPLAY_ZONES:
            return display[:zone_count]

        # Pad with gap color if device has more zones than expected.
        return display + [gap_color] * (zone_count - TOTAL_DISPLAY_ZONES)
