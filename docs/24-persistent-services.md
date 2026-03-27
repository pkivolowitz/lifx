# Persistent Services

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

Any GlowUp component that should run unattended — the server, a
MIDI light bridge, an audio emitter, a distributed agent, the MQTT
broker — needs to survive reboots and restart on failure.  This
document covers how to make any component persistent on Linux and
macOS.

## Linux (systemd)

systemd is the standard service manager on Raspberry Pi OS, Ubuntu,
and Debian.  The pattern is the same for every component: write a
`.service` file, enable it, start it.

### The Pattern

Create `/etc/systemd/system/<name>.service`:

```ini
[Unit]
Description=GlowUp <component>
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/lifx
ExecStart=/usr/bin/python3 <command>
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable <name>
sudo systemctl start <name>
```

### Ready-Made Service Files

The repo includes service files for the most common components:

| File | Component | Command |
|------|-----------|---------|
| `glowup-server.service` | REST API server | `server.py server.json` |
| `glowup-scheduler.service` | Standalone scheduler | `scheduler.py /etc/glowup/schedule.json` |
| `glowup-agent.service` | Distributed worker agent | `distributed/worker_agent.py agent.json` |

Copy and edit for your paths:

```bash
sudo cp deploy/glowup-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable glowup-server
sudo systemctl start glowup-server
```

### Custom Components

Any command you can run in a terminal can be a service.  Examples:

**MIDI light bridge:**

```ini
ExecStart=/usr/bin/python3 -m distributed.midi_light_bridge --ip 192.0.2.23 192.0.2.34
```

**MIDI audio emitter:**

```ini
ExecStart=/home/pi/venv/bin/python3 -m emitters.midi_out --backend fluidsynth --soundfont /home/pi/FluidR3_GM.sf2
```

**N-body visualizer:**

```ini
ExecStart=/usr/bin/python3 -m distributed.nbody_visualizer --particles-per-note 50
```

### Managing Services

```bash
sudo systemctl status <name>          # is it running?
sudo systemctl restart <name>         # restart after config change
sudo systemctl stop <name>            # stop
sudo systemctl disable <name>         # don't start on boot
journalctl -u <name> -f               # live log output
journalctl -u <name> --since "1h ago" # recent logs
```

### When to Restart

Restart a service after:

- Editing its config file (e.g., `server.json`)
- Pulling new code from the repo
- Adding a new effect (effects are loaded at startup)
- Database connection changes

You do **not** need to restart for:

- Playing or stopping effects via the API
- Changing effect parameters at runtime
- Phone override changes

---

## macOS (launchd)

launchd is the macOS equivalent of systemd.  Services are defined
as XML plist files in `~/Library/LaunchAgents/` (per-user) or
`/Library/LaunchDaemons/` (system-wide).

### The Pattern

Create `~/Library/LaunchAgents/com.glowup.<name>.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.glowup.<name></string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/python3</string>
    <string>command</string>
    <string>args</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/path/to/lifx</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/glowup-<name>.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/glowup-<name>.err</string>
</dict>
</plist>
```

Replace `/path/to/python3` with your actual Python (e.g.,
`/opt/homebrew/bin/python3` for Homebrew, or
`/Users/you/venv/bin/python3` for a venv).

Then:

```bash
launchctl load ~/Library/LaunchAgents/com.glowup.<name>.plist
```

### Examples

**Server:**

```xml
<key>ProgramArguments</key>
<array>
  <string>/opt/homebrew/bin/python3</string>
  <string>server.py</string>
  <string>server.json</string>
</array>
```

**MIDI light bridge:**

```xml
<key>ProgramArguments</key>
<array>
  <string>/Users/you/venv/bin/python3</string>
  <string>-m</string>
  <string>distributed.midi_light_bridge</string>
  <string>--ip</string>
  <string>192.0.2.23</string>
  <string>192.0.2.34</string>
</array>
```

**MIDI audio emitter:**

```xml
<key>ProgramArguments</key>
<array>
  <string>/Users/you/venv/bin/python3</string>
  <string>-m</string>
  <string>emitters.midi_out</string>
  <string>--backend</string>
  <string>fluidsynth</string>
  <string>--soundfont</string>
  <string>/Users/you/Downloads/FluidR3_GM.sf2</string>
</array>
```

### Managing Services

```bash
# Load (start and enable on login)
launchctl load ~/Library/LaunchAgents/com.glowup.<name>.plist

# Unload (stop and disable)
launchctl unload ~/Library/LaunchAgents/com.glowup.<name>.plist

# Force restart
launchctl kickstart -k gui/$(id -u)/com.glowup.<name>

# View logs
tail -f /tmp/glowup-<name>.log
```

### KeepAlive vs RunAtLoad

- `RunAtLoad` — start when the plist is loaded (login or manual load).
- `KeepAlive` — restart automatically if the process exits.

Both should be `true` for GlowUp services.

---

## MQTT Broker (Mosquitto)

The MQTT broker is a system service, not a GlowUp component.  It's
managed by its own service manager.

**Linux:**

```bash
sudo apt install mosquitto
sudo systemctl enable mosquitto
sudo systemctl start mosquitto
```

Mosquitto auto-starts on boot after installation on most distros.

**macOS:**

```bash
brew install mosquitto
brew services start mosquitto
```

---

## Multiple Services

A typical deployment might run several services simultaneously:

| Service | Machine | Purpose |
|---------|---------|---------|
| `glowup-server` | Pi | REST API, scheduler, diagnostics |
| `mosquitto` | Pi | MQTT broker |
| `midi-light-bridge` | Any Mac/Pi | MIDI → LIFX lights |
| `midi-emitter` | Any Mac | MIDI → speakers |
| `worker-agent` | Jetson/GPU | Compute (FFT, N-body) |

Each is independent.  Start and stop them individually.  The MQTT
bus connects them — if one goes down, the others keep running.

## Do Not Run Simultaneously

- `server.py` and `scheduler.py` — they conflict over device control.
  Use `server.py` (it includes the scheduler).
