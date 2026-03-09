# GLOWUP — LIFX Effect Engine

A modular effect engine for LIFX devices (string lights, beams, single color
bulbs, and monochrome bulbs), replacing the battery-draining phone app with a
daemon that runs animated effects autonomously from a Raspberry Pi or Mac.

This project utilizes AI assistance (Claude 4.6) for boilerplate and logic
expansion. All final architectural decisions, algorithmic validation, and code
integration are performed by Perry Kivolowitz, the sole Human Author.

## What It Does

- **Discovery** — finds all LIFX devices on your LAN via UDP broadcast
- **Effects** — ships with 9 effects: aurora borealis, binary clock, fireworks, waving flag (199 countries), Larson scanner, Morse code, twinkling lights, standing wave, and color breathe
- **Virtual multizone** — group any combination of devices into a single animation surface. A 108-zone string light and 4 single bulbs become a 112-zone strip. Effects animate across all devices as one. LIFX limits a physical chain to 3 string lights; virtual multizone removes that ceiling — any number of independent strings can be combined.
- **Identify** — pulse a bulb's brightness to figure out which physical lamp corresponds to which IP address
- **Monochrome support** — color effects on white-only bulbs are automatically converted to perceptually correct brightness using BT.709 luma coefficients
- **Scheduler** — daemon that runs effects on a timed schedule using symbolic times (sunrise, sunset, dawn, dusk) with offsets, supporting multiple independent device groups
- **Extensible** — add a new effect by dropping a single Python file in `effects/`; it auto-registers and appears in the CLI

- **REST API server** — HTTP daemon that wraps the entire engine for remote control from any HTTP client
- **iPhone app** — native SwiftUI app with live color monitoring, auto-generated parameter UI, and Keychain-secured auth
- **Cloudflare Tunnel** — secure remote access from anywhere without opening router ports or running a VPN

No cloud dependency. No external Python packages. Just UDP packets on your LAN — with optional secure remote access.

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

## Scheduler (Daemon)

The scheduler runs effects on a timed schedule with sunrise/sunset awareness:

```bash
python3 scheduler.py --dry-run schedule.json.example  # preview resolved times
python3 scheduler.py /etc/glowup/schedule.json          # run as daemon
```

See [schedule.json.example](schedule.json.example) for config format. Deploy as a systemd service with the included [glowup-scheduler.service](glowup-scheduler.service).

## Architecture

| File | Role |
|------|------|
| `transport.py` | LIFX LAN protocol v2: discovery, persistent UDP, extended multizone |
| `engine.py` | Threaded frame loop with thread-safe public API |
| `effects/__init__.py` | Effect base class, Param system, auto-registration |
| `effects/*.py` | Pure renderers — no I/O, no device knowledge |
| `effects/flag_data.py` | 199-country flag color database |
| `glowup.py` | CLI entry point (discover, effects, identify, play) |
| `simulator.py` | Live tkinter preview window (`--sim`, `--sim-only`), optional graceful fallback |
| `solar.py` | Sunrise/sunset calculator (NOAA algorithm, no dependencies) |
| `scheduler.py` | Orchestrator daemon with device groups and symbolic scheduling |
| `server.py` | REST API daemon — subsumes scheduler, enables remote control |
| `ios/GlowUp/` | Native SwiftUI iPhone app |
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

## Requirements

- Python 3.10+
- No external dependencies (stdlib only)
- One or more LIFX devices on the same LAN subnet (multizone, single color, or monochrome)

## License

MIT

## Appreciation

> If you find this software useful, please consider donating to a local foodbank. Even a can of soup makes a difference.
