# Troubleshooting

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

## Device State Issues

LIFX devices maintain internal state that persists across sessions.
Occasionally — particularly after switching between different control
sources (this engine, the LIFX app, HomeKit) — a device may display
unexpected colors or brief visual artifacts when a new effect starts.

If you notice residual colors from a previous session, simply
**power-cycle the device** (off, then on) using the physical switch or
the LIFX app.  If the device is in a location where power-cycling is
inconvenient, opening the official LIFX app and briefly controlling the
device can help clear its internal state.

This is a characteristic of the LIFX firmware's internal state
management and may not be specific to GlowUp.

## TP-Link Deco Mesh Routers — A Special Circle of Hell

If you are using TP-Link Deco mesh routers, read this entire section
before filing a bug report, because every problem you are about to
have is the Deco's fault.

**UDP broadcast is dead.**  The Deco silently eats UDP broadcast
packets between mesh nodes.  LIFX discovery relies on UDP broadcast.
This means `glowup.py discover` will find **zero devices** from any
wireless client.  It only works from a machine on the same wired
segment as the bulbs (e.g., a Pi connected via Ethernet).  This is
not a GlowUp bug.  This is the Deco deciding it knows better than
you what packets your devices should receive.

**The IoT network is a trap.**  The Deco offers an "IoT network"
feature that creates a separate SSID for smart devices.  Enabling
this — even briefly — can force every WiFi device on your network to
disconnect and re-associate.  LIFX bulbs, irrigation controllers,
smart plugs, cameras, and anything else with stored WiFi credentials
may drop off the network.  Disabling the IoT network does not
automatically restore them.  Bulbs need a physical power-cycle
(off at the switch, wait, on again).  Devices you cannot physically
reach (in-wall controllers, outdoor sensors) may remain offline
indefinitely until someone power-cycles them.

**The client list is a lie.**  The Deco's app shows a "client list"
of connected devices.  This list updates on its own schedule, which
appears to be "whenever it feels like it."  A device can be fully
connected, responding to pings, running LIFX protocol, and still not
appear in the Deco client list for minutes or hours.  You cannot
force a refresh.  Do not trust it as a source of truth — use
`lanscan.py` or `ping` from the Pi instead.

**DHCP roulette.**  After a network disruption (IoT network toggle,
firmware update, power outage), the Deco may reassign IP addresses
to devices that reconnect.  Your `server.json` will have stale IPs.
Run `lanscan.py` from the Pi to find the new addresses and update
your config.

**What actually works:**  Put the Pi on Ethernet.  Use direct IP
queries (`discover --ip <addr>`) or `lanscan.py` for device
discovery.  Never enable the IoT network unless you enjoy spending
your morning power-cycling every smart device in your house.  Use
DHCP reservations in the Deco app to pin device IPs so they survive
network disruptions.

## Discovery Failures

### Server Routing: The Solution

**If you have a hardwired Pi server on Ethernet, the problem is solved.**
`glowup discover` automatically routes all queries through the server,
bypassing Deco UDP blocking entirely.  You can run discovery from any
wireless machine and it will work.

```bash
glowup discover              # Routes via server if reachable
glowup discover --ip 10.0.0.28  # Also routes via server
```

The server has direct Ethernet access to all bulbs, so it can query
them even when your wireless client can't.  See
[Server Routing & Safety](../docs/25-server-routing-safety.md) for
details on how this works.

### Fallback Steps (No Server)

If you don't have a server or it's unreachable, try these alternatives:

1. **Direct IP:** `python3 glowup.py discover --ip 192.0.2.62`
   Use the LIFX app to find device IPs, then query each one by IP.

2. **Run lanscan from the Pi:** `python3 lanscan.py` from a machine
   on Ethernet to scan the entire subnet at ARP level.

3. **Check your subnet:** Verify that your machine and the LIFX device
   are on the same subnet (same VLAN, same SSID).  Deco multi-node
   setups often isolate wireless clients from bulbs on other nodes.

4. **Wired connection:** Try discovery from a machine connected via
   Ethernet.  Deco doesn't block UDP on wired segments.

5. **Phone app:** The LIFX app always shows device IPs.  Use those
   with `--ip` for direct queries.

Discovery is flaky by nature — a device that doesn't respond on the
first try often responds on the second.  The server retries
automatically.

### Bottom Line

**Buy a Pi and put it on Ethernet.** Server routing makes discovery
work seamlessly from anywhere on your network, even through multiple
Deco nodes.  It's the single best investment for a robust GlowUp
setup on a mesh network.

## When to Restart the Server

Restart `glowup-server` after:

- **Config file changes** — edits to `server.json` (new devices,
  schedule changes, MQTT settings).
- **Code updates** — pulling new code from the repo.
- **Adding new effects** — new `.py` files in `effects/` are loaded
  at startup (auto-discovered, but only at import time).
- **Database connection changes** — new DSN, PostgreSQL restart, etc.

You do **not** need to restart for:

- Playing/stopping effects via the API or iOS app.
- Changing effect parameters at runtime.
- Phone override changes.

On the Pi:

```bash
sudo systemctl restart glowup-server
```

On macOS (if running as a launchd service):

```bash
launchctl kickstart -k gui/$(id -u)/com.glowup.server
```

## PostgreSQL Setup

GlowUp's diagnostics subsystem logs effect events to PostgreSQL.
This is entirely optional — the server works fine without it.

**What you need:**

- A PostgreSQL instance accessible from the server (a NAS jail, a
  Docker container, a cloud database — anything that speaks SQL).
- The `psycopg2` Python package on the server machine.
- The `GLOWUP_DIAG_DSN` environment variable (or the default DSN).

**Schema setup:**

Apply the schema files in the `sql/` directory:

```bash
psql -h <db-host> -U glowup -d glowup -f sql/midi_events.sql
```

The diagnostics tables (`effect_history`, `device_events`,
`crash_reports`, `signal_snapshots`) are created automatically by
the server on first connection.  The `midi_events` table requires
the manual schema application above.

**Default connection string:**

```
postgresql://glowup:changeme@localhost:5432/glowup
```

Override with the `GLOWUP_DIAG_DSN` environment variable:

```bash
export GLOWUP_DIAG_DSN="postgresql://user:pass@host:5432/dbname"
```

**Installing psycopg2:**

```bash
pip install psycopg2-binary
```

**Verifying the connection:**

```bash
python3 -c "
import psycopg2
conn = psycopg2.connect('postgresql://glowup:changeme@localhost:5432/glowup')
cur = conn.cursor()
cur.execute('SELECT version()')
print(cur.fetchone()[0])
conn.close()
"
```

**If diagnostics fails:** The server logs a warning and continues
without logging.  No effect playback is affected.  Check
`journalctl -u glowup-server` for connection errors.

## Dashboard

The server includes a web dashboard at `/dashboard` that shows:

- **Stats bar** — device count, playing count (physical + groups),
  ARP-discovered count, active schedule entries.
- **Now Playing** — live effects with the source that started them
  (e.g. "Conway", "iPhone", "scheduler").
- **Schedule** — all schedule entries with active/waiting/off badges
  and resolved start/stop times.
- **Devices** — all configured physical devices with playing/idle
  status and current effect.
- **Groups** — virtual multizone groups with member count and
  playing status.
- **Discovered Devices** — ARP keepalive scan results with labels,
  product type, and zone count.  Collapsed by default (click to
  expand).

**Access:** `http://<server-ip>:8420/dashboard`

The dashboard uses its own login page (bearer token entered in the
browser, stored in localStorage).  It auto-refreshes every 5 seconds
with rate-limit backoff to 30 seconds if the server returns 429.

## NTP / Clock Drift

If the server's system clock drifts (NTP disabled or unreachable),
PostgreSQL connections may fail silently — TLS certificate validation
and authentication protocols depend on accurate timestamps.

Verify NTP is active:

```bash
timedatectl status | grep -i ntp
```

If NTP is not synchronized:

```bash
sudo timedatectl set-ntp true
```

The test suite includes an NTP check:

```bash
python3 -m pytest tests/test_environment.py -v
```

## Common Errors

**"No devices found"** — see [Discovery Failures](#discovery-failures)
above.

**"psycopg2 not installed"** — install with `pip install psycopg2-binary`.
Diagnostics is optional; the server runs without it.

**"MQTT connection refused"** — the broker isn't running or isn't
reachable.  On the Pi: `sudo systemctl status mosquitto`.

**"SoundFont not found"** — the `--soundfont` path is wrong.  Use
`file ~/Downloads/*.sf2` to verify the file exists and is a valid
RIFF/SoundFont.

**Lights don't respond to MIDI** — the light bridge doesn't
auto-power devices.  Run with the latest code (which adds
`set_power(True)` during discovery), or power the device on manually
via the LIFX app first.
