# Requirements

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

## What You Need Depends on What You Want

GlowUp is modular.  The core requires nothing beyond Python and
LIFX bulbs.  Each additional capability has its own dependencies.
Install only what you need.

### Core (pretty lights from the CLI)

| Requirement | Install |
|-------------|---------|
| Python 3.10+ | macOS: `brew install python@3.12` · Linux: included · Windows: [python.org](https://www.python.org/downloads/) |
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

### Server (REST API, scheduling)

No additional packages beyond core.  Just run `python3 server.py server.json`.

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
| MIDI files (.mid) | User-supplied — search for "free MIDI files" |

### N-body Visualizer (WebGL particle display)

| Requirement | Install |
|-------------|---------|
| numpy | `pip install numpy` |
| paho-mqtt | `pip install paho-mqtt` |
| MQTT broker | See MQTT above |
| A web browser | Any modern browser with WebGL |

---

## Platform Support

| Platform | Status | Notes |
|----------|--------|-------|
| **macOS** | Fully supported | Primary development platform |
| **Linux (Pi, Ubuntu, etc.)** | Fully supported | Recommended deployment target |
| **Windows** | Untested | Use `--ip` for discovery; effects and server should work |

## Platform-Specific Setup

### macOS

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

> **Windows support has not been tested.**  If you try it, please
> report results via a GitHub issue.

Install Python 3.10+ from [python.org](https://www.python.org/downloads/)
(tkinter is included).  Discovery requires `--ip` to address
devices directly — broadcast auto-detection uses Unix-specific
calls that are unavailable on Windows.

```bash
python glowup.py play aurora --ip 10.0.0.62
python glowup.py play aurora --sim-only --zones 36
```
