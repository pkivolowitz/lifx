# Effect Developer Guide

### Architecture Overview

```
glowup.py              CLI — argparse, dispatches to subcommand handlers
    │
    ├── transport.py       LIFX LAN protocol: discovery, LifxDevice, UDP sockets
    ├── engine.py          Engine (threaded frame loop) + Controller (thread-safe API)
    ├── simulator.py       Live tkinter preview window (optional, graceful fallback)
    ├── solar.py           Sunrise/sunset calculator (NOAA algorithm, no dependencies)
    ├── colorspace.py      CIELAB color interpolation and palette utilities
    ├── lanscan.py         LAN device discovery and product identification
    │
    ├── server.py          REST API server, scheduler, device management
    ├── mqtt_bridge.py     Optional MQTT pub/sub bridge (requires paho-mqtt)
    │
    ├── test_config.py     Config validation tests
    ├── test_effects.py    Effect rendering tests (all effects × multiple zone counts)
    ├── test_override.py   Phone override and group member logic tests
    ├── test_schedule.py   Schedule parsing and entry resolution tests
    ├── test_solar.py      Solar time calculation tests
    ├── test_virtual_multizone.py   VirtualMultizoneDevice zone dispatch tests
    ├── test_multizone_products.py  LIFX product database tests
    │
    └── effects/
        ├── __init__.py       Effect base class, Param system, auto-registry, utilities
        ├── aurora.py         Northern lights color waves
        ├── binclock.py       Binary clock display
        ├── breathe.py        Smooth brightness pulsing
        ├── cylon.py          Bouncing scanner bar (Battlestar Galactica)
        ├── embers.py         Smoldering fire embers
        ├── fireworks.py      Rocket launches and starbursts
        ├── flag.py           Waving flag with Perlin noise perspective projection
        ├── flag_data.py      199-country flag color database
        ├── jacobs_ladder.py  Rising electrical arcs
        ├── morse.py          Morse code message display
        ├── newtons_cradle.py Pendulum simulation with Phong sphere shading
        ├── rule30.py         Wolfram elementary 1-D cellular automaton
        ├── rule_trio.py      Three CAs with CIELAB blending and 50 palettes
        ├── sonar.py          Radar sweep pulse
        ├── spin.py           Rotating color segments
        ├── twinkle.py        Random sparkle points
        ├── wave.py           Traveling color wave
        │
        ├── bloom.py          [hidden] Concentric zone bloom/pulse diagnostic
        ├── crossfade.py      [hidden] Two-color zone-lagged crossfade diagnostic
        ├── polychrome_test.py [hidden] Polychrome vs mono rendering test
        └── zone_map.py       [hidden] Zone index identification diagnostic
```

**Key design principle:** Effects are pure renderers. They receive elapsed
time and zone count, and return a list of HSBK colors. They never touch
the network, device objects, or anything outside their render function.

### Creating a New Effect

1. Create a new file in `effects/` (e.g., `effects/rainbow.py`).
2. Subclass `Effect` and set `name` and `description`.
3. Declare parameters as class-level `Param` instances.
4. Implement `render(self, t: float, zone_count: int) -> list[HSBK]`.
5. That's it — the effect auto-registers and appears in the CLI.

> **Hidden effects:** Effects whose name starts with `_` are hidden from
> the iOS app by default.  Users can reveal them with the "Show Hidden"
> toggle on the effect list screen.  Use this convention for diagnostic
> and test effects that would otherwise clutter the list.  Hidden
> effects are experimental and may be removed at any time.
>
> | Effect | Purpose |
> |--------|---------|
> | `_bloom` | Concentric zone pulse — tests per-zone brightness weighting on polychrome bulbs |
> | `_crossfade` | Two-color crossfade with zone lag — validates smooth HSBK interpolation |
> | `_polychrome_test` | Renders differently on color vs mono bulbs — verifies BT.709 luma path |
> | `_zone_map` | Lights one zone at a time in sequence — identifies physical zone indices |

No imports in `__init__.py` are needed. The framework auto-discovers all
`.py` files in the `effects/` directory via `pkgutil.iter_modules` and
imports them at startup. The `EffectMeta` metaclass automatically
registers any `Effect` subclass that defines a `name`.

### The Effect Base Class

```python
from effects import Effect, Param, HSBK

class MyEffect(Effect):
    name: str = "myeffect"           # Unique ID — used in CLI and API
    description: str = "One-liner"   # Shown in effect listings

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one animation frame.

        Args:
            t:          Seconds elapsed since the effect started (float).
            zone_count: Number of zones on the target device (int).

        Returns:
            A list of exactly `zone_count` HSBK tuples.
        """
        ...
```

**Methods available on `self`:**

| Method                        | Description                                         |
|-------------------------------|-----------------------------------------------------|
| `render(t, zone_count)`       | **Must override.** Produce one frame of colors.     |
| `on_start(zone_count)`        | Called when this effect becomes active. Override for setup. |
| `on_stop()`                   | Called when this effect is stopped/replaced. Override for cleanup. |
| `get_params() -> dict`        | Returns current parameter values as `{name: value}`. |
| `set_params(**kwargs)`        | Update parameters at runtime (validates and clamps). |
| `get_param_defs() -> dict`    | Class method. Returns `{name: Param}` definitions.  |

### The Param System

Parameters are declared as class-level `Param` instances. They serve
three purposes:

1. **CLI** — auto-generated as `--flag` argparse options.
2. **API** — provide metadata (type, range, description) for a future REST API.
3. **Runtime** — store current values with automatic validation and clamping.

```python
from effects import Param, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX

class MyEffect(Effect):
    # Numeric param with range clamping
    speed = Param(2.0, min=0.2, max=30.0,
                  description="Seconds per cycle")

    # Integer param
    count = Param(5, min=1, max=20,
                  description="Number of segments")

    # String param
    message = Param("HELLO", description="Text to display")

    # Param with discrete choices
    mode = Param("linear", choices=["linear", "ease", "bounce"],
                 description="Motion curve type")

    # Color temperature (common pattern)
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")
```

**Param attributes:**

| Attribute     | Type           | Description                                    |
|---------------|----------------|------------------------------------------------|
| `default`     | `Any`          | Default value. Also determines the parameter type (int/float/str). |
| `min`         | `Optional`     | Minimum allowed value (numeric only). Values below are clamped. |
| `max`         | `Optional`     | Maximum allowed value (numeric only). Values above are clamped. |
| `description` | `str`          | Human-readable help text.                      |
| `choices`     | `Optional[list]` | If set, value must be one of these. Raises `ValueError` otherwise. |

**Type inference:** The type of `default` determines the argparse type.
Use `2.0` for float, `2` for int, `"text"` for str.

**Accessing params at render time:** Inside `render()`, parameters are
regular instance attributes:

```python
def render(self, t: float, zone_count: int) -> list[HSBK]:
    phase = (t % self.speed) / self.speed   # self.speed is the current value
    ...
```

### Color Model (HSBK)

LIFX uses HSBK (Hue, Saturation, Brightness, Kelvin) with all components
as unsigned 16-bit integers:

| Component    | Range       | Notes                                         |
|--------------|-------------|-----------------------------------------------|
| Hue          | 0–65535     | Maps to 0–360 degrees. 0=red, ~21845=green, ~43690=blue |
| Saturation   | 0–65535     | 0=white/unsaturated, 65535=fully saturated    |
| Brightness   | 0–65535     | 0=off, 65535=maximum brightness               |
| Kelvin       | 1500–9000   | Color temperature. Only meaningful when saturation is low. |

The `HSBK` type alias is `tuple[int, int, int, int]`.

**Important constants** (importable from `effects`):

```python
HSBK_MAX = 65535        # Max value for H, S, B
KELVIN_MIN = 1500       # Warmest color temperature
KELVIN_MAX = 9000       # Coolest color temperature
KELVIN_DEFAULT = 3500   # Warm white default
```

### Utility Functions

Import these from the `effects` package:

```python
from effects import hue_to_u16, pct_to_u16
```

**`hue_to_u16(degrees: float) -> int`**
Convert a hue in degrees (0–360) to LIFX u16 (0–65535).

```python
red   = hue_to_u16(0)     # 0
green = hue_to_u16(120)   # 21845
blue  = hue_to_u16(240)   # 43690
```

**`pct_to_u16(percent: int | float) -> int`**
Convert a percentage (0–100) to LIFX u16 (0–65535).

```python
half_bright = pct_to_u16(50)   # 32767
full_bright = pct_to_u16(100)  # 65535
```

These helpers let you declare user-facing parameters in intuitive units
(degrees, percentages) while producing the raw u16 values the protocol
requires.

### Color Interpolation (`--lerp`)

When an effect blends between two colors — a stripe boundary on a flag,
the midpoint of a breathing cycle, the antinode of a standing wave — it
must interpolate from one HSBK tuple to another. The choice of *where*
that interpolation happens (which color space) makes a dramatic visible
difference.

GlowUp ships two interpolation backends, selectable at runtime:

```bash
python3 glowup.py play flag --ip 10.0.0.62 --lerp lab    # default — perceptually uniform
python3 glowup.py play flag --ip 10.0.0.62 --lerp hsb    # lightweight fallback
```

The `--lerp` switch is available on the `play` subcommand and in the
server configuration (`"lerp": "lab"` or `"lerp": "hsb"` in the config
JSON). The default is `lab`.

#### The Problem with HSB Interpolation

HSB (Hue, Saturation, Brightness) represents hue as an angle on a color
wheel. Interpolating between two hues means walking around the wheel, and
the "shortest path" algorithm picks the shorter arc. This is mathematically
correct but perceptually wrong.

Consider the French flag: blue (240°) next to white (0° hue, 0%
saturation). The shortest path from 240° to 0° passes through 300° —
*magenta and red*. A border bulb at the blue/white stripe boundary
visibly dips through red where no red should exist. The same artifact
appears anywhere two colors are separated by an unlucky arc on the
HSB wheel.

This was first noticed during live testing with a sheet of white paper
held in front of the string lights as a diffuser, isolating color
transitions from the distraction of individual bulb geometry.

#### CIELAB: Perceptually Uniform Interpolation

The solution is to interpolate in CIELAB (CIE 1976 L\*a\*b\*), a color
space designed by the International Commission on Illumination
specifically so that equal numeric distances correspond to equal
*perceived* color differences. A straight line between any two colors in
Lab space traces the most natural-looking transition a human can perceive.

The conversion pipeline:

```
HSBK → sRGB → linear RGB → CIE XYZ (D65) → CIELAB
                    ↓ interpolate in L*a*b*
CIELAB → CIE XYZ (D65) → linear RGB → sRGB → HSBK
```

Each step uses published standards:
- **sRGB gamma**: IEC 61966-2-1 piecewise transfer function (not a simple
  power curve — there's a linear segment near black)
- **XYZ transform**: BT.709 / sRGB primaries with D65 reference white
- **Lab nonlinearity**: Cube-root compression with linear extension below
  the threshold (δ = 6/29)

The blue-to-white interpolation in Lab passes through lighter, less
saturated blue — exactly what a human would paint if asked to blend those
two colors. No phantom red, no magenta, no perceptual discontinuities.

#### A/B Validation

The difference was validated empirically using the `_crossfade` diagnostic
effect, which alternates between HSB and Lab interpolation on the same
color pattern. Viewing through a diffuser, the Lab transitions were
unanimously smoother, and the French flag's blue/white boundary artifact
was completely eliminated.

#### Performance

Lab interpolation is more expensive than HSB — there's a full color space
round-trip per call. Benchmarked cost per `lerp_color()` call:

| Platform          | Lab       | HSB      | Overhead   |
|-------------------|-----------|----------|------------|
| Mac (Apple Silicon) | 4.4 µs  | ~0.5 µs  | 0.9% of 50ms frame budget (108 zones) |
| Raspberry Pi 5    | 58.8 µs   | ~7 µs   | 12.7% of 50ms frame budget (108 zones) |

Both are well within the real-time animation budget. Users on
significantly slower hardware (original Pi, Pi Zero) can fall back to
`--lerp hsb` to eliminate the overhead entirely.

#### Using lerp_color in Custom Effects

All color blending in effect code should go through `lerp_color()`:

```python
from colorspace import lerp_color

# Blend 30% of the way from color1 to color2.
blended: HSBK = lerp_color(color1, color2, 0.3)
```

The function signature is identical regardless of which backend is active.
Effect code never needs to know whether Lab or HSB is running — the
`--lerp` switch handles it globally.

If your effect overrides brightness after blending (common for
intensity-based effects like aurora and wave), extract hue and saturation
from the blended result and substitute your own brightness:

```python
blended = lerp_color(color1, color2, blend_factor)
colors.append((blended[0], blended[1], my_brightness, self.kelvin))
```

### Lifecycle Hooks

Override these optional methods for effects that need setup or teardown:

```python
def on_start(self, zone_count: int) -> None:
    """Called once when this effect becomes active.

    Use for allocating buffers, seeding random state, or
    any setup that depends on the actual device zone count.
    """
    self._buffer = [(0, 0, 0, KELVIN_DEFAULT)] * zone_count

def on_stop(self) -> None:
    """Called when this effect is stopped or replaced.

    Use to release resources or reset state.
    """
    self._buffer = None
```

### Complete Example

Here is a complete, minimal effect that creates a rotating rainbow:

```python
"""Rainbow effect — rotating spectrum across all zones.

A full hue rotation is spread evenly across the string and
scrolls continuously at a configurable speed.
"""

__version__ = "1.0"

from . import (
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Full hue range (one more than max to wrap correctly).
HUE_RANGE: int = HSBK_MAX + 1


class Rainbow(Effect):
    """Rotating rainbow — the full spectrum scrolls across the string."""

    name: str = "rainbow"
    description: str = "Rotating rainbow across all zones"

    speed = Param(4.0, min=0.5, max=60.0,
                  description="Seconds per full rotation")
    brightness = Param(100, min=0, max=100,
                       description="Brightness percent")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of the rotating rainbow.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.
        """
        bri: int = pct_to_u16(self.brightness)

        # Phase offset advances with time, creating the rotation.
        phase: float = (t / self.speed) % 1.0

        colors: list[HSBK] = []
        for i in range(zone_count):
            # Spread the full hue spectrum evenly across zones,
            # offset by the current phase for rotation.
            hue: int = int(((i / zone_count) + phase) % 1.0 * HUE_RANGE) % HUE_RANGE
            colors.append((hue, HSBK_MAX, bri, self.kelvin))

        return colors
```

Save this as `effects/rainbow.py` and it will automatically appear in
`python3 glowup.py effects` and be playable via `python3 glowup.py play rainbow --ip ...`.

