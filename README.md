<p align="center">
  <img src="docs/assets/logo.jpg" alt="GlowUp" width="200">
</p>

# GLOWUP

**Decentralized home control platform built on Sensors, Operators, and Emitters.**

GlowUp is a self-hosted, signal-driven system for whole-home control.
Sensors feed a shared signal fabric, operators transform and fuse those
signals into decisions, and emitters express the result as light, audio,
voice actions, dashboards, or other outputs.  LIFX is the most mature
deployed emitter today, but the architecture is generalized: multiple
radio and network transports, multi-machine deployment, voice control,
and process isolation are core design goals, not add-ons.

This project utilizes AI assistance (Claude 4.6) for boilerplate and
logic expansion. All architectural decisions and code integration are
by Perry Kivolowitz, the sole Human Author.

## At a Glance

| | |
|-|-|
| **33 effects** | Aurora, fireworks, Newton's Cradle, cellular automata, 199 country flags, plasma, sonar, audio spectrum, and more |
| **SOE signal fabric** | BLE, Zigbee, Vivint, audio, screen, MIDI, REST ingest, and distributed workers all feed the same Sensors-Operators-Emitters model |
| **50+ REST endpoints** | Device control, group CRUD, scheduling, automations, media signals, diagnostics, registry, and distributed fleet operations |
| **Resilient runtime** | ARP keepalive, label-based identity, restartable services, MQTT bridges, and out-of-process adapters isolate failure domains |
| **1000+ tests** | Audit, fuzz, concurrency, REST integration, effect contracts — gated by pre-commit hook |
| **Zero required packages** | Core is pure stdlib.  Every optional dependency (ffmpeg, paho-mqtt, bleak, etc.) is guarded and documented |

## What Do You Want to Do?

Pick the entry point you care about.  Everything else composes around it.

### Pretty lights from the command line

**You need:** Python 3.10+ and LIFX bulbs on your LAN.

```bash
python3 glowup.py discover                          # find devices
python3 glowup.py play aurora --ip <device-ip>      # run an effect
python3 glowup.py play cylon --sim-only --zones 36  # preview without hardware
```

See the **[Effect Gallery](https://pkivolowitz.github.io/lifx/)**
for animated previews of all 33 effects.

### Build a distributed home-control system

Run sensors, operators, emitters, and control surfaces on different
machines.  Use MQTT or UDP for transport, isolate adapters into their
own processes, and let the SOE graph span the fleet.

**You need:** a Linux server, `paho-mqtt`, a broker, and whichever sensors or emitters you deploy

```bash
python3 server.py server.json
python3 -m distributed.worker_agent /etc/glowup/agent.json
python3 -m adapters.run_adapter --adapter zigbee --config /etc/glowup/server.json
```

### Voice-driven home interaction

Wake word, speech capture, transcription, intent execution, and speech
output are first-class parts of the platform.  Voice is another sensor
and emitter path in the same system.

**You need:** a Linux server, voice dependencies, microphone/speaker hardware, and MQTT for satellites

```bash
python3 -m voice.coordinator.daemon
python3 -m voice.satellite.daemon
python3 -m voice.speaker
```

### Music-reactive lighting

Speak, play music, clap — the lights respond in real time.  The
CLI auto-starts your microphone via ffmpeg with no configuration.

**You need:** + ffmpeg

```bash
python3 glowup.py play spectrum2d --ip <device-ip>
python3 glowup.py play waveform --ip <device-ip>
python3 glowup.py play soundlevel --ip <device-ip>
```

### 2D matrix effects (Luna, Tiles, Candle, Ceiling)

Full pixel-grid rendering with auto-detected tile geometry.

**You need:** + a matrix device

```bash
python3 glowup.py play plasma2d --ip <luna-ip>
python3 glowup.py play matrix_rain --ip <luna-ip>
python3 glowup.py play ripple2d --ip <luna-ip>
python3 glowup.py play spectrum2d --ip <luna-ip>      # + ffmpeg for audio
```

### Sensor-driven automation beyond LIFX-only use

GlowUp can ingest BLE, Zigbee, Vivint, audio, and other signals and
route them through operators and automations.  In practice, going
beyond basic LIFX control means running a Linux server to host the
coordinator, MQTT, adapters, and persistent services.

**You need:** a Linux server, plus the dependencies for the transports you actually use

```bash
pip install bleak cryptography
python3 -m ble.sensor --label "Hallway"
```

### Always-on server with scheduling

Headless coordinator with time-based scheduling (sunrise, sunset),
REST API, SSE live updates, iOS app, device registry, operators,
automation, diagnostics, and fleet coordination.

**You need:** a Linux server.  For plain LIFX control you only need Python 3.10+, but anything more ambitious should assume a Linux host.

```bash
python3 server.py server.json
```

### Virtual multizone — stitch devices into one surface

Any combination of string lights, bulbs, Neons, and beams becomes
a single animation canvas.  In GlowUp terms, this is one emitter
surface among many.

**You need:** server or local config file

```bash
python3 glowup.py play aurora --group porch
```

### MIDI-synchronized lighting

MIDI files play through speakers and synchronized lights on the
same event fabric.  Multiple stations, runtime switching, and
remote emitters all use the same distributed model.

**You need:** + paho-mqtt, mosquitto, FluidSynth

```bash
python3 -m emitters.midi_out --backend fluidsynth --soundfont gm.sf2
python3 -m distributed.midi_light_bridge --ip 192.0.2.23 192.0.2.34
python3 glowup.py replay --file song.mid
```

### Write your own operator or emitter

```python
class RollingAverage(Operator):
    operator_type = "rolling_average"
    input_signals = ["*:temperature"]
    output_signals = ["house:climate:avg_temp"]

    def on_signal(self, name: str, value: float) -> None:
        ...
```

Register it, and it becomes part of the same SOE graph.  Effects are
still there, but they are one special case inside a broader operator/
emitter framework.  See the developer docs for effects, operators,
emitters, and adapters.

## Architecture

```
Sensors ──► Operators ──► Emitters
  BLE          FFT          LIFX multizone
  Mic          Beat         LIFX single
  Screen       Threshold    LIFX matrix (tiles)
  MIDI         Blend        MIDI synth
  Camera       Delay        Audio speakers
                            WebGL (browser)
```

The SOE pipeline decouples input from output.  Any sensor can drive
any operator, and any operator can feed any emitter.  New radios,
protocols, nodes, and outputs plug in without rewriting the core.

## Documentation

The **[User Manual](docs/MANUAL.md)** is organized as a progressive
disclosure tree:

- **Core** — CLI, 33 effects, simulator, troubleshooting
- **Server** — API host, scheduling, device registry, operator runtime
- **Remote Access** — iOS app, Cloudflare Tunnel, Home Assistant, Node-RED
- **Database** — PostgreSQL diagnostics, dashboard
- **Distributed** — MQTT, SOE pipeline, worker agents, audio/MIDI pipelines
- **Developer** — effects, sensors, operators, emitters, adapters
- **[Vendor Integrations](docs/CONTRIB.md)** — example adapters for Vivint, Reolink, Brother printers, HDHomeRun

## Requirements

The smallest deployed configuration requires **only Python 3.10+ and a
LIFX device**.  The broader platform is modular: every transport,
sensor, voice, and distributed feature is opt-in.

If all you want is direct LIFX control, stop there.  If you want
scheduling, sensors, voice, MQTT, distributed workers, or resilient
always-on behavior, plan on running a Linux server.

| Feature | Additional packages |
|---------|-------------------|
| Audio-reactive effects | ffmpeg |
| Recording to GIF/MP4 | ffmpeg |
| BLE sensors | bleak, cryptography |
| Screen-reactive lighting | pygame |
| Server + scheduling | *(none)* |
| Distributed / MQTT | paho-mqtt, mosquitto |
| MIDI pipeline | paho-mqtt, pyfluidsynth, python-rtmidi |
| Database / dashboard | psycopg2 |
| Vision / camera | opencv-python |

Full details: **[Requirements](docs/02-requirements.md)**

## Caveat

Tested with LIFX string lights, Neon, Luna (700-series matrix),
and monochrome bulbs.  Please report problems — I don't own
every LIFX product.  Fixes for other devices are welcome.

## License

MIT

## Appreciation

> If you find this software useful, please consider donating to a local food pantry.  Even a single can of soup makes someone in your neighborhood's day a little easier.
