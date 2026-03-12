# MQTT Integration

GlowUp includes a native MQTT bridge that connects the server to any
MQTT broker (Mosquitto, EMQX, HiveMQ, etc.).  Once enabled, any MQTT
client can control your lights and subscribe to real-time state
updates — no HTTP required.

Unlike the Home Assistant, Apple Shortcuts, and Node-RED integrations
(which call the REST API from the outside), the MQTT bridge runs
*inside* `server.py` and publishes state changes automatically.

### Prerequisites

- GlowUp server (`server.py`) running on your Pi, Mac, Windows PC,
  or Linux box
- An MQTT broker reachable from the server (e.g., Mosquitto on the
  same machine)
- The `paho-mqtt` Python package:

```bash
pip install paho-mqtt
```

### Configuration

Add an `"mqtt"` section to your `server.json`:

```json
{
    "mqtt": {
        "broker": "localhost",
        "port": 1883,
        "topic_prefix": "glowup",
        "username": null,
        "password": null,
        "tls": false,
        "publish_colors": false,
        "color_interval": 1.0
    }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `broker` | `"localhost"` | Hostname or IP of the MQTT broker |
| `port` | `1883` | Broker TCP port (`8883` is conventional for TLS) |
| `topic_prefix` | `"glowup"` | Root prefix for all topics |
| `username` | `null` | Broker username (omit or `null` for anonymous) |
| `password` | `null` | Broker password |
| `tls` | `false` | Enable TLS encryption |
| `publish_colors` | `false` | Publish live zone color data (high-frequency) |
| `color_interval` | `1.0` | Seconds between color publishes |

To disable MQTT, remove the `"mqtt"` section entirely.  The server
runs identically without it — MQTT is purely optional.

### Topic Layout

All topics are prefixed with `topic_prefix` (default `glowup`).
Device IDs are the same identifiers used in the REST API: IP addresses
for individual devices (e.g., `10.0.0.62`) and `group:name` for
virtual multizone groups (e.g., `group:porch`).

#### Published by GlowUp (state)

| Topic | Retained | QoS | Payload |
|-------|----------|-----|---------|
| `glowup/status` | Yes | 1 | `"online"` or `"offline"` (Last Will) |
| `glowup/devices` | Yes | 1 | JSON array of device info dicts |
| `glowup/device/{id}/state` | Yes | 1 | JSON: `{"running":true,"effect":"aurora","params":{...},"fps":20.1,"overridden":false}` |
| `glowup/device/{id}/colors` | No | 0 | JSON array of `{"h":...,"s":...,"b":...,"k":...}` per zone (only if `publish_colors` is enabled) |

State topics are published only when values change (polled every 2
seconds).  Color topics are published at `color_interval` rate
regardless of change, since zone colors shift continuously during
effects.

#### Subscribed by GlowUp (commands)

| Topic | Payload | Action |
|-------|---------|--------|
| `glowup/device/{id}/command/play` | `{"effect":"aurora","params":{"speed":10.0}}` | Start an effect |
| `glowup/device/{id}/command/stop` | *(any or empty)* | Stop current effect, fade to black |
| `glowup/device/{id}/command/resume` | *(any or empty)* | Clear override, let scheduler resume |
| `glowup/device/{id}/command/power` | `{"on": true}` or `{"on": false}` | Turn device on or off |

### Quick Start with Mosquitto

If you have Mosquitto installed on the same Pi as GlowUp:

```bash
# Install paho-mqtt
pip install paho-mqtt

# Add the mqtt section to server.json (see above), then restart:
python3 server.py server.json

# In another terminal — play an effect:
mosquitto_pub -t "glowup/device/10.0.0.62/command/play" \
  -m '{"effect":"aurora","params":{"speed":10.0,"brightness":60}}'

# Stop it:
mosquitto_pub -t "glowup/device/10.0.0.62/command/stop" -m ""

# Resume the scheduler:
mosquitto_pub -t "glowup/device/10.0.0.62/command/resume" -m ""

# Watch state changes:
mosquitto_sub -t "glowup/#" -v
```

For virtual multizone groups, use the group ID as the device:

```bash
mosquitto_pub -t "glowup/device/group:porch/command/play" \
  -m '{"effect":"cylon","params":{"speed":4.0}}'
```

### Availability (Last Will and Testament)

The bridge publishes `"online"` to `glowup/status` (retained) on
connect.  If the server crashes or loses its broker connection, the
broker automatically publishes `"offline"` to the same topic via
MQTT's Last Will mechanism.  Home automation systems can subscribe to
this topic to detect when GlowUp is unreachable.

### Notes

- **Dependency:** `paho-mqtt>=2.0` is the only pip dependency in the
  entire project and is required only for MQTT.  If you do not need
  MQTT, do not install it — the server works without it.
- **Scheduler conflict:** Commands received via MQTT set a phone
  override on the device, pausing the GlowUp scheduler.  Publish to
  the `resume` topic to hand control back.
- **Color publishing:** Disabled by default because it generates
  substantial broker traffic (one message per device per
  `color_interval` seconds).  Enable it only if you have a consumer
  for the data (e.g., a Node-RED dashboard visualizing zone colors).
- **Authentication:** The MQTT bridge does not enforce the REST API's
  bearer token — access control is handled by the MQTT broker itself
  (username/password, ACLs, TLS client certificates, etc.).
- **Reconnection:** If the broker restarts or the connection drops,
  paho-mqtt reconnects automatically with exponential backoff (1–60
  seconds).  All subscriptions are re-established on reconnect.
- **Untested:** This integration has not yet been tested against a
  live MQTT broker.  If you try it, please open an issue at the
  [GitHub repo](https://github.com/pkivolowitz/lifx/issues) with
  any corrections or suggestions.

