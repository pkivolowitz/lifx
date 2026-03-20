<p align="center">
  <img src="logo.jpg" alt="GlowUp" width="200">
</p>

# GLOWUP — LIFX Effect Engine

A modular effect engine and distributed media platform for LIFX
devices, replacing the phone app with a CLI and server that run
autonomously from a Raspberry Pi, Mac, or any Linux box.

This project utilizes AI assistance (Claude 4.6) for boilerplate and
logic expansion. All architectural decisions and code integration are
by Perry Kivolowitz, the sole Human Author.

## Highlights

- **26 effects** — aurora, fireworks, Newton's Cradle, cellular automata, waving flags (199 countries), plasma, sonar, and more
- **Virtual multizone** — stitch any devices into one animation surface; removes the 3-string hardware limit
- **MIDI pipeline** — parse and replay MIDI files through synth + synchronized LIFX lights; multi-station broadcasting with runtime station switching
- **N-body visualizer** — WebGL particle simulation driven by MIDI events, rendered in the browser
- **Distributed SOE framework** — Sensors → Operators → Emitters across multiple machines via MQTT
- **REST API + iOS app** — remote control from anywhere; Cloudflare Tunnel for secure access
- **No external Python packages** for core features (stdlib only)

## Quick Start

```bash
python3 glowup.py discover                        # find devices
python3 glowup.py effects                         # list all 26 effects
python3 glowup.py play aurora --ip <device-ip>    # run an effect
python3 glowup.py play cylon --sim-only --zones 36  # preview without hardware
python3 glowup.py record aurora --duration 8      # render to GIF
```

## MIDI Pipeline

```bash
# Terminal 1 — audio
python3 -m emitters.midi_out --backend fluidsynth --soundfont gm.sf2

# Terminal 2 — lights
python3 -m distributed.midi_light_bridge --ip 192.0.2.23 192.0.2.34

# Terminal 3 — play
python3 glowup.py replay --file song.mid
```

All three subscribe to the same MQTT bus independently. Sound and
light are synchronized — driven by the same event stream.

## Effect Gallery

See animated previews: **[Effect Gallery](https://pkivolowitz.github.io/lifx/)**

## Documentation

The **[User Manual](MANUAL.md)** is organized as a progressive
disclosure tree — start with pretty lights, add complexity only
when you need it:

- **Core** — CLI, 26 effects, simulator, troubleshooting
- **Server** — REST API, scheduling, systemd/launchd
- **Remote Access** — iOS app, Cloudflare Tunnel, Home Assistant, Node-RED
- **Database** — PostgreSQL diagnostics, dashboard
- **Distributed** — MQTT, SOE pipeline, audio/MIDI pipelines, N-body visualizer
- **Developer** — write your own effects, sensors, operators, emitters

<p align="center">
  <img src="multizone.PNG" alt="Virtual multizone group in iOS app" width="300">
</p>

## What's New (March 2026)

Major infrastructure and reliability release. Not much user-visible
change, but a large body of work underneath:

- **Unified scheduling** — single `schedule.json` shared between
  server and standalone scheduler. Device groups now accept labels
  and MAC addresses, not just IPs.
- **Label/MAC-based device identity** — `server.json` and
  `schedule.json` use human-readable labels or MAC addresses
  instead of IP addresses. Devices survive DHCP reassignment
  and router swaps.
- **Server route table** — 315 lines of if-elif URL routing replaced
  with a declarative 39-route table. Adding an API endpoint is one
  line.
- **194 regression tests** gated by a pre-commit hook. Includes
  use-case-level pipeline tests, distributed agent tests, MIDI/audio
  fixture validation, and FFT frequency detection.
- **Tech debt audit** — bare-except handlers replaced with logged
  warnings, duplicate logic consolidated, undefined variables fixed,
  raw protocol code extracted to proper layers.
- **MAC-based ARP dedup** — devices that change IPs no longer appear
  as duplicates in discovery.
- **iOS app** update is in progress — the server API is unchanged so
  the current app continues to work.

## Caveat

I have tested with string, Neon, and monochrome lights. Please report
problems — I don't own every LIFX product. Fixes for other devices
are welcome.

## Requirements

- Python 3.10+ (macOS, Linux; Windows untested)
- LIFX devices on the same LAN
- **Optional:** ffmpeg (record), FluidSynth (MIDI audio), paho-mqtt (distributed)
- See [Requirements](docs/02-requirements.md) for platform setup

## License

MIT

## Appreciation

> If you find this software useful, please consider donating to a local foodbank. Even a can of soup makes a difference.
