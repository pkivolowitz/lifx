# Server Routing and Safety Features

GlowUp is **server-preferred**: when a server is reachable, all commands
route through it — discovery, identification, and effect playback.  The
server resolves device labels and MACs to live IPs, manages keepalive,
and runs effects directly.  If the server is unreachable, the CLI falls
back to direct UDP for commands that support it.

Server routing also bypasses mesh router UDP filtering.  Emergency
power-off provides physical safety when hardware misbehavior causes
distress.

## Quick Start

**Play by device label (server resolves label → IP, runs effect):**
```bash
glowup play cylon --device "PORCH STRING LIGHTS"
```

**Server routing (automatic for discover/identify):**
```bash
glowup discover              # Routes via server if reachable; falls back to UDP
glowup identify --ip 192.0.2.28  # Same: server if available, else direct UDP
```

**Emergency power-off:**
```bash
glowup off                   # Requires typing "off" to confirm
```

**Force local (no server):**
```bash
glowup --local discover      # Direct UDP only, never contacts server
glowup --local identify --ip 192.0.2.28
```

**Use non-default server:**
```bash
glowup --server 192.168.1.100:8420 discover
```

---

## Server Routing: The Problem

On a TP-Link Deco mesh network, UDP packets sent from client machines
(laptops, desktops) are **blocked between wireless mesh nodes**.  A
bulb associated with a distant node is unreachable from your dev
machine — even though the Deco app shows it online and ARP can see it.

Result: `glowup discover` finds nothing.  `glowup identify` fails.  You
must SSH to the Pi and run commands there manually.

## Server Routing: The Solution

When the Pi (server) is reachable, glowup **routes all device-facing
commands through it automatically**.  The Pi has unobstructed UDP
access to every bulb on the LAN because it's on the same wired
connection or directly adjacent on the mesh.

### How It Works

1. **Startup probe** (1.5s timeout):
   - `glowup` sends a quick `GET /api/status` to the server
   - If it succeeds: `_server_url` is set; all commands route through the server
   - If it fails or times out: falls back to direct UDP
   - Startup prints: `Routing via server at X` or `Server unreachable — running locally`

2. **Command routing:**
   - `glowup discover` → `GET /api/command/discover[?ip=X]` on the server
   - `glowup identify --ip X` → `POST /api/command/identify` on the server
   - Discovery results and device info printed exactly as if UDP was used

3. **Automatic cancellation:**
   - Server never starts duplicate pulses on the same IP
   - If you call `identify` again on `.28` while one is running, the old pulse
     is cancelled (stop event set) and a new one starts
   - Prevents the "fighting bulb" symptom

### Supported Commands

| Command | Routing | Notes |
|---------|---------|-------|
| `discover` | Server or UDP | All bulbs on LAN queried in parallel |
| `discover --ip X` | Server or UDP | Specific device only |
| `identify --ip X` | Server or UDP | Pulse runs async on server; cancel via Ctrl+C |
| `identify --duration N` | Server only | Pulse duration (default 10s); ignored locally |
| `effects` | Local | No devices involved |
| `play --device` | Server | Server resolves label/MAC → IP, runs effect, sends packets |
| `play --group` | Server or local | Group fetched from server (or `--config` for local file) |
| `play --ip` | Server or UDP | Direct connection to device; works standalone |
| `record` | Local | No devices involved |
| `replay` | Local (MQTT) | Publishes to MQTT broker; handled by SOE pipeline |

### Flags

| Flag | Effect |
|------|--------|
| `--server HOST:PORT` | Override default server address; probe it at startup |
| `--local` | Force direct UDP; skip server probe entirely; never contact server |

**Default server:** `{net.server}:8420` (defined in `network_config.py`)

### API Endpoints (Server-Side)

These endpoints enable client-side routing.  Intended for automated
discovery and identification on networks where mesh filtering blocks
direct UDP.

#### GET /api/command/discover[?ip=X]

Query device(s) from the server.  Executes UDP queries from the Pi
using `LifxDevice.query_all()` in parallel (one thread per IP).
Non-responsive devices are omitted.

**Query parameters:**
- `ip=<device-ip>` (optional): Query only this device.  If omitted, queries
  all bulbs known to the keepalive daemon (see [ARP Keepalive](#arp-keepalive)).

**Response:**
```json
{
  "devices": [
    {
      "ip":      "192.0.2.41",
      "mac":     "d0:73:d5:69:70:db",
      "label":   "Bedroom Neon",
      "product": "LIFX Neon",
      "zones":   100,
      "group":   "bedroom"
    },
    ...
  ]
}
```

**Example:**
```bash
curl -H "Authorization: Bearer TOKEN" http://192.0.2.214:8420/api/command/discover
curl -H "Authorization: Bearer TOKEN" "http://192.0.2.214:8420/api/command/discover?ip=192.0.2.41"
```

#### POST /api/command/identify

Pulse a device's brightness to locate it.  Works for any IP — no need
to configure the device in `server.json`.  The pulse runs in a daemon
thread; the HTTP response returns immediately.

**Request body:**
```json
{
  "ip":       "192.0.2.41",
  "duration": 10.0
}
```

or using a registry label / MAC address:
```json
{
  "device":   "Bedroom Neon",
  "duration": 10.0
}
```

**Parameters:**
- `ip` (string): Device IP address
- `device` (string): Device registry label or MAC address (resolved by server)
- `duration` (optional, float): Pulse duration in seconds (default 10s; max 60s)

One of `ip` or `device` is required.  If `device` is given, the server
resolves it to a live IP via the device registry and ARP table.

**Response:**
```json
{
  "ip":          "192.0.2.41",
  "identifying": true,
  "duration":    10.0,
  "device": {
    "ip":      "192.0.2.41",
    "mac":     "d0:73:d5:69:70:db",
    "label":   "Bedroom Neon",
    "product": "LIFX Neon",
    "zones":   100,
    "group":   ""
  }
}
```

**Example:**
```bash
curl -X POST -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"ip":"192.0.2.41","duration":15}' \
  http://192.0.2.214:8420/api/command/identify
```

#### DELETE /api/command/identify/{ip}

Cancel a running identify pulse early.

**Parameters:**
- `{ip}` (in URL): The device IP whose pulse should be cancelled

**Response:**
```json
{
  "ip":        "192.0.2.41",
  "cancelled": true
}
```

**Error (no pulse running):**
```json
{
  "error": "No active identify pulse for 192.0.2.41"
}
```

**Example:**
```bash
curl -X DELETE -H "Authorization: Bearer TOKEN" \
  http://192.0.2.214:8420/api/command/identify/192.0.2.41
```

#### GET /api/command/identify/cancel-all

Cancel all active identify pulses on all devices at once.  Used by the
emergency `off` command and available for cleanup/emergency use.

**Response:**
```json
{
  "cancelled": 2
}
```

---

## Emergency Power-Off: `glowup off`

**Safety command that powers off every LIFX device on the network.**

### When to Use

- **Physical distress:** Overlapping identify pulses (rare, but possible)
  cause bulbs to fight each other, creating rapidly flashing patterns
  that may trigger photo-sensitive reactions
- **Runaway effects:** An effect stuck in an infinite loop with no easy
  stop button
- **Any emergency:** Total network kill-switch for all lights

### How It Works

Requires **explicit confirmation** (type "off") to prevent accidental
activation.

```bash
$ glowup off

⚠️  EMERGENCY POWER-OFF ⚠️
This will immediately power off ALL LIFX devices on the network.
Type 'off' to confirm, or press Ctrl+C to cancel.

Confirm: off

Powering off all devices...

✓ Broadcast power-off sent to local subnet
✓ Server powered off 6 configured device(s)
✓ Cancelled 0 identify pulse(s) on server

✓ Emergency power-off complete
```

### How It Works (Technical)

1. **Broadcast power-off** (immediate, server-independent):
   - Sends a direct UDP SetPower(False) broadcast frame to port 56700
   - Works on the local subnet regardless of server state
   - All bulbs receive and execute immediately (0ms transition)

2. **Server power-off** (configured devices):
   - POSTs to `/api/server/power-off-all`
   - Server powers off every device in its `server.json` config
   - Returns count of devices sent the power-off command
   - Failures on individual devices don't stop others (fail-open)

3. **Cancel identify pulses** (cleanup):
   - GETs `/api/command/identify/cancel-all`
   - Sets stop event for every running identify pulse
   - Returns count of cancelled pulses

### Confirmation Mechanism

Confirmation **cannot be typo'd**.  Typing "off " (with trailing space),
"Off", "OFF", or anything else prints "Confirmation mismatch" and
cancels.  This prevents accidental activation.

### Can I Cancel It?

**Before typing confirmation:** Press Ctrl+C anytime before the prompt
to exit.

**After typing confirmation:** Broadcast and server power-off execute
immediately — **cannot be cancelled**.  (By design: safety command
should be unstoppable once confirmed.)

### What Gets Powered Off?

**Broadcast (all on subnet):**
- Every LIFX bulb, strip, or string light listening on port 56700
- Works even if the bulb is not in `server.json`
- Works even if the server is offline

**Server-side (configured only):**
- Devices explicitly listed in `/etc/glowup/server.json` on the Pi
- Those with static reservations or group assignments

**Total:** Roughly "everything LIFX you know about" + "any other LIFX
device on the network".  Some may not respond (bad connection, dead
battery, power-cycled), but the command is sent.

### API Endpoint

#### POST /api/server/power-off-all

Bulk power-off of all configured devices.  Used by `glowup off` and
available for programmatic use (e.g., HomeKit shortcut, HA automation).

**Request body:** `{}` (empty JSON object)

**Response:**
```json
{
  "devices_off": 6
}
```

**Example:**
```bash
curl -X POST -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' \
  http://192.0.2.214:8420/api/server/power-off-all
```

---

## ARP Keepalive: Discovering Distant Bulbs

The server's `bulb_keepalive.py` daemon discovers LIFX devices via
**ARP table scanning** — it never misses bulbs that are online, even if
they're hidden behind mesh router UDP filtering.

**Every 60 seconds:** `keepalive` pings the entire subnet to populate
the kernel ARP cache.  This forces the Deco to forward ARP replies from
all nodes.

**Every 5 seconds:** ARP scan reads `/proc/net/arp` (Linux) or `arp -an`
(macOS) and discovers all devices with LIFX MAC prefix (`d0:73:d5:*`).

**Every 15 seconds:** Unicast GetService pings each known bulb to keep
WiFi radios awake (prevents power-save deep sleep).

Result: The dashboard's "Found (ARP)" section always shows what's
actually online, even on a fragmented mesh network.

For details see [MQTT](19-mqtt.md) and
the source: [bulb_keepalive.py](../bulb_keepalive.py).

---

## Troubleshooting

### "Server unreachable — running locally"

The server probe timed out or failed.  Possible causes:

- Server is offline or restarting
- Network is partitioned (e.g., on a different VLAN)
- Firewall blocking port 8420
- Token file missing or invalid (`~/.glowup_token`)

Check:
```bash
curl -H "Authorization: Bearer $(cat ~/.glowup_token)" http://192.0.2.214:8420/api/status
```

If it fails, debug the network/server issue.  `glowup` will fall back to
direct UDP automatically.

### "discover" finds bulbs from phone app but not from glowup

**Server routing is working — you just need to wait.**

When bulbs first come online, the keepalive daemon's subnet sweep and
ARP scan take ~60 seconds to find them.  Wait, then try again:

```bash
sleep 65
glowup discover
```

Or force a direct UDP scan (may fail on Deco):
```bash
glowup --local discover
```

### Identify pulse won't stop

**On direct UDP:** Press Ctrl+C to stop the interactive loop.

**On server:** Press Ctrl+C.  The CLI sends `DELETE /api/command/identify/{ip}`
to the server, setting the stop event.  The pulse should end within
`IDENTIFY_FRAME_INTERVAL` (50ms) and power the bulb off.

If the server doesn't respond:
1. Try manually:
   ```bash
   curl -X DELETE -H "Authorization: Bearer $(cat ~/.glowup_token)" \
     http://192.0.2.214:8420/api/command/identify/192.0.2.28
   ```
2. If that fails, use `glowup off` (nuclear option — powers off everything).

### Overlapping identify pulses ("fighting bulb")

**Already fixed:** The server now cancels any existing pulse on an IP
before starting a new one.  But if you have very old code or are using
the API directly, you can trigger this by calling identify twice
simultaneously on the same IP.

Fix: Use the cancel endpoint:
```bash
curl -X DELETE -H "Authorization: Bearer $(cat ~/.glowup_token)" \
  http://192.0.2.214:8420/api/command/identify/192.0.2.28
```

Or use `glowup off`.

---

## Summary

| Feature | What | Why | How |
|---------|------|-----|-----|
| **Server routing** | Auto-use Pi for all commands | Bypass mesh UDP filtering; label addressing | `glowup play --device "FOO"` (automatic) |
| **Label addressing** | Use device names instead of IPs | Survive DHCP, router swaps, power cycles | `--device "PORCH STRING LIGHTS"` |
| **Force local** | Skip server; use direct UDP | Testing; same-machine server | `glowup --local discover` |
| **Custom server** | Specify non-default server IP | Multiple Pi networks | `glowup --server 192.168.1.100:8420` |
| **Emergency off** | Power off all devices | Safety; runaway effects | `glowup off` (requires confirmation) |
| **Cancel identify** | Stop a pulse early | User-initiated stop | Ctrl+C during `identify`; or DELETE API |
| **ARP keepalive** | Continuous device discovery | Find mesh-hidden bulbs | Automatic (every 60s subnet sweep) |

---

## Handler Mixin Architecture

The server's request handler uses a mixin pattern -- 12
domain-specific handler classes in `handlers/` are composed into a
single `GlowUpRequestHandler`.  Routes are declared in a table
(`_ROUTES`) and dispatched automatically.

## See Also

- [Troubleshooting](14-troubleshooting.md) -- General issues
- [REST API Server](11-rest-api.md) -- Full API reference
- [Quick Start](03-quick-start.md) -- Getting started
