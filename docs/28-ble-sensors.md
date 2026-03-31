# BLE Sensor Integration

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

GlowUp integrates Bluetooth Low Energy (BLE) sensors via a
distributed architecture: a daemon on a remote Pi reads encrypted
HomeKit Accessory Protocol (HAP-BLE) sensor data and publishes it
to MQTT.  The server's `BleAdapter` subscribes and writes normalized
signals to the `SignalBus`, where operators and emitters consume
them like any other signal.

This separation keeps BLE hardware requirements off the server.  The
server needs only `paho-mqtt`, not `bleak` or BLE hardware.  The
BLE daemon can run on any Pi with Bluetooth within range of the
sensors.

---

## Architecture

```
+---------------------+          +---------------------+
|  broker-2 (Pi)      |          |  GlowUp server (Pi) |
|                     |   MQTT   |                     |
|  BLE sensor daemon  | -------> |  BleAdapter         |
|  (ble.sensor)       |          |  (ble_adapter.py)   |
|                     |          |                     |
|  bleak + crypto     |          |  paho-mqtt only     |
+---------------------+          +---------------------+
         |                                |
    BLE radio                        SignalBus
         |                                |
  +-------------+                  +-----------+
  | ONVIS SMS2  |                  | Operators |
  | (HAP-BLE)   |                  | Emitters  |
  +-------------+                  +-----------+
```

- **broker-2** (10.0.0.123) — Raspberry Pi with Bluetooth, running
  the BLE sensor daemon and the MQTT broker (Mosquitto).
- **GlowUp server** (10.0.0.48) — Subscribes to BLE MQTT topics
  via `BleAdapter`, writes signals to the bus.
- **Sensors** — HomeKit BLE accessories (currently ONVIS SMS2).
  Communicate using encrypted HAP-BLE GATT characteristics.

---

## MQTT Topic Format

The BLE daemon publishes to topics under the `glowup/ble/` prefix:

```
glowup/ble/{label}/motion        "1" or "0"
glowup/ble/{label}/temperature   float Celsius (e.g. "22.5")
glowup/ble/{label}/humidity      float percentage (e.g. "48.2")
glowup/ble/{label}/status        JSON health metadata blob
```

The `{label}` segment is the device label from the pairing
configuration (e.g. `hallway`, `bedroom`).

---

## Signal Normalization

The `BleAdapter` (server-side) subscribes to `glowup/ble/#` and
converts MQTT payloads into typed `SignalBus` signals:

| Subtopic | Bus Signal Name | Value Type | Normalization |
|----------|-----------------|------------|---------------|
| `motion` | `{label}:motion` | `float` | `int(float(payload))` -- produces `1.0` or `0.0` |
| `temperature` | `{label}:temperature` | `float` | Raw Celsius from payload, no conversion |
| `humidity` | `{label}:humidity` | `float` | Raw percentage from payload, no conversion |

All bus signals follow the `{source}:{signal}` naming convention.
The transport identifier `ble` is recorded in the signal's
`SignalMeta` for metadata queries, not in the signal name itself.

### Status Blobs

The `status` subtopic carries a JSON health metadata blob -- battery
level, firmware version, connection quality, last-seen timestamp.
Because it is a structured object (not a scalar), it is **not**
written to the `SignalBus`.  Instead, the `BleAdapter` stores it
in an internal `_status` dict, accessible via:

```python
blob = ble_adapter.get_status_blob("hallway")
# Returns dict or None if no status received yet.
```

This keeps the bus clean (scalars only) while preserving health data
for dashboard display and diagnostics.

---

## Server-Side Configuration

The `BleAdapter` is instantiated by the server when a `"ble"` section
is present in `server.json`:

```json
{
    "ble": {
        "broker": "10.0.0.123",
        "port": 1883
    }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `broker` | `str` | `"localhost"` | MQTT broker hostname or IP where the BLE daemon publishes |
| `port` | `int` | `1883` | MQTT broker port |

If the `"ble"` section is absent, the adapter is not created.  The
server runs without BLE support, and no `paho-mqtt` connection is
attempted.  This follows the project rule: everything above core is
optional.

### Guarded Import

The `BleAdapter` is imported with a guard in `server.py`:

```python
try:
    from ble_adapter import BleAdapter
    _HAS_BLE_ADAPTER = True
except ImportError:
    _HAS_BLE_ADAPTER = False
```

If `paho-mqtt` is not installed, the import succeeds but
`MqttAdapterBase.start()` logs a warning and returns without
subscribing (see [Adapter Base Classes](27-adapter-base.md)).

---

## The BLE Sensor Daemon

The daemon runs on the remote Pi (broker-2) and handles all BLE
communication.

### Running the Daemon

```bash
python3 -m ble.sensor --config ble_pairing.json --broker localhost
```

Or via the package entry point:

```bash
python3 -m ble sensor --config ble_pairing.json
```

### CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--config` | `ble_pairing.json` | Path to the pairing configuration file |
| `--broker` | value from `network_config` | MQTT broker address |
| `--port` | `1883` | MQTT broker port |
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

On the remote Pi, run the daemon as a persistent systemd service:

```ini
[Unit]
Description=GlowUp BLE sensor daemon
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/lifx
ExecStart=/usr/bin/python3 -m ble.sensor --config /etc/glowup/ble_pairing.json --broker localhost
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Save as `/etc/systemd/system/glowup-ble-sensor.service`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable glowup-ble-sensor
sudo systemctl start glowup-ble-sensor
```

See [Persistent Services](24-persistent-services.md) for the full
pattern.

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

### MQTT messages not arriving at the server

- Verify the BLE daemon is publishing: subscribe directly on the
  broker and watch for messages:
  ```bash
  mosquitto_sub -h 10.0.0.123 -t "glowup/ble/#" -v
  ```
- Check that the server's `server.json` has a `"ble"` section with
  the correct broker address.
- Verify network connectivity between the server Pi and broker-2.

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

- [Adapter Base Classes](27-adapter-base.md) — The `MqttAdapterBase`
  that `BleAdapter` inherits from
- [MQTT Integration](19-mqtt.md) — MQTT broker setup and topic
  conventions
- [SOE Pipeline](21-soe-pipeline.md) — How BLE signals flow through
  operators to emitters
- [Persistent Services](24-persistent-services.md) — systemd service
  patterns for all GlowUp components
