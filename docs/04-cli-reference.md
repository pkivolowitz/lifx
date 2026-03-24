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

GlowUp is **server-preferred**: when the GlowUp server is reachable, it
handles device resolution, effect execution, and packet delivery.  This
gives you label-based device addressing, ARP-based discovery, keepalive,
and scheduling — features unavailable in standalone mode.  If the server
is unreachable, the CLI falls back to direct UDP (requires `--ip`).

**By device label or MAC (server-preferred):**

```bash
python3 glowup.py play <effect> --device "PORCH STRING LIGHTS" [--fps N] [--param value ...]
python3 glowup.py play <effect> --device "d0:73:d5:d4:79:9c" [--param value ...]
```

The server resolves the label (or MAC) to a live IP via its device
registry and ARP table, then runs the effect.  The CLI blocks until
Ctrl+C, which tells the server to stop.

**By IP (standalone or server):**

```bash
python3 glowup.py play <effect> --ip <device_ip> [--fps N] [--param value ...]
```

Connects directly to the device via UDP.  Works with or without
a server, but requires you to know the device's current IP address.

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
| `--device`  | *(none)* | Target device by registry label or MAC address.  **Requires server.** The server resolves the identifier and runs the effect.  **Visible on dashboard.** |
| `--ip`      | *(none)* | Target device IP address (direct UDP, works standalone).  **Not visible on dashboard** — the server is not involved. |
| `--group`   | *(none)* | Device group name (fetched from server, or from local file with `--config`).  **Visible on dashboard** when fetched from server. |
| `--config`  | *(none)* | Path to local config file containing device groups |
| `--server`  | `192.0.2.48:8420` | Server host:port for remote group lookup |
| `--fps`     | 20      | Frames per second for the render loop     |
| `--sim`     | off     | With `--device`: fetches device geometry from server and opens a local simulator (no packets sent).  With `--ip`: opens a simulator alongside the real lights. |
| `--sim-only` | off    | Same as `--sim` but never sends commands to the lights.  With `--device`, fetches zone count from the server so you see the real geometry. |
| `--zpb`     | 3       | Zones per bulb.  Effects render one color per bulb; the engine replicates it across all zones in the bulb.  Default 3 matches LIFX string lights (36 zones = 12 bulbs).  Use 1 for per-zone rendering. |
| `--lerp`    | oklab   | Color interpolation: `oklab` (best), `lab` (classic CIELAB), `hsb` (cheap) |

You must specify `--device`, `--ip`, `--group`, or `--zones` (not combinations).

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
# Play cylon on porch string lights (server resolves label → IP)
python3 glowup.py play cylon --device "PORCH STRING LIGHTS"

# Same device, red scanner, fast and wide
python3 glowup.py play cylon --device "PORCH STRING LIGHTS" --speed 1.0 --width 12 --hue 0

# Preview what cylon looks like on the string lights (102 zones)
# without actually sending commands — geometry fetched from server
python3 glowup.py play cylon --device "PORCH STRING LIGHTS" --sim-only

# Slow blue-to-green breathe (by IP, standalone or server)
python3 glowup.py play breathe --ip 10.0.0.34 --speed 8.0 --hue1 240 --hue2 120

# Aurora borealis at low brightness
python3 glowup.py play aurora --device "Living Room Floor Lamp" --brightness 40 --speed 15

# Cylon scanner across 5 room lamps (group fetched from server)
python3 glowup.py play cylon --group office --speed 3

# Aurora drifting around a room (group from local config file)
python3 glowup.py play aurora --group living-room --config schedule.json
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

### power

Turn a device or group on or off.  Requires the server.

```bash
python3 glowup.py power on  --device "group:main_bedroom"
python3 glowup.py power off --device "PORCH STRING LIGHTS"
python3 glowup.py power on  --device "group:all"
```

| Option     | Description                                        |
|------------|----------------------------------------------------|
| `state`    | Required: `on` or `off`                            |
| `--device` | Device label, MAC, IP, or group (e.g. `group:all`) |

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
