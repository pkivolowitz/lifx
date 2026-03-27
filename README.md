<p align="center">
  <img src="logo.jpg" alt="GlowUp" width="200">
</p>

# GLOWUP — LIFX Effect Engine

A modular effect engine for LIFX devices.  Replace the phone app with
a CLI and server that run autonomously from a Raspberry Pi, Mac, or
any Linux box.

This project utilizes AI assistance (Claude 4.6) for boilerplate and
logic expansion. All architectural decisions and code integration are
by Perry Kivolowitz, the sole Human Author.

## What Do You Want to Do?

**Start here.**  Pick what sounds like you and follow that path.
Everything else is optional.

### I just want pretty lights

No server, no network services.  Just a Mac or Linux box and LIFX
bulbs on your LAN.

**You need:** Python 3.10+

```bash
python3 glowup.py discover                          # find devices
python3 glowup.py play aurora --ip <device-ip>      # run an effect
python3 glowup.py play cylon --sim-only --zones 36  # preview without hardware
```

33 built-in effects: aurora, fireworks, Newton's Cradle, cellular
automata, waving flags (199 countries), plasma, sonar, and more.
See the **[Effect Gallery](https://pkivolowitz.github.io/lifx/)**
for animated previews.

### I want music-reactive lighting

Speak, play music, clap — the lights react in real time.

**You need:** Python 3.10+ and ffmpeg

```bash
python3 glowup.py play spectrum2d --ip <device-ip>
python3 glowup.py play soundlevel --ip <device-ip>
python3 glowup.py play waveform --ip <device-ip>
```

The CLI auto-captures your local microphone via ffmpeg.  Use
`--audio-device :N` to pick a specific input (list devices with
`ffmpeg -f avfoundation -list_devices true -i ""`).

### I want 2D matrix effects on my Luna / Tiles

Matrix devices (Luna, Tile, Candle, Ceiling) get full 2D rendering.
The CLI auto-detects tile geometry and injects the correct
width/height.

**You need:** Python 3.10+ and a matrix device

```bash
python3 glowup.py play plasma2d --ip <luna-ip>       # 2D plasma field
python3 glowup.py play matrix_rain --ip <luna-ip>     # falling digital rain
python3 glowup.py play ripple2d --ip <luna-ip>        # concentric ripples
python3 glowup.py play spectrum2d --ip <luna-ip>      # audio spectrum (+ ffmpeg)
```

### I want BLE sensor-driven automation

ONVIS SMS2 motion/temperature/humidity sensors trigger effects
and automations via Bluetooth Low Energy.

**You need:** Python 3.10+, bleak, cryptography

```bash
pip install bleak cryptography
python3 -m ble.sensor --label "Hallway"
```

Publishes motion, temperature, and humidity to MQTT.  The server's
automation engine can trigger effects, power devices, and log events
based on sensor state.  See [Requirements](docs/02-requirements.md)
for the full BLE setup.

### I want an always-on server with scheduling

A headless daemon on a Pi (or any box) runs effects on a schedule,
exposes a REST API, and serves an iOS app.

**You need:** Python 3.10+ (no extra packages for the server itself)

```bash
python3 server.py server.json
```

50+ REST endpoints, time-based scheduling with symbolic times
(sunrise, sunset), SSE live updates, device registry, group CRUD,
automations, diagnostics dashboard.  See the
**[User Manual](MANUAL.md)** for full documentation.

### I want stitch devices into one animation surface

Any combination of string lights, bulbs, Neons, and beams becomes a
single virtual multizone strip.  Effects animate across all devices
as one canvas.

**You need:** Server (above) or a local config file

```bash
python3 glowup.py play cylon --group office
python3 glowup.py play aurora --group porch --config schedule.json
```

### I want MIDI-synchronized lighting

Parse and replay MIDI files through speakers and synchronized lights.
Multiple stations, runtime switching.

**You need:** paho-mqtt, MQTT broker, FluidSynth + SoundFont (for audio)

```bash
# Terminal 1 — audio
python3 -m emitters.midi_out --backend fluidsynth --soundfont gm.sf2

# Terminal 2 — lights
python3 -m distributed.midi_light_bridge --ip 192.0.2.23 192.0.2.34

# Terminal 3 — play
python3 glowup.py replay --file song.mid
```

### I want to write my own effects

**You need:** Python 3.10+, and the **[Effect Developer Guide](docs/07-effect-dev-guide.md)**

```python
class MyEffect(Effect):
    name = "my_effect"
    speed = Param(2.0, min=0.1, max=30.0, description="Cycle speed")

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        ...
```

Register it, and it's immediately available in the CLI, server, iOS
app, and scheduler.

## Documentation

The **[User Manual](MANUAL.md)** is organized as a progressive
disclosure tree — start with pretty lights, add complexity only
when you need it:

- **Core** — CLI, 33 effects, simulator, troubleshooting
- **Server** — REST API, scheduling, systemd/launchd
- **Remote Access** — iOS app, Cloudflare Tunnel, Home Assistant, Node-RED
- **Database** — PostgreSQL diagnostics, dashboard
- **Distributed** — MQTT, SOE pipeline, audio/MIDI pipelines, N-body visualizer
- **Developer** — effects, sensors, operators, emitters

<p align="center">
  <img src="multizone.PNG" alt="Virtual multizone group in iOS app" width="300">
</p>

## Requirements

Full details: **[Requirements](docs/02-requirements.md)**

The core requires **only Python 3.10+ and LIFX bulbs**.  Everything
else is opt-in:

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

## Caveat

I have tested with string lights, Neon, Luna (matrix), and monochrome
bulbs.  Please report problems — I don't own every LIFX product.
Fixes for other devices are welcome.

## License

MIT

## Appreciation

> If you find this software useful, please consider donating to a local foodbank. Even a can of soup makes a difference.
