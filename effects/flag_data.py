"""National flag color database for the flag effect.

Each flag is represented as a sequence of color stripes ordered left-to-right
(vertical flags like France) or top-to-bottom (horizontal flags like Germany).
Complex designs (crosses, coats of arms, crescents) are simplified to their
dominant stripe pattern.

Colors are stored as ``(hue_degrees, saturation_pct, brightness_pct)`` tuples.
Brightness values are *relative* -- the effect's brightness parameter scales
the final output, so 100 here means "full brightness of whatever the user set."

The database covers approximately 200 countries plus a handful of special
flags (pride, EU).  Add new entries by appending to :data:`FLAGS`.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"


# ---------------------------------------------------------------------------
# Standard flag colors -- (hue_degrees, saturation_pct, brightness_pct)
# ---------------------------------------------------------------------------
# Tuned for LIFX LED rendering: saturated and bright enough to read well
# on string lights.  Very dark shades (navy, maroon) are kept above ~35 %
# brightness so they remain visible at moderate effect brightness levels.

StripeColor = tuple[float, float, float]
"""Type alias for a single stripe: ``(hue_deg, sat_pct, bri_pct)``."""

# Reds
RED:        StripeColor = (0.0,   100.0, 100.0)
DARK_RED:   StripeColor = (350.0, 100.0, 65.0)
MAROON:     StripeColor = (0.0,   100.0, 35.0)

# Whites and neutrals
WHITE:      StripeColor = (0.0,   0.0,   100.0)
BLACK:      StripeColor = (0.0,   0.0,   5.0)

# Blues
BLUE:       StripeColor = (240.0, 100.0, 100.0)
NAVY:       StripeColor = (230.0, 100.0, 50.0)
ROYAL_BLUE: StripeColor = (220.0, 90.0,  70.0)
SKY_BLUE:   StripeColor = (197.0, 55.0,  100.0)
LIGHT_BLUE: StripeColor = (210.0, 45.0,  100.0)

# Greens
GREEN:      StripeColor = (120.0, 100.0, 75.0)
DARK_GREEN: StripeColor = (150.0, 100.0, 35.0)

# Yellows and golds
YELLOW:     StripeColor = (60.0,  100.0, 100.0)
GOLD:       StripeColor = (48.0,  100.0, 100.0)

# Oranges
ORANGE:     StripeColor = (30.0,  100.0, 100.0)
SAFFRON:    StripeColor = (24.0,  100.0, 100.0)

# Others
TEAL:       StripeColor = (175.0, 100.0, 55.0)
PURPLE:     StripeColor = (270.0, 100.0, 60.0)
PINK:       StripeColor = (330.0, 55.0,  100.0)


# ---------------------------------------------------------------------------
# Flag database -- alphabetical by common English name
# ---------------------------------------------------------------------------

FLAGS: dict[str, list[StripeColor]] = {

    # --- A ----------------------------------------------------------------
    "afghanistan":        [BLACK, RED, GREEN],
    "albania":            [RED, BLACK, RED],
    "algeria":            [GREEN, WHITE],
    "andorra":            [BLUE, YELLOW, RED],
    "angola":             [RED, BLACK],
    "antigua":            [RED, BLACK, LIGHT_BLUE, WHITE, YELLOW],
    "argentina":          [SKY_BLUE, WHITE, SKY_BLUE],
    "armenia":            [RED, BLUE, ORANGE],
    "australia":          [NAVY, WHITE, RED],
    "austria":            [RED, WHITE, RED],
    "azerbaijan":         [BLUE, RED, GREEN],

    # --- B ----------------------------------------------------------------
    "bahamas":            [SKY_BLUE, GOLD, SKY_BLUE],
    "bahrain":            [WHITE, RED],
    "bangladesh":         [DARK_GREEN, RED, DARK_GREEN],
    "barbados":           [BLUE, GOLD, BLUE],
    "belarus":            [RED, GREEN],
    "belgium":            [BLACK, YELLOW, RED],
    "belize":             [BLUE, RED, BLUE],
    "benin":              [GREEN, YELLOW, RED],
    "bhutan":             [YELLOW, ORANGE],
    "bolivia":            [RED, YELLOW, GREEN],
    "bosnia":             [BLUE, YELLOW, BLUE],
    "botswana":           [LIGHT_BLUE, BLACK, LIGHT_BLUE],
    "brazil":             [GREEN, YELLOW, GREEN],
    "brunei":             [YELLOW, WHITE, BLACK],
    "bulgaria":           [WHITE, GREEN, RED],
    "burkina-faso":       [RED, GREEN],
    "burundi":            [RED, WHITE, GREEN],

    # --- C ----------------------------------------------------------------
    "cambodia":           [BLUE, RED, BLUE],
    "cameroon":           [GREEN, RED, YELLOW],
    "canada":             [RED, WHITE, RED],
    "cape-verde":         [BLUE, WHITE, RED],
    "car":                [BLUE, WHITE, GREEN, YELLOW],
    "chad":               [BLUE, YELLOW, RED],
    "chile":              [WHITE, RED, BLUE],
    "china":              [RED, YELLOW, RED],
    "colombia":           [YELLOW, BLUE, RED],
    "comoros":             [GREEN, WHITE, RED, BLUE],
    "congo":              [GREEN, YELLOW, RED],
    "congo-dr":           [BLUE, YELLOW, RED],
    "costa-rica":         [BLUE, WHITE, RED, WHITE, BLUE],
    "croatia":            [RED, WHITE, BLUE],
    "cuba":               [BLUE, WHITE, BLUE, WHITE, BLUE],
    "cyprus":              [WHITE, GREEN, WHITE],
    "czechia":            [WHITE, RED, BLUE],

    # --- D ----------------------------------------------------------------
    "denmark":            [RED, WHITE, RED],
    "djibouti":           [LIGHT_BLUE, GREEN],
    "dominica":           [GREEN, YELLOW, BLACK, WHITE, RED],
    "dominican-republic": [RED, WHITE, BLUE, WHITE, RED],

    # --- E ----------------------------------------------------------------
    "east-timor":         [RED, YELLOW, BLACK],
    "ecuador":            [YELLOW, BLUE, RED],
    "egypt":              [RED, WHITE, BLACK],
    "el-salvador":        [BLUE, WHITE, BLUE],
    "equatorial-guinea":  [GREEN, WHITE, RED, BLUE],
    "eritrea":            [BLUE, GREEN, RED],
    "estonia":            [BLUE, BLACK, WHITE],
    "eswatini":           [BLUE, YELLOW, RED, YELLOW, BLUE],
    "ethiopia":           [GREEN, YELLOW, RED],

    # --- F ----------------------------------------------------------------
    "fiji":               [LIGHT_BLUE, RED, WHITE],
    "finland":            [WHITE, BLUE, WHITE],
    "france":             [BLUE, WHITE, RED],

    # --- G ----------------------------------------------------------------
    "gabon":              [GREEN, YELLOW, BLUE],
    "gambia":             [RED, BLUE, GREEN],
    "georgia":            [WHITE, RED, WHITE],
    "germany":            [BLACK, RED, GOLD],
    "ghana":              [RED, GOLD, GREEN],
    "greece":             [BLUE, WHITE, BLUE, WHITE, BLUE],
    "grenada":            [RED, YELLOW, GREEN],
    "guatemala":          [LIGHT_BLUE, WHITE, LIGHT_BLUE],
    "guinea":             [RED, YELLOW, GREEN],
    "guinea-bissau":      [RED, YELLOW, GREEN],
    "guyana":             [GREEN, WHITE, YELLOW, BLACK, RED],

    # --- H ----------------------------------------------------------------
    "haiti":              [BLUE, RED],
    "honduras":           [BLUE, WHITE, BLUE],
    "hungary":            [RED, WHITE, GREEN],

    # --- I ----------------------------------------------------------------
    "iceland":            [BLUE, WHITE, RED, WHITE, BLUE],
    "india":              [SAFFRON, WHITE, GREEN],
    "indonesia":          [RED, WHITE],
    "iran":               [GREEN, WHITE, RED],
    "iraq":               [RED, WHITE, BLACK],
    "ireland":            [GREEN, WHITE, ORANGE],
    "israel":             [WHITE, BLUE, WHITE],
    "italy":              [GREEN, WHITE, RED],
    "ivory-coast":        [ORANGE, WHITE, GREEN],

    # --- J ----------------------------------------------------------------
    "jamaica":            [GREEN, GOLD, BLACK],
    "japan":              [WHITE, RED, WHITE],
    "jordan":             [BLACK, WHITE, GREEN, RED],

    # --- K ----------------------------------------------------------------
    "kazakhstan":         [TEAL, YELLOW, TEAL],
    "kenya":              [BLACK, RED, GREEN],
    "kiribati":           [RED, WHITE, BLUE],
    "north-korea":        [BLUE, RED, WHITE, RED, BLUE],
    "south-korea":        [WHITE, RED, BLUE, WHITE],
    "kosovo":             [BLUE, YELLOW, BLUE],
    "kuwait":             [GREEN, WHITE, RED, BLACK],
    "kyrgyzstan":         [RED, YELLOW, RED],

    # --- L ----------------------------------------------------------------
    "laos":               [RED, BLUE, RED],
    "latvia":             [MAROON, WHITE, MAROON],
    "lebanon":            [RED, WHITE, GREEN, WHITE, RED],
    "lesotho":            [BLUE, WHITE, GREEN],
    "liberia":            [RED, WHITE, RED, WHITE, NAVY],
    "libya":              [RED, BLACK, GREEN],
    "liechtenstein":      [BLUE, RED],
    "lithuania":          [YELLOW, GREEN, RED],
    "luxembourg":         [RED, WHITE, LIGHT_BLUE],

    # --- M ----------------------------------------------------------------
    "madagascar":         [WHITE, RED, GREEN],
    "malawi":             [BLACK, RED, GREEN],
    "malaysia":           [RED, WHITE, NAVY],
    "maldives":           [RED, GREEN, RED],
    "mali":               [GREEN, GOLD, RED],
    "malta":              [WHITE, RED],
    "marshall-islands":   [BLUE, WHITE, ORANGE],
    "mauritania":         [GREEN, GOLD, GREEN],
    "mauritius":          [RED, BLUE, YELLOW, GREEN],
    "mexico":             [GREEN, WHITE, RED],
    "micronesia":         [LIGHT_BLUE, WHITE, LIGHT_BLUE],
    "moldova":            [BLUE, YELLOW, RED],
    "monaco":             [RED, WHITE],
    "mongolia":           [RED, BLUE, RED],
    "montenegro":         [RED, GOLD, RED],
    "morocco":            [RED, GREEN, RED],
    "mozambique":         [GREEN, BLACK, YELLOW, WHITE, RED],
    "myanmar":            [YELLOW, GREEN, RED],

    # --- N ----------------------------------------------------------------
    "namibia":            [BLUE, RED, GREEN],
    "nauru":              [BLUE, YELLOW, BLUE],
    "nepal":              [RED, BLUE],
    "netherlands":        [RED, WHITE, BLUE],
    "new-zealand":        [NAVY, RED, WHITE],
    "nicaragua":          [BLUE, WHITE, BLUE],
    "niger":              [ORANGE, WHITE, GREEN],
    "nigeria":            [GREEN, WHITE, GREEN],
    "north-macedonia":    [RED, YELLOW, RED],
    "norway":             [RED, WHITE, BLUE, WHITE, RED],

    # --- O ----------------------------------------------------------------
    "oman":               [WHITE, RED, GREEN],

    # --- P ----------------------------------------------------------------
    "pakistan":            [DARK_GREEN, WHITE],
    "palau":              [LIGHT_BLUE, YELLOW, LIGHT_BLUE],
    "palestine":          [BLACK, WHITE, GREEN, RED],
    "panama":             [WHITE, RED, WHITE, BLUE],
    "papua-new-guinea":   [RED, BLACK],
    "paraguay":           [RED, WHITE, BLUE],
    "peru":               [RED, WHITE, RED],
    "philippines":        [BLUE, RED, WHITE],
    "poland":             [WHITE, RED],
    "portugal":           [GREEN, RED],

    # --- Q ----------------------------------------------------------------
    "qatar":              [WHITE, MAROON],

    # --- R ----------------------------------------------------------------
    "romania":            [BLUE, YELLOW, RED],
    "russia":             [WHITE, BLUE, RED],
    "rwanda":             [BLUE, YELLOW, GREEN],

    # --- S ----------------------------------------------------------------
    "saint-lucia":        [LIGHT_BLUE, YELLOW, BLACK],
    "samoa":              [RED, BLUE],
    "san-marino":         [WHITE, LIGHT_BLUE],
    "saudi-arabia":       [DARK_GREEN, WHITE, DARK_GREEN],
    "senegal":            [GREEN, YELLOW, RED],
    "serbia":             [RED, BLUE, WHITE],
    "seychelles":         [BLUE, YELLOW, RED, WHITE, GREEN],
    "sierra-leone":       [GREEN, WHITE, BLUE],
    "singapore":          [RED, WHITE],
    "slovakia":           [WHITE, BLUE, RED],
    "slovenia":           [WHITE, BLUE, RED],
    "solomon-islands":    [BLUE, GREEN],
    "somalia":            [LIGHT_BLUE, WHITE, LIGHT_BLUE],
    "south-africa":       [GREEN, YELLOW, BLACK, WHITE, BLUE, RED],
    "south-sudan":        [BLACK, RED, GREEN, BLUE, YELLOW, WHITE],
    "spain":              [RED, YELLOW, RED],
    "sri-lanka":          [GREEN, ORANGE, MAROON],
    "sudan":              [RED, WHITE, BLACK, GREEN],
    "suriname":           [GREEN, WHITE, RED, WHITE, GREEN],
    "sweden":             [BLUE, YELLOW, BLUE],
    "switzerland":        [RED, WHITE, RED],
    "syria":              [RED, WHITE, BLACK],

    # --- T ----------------------------------------------------------------
    "taiwan":             [RED, BLUE, RED],
    "tajikistan":         [RED, WHITE, GREEN],
    "tanzania":           [GREEN, YELLOW, BLACK, YELLOW, BLUE],
    "thailand":           [RED, WHITE, BLUE, WHITE, RED],
    "togo":               [GREEN, YELLOW, GREEN, WHITE, RED],
    "tonga":              [RED, WHITE],
    "trinidad":           [RED, WHITE, BLACK],
    "tunisia":            [RED, WHITE, RED],
    "turkey":             [RED, WHITE, RED],
    "turkmenistan":       [GREEN, RED, GREEN],
    "tuvalu":             [LIGHT_BLUE, YELLOW, LIGHT_BLUE],

    # --- U ----------------------------------------------------------------
    "uganda":             [BLACK, YELLOW, RED, BLACK, YELLOW, RED],
    "ukraine":            [BLUE, YELLOW],
    "uae":                [GREEN, WHITE, BLACK, RED],
    "uk":                 [RED, WHITE, BLUE],
    "us":                 [RED, WHITE, BLUE],
    "uruguay":            [WHITE, BLUE, WHITE, BLUE, WHITE],
    "uzbekistan":         [BLUE, WHITE, GREEN],

    # --- V ----------------------------------------------------------------
    "vanuatu":            [RED, BLACK, GREEN, YELLOW],
    "vatican":            [YELLOW, WHITE],
    "venezuela":          [YELLOW, BLUE, RED],
    "vietnam":            [RED, YELLOW, RED],

    # --- Y ----------------------------------------------------------------
    "yemen":              [RED, WHITE, BLACK],

    # --- Z ----------------------------------------------------------------
    "zambia":             [GREEN, RED, BLACK, ORANGE],
    "zimbabwe":           [GREEN, YELLOW, RED, BLACK, RED, YELLOW, GREEN],

    # --- Special ----------------------------------------------------------
    "pride":              [RED, ORANGE, YELLOW, GREEN, BLUE, PURPLE],
    "trans":              [SKY_BLUE, PINK, WHITE, PINK, SKY_BLUE],
    "bi":                 [PINK, PURPLE, BLUE],
    "eu":                 [BLUE, GOLD, BLUE],
    "un":                 [LIGHT_BLUE, WHITE, LIGHT_BLUE],
}


# ---------------------------------------------------------------------------
# Aliases -- alternative names that map to a canonical key
# ---------------------------------------------------------------------------

_ALIASES: dict[str, str] = {
    "usa":              "us",
    "united-states":    "us",
    "america":          "us",
    "united-kingdom":   "uk",
    "great-britain":    "uk",
    "britain":          "uk",
    "gb":               "uk",
    "england":          "uk",
    "emirates":         "uae",
    "holland":          "netherlands",
    "cote-divoire":     "ivory-coast",
    "czech-republic":   "czechia",
    "burma":            "myanmar",
    "swaziland":        "eswatini",
    "drc":              "congo-dr",
    "korea":            "south-korea",
    "timor-leste":      "east-timor",
    "central-african-republic": "car",
    "european-union":   "eu",
    "united-nations":   "un",
    "rainbow":          "pride",
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_country_names() -> list[str]:
    """Return a sorted list of all supported country/flag names.

    Includes canonical names only, not aliases.

    Returns:
        Alphabetically sorted list of flag keys.
    """
    return sorted(FLAGS.keys())


def get_flag(country: str) -> list[StripeColor] | None:
    """Look up a country's flag stripe sequence.

    Accepts canonical names (e.g., ``"us"``) and aliases (e.g., ``"usa"``).
    Names are normalized to lowercase with spaces replaced by hyphens.

    Args:
        country: Country or flag name to look up.

    Returns:
        A list of stripe color tuples, or ``None`` if not found.
    """
    key: str = country.lower().strip().replace(" ", "-")
    if key in FLAGS:
        return FLAGS[key]
    canonical: str | None = _ALIASES.get(key)
    if canonical is not None:
        return FLAGS[canonical]
    return None
