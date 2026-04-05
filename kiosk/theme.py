"""Color themes for day and night modes.

Night mode uses deep red/amber tones to preserve dark adaptation
in the bedroom.  Day mode uses warm whites on dark loam.

All colors are (R, G, B) tuples for pygame.
"""

__version__: str = "1.0"

from typing import NamedTuple


class Theme(NamedTuple):
    """Complete color theme for the kiosk."""

    # Background.
    bg: tuple[int, int, int]

    # Clock.
    clock: tuple[int, int, int]
    date: tuple[int, int, int]
    ampm: tuple[int, int, int]

    # Cards.
    card_bg: tuple[int, int, int, int]
    card_border: tuple[int, int, int, int]

    # Text.
    text: tuple[int, int, int]
    label: tuple[int, int, int]
    dim: tuple[int, int, int]

    # Status indicators.
    ok: tuple[int, int, int]
    bad: tuple[int, int, int]
    warn: tuple[int, int, int]

    # Locks.
    locked: tuple[int, int, int]
    unlocked: tuple[int, int, int]

    # Weather.
    temp: tuple[int, int, int]
    humid: tuple[int, int, int]

    # Soil.
    soil_wet: tuple[int, int, int]
    soil_dry: tuple[int, int, int]
    soil_critical: tuple[int, int, int]


# Day theme — warm whites on dark loam.
DAY = Theme(
    bg=(44, 34, 24),
    clock=(255, 255, 255),
    date=(255, 255, 255),
    ampm=(180, 180, 180),
    card_bg=(62, 50, 38, 200),
    card_border=(140, 115, 80, 76),
    text=(255, 255, 255),
    label=(200, 200, 200),
    dim=(140, 140, 140),
    ok=(102, 187, 106),
    bad=(239, 83, 80),
    warn=(255, 183, 77),
    locked=(102, 187, 106),
    unlocked=(239, 83, 80),
    temp=(255, 255, 255),
    humid=(200, 200, 200),
    soil_wet=(102, 187, 106),
    soil_dry=(255, 183, 77),
    soil_critical=(239, 83, 80),
)

# Night theme — deep red/amber for bedroom dark adaptation.
NIGHT = Theme(
    bg=(20, 5, 5),
    clock=(140, 40, 30),
    date=(120, 35, 25),
    ampm=(80, 25, 15),
    card_bg=(40, 12, 8, 180),
    card_border=(80, 30, 20, 60),
    text=(140, 50, 35),
    label=(100, 35, 25),
    dim=(70, 25, 15),
    ok=(80, 50, 20),
    bad=(140, 30, 20),
    warn=(120, 50, 15),
    locked=(80, 50, 20),
    unlocked=(140, 30, 20),
    temp=(140, 50, 35),
    humid=(100, 35, 25),
    soil_wet=(80, 50, 20),
    soil_dry=(120, 50, 15),
    soil_critical=(140, 30, 20),
)
