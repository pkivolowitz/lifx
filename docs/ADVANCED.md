# GlowUp — User Manual

Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
Licensed under the MIT License. See [LICENSE](../LICENSE) for details.

This project utilizes AI assistance (Claude 4.6) for boilerplate and logic
expansion. All final architectural decisions, algorithmic validation, and
code integration are performed by Perry Kivolowitz, the sole Human Author.

---

## How to Read This Manual

GlowUp scales from a single laptop driving one emitter surface to a
distributed, multi-machine home-control system.  You don't need all of it.

**Start with Part I.  Stop when you have what you need.**

| Part | What You Get | Prerequisites |
|------|-------------|---------------|
| **[I. Getting Started](#part-i--getting-started)** | First contact: CLI, one emitter, local control | Python, usually a LIFX device |
| **[II. Server](#part-ii--server)** | Always-on coordinator, API host, scheduling, sensors | Part I + a Pi |
| **[III. Integrations](#part-iii--integrations)** | iOS app, remote access, Home Assistant, Node-RED | Part II |
| **[IV. Distributed](#part-iv--distributed)** | Multi-machine SOE graph, MQTT/UDP transports, remote workers | Part II + MQTT |
| **[V. Developer](#part-v--developer)** | Write effects, sensors, operators, emitters, adapters | Part I |

---

## Part I — Getting Started

The smallest useful GlowUp setup: local control from the command line,
usually with a LIFX device as the first emitter.  No server, no Pi,
no distributed services required.

- [Overview](01-overview.md) — What GlowUp is, what it does
- [Requirements](02-requirements.md) — Hardware, software, network
- [Quick Start](03-quick-start.md) — Get running in 60 seconds
- [CLI Reference](04-cli-reference.md) — discover, effects, identify, play, record, replay
- [Built-in Effects](06-effects.md) — All 35 effects with parameter tables
- [Live Simulator](08-simulator.md) — tkinter preview window (--sim, --sim-only)
- [Troubleshooting](14-troubleshooting.md) — Common issues and fixes

---

## Part II — Server

An always-on coordinator (typically a Raspberry Pi) that runs schedules,
hosts the API, manages devices and operators, and anchors the local
SOE graph.

**Core server:**

- [Scheduler](05-scheduler.md) — Daemon mode, config file, symbolic times (sunrise/sunset)
- [REST API](11-rest-api.md) — Endpoints, auth, SSE live updates, overrides
- [Routing & Safety](25-server-routing-safety.md) — Auto-routing, emergency power-off, ARP keepalive
- [Device Registry](26-device-registry.md) — MAC-based identity, label resolution

**Sensors & adapters:**

- [Adapter Base Classes](27-adapter-base.md) — MqttAdapterBase, PollingAdapterBase, AsyncPollingAdapterBase
- [BLE Sensors](28-ble-sensors.md) — BLE sensor daemon, MQTT bridge, ONVIS
- [Zigbee Service](29-zigbee-service.md) — `glowup-zigbee-service` on broker-2; cross-host signal publish
- [Ernie Sniffer](37-ernie-sniffer.md) — BLE + TPMS capture on ernie (.153); PostgreSQL-backed persistence driving `/ernie`
- [Vendor Integrations](CONTRIB.md) — Vivint, Reolink NVR, Brother printer, HDHomeRun (contrib adapters)

**Automation & display:**

- [Automation & Triggers](30-automation.md) — Sensor-driven rules, watchdog timeouts, CRUD API
- [Operators](31-operators.md) — Signal transformers: occupancy, motion gate, triggers
- [Kiosk Display](32-kiosk.md) — /home ambient display, setup_clock.sh, portrait mode
- [Persistent Services](24-persistent-services.md) — systemd (Linux) and launchd (macOS) patterns

---

## Part III — Integrations

Connect GlowUp to phones, remote access, and external control systems.

> **Caveat emptor:** The GlowUp iOS app has not undergone Apple App
> Store review.  You build and install it yourself via Xcode.

- [GlowUp iOS App](12-ios-app.md) — Building, running, connectivity, app screens
- [Cloudflare Tunnel](15-tunnel.md) — Secure remote access without port forwarding
- [Home Assistant](16-home-assistant.md) — REST command integration with HA
- [macOS Remote Control](17-macos-remote.md) — Shell scripts and .command files for Finder/Dock
- [Node-RED](18-node-red.md) — Flow-based visual automation
- [Database & Dashboard](14-troubleshooting.md#postgresql-setup) — PostgreSQL logging, diagnostics, /dashboard

---

## Part IV — Distributed

Multiple machines working together as one SOE system.  Sensors on one
machine, compute on another, emitters on a third, voice satellites
somewhere else.

- [MQTT](19-mqtt.md) — Broker setup, signal transport, and control plane
- [SOE Pipeline](21-soe-pipeline.md) — Sensors → Operators → Emitters architecture
- [Audio Pipeline](20-media-pipeline.md) — Mic → FFT → MQTT → lights
- [MIDI Pipeline](23-midi-pipeline.md) — MIDI files → synth + lights + N-body visualizer

---

## Part V — Developer

Write your own effects, sensors, operators, emitters, or transport
adapters.  Understand the engine internals or extend the platform.

- [Effect Developer Guide](07-effect-dev-guide.md) — Architecture, base class, Param system, HSBK
- [Engine & Controller API](09-engine-api.md) — Programmatic API, VirtualMultizoneDevice
- [Emitter Developer Guide](22-emitter-dev-guide.md) — Emitter ABC, SynthBackend pattern
- [Service vs Adapter — Decision Guide](35-service-vs-adapter.md) — **READ THIS BEFORE adding any new sensor or integration.** When to write an in-process adapter vs a standalone service.
- [Testing](10-testing.md) — Test modules, 1000+ tests, how to run
- [Test Interpretation Guide](TEST_GUIDE.md) — What tests prove, failure modes, triage
- [Effect Gallery](13-gallery.md) — GitHub Pages gallery with animated GIF previews
