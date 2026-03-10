<p align="center">
  <img src="logo.jpg" alt="GlowUp" width="200">
</p>

# GLOWUP — LIFX Effect Engine

A modular effect engine for LIFX devices (string lights, beams, single color
bulbs, and monochrome bulbs), replacing the battery-draining phone app with a
daemon that runs animated effects autonomously from a Raspberry Pi or Mac.

This project utilizes AI assistance (Claude 4.6) for boilerplate and logic
expansion. All final architectural decisions, algorithmic validation, and code
integration are performed by Perry Kivolowitz, the sole Human Author.

## What It Does

- **Discovery** — finds all LIFX devices on your LAN via UDP broadcast
- **Effects** — ships with 12 effects: aurora borealis, binary clock, fireworks, waving flag (199 countries), Larson scanner, Morse code, twinkling lights, standing wave, color breathe, polychrome bloom, crossfade comparison, and zone map diagnostic
- **Virtual multizone** — group any combination of devices into a single animation surface. A 108-zone string light and 4 single bulbs become a 112-zone strip. Effects animate across all devices as one. LIFX limits a physical chain to 3 string lights; virtual multizone removes that ceiling — any number of independent strings can be combined.
- **Identify** — pulse a bulb's brightness to figure out which physical lamp corresponds to which IP address
- **Monochrome support** — color effects on white-only bulbs are automatically converted to perceptually correct brightness using BT.709 luma coefficients
- **Scheduler** — daemon that runs effects on a timed schedule using symbolic times (sunrise, sunset, dawn, dusk) with offsets, supporting multiple independent device groups
- **Extensible** — add a new effect by dropping a single Python file in `effects/`; it auto-registers and appears in the CLI

- **REST API server** — HTTP daemon that wraps the entire engine for remote control from any HTTP client
- **iPhone app** — native SwiftUI app with live color monitoring, auto-generated parameter UI, and Keychain-secured auth; works over LAN or remotely via tunnel/VPN
- **Cloudflare Tunnel** — secure remote access from anywhere without opening router ports or running a VPN

- **CIELAB color interpolation** — blends between colors in the perceptually uniform CIELAB color space, eliminating the phantom hue artifacts that plague naive HSB interpolation (e.g., red bleeding into a blue/white flag boundary). Switchable at runtime via `--lerp lab|hsb` for hardware flexibility.

No cloud dependency. No external Python packages. Just UDP packets on your LAN — with optional secure remote access.

### iPhone App

The GlowUp iOS app connects to `server.py` running on your Pi or Mac.
The simplest setup is LAN-only: point the app at `http://<pi-ip>:8420`
and you're done. For remote access, use a Cloudflare Tunnel, Tailscale,
or any VPN. See the [User Manual](MANUAL.md#glowup-ios-app) for build
instructions and deployment to your phone.

## Quick Start

```bash
python3 glowup.py discover                        # find devices
python3 glowup.py effects                         # list effects + params
python3 glowup.py identify --ip <device-ip>       # pulse a bulb to locate it
python3 glowup.py play aurora --ip <device-ip>    # run an effect
python3 glowup.py play flag --ip <device-ip> --country france
python3 glowup.py play fireworks --ip <device-ip> # string lights only

# Virtual multizone — animate across a group of devices
python3 glowup.py play cylon --config schedule.json --group office

# Layered help — three levels
python3 glowup.py --help                          # top-level commands
python3 glowup.py play --help                     # play options only
python3 glowup.py play fireworks --help           # full param reference for one effect

# Preview an effect in the simulator without touching your lights
python3 glowup.py play fireworks --ip <device-ip> --sim-only
```

## Caveat

I have tested with string and monochrome lights. I *hope* this all works for
you. Please report problems but remember, I do not own the other types. I'll do
my best. Most of all, I would appreciate you fixing errors for the other LIFX
products if you are willing and able. Thank you.

## Documentation

See the **[User Manual](MANUAL.md)** for:
- Full CLI reference (discover, effects, identify, play)
- Layered help system (`--help` at top-level, play, and per-effect)
- All effects with parameter tables
- Virtual multizone setup and configuration
- Effect developer guide (how to build your own)
- Live simulator (`--sim` and `--sim-only` preview modes)
- Engine, Controller, and VirtualMultizoneDevice API
- REST API server reference
- GlowUp iOS app
- Testing

See **[TUNNEL.md](TUNNEL.md)** for Cloudflare Tunnel setup.

## Server (Recommended Deployment)

`server.py` is the recommended way to run GlowUp as a daemon. It provides a
REST API for remote control (including the iPhone app), a built-in scheduler
with sunrise/sunset awareness, and SSE-based live color streaming — all in a
single process.

```bash
python3 server.py server.json       # run the server
```

See [server.json.example](server.json.example) for config format. Deploy as a
systemd service with the included
[glowup-server.service](glowup-server.service). The server configuration
includes the schedule directly — no separate schedule file is needed.

### Standalone Scheduler (Alternative)

If you don't need the REST API or iPhone app — just timed effects on a cron-like
schedule — `scheduler.py` is a lighter alternative. It spawns a separate process
per device and requires no HTTP or authentication.

```bash
python3 scheduler.py --dry-run schedule.json.example  # preview resolved times
python3 scheduler.py /etc/glowup/schedule.json        # run as daemon
```

See [schedule.json.example](schedule.json.example) for config format. Deploy
with [glowup-scheduler.service](glowup-scheduler.service). **Do not run both
`server.py` and `scheduler.py` simultaneously** — they will conflict over device
control.

## Architecture

| File | Role |
|------|------|
| `transport.py` | LIFX LAN protocol v2: discovery, persistent UDP, extended multizone |
| `engine.py` | Threaded frame loop with thread-safe public API |
| `effects/__init__.py` | Effect base class, Param system, auto-registration |
| `colorspace.py` | CIELAB/HSB color interpolation with runtime method dispatch |
| `effects/*.py` | Pure renderers — no I/O, no device knowledge |
| `effects/flag_data.py` | 199-country flag color database |
| `glowup.py` | CLI entry point (discover, effects, identify, play) |
| `simulator.py` | Live tkinter preview window (`--sim`, `--sim-only`), optional graceful fallback |
| `solar.py` | Sunrise/sunset calculator (NOAA algorithm, no dependencies) |
| `server.py` | REST API daemon with built-in scheduler — recommended deployment |
| `scheduler.py` | Standalone scheduler alternative (no HTTP, no auth) |
| `ios/GlowUp/` | Native SwiftUI iPhone app with asset catalog and app icon |
| `test_virtual_multizone.py` | Mock-based tests for virtual multizone dispatch |

## Effects

| Effect | Description |
|--------|-------------|
| `aurora` | Slow-moving curtains of color like the northern lights |
| `binclock` | Display the current time in binary (per-group colors) |
| `breathe` | All bulbs oscillate between two colors via sine wave |
| `cylon` | Larson scanner — bright eye sweeps back and forth |
| `fireworks` | Rockets launch from both ends, trail exhaust, detonate in expanding color halos (string lights) |
| `flag` | Waving national flag with perspective ripple (199 countries) |
| `morse` | Flashes a message in Morse code |
| `twinkle` | Random zones sparkle and fade like Christmas lights |
| `wave` | Standing wave — bulbs vibrate with fixed nodes |
| `_bloom` | Polychrome bloom exploiting concentric zone architecture |
| `_spin` | Rotate colors within each polychrome bulb |
| `_crossfade` | A/B comparison between HSB and Lab interpolation |
| `_zone_map` | Diagnostic — visualize physical zone layout |
| `_polychrome_test` | Static R/G/B pattern to reveal zone positions |

## Requirements

- Python 3.10+
- No external dependencies (stdlib only)
- One or more LIFX devices on the same LAN subnet (multizone, single color, or monochrome)

## License

MIT

## Appreciation

> If you find this software useful, please consider donating to a local foodbank. Even a can of soup makes a difference.
