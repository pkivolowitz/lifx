# Chapter 28: BLE Sensors (`glowup-ble-sensor`)

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

> **What changed (2026-04-15):** Until this date the hub had an
> in-process `BleAdapter` (an `MqttAdapterBase` subclass) that
> consumed `glowup/ble/#` from a mosquitto bridge that forwarded
> messages from broker-2.  That adapter has been deleted.  In its
> place, `glowup-ble-sensor` runs as a standalone systemd unit on
> broker-2 and **publishes signals cross-host directly to the
> hub mosquitto** — same service pattern as
> [`glowup-zigbee-service`](29-zigbee-service.md), and for the same
> reason.  See [Chapter 35: Service vs. Adapter](35-service-vs-adapter.md)
> for the architectural decision rule and the incident history.

GlowUp integrates Bluetooth Low Energy (BLE) sensors via a
distributed service (not an in-process adapter).  The
`glowup-ble-sensor` daemon runs on broker-2 — the host that
physically owns the BT radio and the ONVIS sensor pairings — and
publishes normalized readings cross-host to the hub mosquitto over
its own paho client.  The hub does not subscribe across the network
for BLE data; cross-host MQTT subscribes are forbidden in this
codebase because they cannot detect their own silence.

This separation keeps BLE hardware requirements off the hub.  The
hub has no BT radio and never imports `bleak`, `cryptography`, or
the HAP libraries.  It just receives `glowup/signals/*` messages
the same way it receives signals from every other producer.

---

## Architecture

```
+---------------------------+              +-----------------------------+
|  broker-2 (Pi 5, .123)    |              |  glowup hub (Pi 5, .214)    |
|                           |              |                             |
|  +---------------------+  |              |  +-----------------------+  |
|  |  ONVIS SMS2 sensors |  |              |  |  hub mosquitto       |  |
|  |  (HAP-BLE)          |  |              |  |  (localhost only)    |  |
|  +---------+-----------+  |              |  +----------+------------+  |
|            |              |              |             |               |
|       BLE radio           |              |             |               |
|            |              |              |             |               |
|  +---------v-----------+  |  cross-host  |             |               |
|  | glowup-ble-sensor   |--|--paho.publish|------------>|               |
|  |  (this service)     |  |  glowup/signals/{l}:{p}    |               |
|  |  bleak + crypto +   |  |  glowup/ble/status/{l}     |               |
|  |  paho-mqtt          |  |              |             |               |
|  +---------------------+  |              |             |               |
|                           |              |             v               |
+---------------------------+              |  +-----------------------+  |
                                           |  | _on_remote_signal     |  |
                                           |  | (server.py)           |  |
                                           |  |                       |  |
                                           |  | + BleTriggerManager   |  |
                                           |  | (infrastructure/...)  |  |
                                           |  +----------+------------+  |
                                           |             |               |
                                           |             v               |
                                           |       SignalBus +           |
                                           |       BleSensorData store + |
                                           |       /api/ble/sensors      |
                                           +-----------------------------+
```

Two distinct topic schemas, by design:

- `glowup/signals/{label}:{prop}` — numeric scalars (motion,
  temperature, humidity).  Same schema as `glowup-zigbee-service`,
  consumed by the hub's `_on_remote_signal` callback in `server.py`,
  which writes them into the `SignalBus` and feeds power-related
  ones to `PowerLogger`.  Operators and automations see BLE
  signals on the bus identically to any other signal source.

- `glowup/ble/status/{label}` — JSON status blobs (state, paired
  sensor list, last_values, timestamp).  These are diagnostic
  metadata, not bus signals, so they ride a separate topic schema
  and are not written to the `SignalBus`.  `BleTriggerManager` on
  the hub subscribes to `glowup/ble/status/#` locally and stores
  the latest blob per label in its in-process `BleSensorData` store
  for the `/api/ble/sensors` diagnostic endpoint.

**Both topic schemas are published with `qos=0, retain=False`.**
Retained sensor data is an anti-pattern: it lets stale values lie
about reality after a restart.  The 2026-04-15 BLE outage was
masked for ~8 hours because the hub kept reading retained messages
from a dead bridge — see `feedback_multi_topic_config_deletion.md`.
Never retain sensor data.

---

## MQTT Topic Format

```
glowup/signals/{label}:motion        "1" or "0"        (numeric)
glowup/signals/{label}:temperature   float Celsius     (numeric, e.g. "22.5")
glowup/signals/{label}:humidity      float percentage  (numeric, e.g. "48.2")
glowup/ble/status/{label}            JSON blob         (diagnostic)
```

The `{label}` segment is the device label from
`/etc/glowup/ble_pairing.json` (e.g. `onvis_motion`,
`hallway_motion`).  The colon between label and property in the
signals schema is mandatory — that's what `_on_remote_signal`
splits on to derive the bus key.

---

## Hub-Side Consumption

After the pivot, three independent code paths on the hub consume
BLE data; all three are local subscribes on the hub's own broker
(no cross-host subscribes anywhere).

1. **`_on_remote_signal` in `server.py`** subscribes to
   `glowup/signals/#`.  Receives every BLE numeric reading,
   writes it into the `SignalBus` under `{label}:{prop}`, and
   stamps `broker2_signals_last_ts` for the `/api/home/health`
   liveness probe.  This is the same callback that handles
   `glowup-zigbee-service` traffic.

2. **`BleTriggerManager`** (in
   [`infrastructure/ble_trigger.py`](../infrastructure/ble_trigger.py))
   subscribes to **both** `glowup/signals/#` and
   `glowup/ble/status/#` locally on the hub broker.  It filters
   the signals stream by label against its `ble_triggers`
   configuration in `server.json`, hydrates the in-process
   `BleSensorData` store with motion / temperature / humidity /
   status fields, and runs the motion-trigger watchdog (no event
   in N minutes → group lights off).

3. **`/api/ble/sensors`** in [`handlers/sensors.py`](../handlers/sensors.py)
   reads the `BleSensorData` store directly (NOT the SignalBus).
   The store is the single source of truth for that endpoint; if
   you ever need to add fields, write them via
   `infrastructure.ble_trigger.sensor_data.update(...)`.

There is no in-process `BleAdapter` on the hub anymore.  Do not
recreate one.  See
[Chapter 35: Service vs. Adapter](35-service-vs-adapter.md) for
the rule and the rationale.

---

## Server-Side Configuration

There is no `"ble"` block in `server.json` for the BLE producer
itself — the producer lives on broker-2 and is configured by the
systemd unit on that host (see "Daemon Configuration" below).

`server.json` does still carry a `"ble_triggers"` block that
configures the hub-side `BleTriggerManager`:

```json
{
    "ble_triggers": {
        "onvis_motion": {
            "group": "group:living_room",
            "on_motion": {"brightness": 70},
            "watchdog_minutes": 30
        }
    }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `group` | `str` | required | Device group to control on motion |
| `on_motion.brightness` | `int` | `70` | Brightness % when motion detected |
| `watchdog_minutes` | `float` | `30` | Minutes of silence before "lights off" fires |

If the `"ble_triggers"` section is empty or absent, the manager
logs "no triggers configured" and exits cleanly.  BLE signals are
still received and stored — only the trigger logic is suppressed.

---

## The BLE Sensor Daemon

The daemon runs on the remote Pi (broker-2) and handles all BLE
communication.

### Running the Daemon

In production, the daemon runs as the `glowup-ble-sensor` systemd
unit on broker-2.  For ad-hoc testing on broker-2:

```bash
python3 -m ble.sensor --config /etc/glowup/ble_pairing.json
```

The default hub broker target is `192.0.2.214` (read from the
`GLB_HUB_BROKER` environment variable, set by the systemd unit).
To point at a non-production hub, override it:

```bash
GLB_HUB_BROKER=192.0.2.250 python3 -m ble.sensor \
    --config /etc/glowup/ble_pairing.json
```

### CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--config` | `ble_pairing.json` | Path to the pairing configuration file |
| `--hub-broker` | `$GLB_HUB_BROKER` or `192.0.2.214` | Hub mosquitto address — must point at the hub, NOT broker-2 localhost |
| `--hub-port` | `$GLB_HUB_PORT` or `1883` | Hub mosquitto port |
| `--poll-interval` | `1.0` | Seconds between motion polls (temperature/humidity polled every 30s regardless) |
| `--verbose`, `-v` | off | Enable debug logging |

Press Ctrl+C to stop.  The daemon handles `SIGINT` and `SIGTERM`
gracefully, cancelling all async tasks and disconnecting MQTT.

### What the Daemon Does

- Reads the pairing config to find paired devices.
- For each paired device, spawns an async monitor task that:
  - Connects via BLE (bleak).
  - Runs HAP pair-verify to establish an encrypted session.
  - Polls motion every `poll_interval` seconds.
  - Polls temperature and humidity at a lower rate.
  - Publishes all readings to MQTT.
  - Reconnects with exponential backoff on disconnection.
- For devices in `"gsn"` mode, runs a passive BLE advertisement
  scanner that detects motion from Global State Number changes
  (no encrypted connection needed).
- Publishes a status blob every 60 seconds (defined by
  `STATUS_INTERVAL`).

### Device Modes

| Mode | Connection | Use Case |
|------|------------|----------|
| `gatt` (default) | Encrypted GATT reads via pair-verify | Full sensor access (motion + temperature + humidity) |
| `gsn` | Passive advertisement scanning | Motion-only detection via GSN changes; no pairing session needed |

GSN mode is useful when a device has unreliable GATT connections
but still broadcasts advertisement updates when motion is detected.
Multiple GSN devices share a single scanner task.

---

## Pairing Configuration

The pairing config file (`ble_pairing.json`) stores device addresses,
pairing keys, and mode settings.  It is created by the `ble pair`
command and should not be edited by hand unless you know the key
format.

### File Format

```json
{
    "controller": {
        "ltsk": "hex-encoded-long-term-secret-key",
        "ltpk": "hex-encoded-long-term-public-key",
        "id": "controller-uuid"
    },
    "devices": {
        "hallway": {
            "address": "AA:BB:CC:DD:EE:FF",
            "type": "onvis-sms2",
            "paired": true,
            "mode": "gsn",
            "setup_code": "123-45-678",
            "accessory_ltpk": "hex-encoded-accessory-public-key"
        },
        "bedroom": {
            "address": "11:22:33:44:55:66",
            "type": "onvis-sms2",
            "paired": true,
            "mode": "gatt",
            "setup_code": "987-65-432",
            "accessory_ltpk": "hex-encoded-accessory-public-key"
        }
    }
}
```

| Field | Description |
|-------|-------------|
| `controller.ltsk` | Controller's long-term secret key (Curve25519), hex-encoded |
| `controller.ltpk` | Controller's long-term public key, hex-encoded |
| `controller.id` | Controller UUID (generated during first pairing) |
| `devices.{label}.address` | BLE MAC address of the accessory |
| `devices.{label}.type` | Device type identifier (e.g. `onvis-sms2`) |
| `devices.{label}.paired` | `true` if pair-setup has completed |
| `devices.{label}.mode` | `"gatt"` (encrypted reads) or `"gsn"` (passive scanning) |
| `devices.{label}.setup_code` | HomeKit setup code from the device (XXX-XX-XXX format) |
| `devices.{label}.accessory_ltpk` | Accessory's long-term public key, stored after pairing |

### Pairing a New Device

```bash
# Discover nearby HAP-BLE accessories:
python3 -m ble discover

# Pair with a device (add to registry first, then pair):
python3 -m ble pair hallway --code 123-45-678

# Re-pair a previously paired device:
python3 -m ble pair hallway --force
```

The `pair` command performs the full HomeKit pair-setup sequence
(SRP key exchange, Ed25519 key verification) and saves the resulting
long-term keys to the pairing config file.

---

## systemd Service

The canonical unit file lives at
[`deploy/broker-2/glowup-ble-sensor.service`](../deploy/broker-2/glowup-ble-sensor.service).
Read its top-of-file comment block for the install commands and
the rollback story.  The key bits:

- `Environment=GLB_HUB_BROKER=192.0.2.214` — the unit owns the
  hub address (per the installer-owns-config rule).  Change
  here if the hub IP ever moves and re-install the unit.
- `ExecStart=/usr/bin/python3 -m ble.sensor --config /etc/glowup/ble_pairing.json`
  — no `--broker` argument; the producer publishes only to the
  hub broker resolved from `GLB_HUB_BROKER`.
- `User=a` / `WorkingDirectory=/opt/glowup-sensors` — broker-2
  hosts the BLE service in `/opt/glowup-sensors/ble/` (NOT the
  dev `~/glowup` checkout — broker-2 is a non-dev host with files
  only, no repo).

Install / update from a dev machine (per the comments in the unit
file itself):

```bash
scp deploy/broker-2/glowup-ble-sensor.service \
    mortimer.snerd@192.0.2.123:/tmp/glowup-ble-sensor.service
ssh mortimer.snerd@192.0.2.123 \
  'sudo install -o root -g root -m 0644 \
     /tmp/glowup-ble-sensor.service \
     /etc/systemd/system/glowup-ble-sensor.service && \
   sudo systemctl daemon-reload && \
   sudo systemctl restart glowup-ble-sensor'
```

The python module itself (`ble/sensor.py` and friends) is deployed
separately into `/opt/glowup-sensors/ble/` on broker-2.

See [Persistent Services](24-persistent-services.md) for the full
pattern, and [Chapter 35: Service vs. Adapter](35-service-vs-adapter.md)
for why this lives on broker-2 rather than as an adapter on the hub.

---

## BLE Package Structure

The `ble/` package contains the daemon and its supporting modules:

| File | Purpose |
|------|---------|
| `__init__.py` | Package marker |
| `__main__.py` | CLI dispatcher (`python3 -m ble`) — sub-commands: `discover`, `pair`, `sensor`, `signal` |
| `sensor.py` | The sensor daemon — encrypted HAP-BLE reads, MQTT publishing, reconnect logic |
| `scanner.py` | BLE device discovery (bleak-based advertisement scanning) |
| `hap_session.py` | HAP pair-setup and pair-verify protocol implementation |
| `hap_pdu.py` | HAP-BLE PDU framing (request/response encoding) |
| `hap_constants.py` | HAP characteristic UUIDs, TLV type codes, error codes |
| `crypto.py` | ChaCha20-Poly1305 encryption/decryption for HAP sessions |
| `srp.py` | SRP-6a (Secure Remote Password) for HomeKit pair-setup |
| `tlv.py` | TLV8 encoding/decoding (HomeKit's type-length-value wire format) |
| `registry.py` | BLE device registry — tracks paired devices and their keys |
| `signal_meter.py` | Passive RSSI signal meter for debugging BLE range issues |

---

## Supported Sensors

### ONVIS SMS2

3-in-1 HomeKit sensor: motion + temperature + humidity.

- **Protocol:** HAP-BLE (HomeKit Accessory Protocol over BLE)
- **Encryption:** ChaCha20-Poly1305 with keys derived from SRP
  pair-setup
- **Motion detection:** PIR sensor, reported via HAP characteristic
  UUID `00000022` (MotionDetected)
- **Temperature:** Celsius float, HAP characteristic UUID `00000011`
  (CurrentTemperature)
- **Humidity:** Percentage float, HAP characteristic UUID `00000010`
  (CurrentRelativeHumidity)
- **Modes:** Works in both `gatt` (encrypted GATT reads) and `gsn`
  (passive advertisement scanning for motion only)
- **Known IIDs:** The daemon includes hardcoded Instance ID values
  for the SMS2 as a fallback when descriptor reads fail (they can
  desync the encrypted session by performing unencrypted reads
  after pair-verify)

### Adding New Sensor Types

New HAP-BLE sensor types can be added by:

- Adding the device's characteristic UUIDs to `SENSOR_NAMES` in
  `sensor.py` (maps UUID to a human-readable subtopic name).
- If the device uses non-standard IIDs, adding them to a
  `*_KNOWN_IIDS` dict alongside the existing `SMS2_KNOWN_IIDS`.
- Testing both `gatt` and `gsn` modes (not all devices broadcast
  GSN changes on motion).

Non-HomeKit BLE sensors (e.g. those using proprietary protocols)
would need a new monitor coroutine in `sensor.py` but can reuse the
same MQTT publishing path.

---

## Troubleshooting

### "No paired devices" on daemon start

The pairing config file has no entries with `"paired": true`.  Run
the pairing sequence first:

```bash
python3 -m ble discover
python3 -m ble pair <label> --code XXX-XX-XXX
```

### MQTT messages not arriving at the hub

`feedback_read_the_producer_first.md` codifies the lesson here:
**read the producer side first**.  The producer publishes
cross-host directly to the hub mosquitto, so check those topics
on the hub:

```bash
mosquitto_sub -h 192.0.2.214 -t 'glowup/signals/onvis_motion:#' -W 90 -v
mosquitto_sub -h 192.0.2.214 -t 'glowup/ble/status/#'           -W 90 -v
```

If both are silent, walk the data path producer-side:

1. Is `glowup-ble-sensor` running on broker-2?
   ```bash
   ssh mortimer.snerd@192.0.2.123 'systemctl is-active glowup-ble-sensor'
   ssh mortimer.snerd@192.0.2.123 'sudo journalctl -u glowup-ble-sensor -n 30 --no-pager'
   ```
2. Is it publishing successfully?  Look for lines like
   `pub onvis_motion → motion=1.0 rc=0` (rc=0 means the publish
   reached the hub mosquitto cleanly; any non-zero rc is a
   network-side failure visible in the log).
3. Has the hub mosquitto rejected the connection?  Check
   `journalctl -u mosquitto` on the hub for connect attempts
   from 192.0.2.123.
4. Is `GLB_HUB_BROKER` set correctly in the systemd unit?
   `ssh mortimer.snerd@192.0.2.123 'systemctl show -p Environment glowup-ble-sensor'`

Do not poke at any in-process `BleAdapter` on the hub — it
no longer exists.  Adapter-side debugging is the wrong direction
for this architecture.

### Frequent BLE disconnections

BLE connections are inherently fragile.  The daemon reconnects
automatically with exponential backoff (starting at 5 seconds,
capped at 60 seconds, defined by `RECONNECT_DELAY` and
`MAX_RECONNECT_DELAY`).

- Move the Pi closer to the sensor.  Use `python3 -m ble signal`
  to check RSSI at the current location.
- Switch the device to `"gsn"` mode in the pairing config if you
  only need motion detection.  GSN mode uses passive scanning and
  does not require a GATT connection.
- Check for BlueZ conflicts: only one BLE scan can run at a time.
  The daemon serializes scans with an asyncio lock
  (`_scan_lock`), but other Bluetooth software on the same Pi
  may interfere.

### Motion reads as 0 but sensor LED blinks

The encrypted session may have desynced.  Restart the daemon to
force a fresh pair-verify:

```bash
sudo systemctl restart glowup-ble-sensor
```

If the problem persists, switch to `"gsn"` mode for that device.

---

## See Also

- [Chapter 35: Service vs. Adapter](35-service-vs-adapter.md) —
  The architectural decision rule and the "what changed and why"
  story.  Read this first if you ever wonder whether a future
  sensor type should live on the hub or on broker-2.
- [Chapter 29: Zigbee Service](29-zigbee-service.md) — The other
  service-pattern producer on broker-2; same architecture.
- [Chapter 27: Adapter Base Classes](27-adapter-base.md) — Note
  that `glowup-ble-sensor` is **not** an `AdapterBase` subclass.
  Services join the SOE pipeline at the same point an adapter
  would, but from the producer side of the wire.
- [Chapter 19: MQTT Topology](19-mqtt.md) — Broker layout and
  topic conventions.
- [Chapter 21: SOE Pipeline](21-soe-pipeline.md) — How signals
  flow through operators to emitters once a producer (adapter or
  service) writes them.
- [Persistent Services](24-persistent-services.md) — systemd
  service patterns for all GlowUp components.
