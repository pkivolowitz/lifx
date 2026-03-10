"""Three independent Wolfram elementary cellular automata with perceptual colour blending.

Three CA simulations run simultaneously on the zone strip.  Each CA has its
own rule number, speed, and primary colour.  At every zone the active CAs
contribute their primary colour; the contributions are blended through
CIELAB space via :func:`~colorspace.lerp_color`, the same perceptually
uniform blending used by every other effect in this project.

Blend logic per zone
--------------------
* 0 CAs alive → background brightness (dead).
* 1 CA alive  → pure primary of that CA.
* 2 CAs alive → ``lerp_color(primary_x, primary_y, 0.5)``  (equal mix).
* 3 CAs alive → ``lerp_color(lerp_color(A, B, 0.5), C, 1/3)``
  (algebraically equal 1/3 weight to each primary).

Because each CA can run at a slightly different speed (the *drift* params)
the three patterns continuously slide relative to one another, producing a
slowly shifting macro-scale colour interference pattern on top of the
underlying cellular chaos.

Palette presets (--palette 1..50)
----------------------------------
All three primaries and the saturation are set from a coordinated scheme;
--hue-a/b/c and --sat are ignored when a palette is active.

Nature & elements
   0  custom          use --hue-a / --hue-b / --hue-c and --sat
   1  pastels         soft pink, lavender, mint
   2  earth           amber, terracotta, sage
   3  water           ocean blue, cyan, seafoam
   4  fire            red, orange, amber
   6  marble          cool/warm near-whites (the veins do the work)
   8  aurora          emerald, teal, deep purple
  10  forest          deep green, moss, bark brown
  11  deep sea        midnight blue, bioluminescent teal, violet
  39  tropical        hot teal, coral, sunny yellow
  40  coral reef      coral orange, teal, deep blue
  41  galaxy          deep violet, midnight blue, pale blue
  42  autumn          burnt orange, burgundy, golden yellow
  43  winter          ice blue, silver, pale lavender
  44  desert          sand, rust orange, warm brown
  45  arctic          pale blue, ice white, steel gray

Artists
   5  van gogh        cobalt blue, warm gold, ice blue
   7  sunset          warm orange, deep magenta, violet  (Turner)
  13  monet           soft lilac, water green, dusty rose
  14  klimt           deep gold, teal, burgundy
  15  rothko          deep crimson, burnt sienna, muted orange
  16  hokusai         deep navy, slate blue, pale blue-gray
  17  turner          golden amber, hazy orange, pale sky blue
  18  mondrian        red, cobalt blue, golden yellow
  19  warhol          hot pink, lime green, turquoise
  20  rembrandt       warm umber, antique gold, dark amber

Holidays
  21  christmas       red, deep green, gold
  22  halloween       orange, deep purple, yellow
  23  hanukkah        royal blue, sky blue, gold
  24  valentines      rose red, hot pink, blush
  25  easter          soft purple, pale yellow, light green
  26  independence    red, white-blue, blue
  27  st patricks     shamrock green, gold, light green
  28  thanksgiving    burnt orange, warm brown, deep gold
  29  new year        champagne gold, silver, midnight blue
  30  mardi gras      deep purple, gold, green
  31  diwali          deep gold, magenta, saffron

School colors
  32  michigan        maize, cobalt blue, sky blue
  33  alabama         crimson, silver, gold
  34  lsu             purple, gold, pale gold
  35  texas           burnt orange, warm brown, gold
  36  ohio state      scarlet, silver, gold
  37  notre dame      gold, navy, green
  38  ucla            blue, gold, sky blue

Moods & aesthetics
   9  neon            hot pink, electric cyan, acid green
  12  cherry blossom  pale pink, blush, soft lavender
  46  vaporwave       hot pink, purple, electric cyan
  47  cyberpunk       neon green, electric blue, magenta
  48  cottagecore     sage green, blush pink, warm cream
  49  gothic          deep burgundy, deep purple, dark rose
  50  lo-fi           warm amber, dusty rose, muted sage
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import random

from colorspace import lerp_color

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Neighbourhood size for Wolfram elementary CAs (left, center, right → 3 bits).
PATTERN_COUNT: int = 8

# Sentinel: CA rule table has not been built yet.
RULE_UNINITIALISED: int = -1

# Palette index meaning "use user-specified hues".
PALETTE_CUSTOM: int = 0

# Blend factor for equal two-colour mix.
BLEND_HALF: float = 0.5

# Blend factor that gives equal 1/3 weight when nested with BLEND_HALF.
# lerp(lerp(A, B, 0.5), C, 1/3) = 1/3 A + 1/3 B + 1/3 C.
BLEND_THIRD: float = 1.0 / 3.0

# Divisor added to the gap-fill search radius when fading a single-sided
# neighbour toward background — prevents reaching full background before
# the edge of the search window.
GAP_FADE_DIVISOR: int = 1

# ---------------------------------------------------------------------------
# Palette presets: (hue_a_deg, hue_b_deg, hue_c_deg, saturation_0_to_100)
# ---------------------------------------------------------------------------

PALETTES: dict[int, tuple[float, float, float, int]] = {
    # ---- Nature & elements ----                   hue_a   hue_b   hue_c  sat
     1: (340.0, 270.0, 150.0, 45),   # pastels:       pink, lavender, mint
     2: ( 35.0,  18.0, 130.0, 70),   # earth:         amber, terracotta, sage
     3: (220.0, 185.0, 165.0, 85),   # water:         ocean, cyan, seafoam
     4: (  0.0,  30.0,  55.0, 95),   # fire:          red, orange, amber
     6: (210.0,  42.0, 185.0, 12),   # marble:        near-whites; veins carry the sat
     8: (145.0, 178.0, 268.0, 85),   # aurora:        emerald, teal, deep purple
    10: (130.0,  95.0,  28.0, 72),   # forest:        deep green, moss, bark brown
    11: (232.0, 172.0, 262.0, 90),   # deep sea:      midnight blue, bioluminescent teal, violet
    39: (175.0,  15.0,  55.0, 90),   # tropical:      hot teal, coral, sunny yellow
    40: ( 18.0, 175.0, 230.0, 88),   # coral reef:    coral orange, teal, deep blue
    41: (268.0, 235.0, 215.0, 78),   # galaxy:        deep violet, midnight blue, pale blue
    42: ( 22.0, 355.0,  48.0, 85),   # autumn:        burnt orange, burgundy, golden yellow
    43: (205.0, 215.0, 270.0, 35),   # winter:        ice blue, silver, pale lavender
    44: ( 45.0,  18.0,  28.0, 68),   # desert:        sand, rust orange, warm brown
    45: (200.0, 210.0, 215.0, 20),   # arctic:        pale blue, ice white, steel gray
    # ---- Artists ----
     5: (225.0,  48.0, 195.0, 88),   # van gogh:      cobalt blue, warm gold, ice blue
     7: ( 20.0, 330.0, 275.0, 88),   # sunset/turner: warm orange, deep magenta, soft violet
    13: (280.0, 160.0, 340.0, 55),   # monet:         soft lilac, water green, dusty rose
    14: ( 45.0, 175.0, 350.0, 85),   # klimt:         deep gold, teal, burgundy
    15: (355.0,  22.0,  32.0, 82),   # rothko:        deep crimson, burnt sienna, muted orange
    16: (225.0, 210.0, 200.0, 80),   # hokusai:       deep navy, slate blue, pale blue-gray
    17: ( 42.0,  28.0, 200.0, 75),   # turner:        golden amber, hazy orange, pale sky blue
    18: (  5.0, 230.0,  52.0,100),   # mondrian:      red, cobalt blue, golden yellow
    19: (330.0,  88.0, 178.0,100),   # warhol:        hot pink, lime green, turquoise
    20: ( 28.0,  44.0,  35.0, 78),   # rembrandt:     warm umber, antique gold, dark amber
    # ---- Holidays ----
    21: (  5.0, 125.0,  48.0, 92),   # christmas:     red, deep green, gold
    22: ( 25.0, 270.0,  58.0, 95),   # halloween:     orange, deep purple, yellow
    23: (228.0, 205.0,  48.0, 80),   # hanukkah:      royal blue, sky blue, gold
    24: (355.0, 340.0,  15.0, 80),   # valentines:    rose red, hot pink, blush
    25: (280.0,  60.0, 140.0, 45),   # easter:        soft purple, pale yellow, light green
    26: (  5.0, 218.0, 238.0, 90),   # independence:  red, white-blue, blue
    27: (130.0,  50.0, 145.0, 85),   # st patricks:   shamrock green, gold, light green
    28: ( 22.0,  30.0,  45.0, 80),   # thanksgiving:  burnt orange, warm brown, deep gold
    29: ( 48.0, 218.0, 240.0, 65),   # new year:      champagne gold, silver, midnight blue
    30: (270.0,  50.0, 130.0, 90),   # mardi gras:    deep purple, gold, green
    31: ( 45.0, 310.0,  30.0, 90),   # diwali:        deep gold, magenta, saffron
    # ---- School colors ----
    32: ( 50.0, 230.0, 210.0, 88),   # michigan:      maize, cobalt blue, sky blue
    33: (350.0, 215.0,  48.0, 82),   # alabama:       crimson, silver, gold
    34: (270.0,  48.0,  52.0, 90),   # lsu:           purple, gold, pale gold
    35: ( 22.0,  28.0,  45.0, 80),   # texas:         burnt orange, warm brown, gold
    36: (  5.0, 215.0,  48.0, 82),   # ohio state:    scarlet, silver, gold
    37: ( 48.0, 225.0, 130.0, 88),   # notre dame:    gold, navy, green
    38: (228.0,  50.0, 210.0, 85),   # ucla:          blue, gold, sky blue
    # ---- Moods & aesthetics ----
     9: (310.0, 183.0,  90.0,100),   # neon:          hot pink, electric cyan, acid green
    12: (348.0,  15.0, 290.0, 38),   # cherry blossom: pale pink, blush, soft lavender
    46: (310.0, 270.0, 185.0, 95),   # vaporwave:     hot pink, purple, electric cyan
    47: (130.0, 225.0, 300.0,100),   # cyberpunk:     neon green, electric blue, magenta
    48: (130.0, 350.0,  45.0, 48),   # cottagecore:   sage green, blush pink, warm cream
    49: (350.0, 270.0, 340.0, 78),   # gothic:        deep burgundy, deep purple, dark rose
    50: ( 35.0, 348.0, 130.0, 52),   # lo-fi:         warm amber, dusty rose, muted sage
}


# ---------------------------------------------------------------------------
# Internal single-CA state machine
# ---------------------------------------------------------------------------

class _CA:
    """Lightweight single-track Wolfram elementary cellular automaton.

    Not an Effect subclass — used solely as a helper inside
    :class:`RuleTrio` to avoid repeating CA machinery three times.
    """

    def __init__(self) -> None:
        self.state:       list[int] = []
        self.generation:  int       = 0
        self._rule_table: list[int] = []
        self._built_rule: int       = RULE_UNINITIALISED

    # ------------------------------------------------------------------

    def seed(self, zone_count: int, rule: int) -> None:
        """Seed with a random initial state and build the rule table.

        A random seed fills the strip immediately with complex state
        rather than requiring dozens of generations to propagate from a
        single centre cell.

        Args:
            zone_count: Number of cells (zones).
            rule:       Wolfram elementary rule number 0-255.
        """
        self.generation = 0
        self.state = [random.randint(0, 1) for _ in range(zone_count)]
        self._ensure_table(rule)

    # ------------------------------------------------------------------

    def advance_to(self, target_gen: int, rule: int) -> None:
        """Step the CA forward until generation equals *target_gen*.

        Args:
            target_gen: Target generation index.
            rule:       Current rule (table rebuilt on change).
        """
        self._ensure_table(rule)
        while self.generation < target_gen:
            self._step()

    # ------------------------------------------------------------------

    def _ensure_table(self, rule: int) -> None:
        """Rebuild the lookup table only if *rule* has changed.

        Args:
            rule: Wolfram elementary rule number 0-255.
        """
        if rule == self._built_rule:
            return
        self._rule_table = [(rule >> i) & 1 for i in range(PATTERN_COUNT)]
        self._built_rule = rule

    # ------------------------------------------------------------------

    def _step(self) -> None:
        """Advance one generation with periodic (wrap-around) boundary conditions.

        The strip is treated as a ring: leftmost cell's left neighbour is
        the rightmost cell and vice versa, so no cell is ever stranded at
        a dead edge.
        """
        n: int = len(self.state)
        new: list[int] = [0] * n
        for i in range(n):
            left:   int = self.state[(i - 1) % n]
            center: int = self.state[i]
            right:  int = self.state[(i + 1) % n]
            # Standard Wolfram neighbourhood encoding: left is MSB.
            new[i] = self._rule_table[(left << 2) | (center << 1) | right]
        self.state = new
        self.generation += 1


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------

class RuleTrio(Effect):
    """Three independent cellular automata with perceptual CIELAB colour blending.

    Each CA runs its own Wolfram rule at its own speed.  At each zone the
    outputs of the three automata are combined using :func:`lerp_color` —
    the same CIELAB-backed blending used by every other effect — so colour
    mixes are always perceptually uniform, with no muddy intermediates or
    brightness dips.

    The slight speed differences (controlled by *drift_b* and *drift_c*)
    cause the three patterns to slide relative to one another, producing
    slowly evolving macro-scale colour structures.

    Quick-start examples
    --------------------
    ``--palette 1``  Pastel pink, lavender and mint.
    ``--palette 2``  Amber, terracotta, sage — warm earth tones.
    ``--palette 3``  Ocean, cyan, seafoam — cool water tones.
    ``--palette 4``  Red, orange, amber — fire.
    Custom: ``--hue-a 30 --hue-b 180 --hue-c 300 --sat 70``
    """

    name: str = "rule_trio"
    description: str = (
        "Three independent 1-D cellular automata blended through CIELAB colour space"
    )

    # ------------------------------------------------------------------
    # Tunable parameters
    # ------------------------------------------------------------------

    rule_a = Param(
        30, min=0, max=255,
        description="Wolfram rule for CA A (30=chaotic, 90=fractal, 110=complex)",
    )
    rule_b = Param(
        30, min=0, max=255,
        description="Wolfram rule for CA B",
    )
    rule_c = Param(
        30, min=0, max=255,
        description="Wolfram rule for CA C",
    )

    speed = Param(
        8.0, min=0.5, max=120.0,
        description="Base generations per second for CA A",
    )
    drift_b = Param(
        1.31, min=0.1, max=8.0,
        description=(
            "Speed multiplier for CA B relative to CA A; "
            "irrational default avoids phase lock-in"
        ),
    )
    drift_c = Param(
        1.73, min=0.1, max=8.0,
        description=(
            "Speed multiplier for CA C relative to CA A; "
            "irrational default avoids phase lock-in"
        ),
    )

    palette = Param(
        0, min=0, max=50,
        description=(
            "Colour preset 0-50 (0=custom; non-zero overrides --hue-a/b/c and --sat). "
            "Nature: 1=pastels 2=earth 3=water 4=fire 6=marble 8=aurora 10=forest "
            "11=deep sea 39=tropical 40=coral reef 41=galaxy 42=autumn 43=winter "
            "44=desert 45=arctic. "
            "Artists: 5=van gogh 7=sunset 13=monet 14=klimt 15=rothko 16=hokusai "
            "17=turner 18=mondrian 19=warhol 20=rembrandt. "
            "Holidays: 21=christmas 22=halloween 23=hanukkah 24=valentines 25=easter "
            "26=independence 27=st patricks 28=thanksgiving 29=new year 30=mardi gras "
            "31=diwali. "
            "Schools: 32=michigan 33=alabama 34=lsu 35=texas 36=ohio state "
            "37=notre dame 38=ucla. "
            "Aesthetics: 9=neon 12=cherry blossom 46=vaporwave 47=cyberpunk "
            "48=cottagecore 49=gothic 50=lo-fi."
        ),
    )
    hue_a = Param(
        0.0, min=0.0, max=360.0,
        description="CA A primary hue in degrees (custom palette only)",
    )
    hue_b = Param(
        120.0, min=0.0, max=360.0,
        description="CA B primary hue in degrees (custom palette only)",
    )
    hue_c = Param(
        240.0, min=0.0, max=360.0,
        description="CA C primary hue in degrees (custom palette only)",
    )
    sat = Param(
        80, min=0, max=100,
        description="Primary saturation as percent (custom palette only)",
    )
    brightness = Param(
        90, min=1, max=100,
        description="Primary brightness as percent",
    )
    bg = Param(
        0, min=0, max=20,
        description="Dead-cell background brightness as percent (0 = fully off)",
    )
    gap_fill = Param(
        3, min=0, max=12,
        description=(
            "Gap-fill search radius in zones: dead zones look this far in each "
            "direction for alive neighbours and blend between them through CIELAB. "
            "0 = disabled (hard binary on/off). "
            "On LIFX string lights (3 zones per bulb) multiples of 3 align to "
            "bulb boundaries: 3=1 bulb, 6=2 bulbs, 9=3 bulbs. "
            "Note the hardware already optically mixes the 3 zones inside each "
            "bulb, so aggressive software blurring is usually unnecessary."
        ),
    )
    kelvin = Param(
        KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
        description="Color temperature in Kelvin",
    )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, **overrides: dict) -> None:
        """Initialise effect state.

        Args:
            **overrides: Parameter names mapped to override values.
        """
        super().__init__(**overrides)
        self._ca_a: _CA = _CA()
        self._ca_b: _CA = _CA()
        self._ca_c: _CA = _CA()

    def on_start(self, zone_count: int) -> None:
        """Seed all three automata with independent random initial states.

        Args:
            zone_count: Number of zones on the target device.
        """
        self._ca_a.seed(zone_count, int(self.rule_a))
        self._ca_b.seed(zone_count, int(self.rule_b))
        self._ca_c.seed(zone_count, int(self.rule_c))

    def on_stop(self) -> None:
        """Reset all CA states so the next start is always fresh."""
        for ca in (self._ca_a, self._ca_b, self._ca_c):
            ca.state = []
            ca.generation = 0

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one animation frame by blending three CA outputs.

        Each zone's colour is determined by which CAs have a live cell
        there: 0 alive → background; 1 alive → that primary; 2 alive →
        CIELAB midpoint; 3 alive → CIELAB centroid of all three.

        If *gap_fill* > 0, a second pass replaces dead zones with a
        CIELAB blend of their nearest alive neighbours so that black
        gaps between coloured regions are eliminated.

        Args:
            t:          Seconds elapsed since the effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        # Guard: initialise if on_start was not called.
        if len(self._ca_a.state) != zone_count:
            self.on_start(zone_count)

        # Advance each CA to its target generation.
        base: float = self.speed
        self._ca_a.advance_to(int(t * base),                int(self.rule_a))
        self._ca_b.advance_to(int(t * base * self.drift_b), int(self.rule_b))
        self._ca_c.advance_to(int(t * base * self.drift_c), int(self.rule_c))

        # Resolve the three primaries (accounts for palette override).
        color_a, color_b, color_c = self._resolve_primaries()

        # Background: achromatic at bg brightness, same kelvin.
        bg_bri: int  = pct_to_u16(self.bg)
        dead:   HSBK = (0, 0, bg_bri, self.kelvin)

        colors:   list[HSBK] = []
        is_alive: list[bool] = []

        for i in range(zone_count):
            alive_a: bool = bool(self._ca_a.state[i])
            alive_b: bool = bool(self._ca_b.state[i])
            alive_c: bool = bool(self._ca_c.state[i])

            alive_count: int = alive_a + alive_b + alive_c

            if alive_count == 0:
                colors.append(dead)
                is_alive.append(False)

            elif alive_count == 1:
                # Only one primary — use it directly, no blending needed.
                if alive_a:
                    colors.append(color_a)
                elif alive_b:
                    colors.append(color_b)
                else:
                    colors.append(color_c)
                is_alive.append(True)

            elif alive_count == 2:
                # Two-colour CIELAB midpoint.
                if alive_a and alive_b:
                    colors.append(lerp_color(color_a, color_b, BLEND_HALF))
                elif alive_a and alive_c:
                    colors.append(lerp_color(color_a, color_c, BLEND_HALF))
                else:
                    colors.append(lerp_color(color_b, color_c, BLEND_HALF))
                is_alive.append(True)

            else:
                # All three alive: lerp(lerp(A, B, 0.5), C, 1/3) = equal thirds.
                mid: HSBK = lerp_color(color_a, color_b, BLEND_HALF)
                colors.append(lerp_color(mid, color_c, BLEND_THIRD))
                is_alive.append(True)

        # Optional second pass: fill dead zones with a blend of their nearest
        # alive neighbours so coloured regions flow into one another seamlessly.
        radius: int = int(self.gap_fill)
        if radius > 0:
            colors = self._fill_gaps(colors, is_alive, dead, radius)

        return colors

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fill_gaps(
        self,
        colors:   list[HSBK],
        is_alive: list[bool],
        dead:     HSBK,
        radius:   int,
    ) -> list[HSBK]:
        """Fill dead zones with a perceptual blend of their nearest alive neighbours.

        For each dead zone the method searches up to *radius* zones to the
        left and right for the nearest alive zone in each direction.

        * **Both sides found** — blend between the two neighbour colours,
          weighted so the closer one has more influence.  No additional
          brightness reduction: the gap colour looks like a natural bridge.
        * **One side only** — fade from the neighbour colour toward *dead*
          in proportion to distance, so colour tapers smoothly to background
          at the outer edge of the search window.
        * **Neither side** — leave *dead* unchanged.

        The strip is treated as linear (non-wrapping) for this pass so that
        the colour at each end of the physical strip is not polluted by the
        colour at the opposite end.

        Args:
            colors:   Per-zone HSBK list from the primary render pass.
            is_alive: Per-zone alive flag (True if any CA was live there).
            dead:     Background HSBK used when no neighbour is reachable.
            radius:   Maximum search distance in zones (not bulbs; on LIFX
                      string lights 3 zones = 1 physical bulb).

        Returns:
            A new per-zone HSBK list with dead zones replaced.
        """
        n: int = len(colors)
        result: list[HSBK] = list(colors)  # shallow copy; alive zones pass through

        for i in range(n):
            if is_alive[i]:
                continue  # already coloured — nothing to do

            # --- Search left ---
            left_color: HSBK | None = None
            left_dist:  int         = 0
            for d in range(1, radius + 1):
                j: int = i - d
                if j < 0:
                    break
                if is_alive[j]:
                    left_color = colors[j]
                    left_dist  = d
                    break

            # --- Search right ---
            right_color: HSBK | None = None
            right_dist:  int         = 0
            for d in range(1, radius + 1):
                j = i + d
                if j >= n:
                    break
                if is_alive[j]:
                    right_color = colors[j]
                    right_dist  = d
                    break

            # --- Blend ---
            if left_color is not None and right_color is not None:
                # Weight by distance: closer neighbour dominates.
                # blend=0 → all left, blend=1 → all right.
                total: int = left_dist + right_dist
                blend: float = left_dist / total
                result[i] = lerp_color(left_color, right_color, blend)

            elif left_color is not None:
                # Fade toward background over the search window.
                fade: float = left_dist / (radius + GAP_FADE_DIVISOR)
                result[i] = lerp_color(left_color, dead, fade)

            elif right_color is not None:
                fade = right_dist / (radius + GAP_FADE_DIVISOR)
                result[i] = lerp_color(right_color, dead, fade)

            # else: no alive neighbour within radius — keep dead unchanged.

        return result

    def _resolve_primaries(self) -> tuple[HSBK, HSBK, HSBK]:
        """Return the three primary HSBK values for the current parameter state.

        When a palette preset is active its hues and saturation override
        the user-visible params.  Brightness is always taken from
        :attr:`brightness`.

        Returns:
            Three HSBK tuples — one per CA primary.
        """
        bri_u16:    int   = pct_to_u16(self.brightness)
        palette_idx: int  = int(self.palette)

        if palette_idx in PALETTES:
            ha, hb, hc, sat_pct = PALETTES[palette_idx]
            sat_u16: int = pct_to_u16(sat_pct)
        else:
            ha      = float(self.hue_a)
            hb      = float(self.hue_b)
            hc      = float(self.hue_c)
            sat_u16 = pct_to_u16(self.sat)

        color_a: HSBK = (hue_to_u16(ha), sat_u16, bri_u16, self.kelvin)
        color_b: HSBK = (hue_to_u16(hb), sat_u16, bri_u16, self.kelvin)
        color_c: HSBK = (hue_to_u16(hc), sat_u16, bri_u16, self.kelvin)

        return color_a, color_b, color_c
