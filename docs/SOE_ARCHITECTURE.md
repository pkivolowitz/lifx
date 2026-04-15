# GlowUp SOE Architecture

**Sensors → Operators → Emitters**

A comprehensive design and engineering reference for the signal-driven
pipeline that unifies sensing, computation, and actuation in GlowUp.

Copyright (c) 2026 Perry Kivolowitz. All rights reserved.

---

## 1. Architectural Lineage

The SOE pipeline is iteration five of an architecture first shipped
commercially in 1988 as **Loaders → Operators → Savers** in ADPro
(ASDG, Inc.) for the Commodore Amiga.  That architecture powered
desktop publishing, multimedia, desktop video, and desktop film
products through Elastic Reality (Academy Award for Technical
Achievement, 1996).

The core insight has survived five generations because it is domain-
agnostic: **data enters from sources, transforms flow through a
composable graph, and results exit to sinks.**  The names change
(Loaders/Sensors, Operators/Operators, Savers/Emitters) but the
separation of concerns is identical.

In GlowUp, the insight is sharpened: **a sensor reading and a user-set
parameter are both just signals on the bus.**  An operator's `read()`
does not distinguish source.  A parameter adjustment from the iOS app
and a temperature reading from a Zigbee sensor are the same type of
event — a named value changed.

---

## 2. The Signal Bus

The **SignalBus** is the central nervous system.  Every sensor, operator,
emitter, and effect reads from and writes to the same bus.

### Signal Identity

Signals are identified by a flat namespace of colon-separated strings:

```
{source}:{property}
```

Examples:
- `onvis_motion:motion` — BLE motion sensor
- `hallway_contact:contact` — Zigbee door sensor
- `front_door:lock_state` — Vivint lock
- `house:occupancy:state` — derived occupancy (operator output)
- `breathe:speed` — effect parameter (engine-written)

**Transport is metadata, not namespace.**  The signal name carries no
transport prefix.  A signal named `front_door:lock_state` does not
reveal whether the data arrived over Vivint cloud, Z-Wave, Zigbee, or
was typed into a REST endpoint.  The transport is stored in
`SignalMeta.transport` and queryable via `signals_by_transport()`.

This is the key to fungibility: an operator that subscribes to
`*:lock_state` will work with any lock, from any manufacturer, on
any radio, without code changes.

### Signal Metadata

Every signal may be registered with a `SignalMeta` dataclass:

```python
SignalMeta(
    signal_type="scalar",       # "scalar" or "array"
    description="BLE motion",   # human-readable
    source_name="onvis_motion", # device/source identifier
    transport="ble",            # radio/protocol family
    min_val=0.0,                # expected range floor
    max_val=1.0,                # expected range ceiling
)
```

Metadata is advisory.  The bus does not enforce ranges.  But it
enables:
- **Collision detection:** if a signal is re-registered with a different
  transport, the bus logs a warning.
- **Dashboard display:** the iOS app can show units and ranges.
- **Transport routing:** `signals_by_transport("ble")` returns all BLE signals.

### Thread Safety

The bus uses a single `threading.Lock` for all read/write/register
operations.  Network publish (MQTT) happens outside the lock to avoid
blocking reads during I/O.  Local-before-remote ordering is acceptable.

### Timestamps

Every `write()` records a monotonic timestamp.  Operators can call
`read_timestamp()` or `read_with_timestamp()` to determine signal age
— critical for staleness detection and watchdog timers.

---

## 3. Sensors (Transport Adapters)

Sensors are the S in SOE.  They bridge external data sources to the
SignalBus.

### SensorAdapter ABC

All adapters inherit from `SensorAdapter` (in `sensor_adapter.py`),
which provides:

```python
class SensorAdapter(ABC):
    TRANSPORT: str = ""               # subclass sets: "ble", "zigbee", etc.

    def __init__(self, bus): ...      # stores bus reference
    @abstractmethod def start(): ...  # begin ingestion
    @abstractmethod def stop(): ...   # release resources

    def _write_signal(self, name, value, source_name, description=""): ...
        # registers metadata + writes to bus in one call
```

### Current Adapters

| Adapter | Transport | Ingestion | Normalization |
|---------|-----------|-----------|---------------|
| `glowup-ble-sensor` (broker-2) | BLE / HAP | Encrypted HAP-BLE reads on broker-2 → cross-host paho publish to hub on `glowup/signals/{label}:{prop}` (numeric) and `glowup/ble/status/{label}` (JSON) | motion: int→float, temp/humid: raw Celsius/% — standalone systemd service on broker-2, not an `AdapterBase` subclass |
| `glowup-zigbee-service` (broker-2) | Zigbee | Local Z2M MQTT subscribe → cross-host paho publish to hub on `glowup/signals/{device}:{prop}` | boolean→0.0/1.0, battery÷100, temp/humid: raw — runs as a standalone systemd service on broker-2, not an `AdapterBase` subclass |
| `VivintAdapter` | Vivint | Async cloud API polling + PubNub | lock: bool→0.0/1.0, battery÷100 |

### Writing a New Adapter

A new transport adapter (e.g., 433 MHz via `rtl_433`) follows this
template:

1. Subclass `SensorAdapter`.
2. Set `TRANSPORT = "433mhz"`.
3. Implement `start()` — spawn subprocess, connect socket, etc.
4. Implement `stop()` — clean up.
5. On each data event, call `self._write_signal(name, value, source, desc)`.

The adapter does not need to know about operators, effects, or emitters.
It writes signals; the bus does the rest.

### Configuration

All adapters accept configurable MQTT topic prefixes:

```json
{
    "vivint": {"mqtt_topic_prefix": "glowup/vivint"}
}
```

(BLE no longer takes a `topic_prefix` config key — the
`glowup-ble-sensor` service publishes on a fixed schema:
`glowup/signals/{label}:{prop}` for numerics and
`glowup/ble/status/{label}` for JSON status.  See
[Chapter 28](28-ble-sensors.md).)

---

## 4. Operators

Operators are the O in SOE.  They read signals, compute, and write
derived signals.

### Operator ABC

All operators inherit from `Operator` (in `operators/__init__.py`):

```python
class Operator:
    operator_type: str            # unique type identifier (registry key)
    description: str              # human-readable one-liner
    input_signals: list[str]      # fnmatch patterns to subscribe to
    output_signals: list[str]     # signals this operator writes
    depends_on: list[str]         # operator types that must run first
    tick_mode: str                # "reactive", "periodic", "both", "engine"
    tick_hz: float                # rate for periodic tick (Hz)

    def on_signal(name, value):   # reactive: input changed
    def on_tick(dt):              # periodic: timer fired
    def on_configure(config):     # deferred init
    def on_start():               # acquire resources
    def on_stop():                # release resources
    def read(signal, default):    # read from bus
    def write(signal, value):     # write to bus
```

### Registration

Operators register automatically via `__init_subclass__`.  Setting
`operator_type = "occupancy"` adds the class to the global registry.
Effects also inherit from Operator but set `operator_type = None` —
they register via `EffectMeta` in the effect registry instead.

**Validation at registration time:**
- **Tick mode:** invalid values are caught and defaulted to `"reactive"` with a warning.
- **Type collision:** if two classes register the same `operator_type`, the second overwrites the first with a warning logged.

### Tick Modes

| Mode | Dispatch | Use Case |
|------|----------|----------|
| `reactive` | `on_signal()` fires when a subscribed input changes | Gate, threshold |
| `periodic` | `on_tick()` fires at `tick_hz` rate | Debounce timer, decay |
| `both` | Reactive + periodic | Occupancy (instant on unlock, timer for AWAY) |
| `engine` | Engine render loop drives at frame rate | Effects only |

### Dependency Declaration and Topological Ordering

Operators declare dependencies via the `depends_on` class attribute:

```python
class MotionGateOperator(Operator):
    operator_type = "motion_gate"
    depends_on = ["occupancy"]     # must evaluate after OccupancyOperator

class TriggerOperator(Operator):
    operator_type = "trigger"
    depends_on = ["motion_gate"]   # must evaluate after gating
```

The `OperatorManager` topologically sorts all operators at configure
time using Kahn's algorithm.  This guarantees that when a signal
propagates through the chain (lock change → occupancy → motion gate →
trigger), each operator sees its dependencies' outputs before
evaluating.

Cycles are detected and logged.  Cyclic operators are appended at
the end rather than silently dropped.

### Signal Matching

Input signal patterns use `fnmatch` wildcards:

```python
input_signals = ["*:lock_state"]          # any device, lock_state property
input_signals = ["*:motion", "*:occupancy"]  # motion or occupancy from anyone
```

The `OperatorManager` polls the bus at 50 Hz, detects value changes,
and dispatches to matching operators.  Performance is O(N×M) where N
is operator count and M is pattern count — negligible for home
automation scale (< 1ms per poll at 20 operators).

### Auto-Disable

If an operator throws 10 consecutive exceptions in `on_signal` or
`on_tick`, it is automatically disabled.  A `system:operator_disabled`
signal is written to the bus with the operator name as value, allowing
dashboards and monitoring operators to detect failures without polling.

### Concrete Operators

**OccupancyOperator** (`operators/occupancy.py`)
- Input: `*:lock_state`
- Output: `house:occupancy:state` (1.0 = HOME, 0.0 = AWAY)
- Logic: any lock unlocks → instant HOME. All locks locked for
  `away_confirm_seconds` (configurable, default 120s) → AWAY.
- Persistence: SQLite (survives restart).

**MotionGateOperator** (`operators/motion_gate.py`)
- Input: motion patterns + `house:occupancy:state`
- Output: `{motion_signal}:gated`
- Logic: pass through when HOME, suppress to 0.0 when AWAY.
- Reacts to occupancy changes: when AWAY transition occurs, immediately
  suppresses all known motion signals (doesn't wait for next motion event).
- Depends on: `occupancy`

**TriggerOperator** (`operators/trigger.py`)
- Input: one sensor signal (configurable)
- Output: device actions via DeviceManager
- Logic: condition evaluation → play/stop effect on group.
- Watchdog timeout for off-action. Schedule conflict awareness.
- Configurable per-trigger debounce (default 2s).
- Depends on: `motion_gate`

### Condition Evaluation

Shared module `operators/conditions.py` provides:

```python
evaluate_condition("gt", 25.0, temperature)   # temperature > 25°C?
evaluate_condition("eq", 1, motion_value)      # motion detected?
```

Operators: `eq`, `gt`, `lt`, `gte`, `lte`.  Type-safe with graceful
error handling.

---

## 5. Emitters

Emitters are the E in SOE.  They consume rendered output and deliver
it to the physical world.

| Emitter | Target |
|---------|--------|
| `LifxEmitter` | LIFX multizone, single, monochrome bulbs |
| `VirtualMultizoneEmitter` | Stitched device groups |
| `MidiOutEmitter` | FluidSynth / rtmidi audio |
| `AudioOutEmitter` | Speaker output |
| `WebGLEmitter` | Browser particle visualization |

Emitters implement the `on_emit(colors)` lifecycle.  The engine calls
this at frame rate with the rendered HSBK frame.

---

## 6. Effects as Operators

Effects inherit from Operator:

```python
class Effect(Operator, metaclass=EffectMeta):
    operator_type = None      # not in operator registry
    tick_mode = "engine"      # engine drives, not OperatorManager
```

This gives effects the full signal-bus API (`read`, `write`,
`get_params`, `set_params`) while keeping their rendering on the
deterministic frame-rate engine path.

### Params as Signals

When the engine starts an effect with a signal bus, every numeric
parameter is written to the bus as `{effect_name}:{param_name}`.
Each frame, the engine reads these signals back and applies them.
This means:

- A binding from `backyard:audio:bass` to `breathe:speed` is just
  signal routing — read one name, write another.
- An operator could subscribe to `breathe:speed` and react to
  parameter changes.
- `Controller.update_params()` writes to the bus; the next frame
  picks up the change.

The bus is the single source of truth for parameters when active.

---

## 7. Composition Model

### Signal Flow Graph

```
          ┌──────────────────────────┐
Sensors   │ glowup-ble-sensor        │──► onvis_motion:motion
          │ (broker-2, cross-host)   │
          │ glowup-zigbee-service    │──► hallway_contact:contact
          │ (broker-2, cross-host)   │
          │ VivintAdapter            │──► front_door:lock_state
          └──────────┬───────────────┘
                 │ SignalBus
          ┌──────▼───────┐
Operators │ Occupancy    │──► house:occupancy:state
          │ MotionGate   │──► onvis_motion:motion:gated
          │ Trigger      │──► [device action]
          └──────┬───────┘
                 │
          ┌──────▼───────┐
Engine    │ Effect.render │──► HSBK frames
          └──────┬───────┘
                 │
          ┌──────▼───────┐
Emitters  │ LifxEmitter  │──► UDP packets to bulbs
          │ MidiOut      │──► audio to speakers
          └──────────────┘
```

### Topological Order

Operators are dispatched in dependency order:

```
1. OccupancyOperator    (depends_on: [])
2. MotionGateOperator   (depends_on: ["occupancy"])
3. TriggerOperator      (depends_on: ["motion_gate"])
```

Config file ordering does not matter.  The manager sorts at startup.

### Fungibility

Because signals are transport-free:
- Replace a Vivint lock with a Z-Wave lock → same `*:lock_state`
  signal, OccupancyOperator works unchanged.
- Replace ONVIS BLE sensor with Aqara Zigbee sensor → same
  `*:motion` signal, MotionGateOperator works unchanged.
- Replace LIFX lights with Hue → write a HueEmitter, effects
  work unchanged.

Nothing in the operator layer knows or cares about the hardware.

---

## 8. Safeguards

### Collision Detection

The SignalBus warns when a signal registered by one transport is
overwritten by a different transport.  This catches namespace
collisions (e.g., an effect named identically to a device).

### Auto-Disable with Notification

Operators that throw 10 consecutive exceptions are disabled and a
`system:operator_disabled` signal is published.  Dashboards can
subscribe and alert the user.

### Cycle Detection

The topological sort detects dependency cycles and logs warnings.
Cyclic operators are appended after sorted ones rather than silently
dropped.

### Guarded Module Loading

Concrete operator modules are loaded with try/except.  A missing
dependency (e.g., `vivintpy` not installed) disables that operator
without affecting others or the Operator ABC itself.

---

## 9. Extension Points

| Want to... | Do this |
|------------|---------|
| Add a new radio (433 MHz, Thread, Z-Wave) | Subclass `SensorAdapter`, set TRANSPORT, implement start/stop |
| Add a new derived signal (comfort index, sleep state) | Subclass `Operator`, declare input/output signals, implement on_signal/on_tick |
| Add a new output device (Hue, Nanoleaf, HVAC) | Subclass `Emitter`, implement on_emit |
| Add a new effect | Subclass `Effect`, implement render() |
| Add a new condition type | Add to `operators/conditions.py` CONDITION_OPS dict |
| React to operator failures | Subscribe to `system:operator_disabled` signal |

Every extension follows the same pattern: declare what you read,
declare what you write, implement the transform.  The bus handles
routing.  The manager handles lifecycle.  The topology handles ordering.

---

## 10. Design Principles

- **Signals, not callbacks.** Components communicate through named values
  on a shared bus, not direct function calls.  This decouples producers
  from consumers and makes the system inspectable at runtime.

- **Transport is metadata.** The bus doesn't care how data arrived.  An
  operator that processes lock state works identically whether the lock
  speaks Vivint cloud, Z-Wave, Zigbee, or BLE.

- **Parameters are signals.** There is no distinction between a sensor
  reading and a user-set parameter.  Both are named values on the bus.
  This is the Loaders → Operators → Savers insight, restated.

- **Composition through dependency, not wiring.** Operators declare what
  they depend on (by operator type), not what they're wired to (by signal
  name).  The manager resolves ordering.  Operators themselves use
  wildcard patterns to find signals.

- **Fail locally.** A broken operator is disabled; the rest continue.  A
  missing adapter dependency skips that adapter.  The system degrades
  gracefully rather than crashing globally.

- **Effects are specialized operators.** They share the signal-bus API
  but run on a deterministic frame-rate loop.  This is a deliberate
  trade-off: effects need consistent timing; operators need event
  responsiveness.  Unifying them on one dispatch path would weaken both.
