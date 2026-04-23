# Device Registry

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

LIFX devices get their IP addresses from DHCP.  Swap a router, move
a bulb to a different room, or wait long enough for a lease to expire,
and every IP-based configuration breaks.  The device registry solves
this by making **MAC addresses** the permanent identity for every
device and resolving IPs at runtime through ARP.

The registry also extends beyond LIFX.  Any network device — cameras,
printers, Zigbee coordinators — can be registered, giving the entire
GlowUp platform a single source of truth for "what is this device and
where is it right now?"

---

## Concepts

- **MAC-based identity** — A device's MAC address never changes.
  DHCP can reassign IPs freely; the registry maps MAC to a
  human-readable label and resolves the current IP on demand.

- **Labels** — Short, unique names (max 32 bytes UTF-8, matching the
  LIFX firmware `SetLabel` limit).  Labels are case-insensitive for
  lookups but preserve the original casing.

- **Runtime IP resolution** — The server never stores IPs as
  configuration.  It asks the `BulbKeepAlive` daemon's ARP table
  "what IP does this MAC have right now?" every time it needs to
  talk to a device.

- **Offline registration** — Devices that are powered off or
  unreachable can be registered with an explicit MAC and IP.  The
  stored IP is a hint for reverse lookups; the ARP table is still
  preferred when the device comes online.

---

## Registry File

The registry lives outside the git repo so code updates never
overwrite device identity:

```
/etc/glowup/device_registry.json
```

Override the path with the `GLOWUP_DEVICE_REGISTRY` environment
variable.

### File Format

```json
{
    "_comment": "MAC-based device identity.  Survives DHCP changes and git pulls.  Do not edit while server is running.",
    "devices": {
        "d0:73:d5:69:70:db": {
            "label": "porch-left",
            "notes": "front porch, left side"
        },
        "d0:73:d5:69:e3:82": {
            "label": "bedroom-neon",
            "notes": "above headboard"
        }
    }
}
```

- Keys are lowercase colon-separated MAC addresses.
- Each entry must have a non-empty `label` (unique across the registry).
- `notes` is optional free-form text for your own reference.
- `ip` may appear on entries registered with `--offline`; it is a
  fallback hint, not authoritative.

### Validation Rules

The registry enforces these constraints on load:

- MAC addresses must match the pattern `xx:xx:xx:xx:xx:xx`
  (lowercase hex, colon-separated).
- Labels must be non-empty and no longer than 32 bytes when
  UTF-8 encoded (the LIFX firmware `SetLabel` limit).
- Labels must be unique (case-insensitive).  Two devices cannot
  share a label.
- Any violation raises `ValueError` with a descriptive message.

### Atomic Writes

The registry writes to a `.tmp` file first, then atomically replaces
the target with `os.replace()`.  A crash or power loss mid-write
cannot corrupt the registry.

---

## The `register_device.py` CLI Tool

A thin HTTP client that talks to the server's registry API.  The
server owns the registry file; this tool never touches it directly.

### Prerequisites

- The GlowUp server must be running.
- A bearer token file at `~/.glowup_token` (see
  [REST API](11-rest-api.md) for token setup).

### Commands

#### Register a Device

```bash
python3 register_device.py 10.0.0.164 "porch-left"
python3 register_device.py d0:73:d5:69:70:db "porch-left"
```

Pass an IP address or a MAC address as the first argument.  When an
IP is given, the server resolves it to a MAC via the keepalive ARP
table.  After registration, the server writes the label to the bulb's
firmware (LIFX `SetLabel`).

If the label is omitted, the tool prompts interactively:

```bash
python3 register_device.py 10.0.0.164
Label for 10.0.0.164: porch-left
```

#### Register an Offline Device

```bash
python3 register_device.py --offline 10.0.0.200 aa:bb:cc:dd:ee:ff "nvr-cam-1"
```

When both IP and MAC are known but the device is not reachable, use
`--offline`.  The server skips the ARP lookup and stores both values.
The firmware label write is deferred until the device comes online.

| Argument | Description |
|----------|-------------|
| `<ip>` | Static or last-known IP address |
| `<mac>` | MAC address of the device |
| `<label>` | Human-readable label |

#### Remove a Device

```bash
python3 register_device.py --remove porch-left
python3 register_device.py --remove d0:73:d5:69:70:db
```

Accepts a label or a MAC address.  Removes the device from the
registry and saves immediately.

#### List All Devices

```bash
python3 register_device.py --list
```

Prints a table of all registered devices with their MAC, label,
current IP (from the keepalive ARP table), online/offline status,
and notes.

```
MAC Address           Label                     IP Address       Status    Notes
================================================================================
d0:73:d5:69:70:db     porch-left                10.0.0.164       online    front porch
d0:73:d5:69:e3:82     bedroom-neon              -                offline   above headboard
```

#### Push All Labels to Firmware

```bash
python3 register_device.py --push-labels
```

Iterates every registered device, resolves its current IP via ARP,
and writes the registry label to the bulb's firmware using the LIFX
`SetLabel` packet.  Useful after a factory reset or when deploying
new bulbs in bulk.

Output reports per-device status:

```
  OK       d0:73:d5:69:70:db  porch-left -> 10.0.0.164
  OFFLINE  d0:73:d5:69:e3:82  bedroom-neon

Pushed: 1  Failed: 0  Offline: 1
```

#### Clear a Firmware Label

```bash
python3 register_device.py --clear-label 10.0.0.164
python3 register_device.py --clear-label d0:73:d5:69:70:db
```

Writes a single space to the bulb's firmware label.  LIFX firmware
ignores all-null labels, so a space character is the smallest value
the firmware accepts as a real write.

### The `--force` Flag

Add `--force` to any registration command to reassign a label that is
already in use by a different MAC address.  Without `--force`, the
server rejects duplicate labels:

```bash
# Label "porch-left" is already assigned to d0:73:d5:69:70:db.
# Reassign it to the new MAC:
python3 register_device.py --force d0:73:d5:69:e3:82 "porch-left"
```

When forced, the old MAC's entry is removed entirely.

---

## REST API Endpoints

All endpoints require a bearer token in the `Authorization` header.

### GET /api/registry

List all registered devices with live status.

**Response:**

```json
{
    "devices": [
        {
            "mac": "d0:73:d5:69:70:db",
            "label": "porch-left",
            "ip": "10.0.0.164",
            "online": true,
            "notes": "front porch"
        }
    ]
}
```

**Example:**

```bash
curl -H "Authorization: Bearer TOKEN" http://10.0.0.214:8420/api/registry
```

### POST /api/registry/device

Register or update a device.

**Request body:**

```json
{
    "ip": "10.0.0.164",
    "label": "porch-left",
    "force": false
}
```

Or with a MAC address directly:

```json
{
    "mac": "d0:73:d5:69:70:db",
    "label": "porch-left"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ip` | string | one of `ip` or `mac` | Device IP (server resolves to MAC via ARP) |
| `mac` | string | one of `ip` or `mac` | Device MAC address |
| `label` | string | yes | Human-readable label (max 32 bytes UTF-8) |
| `force` | bool | no | Reassign label from its current MAC (default `false`) |

**Response:**

```json
{
    "mac": "d0:73:d5:69:70:db",
    "label": "porch-left",
    "firmware_written": true
}
```

### DELETE /api/registry/device/{identifier}

Remove a device by MAC or label.

**URL parameter:**

| Parameter | Description |
|-----------|-------------|
| `{identifier}` | MAC address (URL-encoded colons) or label |

**Response:**

```json
{
    "removed": "porch-left"
}
```

**Example:**

```bash
curl -X DELETE -H "Authorization: Bearer TOKEN" \
    http://10.0.0.214:8420/api/registry/device/porch-left
```

### POST /api/registry/push-labels

Write all registry labels to bulb firmware.

**Request body:** `{}` (empty JSON object)

**Response:**

```json
{
    "results": [
        {
            "mac": "d0:73:d5:69:70:db",
            "label": "porch-left",
            "ip": "10.0.0.164",
            "status": "ok"
        },
        {
            "mac": "d0:73:d5:69:e3:82",
            "label": "bedroom-neon",
            "status": "offline"
        }
    ]
}
```

### POST /api/registry/push-label

Write a specific label to a single device's firmware.

**Request body:**

```json
{
    "ip": "10.0.0.164",
    "label": "porch-left"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ip` | string | one of `ip` or `mac` | Device IP address |
| `mac` | string | one of `ip` or `mac` | Device MAC address |
| `label` | string | yes | Label to write (a single space clears the label) |

---

## How Label Resolution Works

When the server receives a command referencing a device by label
(e.g. `glowup play cylon --device "porch-left"`), the resolution
chain is:

- **Label to MAC** — The `DeviceRegistry` does a case-insensitive
  lookup in its `_label_to_mac` dict.  O(1).

- **MAC to IP** — The `BulbKeepAlive` daemon maintains a live ARP
  table mapping MAC addresses to their current IPs.  The registry
  calls `keepalive.ip_for_mac(mac)` to get the current IP.

- **IP to device** — Standard LIFX UDP communication on port 56700.

The reverse path also works:

- **IP to MAC** — The keepalive daemon's `known_bulbs` dict maps
  IPs to MACs.

- **MAC to label** — The registry's `mac_to_label()` method.

This two-step resolution means the server never needs to store IP
addresses in configuration.  If a router reassigns DHCP leases,
the keepalive daemon discovers the new IPs automatically and the
registry labels continue to work.

### The `resolve_identifier` Method

The `DeviceRegistry.resolve_identifier()` method accepts any of
the three identifier forms (IP, MAC, or label) and returns a
`(mac, label)` tuple:

```python
mac, label = registry.resolve_identifier("porch-left")
# mac = "d0:73:d5:69:70:db", label = "porch-left"

mac, label = registry.resolve_identifier("d0:73:d5:69:70:db")
# mac = "d0:73:d5:69:70:db", label = "porch-left"

mac, label = registry.resolve_identifier("10.0.0.164")
# mac = None, label = None  (IP requires ARP table, not available here)
```

For full resolution including IPs, use `resolve_to_ip()` which
takes a keepalive daemon instance:

```python
ip = registry.resolve_to_ip("porch-left", keepalive)
# ip = "10.0.0.164"  (or None if offline)
```

---

## Integration with BulbKeepAlive

The `BulbKeepAlive` daemon runs on the server and maintains a live
picture of which devices are online and at what IP.  The registry
delegates all IP resolution to it.

- **ARP scanning** — Every few seconds, keepalive reads the kernel
  ARP table and identifies LIFX devices by their OUI prefix
  (`d0:73:d5`).

- **MAC-to-IP mapping** — `keepalive.known_bulbs_by_mac` is a dict
  of `{mac: ip}` updated continuously.

- **`ip_for_mac(mac)`** — Returns the current IP for a MAC, or
  `None` if the device is offline.

- **`format_table(keepalive)`** — The registry's display method
  accepts an optional keepalive instance.  When provided, the table
  includes the live IP and online/offline status for each device.

Without a keepalive daemon (e.g. in tests or CLI-only mode), the
registry still works for MAC-to-label and label-to-MAC lookups.
IP resolution simply returns `None`.

---

## Thread Safety

The `DeviceRegistry` is thread-safe.  Two locks protect concurrent
access:

- **`_lock`** — Guards the in-memory dicts (`_devices` and
  `_label_to_mac`).  Lookups acquire this lock briefly for O(1)
  reads.

- **`_io_lock`** — Serializes file I/O (`load` and `save`).
  Prevents concurrent read-modify-write races when the server
  handles multiple registration requests simultaneously.

The locks are separate so that lookup operations (which are frequent)
never block on disk I/O (which is rare).

---

## See Also

- [Server Routing and Safety](25-server-routing-safety.md) — How
  the server resolves labels to devices for command routing
- [REST API Server](11-rest-api.md) — Full API reference
- [Persistent Services](24-persistent-services.md) — Running the
  server as a systemd service
