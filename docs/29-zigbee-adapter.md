# Zigbee Adapter

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

GlowUp integrates Zigbee devices via Zigbee2MQTT.  The
`ZigbeeAdapter` subscribes to Z2M's MQTT topics, normalizes the
JSON payloads into typed floats, writes them to the `SignalBus`,
and republishes the normalized values to GlowUp's own MQTT topic
tree for remote subscribers.

This adapter handles motion sensors, contact sensors, soil moisture
probes, and temperature/humidity sensors paired to the SONOFF Zigbee
dongle.  It does **not** handle locks -- those stay on Vivint (see
`vivint_adapter.py`), though lock state normalization is present for
future-proofing.

---

## Architecture

```
+-------------------+       +-------------------+       +-------------------+
|  SONOFF Dongle    |       |  Zigbee2MQTT      |       |  GlowUp Server   |
|  (USB Coordinator)|  Zigbee|  (broker-2)       |  MQTT |  (Pi)            |
|                   | -----> |                   | -----> |  ZigbeeAdapter   |
+-------------------+       +-------------------+       +-------------------+
                                                                |
                                                          SignalBus
                                                                |
                                                    +-----------+-----------+
                                                    |                       |
                                              Operators               Emitters
                                         (occupancy, gate,        (LIFX bulbs,
                                          trigger, etc.)           grid, etc.)
```

- **SONOFF dongle** -- USB Zigbee coordinator on broker-2 (10.0.0.123).
  Pairs with Zigbee devices and forwards radio traffic.
- **Zigbee2MQTT (Z2M)** -- Runs on broker-2 alongside the Mosquitto
  broker.  Translates Zigbee device messages into JSON payloads and
  publishes them to MQTT topics under the `zigbee2mqtt/` prefix.
- **ZigbeeAdapter** -- Server-side adapter that subscribes to
  `zigbee2mqtt/#`, normalizes values, and writes to the `SignalBus`.

---

## Inheritance

`ZigbeeAdapter` inherits from `MqttAdapterBase` (see [Chapter 27:
Adapter Base Classes](27-adapter-base.md)).  The base class provides:

- MQTT client lifecycle (connect, subscribe, reconnect)
- Thread management (daemon subscriber thread)
- The `_handle_message(topic, payload)` callback contract

`ZigbeeAdapter` implements `_handle_message` for Z2M-specific topic
parsing, JSON deserialization, and signal normalization.  It also
overrides the `_on_started` and `_on_stopped` hooks for logging.

---

## Signal Normalization

Every property in a Z2M JSON payload is normalized to a `float`
before being written to the bus.  Non-numeric, non-boolean values
(strings, nested objects) are silently skipped.

| Property Type | Example Properties | Normalization Rule |
|---------------|-------------------|--------------------|
| Boolean | `occupancy`, `contact`, `water_leak`, `vibration`, `tamper`, `battery_low` | `True` -> `1.0`, `False` -> `0.0` (also handles integer `0`/`1`) |
| Battery | `battery` | `0`-`100` integer -> `0.0`-`1.0` float (divided by 100) |
| Environmental | `temperature`, `humidity`, `illuminance` | Raw float, natural range (no scaling) |
| Lock state | `lock_state` | `"LOCK"` -> `1.0`, `"UNLOCK"` -> `0.0` (string mapping) |

The boolean property set is defined in the `BOOLEAN_PROPERTIES`
frozenset:

```python
BOOLEAN_PROPERTIES: frozenset[str] = frozenset({
    "occupancy", "contact", "water_leak", "vibration",
    "tamper", "battery_low",
})
```

---

## MQTT Topic Flow

### Inbound (Z2M -> Adapter)

The adapter subscribes to `{z2m_prefix}/#` (default:
`zigbee2mqtt/#`).  Z2M publishes device state to topics of the form:

```
zigbee2mqtt/{friendly_name}
```

where `{friendly_name}` is the Z2M device name (e.g.,
`hallway_motion`, `garden_soil`).

The adapter filters out:

- **Bridge messages** -- Topics with `bridge` as the second segment
  (e.g., `zigbee2mqtt/bridge/state`) are Z2M internal coordination
  messages and are skipped.
- **Subtopics** -- Topics with more than two segments (e.g.,
  `zigbee2mqtt/{name}/set` or `zigbee2mqtt/{name}/get`) are command
  topics, not state reports, and are skipped.

### Outbound (Adapter -> GlowUp MQTT)

After normalization, each property is republished to GlowUp's own
topic tree:

```
glowup/zigbee/{friendly_name}/{property}
```

For example, a motion sensor named `hallway_motion` with
`occupancy: true` produces:

```
glowup/zigbee/hallway_motion/occupancy    "1.0"
```

This allows remote subscribers (other Pis, Node-RED, Home Assistant)
to consume normalized GlowUp signals without parsing Z2M JSON.
Republished messages use QoS 0 (at-most-once) for low-latency sensor
data.

### SignalBus

Simultaneously, the same value is written to the SignalBus as:

```
hallway_motion:occupancy = 1.0
```

Bus signal names follow the `{source}:{property}` convention.  The
transport identifier `zigbee` is stored in the signal's `SignalMeta`
metadata, not in the signal name.  This keeps signal names
transport-agnostic so operators and automations can match across
transports.

---

## Configuration

The adapter is configured in the `"zigbee"` section of `server.json`:

```json
{
    "zigbee": {
        "enabled": true,
        "broker": "10.0.0.123",
        "port": 1883,
        "z2m_prefix": "zigbee2mqtt",
        "topic_prefix": "glowup/zigbee"
    }
}
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | `bool` | `false` | Whether to start the Zigbee adapter |
| `broker` | `str` | `"localhost"` | MQTT broker address (broker-2 for Zigbee) |
| `port` | `int` | `1883` | MQTT broker port |
| `z2m_prefix` | `str` | `"zigbee2mqtt"` | Z2M's MQTT topic prefix (subscribe target) |
| `topic_prefix` | `str` | `"glowup/zigbee"` | GlowUp output topic prefix (republish target) |

---

## Supported Device Types

The adapter is device-agnostic -- it normalizes any JSON property Z2M
publishes.  In practice, these are the device types currently paired:

- **Motion sensors** -- Publish `occupancy` (boolean), `battery`
  (0-100), `illuminance` (lux).
- **Contact sensors** -- Publish `contact` (boolean), `battery`
  (0-100).
- **Soil moisture probes** -- Publish `soil_moisture` (percentage),
  `temperature` (Celsius), `battery` (0-100).
- **Temperature/humidity sensors** -- Publish `temperature` (Celsius),
  `humidity` (percentage), `battery` (0-100).

Any new Zigbee device paired to Z2M will have its properties
automatically normalized and published without code changes.

---

## Introspection

The `get_status()` method returns the adapter's runtime state for API
responses:

```python
adapter.get_status()
# {
#     "running": True,
#     "z2m_prefix": "zigbee2mqtt",
#     "glowup_prefix": "glowup/zigbee"
# }
```

---

## Module Constants

```python
DEFAULT_Z2M_PREFIX     = "zigbee2mqtt"      # Default Z2M topic prefix
DEFAULT_GLOWUP_PREFIX  = "glowup/zigbee"    # Default GlowUp output prefix
TRANSPORT              = "zigbee"           # Metadata transport identifier
BRIDGE_SUBTOPIC        = "bridge"           # Z2M internal topic segment
MQTT_QOS               = 0                  # At-most-once for low-latency
BATTERY_SCALE          = 100.0              # Divisor for battery normalization

LOCK_STATE_MAP = {"LOCK": 1.0, "UNLOCK": 0.0}
```

---

## See Also

- [Chapter 27: Adapter Base Classes](27-adapter-base.md) -- the
  `MqttAdapterBase` that `ZigbeeAdapter` inherits from
- [Chapter 28: BLE Sensors](28-ble-sensors.md) -- the BLE adapter,
  which follows the same pattern for a different transport
- [Chapter 21: SOE Pipeline](21-soe-pipeline.md) -- how signals flow
  from adapters through operators to emitters
