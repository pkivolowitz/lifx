# Automation & Trigger System

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

GlowUp's automation system binds sensor events to device actions:
motion triggers lights on, a watchdog timeout turns them off.  It
replaces the earlier hardcoded `ble_trigger.py` with a general-purpose
rule engine that supports full CRUD via REST API and works across BLE,
Zigbee, and Vivint sensor transports.

The automation system exists in two generations:

- **AutomationManager** (`automation.py`) -- the original MQTT-based
  implementation with its own subscriber thread and watchdog loop.
  Still functional but being superseded.
- **TriggerOperator** (`operators/trigger.py`) -- the SOE-native
  replacement.  Each automation rule becomes an operator instance
  managed by the `OperatorManager`.  See [Chapter 31: Operator
  Framework](31-operators.md).

The server auto-migrates `automations` config entries into trigger
operators at startup.  New automations should be configured as
operators with `"type": "trigger"`.

---

## Configuration Format

Each automation is an entry in the `"operators"` array (or the legacy
`"automations"` array) in `server.json` with `"type": "trigger"`:

```json
{
    "type": "trigger",
    "name": "Living room motion",
    "enabled": true,
    "sensor": {
        "type": "ble",
        "label": "onvis_motion",
        "characteristic": "motion"
    },
    "trigger": {
        "condition": "eq",
        "value": 1
    },
    "action": {
        "group": "living_room",
        "effect": "on",
        "params": {"brightness": 70}
    },
    "off_trigger": {
        "type": "watchdog",
        "minutes": 30
    },
    "off_action": {
        "effect": "off",
        "params": {}
    },
    "schedule_conflict": "defer"
}
```

### Fields

**Top level:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | `str` | *(required)* | Human-readable rule name |
| `enabled` | `bool` | `true` | Whether the rule is active |

**`sensor` -- what to watch:**

| Key | Type | Description |
|-----|------|-------------|
| `type` | `str` | Transport: `"ble"`, `"zigbee"`, or `"vivint"` |
| `label` | `str` | Device label (e.g., `"onvis_motion"`, `"hallway_motion"`) |
| `characteristic` | `str` | Signal property to monitor |

Valid characteristics: `motion`, `temperature`, `humidity`,
`lock_state`, `contact`, `battery`, `occupancy`, `illuminance`.

**`trigger` -- when to fire:**

| Key | Type | Description |
|-----|------|-------------|
| `condition` | `str` | Comparison operator: `eq`, `gt`, `lt`, `gte`, `lte` |
| `value` | `number` | Threshold value to compare against |

The trigger fires when `sensor_value <condition> threshold` evaluates
to `true`.

**`action` -- what to do when triggered:**

| Key | Type | Description |
|-----|------|-------------|
| `group` | `str` | Target device group name |
| `effect` | `str` | Effect to play (e.g., `"on"`, `"color"`, `"off"`) |
| `params` | `dict` | Effect parameters (e.g., `{"brightness": 70}`) |

Audio/media effects are not allowed in automations.

**`off_trigger` -- when to stop:**

| Key | Type | Description |
|-----|------|-------------|
| `type` | `str` | `"watchdog"` (timeout) or `"condition"` (sensor value match) |
| `minutes` | `float` | Watchdog timeout in minutes (watchdog type only) |
| `condition` | `str` | Comparison operator (condition type only) |
| `value` | `number` | Threshold (condition type only) |

**`off_action` -- what to do when the off-trigger fires:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `effect` | `str` | `"off"` | Effect to play (usually `"off"`) |
| `params` | `dict` | `{}` | Effect parameters |

**`schedule_conflict` -- behavior when a schedule is active:**

| Value | Behavior |
|-------|----------|
| `defer` | Do not fire the automation if a schedule entry is active for the group |
| `override` | Fire the automation regardless of active schedules |
| `coexist` | Allow both automation and schedule to run |

---

## The Watchdog Thread

The watchdog is the timeout mechanism for turning lights off after
sensor activity stops.  It runs as a background thread, checking
every 60 seconds whether any active automation's last trigger event
has exceeded its configured timeout.

The check uses a two-phase pattern to avoid holding locks during
device I/O:

- **Phase 1 (under lock)** -- Iterate all automations, identify
  expired watchdogs.  Collect the list of automations needing
  off-actions.  No I/O occurs here.
- **Phase 2 (outside lock)** -- Execute off-actions for expired
  automations.  Device commands may block on network I/O, so
  the lock is not held.

Safety guards:

- If `last_trigger` is `0.0` (automation has never been triggered),
  the watchdog skips it to avoid spurious off-actions at startup.
- The `last_action` timestamp is updated after off-actions to
  prevent rapid on/off cycling from debounce interactions.

```
Sensor fires  -->  action played  -->  watchdog timer starts
                                           |
                              (no new events for N minutes)
                                           |
                                  off-action fires  -->  idle
```

---

## Debounce

A minimum `DEBOUNCE_SECONDS` (2.0 seconds) gap is enforced between
repeated trigger actions.  This prevents hammering bulbs when a
sensor fires continuously (e.g., motion sensor refreshing every
second while someone is in the room).

The debounce applies to both on-actions and off-actions independently.

---

## REST API

The server exposes CRUD endpoints for automations:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/automations` | List all automations with current state |
| `POST` | `/api/automations` | Create a new automation |
| `PUT` | `/api/automations/{index}` | Update an automation by index |
| `POST` | `/api/automations/{index}/enabled` | Enable or disable an automation |
| `DELETE` | `/api/automations/{index}` | Delete an automation |

Changes are persisted to `server.json` on disk.  The automation
manager (or operator manager) reloads its state after each CRUD
operation.

### Validation

The `validate_automation()` function checks every field before
accepting a create or update:

- `name` is required.
- `sensor.label` must be non-empty.
- `sensor.characteristic` must be in the valid set.
- `trigger.condition` must be a recognized operator (`eq`, `gt`,
  `lt`, `gte`, `lte`).
- `action.group` must exist in the server's group registry.
- `action.effect` must be a registered non-media effect.
- `off_trigger.minutes` must be a positive number (watchdog type).
- `schedule_conflict` must be `defer`, `override`, or `coexist`.

Validation errors are returned as a list of human-readable strings.

---

## Legacy Migration

### `ble_triggers` -> `automations`

The original `ble_trigger.py` used a `"ble_triggers"` config format
keyed by sensor label.  The `migrate_ble_triggers()` function
auto-converts these to the `automations` array format at startup:

```json
// Old format (ble_triggers):
{
    "ble_triggers": {
        "onvis_motion": {
            "group": "group:living_room",
            "on_motion": {"brightness": 70},
            "watchdog_minutes": 30
        }
    }
}

// New format (automations):
{
    "automations": [
        {
            "name": "onvis_motion (migrated)",
            "enabled": true,
            "sensor": {"type": "ble", "label": "onvis_motion",
                       "characteristic": "motion"},
            "trigger": {"condition": "eq", "value": 1},
            "action": {"group": "living_room", "effect": "on",
                       "params": {"brightness": 70}},
            "off_trigger": {"type": "watchdog", "minutes": 30},
            "off_action": {"effect": "off", "params": {}},
            "schedule_conflict": "defer"
        }
    ]
}
```

Migration only runs if `"automations"` does not already exist in the
config.  The `group:` prefix is stripped from group names.

### `automations` -> Trigger Operators

At server startup, the `_migrate_automations_to_triggers()` function
converts any remaining `automations` entries into `trigger`-type
operator configs and appends them to the `"operators"` array.  The
`"automations"` key is then removed from the config.  This ensures
all automation logic runs through the unified operator framework.

---

## Integration with the Operator Framework

Each automation rule is ultimately instantiated as a `TriggerOperator`
-- an operator in the SOE pipeline.  This means automations compose
naturally with other operators:

- **OccupancyOperator** derives HOME/AWAY from lock state.
- **MotionGateOperator** suppresses motion when AWAY.
- **TriggerOperator** subscribes to gated motion signals, so it only
  fires when the household is home.

No special-case code is needed for pet suppression or occupancy
awareness -- it falls out of the operator dependency chain.  See
[Chapter 31: Operator Framework](31-operators.md) for details.

---

## Module Constants

```python
# AutomationManager has been retired in favour of TriggerOperator;
# its TRANSPORT_PREFIXES dict is dead code preserved only because
# tests still import the manager class.  The live-fire BLE/Zigbee
# subscribers no longer use this dict — see Chapter 28 (BLE Sensors)
# and Chapter 29 (Zigbee Service) for the current data paths.
DEBOUNCE_SECONDS          = 2.0     # Min gap between repeated actions
WATCHDOG_CHECK_INTERVAL   = 60.0    # Seconds between watchdog checks
DEFAULT_WATCHDOG_MINUTES  = 30.0    # Default timeout when not specified
```

---

## See Also

- [Chapter 31: Operator Framework](31-operators.md) -- the
  `TriggerOperator` that replaces `AutomationManager`
- [Chapter 28: BLE Sensors](28-ble-sensors.md) -- BLE sensor
  integration that feeds automation triggers
- [Chapter 29: Zigbee Service](29-zigbee-service.md) -- Zigbee
  sensor integration via `glowup-zigbee-service` on broker-2
- [Chapter 21: SOE Pipeline](21-soe-pipeline.md) -- how signals flow
  through the platform
