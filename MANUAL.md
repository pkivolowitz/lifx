# GLOWUP - LIFX Effect Engine — User Manual

Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
Licensed under the MIT License. See [LICENSE](LICENSE) for details.

This project utilizes AI assistance (Claude 4.6) for boilerplate and logic
expansion. All final architectural decisions, algorithmic validation, and
code integration are performed by Perry Kivolowitz, the sole Human Author.

---

## Table of Contents

1. [Overview](#overview)
2. [Requirements](#requirements)
3. [Quick Start](#quick-start)
4. [CLI Reference](#cli-reference)
   - [discover](#discover)
   - [effects](#effects)
   - [identify](#identify)
   - [play](#play)
5. [Scheduler (Daemon)](#scheduler-daemon)
   - [Configuration File](#configuration-file)
   - [Symbolic Times](#symbolic-times)
   - [Dry Run](#dry-run)
   - [Installing as a systemd Service](#installing-as-a-systemd-service)
   - [Controlling the Service](#controlling-the-service)
6. [Built-in Effects](#built-in-effects)
   - [cylon](#cylon)
   - [breathe](#breathe)
   - [wave](#wave)
   - [twinkle](#twinkle)
   - [morse](#morse)
   - [aurora](#aurora)
   - [binclock](#binclock)
   - [flag](#flag)
   - [fireworks](#fireworks)
   - [rule30](#rule30)
   - [rule_trio](#rule_trio)
   - [newtons_cradle](#newtons_cradle)
7. [Effect Developer Guide](#effect-developer-guide)
   - [Architecture Overview](#architecture-overview)
   - [Creating a New Effect](#creating-a-new-effect)
   - [The Effect Base Class](#the-effect-base-class)
   - [The Param System](#the-param-system)
   - [Color Model (HSBK)](#color-model-hsbk)
   - [Utility Functions](#utility-functions)
   - [Color Interpolation (`--lerp`)](#color-interpolation---lerp)
   - [Lifecycle Hooks](#lifecycle-hooks)
   - [Complete Example](#complete-example)
8. [Live Simulator](#live-simulator)
9. [Engine and Controller API](#engine-and-controller-api)
   - [VirtualMultizoneDevice](#virtualmultizonedevice)
10. [Testing](#testing)
11. [REST API Server](#rest-api-server)
    - [Server Configuration](#server-configuration)
    - [API Endpoints](#api-endpoints)
    - [Authentication](#authentication)
    - [Server-Sent Events (Live Colors)](#server-sent-events-live-colors)
    - [Phone Override Behavior](#phone-override-behavior)
    - [Installing the Server as a systemd Service](#installing-the-server-as-a-systemd-service)
12. [GlowUp iOS App](#glowup-ios-app)
    - [Connectivity Options](#connectivity-options)
    - [Building the App](#building-the-app)
    - [Running on Your iPhone](#running-on-your-iphone)
    - [App Screens](#app-screens)
13. [Cloudflare Tunnel (Remote Access)](#cloudflare-tunnel-remote-access)

---

## Overview

The GLOWUP LIFX Effect Engine drives animated lighting effects on LIFX
devices (string lights, beams, Z strips, single color bulbs, and monochrome
bulbs) over the local network using the LIFX LAN protocol. It replaces the
battery-draining phone app with a lightweight CLI that can run on a
Raspberry Pi or similar as a daemon.

Color effects on monochrome (white-only) bulbs are automatically converted
to perceptually correct brightness using BT.709 luma coefficients.

**Virtual multizone** — Any combination of LIFX devices can be grouped
into a virtual multizone strip.  Multizone devices (string lights, beams)
contribute all their zones; single bulbs contribute one zone each.  Five
white lamps around a room become a 5-zone animation surface; add a
108-zone string light and it becomes 113 zones.  A cylon scanner sweeps
lamp to lamp, aurora curtains drift around you, a wave oscillates across
the room.  Define device groups in a config file and the engine treats
them as one device.  Effects don't need any changes — they already
render per-zone colors, and the virtual device routes each color back
to the correct physical device, batching multizone updates efficiently.

LIFX limits a single physical chain to 3 string lights (36 bulbs,
108 zones — 12 bulbs × 3 zones × 3 strings).  The virtual multizone
feature removes that ceiling entirely.  Each chain is an independent
network device with its own IP address; the engine stitches them
together in software.  Five separate 3-string chains scattered around
a room become a single 180-bulb, 540-zone animation surface with no
hardware modifications.

Effects are **pure renderers** — they know nothing about devices or
networking. Given a timestamp and a zone count, they return a list of
colors. The engine handles framing, timing, and transport.

## Requirements

- Python 3.10+
- One or more LIFX devices on the same LAN subnet (multizone, single color, or monochrome)
- No external dependencies — the entire stack is pure Python

## Quick Start

```bash
# 1. Find your LIFX devices
python3 glowup.py discover

# 2. See what effects are available
python3 glowup.py effects

# 3. Run an effect (replace IP with your device's IP)
python3 glowup.py play cylon --ip <device-ip>

# 4. Or animate a group of bulbs as a virtual multizone
python3 glowup.py play cylon --config schedule.json --group office

# 5. Press Ctrl+C to stop (fades to black gracefully)
```

---

## CLI Reference

The program is invoked as:

```
python3 glowup.py <command> [options]
```

### discover

Find all LIFX devices on the local network via UDP broadcast.

```bash
python3 glowup.py discover [--timeout SECONDS] [--ip ADDRESS] [--json]
```

| Option      | Default | Description                                    |
|-------------|---------|------------------------------------------------|
| `--timeout` | 3.0     | How long to listen for responses (s)           |
| `--ip`      | *(none)* | Query a specific device IP instead of broadcast |
| `--json`    | off     | Also print results as JSON                     |

Output is a formatted table showing each device's label, product type,
group, IP address, MAC address, and zone count.

### effects

List all registered effects and their tunable parameters.

```bash
python3 glowup.py effects
```

Each effect is printed with its name, description, and every parameter
including its default value and valid range.

### identify

Pulse a device's brightness so you can visually locate which physical
bulb corresponds to a given IP address. The device slowly breathes
between dim and full brightness in warm white until you press Ctrl+C.

```bash
python3 glowup.py identify --ip <device-ip>
```

| Option | Default | Description                          |
|--------|---------|--------------------------------------|
| `--ip` | *(required)* | Target device IP address or hostname |

Works with all device types (multizone, single color, monochrome).
On stop, the device is powered off.

### play

Run an effect on a device or device group. Blocks until Ctrl+C or SIGTERM.

**Single device:**

```bash
python3 glowup.py play <effect> --ip <device_ip> [--fps N] [--param value ...]
```

**Virtual multizone (device group):**

```bash
python3 glowup.py play <effect> --config <file> --group <name> [--fps N] [--param value ...]
```

When using `--config`/`--group`, devices are combined into a virtual
multizone strip.  Multizone devices (string lights, beams) contribute all
their physical zones; single bulbs contribute one zone each.  A group
containing a 108-zone string light and 4 single bulbs becomes a 112-zone
virtual device.  Effects that spread patterns across zones (cylon, aurora,
wave, twinkle) animate across all devices as if they were a single strip.
Multizone devices receive efficient batched updates (the same 2-packet
extended multizone protocol); single bulbs receive individual `set_color()`
calls.  Monochrome bulbs in the group automatically receive BT.709
luma-converted brightness.  You can mix any device types freely.

| Option      | Default | Description                               |
|-------------|---------|-------------------------------------------|
| `--ip`      | *(none)* | Target device IP address (single device mode) |
| `--config`  | *(none)* | Path to config file containing device groups |
| `--group`   | *(none)* | Device group name (requires `--config`)   |
| `--fps`     | 20      | Frames per second for the render loop     |
| `--sim`     | off     | Open a live simulator window alongside the real lights               |
| `--sim-only` | off    | Query device geometry then run the effect in the simulator only — no commands sent to the lights (see [Sim-Only Mode](#sim-only-mode)) |
| `--zpb`     | 1       | Zones per bulb — group adjacent zones into single displayed bulbs |

You must specify either `--ip` or both `--config` and `--group` (not both).

Effect-specific parameters are auto-generated as `--flag` options using
hyphenated names (e.g. `--launch-rate`, `--burst-spread`).  Any parameter
not specified on the command line uses the effect's default.

**Getting help — three levels:**

```bash
# Top-level: subcommands and global options
python3 glowup.py --help

# play options only (no effect parameters — they vary per effect)
python3 glowup.py play --help

# Full parameter reference for one specific effect
python3 glowup.py play fireworks --help
python3 glowup.py play cylon --help
```

`play --help` intentionally omits effect parameters to keep the output
readable.  Use `play <effect> --help` to see every parameter for that
effect, its default value, and its valid range.

**Examples:**

```bash
# Red cylon scanner, fast and wide
python3 glowup.py play cylon --ip <device-ip> --speed 1.0 --width 12 --hue 0

# Slow blue-to-green breathe
python3 glowup.py play breathe --ip <device-ip> --speed 8.0 --hue1 240 --hue2 120

# Morse code message
python3 glowup.py play morse --ip <device-ip> --message "SOS" --unit 0.1

# Aurora borealis at low brightness
python3 glowup.py play aurora --ip <device-ip> --brightness 40 --speed 15

# Waving French flag
python3 glowup.py play flag --ip <device-ip> --country france

# Cylon scanner across 5 room lamps (virtual multizone)
python3 glowup.py play cylon --config schedule.json --group office --speed 3

# Aurora drifting around a room
python3 glowup.py play aurora --config schedule.json --group living-room

# Preview an effect in the simulator window alongside the real lights
python3 glowup.py play cylon --ip <device-ip> --sim
```

On stop (Ctrl+C or closing the simulator window), the device fades to
black over 500ms.

---

## Scheduler (Daemon)

The scheduler (`scheduler.py`) runs effects on a timed schedule, with
sunrise/sunset awareness. It manages multiple independent device groups,
each running its own effect on its own schedule. Designed to run as a
systemd service on a Raspberry Pi (or any Linux box).

```bash
python3 scheduler.py /etc/glowup/schedule.json
```

The scheduler polls every 30 seconds to determine which schedule entry
should be active for each group. When the active entry changes, it
gracefully stops the old effect (SIGTERM → fade to black) and starts
the new one. Crashed subprocesses are automatically restarted.

### Configuration File

The config file is JSON with three sections: `location`, `groups`, and
`schedule`.

```json
{
    "location": {
        "latitude": 43.07,
        "longitude": -89.40,
        "_comment": "Your coordinates — needed for sunrise/sunset"
    },
    "groups": {
        "porch": ["porch_string_lights"],
        "living-room": ["10.0.0.10", "10.0.0.12"]
    },
    "schedule": [
        {
            "name": "porch evening aurora",
            "group": "porch",
            "start": "sunset-30m",
            "stop": "23:00",
            "effect": "aurora",
            "params": {
                "speed": 10.0,
                "brightness": 60
            }
        },
        {
            "name": "porch weekday morning",
            "days": "MTWRF",
            "group": "porch",
            "start": "sunrise-30m",
            "stop": "sunrise+30m",
            "effect": "flag",
            "params": {
                "country": "us",
                "brightness": 70
            }
        },
        {
            "name": "porch overnight clock",
            "group": "porch",
            "start": "23:00",
            "stop": "sunrise-30m",
            "effect": "binclock",
            "params": {
                "brightness": 40
            }
        }
    ]
}
```

**`location`** — Your latitude and longitude in decimal degrees. Required
for resolving symbolic times (sunrise, sunset, etc.).

**`groups`** — Named collections of device IPs or hostnames. Each group
is managed independently — multiple groups can run different effects at
the same time. Use hostnames if you have DNS/mDNS set up, or raw IPs.

**`schedule`** — Ordered list of schedule entries. Each entry specifies:

| Field    | Required | Description                                      |
|----------|----------|--------------------------------------------------|
| `name`   | yes      | Human-readable label (used in logs)              |
| `group`  | yes      | Which device group to target                     |
| `start`  | yes      | When to start (fixed time or symbolic)           |
| `stop`   | yes      | When to stop (fixed time or symbolic)            |
| `effect` | yes      | Effect name (e.g., `"aurora"`, `"cylon"`)        |
| `params` | no       | Effect parameter overrides (e.g., `{"speed": 5}`) |
| `days`   | no       | Day-of-week filter (e.g., `"MTWRF"` for weekdays) |

**Day-of-week filtering** — The `days` field restricts an entry to specific
days using the academic letter convention:

| Letter | Day       |
|--------|-----------|
| M      | Monday    |
| T      | Tuesday   |
| W      | Wednesday |
| R      | Thursday  |
| F      | Friday    |
| S      | Saturday  |
| U      | Sunday    |

Examples: `"MTWRF"` = weekdays, `"SU"` = weekends, `"MWF"` = Mon/Wed/Fri.
Omitting the field (or setting it to `""`) means every day. Letters can
appear in any order but must not repeat.

When multiple entries for the same group overlap, the first match in
config file order wins (put higher-priority entries first).

Overnight entries work automatically — if `stop` is earlier than `start`,
the scheduler adds a day to the stop time (e.g., `"start": "23:00",
"stop": "06:00"` runs from 11 PM to 6 AM the next morning).

### Symbolic Times

Start and stop times can be fixed (`"14:30"`) or symbolic:

| Symbol     | Meaning                                          |
|------------|--------------------------------------------------|
| `sunrise`  | Sun crosses the horizon (upper limb visible)     |
| `sunset`   | Sun crosses the horizon (upper limb disappears)  |
| `dawn`     | Civil twilight begins (sun 6° below horizon)     |
| `dusk`     | Civil twilight ends (sun 6° below horizon)       |
| `noon`     | Solar noon (sun at highest point)                |
| `midnight` | 00:00 local time                                 |

Add offsets with `+` or `-`:

```
sunset-30m       30 minutes before sunset
sunrise+1h       1 hour after sunrise
dawn+1h30m       1 hour 30 minutes after dawn
noon-2h          2 hours before solar noon
```

Solar calculations use the NOAA algorithm (built-in, no dependencies)
and are recalculated daily.

### Dry Run

Preview the resolved schedule without running any effects:

```bash
python3 scheduler.py --dry-run schedule.json.example
```

This prints solar event times for your location, all device groups, and
the resolved schedule with concrete times. Active entries are flagged.
Use this to verify your config before deploying.

### Installing as a systemd Service

1. **Clone the repository** to the target machine:

```bash
git clone https://github.com/pkivolowitz/lifx.git /home/pi/lifx
```

2. **Create the config file** at `/etc/glowup/schedule.json`:

```bash
sudo mkdir -p /etc/glowup
sudo cp /home/pi/lifx/schedule.json.example /etc/glowup/schedule.json
sudo nano /etc/glowup/schedule.json   # edit for your location, devices, schedule
```

3. **Test with dry run** to verify times resolve correctly:

```bash
python3 /home/pi/lifx/scheduler.py --dry-run /etc/glowup/schedule.json
```

4. **Test live** before installing the service:

```bash
python3 /home/pi/lifx/scheduler.py /etc/glowup/schedule.json
# Watch the logs, Ctrl+C to stop
```

5. **Install the systemd service**:

```bash
sudo cp /home/pi/lifx/glowup-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable glowup-scheduler
sudo systemctl start glowup-scheduler
```

If your install path is not `/home/pi/lifx`, edit the service file first:

```bash
sudo nano /etc/systemd/system/glowup-scheduler.service
# Update ExecStart and WorkingDirectory to match your paths
```

### Controlling the Service

```bash
# Check status and recent logs
sudo systemctl status glowup-scheduler

# View full logs
sudo journalctl -u glowup-scheduler -f          # follow live
sudo journalctl -u glowup-scheduler --since today

# Stop / start / restart
sudo systemctl stop glowup-scheduler
sudo systemctl start glowup-scheduler
sudo systemctl restart glowup-scheduler

# Disable (won't start on boot)
sudo systemctl disable glowup-scheduler

# After editing the config file, restart to pick up changes
sudo systemctl restart glowup-scheduler
```

The scheduler logs to the systemd journal, including solar event times
(recalculated daily), schedule transitions, subprocess starts/stops,
and any errors.

---

## Built-in Effects

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
| `--speed`      | 8.0     | 0.5–120    | Base generations per second for CA A                                         |
| `--drift-b`    | 1.31    | 0.1–8.0    | Speed multiplier for CA B (irrational default avoids phase lock-in)          |
| `--drift-c`    | 1.73    | 0.1–8.0    | Speed multiplier for CA C (irrational default avoids phase lock-in)          |
| `--palette`    | 0       | 0–50       | Colour preset (see table below); 0 = use `--hue-a/b/c` and `--sat`          |
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
When `--palette N` is non-zero it overrides `--hue-a`, `--hue-b`, `--hue-c`,
and `--sat`.

**Nature & elements**

| # | Name          | Primaries                                    | Sat |
|---|---------------|----------------------------------------------|-----|
| 1 | pastels       | Soft pink (340°), lavender (270°), mint (150°) | 45% |
| 2 | earth         | Amber (35°), terracotta (18°), sage (130°)   | 70% |
| 3 | water         | Ocean blue (220°), cyan (185°), seafoam (165°) | 85% |
| 4 | fire          | Red (0°), orange (30°), amber (55°)          | 95% |
| 6 | marble        | Blue-gray (210°), warm-white (42°), cool-white (185°) | 12% |
| 8 | aurora        | Emerald (145°), teal (178°), deep purple (268°) | 85% |
|10 | forest        | Deep green (130°), moss (95°), bark brown (28°) | 72% |
|11 | deep sea      | Midnight blue (232°), bioluminescent teal (172°), violet (262°) | 90% |
|39 | tropical      | Hot teal (175°), coral (15°), sunny yellow (55°) | 90% |
|40 | coral reef    | Coral orange (18°), teal (175°), deep blue (230°) | 88% |
|41 | galaxy        | Deep violet (268°), midnight blue (235°), pale blue (215°) | 78% |
|42 | autumn        | Burnt orange (22°), burgundy (355°), golden yellow (48°) | 85% |
|43 | winter        | Ice blue (205°), silver (215°), pale lavender (270°) | 35% |
|44 | desert        | Sand (45°), rust orange (18°), warm brown (28°) | 68% |
|45 | arctic        | Pale blue (200°), ice white (210°), steel gray (215°) | 20% |

*Marble (6) at 12% saturation looks nearly white; the CA's alive/dead spatial
structure creates the veining.  Arctic (45) at 20% reads as frost breath
patterns.*

**Artists**

| #  | Name          | Primaries                                    | Sat | Inspiration                              |
|----|---------------|----------------------------------------------|-----|------------------------------------------|
|  5 | van gogh      | Cobalt blue (225°), warm gold (48°), ice blue (195°) | 88% | *Starry Night* — swirling contrast       |
|  7 | sunset        | Warm orange (20°), deep magenta (330°), soft violet (275°) | 88% | Turner atmospheric skies |
| 13 | monet         | Soft lilac (280°), water green (160°), dusty rose (340°) | 55% | *Water Lilies* — hazy impressionism      |
| 14 | klimt         | Deep gold (45°), teal (175°), burgundy (350°) | 85% | *The Kiss* — opulent gilded contrasts    |
| 15 | rothko        | Deep crimson (355°), burnt sienna (22°), muted orange (32°) | 82% | Colour field — warm moody cluster |
| 16 | hokusai       | Deep navy (225°), slate blue (210°), pale blue-gray (200°) | 80% | *The Great Wave* — layered ocean blues |
| 17 | turner        | Golden amber (42°), hazy orange (28°), pale sky blue (200°) | 75% | Luminous atmospheric haze               |
| 18 | mondrian      | Red (5°), cobalt blue (230°), golden yellow (52°) | 100% | Primary colour grid — bold, uncompromising |
| 19 | warhol        | Hot pink (330°), lime green (88°), turquoise (178°) | 100% | Pop art — saturated, flat, electric    |
| 20 | rembrandt     | Warm umber (28°), antique gold (44°), dark amber (35°) | 78% | Chiaroscuro — all warm, all depth      |

*Mondrian (18) and Warhol (19) run at full saturation; `--brightness 60`
tones them down if the output is too intense.  Rothko (15) keeps all three
primaries within a 37° warm arc — CIELAB blends between them stay emotionally
consistent rather than going muddy.*

**Holidays**

| #  | Name          | Primaries                                    | Sat |
|----|---------------|----------------------------------------------|-----|
| 21 | christmas     | Red (5°), deep green (125°), gold (48°)      | 92% |
| 22 | halloween     | Orange (25°), deep purple (270°), yellow (58°) | 95% |
| 23 | hanukkah      | Royal blue (228°), sky blue (205°), gold (48°) | 80% |
| 24 | valentines    | Rose red (355°), hot pink (340°), blush (15°) | 80% |
| 25 | easter        | Soft purple (280°), pale yellow (60°), light green (140°) | 45% |
| 26 | independence  | Red (5°), white-blue (218°), blue (238°)     | 90% |
| 27 | st patricks   | Shamrock green (130°), gold (50°), light green (145°) | 85% |
| 28 | thanksgiving  | Burnt orange (22°), warm brown (30°), deep gold (45°) | 80% |
| 29 | new year      | Champagne gold (48°), silver (218°), midnight blue (240°) | 65% |
| 30 | mardi gras    | Deep purple (270°), gold (50°), green (130°) | 90% |
| 31 | diwali        | Deep gold (45°), magenta (310°), saffron (30°) | 90% |

**School colors**

| #  | School        | Primaries                                    | Sat |
|----|---------------|----------------------------------------------|-----|
| 32 | michigan      | Maize (50°), cobalt blue (230°), sky blue (210°) | 88% |
| 33 | alabama       | Crimson (350°), silver (215°), gold (48°)    | 82% |
| 34 | lsu           | Purple (270°), gold (48°), pale gold (52°)   | 90% |
| 35 | texas         | Burnt orange (22°), warm brown (28°), gold (45°) | 80% |
| 36 | ohio state    | Scarlet (5°), silver (215°), gold (48°)      | 82% |
| 37 | notre dame    | Gold (48°), navy (225°), green (130°)        | 88% |
| 38 | ucla          | Blue (228°), gold (50°), sky blue (210°)     | 85% |

**Moods & aesthetics**

| #  | Name          | Primaries                                    | Sat | Character                                |
|----|---------------|----------------------------------------------|-----|------------------------------------------|
|  9 | neon          | Hot pink (310°), electric cyan (183°), acid green (90°) | 100% | Club lighting — maximum intensity |
| 12 | cherry blossom | Pale pink (348°), blush (15°), soft lavender (290°) | 38% | Gentle, floral, Japanese spring |
| 46 | vaporwave     | Hot pink (310°), purple (270°), electric cyan (185°) | 95% | Retrowave — 80s neon nostalgia          |
| 47 | cyberpunk     | Neon green (130°), electric blue (225°), magenta (300°) | 100% | High-contrast dystopian city lights  |
| 48 | cottagecore   | Sage green (130°), blush pink (350°), warm cream (45°) | 48% | Soft, domestic, garden-morning light  |
| 49 | gothic        | Deep burgundy (350°), deep purple (270°), dark rose (340°) | 78% | Brooding — all hues cluster near red |
| 50 | lo-fi         | Warm amber (35°), dusty rose (348°), muted sage (130°) | 52% | Relaxed, cozy, late-night study vibes |

#### Examples

```bash
# Water palette — default settings
python3 glowup.py play rule_trio --ip <device-ip> --palette 3

# Van Gogh with Rule 90 (fractal) on CA A for structured cobalt regions
python3 glowup.py play rule_trio --ip <device-ip> --palette 5 --rule-a 90

# Halloween at high speed — frantic orange-purple chaos
python3 glowup.py play rule_trio --ip <device-ip> --palette 22 --speed 20

# Mardi Gras with wider gap fill (2-bulb radius)
python3 glowup.py play rule_trio --ip <device-ip> --palette 30 --gap-fill 6

# Hokusai wave — all three CAs on Rule 90 for deep fractal blue layering
python3 glowup.py play rule_trio --ip <device-ip> --palette 16 --rule-a 90 --rule-b 90 --rule-c 90

# Rothko — slow drift, very moody
python3 glowup.py play rule_trio --ip <device-ip> --palette 15 --speed 3 --drift-b 1.1 --drift-c 1.2

# Mondrian pop art — dial brightness down from the default 90
python3 glowup.py play rule_trio --ip <device-ip> --palette 18 --brightness 60

# Custom palette: coral, turquoise, gold
python3 glowup.py play rule_trio --ip <device-ip> --hue-a 15 --hue-b 178 --hue-c 48 --sat 85

# Michigan colors for game day
python3 glowup.py play rule_trio --ip <device-ip> --palette 32 --speed 6

# Gothic — slow, dim background glow, maximum menace
python3 glowup.py play rule_trio --ip <device-ip> --palette 49 --speed 4 --bg 3
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

## Effect Developer Guide

### Architecture Overview

```
glowup.py            CLI — argparse, dispatches to subcommand handlers
    │
    ├── transport.py     LIFX LAN protocol: discovery, LifxDevice, UDP sockets
    ├── engine.py        Engine (threaded frame loop) + Controller (thread-safe API)
    ├── simulator.py     Live tkinter preview window (optional, graceful fallback)
    ├── solar.py         Sunrise/sunset calculator (NOAA algorithm, no dependencies)
    ├── scheduler.py     Daemon: named device groups, symbolic time scheduling
    │
    ├── test_virtual_multizone.py  Tests for VirtualMultizoneDevice (mock-based)
    │
    └── effects/
        ├── __init__.py   Effect base class, Param system, auto-registry, utilities
        ├── cylon.py      Individual effect implementations
        ├── breathe.py
        ├── wave.py
        ├── twinkle.py
        ├── morse.py
        ├── aurora.py
        ├── binclock.py
        ├── flag.py         Waving flag with Perlin noise perspective projection
        └── flag_data.py    199-country flag color database
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

The difference was validated empirically using the `crossfade` diagnostic
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

---

## Live Simulator

The `--sim` flag on the `play` command opens a tkinter window that
displays the effect output in real-time as colored rectangles — one per
zone.  This lets you preview effects without physical hardware, or watch
what the engine is sending alongside real devices.

```bash
# Preview cylon on your lights and in the simulator window
python3 glowup.py play cylon --ip 10.0.0.62 --sim

# Show 36 bulbs instead of 108 zones (LIFX strings have 3 zones per bulb)
python3 glowup.py play cylon --ip 10.0.0.62 --sim --zpb 3

# Works with virtual multizone groups too
python3 glowup.py play aurora --config schedule.json --group office --sim
```

The simulator window shows:

- **Zone strip** — a row of colored rectangles, one per zone (or per
  bulb when `--zpb` is set), updated every frame with true RGB color
  converted from the LIFX HSBK values.  Monochrome (non-polychrome)
  zones are rendered in grayscale using BT.709 luma weighting, matching
  what the physical bulbs actually display.
- **Header** — the effect name and zone count.
- **FPS counter** — the actual display refresh rate (smoothed over 10
  frames).

**Closing the window** triggers the same clean shutdown as Ctrl+C — the
effect fades to black and devices are powered off.

### How It Works

The engine renders frames in a background thread.  After dispatching
colors to devices, it calls an optional `frame_callback` with the
rendered color list.  The simulator puts frame data onto a
`queue.Queue` (thread-safe), and the tkinter event loop on the main
thread polls that queue via `root.after()` to update the display.
This satisfies the macOS requirement that all tkinter calls happen on
the main thread.

### Graceful Fallback

If tkinter is not available (missing the `_tkinter` C extension), the
`--sim` flag prints a note and continues without the window.  The rest
of the system is completely unaffected.  To install tkinter on macOS
with Homebrew Python:

```bash
brew install tcl-tk python-tk@3.10
```

### Zones Per Bulb (`--zpb`)

LIFX string lights use 3 zones per physical bulb (108 zones = 36 bulbs).
By default, the simulator shows one rectangle per zone.  Use `--zpb 3`
to group zones into bulbs — the display shows the middle zone's color
for each group, matching the visual appearance of the physical string.

### Zoom (`--zoom`)

The `--zoom` flag scales all simulator dimensions by an integer factor
(1–10). Zone widths, heights, padding, and header font size are all
multiplied, producing a proportionally larger window with sharp pixel
edges (nearest-neighbor scaling, no interpolation blur).

```bash
# Double-size simulator window
python3 glowup.py play aurora --ip 10.0.0.62 --sim --zoom 2

# Monitor mode also supports zoom
python3 glowup.py monitor --ip 10.0.0.62 --zoom 3
```

Useful for presentations, demos, and high-DPI displays where the
default window is too small to read comfortably.

### Adaptive Sizing

Zone widths automatically shrink for large zone counts so the window
fits on screen (capped at 1600px).  A 108-zone string light fits
comfortably; a 200-zone virtual group will use narrower rectangles.
Using `--zpb` reduces the rectangle count, producing wider, more
readable bulbs.

### Monitor Mode

The `monitor` subcommand turns the simulator into a read-only live
display of a real device's current zone colors.  Unlike `play --sim`
(which shows what the engine is *sending*), `monitor` queries the
device for its *actual* state — whatever is driving the lights (the
scheduler on a Pi, the LIFX phone app, or any other controller).

```bash
# Monitor a string light at 4 Hz (default)
python3 glowup.py monitor --ip 10.0.0.62 --zpb 3

# Higher polling rate for smoother updates
python3 glowup.py monitor --ip 10.0.0.62 --zpb 3 --hz 10
```

| Flag    | Default | Description                                      |
|---------|---------|--------------------------------------------------|
| `--ip`  | —       | Device IP address (required)                     |
| `--hz`  | 4.0     | Polling rate in Hz (0.5–20.0)                    |
| `--zpb` | 1       | Zones per bulb (3 for LIFX string lights)        |

Monitor mode is completely passive — it only reads the device state
and never sends color or power commands.  You can safely run it from
any machine on the LAN while the scheduler drives the lights from
another.

### Sim-Only Mode

The `--sim-only` flag queries the real device (or group) to discover its
zone count and polychrome capabilities, then immediately closes the
connection.  From that point on, **no packets are sent to the lights**.
The effect runs entirely inside the simulator window.

This is useful when you want to:

- Preview a new or modified effect without disturbing lights that are
  already in use (e.g., the scheduler is running).
- Tune parameters in advance and decide on values before deploying.
- Develop effects on a machine that is not on the same LAN as the lights.

```bash
# Preview fireworks on your string light without touching it
python3 glowup.py play fireworks --ip <device-ip> --sim-only

# Preview across a virtual multizone group, 3 zones per bulb display
python3 glowup.py play aurora --config schedule.json --group porch --sim-only --zpb 3

# Tune parameters first, then deploy for real
python3 glowup.py play cylon --ip <device-ip> --sim-only --speed 1.5 --width 8
```

The simulator title bar shows the effect name and zone count.  All
effect parameters work identically to normal `play` mode.

`--sim-only` requires tkinter.  If it is not available, the command
exits with an error rather than silently doing nothing.

`--sim-only` and `--sim` are mutually exclusive — `--sim-only` implies
the simulator; adding `--sim` is redundant but harmless.

### macOS Accessibility Permission

On macOS, the simulator window uses `osascript` to ask System Events
to bring the Python process to the foreground.  The first time you
run any simulator mode (`--sim`, `--sim-only`, or `monitor`), macOS will prompt you
to grant **Accessibility** permission to your terminal application
(Terminal, iTerm2, VS Code, etc.).  This is a standard macOS security
gate for any program that activates another process's window.  The
permission is granted once and remembered — subsequent launches will
not prompt again.

If you prefer not to grant the permission, simply dismiss the dialog.
The simulator will still work; the window just won't automatically
come to the front on launch.

---

## Engine and Controller API

The `Controller` class in `engine.py` is the thread-safe public interface
for controlling the effect engine. It is designed to be driven by the CLI
today and a REST API in the future.

### Controller Methods

```python
from transport import LifxDevice
from engine import Controller

# Create a controller with one or more devices
device = LifxDevice("<device-ip>")
device.query_all()
ctrl = Controller([device], fps=20)
```

**`play(effect_name: str, **params) -> None`**
Start an effect by its registered name. Any keyword arguments override
the effect's default parameters.

```python
ctrl.play("cylon", speed=1.5, width=12, hue=0)
```

**`stop(fade_ms: int = 500) -> None`**
Stop the current effect and fade to black. Pass `fade_ms=0` to skip
the fade.

```python
ctrl.stop(fade_ms=1000)  # 1-second fade out
```

**`update_params(**kwargs) -> None`**
Update parameters on the running effect without restarting it. Unknown
parameter names are silently ignored.

```python
ctrl.update_params(speed=3.0, hue=240)
```

**`get_status() -> dict`**
Returns the current engine state as a JSON-serializable dict:

```python
{
    "running": True,
    "effect": "cylon",
    "params": {"speed": 1.5, "width": 12, "hue": 0.0, ...},
    "fps": 20,
    "devices": [
        {"ip": "<device-ip>", "mac": "aa:bb:cc:dd:ee:ff",
         "label": "My Light", "product": "String Light", "zones": 108}
    ]
}
```

**`list_effects() -> dict`**
Returns all registered effects with parameter metadata:

```python
{
    "cylon": {
        "description": "Larson scanner — a bright eye sweeps back and forth",
        "params": {
            "speed": {"default": 2.0, "min": 0.2, "max": 30.0,
                      "description": "Seconds per full sweep", "type": "float"},
            ...
        }
    },
    ...
}
```

### VirtualMultizoneDevice

The `VirtualMultizoneDevice` class in `engine.py` wraps any combination of
LIFX devices into a single virtual multizone device.  Multizone devices
contribute all their physical zones; single bulbs contribute one zone each.

```python
from transport import LifxDevice
from engine import VirtualMultizoneDevice, Controller

# Connect devices of any type
string_light = LifxDevice("10.0.0.62")  # 108-zone multizone
white_bulb_1 = LifxDevice("10.0.0.25")  # monochrome single
color_bulb_1 = LifxDevice("10.0.0.30")  # color single

for dev in [string_light, white_bulb_1, color_bulb_1]:
    dev.query_all()

# Wrap them — total zone count = 108 + 1 + 1 = 110
vdev = VirtualMultizoneDevice([string_light, white_bulb_1, color_bulb_1])
print(vdev.zone_count)  # 110

# Use exactly like a regular device
ctrl = Controller([vdev], fps=20)
ctrl.play("cylon", speed=3.0)
```

**How dispatch works:**

The constructor builds a zone map — a list of `(device, zone_index)` tuples.
When `set_zones()` is called with the rendered colors:

- **Multizone device zones** are accumulated into a per-device batch, then
  flushed with a single `set_zones()` call (efficient 2-packet extended
  multizone protocol, same as direct use).
- **Single color bulbs** receive `set_color()` with full HSBK.
- **Monochrome bulbs** receive `set_color()` with BT.709 luma-converted
  brightness (hue and saturation are converted to perceptual brightness).

The class duck-types the `LifxDevice` interface, so the `Engine`,
`Controller`, and all effects work without modification.

### LifxDevice Key Methods

```python
from transport import LifxDevice, discover_devices

# Discovery
devices = discover_devices(timeout=3.0)

# Direct connection
dev = LifxDevice("<device-ip>")
dev.query_all()          # Populates label, product, group, zone_count

# Properties
dev.label                # "My Light"
dev.product_name         # "String Light"
dev.zone_count           # 108
dev.mac_str              # "aa:bb:cc:dd:ee:ff"
dev.is_multizone         # True for string lights, beams, Z strips
dev.is_polychrome        # True for color devices, False for monochrome

# Zone control (multizone devices)
colors = [(hue, sat, bri, kelvin)] * dev.zone_count
dev.set_zones(colors, duration_ms=0, rapid=True)

# Single color (non-multizone)
dev.set_color(hue, sat, bri, kelvin, duration_ms=0)

# Power
dev.set_power(on=True, duration_ms=1000)

# Cleanup
dev.close()
```

---

## Testing

### VirtualMultizoneDevice Tests

The file `test_virtual_multizone.py` contains mock-based tests that verify
the `VirtualMultizoneDevice` zone mapping and dispatch logic without
requiring physical LIFX hardware.

```bash
python3 test_virtual_multizone.py
```

**What it tests:**

| Test | Description |
|------|-------------|
| Zone map expansion | A 6-zone multizone + color bulb + mono bulb = 8 virtual zones |
| set_zones dispatch | Multizone gets 1 batched `set_zones()`, color bulb gets `set_color()`, mono gets BT.709 luma |
| set_color broadcast | Fade-to-black `set_color()` reaches all devices |
| set_power broadcast | `set_power()` reaches all devices |
| Two multizone devices | Two strips (4+3) batch independently into separate `set_zones()` calls |
| All singles regression | Pure single-bulb group still works (original use case) |

The tests use `MockDevice` objects that record all method calls for
assertion.  No sockets are opened and no network traffic is generated.

To add new tests, follow the same pattern: create `MockDevice` instances
with the desired `zone_count`, `is_multizone`, and `is_polychrome` values,
build a `VirtualMultizoneDevice`, call methods, and assert against the
recorded calls.

---

## REST API Server

The REST API server (`server.py`) exposes all GlowUp functionality over
HTTP, enabling remote control from the iOS app or any HTTP client.  It
replaces `scheduler.py` by managing effects directly through the
`Controller` API instead of spawning subprocesses.

```bash
python3 server.py server.json              # start the server
python3 server.py --dry-run server.json    # preview resolved schedule
```

### Server Configuration

The server reads a JSON configuration file that combines server settings
with the same schedule format used by `scheduler.py`:

```json
{
    "port": 8420,
    "auth_token": "your-secret-token-here",
    "location": {
        "latitude": 43.07,
        "longitude": -89.40
    },
    "groups": {
        "porch": ["10.0.0.62"]
    },
    "nicknames": {
        "10.0.0.62": "Porch Lights"
    },
    "schedule": [
        {
            "name": "evening aurora",
            "group": "porch",
            "start": "sunset-30m",
            "stop": "23:00",
            "effect": "aurora",
            "params": {"speed": 10.0, "brightness": 60}
        },
        {
            "name": "weekend cylon",
            "days": "SU",
            "group": "porch",
            "start": "sunset",
            "stop": "23:00",
            "effect": "cylon",
            "params": {"speed": 4.0}
        }
    ]
}
```

The `nicknames` section maps device IPs to custom display names shown
in the iPhone app.  Nicknames can also be set from the app itself
(swipe left on a device row) and are persisted back to this file.

Generate a secure token:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

The `groups` and `schedule` sections are optional — the server works in
API-only mode without them.

### API Endpoints

All endpoints require a bearer token in the `Authorization` header:

```
Authorization: Bearer your-secret-token-here
```

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/devices` | List all discovered devices |
| `GET` | `/api/effects` | List effects with full parameter metadata |
| `GET` | `/api/devices/{ip}/status` | Current effect name, params, FPS |
| `GET` | `/api/devices/{ip}/colors` | Snapshot of zone HSBK values |
| `GET` | `/api/devices/{ip}/colors/stream` | SSE stream of zone colors at 4 Hz |
| `POST` | `/api/devices/{ip}/play` | Start an effect (body: `{"effect":"name","params":{...}}`) |
| `POST` | `/api/devices/{ip}/stop` | Stop current effect (fade to black) |
| `POST` | `/api/devices/{ip}/power` | Power on/off (body: `{"on": true}`) |
| `POST` | `/api/discover` | Re-run device discovery |

**Examples:**

```bash
TOKEN="your-token"
BASE="http://localhost:8420"

# List devices
curl -H "Authorization: Bearer $TOKEN" $BASE/api/devices

# Play an effect
curl -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"effect":"cylon","params":{"speed":2.0,"hue":120}}' \
     $BASE/api/devices/10.0.0.62/play

# Stop the effect
curl -X POST -H "Authorization: Bearer $TOKEN" \
     $BASE/api/devices/10.0.0.62/stop
```

### Authentication

Every request must include a valid bearer token.  The token is compared
using `hmac.compare_digest()` for timing-safe validation.  Invalid or
missing tokens receive a `401 Unauthorized` response.

### Server-Sent Events (Live Colors)

The `/api/devices/{ip}/colors/stream` endpoint opens a long-lived HTTP
connection that pushes zone color updates at 4 Hz using the Server-Sent
Events protocol:

```
data: {"zones": [{"h": 0, "s": 65535, "b": 32768, "k": 3500}, ...]}

data: {"zones": [{"h": 100, "s": 65535, "b": 32768, "k": 3500}, ...]}
```

The stream creates a separate read-only device connection to avoid
socket contention with the engine's animation loop (the same pattern
used by monitor mode).

### Phone Override Behavior

When the phone app sends a `play` command, the server marks the device
as "overridden" so the scheduler skips it.  The override persists until
the next schedule transition, at which point the scheduler resumes
control automatically.

Sending `stop` from the phone keeps the override active (the device
stays dark) until the next schedule transition.

### Installing the Server as a systemd Service

```bash
sudo cp glowup-server.service /etc/systemd/system/
sudo cp server.json /etc/glowup/server.json
sudo systemctl daemon-reload
sudo systemctl enable glowup-server
sudo systemctl start glowup-server
```

If migrating from `scheduler.py`, disable the old service first:

```bash
sudo systemctl stop glowup-scheduler
sudo systemctl disable glowup-scheduler
```

---

## GlowUp iOS App

The GlowUp iOS app is a native SwiftUI remote control for your LIFX
devices.  It communicates with `server.py` over HTTP(S) and provides
live color monitoring, auto-generated parameter UI, and
Keychain-secured authentication.

### Connectivity Options

The app connects to a running `server.py` instance.  There are
several ways to make this work depending on your setup:

| Method | Setup | Use Case |
|--------|-------|----------|
| **LAN (direct IP)** | Point the app at `http://<pi-ip>:8420` | Controlling lights from home — no tunnel, no account, simplest setup |
| **Cloudflare Tunnel** | See [TUNNEL.md](TUNNEL.md) | Secure remote access from anywhere without opening router ports |
| **Tailscale / WireGuard** | Install on Pi and phone | Private VPN mesh — works from anywhere, free for personal use |
| **Port forwarding** | Forward 8420 on your router | Works remotely but exposes a port to the internet |

For most users, **LAN mode is all you need** — your phone and the Pi
are on the same WiFi, so just enter the Pi's local IP address as the
server URL in the app's Settings screen.

### Building the App

**Requirements:**

- macOS with Xcode 16+ installed
- Apple ID signed into Xcode (free tier works for simulator testing)
- For deploying to a physical iPhone: a free Apple ID is sufficient for
  7-day provisioning profiles; a $99/yr Apple Developer account removes
  that expiration

**Steps:**

1. Open the project:
   ```bash
   open ios/GlowUp.xcodeproj
   ```
2. In Xcode, select the **GlowUp** target, go to **Signing &
   Capabilities**, check **Automatically manage signing**, and select
   your Apple ID team
3. If the bundle identifier `com.kivolowitz.glowup` is taken under your
   team, change it to something unique (e.g.,
   `com.yourname.glowup`)
4. Select an iPhone simulator or your connected device as the run
   destination
5. Build and run (**Cmd+R**)

### Running on Your iPhone

To install on a physical device for the first time:

1. Connect your iPhone to your Mac via USB
2. On the phone, tap **Trust This Computer** when prompted
3. On the phone, enable **Developer Mode**: Settings → Privacy &
   Security → Developer Mode → toggle on and restart
4. In Xcode, select your iPhone from the run destination dropdown (top
   toolbar, next to the Play button)
5. Build and run (**Cmd+R**) — Xcode will automatically create a
   provisioning profile
6. On first launch, you may need to trust the developer certificate on
   the phone: **Settings → General → VPN & Device Management** → tap
   your developer certificate → Trust

After the first wired install, you can enable wireless debugging in
Xcode: **Window → Devices and Simulators**, select your phone, and
check **Connect via network**.

### App Screens

1. **Device List** — Shows all discovered devices with name, product
   type, group, and current effect.  Pull-to-refresh fetches the
   latest state.

2. **Device Detail** — Live color strip visualization (SSE-fed at 4 Hz),
   current effect info, power toggle, stop button, restart button, and
   a link to change the effect.

3. **Effect Picker** — Lists all registered effects with descriptions
   and parameter counts.

4. **Effect Config** — Auto-generated parameter UI built from the
   server's `Param` metadata.  Sliders for numeric params, pickers
   for choice params, text fields for strings.  Tap "Play" to send
   the command.

5. **Settings** — Server URL and API token configuration.  Token is
   stored in the iOS Keychain.  Includes a "Test Connection" button
   and an About section displaying the app icon, version, and license
   information.

---

## Cloudflare Tunnel (Remote Access)

Cloudflare Tunnel creates an outbound-only encrypted connection from
the Pi to Cloudflare's edge network.  The phone connects to
`https://lights.yourdomain.com` — no ports are opened on the router
and no dynamic DNS is needed.

See [TUNNEL.md](TUNNEL.md) for complete setup instructions.
