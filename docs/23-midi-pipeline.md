# 23 — MIDI Pipeline

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

The MIDI pipeline extends the SOE (Sensors → Operators → Emitters)
architecture with MIDI as a first-class modality.  MIDI files are
parsed, replayed onto the MQTT signal bus, and consumed by any
combination of audio emitters, light bridges, and persistence
emitters — all running as independent processes on any machine in
the network.

## Architecture

```
                                    ┌─────────────────────┐
                                    │  FluidSynth Emitter  │
                                    │  (any machine)       │
                                    │  → speakers           │
                                    └─────────┬───────────┘
                                              │ subscribe
┌──────────┐     ┌──────────────┐     ┌───────┴───────┐
│ MIDI File │ ──→ │ MIDI Sensor  │ ──→ │  MQTT Broker  │
│ (.mid)    │     │ (parser +    │     │  (Pi)         │
│           │     │  replay)     │     └───────┬───────┘
└──────────┘     └──────────────┘              │ subscribe
                                    ┌──────────┴──────────┐
                                    │  MIDI Light Bridge   │
                                    │  (any machine)       │
                                    │  → LIFX string light │
                                    └─────────────────────┘
```

All subscribers receive the same events simultaneously.  The sensor
doesn't know (or care) who is listening.

## Components

### MIDI Parser (`distributed/midi_parser.py`)

Pure-Python MIDI file parser with zero external dependencies.
Handles Standard MIDI File formats 0, 1, and 2.

**Features:**

- Variable-length quantity (VLQ) decoding
- Running status (implicit status byte reuse)
- All channel voice messages: note on/off, CC, program change,
  pitch bend, poly aftertouch, channel pressure
- Meta events: tempo, time signature, key signature, track name,
  text, markers
- System Exclusive (SysEx) events
- Tempo mapping: converts raw tick positions to wall-clock seconds

**Key classes:**

| Class | Purpose |
|-------|---------|
| `MidiParser` | Parse a `.mid` file into a list of `MidiEvent` |
| `MidiEvent` | Single event with absolute tick, time_s, and typed fields |
| `MidiHeader` | File header: format, track count, ticks per quarter |
| `TempoChange` | Tempo change point for tick-to-seconds conversion |

**Usage:**

```python
from distributed.midi_parser import MidiParser

parser = MidiParser("song.mid")
print(parser.summary())

for event in parser.events():
    print(event.to_dict())
```

### MIDI Sensor (`distributed/midi_sensor.py`)

Replays parsed MIDI events onto the MQTT signal bus at the original
tempo (or accelerated for bulk data loading).

**Signal name:** `sensor:midi:events` (configurable via `--signal-name`)

**Event format (JSON):**

```json
{"track": 1, "tick": 480, "time_s": 0.5, "event_type": "note_on",
 "channel": 0, "note": 60, "velocity": 100}
```

**Stream markers:**

- `stream_start` — published before the first event, includes file
  metadata (format, tracks, duration, tempo).
- `stream_end` — published after the last event.

**Speed control:**

| `--speed` | Behavior |
|-----------|----------|
| `1.0` (default) | Real-time at original tempo |
| `2.0` | Double speed |
| `0` | As fast as possible (bulk ingest) |

### MIDI Emitter (`emitters/midi_out.py`)

Subscribes to MIDI events on the bus and plays them through a
pluggable synthesizer backend.

**Synth backends:**

| Backend | Install | Description |
|---------|---------|-------------|
| `fluidsynth` | `brew install fluid-synth` + `pip install pyfluidsynth` | Software synth with SoundFont2.  Self-contained, no external app. |
| `rtmidi` | `pip install python-rtmidi` | Routes MIDI to virtual ports, DAWs, or hardware synths.  No sound itself. |

**Backend ABC:** `SynthBackend` — implement `start()`, `stop()`,
`note_on()`, `note_off()`, `control_change()`, `program_change()`,
`pitch_bend()`, `all_notes_off()`.  Adding a new backend is one class.

**Station switching:**

The emitter supports runtime station switching without restart.
Publish a control command to the MQTT control channel:

```bash
# Switch to a different signal (station)
mosquitto_pub -h 192.0.2.48 -t glowup/midi_emitter/control \
  -m '{"tune": "sensor:midi:jazz"}'

# Query current station
mosquitto_pub -h 192.0.2.48 -t glowup/midi_emitter/control \
  -m '{"status": true}'
```

Status is published to `glowup/midi_emitter/status`.

### MIDI Light Bridge (`distributed/midi_light_bridge.py`)

Subscribes to the same MIDI events and translates them into colors
on LIFX multizone devices (string lights, beams, neon flex).

**Virtual multizone:** Pass multiple ``--ip`` arguments to combine
devices into a single virtual strip.  Zones are concatenated in
order — a Neon (24 zones) + String (36 zones) = 60-zone strip.
More devices = more spatial resolution = finer musical detail.

```bash
# Single device
python3 -m distributed.midi_light_bridge --ip 192.0.2.34

# Virtual strip — two devices stitched together
python3 -m distributed.midi_light_bridge --ip 192.0.2.34 192.0.2.23
```

**Note tracking:** Lights follow MIDI note on/off exactly — a held
note stays lit for its full duration, and goes dark on note_off.
No fixed decay timer.  This matches how the audio emitter works.

**Mapping:**

| MIDI property | Light property |
|---------------|----------------|
| Note pitch | Zone position (low=left, high=right) |
| Velocity | Brightness |
| Channel | Hue (each channel gets a distinct color) |
| Note off | Zone goes dark (immediate) |

**Default channel colors:**

| Channel | Instrument (BWV 565) | Color |
|---------|---------------------|-------|
| 0 | Swell organ | Red |
| 1 | Great organ | Blue |
| 2 | Pedal organ | Green |

**Device power:** The bridge automatically powers on each device
during discovery.  No need to turn lights on manually first.

**Rendering:** A dedicated render thread pushes zone colors to all
devices at a steady frame rate (default 15 fps), decoupled from
the MIDI event rate.

### N-body Particle Visualizer (`distributed/nbody_visualizer.py`)

A combined operator + WebGL emitter that turns MIDI events into a
particle simulation rendered in the browser.  Each note_on spawns
a cluster of particles; gravity pulls them together, same-charge
repulsion pushes them apart.  The result looks like atomic
spectroscopy driven by music.

```bash
# O(n) independent particles (smooth on any machine)
python3 -m distributed.nbody_visualizer --particles-per-note 50

# O(n²) pairwise forces (GPU stress test)
python3 -m distributed.nbody_visualizer --particles-per-note 30 \
  --max-particles 2000 --forces
```

Open `http://localhost:8421` in a browser.  The WebGL page polls
for frames via HTTP — no WebSocket needed.

**Parameters:**

| Flag | Default | Description |
|------|---------|-------------|
| `--particles-per-note` | 50 | Particles spawned per note_on |
| `--max-particles` | 5000 | Total particle cap |
| `--fps` | 20 | Simulation and publish rate |
| `--forces` | off | Enable O(n²) gravitational + electrostatic forces |
| `--http-port` | 8421 | Browser page port |

Without `--forces`, particles fall with gravity, bounce off walls,
and fade out independently (O(n) — smooth on any hardware).  With
`--forces`, particles attract via gravity and repel same-charge via
electrostatics (O(n²) — needs GPU for large particle counts).

For distributed operation (compute on a GPU node, display anywhere),
use `distributed/nbody_operator.py` + `distributed/webgl_emitter.py`
as separate processes connected via MQTT.

### PostgreSQL Schema (`sql/midi_events.sql`)

Structured storage for MIDI events — one row per event, fully
queryable.

```sql
SELECT * FROM midi_events WHERE note = 60 AND event_type = 'note_on';
SELECT source_file, count(*) FROM midi_events GROUP BY source_file;
```

The schema is applied to the `glowup` database on the PostgreSQL
jail (192.0.2.42).  Events are stored by the persistence emitter
when it subscribes to `sensor:midi:events` — the sensor never
writes to the database directly.

## CLI

### `replay` verb

Added to `glowup.py` as a reusable verb:

```bash
# Real-time replay (drives lights + audio simultaneously)
python3 glowup.py replay --file song.mid

# Bulk ingest (as fast as possible, for data loading)
python3 glowup.py replay --file song.mid --speed 0

# Double speed
python3 glowup.py replay --file song.mid --speed 2

# Custom broker and signal name
python3 glowup.py replay --file song.mid --broker 192.0.2.48 \
  --signal-name sensor:midi:bach
```

### Standalone module entry points

```bash
# MIDI sensor (direct, without glowup.py wrapper)
python3 -m distributed.midi_sensor --file song.mid --broker 192.0.2.48

# MIDI audio emitter
python3 -m emitters.midi_out --backend fluidsynth \
  --soundfont /path/to/gm.sf2

# MIDI light bridge
python3 -m distributed.midi_light_bridge --ip 192.0.2.62
```

## Quick Start — Full Pipeline

Three terminals, any machine(s) on the network:

**Terminal 1 — Audio emitter:**
```bash
python3 -m emitters.midi_out --backend fluidsynth \
  --soundfont ~/Downloads/FluidR3_GM.sf2 --gain 2.0
```

**Terminal 2 — Light bridge:**
```bash
python3 -m distributed.midi_light_bridge --ip 192.0.2.62
```

**Terminal 3 — Replay:**
```bash
python3 glowup.py replay --file song.mid
```

The sensor publishes to the MQTT bus.  Both the audio emitter and
the light bridge subscribe independently.  Sound and light are
synchronized — driven by the same event stream.

## Multi-Station Broadcasting

Different sensors can broadcast on different signal names
(stations).  Emitters tune in to whichever station they want:

```bash
# Station 1 — Bach on Conway
python3 glowup.py replay --file bach.mid --signal-name sensor:midi:bach

# Station 2 — Jazz on Bed
python3 glowup.py replay --file jazz.mid --signal-name sensor:midi:jazz

# Listener tunes to bach
python3 -m emitters.midi_out --backend fluidsynth \
  --soundfont ~/Downloads/FluidR3_GM.sf2 \
  --signal-name sensor:midi:bach

# Switch to jazz at runtime (no restart)
mosquitto_pub -h 192.0.2.48 -t glowup/midi_emitter/control \
  -m '{"tune": "sensor:midi:jazz"}'
```

## Dependencies

| Component | Requires | Install |
|-----------|----------|---------|
| Parser | (none) | Built-in |
| Sensor | paho-mqtt | `pip install paho-mqtt` |
| Emitter (fluidsynth) | pyfluidsynth, fluid-synth | `brew install fluid-synth && pip install pyfluidsynth` |
| Emitter (rtmidi) | python-rtmidi | `pip install python-rtmidi` |
| Light bridge | paho-mqtt | `pip install paho-mqtt` |
| SoundFont | FluidR3_GM_GS.sf2 | [archive.org](https://archive.org/download/fluidr3-gm-gs/FluidR3_GM_GS.sf2) |
| MIDI files | Standard MIDI Files (.mid) | User-supplied — search for "free MIDI files" or convert from sheet music |

**MIDI files:** GlowUp does not ship MIDI files.  Standard MIDI
Files (.mid) are widely available online — search for "free MIDI
files" for classical, jazz, pop, and game music.  Any SMF format
0 or 1 file will work.

**SoundFont quality:** The FluidR3_GM_GS soundfont (144 MB) is a
good general-purpose choice.  For specific genres (jazz guitar,
orchestral brass), specialized soundfonts will sound better.
The `--soundfont` flag makes it easy to swap.

**Linux install:** On Debian/Ubuntu, use `sudo apt install fluidsynth`
and `pip install pyfluidsynth` (the brew command above is for macOS).

## Design Decisions

- **MQTT, not UDP** — MIDI events are tiny and infrequent compared to
  PCM audio.  MQTT provides pub/sub fanout (multiple listeners) without
  target IP configuration.  The tradeoff (broker hop) adds < 1ms on LAN.

- **Parser separated from sensor** — the parser yields `MidiEvent`
  dataclasses from bytes.  The sensor wires those to MQTT.  A future
  database replay source or live MIDI device sensor reuses the same
  event format.

- **Synth backend is pluggable** — the emitter framework handles bus
  subscription and event dispatch.  Sound production is delegated to
  a `SynthBackend` ABC.  Adding a new backend is one class with six
  methods.

- **Persistence is orthogonal** — the sensor never writes to the
  database.  If the persistence emitter is subscribed, storage happens
  automatically.  `--persist` was explicitly rejected during design.

- **Station switching at runtime** — emitters can change their MQTT
  subscription without restart via a control channel.  Multiple
  stations can broadcast simultaneously.

- **FluidSynth API pitfalls** — three lessons learned:
  (1) Use ``program_change()``, not ``program_select()`` — the latter
  hardcodes bank 0, ignoring bank select CCs the MIDI file already sent.
  (2) pyfluidsynth's ``pitch_bend()`` expects -8192..+8191 (center=0),
  but MIDI wire format is 0..16383 (center=8192) — subtract 8192 before
  calling.  (3) Call ``sfload(path, update_midi_preset=1)`` so FluidSynth
  assigns the soundfont to all channels on load.

- **Note tracking, not decay** — the light bridge tracks note_on/off
  directly.  A held note stays lit for its full duration.  No fixed
  decay timer.  This matches how the audio emitter works — the light
  follows the music, not an approximation of it.

- **Virtual multizone for lights** — multiple ``--ip`` arguments
  combine devices into a single strip.  More devices = more zones =
  higher spatial resolution for the music.  Devices are auto-powered
  on during discovery.
