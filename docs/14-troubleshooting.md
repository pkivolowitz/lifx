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

## Discovery Failures

Discovery uses UDP broadcast, which some mesh routers (e.g., TP-Link
Deco) block between nodes.  If `discover` finds nothing:

1. Try direct IP: `python3 glowup.py discover --ip 192.0.2.62`
2. Check that your machine and the LIFX device are on the same
   subnet (same VLAN, same SSID).
3. Try from a wired connection if wireless discovery fails.
4. The LIFX phone app always shows device IPs — use those with `--ip`.

Discovery is flaky by nature — a device that doesn't respond on the
first try often responds on the second.  The server retries
automatically.

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

- **Device inventory** — all configured devices and their status.
- **Now playing** — which effects are currently running on which
  devices.
- **Recent history** — the last 50 effect events (start/stop times,
  parameters, stop reason).

**Requires:** PostgreSQL (diagnostics subsystem) — without it, the
dashboard shows empty tables.

**Access:** `http://<server-ip>:8420/dashboard`

The dashboard uses its own login page (bearer token entered in the
browser, stored in localStorage).  It auto-refreshes every 5 seconds.

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
