# CLI Reference

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

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

When the GlowUp server is reachable, this command routes via the server
and queries devices from the Pi (bypassing mesh router UDP filtering).
Use `--local` to force direct UDP.

See [Server Routing & Safety](docs/25-server-routing-safety.md) for details.

### effects

List all registered effects and their tunable parameters.

```bash
python3 glowup.py effects
```

Each effect is printed with its name, description, and every parameter
including its default value and valid range.

### identify

Pulse a device's brightness so you can visually locate which physical
bulb corresponds to a given IP address.

```bash
python3 glowup.py identify --ip <device-ip> [--duration SECONDS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--ip` | *(required)* | Target device IP address or hostname |
| `--duration` | 10 | Pulse duration in seconds (server mode only; ignored locally) |

**Local mode** (direct UDP): The device slowly breathes between dim and
full brightness in warm white until you press Ctrl+C.  On stop, the device
is powered off.

**Server mode** (when server is reachable): The pulse runs asynchronously
on the server for the specified duration and returns immediately.  The
pulse can be cancelled early with Ctrl+C.

Works with all device types (multizone, single color, monochrome).

See [Server Routing & Safety](docs/25-server-routing-safety.md) for details.

### play

Run an effect on a device or device group. Blocks until Ctrl+C or SIGTERM.

**Single device:**

```bash
python3 glowup.py play <effect> --ip <device_ip> [--fps N] [--param value ...]
```

**Virtual multizone (device group from server):**

```bash
python3 glowup.py play <effect> --group <name> [--fps N] [--param value ...]
```

**Virtual multizone (device group from local file):**

```bash
python3 glowup.py play <effect> --group <name> --config <file> [--fps N] [--param value ...]
```

When using `--group`, the device list is fetched from the GlowUp server
via `GET /api/groups`.  The server returns groups defined in its config
file, authenticated by a bearer token stored in `~/.glowup_token`.
Add `--config <file>` to load the group from a local JSON file instead.

Devices in a group are combined into a virtual multizone strip.
Multizone devices (string lights, beams) contribute all their physical
zones; single bulbs contribute one zone each.  A group containing a
108-zone string light and 4 single bulbs becomes a 112-zone virtual
device.  Effects that spread patterns across zones (cylon, aurora,
wave, twinkle) animate across all devices as if they were a single strip.
Multizone devices receive efficient batched updates (the same 2-packet
extended multizone protocol); single bulbs receive individual `set_color()`
calls.  Monochrome bulbs in the group automatically receive BT.709
luma-converted brightness.  You can mix any device types freely.

| Option      | Default | Description                               |
|-------------|---------|-------------------------------------------|
| `--ip`      | *(none)* | Target device IP address (single device mode) |
| `--group`   | *(none)* | Device group name (fetched from server, or from local file with `--config`) |
| `--config`  | *(none)* | Path to local config file containing device groups |
| `--server`  | `192.0.2.48:8420` | Server host:port for remote group lookup |
| `--fps`     | 20      | Frames per second for the render loop     |
| `--sim`     | off     | Open a live simulator window alongside the real lights               |
| `--sim-only` | off    | Query device geometry then run the effect in the simulator only — no commands sent to the lights (see [Sim-Only Mode](#sim-only-mode)) |
| `--zpb`     | 3       | Zones per bulb.  Effects render one color per bulb; the engine replicates it across all zones in the bulb.  Default 3 matches LIFX string lights (36 zones = 12 bulbs).  Use 1 for per-zone rendering. |
| `--lerp`    | oklab   | Color interpolation: `oklab` (best), `lab` (classic CIELAB), `hsb` (cheap) |

You must specify `--ip`, `--group`, or `--zones` (not combinations).

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

# Cylon scanner across 5 room lamps (group fetched from server)
python3 glowup.py play cylon --group office --speed 3

# Aurora drifting around a room (group from local config file)
python3 glowup.py play aurora --group living-room --config schedule.json

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

### off

⚠️  **EMERGENCY POWER-OFF** — Power off all LIFX devices on the network.

```bash
python3 glowup.py off
```

Requires explicit confirmation by typing "off" at the prompt.  No arguments.

This command:
1. Sends a direct UDP broadcast power-off to all devices on the local subnet
2. Tells the server to power off all configured devices
3. Cancels any running identify pulses
4. Works even if the server is offline (broadcast is independent)

**Use when:**
- Physical distress from flashing lights (e.g., overlapping identify pulses)
- Runaway effects with no easy stop
- Any emergency requiring immediate darkness

**Example:**
```
$ python3 glowup.py off

⚠️  EMERGENCY POWER-OFF ⚠️
This will immediately power off ALL LIFX devices on the network.
Type 'off' to confirm, or press Ctrl+C to cancel.

Confirm: off

Powering off all devices...

✓ Broadcast power-off sent to local subnet
✓ Server powered off 6 configured device(s)
✓ Cancelled 0 identify pulse(s) on server

✓ Emergency power-off complete
```

See [Server Routing & Safety](docs/25-server-routing-safety.md) for details.

**Requires:** [ffmpeg](https://ffmpeg.org/) must be installed and on your PATH.
