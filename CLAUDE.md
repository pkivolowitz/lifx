# LIFX Effect Engine

## Project Overview
A modular effect engine for LIFX string lights. Replaces the battery-draining phone app with a daemon that runs effects autonomously from a Raspberry Pi or Mac.

## Architecture
- **transport.py** — LIFX LAN protocol v2: discovery, persistent UDP sockets, extended multizone (type 510/511/512)
- **engine.py** — Threaded frame loop (`Engine`) with thread-safe public API (`Controller`)
- **effects/__init__.py** — `Effect` base class with `EffectMeta` auto-registration, `Param` system for self-describing parameters
- **effects/*.py** — Individual effects: pure renderers, no device/network knowledge
- **glowup.py** — CLI entry point with auto-generated argparse from `Param` declarations
- **solar.py** — NOAA solar position algorithm: sunrise/sunset/dawn/dusk/noon, no dependencies
- **scheduler.py** — Orchestrator daemon with named device groups and symbolic scheduling
- **discover.py** — Standalone LIFX discovery tool
- **lanscan.py** — General LAN scanner (async ping sweep + ARP + OUI lookup)

## Key Technical Details
- HSBK color model: all values 0-65535, kelvin 1500-9000
- 3 zones per physical bulb on LIFX string lights
- Extended multizone: up to 82 zones per packet, apply=0 stages, apply=1 commits
- Fire-and-forget (rapid mode) for animation frames
- MULTIZONE_PRODUCTS: 19 IDs covering Z, Beam, Neon, String, Outdoor Neon, Indoor Neon, Permanent Outdoor (US + Intl variants); see transport.py for full annotated list
- Broadcast address auto-detected per platform (macOS ifconfig, Linux ioctl)

## Adding a New Effect
1. Create `effects/myeffect.py`
2. Subclass `Effect`, set `name` and `description`
3. Declare parameters as class-level `Param(default, min, max, description)`
4. Implement `render(self, t: float, zone_count: int) -> list[HSBK]`
5. The effect auto-registers and appears in CLI

## Running
```bash
python3 glowup.py discover                     # find devices
python3 glowup.py effects                      # list effects + params
python3 glowup.py identify --ip <device-ip>    # pulse to locate a device
python3 glowup.py play cylon --ip <device-ip>  # run an effect
python3 scheduler.py --dry-run schedule.json # preview schedule
```

## Code Standards
- PEP 257 docstrings on all public classes, methods, and functions
- Explanatory inline comments
- Type hints on all function signatures and variables
- Version strings (`__version__`) in every module
- No magic numbers — each file to contain a constants section
- Honor the 3 level help system
- py_compile each module before testing
- Expansive commit messages; every high-level change gets its own commit
- Code to be bullet and idiot proofed
- Push back on bad ideas - notice questionable practices and suggest improvement
- Do the **right** thing not the expedient thing - technical debt to be avoided
