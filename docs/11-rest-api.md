# REST API Server

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
with the same schedule format used by `scheduler.py`:

```json
{
    "port": 8420,
    "auth_token": "your-secret-token-here",
    "location": {
        "latitude": 43.07,
        "longitude": -89.40
    },
    "groups": {
        "porch": ["10.0.0.62"]
    },
    "nicknames": {
        "10.0.0.62": "Porch Lights"
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

The `nicknames` section maps device IPs to custom display names shown
in the iPhone app.  Nicknames can also be set from the app itself
(swipe left on a device row) and are persisted back to this file.

Generate a secure token:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

The `groups` section is **required** — it is the server's only source of
device IPs.  The server does not perform broadcast discovery; instead it
queries each configured IP directly at startup.  This is both faster and
more reliable than broadcast discovery, which requires multiple retries
with long timeouts and is defeated by mesh routers that filter broadcast
packets between nodes.

Groups with two or more devices automatically appear as a virtual
multizone device in the API and the iOS app, identified by
`group:<name>` (e.g., `group:porch`).  The individual member devices
also appear for independent control.  The scheduler plays effects on
the virtual device so the animation spans all member devices as a
unified canvas.

The `schedule` section is optional — the server works in API-only mode
without it.

> **Tip:** Because the server relies on IP addresses to reach each
> device, LIFX bulbs should be given **static IP addresses** or
> **DHCP address reservations** in your router.  If a device's IP
> changes (e.g. after a power outage or DHCP lease renewal), the
> server will no longer be able to reach it at the configured address.

### API Endpoints

All endpoints require a bearer token in the `Authorization` header:

```
Authorization: Bearer your-secret-token-here
```

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | Server readiness and version |
| `GET` | `/api/devices` | List all configured devices |
| `GET` | `/api/effects` | List effects with full parameter metadata |
| `GET` | `/api/devices/{ip}/status` | Current effect name, params, FPS |
| `GET` | `/api/devices/{ip}/colors` | Snapshot of zone HSBK values |
| `GET` | `/api/devices/{ip}/colors/stream` | SSE stream of zone colors at 4 Hz |
| `POST` | `/api/devices/{ip}/play` | Start an effect (body: `{"effect":"name","params":{...}}`) |
| `POST` | `/api/devices/{ip}/stop` | Stop current effect (fade to black) |
| `POST` | `/api/devices/{ip}/resume` | Clear phone override, resume schedule |
| `POST` | `/api/devices/{ip}/power` | Power on/off (body: `{"on": true}`) |

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
     $BASE/api/devices/10.0.0.62/play

# Stop the effect
curl -X POST -H "Authorization: Bearer $TOKEN" \
     $BASE/api/devices/10.0.0.62/stop
```

### Server Readiness

The server binds its HTTP port immediately on startup, then loads
devices from config IPs in the background.  This eliminates the
"connection refused" window that caused 502 errors from reverse
proxies (e.g. Cloudflare Tunnel) during restart.

Clients should query `GET /api/status` on connect:

```json
{"status": "loading", "ready": false, "version": "1.8"}
```

Once device loading completes the response changes to:

```json
{"status": "ready", "ready": true, "version": "1.8"}
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
virtual multizone group (e.g., `group:porch` contains 10.0.0.62 and
10.0.0.23), the scheduler checks both the group ID *and* every member
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

