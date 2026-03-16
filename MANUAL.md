# GLOWUP - LIFX Effect Engine — User Manual

Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
Licensed under the MIT License. See [LICENSE](LICENSE) for details.

This project utilizes AI assistance (Claude 4.6) for boilerplate and logic
expansion. All final architectural decisions, algorithmic validation, and
code integration are performed by Perry Kivolowitz, the sole Human Author.

---

## Table of Contents

| # | Section | Description |
|---|---------|-------------|
| 1 | [Overview](docs/01-overview.md) | What GlowUp is, what it does |
| 2 | [Requirements](docs/02-requirements.md) | Hardware, software, and network prerequisites |
| 3 | [Quick Start](docs/03-quick-start.md) | Get running in 60 seconds |
| 4 | [CLI Reference](docs/04-cli-reference.md) | discover, effects, identify, play, record |
| 5 | [Scheduler](docs/05-scheduler.md) | Daemon mode, config file, symbolic times, systemd |
| 6 | [Built-in Effects](docs/06-effects.md) | All 21 public effects with parameter tables |
| 7 | [Effect Developer Guide](docs/07-effect-dev-guide.md) | Architecture, base class, Param system, HSBK, examples |
| 8 | [Live Simulator](docs/08-simulator.md) | tkinter preview window (--sim, --sim-only) |
| 9 | [Engine and Controller API](docs/09-engine-api.md) | Programmatic API, VirtualMultizoneDevice |
| 10 | [Testing](docs/10-testing.md) | 7 test modules, 189+ tests, how to run |
| 11 | [REST API Server](docs/11-rest-api.md) | server.py endpoints, auth, SSE, overrides, systemd |
| 12 | [GlowUp iOS App](docs/12-ios-app.md) | Building, running, connectivity, app screens |
| 13 | [Effect Gallery](docs/13-gallery.md) | GitHub Pages gallery with animated GIF previews |
| 14 | [Troubleshooting](docs/14-troubleshooting.md) | Common issues and fixes |
| 15 | [Cloudflare Tunnel](docs/15-tunnel.md) | Secure remote access without port forwarding |
| 16 | [Home Assistant](docs/16-home-assistant.md) | REST command integration with HA |
| 17 | [macOS Remote Control](docs/17-macos-remote.md) | Shell scripts and .command files for Finder/Dock |
| 18 | [Node-RED](docs/18-node-red.md) | Flow-based visual automation |
| 19 | [MQTT](docs/19-mqtt.md) | Native pub/sub bridge for device control |
| 20 | [Media Pipeline](docs/20-media-pipeline.md) | Audio-reactive lighting, Mosaic Warfare architecture |
| 21 | [SOE Pipeline](docs/21-soe-pipeline.md) | Sensors → Operators → Emitters architecture, dispatch modes, vision |
| 22 | [Emitter Developer Guide](docs/22-emitter-dev-guide.md) | Emitter ABC, lifecycle hooks, Param system, creating new emitters |
| 23 | [MIDI Pipeline](docs/23-midi-pipeline.md) | MIDI sensor, emitter, light bridge, multi-station broadcasting |
