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
   - [record](#record)
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
   - [embers](#embers)
   - [jacobs_ladder](#jacobs_ladder)
   - [spin](#spin)
   - [sonar](#sonar)
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
13. [Effect Gallery](#effect-gallery)
14. [Troubleshooting](#troubleshooting)
15. [Cloudflare Tunnel (Remote Access)](#cloudflare-tunnel-remote-access)
16. [Home Assistant Integration](#home-assistant-integration)
17. [Apple Shortcuts Integration](#apple-shortcuts-integration)
18. [Node-RED Integration](#node-red-integration)
19. [MQTT Integration](#mqtt-integration)

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

- **Python 3.10+**
- One or more LIFX devices on the same LAN subnet (multizone, single color, or monochrome)
- No external Python packages — the entire stack is pure Python stdlib
- **Optional:** [ffmpeg](https://ffmpeg.org/) for the `record` subcommand (rendering effects to GIF/MP4/WebM)

### Platform Support

| Platform | Status | Notes |
|----------|--------|-------|
| **macOS** | Fully supported | Primary development platform. Broadcast auto-detection via `ifconfig`, simulator window focus via `osascript`. |
| **Linux (Raspberry Pi, Ubuntu, etc.)** | Fully supported | Broadcast auto-detection via `ioctl`. Recommended deployment target. |
| **Windows** | Degraded (untested) | Broadcast discovery is unavailable — use `--ip` to address devices directly. Effects, simulator, and server should work. See [Windows notes](#windows) below. |

### Platform-Specific Setup

**macOS** — Python 3.10+ from Homebrew, the Xcode command-line tools,
or a conda environment.  tkinter ships with the standard Python
distribution on macOS:

```bash
# Homebrew
brew install python@3.12

# For the record subcommand (optional)
brew install ffmpeg

# Or conda
conda create -n glowup python=3.12
conda activate glowup
```

**Linux (Debian / Ubuntu / Raspberry Pi OS)** — install Python and
tkinter (needed only for the `--sim` live preview):

```bash
sudo apt update
sudo apt install python3 python3-tk

# For the record subcommand (optional)
sudo apt install ffmpeg
```

On Raspberry Pi OS (Bookworm), Python 3.11+ is included by default.
Install tkinter only if you plan to use the simulator on a desktop —
headless Pi deployments (server, scheduler) do not need it.

#### Windows

> **Windows support has not been tested.**  The guidance below is based
> on code analysis, not hands-on verification.  If you try it, we would
> appreciate a report — good or bad — via a GitHub issue.

GlowUp should run on Windows with one limitation: the `discover`
command cannot auto-detect your subnet's broadcast address (this
requires Unix-specific `fcntl`/ioctl calls).  Everything else should
work — effects, the simulator, and the server.

Install Python 3.10+ from [python.org](https://www.python.org/downloads/)
(tkinter is included by default on Windows).

To work around the discovery limitation, find your device IPs using the
official LIFX app or your router's DHCP lease table, then address
devices directly:

```bash
# Play an effect by IP (no broadcast discovery needed)
python glowup.py play aurora --ip 10.0.0.62

# Simulator-only mode works with no devices at all
python glowup.py play aurora --ip 10.0.0.62 --sim-only

# Server mode — list device IPs in server.json
python server.py server.json
```

> **Note:** `discover` may still work on simple single-subnet networks
> because the fallback broadcast address `255.255.255.255` is used
> automatically.  Results vary by network configuration.

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

### record

Render an effect headlessly to GIF, MP4, or WebM via ffmpeg.  No device
or network connection needed — the effect is rendered at deterministic
timestamps and piped as raw RGB frames to ffmpeg.

```bash
python3 glowup.py record <effect> [--duration N] [--format gif|mp4|webm] [--output file] [params...]
```

| Option         | Default | Description                                           |
|----------------|---------|-------------------------------------------------------|
| `--zones`      | 108     | Number of zones to simulate                           |
| `--zpb`        | 3       | Zones per bulb (groups zones into displayed bulbs)    |
| `--fps`        | 20      | Frames per second                                     |
| `--duration`   | *(auto)* | Recording duration in seconds (see below)            |
| `--width`      | 600     | Output width in pixels                                |
| `--height`     | 80      | Output height in pixels                               |
| `--format`     | gif     | Output format: `gif`, `mp4`, or `webm`               |
| `--output`     | *(auto)* | Output file path (default: `<effect>.<format>`)      |
| `--lerp`       | lab     | Color interpolation: `lab` or `hsb`                  |
| `--author`     | *(none)* | Author name for the metadata sidecar                 |
| `--title`      | *(none)* | Title / description for the metadata sidecar         |
| `--media-url`  | *(auto)* | Relative URL for gallery use (defaults to filename)  |
| `--realtime`   | off      | Sleep between frames so wall-clock effects (e.g. binclock) animate correctly. Recording takes real time. |

**Seamless looping** — If no `--duration` is specified and the effect has
a known period (e.g. `speed = 3.0` seconds), exactly one cycle is recorded
so the GIF loops seamlessly.  Aperiodic effects (fireworks, twinkle,
rule30, etc.) default to 5 seconds.

**JSON metadata sidecar** — Every recording produces a companion `.json`
file alongside the output containing:

- Effect name, description, and all parameter values
- Recording dimensions, duration, FPS, format, and looping flag
- A ready-to-paste CLI command that reproduces the effect on live hardware
- Optional author, title, and media_url fields for gallery integration

**Examples:**

```bash
# Record one seamless cycle of cylon (auto-detects 2s period)
python3 glowup.py record cylon

# 10-second fireworks in MP4 format
python3 glowup.py record fireworks --duration 10 --format mp4

# Custom parameters — fast red cylon with wide trail
python3 glowup.py record cylon --speed 1.0 --hue 0 --trail 0.8

# Gallery-ready recording with metadata
python3 glowup.py record aurora --duration 8 \
    --output docs/assets/previews/aurora.gif \
    --media-url assets/previews/aurora.gif \
    --author "Perry" --title "Aurora Borealis"
```

The help system works the same as `play`:

```bash
python3 glowup.py record --help              # record options
python3 glowup.py record fireworks --help     # effect parameters
```

**Requires:** [ffmpeg](https://ffmpeg.org/) must be installed and on your PATH.

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

Groups with two or more devices are automatically combined into a
**virtual multizone device** — a single unified zone canvas spanning
all member devices.  Effects render across the combined zone count
as if all the lights were one long strip.

> **IP order matters:** The order of IPs in the group array determines
> the left-to-right zone layout on the virtual canvas.  The first IP's
> zones come first (leftmost), the second IP's zones follow, and so on.
> If an animation runs in the wrong direction, swap the IPs in the
> array and restart the server.

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

---

## Effect Developer Guide

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

GlowUp includes a comprehensive test suite that validates the core engine
without requiring physical LIFX hardware or network access.  All tests
use mock objects, temporary files, or pure math — no sockets are opened.

### Running the Full Suite

```bash
# Run every test file at once (stop on first failure):
for f in test_*.py; do python3 -m unittest "$f" || exit 1; done

# Or run a specific module:
python3 -m unittest test_effects -v
```

### Test Modules

| Module | Tests | What it covers |
|--------|------:|----------------|
| `test_effects.py` | 73 | Every registered effect × {1, 3, 36, 108} zones — frame length, HSBK range (0–65535 for H/S/B, 1500–9000 K), 50-frame stability for stateful effects, registry sanity |
| `test_schedule.py` | 28 | `_parse_time_spec` (fixed times, symbolic solar times, offsets), `_validate_days`, `_days_display`, `_resolve_entries` (overnight wraparound, group filtering), `_find_active_entry` |
| `test_config.py` | 22 | `_load_config` validation: auth tokens (missing, default, empty, non-string), ports (zero, negative, >65535, string), groups (missing, empty), schedule entries (missing fields, unknown groups, invalid days), MQTT section (bad port, empty prefix, negative interval), file errors |
| `test_override.py` | 18 | DeviceManager override logic: basic set/clear, group-level overrides, individual member overrides within groups (`is_overridden_or_member`), override entry tracking, clear-and-resume |
| `test_solar.py` | 12 | `sun_times()` for Mobile AL, NYC, Tromso (polar night + midnight sun), Quito (equator): event ordering, day length, timezone awareness, noon always present, latitude validation |
| `test_virtual_multizone.py` | 6 | `VirtualMultizoneDevice` zone mapping and dispatch: multizone + color + mono mix, batched `set_zones()`, `set_color` broadcast, two-strip independent batching, all-singles regression |
| `test_multizone_products.py` | varies | LIFX product database: product IDs, zone counts, multizone detection |

### VirtualMultizoneDevice Tests (Detail)

The tests in `test_virtual_multizone.py` use `MockDevice` objects that
record all method calls for assertion.  To add new tests, follow the same
pattern: create `MockDevice` instances with the desired `zone_count`,
`is_multizone`, and `is_polychrome` values, build a
`VirtualMultizoneDevice`, call methods, and assert against the recorded
calls.

### Effect Rendering Tests (Detail)

`test_effects.py` dynamically generates a test method for every registered
effect at four zone counts (1, 3, 36, 108 zones).  Each test calls
`on_start()` then `render()` at t=0, 1, and 10 seconds, validating:

- Frame length matches the requested zone count
- Every HSBK tuple has exactly 4 components
- H, S, B values are in [0, 65535]
- Kelvin values are in [1500, 9000]

A separate stability test renders 50 consecutive frames at 20 fps for
every effect to catch late-onset crashes in stateful effects like
fireworks, rule30, and newtons_cradle.

### Override Tests (Detail)

`test_override.py` covers the group-member override bug fixed in 8a27b45.
When a user overrides an individual device (e.g., 10.0.0.62) that belongs
to a group (e.g., group:porch), the scheduler must recognize the conflict
and skip the group.  The `is_overridden_or_member()` method checks both
the group ID and every member IP.  Tests verify:

- Overriding one member makes the group check return True
- Overriding a non-member does not affect the group
- Clearing the member override restores normal scheduling
- Both group and member overridden simultaneously works correctly

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

The `groups` section is **required** — it is the server's only source of
device IPs.  The server does not perform broadcast discovery; instead it
queries each configured IP directly at startup.  This is both faster and
more reliable than broadcast discovery, which requires multiple retries
with long timeouts and is defeated by mesh routers that filter broadcast
packets between nodes.

Groups with two or more devices automatically appear as a virtual
multizone device in the API and the iOS app, identified by
`group:<name>` (e.g., `group:porch`).  The individual member devices
also appear for independent control.  The scheduler plays effects on
the virtual device so the animation spans all member devices as a
unified canvas.

The `schedule` section is optional — the server works in API-only mode
without it.

> **Tip:** Because the server relies on IP addresses to reach each
> device, LIFX bulbs should be given **static IP addresses** or
> **DHCP address reservations** in your router.  If a device's IP
> changes (e.g. after a power outage or DHCP lease renewal), the
> server will no longer be able to reach it at the configured address.

### API Endpoints

All endpoints require a bearer token in the `Authorization` header:

```
Authorization: Bearer your-secret-token-here
```

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | Server readiness and version |
| `GET` | `/api/devices` | List all configured devices |
| `GET` | `/api/effects` | List effects with full parameter metadata |
| `GET` | `/api/devices/{ip}/status` | Current effect name, params, FPS |
| `GET` | `/api/devices/{ip}/colors` | Snapshot of zone HSBK values |
| `GET` | `/api/devices/{ip}/colors/stream` | SSE stream of zone colors at 4 Hz |
| `POST` | `/api/devices/{ip}/play` | Start an effect (body: `{"effect":"name","params":{...}}`) |
| `POST` | `/api/devices/{ip}/stop` | Stop current effect (fade to black) |
| `POST` | `/api/devices/{ip}/resume` | Clear phone override, resume schedule |
| `POST` | `/api/devices/{ip}/power` | Power on/off (body: `{"on": true}`) |

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

### Server Readiness

The server binds its HTTP port immediately on startup, then loads
devices from config IPs in the background.  This eliminates the
"connection refused" window that caused 502 errors from reverse
proxies (e.g. Cloudflare Tunnel) during restart.

Clients should query `GET /api/status` on connect:

```json
{"status": "loading", "ready": false, "version": "1.8"}
```

Once device loading completes the response changes to:

```json
{"status": "ready", "ready": true, "version": "1.8"}
```

While `ready` is `false`, other endpoints work normally but return empty
device lists.  The iOS app uses this to show a loading indicator instead
of an empty screen.

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

When the phone app (or any external client — REST, MQTT, Home Assistant,
Shortcuts) sends a `play` or `stop` command, the server marks the device
as "overridden" so the scheduler skips it.  The iOS app shows an orange
"Schedule paused on this device" banner on the device detail screen
while an override is active.

**Override lifecycle:**

```
  ┌─────────────┐   play / stop    ┌────────────────┐
  │  Scheduled   │ ──────────────► │   Overridden    │
  │  (normal)    │                 │ (scheduler skip)│
  └─────────────┘                 └────────────────┘
        ▲                                │
        │      resume / schedule         │
        │         transition             │
        └────────────────────────────────┘
```

Overrides are cleared in two ways:

- **Manual resume:** Tap "Resume Schedule" in the app (calls
  `POST /api/devices/{ip}/resume`), or publish to the MQTT resume
  topic.  The scheduler picks up on its next poll cycle (every 30 s).
- **Schedule transition:** When the active schedule entry changes
  (e.g., from "evening" to "night"), overrides that were set against
  the outgoing entry are cleared automatically.  Overrides set after
  a server restart or against a different entry are preserved.

**Virtual multizone groups:** When multiple devices are stitched into a
virtual multizone group (e.g., `group:porch` contains 10.0.0.62 and
10.0.0.23), the scheduler checks both the group ID *and* every member
IP for overrides.  This means overriding a single member device via the
app correctly pauses the entire group's schedule — the scheduler will
not overwrite a manually controlled device.

The `GET /api/devices/{ip}/status` and `GET /api/devices` responses
include an `"overridden"` boolean field so the app can display the
override state.

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

1. **Device List** — Shows all configured devices with name, product
   type, group, and current effect.  Virtual multizone groups are
   prefixed with "Group:" and display a group icon with member count.
   Pull-to-refresh fetches the latest state.

2. **Device Detail** — Live color strip visualization (SSE-fed at 4 Hz),
   current effect info, power toggle, stop button, restart button, and
   a link to change the effect.  Virtual groups show the combined zone
   count, member device IPs, and type "Virtual Group" (see screenshot
   below).

3. **Effect Picker** — Lists all registered effects with descriptions
   and parameter counts.

4. **Effect Config** — Auto-generated parameter UI built from the
   server's `Param` metadata.  Sliders for numeric params, pickers
   for choice params, text fields for strings.  Tap "Play" to send
   the command.  **Save as Defaults** pushes the current parameter
   values to the server so the scheduler uses them — no need to edit
   `server.json` by hand.  Parameter values persist across app
   sessions.

5. **Settings** — Server URL and API token configuration.  Token is
   stored in the iOS Keychain.  Includes a "Test Connection" button
   and an About section displaying the app icon, version, and license
   information.

#### Virtual Group Detail

The screenshot below shows the Device Detail screen for a virtual
multizone group named "porch" — two physical string lights (10.0.0.23
and 10.0.0.62) combined into a single 144-zone animation surface.

<p align="center">
  <img src="multizone.PNG" alt="Virtual multizone group detail" width="300">
</p>

---

## Effect Gallery

An online gallery showcasing animated previews of every effect is
available at the project's GitHub Pages site.  Each preview was rendered
headlessly using the `record` subcommand — no physical hardware needed.

**View the gallery:** [https://pkivolowitz.github.io/lifx/](https://pkivolowitz.github.io/lifx/)

Gallery features:

- Animated GIF previews of all 16 public effects
- Effect descriptions and full parameter tables
- Click-to-copy CLI command to reproduce any effect on your own hardware
- Seamless loop badge for periodic effects

**Adding to the gallery:**

```bash
# Record an effect preview
python3 glowup.py record aurora --duration 8 \
    --output docs/assets/previews/aurora.gif \
    --media-url assets/previews/aurora.gif \
    --title "Aurora Borealis" --author "Your Name"

# Rebuild the manifest (combines all JSON sidecars)
python3 -c "
import json, glob
sidecars = sorted(glob.glob('docs/assets/previews/*.json'))
effects = [json.load(open(p)) for p in sidecars]
json.dump(effects, open('docs/effects.json', 'w'), indent=2)
"

# Commit and push — GitHub Pages deploys automatically
git add docs/ && git commit -m "Add aurora to gallery" && git push
```

To enable GitHub Pages: repo Settings > Pages > Source: "Deploy from a
branch" > Branch: `master`, folder: `/docs`.

---

## Troubleshooting

LIFX devices maintain internal state that persists across sessions.
Occasionally — particularly after switching between different control
sources (this engine, the LIFX app, HomeKit) — a device may display
unexpected colors or brief visual artifacts when a new effect starts.

If you notice residual colors from a previous session, simply
**power-cycle the device** (off, then on) using the physical switch or
the LIFX app.  If the device is in a location where power-cycling is
inconvenient, opening the official LIFX app and briefly controlling the
device can help clear its internal state.

This is a characteristic of the LIFX firmware's internal state
management and may not be specific to GlowUp.

---

## Cloudflare Tunnel (Remote Access)

Cloudflare Tunnel creates an outbound-only encrypted connection from
the Pi to Cloudflare's edge network.  The phone connects to
`https://lights.yourdomain.com` — no ports are opened on the router
and no dynamic DNS is needed.

See [TUNNEL.md](TUNNEL.md) for complete setup instructions.

---

## Home Assistant Integration

GlowUp's REST API works with [Home Assistant](https://www.home-assistant.io/)
out of the box using HA's built-in
[`rest_command`](https://www.home-assistant.io/integrations/rest_command/)
integration.  No custom component or code changes are needed — just add a
few lines to your HA `configuration.yaml`.

### Prerequisites

- GlowUp server (`server.py`) running on your Pi, Mac, Windows PC, or Linux box
- An auth token configured in `server.json`
  (see [Server Configuration](#server-configuration) for how to generate one)
- Home Assistant installed and accessible on the same network
  (or reachable via Cloudflare Tunnel / VPN)

### Configuration

Add the following to your Home Assistant `configuration.yaml`:

```yaml
rest_command:
  glowup_play:
    url: "http://GLOWUP_IP:8420/api/devices/{{ ip }}/play"
    method: POST
    headers:
      Authorization: "Bearer YOUR_AUTH_TOKEN"
      Content-Type: "application/json"
    payload: '{"effect":"{{ effect }}","params":{{ params | default("{}")}}}'
    content_type: "application/json"

  glowup_stop:
    url: "http://GLOWUP_IP:8420/api/devices/{{ ip }}/stop"
    method: POST
    headers:
      Authorization: "Bearer YOUR_AUTH_TOKEN"

  glowup_resume:
    url: "http://GLOWUP_IP:8420/api/devices/{{ ip }}/resume"
    method: POST
    headers:
      Authorization: "Bearer YOUR_AUTH_TOKEN"
```

Replace `GLOWUP_IP` with the IP address (or hostname) of the machine
running `server.py`, and `YOUR_AUTH_TOKEN` with the token from your
`server.json`.  If using a Cloudflare Tunnel, replace the URL with
your tunnel hostname (e.g., `https://lights.yourdomain.com`).

Restart Home Assistant after saving the file, or reload the REST
command integration from **Developer Tools → YAML → REST commands**.

### Automation Example

Trigger an effect automatically — for example, play aurora at sunset:

```yaml
automation:
  - alias: "GlowUp aurora at sunset"
    trigger:
      - platform: sun
        event: sunset
        offset: "-00:30:00"
    action:
      - service: rest_command.glowup_play
        data:
          ip: "10.0.0.62"
          effect: "aurora"
          params: '{"speed": 10.0, "brightness": 60}'

  - alias: "GlowUp off at midnight"
    trigger:
      - platform: time
        at: "00:00:00"
    action:
      - service: rest_command.glowup_stop
        data:
          ip: "10.0.0.62"
```

For virtual multizone groups, use the group identifier (e.g.,
`group:porch`) as the `ip` value.

### Dashboard Button

Add a button to your Lovelace dashboard that plays an effect on tap:

```yaml
type: button
name: "Aurora"
icon: mdi:aurora
tap_action:
  action: call-service
  service: rest_command.glowup_play
  data:
    ip: "10.0.0.62"
    effect: "aurora"
    params: '{"speed": 10.0, "brightness": 80}'
```

### Notes

- **Scheduling:** GlowUp has its own scheduler with sunrise/sunset
  awareness built into `server.py`.  You can use either GlowUp's
  scheduler or HA automations — but not both on the same device
  simultaneously, as they will conflict.  If using HA automations,
  leave the `schedule` section out of your `server.json`.
- **Phone override:** Effects started via HA (or any HTTP client)
  set a phone override on the device, pausing the GlowUp scheduler.
  Use `rest_command.glowup_resume` to clear the override and return
  to the scheduled program.
- **Live color monitoring:** HA's `rest_command` does not support
  Server-Sent Events, so live zone color streaming requires the
  GlowUp iOS app or a direct SSE client.
- **No custom component needed:** A full HA custom component
  (registering GlowUp devices as HA light entities) is possible but
  unnecessary for most use cases.  The REST command approach covers
  play, stop, resume, and power control with no additional code.
- **Untested:** This integration has not yet been tested against a
  live Home Assistant instance.  If you try it, please open an issue
  at the [GitHub repo](https://github.com/pkivolowitz/lifx/issues)
  with any corrections or suggestions.

---

## Apple Shortcuts Integration

The GlowUp REST API works directly with Apple's
[Shortcuts](https://support.apple.com/guide/shortcuts/welcome/ios) app
on iPhone, iPad, and Mac.  No additional software is needed — Shortcuts
ships with every Apple device and can make HTTP requests natively via
the **Get Contents of URL** action.

### Basic Shortcut: Play an Effect

1. Open the **Shortcuts** app and tap **+** to create a new shortcut.
2. Add a **Get Contents of URL** action.
3. Set the URL to:
   ```
   http://GLOWUP_IP:8420/api/devices/DEVICE_IP/play
   ```
4. Tap **Show More** and configure:
   - **Method:** POST
   - **Headers:**
     - `Authorization` → `Bearer YOUR_AUTH_TOKEN`
     - `Content-Type` → `application/json`
   - **Request Body:** JSON
     - `effect` (Text) → `aurora`
     - `params` (Dictionary) → `{"speed": 10.0, "brightness": 60}`
5. Name the shortcut (e.g., "Porch Aurora").

Replace `GLOWUP_IP`, `DEVICE_IP`, and `YOUR_AUTH_TOKEN` with your
actual values (see [Server Configuration](#server-configuration)).

### Stop Shortcut

Create a second shortcut with the same steps but change:
- **URL:** `http://GLOWUP_IP:8420/api/devices/DEVICE_IP/stop`
- **Request Body:** leave empty (no body needed)

### Resume Schedule Shortcut

Same pattern with:
- **URL:** `http://GLOWUP_IP:8420/api/devices/DEVICE_IP/resume`
- **Request Body:** leave empty

### Trigger Ideas

Once you have shortcuts defined, Apple lets you trigger them in
several ways — all without opening the GlowUp app:

| Trigger | How | Example |
|---------|-----|---------|
| **Siri** | "Hey Siri, Porch Aurora" | Voice control from any Apple device |
| **Home Screen widget** | Long-press home screen → add Shortcuts widget | One-tap effect launcher |
| **NFC tag** | Shortcuts → Automation → NFC → scan tag | Tap phone to an NFC sticker on the wall to start an effect |
| **Time of day** | Shortcuts → Automation → Time of Day | Play an effect at sunset (Shortcuts has its own sunrise/sunset triggers) |
| **Arrive / Leave** | Shortcuts → Automation → Arrive / Leave | Turn on lights when you get home |
| **Focus mode** | Shortcuts → Automation → Focus | Start a calm effect when Do Not Disturb activates |

### Notes

- **Siri voice control** works immediately — Siri uses the shortcut
  name as the voice command.  Name your shortcuts accordingly
  (e.g., "Porch Aurora", "Stop Porch", "Resume Porch").
- **NFC tags** are inexpensive (a few cents each) and can be stuck
  anywhere — on the wall by the front door, on a nightstand, etc.
  Tap your phone to the tag and the shortcut runs instantly.
- **Scheduler conflict:** Like any external client, effects started
  via Shortcuts set a phone override on the device.  Use your
  "Resume" shortcut to hand control back to the GlowUp scheduler.
- **Remote access:** If your server is reachable via Cloudflare
  Tunnel, replace the LAN URL with your tunnel hostname
  (e.g., `https://lights.yourdomain.com/api/devices/...`).
  Shortcuts work identically over the internet.
- **Untested:** This integration has not yet been tested against a
  live Apple Shortcuts setup.  If you try it, please open an issue
  at the [GitHub repo](https://github.com/pkivolowitz/lifx/issues)
  with any corrections or suggestions.

---

## Node-RED Integration

[Node-RED](https://nodered.org/) is a flow-based visual programming
tool popular in the home-automation community.  GlowUp's REST API
integrates with Node-RED using the built-in **http request** node —
no custom nodes or palettes are needed.

### Prerequisites

- Node-RED installed and running (standalone, or as a Home Assistant
  add-on — either works)
- GlowUp server (`server.py`) running and reachable from the
  Node-RED host

### Play Effect Flow

Import the following JSON into Node-RED (**Menu → Import → Clipboard**)
to create a one-click flow that plays an effect:

```json
[
    {
        "id": "glowup_play",
        "type": "http request",
        "name": "GlowUp Play",
        "method": "POST",
        "url": "http://GLOWUP_IP:8420/api/devices/DEVICE_IP/play",
        "headers": {
            "Authorization": "Bearer YOUR_AUTH_TOKEN",
            "Content-Type": "application/json"
        },
        "payload": "{\"effect\":\"aurora\",\"params\":{\"speed\":10.0,\"brightness\":60}}",
        "paytoqs": "ignore",
        "ret": "txt",
        "wires": []
    }
]
```

Replace `GLOWUP_IP`, `DEVICE_IP`, and `YOUR_AUTH_TOKEN` with your
actual values.

Wire an **inject** node to the input to trigger it manually, or
connect any Node-RED trigger (MQTT message, time scheduler, dashboard
button, webhook, etc.).

### Stop and Resume Flows

Use the same **http request** node pattern with these URLs:

| Action | Method | URL | Body |
|--------|--------|-----|------|
| **Stop** | POST | `http://GLOWUP_IP:8420/api/devices/DEVICE_IP/stop` | *(none)* |
| **Resume** | POST | `http://GLOWUP_IP:8420/api/devices/DEVICE_IP/resume` | *(none)* |

### Dynamic Effect Selection

To select the effect at runtime, wire a **function** node ahead of the
http request node that sets the payload dynamically:

```javascript
msg.payload = {
    effect: msg.effect || "aurora",
    params: msg.params || { speed: 10.0, brightness: 60 }
};
msg.headers = {
    "Authorization": "Bearer YOUR_AUTH_TOKEN",
    "Content-Type": "application/json"
};
return msg;
```

Then set the http request node's **Method** to `POST` and leave its
body configuration set to use `msg.payload`.

### Dashboard Button

If you use
[node-red-dashboard](https://flows.nodered.org/node/node-red-dashboard)
or
[FlexDash](https://flows.nodered.org/node/@flexdash/node-red-fd-corewidgets),
wire a **button** widget node into the play flow for a browser-based
control panel.

### Live Color Streaming

Node-RED can consume GlowUp's Server-Sent Events stream for real-time
zone color monitoring.  Use the
[node-red-contrib-sse-client](https://flows.nodered.org/node/node-red-contrib-sse-client)
node pointed at:

```
http://GLOWUP_IP:8420/api/devices/DEVICE_IP/colors/stream
```

Each event delivers the current HSBK values for all zones, which you
can feed into dashboard gauges, color displays, or further automation
logic.

### Notes

- **MQTT bridge:** If your Node-RED setup is MQTT-centric, create a
  simple flow that subscribes to an MQTT topic (e.g.,
  `glowup/play/porch`) and forwards the message payload to the
  GlowUp http request node.  This gives you MQTT control of GlowUp
  without modifying server.py.
- **Scheduler conflict:** Like any external client, effects started
  via Node-RED set a phone override on the device.  Use the resume
  endpoint to hand control back to the GlowUp scheduler.
- **Error handling:** Wire the http request node's second output
  (error) to a **debug** node or a **catch** node to log failures.
- **Untested:** This integration has not yet been tested against a
  live Node-RED instance.  If you try it, please open an issue
  at the [GitHub repo](https://github.com/pkivolowitz/lifx/issues)
  with any corrections or suggestions.

---

## MQTT Integration

GlowUp includes a native MQTT bridge that connects the server to any
MQTT broker (Mosquitto, EMQX, HiveMQ, etc.).  Once enabled, any MQTT
client can control your lights and subscribe to real-time state
updates — no HTTP required.

Unlike the Home Assistant, Apple Shortcuts, and Node-RED integrations
(which call the REST API from the outside), the MQTT bridge runs
*inside* `server.py` and publishes state changes automatically.

### Prerequisites

- GlowUp server (`server.py`) running on your Pi, Mac, Windows PC,
  or Linux box
- An MQTT broker reachable from the server (e.g., Mosquitto on the
  same machine)
- The `paho-mqtt` Python package:

```bash
pip install paho-mqtt
```

### Configuration

Add an `"mqtt"` section to your `server.json`:

```json
{
    "mqtt": {
        "broker": "localhost",
        "port": 1883,
        "topic_prefix": "glowup",
        "username": null,
        "password": null,
        "tls": false,
        "publish_colors": false,
        "color_interval": 1.0
    }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `broker` | `"localhost"` | Hostname or IP of the MQTT broker |
| `port` | `1883` | Broker TCP port (`8883` is conventional for TLS) |
| `topic_prefix` | `"glowup"` | Root prefix for all topics |
| `username` | `null` | Broker username (omit or `null` for anonymous) |
| `password` | `null` | Broker password |
| `tls` | `false` | Enable TLS encryption |
| `publish_colors` | `false` | Publish live zone color data (high-frequency) |
| `color_interval` | `1.0` | Seconds between color publishes |

To disable MQTT, remove the `"mqtt"` section entirely.  The server
runs identically without it — MQTT is purely optional.

### Topic Layout

All topics are prefixed with `topic_prefix` (default `glowup`).
Device IDs are the same identifiers used in the REST API: IP addresses
for individual devices (e.g., `10.0.0.62`) and `group:name` for
virtual multizone groups (e.g., `group:porch`).

#### Published by GlowUp (state)

| Topic | Retained | QoS | Payload |
|-------|----------|-----|---------|
| `glowup/status` | Yes | 1 | `"online"` or `"offline"` (Last Will) |
| `glowup/devices` | Yes | 1 | JSON array of device info dicts |
| `glowup/device/{id}/state` | Yes | 1 | JSON: `{"running":true,"effect":"aurora","params":{...},"fps":20.1,"overridden":false}` |
| `glowup/device/{id}/colors` | No | 0 | JSON array of `{"h":...,"s":...,"b":...,"k":...}` per zone (only if `publish_colors` is enabled) |

State topics are published only when values change (polled every 2
seconds).  Color topics are published at `color_interval` rate
regardless of change, since zone colors shift continuously during
effects.

#### Subscribed by GlowUp (commands)

| Topic | Payload | Action |
|-------|---------|--------|
| `glowup/device/{id}/command/play` | `{"effect":"aurora","params":{"speed":10.0}}` | Start an effect |
| `glowup/device/{id}/command/stop` | *(any or empty)* | Stop current effect, fade to black |
| `glowup/device/{id}/command/resume` | *(any or empty)* | Clear override, let scheduler resume |
| `glowup/device/{id}/command/power` | `{"on": true}` or `{"on": false}` | Turn device on or off |

### Quick Start with Mosquitto

If you have Mosquitto installed on the same Pi as GlowUp:

```bash
# Install paho-mqtt
pip install paho-mqtt

# Add the mqtt section to server.json (see above), then restart:
python3 server.py server.json

# In another terminal — play an effect:
mosquitto_pub -t "glowup/device/10.0.0.62/command/play" \
  -m '{"effect":"aurora","params":{"speed":10.0,"brightness":60}}'

# Stop it:
mosquitto_pub -t "glowup/device/10.0.0.62/command/stop" -m ""

# Resume the scheduler:
mosquitto_pub -t "glowup/device/10.0.0.62/command/resume" -m ""

# Watch state changes:
mosquitto_sub -t "glowup/#" -v
```

For virtual multizone groups, use the group ID as the device:

```bash
mosquitto_pub -t "glowup/device/group:porch/command/play" \
  -m '{"effect":"cylon","params":{"speed":4.0}}'
```

### Availability (Last Will and Testament)

The bridge publishes `"online"` to `glowup/status` (retained) on
connect.  If the server crashes or loses its broker connection, the
broker automatically publishes `"offline"` to the same topic via
MQTT's Last Will mechanism.  Home automation systems can subscribe to
this topic to detect when GlowUp is unreachable.

### Notes

- **Dependency:** `paho-mqtt>=2.0` is the only pip dependency in the
  entire project and is required only for MQTT.  If you do not need
  MQTT, do not install it — the server works without it.
- **Scheduler conflict:** Commands received via MQTT set a phone
  override on the device, pausing the GlowUp scheduler.  Publish to
  the `resume` topic to hand control back.
- **Color publishing:** Disabled by default because it generates
  substantial broker traffic (one message per device per
  `color_interval` seconds).  Enable it only if you have a consumer
  for the data (e.g., a Node-RED dashboard visualizing zone colors).
- **Authentication:** The MQTT bridge does not enforce the REST API's
  bearer token — access control is handled by the MQTT broker itself
  (username/password, ACLs, TLS client certificates, etc.).
- **Reconnection:** If the broker restarts or the connection drops,
  paho-mqtt reconnects automatically with exponential backoff (1–60
  seconds).  All subscriptions are re-established on reconnect.
- **Untested:** This integration has not yet been tested against a
  live MQTT broker.  If you try it, please open an issue at the
  [GitHub repo](https://github.com/pkivolowitz/lifx/issues) with
  any corrections or suggestions.
