# GLOWUP - LIFX Effect Engine — User Manual

Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
Licensed under the MIT License. See [LICENSE](../LICENSE) for details.

This project utilizes AI assistance (Claude 4.6) for boilerplate and logic
expansion. All final architectural decisions, algorithmic validation, and
code integration are performed by Perry Kivolowitz, the sole Human Author.

---

## Choose Your Path

GlowUp scales from a single laptop running light effects to a
distributed multi-machine platform with audio, MIDI, database
persistence, and ML pipelines.  You don't need all of it.  Start
where you are and add layers when you need them.

- **[Core](#core)** — Pretty lights from the command line. Start here.
  - **[Server](#server)** — Always-on Pi with REST API and scheduling.
    - **[Remote Access](#remote-access)** — iOS app, Cloudflare tunnel, Home Assistant, Node-RED.
    - **[Database](#database)** — PostgreSQL logging, diagnostics, dashboard.
    - **[Distributed](#distributed)** — Multi-machine MQTT bus, SOE pipeline, agents.
      - **[Audio Pipeline](#audio-pipeline)** — Mic → FFT → lights.
      - **[MIDI Pipeline](#midi-pipeline)** — MIDI files → synth + lights + N-body visualizer.
  - **[Persistent Services](#persistent-services)** — Make any component survive reboots (systemd, launchd).
  - **[Developer](#developer)** — Write your own effects, sensors, operators, emitters.

Pick the section that matches what you want to do.  Everything
below your section is optional — you'll never see it unless you
go looking.

---

## Core

**You want:** Pretty lights on your LIFX devices from the command
line.  No server, no Pi, no network services.  Just a Mac or Linux
box and some bulbs.

**You need:** Python 3.10+, LIFX devices on your LAN.

**You get:** 33 built-in effects, live simulator preview, CLI
control, video recording of effects.

| Section | Description |
|---------|-------------|
| [Overview](01-overview.md) | What GlowUp is, what it does |
| [Requirements](02-requirements.md) | Hardware, software, and network prerequisites |
| [Quick Start](03-quick-start.md) | Get running in 60 seconds |
| [CLI Reference](04-cli-reference.md) | discover, effects, identify, play, record, replay |
| [Built-in Effects](06-effects.md) | All 33 public effects with parameter tables |
| [Live Simulator](08-simulator.md) | tkinter preview window (--sim, --sim-only) |
| [Troubleshooting](14-troubleshooting.md) | Common issues and fixes |

**What you're skipping:** Server, scheduling, remote access, iOS
app, distributed pipelines, MIDI, database.  You can always add
these later.

**Next:** [Server](#server) | [Developer](#developer)

---

## Server

**You want:** An always-on server (typically a Raspberry Pi) that
runs effects on a schedule and exposes a REST API for control.

**Requires:** [Core](#core).

**You get:** Headless daemon mode, time-based scheduling with
symbolic times (sunrise, sunset), REST API with auth, SSE live
updates.

| Section | Description |
|---------|-------------|
| [Scheduler](05-scheduler.md) | Daemon mode, config file, symbolic times, systemd |
| [REST API Server](11-rest-api.md) | server.py endpoints, auth, SSE, overrides, systemd |
| [Server Routing & Safety](25-server-routing-safety.md) | Auto-routing via server, emergency power-off, ARP keepalive |

**Back:** [Core](#core) | **Next:** [Remote Access](#remote-access) | [Database](#database) | [Distributed](#distributed)

---

## Remote Access

**You want:** To control GlowUp from your phone, from outside your
house, or from home automation platforms.

**Requires:** [Server](#server).

**You get:** iOS app, Cloudflare tunnel for secure remote access,
Home Assistant integration, macOS Dock/Finder shortcuts, Node-RED
visual flows.

> **Caveat emptor:** The GlowUp iOS app has not undergone Apple App
> Store review.  You build and install it yourself via Xcode.  The
> source code is open and available for inspection under the MIT
> License, but the software is provided **as-is**, without warranty
> of any kind — including merchantability or fitness for a particular
> purpose.  Use at your own discretion.

| Section | Description |
|---------|-------------|
| [GlowUp iOS App](12-ios-app.md) | Building, running, connectivity, app screens |
| [Cloudflare Tunnel](15-tunnel.md) | Secure remote access without port forwarding |
| [Home Assistant](16-home-assistant.md) | REST command integration with HA |
| [macOS Remote Control](17-macos-remote.md) | Shell scripts and .command files for Finder/Dock |
| [Node-RED](18-node-red.md) | Flow-based visual automation |

**Back:** [Server](#server)

---

## Database

**You want:** Persistent logging of what effects ran, when, on
which devices.  Diagnostics, history, a dashboard.

**Requires:** [Server](#server) + PostgreSQL (on a NAS, jail, or
any host).

**You get:** Effect history, device event logging, crash reports,
signal snapshots, live dashboard at `/dashboard`.

| Section | Description |
|---------|-------------|
| [Troubleshooting — PostgreSQL](14-troubleshooting.md#postgresql-setup) | Connection string, schema setup, psycopg2 install |
| [Troubleshooting — Dashboard](14-troubleshooting.md#dashboard) | Web dashboard at /dashboard, device inventory, effect history |

**Back:** [Server](#server)

---

## Distributed

**You want:** Multiple machines working together.  Sensors on one
machine, compute on another, output on a third.  The MQTT bus
connects everything.

**Requires:** [Server](#server) + MQTT broker (on the Pi).

**You get:** SOE (Sensors → Operators → Emitters) architecture,
worker agents, orchestrator, capability registration, two-tier
transport (MQTT + UDP).

| Section | Description |
|---------|-------------|
| [MQTT](19-mqtt.md) | MQTT broker setup and signal bus |
| [SOE Pipeline](21-soe-pipeline.md) | Sensors → Operators → Emitters architecture |

**Back:** [Server](#server) | **Next:** [Audio Pipeline](#audio-pipeline) | [MIDI Pipeline](#midi-pipeline)

---

## Audio Pipeline

**You want:** Music-reactive lighting.  Microphone on one machine,
FFT on a GPU node, lights respond to the beat.

**Requires:** [Distributed](#distributed) + ffmpeg + a machine with
a microphone.

| Section | Description |
|---------|-------------|
| [Media Pipeline](20-media-pipeline.md) | Mic → UDP → FFT → MQTT → lights |

**Back:** [Distributed](#distributed)

---

## MIDI Pipeline

**You want:** Play MIDI files through speakers and synchronized
lights.  Multiple stations broadcasting different music.  Switch
between stations at runtime.

**Requires:** [Distributed](#distributed) + FluidSynth + a SoundFont
file.

| Section | Description |
|---------|-------------|
| [MIDI Pipeline](23-midi-pipeline.md) | MIDI sensor, emitter, light bridge, multi-station |

**Back:** [Distributed](#distributed)

---

## Persistent Services

**You want:** Any GlowUp component to run unattended — survive
reboots, restart on failure, start on boot.

**Requires:** [Core](#core) and whichever component you want to
make persistent.

**You get:** systemd (Linux) and launchd (macOS) setup for the
server, agents, light bridge, audio emitter, MQTT broker, and any
custom component.

| Section | Description |
|---------|-------------|
| [Persistent Services](24-persistent-services.md) | systemd and launchd patterns for every component |

**Back:** [Core](#core)

---

## Developer

**You want:** To write your own effects, sensors, operators, or
emitters.  To understand the engine internals or extend the platform.

**Requires:** [Core](#core) for effects.
[Distributed](#distributed) for SOE components.

**You get:** Effect authoring framework, Param system, HSBK color
model, Emitter ABC, SynthBackend pattern, testing infrastructure,
gallery publishing.

| Section | Description |
|---------|-------------|
| [Effect Developer Guide](07-effect-dev-guide.md) | Architecture, base class, Param system, HSBK, examples |
| [Engine and Controller API](09-engine-api.md) | Programmatic API, VirtualMultizoneDevice |
| [Testing](10-testing.md) | Test modules, 780+ tests, how to run |
| [Test Interpretation Guide](TEST_GUIDE.md) | What the high-level tests prove, failure modes, triage reference (developer-level) |
| [Effect Gallery](13-gallery.md) | GitHub Pages gallery with animated GIF previews |
| [SOE Pipeline](21-soe-pipeline.md) | Architecture and extension points |
| [Emitter Developer Guide](22-emitter-dev-guide.md) | Emitter ABC, SynthBackend pattern, creating new emitters |

**Back:** [Core](#core)

---

## Full Chapter Index

Every chapter in one flat list, for reference.

| Section | Path |
|---------|------|
| [Overview](01-overview.md) | Core |
| [Requirements](02-requirements.md) | Core |
| [Quick Start](03-quick-start.md) | Core |
| [CLI Reference](04-cli-reference.md) | Core |
| [Scheduler](05-scheduler.md) | Core → Server |
| [Built-in Effects](06-effects.md) | Core |
| [Effect Developer Guide](07-effect-dev-guide.md) | Core → Developer |
| [Live Simulator](08-simulator.md) | Core |
| [Engine and Controller API](09-engine-api.md) | Core → Developer |
| [Testing](10-testing.md) | Core → Developer |
| [REST API Server](11-rest-api.md) | Core → Server |
| [GlowUp iOS App](12-ios-app.md) | Core → Server → Remote Access |
| [Effect Gallery](13-gallery.md) | Core → Developer |
| [Troubleshooting](14-troubleshooting.md) | Core |
| [Cloudflare Tunnel](15-tunnel.md) | Core → Server → Remote Access |
| [Home Assistant](16-home-assistant.md) | Core → Server → Remote Access |
| [macOS Remote Control](17-macos-remote.md) | Core → Server → Remote Access |
| [Node-RED](18-node-red.md) | Core → Server → Remote Access |
| [MQTT](19-mqtt.md) | Core → Server → Distributed |
| [Media Pipeline](20-media-pipeline.md) | Core → Server → Distributed → Audio |
| [SOE Pipeline](21-soe-pipeline.md) | Core → Server → Distributed |
| [Emitter Developer Guide](22-emitter-dev-guide.md) | Core → Developer |
| [MIDI Pipeline](23-midi-pipeline.md) | Core → Server → Distributed → MIDI |
| [Persistent Services](24-persistent-services.md) | Core → any component |
| [Server Routing & Safety](25-server-routing-safety.md) | Core → Server |
