# SOE Pipeline Architecture

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

The SOE pipeline — **Sensors, Operators, Emitters** — is the core
abstraction that makes GlowUp a general-purpose platform rather than
a lighting controller.  It is the fifth iteration of the
**Loaders → Operators → Savers** architecture that created Desktop
Publishing, Multimedia, Desktop Video, and Desktop Film in the 1990s.
This time the architecture is applied to physical-world sensors and
distributed compute instead of pixels and files.

## The Insight

LIFX lights are one emitter among many.  A relay, a speaker, a
database, a Slack webhook, an 8K display — these are all emitters.
An iPhone microphone, an RTSP camera, an ESP32 temperature sensor,
a medical device — these are all sensors.  An FFT, an ML classifier,
a rolling average, a Kalman filter — these are all operators.

The power is in **composability**.  Users compose pipelines from
primitives to create workflows the platform designers never imagined.
The number of useful configurations is *sensors × operators × emitters*,
not a fixed list of presets.

## Three Stages

```
┌───────────┐     ┌────────────┐     ┌───────────┐
│  Sensors  │────▶│  Operators │────▶│  Emitters  │
│           │     │            │     │            │
│ Capture   │     │ Transform  │     │ Express    │
│ the world │     │ the data   │     │ the result │
└───────────┘     └────────────┘     └───────────┘
```

### Sensors

Input nodes that capture data from the environment and write named
signals to the **SignalBus**.

| Sensor | Implementation | Status |
|--------|---------------|--------|
| RTSP camera audio | `media/source.py` → `RtspSource` | Deployed |
| iPhone microphone | iOS `AudioStreamService` → HTTP ingest | Deployed |
| Signal ingest API | `POST /api/media/signals/ingest` | Deployed |
| Distributed agents | Jetson workers via MQTT | Deployed (Judy) |
| ESP32 sensor nodes | UDP → SignalBus bridge | Designed |
| Medical devices | MQTT / serial | Planned |

Sensors are **lazy** — they only start when something downstream
needs their data.  The MediaManager reference-counts acquisitions
and stops sources after an idle timeout when unreferenced.

### Operators

Compute and transform nodes that process sensor data into meaningful
signals.  Operators run wherever compute is available: the Pi, a
Jetson, the ML box, or the cloud.

| Operator | Location | What it does |
|----------|----------|-------------|
| SignalExtractor | Pi / any node | FFT, 8-band binning, beat detection, spectral centroid |
| Effect engine | Pi | Renders HSBK frames from signals + time |
| ML inference | Jetson / ML box | Object detection, acoustic classification |
| Kalman filter | Any | Sensor fusion, noise rejection |
| Schema transform | Any | Reshape signals for specific emitters |

Operators are **composable** — they chain and loop.  An FFT feeds a
beat detector; a beat detector feeds an ML classifier; a classifier
feeds a threshold operator that gates an emitter.

### Emitters

Output nodes that express processed results in a specific medium.
The Emitter ABC (`emitters/__init__.py`) defines the lifecycle
contract; concrete implementations handle the protocol details.

| Emitter | Type | Frame type | Status |
|---------|------|-----------|--------|
| LIFX devices | `lifx` | `list[HSBK]` / `HSBK` | Deployed |
| Virtual multizone | — | `list[HSBK]` | Deployed |
| Screen simulator | — | `list[HSBK]` | Deployed |
| Screen matrix | — | `list[list[HSBK]]` | Deployed |
| Persistence (CSV/DB) | `csv`, `sqlite` | `dict[str, float]` | Designed |
| Webhook | `webhook` | `dict[str, Any]` | Planned |
| GPIO relay | `relay` | `bool` | Planned |
| DMX512 | `dmx` | `list[int]` | Planned |
| Speaker / audio out | `audio` | `bytes` | Planned |

Emitters are **output-agnostic** — the pipeline doesn't care whether
the result drives photons, electrons, or HTTP requests.

## The SignalBus

The SignalBus (`media/__init__.py`) is the central nervous system.
It is a thread-safe dictionary of named signals that decouples
sensors from operators and operators from emitters.

```
  Sensors write ──▶  SignalBus  ◀── Operators and Emitters read
                   (thread-safe)
                        │
                   MQTT bridge
                   (optional)
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
         Remote nodes        Remote nodes
```

Signal names are hierarchical: `{source}:{domain}:{signal}` — e.g.,
`foyer:audio:bass`, `judy:video:person_count`, `esp32_porch:env:temp`.

Scalar signals are normalized to [0.0, 1.0].  Array signals (frequency
bands, pixel rows) carry their natural range.  The optional MQTT bridge
publishes and subscribes, enabling multi-node deployments where sensors,
operators, and emitters live on different machines.

## Dispatch Modes

The EmitterManager (`emitters/__init__.py`) orchestrates emitter
lifecycles and supports three timing modes:

### Continuous

Dispatched from the Engine's send thread on every rendered frame.
The emitter receives the frame produced by the operator (typically
`list[HSBK]`).  This is the mode used by LIFX devices — the emitter
must return quickly (UDP fire-and-forget).

```json
{
    "type": "lifx",
    "timing": "continuous"
}
```

### Periodic

Dispatched by the EmitterManager's daemon thread at a configured
rate.  The frame is a **signal snapshot** — a dict of signal names
and their current values read from the bus.  Use this for emitters
that do I/O (database writes, API calls, file appends) that would
block the engine's send thread.

```json
{
    "type": "csv",
    "timing": "periodic",
    "rate_hz": 5,
    "signals": ["foyer:audio:*"]
}
```

### Event-Driven

Dispatched by the daemon thread when a trigger signal crosses a
threshold.  Supports edge detection (rising, falling, any) and
cooldown to prevent alert storms.  Ideal for notifications,
safety alerts, and state-change logging.

```json
{
    "type": "webhook",
    "timing": "event",
    "trigger_signal": "house:safety:fall",
    "trigger_threshold": 0.8,
    "trigger_edge": "rising",
    "cooldown_seconds": 300
}
```

## Lifecycle

Every emitter follows the same lifecycle, managed by the
EmitterManager:

```
  configure()          Allocate, discover, connect.
       │
       ▼
    open()             Acquire resources, prepare for first emit.
       │
       ▼
  ┌─ emit() ◀─┐       Receive frames (continuous, periodic, or event).
  │            │
  │  flush()   │       Periodically flush buffered output.
  │     │      │
  └─────┴──────┘
       │
       ▼
    close()            Release all resources.
```

The manager tracks consecutive failures per emitter and auto-disables
after 10 consecutive `on_emit()` failures.  Disabled emitters can be
re-enabled via the API without restarting the server.

## Mosaic Warfare

The design philosophy is borrowed from the US military's **Mosaic
Warfare** doctrine: *Any Sensor, Any Decider, Any Shooter*.  In
GlowUp terms: **Any Sensor, Any Operator, Any Emitter**.

The three form a triangle, not a fixed pipeline.  A user can start
from any vertex — pick a device first, browse effects first, or
choose a sensor first — and compose the other two.  The iOS app's
hub screen reflects this: all three pickers are always visible.

```
        Sensor
       ╱      ╲
      ╱        ╲
   Effect ─── Surface
```

This decoupling means every new sensor multiplies the value of every
existing operator and emitter, and vice versa.  A single new sensor
(say, a thermal camera) instantly composes with every existing
operator and every existing emitter — no integration code required.

## Distributed Deployment

The SignalBus MQTT bridge enables multi-node deployment where each
stage runs on the most appropriate hardware:

```
  Conway (Mac)              Judy (Jetson)           ML Box (AMD/RTX)
  ┌──────────┐              ┌──────────┐           ┌──────────┐
  │ iPhone   │              │ Camera   │           │ ML model │
  │ mic      │──UDP──▶      │ FFT      │──MQTT──▶  │ inference│──MQTT──▶
  └──────────┘              └──────────┘           └──────────┘
                                    │
                                    ▼
                            ┌──────────────┐
                            │  Pi (Server) │
                            │  SignalBus   │
                            │  Engine      │
                            │  Emitters    │
                            └──────────────┘
                                    │
                              UDP to LIFX
```

Any node that can run Python can be a sensor or operator node.
The GlowUp server doesn't care where signals originate — it reads
the bus.

## The Data Flywheel

The Persistence Emitter (planned) closes the loop.  Every deployment
simultaneously serves users *and* collects structured data.  More
deployments produce more data, which trains better operators (ML
models), which deliver more value, which drives more deployments.

Persisted data can optionally become a **metasensor** — replaying
historical signals through new operators for offline analysis,
A/B testing, and model training without needing live hardware.

## What SOE Is Not

SOE is not a lighting feature.  It is a **generalized sensor fusion,
effectuation, and distributed computation platform**.  LIFX lights
are one emitter among many.  The theremin was a pipeline proof of
concept, not a goal.  The real product is the composable pipeline
that drops into eldercare, acoustic detection, industrial monitoring,
entertainment — any domain where sensors feed compute that drives
action.

## Sensor Adapters

Sensor adapters bridge external data sources into the SOE pipeline
via the SignalBus.  All adapters inherit from a common base:

```python
class SensorAdapter(AdapterBase):
```

SensorAdapter inherits from `AdapterBase` (defined in `adapter_base.py`),
which provides the `_running` flag, `running` property, and abstract
`start()`/`stop()` lifecycle.  Three transport-specific base classes
handle lifecycle boilerplate: `MqttAdapterBase`, `PollingAdapterBase`,
and `AsyncPollingAdapterBase`.

| Adapter | Source | Base class | Status |
|---------|--------|-----------|--------|
| BleAdapter | ONVIS BLE sensors | MqttAdapterBase | Deployed |
| ZigbeeAdapter | Zigbee2MQTT devices | MqttAdapterBase | Deployed |
| VivintAdapter | Vivint locks and sensors | MqttAdapterBase | Deployed |
| NvrAdapter | Reolink NVR snapshot proxy | PollingAdapterBase | Deployed |
| PrinterAdapter | Brother printer monitor | AsyncPollingAdapterBase | Deployed |

## Operators

Operators transform signals between sensors and emitters.  Three
built-in operators:

- `OccupancyOperator` — home/away state derived from lock patterns
- `MotionGateOperator` — suppresses motion events when away
- `TriggerOperator` — binds sensor events to device actions with
  watchdog timeouts

See the `operators/` directory for implementation details.

## Current Integration Status

| Component | Status | Notes |
|-----------|--------|-------|
| Emitter ABC | Deployed | `emitters/__init__.py` v0.2 |
| EmitterManager | Deployed | Same file, lifecycle + dispatch |
| LifxEmitter | Deployed | `emitters/lifx.py` v2.0, dual interface |
| VirtualMultizoneEmitter | Deployed | `emitters/virtual.py` v2.0 |
| Screen emitters | Deployed | `emitters/screen.py`, `screen_matrix.py` v2.0 |
| Engine integration | Transitional | Engine calls emitter methods directly; EmitterManager not yet wired into send loop |
| Config-based emitters | Not yet | `server.json` `"emitters"` section not yet parsed at startup |
| REST API endpoints | Not yet | `/api/emitters/*` not yet implemented |

The next integration step is wiring the EmitterManager into the
Engine's send loop so that continuous emitters are dispatched via
`emit_frame()` rather than direct method calls.  This preserves
backward compatibility — the LifxEmitter exposes both the SOE
lifecycle and the engine-facing methods simultaneously.
