# REST API Server

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

The REST API server (`server.py`) exposes all GlowUp functionality over
HTTP, enabling remote control from the iOS app or any HTTP client.  It
replaces `scheduler.py` by managing effects directly through the
`Controller` API instead of spawning subprocesses.

```bash
python3 server.py server.json              # start the server
python3 server.py --dry-run server.json    # preview resolved schedule
```

### Server Configuration

The server reads a JSON configuration file that combines server settings
with the same schedule format used by `scheduler.py`.

**Device identifiers** in the `groups` section can be registry labels,
MAC addresses, or raw IP addresses.  At startup the server resolves
each entry to a live IP via the device registry (label → MAC) and the
keepalive daemon's ARP table (MAC → IP).  This means device IPs are a
runtime detail, not a configuration input — devices survive DHCP
reassignment, router swaps, and power cycles without config changes.

```json
{
    "port": 8420,
    "auth_token": "your-secret-token-here",
    "location": {
        "latitude": 43.07,
        "longitude": -89.40
    },
    "groups": {
        "porch": ["Porch Front", "PORCH STRING LIGHTS"],
        "office": ["Dragon Fly 1A", "Dragon Fly 1B", "Dragon Fly 2A", "Dragon Fly 2B"],
        "unregistered": ["d0:73:d5:69:59:41", "d0:73:d5:6a:cd:ba"]
    },
    "schedule": [
        {
            "name": "evening aurora",
            "group": "porch",
            "start": "sunset-30m",
            "stop": "23:00",
            "effect": "aurora",
            "params": {"speed": 10.0, "brightness": 60}
        },
        {
            "name": "weekend cylon",
            "days": "SU",
            "group": "porch",
            "start": "sunset",
            "stop": "23:00",
            "effect": "cylon",
            "params": {"speed": 4.0}
        }
    ]
}
```

Group entries accept three formats:

- **Registry labels** (e.g. `"PORCH STRING LIGHTS"`) — resolved via
  the device registry (`/etc/glowup/device_registry.json`) to a MAC,
  then via ARP to a live IP.  Most readable; recommended.
- **MAC addresses** (e.g. `"d0:73:d5:69:59:41"`) — resolved directly
  via ARP.  Use for devices not yet registered in the registry.
- **Raw IPs** (e.g. `"10.0.0.35"`) — passed through unchanged.
  Backward compatible but fragile; breaks when DHCP reassigns.

Generate a secure token:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

The `groups` section is **required** — it is the server's only source of
devices.  The server does not perform broadcast discovery; instead it
resolves each configured identifier at startup, performs a subnet sweep
to populate the ARP table, then queries each device directly.  This is
both faster and more reliable than broadcast discovery, which requires
multiple retries with long timeouts and is defeated by mesh routers that
filter broadcast packets between nodes.

Groups with two or more devices automatically appear as a virtual
multizone device in the API and the iOS app, identified by
`group:<name>` (e.g., `group:porch`).  The individual member devices
also appear for independent control.  The scheduler plays effects on
the virtual device so the animation spans all member devices as a
unified canvas.

The `schedule` section is optional — the server works in API-only mode
without it.

The optional `nicknames` section maps device IPs to custom display names
shown in the iPhone app.  With label-based config, registry labels
typically serve this purpose.

**API device addressing:** All `/api/devices/{id}/...` endpoints accept
registry labels, MAC addresses, or IPs as the device identifier.  Labels
with spaces are URL-encoded (e.g., `PORCH%20STRING%20LIGHTS`).  The
server resolves the identifier to an internal IP before dispatching to
the handler.

### API Endpoints

All endpoints require a bearer token in the `Authorization` header:

```
Authorization: Bearer your-secret-token-here
```

#### Core

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | Server readiness and version |
| `GET` | `/api/devices` | List all configured devices |
| `GET` | `/api/effects` | List effects with parameter metadata and device affinity |

#### Device Control

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/devices/{id}/status` | Current effect name, params, FPS, override state |
| `GET` | `/api/devices/{id}/colors` | Snapshot of zone HSBK values |
| `GET` | `/api/devices/{id}/colors/stream` | SSE stream of zone colors at 4 Hz |
| `POST` | `/api/devices/{id}/play` | Start an effect (body: `{"effect":"name","params":{...}}`) |
| `POST` | `/api/devices/{id}/stop` | Stop current effect (fade to black) |
| `POST` | `/api/devices/{id}/resume` | Clear phone override, resume schedule |
| `POST` | `/api/devices/{id}/power` | Power on/off (body: `{"on": true}`) |
| `POST` | `/api/devices/{id}/brightness` | Set brightness (body: `{"brightness": 0-100}`) |
| `POST` | `/api/devices/{id}/identify` | Pulse brightness to locate device |
| `POST` | `/api/devices/{id}/reintrospect` | Re-query device geometry (zone count, tile chain) |
| `POST` | `/api/devices/{id}/nickname` | Set custom display name (body: `{"nickname":"..."}`) |
| `POST` | `/api/devices/{id}/reset` | Deep-reset device hardware |

#### Groups

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/groups` | Device groups from config |
| `POST` | `/api/groups` | Create a new group (body: `{"name":"...","members":[...]}`) |
| `PUT` | `/api/groups/{name}` | Update a group |
| `DELETE` | `/api/groups/{name}` | Delete a group |

#### Scheduling

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/schedule` | Schedule entries with resolved times |
| `POST` | `/api/schedule` | Create a new schedule entry |
| `PUT` | `/api/schedule/{index}` | Update a schedule entry |
| `DELETE` | `/api/schedule/{index}` | Delete a schedule entry |
| `POST` | `/api/schedule/{index}/enabled` | Enable or disable a schedule entry |

#### Effect Defaults

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/effects/{name}/defaults` | Save tuned parameters as defaults |

#### Device Registry

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/registry` | List registered devices with live status |
| `POST` | `/api/registry/device` | Add or update a device (body: `{"mac":"...","label":"..."}`) |
| `POST` | `/api/registry/push-label` | Write one label to bulb firmware |
| `POST` | `/api/registry/push-labels` | Write all labels to bulb firmware |
| `DELETE` | `/api/registry/device/{mac}` | Remove device from registry |

#### BLE Sensors

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/ble/sensors` | List all BLE sensor readings |
| `GET` | `/api/ble/sensors/{label}` | Single sensor details |
| `PUT` | `/api/ble/sensors/{label}/location` | Set sensor display location |

#### Automations

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/automations` | List automations with status |
| `POST` | `/api/automations` | Create a new automation |
| `PUT` | `/api/automations/{index}` | Update an automation |
| `DELETE` | `/api/automations/{index}` | Delete an automation |
| `POST` | `/api/automations/{index}/enabled` | Enable or disable an automation |

#### Media Pipeline

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/media/sources` | List media sources with status (see [Media Pipeline](20-media-pipeline.md)) |
| `GET` | `/api/media/signals` | List available signal names |
| `GET` | `/api/media/stream/{source}` | Raw PCM audio stream (chunked) |
| `POST` | `/api/media/sources/{name}/start` | Manually start a media source |
| `POST` | `/api/media/sources/{name}/stop` | Manually stop a media source |
| `POST` | `/api/media/signals/ingest` | Write signals from external source |

#### Diagnostics & Discovery

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/diagnostics/now_playing` | Effects currently playing |
| `GET` | `/api/diagnostics/history` | Recent effect events (last 50) |
| `GET` | `/api/discovered_bulbs` | Bulbs found via ARP keepalive |
| `GET` | `/api/command/discover` | Discovered LIFX devices (query: `?ip=...`) |
| `POST` | `/api/command/identify` | Pulse any device by IP to locate it |
| `DELETE` | `/api/command/identify/{ip}` | Cancel a running identify pulse |
| `GET` | `/api/command/identify/cancel-all` | Cancel all active identify pulses |

#### Server Management

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/server/rediscover` | Re-resolve groups and reload devices |
| `POST` | `/api/server/power-off-all` | Emergency bulk power-off all devices |

#### Distributed / Fleet

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/fleet` | Distributed fleet status |
| `POST` | `/api/assign` | Issue work assignment to compute node |
| `POST` | `/api/assign/{node}/cancel/{id}` | Cancel work assignment |
| `GET` | `/api/calibrate/time_sync` | Server monotonic time for clock offset estimation |
| `POST` | `/api/calibrate/start/{device}` | Start device calibration |
| `POST` | `/api/calibrate/result/{device}` | Apply measured delay |

#### Dashboards (no auth required)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/dashboard` | Web dashboard |
| `GET` | `/home` | Sensor display dashboard |
| `GET` | `/api/home/lights` | Light status for home display |
| `GET` | `/api/home/photos` | Photos for home display |
| `GET` | `/photos/{filename}` | Serve photo from static/photos/ |

**Examples:**

```bash
TOKEN="your-token"
BASE="http://localhost:8420"

# List devices
curl -H "Authorization: Bearer $TOKEN" $BASE/api/devices

# Play an effect
curl -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"effect":"cylon","params":{"speed":2.0,"hue":120}}' \
     $BASE/api/devices/192.0.2.62/play

# Stop the effect
curl -X POST -H "Authorization: Bearer $TOKEN" \
     $BASE/api/devices/192.0.2.62/stop

# List device groups (used by glowup.py --group)
curl -H "Authorization: Bearer $TOKEN" $BASE/api/groups
```

### Server Readiness

The server binds its HTTP port immediately on startup, then loads
devices from config IPs in the background.  This eliminates the
"connection refused" window that caused 502 errors from reverse
proxies (e.g. Cloudflare Tunnel) during restart.

Clients should query `GET /api/status` on connect:

```json
{"status": "loading", "ready": false, "version": "2.0"}
```

Once device loading completes the response changes to:

```json
{"status": "ready", "ready": true, "version": "2.0"}
```

While `ready` is `false`, other endpoints work normally but return empty
device lists.  The iOS app uses this to show a loading indicator instead
of an empty screen.

### Authentication

Every request must include a valid bearer token.  The token is compared
using `hmac.compare_digest()` for timing-safe validation.  Invalid or
missing tokens receive a `401 Unauthorized` response.

### Server-Sent Events (Live Colors)

The `/api/devices/{ip}/colors/stream` endpoint opens a long-lived HTTP
connection that pushes zone color updates at 4 Hz using the Server-Sent
Events protocol:

```
data: {"zones": [{"h": 0, "s": 65535, "b": 32768, "k": 3500}, ...]}

data: {"zones": [{"h": 100, "s": 65535, "b": 32768, "k": 3500}, ...]}
```

The stream creates a separate read-only device connection to avoid
socket contention with the engine's animation loop (the same pattern
used by monitor mode).

### Phone Override Behavior

When the phone app (or any external client — REST, MQTT, Home Assistant,
Shortcuts) sends a `play` or `stop` command, the server marks the device
as "overridden" so the scheduler skips it.  The iOS app shows an orange
"Schedule paused on this device" banner on the device detail screen
while an override is active.

**Override lifecycle:**

```
  ┌─────────────┐   play / stop    ┌────────────────┐
  │  Scheduled   │ ──────────────► │   Overridden    │
  │  (normal)    │                 │ (scheduler skip)│
  └─────────────┘                 └────────────────┘
        ▲                                │
        │      resume / schedule         │
        │         transition             │
        └────────────────────────────────┘
```

Overrides are cleared in two ways:

- **Manual resume:** Tap "Resume Schedule" in the app (calls
  `POST /api/devices/{ip}/resume`), or publish to the MQTT resume
  topic.  The scheduler picks up on its next poll cycle (every 30 s).
- **Schedule transition:** When the active schedule entry changes
  (e.g., from "evening" to "night"), overrides that were set against
  the outgoing entry are cleared automatically.  Overrides set after
  a server restart or against a different entry are preserved.

**Virtual multizone groups:** When multiple devices are stitched into a
virtual multizone group (e.g., `group:porch` contains 192.0.2.62 and
192.0.2.23), the scheduler checks both the group ID *and* every member
IP for overrides.  This means overriding a single member device via the
app correctly pauses the entire group's schedule — the scheduler will
not overwrite a manually controlled device.

The `GET /api/devices/{ip}/status` and `GET /api/devices` responses
include an `"overridden"` boolean field so the app can display the
override state.

### Installing the Server as a systemd Service

```bash
sudo cp glowup-server.service /etc/systemd/system/
sudo cp server.json /etc/glowup/server.json
sudo systemctl daemon-reload
sudo systemctl enable glowup-server
sudo systemctl start glowup-server
```

If migrating from `scheduler.py`, disable the old service first:

```bash
sudo systemctl stop glowup-scheduler
sudo systemctl disable glowup-scheduler
```

### Installing the Server as a macOS launchd Service

Create `~/Library/LaunchAgents/com.glowup.server.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.glowup.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>server.py</string>
    <string>server.json</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/path/to/lifx</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/glowup-server.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/glowup-server.err</string>
</dict>
</plist>
```

Replace `/path/to/lifx` with your actual repo path and
`/usr/bin/python3` with your Python path (e.g.,
`/opt/homebrew/bin/python3` for Homebrew).

Load and start:

```bash
launchctl load ~/Library/LaunchAgents/com.glowup.server.plist
```

Stop and unload:

```bash
launchctl unload ~/Library/LaunchAgents/com.glowup.server.plist
```

The server will start automatically on login and restart if it
crashes (``KeepAlive`` is true).

### Running Distributed Agents as Services

Worker agents (on Jetsons, Macs, or any Linux machine) can be
made persistent using the same patterns.

**Linux (systemd):**

```bash
sudo cp glowup-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable glowup-agent
sudo systemctl start glowup-agent
```

**macOS (launchd):** Create a plist similar to the server plist
above, replacing `server.py` with the agent command:

```xml
<key>ProgramArguments</key>
<array>
  <string>/path/to/venv/bin/python3</string>
  <string>-m</string>
  <string>distributed.worker_agent</string>
  <string>agent.json</string>
</array>
```

**MIDI light bridge, audio emitter, or N-body visualizer** can also
be made persistent using the same plist/service pattern — just change
the ``ProgramArguments`` to the appropriate command.
