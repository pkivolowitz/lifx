# Operator Framework

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

Operators are the transform/compute layer of the SOE (Sensors ->
Operators -> Emitters) pipeline.  They sit between sensor adapters
and emitters, reading signals from the `SignalBus`, applying
computations, and writing derived signals back.  An FFT, a Kalman
filter, an occupancy derivation from lock state, and an HSBK-rendering
Effect are all operators.

The design descends from the original Loaders -> Operators -> Savers
architecture (Amiga ADPro, 1989).  A sensor reading and a user-set
parameter are both just signals on the bus.  An operator's `read()`
does not distinguish source.

---

## The Operator ABC

Every transform node in GlowUp -- including `Effect` subclasses that
render HSBK frames -- inherits from `Operator`.  The ABC lives in
`operators/__init__.py`.

### Class Attributes (subclasses must define)

| Attribute | Type | Description |
|-----------|------|-------------|
| `operator_type` | `str` | Unique type identifier (registry key) |
| `description` | `str` | Human-readable one-liner |
| `input_signals` | `list[str]` | Signal patterns to subscribe to (supports fnmatch wildcards) |
| `output_signals` | `list[str]` | Signal names this operator writes |
| `depends_on` | `list[str]` | Operator types that must be dispatched first |
| `tick_mode` | `str` | Dispatch mode: `"reactive"`, `"periodic"`, `"both"`, or `"engine"` |
| `tick_hz` | `float` | Tick rate for periodic operators (default: `1.0`) |

### Lifecycle Methods

| Method | When Called | Purpose |
|--------|------------|---------|
| `on_configure(config)` | Once after construction | Deferred init (DB connections, device discovery) |
| `on_start()` | Pipeline start | Acquire resources, start background work |
| `on_signal(name, value)` | Input signal changes | Reactive processing |
| `on_tick(dt)` | At `tick_hz` rate | Periodic processing (debounce, decay, watchdog) |
| `on_stop()` | Pipeline stop | Release resources |

### Bus Access

```python
# Read any signal from the bus.
value = self.read("vivint:front_door_lock:lock_state", default=0.0)

# Write a derived signal back to the bus.
self.write("house:occupancy:state", 1.0)
```

### Parameters

Operators declare configurable parameters as class-level `Param`
instances, the same pattern used by Effects and Emitters.  Config
values override defaults at construction time.

```python
away_confirm_seconds = Param(
    120.0, min=30.0, max=600.0,
    description="Seconds before AWAY transition",
)
```

At runtime, `get_params()` returns current values and `set_params()`
updates them with validation.

### Tick Modes

| Mode | Behavior |
|------|----------|
| `"reactive"` | `on_signal()` fires on subscribed input changes only |
| `"periodic"` | `on_tick()` fires at `tick_hz` rate only |
| `"both"` | Reactive + periodic |
| `"engine"` | Effects only; the Engine's send loop drives rendering; `OperatorManager` skips these |

### Signal Matching

`input_signals` supports fnmatch wildcards for flexible subscriptions:

```python
input_signals = ["*:lock_state"]       # Any device, lock_state property
input_signals = ["*:occupancy"]        # Any motion sensor
input_signals = ["hallway_motion:*"]   # All properties from one device
```

### Auto-Registration

Concrete subclasses with a non-`None` `operator_type` are
automatically registered in the global registry via
`__init_subclass__`.  Effect subclasses leave `operator_type = None`
and register through the effect registry instead.

```python
from operators import get_registry, get_operator_types, create_operator

# List available operator types.
get_operator_types()
# ["motion_gate", "occupancy", "trigger"]

# Instantiate by type.
op = create_operator("occupancy", "house_occupancy", config, bus)
```

---

## OperatorManager

The `OperatorManager` manages operator lifecycles and dispatches
signals and ticks.  It parallels the `EmitterManager` in design.

### Configuration

The manager is configured from the `"operators"` array in
`server.json`.  Each entry must have `"type"` and `"name"` keys:

```json
{
    "operators": [
        {
            "type": "occupancy",
            "name": "house_occupancy",
            "tick_hz": 1.0,
            "away_confirm_seconds": 120,
            "db_path": "/etc/glowup/state.db"
        },
        {
            "type": "motion_gate",
            "name": "gated_motion",
            "occupancy_signal": "house:occupancy:state",
            "motion_signals": ["*:occupancy", "*:motion"]
        },
        {
            "type": "trigger",
            "name": "hallway_lights",
            "sensor": {"type": "zigbee", "label": "hallway_motion",
                       "characteristic": "occupancy"},
            "trigger": {"condition": "eq", "value": 1},
            "action": {"group": "hallway", "effect": "on",
                       "params": {"brightness": 70}},
            "off_trigger": {"type": "watchdog", "minutes": 30},
            "off_action": {"effect": "off", "params": {}},
            "schedule_conflict": "defer"
        }
    ]
}
```

### Topological Sort

Operators declare dependencies via `depends_on` (a list of operator
type strings).  The `OperatorManager` sorts operators topologically
using Kahn's algorithm so dependencies are always dispatched before
dependents, regardless of config file ordering.

```
occupancy  -->  motion_gate  -->  trigger
(no deps)      (depends_on:       (depends_on:
                ["occupancy"])      ["motion_gate"])
```

Cycles are detected and logged -- cyclic operators are appended at
the end rather than silently dropped.

### Signal Dispatch

The manager's tick loop polls the `SignalBus` for changes and routes
them to all matching reactive operators.  The dispatch respects tick
mode -- engine-driven operators (Effects) and periodic-only operators
are skipped during signal dispatch.

### Auto-Disable

If an operator's `on_signal()` or `on_tick()` throws exceptions
`MAX_CONSECUTIVE_FAILURES` times (10), the operator is automatically
disabled and a `system:operator_disabled` signal is written to the bus
so dashboards and monitoring can react.

### Introspection

```python
manager.get_status()
# [
#     {
#         "name": "house_occupancy",
#         "type": "occupancy",
#         "started": True,
#         "enabled": True,
#         "consecutive_failures": 0,
#         "tick_mode": "both",
#         "input_signals": ["*:lock_state"],
#         "output_signals": ["house:occupancy:state"],
#         "params": {"away_confirm_seconds": 120.0}
#     },
#     ...
# ]
```

---

## Built-in Operators

### OccupancyOperator (`operators/occupancy.py`)

Derives HOME/AWAY from aggregate lock state.

- **Input:** `*:lock_state` (any lock signal on the bus)
- **Output:** `house:occupancy:state` (`1.0` = HOME, `0.0` = AWAY)
- **Tick mode:** `both` (reactive to lock changes + periodic timer check)
- **Logic:**
  - Any lock unlocked -> HOME immediately
  - All locks locked for `away_confirm_seconds` (default 120s) -> AWAY
  - The debounce prevents false AWAY during normal activity (locking
    the front door while walking to the back door)
- **Persistence:** Writes transitions to SQLite (`state.db`) so state
  survives server restart
- **Pet rationale:** The household has 3 dogs.  Motion sensors fire
  constantly when humans are away.  Lock state is the only clean
  discriminator -- dogs cannot work a deadbolt.

```json
{
    "type": "occupancy",
    "name": "house_occupancy",
    "tick_hz": 1.0,
    "away_confirm_seconds": 120,
    "db_path": "/etc/glowup/state.db"
}
```

### MotionGateOperator (`operators/motion_gate.py`)

Suppresses motion signals when occupancy is AWAY.

- **Input:** Configured motion signal patterns + the occupancy signal
- **Output:** `{original_signal}:gated` (e.g.,
  `hallway_motion:occupancy:gated`)
- **Tick mode:** `reactive`
- **Depends on:** `occupancy`
- **Logic:**
  - HOME: pass motion through (`1.0` stays `1.0`)
  - AWAY: suppress (write `0.0` regardless of raw value)
  - On occupancy AWAY transition: immediately suppress all
    previously-seen motion signals (don't wait for next event)
- **Downstream usage:** Automations and triggers should subscribe to
  `:gated` signals, not raw motion, to respect occupancy state

```json
{
    "type": "motion_gate",
    "name": "gated_motion",
    "occupancy_signal": "house:occupancy:state",
    "motion_signals": ["*:occupancy", "*:motion"]
}
```

### TriggerOperator (`operators/trigger.py`)

The runtime engine for automation rules.  Replaces
`AutomationManager` with individual operator instances managed by the
`OperatorManager`.  See [Chapter 30: Automation & Trigger
System](30-automation.md) for the full configuration format.

- **Input:** Dynamically set from `sensor` config
- **Output:** None (drives device actions via `DeviceManager`)
- **Tick mode:** `both` (reactive to sensor changes + periodic
  watchdog check)
- **Depends on:** `motion_gate`
- **Key behaviors:**
  - Evaluates trigger condition (`eq`, `gt`, `lt`, `gte`, `lte`)
  - Fires on-action via `DeviceManager.play()`
  - Manages watchdog timeout for off-actions
  - Respects `schedule_conflict` policy
  - Debounce prevents hammering bulbs (configurable, default 2s)
  - DeviceManager reference injected via `on_configure()`

Because TriggerOperators are just operators, they compose naturally
with upstream operators.  A trigger subscribing to
`hallway_motion:occupancy:gated` only fires when the occupancy gate
passes motion through -- no special-case code needed.

---

## Writing a Custom Operator

A minimal operator that derives a signal:

```python
"""DaylightOperator — publish daylight/night from illuminance threshold."""

from operators import Operator, TICK_REACTIVE, SignalValue
from param import Param

class DaylightOperator(Operator):
    operator_type = "daylight"
    description = "Derive day/night from illuminance threshold"

    input_signals = ["*:illuminance"]
    output_signals = ["house:daylight:state"]

    lux_threshold = Param(
        50.0, min=1.0, max=1000.0,
        description="Illuminance threshold for daylight (lux)",
    )

    def on_signal(self, name: str, value: SignalValue) -> None:
        try:
            lux = float(value) if not isinstance(value, list) else 0.0
        except (ValueError, TypeError):
            return
        daylight = 1.0 if lux >= self.lux_threshold else 0.0
        self.write("house:daylight:state", daylight)
```

Steps:

- Create a file in `operators/` (e.g., `operators/daylight.py`).
- Subclass `Operator`.
- Set `operator_type` to a unique string.
- Declare `input_signals` and `output_signals`.
- Implement `on_signal()` and/or `on_tick()`.
- Add the module name to the auto-import list in
  `operators/__init__.py`.
- Configure in `server.json`:

```json
{
    "type": "daylight",
    "name": "daylight_sensor",
    "lux_threshold": 100
}
```

The operator is automatically registered and available by type.

---

## Module Constants

```python
TICK_REACTIVE       = "reactive"
TICK_PERIODIC       = "periodic"
TICK_BOTH           = "both"
TICK_ENGINE         = "engine"

DEFAULT_TICK_HZ     = 1.0         # Default tick rate (Hz)
TICK_POLL_HZ        = 50.0        # Manager poll granularity (Hz)
TICK_POLL_INTERVAL  = 0.02        # Derived poll interval (seconds)
MIN_TICK_HZ         = 0.01        # Floor for operator tick_hz

MAX_CONSECUTIVE_FAILURES = 10     # Auto-disable threshold
```

---

## See Also

- [Chapter 21: SOE Pipeline](21-soe-pipeline.md) -- the full
  Sensors -> Operators -> Emitters architecture
- [Chapter 30: Automation & Trigger System](30-automation.md) --
  automation configuration and migration
- [Chapter 20: Media Pipeline](20-media-pipeline.md) -- the
  `SignalBus` that operators read from and write to
