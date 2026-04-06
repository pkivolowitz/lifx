# Requirements

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

## What You Need Depends on What You Want

GlowUp is modular.  The smallest deployment requires nothing beyond
Python and a LIFX device, but the overall platform is broader than
lighting.  Each transport, sensor family, voice feature, and
distributed capability has its own dependencies.
Install only what you need.

A multiplatform installer is in development.  Today, setup is still
manual.

## Where Serverless Ends

You can stay **serverless** if all you want is direct LIFX control from
the CLI:

- discovery
- playing effects by IP
- simulator preview
- recording renders

That mode stops the moment you want the system to be **always on** or
to coordinate anything beyond direct one-machine LIFX control.

You should plan on a **Linux server** when you want any of the following:

- scheduling
- label-based device resolution and registry-backed identity
- MQTT
- sensors or adapters
- voice control
- distributed workers
- dashboards, remote access, or persistent services

### Core (single-node starter deployment)

| Requirement | Install |
|-------------|---------|
| Python 3.10+ | Minimum supported version on every platform. macOS example: `brew install python@3.12` · Linux: included · Windows: [python.org](https://www.python.org/downloads/) |
| LIFX devices on your LAN | Any multizone, single color, or monochrome bulb |

No external Python packages — the core is pure stdlib.

### Simulator preview (`--sim`, `--sim-only`)

| Requirement | Install |
|-------------|---------|
| tkinter | macOS: included · Linux: `sudo apt install python3-tk` · Windows: included |

### Recording effects to GIF/MP4/WebM (`record`)

| Requirement | Install |
|-------------|---------|
| ffmpeg | macOS: `brew install ffmpeg` · Linux: `sudo apt install ffmpeg` |

### Server (API host, scheduling, SOE coordination)

This is the line where GlowUp stops being serverless.  If you want
always-on coordination, scheduling, sensors, MQTT, voice, or distributed
execution, run a Linux server.

MQTT is required for the servered system to function as intended.  Plan
on a Linux server, `paho-mqtt`, and a broker.

| Requirement | Install |
|-------------|---------|
| Python 3.10+ | Same minimum as core |
| Linux server | Raspberry Pi, Ubuntu box, mini PC, or other always-on Linux host |
| MQTT broker | `sudo apt install mosquitto` or equivalent |
| paho-mqtt | `pip install paho-mqtt` |

Then run `python3 server.py server.json`.

To run the server (or any component) as a persistent service that
survives reboots, see [Persistent Services](24-persistent-services.md).

### Database (diagnostics, dashboard)

| Requirement | Install |
|-------------|---------|
| Server (above) | Running `server.py` |
| psycopg2 | `pip install psycopg2-binary` |
| PostgreSQL | Any accessible PostgreSQL instance (NAS, Docker, cloud) |

### Remote Access (iOS app, Cloudflare Tunnel)

| Requirement | Install |
|-------------|---------|
| Server (above) | Running `server.py` |
| cloudflared *(for tunnel)* | See [Cloudflare Tunnel](15-tunnel.md) |
| Xcode *(for iOS app)* | App Store |

### Audio Output (theremin, audio effects, speakers)

| Requirement | Install |
|-------------|---------|
| sounddevice | `pip install sounddevice` |
| numpy | `pip install numpy` |

### BLE Sensors (example: ONVIS motion/temperature)

| Requirement | Install |
|-------------|---------|
| bleak | `pip install bleak` |
| cryptography | `pip install cryptography` |

### Screen-Reactive Lighting (`screen_light` effect)

| Requirement | Install |
|-------------|---------|
| pygame | `pip install pygame` |

### Vision / Camera Media Source

| Requirement | Install |
|-------------|---------|
| opencv | `pip install opencv-python` |

### MQTT / Distributed Framework

| Requirement | Install |
|-------------|---------|
| MQTT broker | macOS: `brew install mosquitto` · Linux: `sudo apt install mosquitto` |
| paho-mqtt | `pip install paho-mqtt` |

### MIDI Pipeline (replay, audio, lights)

| Requirement | Install |
|-------------|---------|
| paho-mqtt | `pip install paho-mqtt` |
| MQTT broker | See MQTT above |
| FluidSynth *(for audio playback)* | macOS: `brew install fluid-synth` · Linux: `sudo apt install fluidsynth` |
| pyfluidsynth *(for audio playback)* | `pip install pyfluidsynth` |
| SoundFont (.sf2 file) | [FluidR3_GM_GS](https://archive.org/download/fluidr3-gm-gs/FluidR3_GM_GS.sf2) (144 MB, free) |
| python-rtmidi *(alternative to FluidSynth)* | `pip install python-rtmidi` |
| MIDI files (.mid) | User-supplied — search for "free MIDI files" |

### N-body Visualizer (WebGL particle display)

| Requirement | Install |
|-------------|---------|
| numpy | `pip install numpy` |
| cupy *(optional GPU acceleration)* | `pip install cupy` — falls back to numpy |
| scipy *(optional audio filtering)* | `pip install scipy` — optional enhancement |
| paho-mqtt | `pip install paho-mqtt` |
| MQTT broker | See MQTT above |
| A web browser | Any modern browser with WebGL |

Best results come from an NVIDIA 4XXX-class GPU.  Lower-end GPUs and
CPU-only fallback paths can work, but this feature is happiest on a
machine with real graphics headroom.

### Voice Control

| Requirement | Install |
|-------------|---------|
| MQTT broker | See MQTT above |
| `paho-mqtt` | `pip install paho-mqtt` |
| Piper *(recommended voice output)* | install `piper` / `piper-tts` for local speech output |
| microphone / speaker hardware | depends on deployment |
| Pi 4-class satellite or better | practical minimum for a useful always-on satellite node |
| speech dependencies | install the packages required by your chosen voice path |

---

## Platform Support

| Platform | Status | Notes |
|----------|--------|-------|
| **macOS** | Mature | Primary development platform; strong support for local and development workflows |
| **Linux (Pi, Ubuntu, etc.)** | Mature | Recommended deployment target for servers, adapters, MQTT, and persistent services |
| **Windows** | In its infancy | Start with direct `--ip` usage; expect rough edges |

## Platform-Specific Setup

### macOS

Python 3.10 is the minimum supported version.  Homebrew Python 3.12 is
just an example of a current installation target, not a stricter
requirement.

```bash
brew install python@3.12
```

### Linux (Debian / Ubuntu / Raspberry Pi OS)

```bash
sudo apt update
sudo apt install python3 python3-tk python3.12-venv
```

Create a virtual environment (Ubuntu blocks system-wide pip
installs):

```bash
python3 -m venv ~/venv
~/venv/bin/pip install --upgrade pip
```

Then use `~/venv/bin/python3` or activate with
`source ~/venv/bin/activate`.

On Raspberry Pi OS (Bookworm), Python 3.11+ is included.  Install
tkinter only if using the simulator on a desktop — headless
deployments don't need it.

### Windows

> **Windows support is in its infancy.**  Basic direct-device workflows
> are the right starting point.  If you try broader usage, please report
> results via a GitHub issue.

Install Python 3.10+ from [python.org](https://www.python.org/downloads/)
(tkinter is included).  Discovery requires `--ip` to address
devices directly — broadcast auto-detection uses Unix-specific
calls that are unavailable on Windows.

```bash
python glowup.py play aurora --ip 192.0.2.62
python glowup.py play aurora --sim-only --zones 36
```
