# Built-in Effects

### cylon

**Larson scanner** — a bright eye sweeps back and forth with a smooth
cosine falloff trail. Classic Battlestar Galactica / Knight Rider look.
The eye follows sinusoidal easing so direction reversals look natural.

| Parameter    | Default | Range       | Description                                    |
|--------------|---------|-------------|------------------------------------------------|
| `speed`      | 2.0     | 0.2–30.0   | Seconds per full sweep (there and back)        |
| `width`      | 5       | 1–50       | Width of the eye in bulbs                      |
| `hue`        | 0.0     | 0.0–360.0  | Eye color hue in degrees (0=red)               |
| `brightness` | 100     | 0–100      | Eye brightness as percent                      |
| `bg`         | 0       | 0–100      | Background brightness as percent               |
| `trail`      | 0.4     | 0.0–1.0    | Trail decay factor (0=no trail, 1=max)         |
| `kelvin`     | 3500    | 1500–9000  | Color temperature in Kelvin                    |

### breathe

**Color oscillator** — all bulbs oscillate between two colors via sine
wave. Color 1 shows at the trough, color 2 at the peak, with smooth
blending through the full cycle. Hue interpolation takes the shortest
path around the color wheel.

| Parameter    | Default | Range       | Description                                    |
|--------------|---------|-------------|------------------------------------------------|
| `speed`      | 4.0     | 0.5–30.0   | Seconds per full cycle                         |
| `hue1`       | 240.0   | 0.0–360.0  | Color 1 hue in degrees (shown at sin < 0)      |
| `hue2`       | 0.0     | 0.0–360.0  | Color 2 hue in degrees (shown at sin > 0)      |
| `sat1`       | 100     | 0–100      | Color 1 saturation percent                     |
| `sat2`       | 100     | 0–100      | Color 2 saturation percent                     |
| `brightness` | 100     | 0–100      | Overall brightness percent                     |
| `kelvin`     | 3500    | 1500–9000  | Color temperature in Kelvin                    |

### wave

**Standing wave** — simulates a vibrating string. Bulbs oscillate between
two colors with fixed nodes (stationary points), just like a real vibrating
string. Adjacent segments swing in opposite directions.

| Parameter    | Default | Range       | Description                                    |
|--------------|---------|-------------|------------------------------------------------|
| `speed`      | 3.0     | 0.3–30.0   | Seconds per oscillation cycle                  |
| `nodes`      | 6       | 1–20       | Number of stationary nodes along the string    |
| `hue1`       | 240.0   | 0.0–360.0  | Color 1 hue (negative displacement)            |
| `hue2`       | 0.0     | 0.0–360.0  | Color 2 hue (positive displacement)            |
| `sat1`       | 100     | 0–100      | Color 1 saturation percent                     |
| `sat2`       | 100     | 0–100      | Color 2 saturation percent                     |
| `brightness` | 100     | 0–100      | Overall brightness percent                     |
| `kelvin`     | 3500    | 1500–9000  | Color temperature in Kelvin                    |

### twinkle

**Christmas lights** — random zones sparkle and fade independently.
Each zone triggers sparkles at random intervals, flashing bright then
decaying with a quadratic falloff (fast flash, slow tail) back to the
background color.

| Parameter    | Default | Range       | Description                                    |
|--------------|---------|-------------|------------------------------------------------|
| `speed`      | 0.5     | 0.1–5.0    | Sparkle fade duration in seconds               |
| `density`    | 0.15    | 0.01–1.0   | Probability a zone sparks per frame            |
| `hue`        | 0.0     | 0.0–360.0  | Sparkle hue in degrees                         |
| `saturation` | 0       | 0–100      | Sparkle saturation (0=white sparkle)           |
| `brightness` | 100     | 0–100      | Peak sparkle brightness percent                |
| `bg_hue`     | 240.0   | 0.0–360.0  | Background hue in degrees                      |
| `bg_sat`     | 80      | 0–100      | Background saturation percent                  |
| `bg_bri`     | 10      | 0–100      | Background brightness percent                  |
| `kelvin`     | 3500    | 1500–9000  | Color temperature in Kelvin                    |

### morse

**Morse code transmitter** — the entire string flashes in unison,
encoding a message in International Morse Code. Standard timing:
dot = 1 unit, dash = 3 units, intra-char gap = 1 unit, inter-char
gap = 3 units, word gap = 7 units. Loops with a configurable pause.

| Parameter    | Default       | Range       | Description                              |
|--------------|---------------|-------------|------------------------------------------|
| `message`    | "HELLO WORLD" | *(any text)* | Message to transmit                     |
| `unit`       | 0.15          | 0.05–2.0   | Duration of one dot in seconds           |
| `hue`        | 0.0           | 0.0–360.0  | Flash hue in degrees                     |
| `saturation` | 0             | 0–100      | Flash saturation (0=white)               |
| `brightness` | 100           | 0–100      | Flash brightness percent                 |
| `bg_bri`     | 0             | 0–100      | Background brightness (off between flashes) |
| `pause`      | 5.0           | 0.0–30.0   | Pause in seconds before repeating        |
| `kelvin`     | 3500          | 1500–9000  | Color temperature in Kelvin              |

### aurora

**Northern lights** — slow-moving curtains of color drift across the
string. Four overlapping sine wave layers at different frequencies create
organic brightness variation, while two independent hue waves sweep a
green-blue-purple palette across the zones.

| Parameter    | Default | Range       | Description                                    |
|--------------|---------|-------------|------------------------------------------------|
| `speed`      | 8.0     | 1.0–60.0   | Seconds per full drift cycle                   |
| `brightness` | 80      | 0–100      | Peak brightness percent                        |
| `bg_bri`     | 5       | 0–100      | Background brightness percent                  |
| `kelvin`     | 3500    | 1500–9000  | Color temperature in Kelvin                    |

### binclock

**Binary clock** — displays the current wall-clock time in binary.
Each strand of 36 bulbs encodes hours (4 bits), minutes (6 bits), and
seconds (6 bits). Each bit is shown on 2 adjacent bulbs for visibility,
with 2-bulb gaps between groups. Hours, minutes, and seconds each use
a distinct color (default: red, green, blue) for easy visual identification.

Layout per strand (36 bulbs):
```
[HHHH HHHH] [gap] [MMMMMM MMMMMM MMMMMM] [gap] [SSSSSS SSSSSS SSSSSS]
  4 bits×2    ×2      6 bits × 2 bulbs      ×2      6 bits × 2 bulbs
```

| Parameter    | Default | Range       | Description                                    |
|--------------|---------|-------------|------------------------------------------------|
| `hour_hue`   | 0.0     | 0.0–360.0  | Hour bit hue in degrees (0=red)                |
| `min_hue`    | 120.0   | 0.0–360.0  | Minute bit hue in degrees (120=green)          |
| `sec_hue`    | 240.0   | 0.0–360.0  | Second bit hue in degrees (240=blue)           |
| `brightness` | 80      | 0–100      | "On" bit brightness percent                    |
| `off_bri`    | 0       | 0–100      | "Off" bit brightness percent                   |
| `gap_bri`    | 0       | 0–100      | Gap brightness percent (0=dark)                |
| `kelvin`     | 3500    | 1500–9000  | Color temperature in Kelvin                    |

### flag

**Waving national flag** — displays a country's flag as colored stripes
across the string, with a physically-motivated ripple animation. The flag's
color stripes are laid along a virtual 1D surface. Five-octave fractal
Brownian motion (Perlin noise) displaces each point in depth, and a
perspective projection maps the result onto zones. Stripes closer to the
viewer expand and may occlude stripes farther away. Surface slope modulates
brightness to simulate fold shading. Per-zone temporal smoothing (EMA)
prevents single-bulb flicker at stripe boundaries.

For single-color flags the effect displays a static solid color.

Supports 199 countries plus special flags (pride, trans, bi, eu, un).
Common aliases work: "usa" → "us", "holland" → "netherlands", etc.

| Parameter    | Default | Range       | Description                                    |
|--------------|---------|-------------|------------------------------------------------|
| `country`    | "us"    | *(name)*   | Country name (e.g., us, france, japan, germany) |
| `speed`      | 1.5     | 0.1–20.0   | Wave propagation speed                         |
| `brightness` | 80      | 0–100      | Overall brightness percent                     |
| `direction`  | "left"  | left/right | Stripe read direction (match your layout)      |
| `kelvin`     | 3500    | 1500–9000  | Color temperature in Kelvin                    |

**Examples:**

```bash
# French flag
python3 glowup.py play flag --ip <device-ip> --country france

# Slow Japanese flag
python3 glowup.py play flag --ip <device-ip> --country japan --speed 3

# US flag, reading right to left
python3 glowup.py play flag --ip <device-ip> --country us --direction right
```

### fireworks

**Fireworks display** — rockets launch from random ends of the string,
trailing white-hot exhaust as they decelerate toward a random zenith.
At the peak each rocket detonates: a gaussian bloom of saturated color
expands outward in both directions and fades quadratically to black.

Multiple rockets fly simultaneously. Where they share zones their
brightness contributions are **additive** — overlapping bursts look
brighter, never fighting for a single color slot. Hue is taken from
whichever rocket contributes the most brightness to a given zone, so
layers of color stack naturally.

Designed for multizone string-light devices. On single-zone bulbs the
effect degenerates to simple on/off flashes.

| Parameter         | Default | Range       | Description                                          |
|-------------------|---------|-------------|------------------------------------------------------|
| `--max-rockets`   | 3       | 1–20        | Maximum simultaneous rockets in flight               |
| `--launch-rate`   | 0.5     | 0.05–5.0   | Average new rockets launched per second              |
| `--ascent-speed`  | 10.0    | 1.0–60.0   | Rocket travel speed in zones per second              |
| `--trail-length`  | 8       | 1–30        | Exhaust trail length in zones                        |
| `--burst-spread`  | 20      | 2–60        | Maximum burst radius in zones from zenith            |
| `--burst-duration`| 1.8     | 0.2–8.0    | Seconds for the burst to fade completely to black    |
| `--kelvin`        | 3500    | 1500–9000  | Color temperature in Kelvin                          |

**Examples:**

```bash
# Default fireworks display
python3 glowup.py play fireworks --ip <device-ip>

# Rapid-fire with long trails and big bursts
python3 glowup.py play fireworks --ip <device-ip> --launch-rate 2.0 --trail-length 12 --burst-spread 20

# Slow, dramatic rockets with long fades
python3 glowup.py play fireworks --ip <device-ip> --ascent-speed 4.0 --burst-duration 4.0 --max-rockets 5
```

---

### rule30

**Wolfram elementary 1-D cellular automaton** — each zone on the strip is one
cell.  Every frame the simulation advances by one or more generations using the
selected Wolfram rule, mapping live cells to a coloured zone and dead cells to
a dim background.

The strip is treated as a **periodic ring**: the leftmost cell's left neighbour
is the rightmost cell and vice versa, so no edge starvation occurs.

#### How it works

An elementary CA is defined by its neighbourhood: the state of a cell in the
next generation depends solely on its current state and the states of its
immediate left and right neighbours.  Those three binary values form an 8-bit
index into a lookup table.  The lookup table for rule *N* is simply the
binary representation of *N*.

**Rule 30** produces visually chaotic, pseudo-random output from a single live
seed cell — it is so unpredictable that Wolfram used it as the random-number
generator in Mathematica for many years.

**Notable rules**

| Rule | Character                                              |
|------|--------------------------------------------------------|
| 30   | Chaotic / pseudo-random.  Default.                    |
| 90   | Sierpiński triangle — self-similar fractal.            |
| 110  | Turing-complete.  Complex glider-like structures.      |
| 184  | Traffic-flow model.  Waves propagate rightward.        |

| Parameter     | Default | Range      | Description                                                        |
|---------------|---------|------------|--------------------------------------------------------------------|
| `--rule`      | 30      | 0–255      | Wolfram elementary CA rule number                                  |
| `--speed`     | 8.0     | 0.5–120    | Generations per second                                             |
| `--hue`       | 200.0   | 0–360      | Live-cell hue in degrees (200 = teal)                              |
| `--brightness`| 100     | 1–100      | Live-cell brightness as percent                                    |
| `--bg`        | 0       | 0–30       | Dead-cell background brightness as percent                         |
| `--seed`      | 0       | 0–2        | Initial seed: 0 = single centre cell, 1 = random, 2 = all alive   |
| `--kelvin`    | 3500    | 1500–9000  | Color temperature in Kelvin                                        |

**Examples:**

```bash
# Default: Rule 30, teal, from a single centre cell
python3 glowup.py play rule30 --ip <device-ip>

# Sierpiński fractal in green
python3 glowup.py play rule30 --ip <device-ip> --rule 90 --hue 120

# Fast chaotic shimmer with dim background glow
python3 glowup.py play rule30 --ip <device-ip> --speed 30 --bg 4

# Rule 110 with random seed
python3 glowup.py play rule30 --ip <device-ip> --rule 110 --seed 1
```

---

### rule_trio

**Three independent Wolfram cellular automata with perceptual colour blending.**
Three separate CA simulations run simultaneously on the same zone strip.  Each
CA has its own rule number, speed, and primary colour.  At every zone the
outputs of all three are combined:

| Active CAs at zone | Result                                              |
|--------------------|-----------------------------------------------------|
| 0 (none)           | Background brightness (`--bg`)                      |
| 1                  | Pure primary of that CA                             |
| 2                  | CIELAB midpoint of the two active primaries         |
| 3 (all)            | CIELAB centroid — equal ⅓ weight to each primary    |

Colour blending is performed through **CIELAB space** (via `lerp_color`) so
transitions are perceptually uniform with no brightness dips or muddy
intermediates.

Because each CA can run at a slightly different speed (controlled by `--drift-b`
and `--drift-c`) the three patterns continuously slide relative to one another.
The interference creates a slowly shifting, never-repeating macro-scale colour
structure riding on top of the underlying cellular chaos.

On **LIFX string lights** (3 zones per physical bulb) two blending layers
operate simultaneously: software blending from this effect, and optical blending
inside each glass bulb.  This dual-layer smoothing makes aggressive gap-filling
unnecessary; the default `--gap-fill 3` (one bulb radius) is usually ideal.

#### Parameters

| Parameter      | Default | Range      | Description                                                                  |
|----------------|---------|------------|------------------------------------------------------------------------------|
| `--rule-a`     | 30      | 0–255      | Wolfram rule for CA A                                                        |
| `--rule-b`     | 30      | 0–255      | Wolfram rule for CA B                                                        |
| `--rule-c`     | 30      | 0–255      | Wolfram rule for CA C                                                        |
| `--speed`      | 1.5     | 0.5–120    | Base generations per second for CA A                                         |
| `--drift-b`    | 1.31    | 0.1–8.0    | Speed multiplier for CA B (irrational default avoids phase lock-in)          |
| `--drift-c`    | 1.73    | 0.1–8.0    | Speed multiplier for CA C (irrational default avoids phase lock-in)          |
| `--palette`    | custom  | 51 presets | Colour preset (see table below); "custom" = use `--hue-a/b/c` and `--sat`   |
| `--hue-a`      | 0.0     | 0–360      | CA A primary hue in degrees (custom palette only)                            |
| `--hue-b`      | 120.0   | 0–360      | CA B primary hue in degrees (custom palette only)                            |
| `--hue-c`      | 240.0   | 0–360      | CA C primary hue in degrees (custom palette only)                            |
| `--sat`        | 80      | 0–100      | Primary saturation as percent (custom palette only)                          |
| `--brightness` | 90      | 1–100      | Primary brightness as percent                                                |
| `--bg`         | 0       | 0–20       | Dead-cell background brightness as percent                                   |
| `--gap-fill`   | 3       | 0–12       | Gap-fill radius in zones; 0 disables; multiples of 3 align to bulb edges    |
| `--kelvin`     | 3500    | 1500–9000  | Color temperature in Kelvin                                                  |

#### Gap fill

When `--gap-fill N` is non-zero, a post-processing pass fills dead zones by
searching up to *N* zones left and right for the nearest live neighbours and
blending between them through CIELAB:

- **Both sides found** — CIELAB blend weighted by distance (closer wins).
- **One side only** — fades from the neighbour colour toward background with
  distance.
- **Neither side** — zone stays at background brightness.

On LIFX string lights, use multiples of 3 to respect bulb boundaries:
`--gap-fill 3` fills up to one bulb, `--gap-fill 6` two bulbs, etc.

#### Palettes

Palettes are coordinated sets of three primary hues plus a saturation value.
When `--palette NAME` is anything other than "custom" it overrides `--hue-a`,
`--hue-b`, `--hue-c`, and `--sat`.

**Nature & elements**

| Name          | Primaries                                    | Sat |
|---------------|----------------------------------------------|-----|
| pastels       | Soft pink (340°), lavender (270°), mint (150°) | 45% |
| earth         | Amber (35°), terracotta (18°), sage (130°)   | 70% |
| water         | Ocean blue (220°), cyan (185°), seafoam (165°) | 85% |
| fire          | Red (0°), orange (30°), amber (55°)          | 95% |
| marble        | Blue-gray (210°), warm-white (42°), cool-white (185°) | 12% |
| aurora        | Emerald (145°), teal (178°), deep purple (268°) | 85% |
| forest        | Deep green (130°), moss (95°), bark brown (28°) | 72% |
| deep sea      | Midnight blue (232°), bioluminescent teal (172°), violet (262°) | 90% |
| tropical      | Hot teal (175°), coral (15°), sunny yellow (55°) | 90% |
| coral reef    | Coral orange (18°), teal (175°), deep blue (230°) | 88% |
| galaxy        | Deep violet (268°), midnight blue (235°), pale blue (215°) | 78% |
| autumn        | Burnt orange (22°), burgundy (355°), golden yellow (48°) | 85% |
| winter        | Ice blue (205°), silver (215°), pale lavender (270°) | 35% |
| desert        | Sand (45°), rust orange (18°), warm brown (28°) | 68% |
| arctic        | Pale blue (200°), ice white (210°), steel gray (215°) | 20% |

*Marble at 12% saturation looks nearly white; the CA's alive/dead spatial
structure creates the veining.  Arctic at 20% reads as frost breath patterns.*

**Artists**

| Name          | Primaries                                    | Sat | Inspiration                              |
|---------------|----------------------------------------------|-----|------------------------------------------|
| van gogh      | Cobalt blue (225°), warm gold (48°), ice blue (195°) | 88% | *Starry Night* — swirling contrast       |
| sunset        | Warm orange (20°), deep magenta (330°), soft violet (275°) | 88% | Turner atmospheric skies |
| monet         | Soft lilac (280°), water green (160°), dusty rose (340°) | 55% | *Water Lilies* — hazy impressionism      |
| klimt         | Deep gold (45°), teal (175°), burgundy (350°) | 85% | *The Kiss* — opulent gilded contrasts    |
| rothko        | Deep crimson (355°), burnt sienna (22°), muted orange (32°) | 82% | Colour field — warm moody cluster |
| hokusai       | Deep navy (225°), slate blue (210°), pale blue-gray (200°) | 80% | *The Great Wave* — layered ocean blues |
| turner        | Golden amber (42°), hazy orange (28°), pale sky blue (200°) | 75% | Luminous atmospheric haze               |
| mondrian      | Red (5°), cobalt blue (230°), golden yellow (52°) | 100% | Primary colour grid — bold, uncompromising |
| warhol        | Hot pink (330°), lime green (88°), turquoise (178°) | 100% | Pop art — saturated, flat, electric    |
| rembrandt     | Warm umber (28°), antique gold (44°), dark amber (35°) | 78% | Chiaroscuro — all warm, all depth      |

*Mondrian and Warhol run at full saturation; `--brightness 60` tones them down
if the output is too intense.  Rothko keeps all three primaries within a 37°
warm arc — CIELAB blends between them stay emotionally consistent rather than
going muddy.*

**Holidays**

| Name          | Primaries                                    | Sat |
|---------------|----------------------------------------------|-----|
| christmas     | Red (5°), deep green (125°), gold (48°)      | 92% |
| halloween     | Orange (25°), deep purple (270°), yellow (58°) | 95% |
| hanukkah      | Royal blue (228°), sky blue (205°), gold (48°) | 80% |
| valentines    | Rose red (355°), hot pink (340°), blush (15°) | 80% |
| easter        | Soft purple (280°), pale yellow (60°), light green (140°) | 45% |
| independence  | Red (5°), white-blue (218°), blue (238°)     | 90% |
| st patricks   | Shamrock green (130°), gold (50°), light green (145°) | 85% |
| thanksgiving  | Burnt orange (22°), warm brown (30°), deep gold (45°) | 80% |
| new year      | Champagne gold (48°), silver (218°), midnight blue (240°) | 65% |
| mardi gras    | Deep purple (270°), gold (50°), green (130°) | 90% |
| diwali        | Deep gold (45°), magenta (310°), saffron (30°) | 90% |

**School colors**

| School        | Primaries                                    | Sat |
|---------------|----------------------------------------------|-----|
| michigan      | Maize (50°), cobalt blue (230°), sky blue (210°) | 88% |
| alabama       | Crimson (350°), silver (215°), gold (48°)    | 82% |
| lsu           | Purple (270°), gold (48°), pale gold (52°)   | 90% |
| texas         | Burnt orange (22°), warm brown (28°), gold (45°) | 80% |
| ohio state    | Scarlet (5°), silver (215°), gold (48°)      | 82% |
| notre dame    | Gold (48°), navy (225°), green (130°)        | 88% |
| ucla          | Blue (228°), gold (50°), sky blue (210°)     | 85% |

**Moods & aesthetics**

| Name          | Primaries                                    | Sat | Character                                |
|---------------|----------------------------------------------|-----|------------------------------------------|
| neon          | Hot pink (310°), electric cyan (183°), acid green (90°) | 100% | Club lighting — maximum intensity |
| cherry blossom | Pale pink (348°), blush (15°), soft lavender (290°) | 38% | Gentle, floral, Japanese spring |
| vaporwave     | Hot pink (310°), purple (270°), electric cyan (185°) | 95% | Retrowave — 80s neon nostalgia          |
| cyberpunk     | Neon green (130°), electric blue (225°), magenta (300°) | 100% | High-contrast dystopian city lights  |
| cottagecore   | Sage green (130°), blush pink (350°), warm cream (45°) | 48% | Soft, domestic, garden-morning light  |
| gothic        | Deep burgundy (350°), deep purple (270°), dark rose (340°) | 78% | Brooding — all hues cluster near red |
| lo-fi         | Warm amber (35°), dusty rose (348°), muted sage (130°) | 52% | Relaxed, cozy, late-night study vibes |

#### Examples

```bash
# Water palette — default settings
python3 glowup.py play rule_trio --ip <device-ip> --palette water

# Van Gogh with Rule 90 (fractal) on CA A for structured cobalt regions
python3 glowup.py play rule_trio --ip <device-ip> --palette "van gogh" --rule-a 90

# Halloween at high speed — frantic orange-purple chaos
python3 glowup.py play rule_trio --ip <device-ip> --palette halloween --speed 20

# Mardi Gras with wider gap fill (2-bulb radius)
python3 glowup.py play rule_trio --ip <device-ip> --palette "mardi gras" --gap-fill 6

# Hokusai wave — all three CAs on Rule 90 for deep fractal blue layering
python3 glowup.py play rule_trio --ip <device-ip> --palette hokusai --rule-a 90 --rule-b 90 --rule-c 90

# Rothko — slow drift, very moody
python3 glowup.py play rule_trio --ip <device-ip> --palette rothko --speed 3 --drift-b 1.1 --drift-c 1.2

# Mondrian pop art — dial brightness down from the default 90
python3 glowup.py play rule_trio --ip <device-ip> --palette mondrian --brightness 60

# Custom palette: coral, turquoise, gold
python3 glowup.py play rule_trio --ip <device-ip> --palette custom --hue-a 15 --hue-b 178 --hue-c 48 --sat 85

# Michigan colors for game day
python3 glowup.py play rule_trio --ip <device-ip> --palette michigan --speed 6

# Gothic — slow, dim background glow, maximum menace
python3 glowup.py play rule_trio --ip <device-ip> --palette gothic --speed 4 --bg 3
```

---

### newtons_cradle

#### Background

Newton's Cradle is a physics desk toy — a row of steel balls hanging from strings.
Lift the rightmost ball, release it, and it strikes the row; the leftmost ball flies
out, swings back, and the cycle repeats indefinitely.

This effect has personal history.  The author first wrote a Newton's Cradle simulation
on the **Commodore Amiga in 1985** — a program that appeared on **Fish Disk #1**, the
very first disk in Fred Fish's legendary freely distributable software library.  The
original Amiga version featured per-pixel sphere shading, which was remarkable for the
hardware of the era.  This LIFX implementation carries that tradition forward: each ball
is rendered as a lit 3-D sphere using a full Phong illumination model.

#### How It Works

Five steel-coloured balls hang in a row.  The rightmost ball swings to the right and
returns; immediately the leftmost ball swings to the left and returns.  The middle balls
remain stationary throughout — exactly as physics demands.

The animation phase is driven by a sinusoidal pendulum model:

- **Phase 0 → 0.5:** right ball swings out and returns.
- **Phase 0.5 → 1.0:** left ball swings out and returns.
- Maximum speed occurs at the moment of collision (phase = 0 and 0.5); speed tapers
  to zero at the ends of the arc — physically correct simple-harmonic motion.

#### Ball Shading (Phong Illumination)

Each ball is rendered as a 3-D sphere cross-section under a Phong illumination model:

```
I = I_a · ambient
  + I_d · max(0, N · L)              (Lambertian diffuse)
  + I_s · max(0, R · V)^shininess    (Phong specular)
```

The light source sits at **25° from vertical toward the upper-left**, giving a classic
studio-lighting look.  The surface normal at each zone is derived from the unit-circle
sphere cross-section: `N = (x_rel, √(1 − x_rel²))`.

The specular highlight blends toward pure white via CIELAB interpolation (`lerp_color`)
so the colour transition from ball surface to hot-spot is perceptually smooth on any hue.

**Ball edges are naturally dark**: at `x_rel = ±1` the normal is horizontal, so the
upper-left light contributes nothing; only ambient (10%) brightness remains.  This dark
edge visually separates adjacent balls without requiring a physical gap between them —
set `--gap 0` for a fully packed row where shading alone draws the boundaries.

#### Auto-Layout

When `--ball-width 0` (default) and `--swing 0` (default), the effect auto-sizes in
two passes so the pendulum uses the full strip.

**Pass 1 — ball width** (treating swing = ball_width as a baseline):

```
ball_width = (zone_count − (n−1)×gap) ÷ (n + 2)     [floor division]
```

**Pass 2 — swing boost** (distribute the floor-division remainder to the swing arms):

```
leftover = zone_count − (n × bw + (n−1)×gap + 2 × bw)
swing    = ball_width + leftover ÷ 2
```

This is important on the standard **36-zone / 12-bulb LIFX string light**.  Pass 1
alone gives `bw = 4, swing = 4`, leaving 4 zones unused and producing a short arc.
Pass 2 redistributes those 4 zones to give `swing = 6`, so the pendulum travels from
zone 2 to zone 34 — the full usable strip — with no wasted space.

| Strip size | Balls | Gap | ball_width | swing |
|------------|-------|-----|-----------|-------|
| 36 zones (12 bulbs) | 5 | 1 | 4 | **6** |
| 108 zones (36 bulbs) | 5 | 1 | 14 | **17** |

#### Parameters

| Parameter      | Default | Range       | Description |
|----------------|---------|-------------|-------------|
| `--num-balls`  | 5       | 2–10        | Number of balls in the cradle |
| `--ball-width` | 0       | 0–30        | Zones per ball; 0 = auto-size to fill strip |
| `--gap`        | 1       | 0–9         | Zones between adjacent balls at rest; 0 = pure shading separation |
| `--swing`      | 0       | 0–80        | Swing arc beyond row end in zones; 0 = auto = one ball-width |
| `--speed`      | 1.5     | 0.3–10.0    | Full period in seconds (right-swing + left-swing = one cycle) |
| `--hue`        | 200     | 0–360       | Base hue in degrees (200 = steel-teal, 45 = gold, 0 = red) |
| `--sat`        | 15      | 0–100       | Base saturation percent (0 = pure gray / brushed steel) |
| `--brightness` | 90      | 1–100       | Maximum ball brightness percent |
| `--shininess`  | 25      | 1–100       | Specular exponent: 8=matte, 25=brushed metal, 60=chrome, 100=mirror |
| `--kelvin`     | 4000    | 1500–9000   | Color temperature in Kelvin |

#### Shading Presets

| Look               | Invocation |
|--------------------|-----------|
| Brushed steel      | `--sat 0` |
| Gold balls         | `--sat 80 --hue 45` |
| Blue titanium      | `--hue 200 --sat 30` |
| Copper             | `--hue 20 --sat 60` |
| Matte rubber       | `--shininess 8` |
| Mirror chrome      | `--shininess 60 --sat 5` |
| Slow-motion        | `--speed 3.0` |
| Touching balls     | `--gap 0` |

#### Examples

```bash
# Default — brushed steel-teal balls, auto-sized to the strip
python3 glowup.py play newtons_cradle --ip <device-ip>

# Pure brushed steel (no hue, just brightness gradients)
python3 glowup.py play newtons_cradle --ip <device-ip> --sat 0

# Gold balls with mirror chrome highlight
python3 glowup.py play newtons_cradle --ip <device-ip> --hue 45 --sat 80 --shininess 60

# Tight pack: balls touching, shading alone separates them
python3 glowup.py play newtons_cradle --ip <device-ip> --gap 0

# Slow-motion with 7 copper-toned balls
python3 glowup.py play newtons_cradle --ip <device-ip> --num-balls 7 --hue 20 --sat 60 --speed 3.0

# Maximum LIFX bulb separation (3 zones = one physical bulb)
python3 glowup.py play newtons_cradle --ip <device-ip> --gap 3
```

---

### embers

#### Background

Embers simulates a column of rising, cooling embers using a 1D heat
diffusion and convection model.  Heat is randomly injected at the
bottom of the string and undergoes three physical processes each frame:

1. **Convection** — the heat buffer shifts upward periodically,
   simulating buoyancy.  Hot embers visibly rise along the string.

2. **Diffusion + cooling** — each cell averages with its neighbours
   and is multiplied by a cooling factor:
   `T'[i] = (T[i-1] + T[i] + T[i+1]) / 3 × cooling`

3. **Turbulence** — random per-cell perturbation adds flicker and
   prevents the gradient from settling into a static equilibrium.

Occasional large heat bursts create visible "puffs" that travel up
the string as they cool and fade.

#### Color Gradient

Temperature maps to a physically motivated ember gradient:

| Temperature | Color |
|-------------|-------|
| 0.0 – 0.05 | Black (cold/dead) |
| 0.05 – 0.30 | Deep red (first glow) |
| 0.30 – 0.60 | Red → orange |
| 0.60 – 1.0 | Orange → bright yellow-white (hottest) |

#### Parameters

| Parameter      | Default | Range       | Description |
|----------------|---------|-------------|-------------|
| `--intensity`  | 0.7     | 0.0–1.0    | Probability of heat injection per frame |
| `--cooling`    | 0.98    | 0.80–0.999 | Cooling factor per step (lower = faster fade) |
| `--turbulence` | 0.08    | 0.0–0.3    | Random per-cell flicker amplitude |
| `--brightness` | 100     | 0–100      | Overall brightness percent |
| `--kelvin`     | 3500    | 1500–9000  | Color temperature in Kelvin |

#### Examples

```bash
# Default — embers rising from one end
python3 glowup.py play embers --ip <device-ip> --zpb 3

# Slow burn — embers cool quickly, barely reach the top
python3 glowup.py play embers --ip <device-ip> --zpb 3 --cooling 0.93

# Roaring fire — high intensity, long-lived embers
python3 glowup.py play embers --ip <device-ip> --zpb 3 --intensity 0.9 --cooling 0.99

# Calm glow — low turbulence, gentle injection
python3 glowup.py play embers --ip <device-ip> --zpb 3 --intensity 0.4 --turbulence 0.02
```

---

### jacobs_ladder

#### Background

Jacob's Ladder is a classic Frankenstein laboratory prop — two vertical
conductors with an electric arc that forms at the bottom, rises to the
top, breaks, and reforms.  This effect recreates that look on a string
light.

#### How It Works

Pairs of bright electrode nodes connected by a flickering blue-white
arc drift along the string.  When an arc reaches the far end it breaks
off and a new one spawns at the entry end.  Multiple arc pairs can
coexist, and at least one is always visible.

The electrode gap is modulated by smooth noise — a random walk toward
random targets, eased with a step factor — so the gap breathes apart
and together without ever collapsing or stretching too far.

#### Arc Dynamics

Each arc has several layers of intensity variation:

- **Per-arc flicker** — the entire arc's brightness varies randomly
  each frame across a wide range (0.15–0.85), creating dramatic dips
  where the arc nearly dies and moments where it blazes bright.
- **Surges** — 10% chance per frame of a full-intensity blaze that
  also lights up the electrodes to maximum.
- **Crackle** — 12% chance per bulb per frame of an individual bright
  white spike within the arc body, simulating electrical breakdown
  points.
- **Per-bulb flicker** — each bulb within the arc has independent
  random intensity (0.25–1.0), giving the arc visible internal
  structure.
- **Sine profile** — the arc is brightest in the center and tapers
  toward the electrodes.

#### Parameters

| Parameter      | Default | Range       | Description |
|----------------|---------|-------------|-------------|
| `--speed`      | 0.15    | 0.02–1.0   | Arc drift speed in bulbs per frame |
| `--arcs`       | 2       | 1–5        | Target number of simultaneous arc pairs |
| `--gap`        | 4       | 2–12       | Base gap between electrodes in bulbs |
| `--reverse`    | 0       | 0–1        | Drift direction: 0 = forward, 1 = reverse |
| `--brightness` | 100     | 0–100      | Overall brightness percent |
| `--kelvin`     | 3500    | 1500–9000  | Color temperature in Kelvin |

#### Examples

```bash
# Default — two arcs drifting along the string
python3 glowup.py play jacobs_ladder --ip <device-ip> --zpb 3

# Slow creep with wide electrode gaps
python3 glowup.py play jacobs_ladder --ip <device-ip> --zpb 3 --speed 0.06 --gap 6

# Busy lab — many arcs, fast drift
python3 glowup.py play jacobs_ladder --ip <device-ip> --zpb 3 --arcs 4 --speed 0.25

# Reverse direction
python3 glowup.py play jacobs_ladder --ip <device-ip> --zpb 3 --reverse 1
```

### spin

Migrates colors through the concentric rings of each polychrome bulb.
Each LIFX bulb contains three nested light-guide tubes (inner, middle,
outer), and spin cycles colors through them so hues appear to flow
between the inside and outside of every bulb simultaneously.

Colors come from the 50-palette preset system shared with `rule_trio`.
The "custom" palette provides an evenly-spaced rainbow (red / green /
blue).  All palettes interpolate smoothly via CIELAB.

| Parameter | Default | Range | Description |
|---|---|---|---|
| `speed` | 2.0 | 0.2–30.0 | Seconds per full rotation |
| `brightness` | 100 | 0–100 | Brightness percent |
| `kelvin` | 3500 | 1500–9000 | Color temperature |
| `palette` | custom | 51 presets | Colour preset (50 named + rainbow default) |
| `bulb_offset` | 30.0 | 0–360 | Hue offset in degrees between adjacent bulbs |
| `zones_per_bulb` | 3 | 1–16 | Zones per physical bulb |

```bash
# Default rainbow spin
python3 glowup.py play spin --ip <device-ip> --zpb 3

# Fire palette — red/orange/amber
python3 glowup.py play spin --ip <device-ip> --zpb 3 --palette fire

# Slow holiday spin
python3 glowup.py play spin --ip <device-ip> --zpb 3 --palette christmas --speed 5

# Wide color separation — great on concentric rings
python3 glowup.py play spin --ip <device-ip> --zpb 3 --palette mondrian
```

> **Tip:** Spin looks best with palettes whose colors are widely separated
> on the color wheel — the concentric rings make the transitions between
> dissimilar hues really visible.  Try `mondrian` (red/blue/yellow),
> `cyberpunk` (green/blue/magenta), or `tropical` (teal/coral/yellow).

---

### sonar

Sonar simulates a radar / sonar display on a string light.  Wavefronts
radiate outward from sources positioned at the ends of the string and
between drifting obstacles.  When a wavefront hits an obstacle it
reflects; when it returns to its source it is absorbed and its tail
continues to fade.

#### How It Works

1. **Obstacles** — red-orange markers meander slowly across the middle
   of the string.  One obstacle is placed per 24 bulbs (minimum 1).

2. **Sources** — emitting points sit at each end of the string and
   midway between adjacent obstacles.  Each source is limited to one
   live pulse at a time.

3. **Wavefronts** — bright white pulses travel outward at the
   configured speed.  As the wavefront passes each bulb it deposits a
   particle that decays over time, producing a fading tail.

4. **Reflection & absorption** — a wavefront that reaches an obstacle
   reverses direction; when it returns to its source it is absorbed.

Obstacle count scales with string length: 1 per 24 bulbs (72 zones at
the default 3 zones-per-bulb), minimum 1.

#### Parameters

| Parameter | Default | Range | Description |
|---|---|---|---|
| `speed` | 8.0 | 0.3–20.0 | Wavefront travel speed in bulbs per second |
| `decay` | 2.0 | 0.1–10.0 | Particle decay time in seconds (tail lifetime) |
| `pulse_interval` | 2.0 | 0.5–15.0 | Seconds between pulse emissions |
| `obstacle_speed` | 0.5 | 0.0–3.0 | Obstacle drift speed in bulbs per second |
| `obstacle_hue` | 15.0 | 0–360 | Obstacle color hue in degrees |
| `obstacle_brightness` | 80 | 10–100 | Obstacle brightness as percent |
| `brightness` | 100 | 10–100 | Wavefront peak brightness as percent |
| `kelvin` | 6500 | 1500–9000 | Wavefront color temperature |
| `zones_per_bulb` | 3 | 1–10 | Zones per physical bulb |

#### Examples

```bash
# Default sonar — one obstacle drifting on a 36-bulb string
python3 glowup.py play sonar --ip <device-ip> --zpb 3

# Long tails — slow pulses with extended decay
python3 glowup.py play sonar --ip <device-ip> --zpb 3 --speed 4 --decay 5

# Fast radar sweep — quick pulses, short tails
python3 glowup.py play sonar --ip <device-ip> --zpb 3 --speed 16 --decay 0.5 --pulse_interval 1

# Frozen obstacles — set obstacle speed to zero
python3 glowup.py play sonar --ip <device-ip> --zpb 3 --obstacle_speed 0
```

