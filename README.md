# LIFX Effect Engine

A modular effect engine for LIFX string lights, replacing the battery-draining phone app with a daemon that runs animated effects autonomously from a Raspberry Pi or Mac.

## What It Does

- **Discovery** — finds all LIFX devices on your LAN via UDP broadcast
- **Effects** — ships with aurora borealis, binary clock, waving flag, Larson scanner (Cylon), Morse code, and more
- **Scheduler** — daemon that runs effects on a timed schedule using symbolic times (sunrise, sunset, dawn, dusk) with offsets, supporting multiple independent device groups
- **Extensible** — add a new effect by dropping a single Python file in `effects/`; it auto-registers and appears in the CLI

No cloud. No app. No account. Just UDP packets on your LAN.

## Quick Start

```bash
python3 glowup.py discover                        # find devices
python3 glowup.py effects                         # list effects + params
python3 glowup.py play aurora --ip <device-ip>    # run an effect
python3 glowup.py play flag --ip <device-ip> --country france
```

## Documentation

See the **[User Manual](MANUAL.md)** for:
- Full CLI reference
- All effects with parameter tables
- Effect developer guide (how to build your own)
- Engine and Controller API

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
| `glowup.py` | CLI entry point |
| `solar.py` | Sunrise/sunset calculator (NOAA algorithm, no dependencies) |
| `scheduler.py` | Orchestrator daemon with device groups and symbolic scheduling |

## Effects

| Effect | Description |
|--------|-------------|
| `aurora` | Slow-moving curtains of color like the northern lights |
| `binclock` | Display the current time in binary (per-group colors) |
| `breathe` | All bulbs oscillate between two colors via sine wave |
| `cylon` | Larson scanner — bright eye sweeps back and forth |
| `flag` | Waving national flag with perspective ripple (199 countries) |
| `morse` | Flashes a message in Morse code |
| `twinkle` | Random zones sparkle and fade like Christmas lights |
| `wave` | Standing wave — bulbs vibrate with fixed nodes |

## Requirements

- Python 3.10+
- No external dependencies (stdlib only)
- LIFX devices on the same LAN subnet

## License

MIT
