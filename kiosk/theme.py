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


# Day theme — monochromatic seafoam on pure-hue teal shades.
# Derived from Benjamin Moore 2039-60 "Seafoam" (#b9efe1, HLS 164.4°).
# Backgrounds use S=1.0 with low L (pure hue + black) so they never
# drift into neutral/brown territory; text stays near-white with a
# slight hue tint for subtle cohesion.
DAY = Theme(
    bg=(0, 61, 45),
    clock=(255, 255, 255),
    date=(214, 235, 230),
    ampm=(255, 255, 255),  # unused; AM/PM ditched
    card_bg=(0, 0, 0, 0),  # transparent — canvas bg shows through
    card_border=(130, 230, 195, 255),
    text=(240, 245, 243),
    label=(159, 223, 207),
    dim=(112, 169, 154),
    ok=(99, 233, 198),
    bad=(178, 255, 235),
    warn=(158, 250, 226),
    locked=(99, 233, 198),
    unlocked=(178, 255, 235),
    temp=(255, 255, 255),
    humid=(159, 223, 207),
    soil_wet=(99, 233, 198),
    soil_dry=(158, 250, 226),
    soil_critical=(178, 255, 235),
)

# Night theme — bedroom dark adaptation.
# Two colors only, by request: a dim red for everything benign and an
# alert red for anything dangerous.  No greens, no ambers, no per-role
# tints — the wallclock at night should be readable but never bright,
# and the eye should immediately see RED-RED as "something is wrong."
# Background is near-black with a faint red tint so the bezel doesn't
# look like a pure void next to the dim text.
_NIGHT_DIM: tuple[int, int, int] = (70, 20, 10)
_NIGHT_ALERT: tuple[int, int, int] = (180, 35, 15)
_NIGHT_BG: tuple[int, int, int] = (10, 3, 2)

NIGHT = Theme(
    bg=_NIGHT_BG,
    clock=_NIGHT_DIM,
    date=_NIGHT_DIM,
    ampm=_NIGHT_DIM,  # unused — kept for Theme schema
    # No card backgrounds and no borders at night — Perry's call.
    # The dim text against the near-black canvas is the entire UI;
    # extra rectangles only add visual noise and stray photons.
    # _draw_card skips both when alpha is 0.
    card_bg=(0, 0, 0, 0),
    card_border=(0, 0, 0, 0),
    text=_NIGHT_DIM,
    label=_NIGHT_DIM,
    dim=_NIGHT_DIM,
    ok=_NIGHT_DIM,
    bad=_NIGHT_ALERT,
    warn=_NIGHT_ALERT,
    locked=_NIGHT_DIM,
    unlocked=_NIGHT_ALERT,
    temp=_NIGHT_DIM,
    humid=_NIGHT_DIM,
    soil_wet=_NIGHT_DIM,
    soil_dry=_NIGHT_DIM,
    soil_critical=_NIGHT_ALERT,
)
