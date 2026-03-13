# Media Pipeline

The media pipeline turns GlowUp from a static effect engine into a
real-time reactive lighting platform.  Any audio or video source —
camera microphones, iPhone mics, media files — can drive any effect
on any surface.

## Design Philosophy: Mosaic Warfare

The architecture is inspired by the US military's **Mosaic Warfare**
doctrine: *Any Sensor, Any Decider, Any Shooter*.  In GlowUp terms:

- **Sensor**: Any source of signals — iPhone microphone, RTSP camera
  audio, file, ESP32, future video feeds.
- **Effect**: Any effect that reads from the signal bus — purpose-built
  audio visualizers or existing effects with parameter bindings.
- **Surface**: Any LIFX device or virtual multizone group.

The three form a triangle, not a pipeline.  There is no forced ordering.
A user can start from any vertex — pick a device first, or browse
effects first, or choose a sensor first — and compose the other two.
The iOS app's hub screen reflects this: all three pickers are always
visible.

This decoupling means the number of useful configurations is
*sensors x effects x surfaces*, not a fixed list of presets.

## Architecture

```
Any Node (Pi, Mac, Jetson, Cloud)
  MediaSource ──> SignalExtractor ──> SignalBus
  (ffmpeg pipe)   (FFT, bands,       (thread-safe dict
                   beat, centroid)     + optional MQTT)

                         │
              ┌──────────▼───────────┐
              │    GlowUp Server     │
              │                      │
              │  Engine reads bus    │
              │  Binds signals to   │
              │  effect params      │
              │  Renders frames     │
              │  Sends UDP to LIFX  │
              └──────────────────────┘
```

## Five Layers

### 1. SignalBus (`media/__init__.py`)

Thread-safe dictionary of named signals.  Everything writes here,
everything reads here.

- Signal names are hierarchical: `{source}:audio:{signal}`
  (e.g., `foyer:audio:bass`, `iphone:audio:bands`)
- Scalar values are normalized to [0.0, 1.0]
- Array values (frequency bands) for per-zone mapping
- Optional MQTT bridge publishes/subscribes for distributed nodes

### 2. MediaSource (`media/source.py`)

Abstract base for raw media data producers.

- **RtspSource**: wraps `ffmpeg -i rtsp://... -f s16le -ar 16000 pipe:1`
- Reconnects on EOF with exponential backoff (1s to 60s cap)
- Credential isolation: RTSP URLs use `{user}:{password}` placeholders
  resolved from a chmod-600 credentials file at startup

### 3. SignalExtractor (`media/extractors.py`)

Computes named signals from raw audio and writes to the bus.

- Ring buffer accumulates PCM samples
- Every 1024 samples (64ms at 16kHz): Hann window, radix-2 FFT,
  magnitude spectrum
- Logarithmic frequency binning into 8 bands
- Exponential moving average smoothing with global peak normalization
- Beat detection via energy ratio against recent average

Writes per source: `bands` (array), `bass`, `mid`, `treble`, `rms`,
`energy`, `beat`, `centroid`.

### 4. FFT (`media/fft.py`)

Dual-path implementation with optional dependency acceleration:

| Dependency | Speed (1024 samples, Pi 4) | Fallback |
|------------|---------------------------|----------|
| numpy | ~0.1ms | Pure-Python `cmath` radix-2 Cooley-Tukey (~3ms) |
| scipy.signal | Advanced windowing | Manual Hann window |

No hard dependencies.  Pure Python works; numpy is recommended.

### 5. MediaManager (`media/__init__.py`)

Lifecycle orchestration.  Reference-counted source management:

- `acquire(name)` — start source on first reference
- `release(name)` — stop after idle timeout when unreferenced
- Prevents wasting CPU on unused camera streams

## Audio-Reactive Effects

### soundlevel

Maps a single signal (RMS by default) to all zones uniformly.
Simple volume-to-brightness mapping.  Good for ambient reactive
lighting where frequency detail isn't needed.

### waveform

Maps the 8 frequency bands to zones proportionally — low frequencies
(bass, sub) on one end, high frequencies (treble, air) on the other.
Each zone's hue corresponds to its frequency range (warm for bass,
cool for treble).  Includes a noise gate to suppress ambient room
noise.

## iPhone as a Sensor

The iOS app includes `AudioStreamService` — a complete audio sensor
node that turns the iPhone into a low-latency microphone for the
media pipeline.

1. AVAudioEngine captures mic audio at 16kHz mono
2. Accelerate framework (vDSP) computes hardware-accelerated FFT
3. Logarithmic 8-band frequency binning matches server-side processing
4. Beat detection, spectral centroid, derived bass/mid/treble signals
5. HTTP POST to `/api/media/signals/ingest` at ~15 Hz

The server's ingest endpoint writes directly to the SignalBus.
Any effect using `source: "iphone"` responds immediately.

Latency: near-instantaneous (one FFT window + HTTP round trip).
Compare to RTSP camera audio: ~2 seconds due to AAC encoder buffering.

## Server Configuration

### Media Sources

Add a `media_sources` block to `server.json`:

```json
{
    "media_sources": {
        "foyer": {
            "type": "rtsp",
            "url": "rtsp://{user}:{password}@10.0.0.39:554/Preview_01_main",
            "stream": "audio",
            "credentials_file": "/etc/glowup/rtsp_creds.json"
        }
    }
}
```

### Credentials File

Keep NVR passwords out of `server.json`:

```json
{
    "user": "admin",
    "password": "your-password-here"
}
```

Set permissions: `chmod 600 /etc/glowup/rtsp_creds.json`.
The server resolves `{user}` and `{password}` placeholders at startup
and warns if file permissions are too open.

### Optional Dependencies

| Tier | Install | What it enables |
|------|---------|-----------------|
| 0 (vanilla) | Nothing | Pure-Python FFT, everything works |
| 1 (recommended) | `pip install numpy` | 30x FFT speedup |
| 2 (full audio) | `pip install numpy scipy` | Advanced spectral analysis |
| 3 (video) | `pip install numpy opencv-python` | Future video extraction |
| 4 (distributed) | `pip install paho-mqtt` | Multi-node signal sharing |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/media/sources` | List sources with status (never exposes URLs) |
| `GET` | `/api/media/signals` | List available signal names |
| `POST` | `/api/media/sources/{name}/start` | Manually start a source |
| `POST` | `/api/media/sources/{name}/stop` | Manually stop a source |
| `POST` | `/api/media/signals/ingest` | Write signals from external source |

### Ingest Endpoint

Any HTTP client can write signals to the bus:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "source": "iphone",
       "signals": {
         "rms": 0.7,
         "bands": [0.1, 0.3, 0.8, 0.5, 0.2, 0.1, 0.05, 0.02],
         "beat": 1.0,
         "bass": 0.5
       }
     }' \
     http://localhost:8420/api/media/signals/ingest
```

Signals are written as `{source}:audio:{name}` — so the above creates
`iphone:audio:rms`, `iphone:audio:bands`, etc.

## Distributed Architecture

The SignalBus MQTT bridge enables multi-node deployment:

```
  Jetson (GPU)              Cloud GPU
  - Camera feeds            - ML inference
  - Video extraction        - Object detection
  - Publishes via MQTT      - Publishes via MQTT
        │                         │
        └────────┬────────────────┘
                 │
          MQTT Broker (Pi)
                 │
          GlowUp Server (Pi)
          - Reads signals from bus
          - Renders effects
          - Sends UDP to LIFX
```

Any node that can run Python + ffmpeg can be a media source node.
The GlowUp server doesn't care where signals originate — it reads
the bus.

## iOS Hub Screen

The app's main screen is the **Mosaic Warfare triangle**: three
always-visible pickers for Sensor, Effect, and Surface.  The user
picks any vertex first.

- **Sensor selected first**: filters effects to matching type
  (audio sensors show audio effects, "None" shows all others)
- **Effect selected first**: all effects visible until sensor narrows
- **Surface**: always independent, pick any time

Section headers show current selection with a clear button for
changing your mind.  A Go/Stop button appears when all three vertices
are filled.

Navigation to Devices, Schedule, and Settings is at the bottom of
the hub.

## Security

- RTSP URLs with NVR credentials are **never** exposed via any API
- `/api/media/sources` returns name, type, status — never URLs
- Credentials file is separate from server config, chmod 600
- Signal names are safe hierarchical strings
