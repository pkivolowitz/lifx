# macOS Remote Control

The `shortcuts/` directory contains a shell-based remote control for
GlowUp.  No app installation required — just a terminal or a
double-clickable `.command` file from Finder.

### Setup

1. Store your auth token (once per machine):

   ```bash
   echo -n "YOUR_AUTH_TOKEN" > ~/.glowup_token
   chmod 600 ~/.glowup_token
   ```

   Replace `YOUR_AUTH_TOKEN` with the `auth_token` value from your
   `server.json` (see [Server Configuration](#server-configuration)).

2. If your server is not at `10.0.0.48`, edit the default in
   `shortcuts/glowup.sh` or set the `GLOWUP_HOST` environment variable.

### The glowup.sh CLI

`shortcuts/glowup.sh` is a single script that wraps the REST API:

```bash
# Play an effect on a group or device
glowup.sh play porch aurora speed=10 brightness=80
glowup.sh play living-room cylon hue=240
glowup.sh play 10.0.0.62 breathe

# Stop the current effect (override stays active)
glowup.sh stop porch

# Resume the schedule (clears override)
glowup.sh resume porch

# Power on / off
glowup.sh on porch
glowup.sh off all

# Query status
glowup.sh status              # all devices
glowup.sh status porch        # one group

# List available effects
glowup.sh list
```

**Targets** can be group names (`porch`, `living-room`, `all`,
`testing`) or individual device IPs (`10.0.0.62`).  Group names
are automatically expanded to `group:NAME` for the API.

Effect parameters are passed as `key=value` pairs after the effect
name.  Numbers are sent as JSON numbers; everything else as strings.

### One-Click .command Files

The `shortcuts/` directory also includes pre-built `.command` files
that can be double-clicked from Finder or dragged to the Dock:

| File | Action |
|------|--------|
| `Porch Aurora.command` | Play aurora on porch |
| `Porch Fireworks.command` | Play fireworks on porch |
| `Porch Flag.command` | Play US flag on porch |
| `Stop Porch.command` | Stop the porch effect |
| `Resume Porch.command` | Resume porch schedule |
| `Porch On.command` | Power on porch |
| `Porch Off.command` | Power off porch |

Each `.command` file is a one-liner that calls `glowup.sh`.  Create
your own by copying any existing one and changing the arguments.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GLOWUP_HOST` | `10.0.0.48` | Server IP or hostname |
| `GLOWUP_PORT` | `8420` | Server port |
| `GLOWUP_TOKEN` | *(reads `~/.glowup_token`)* | Auth token (overrides file) |

### Notes

- **No secrets in the repo.** The auth token is read at runtime from
  `~/.glowup_token` or the `GLOWUP_TOKEN` environment variable.
- **Scheduler conflict:** Like any external client, effects started
  via `glowup.sh play` set a phone override on the device.  Use
  `glowup.sh resume` to hand control back to the scheduler.
- **Apple Shortcuts:** The GlowUp REST API is fully compatible with
  the Shortcuts app on iPhone, iPad, and Mac.  You can build
  shortcuts using either the "Run Shell Script" action (calling
  `glowup.sh`) or the "Get Contents of URL" action (calling the
  REST API directly with POST method and Bearer token header).
  Either approach gives you Siri voice control, NFC tag triggers,
  time-of-day automations, and Home Screen widgets.  The details
  are left as an exercise for the reader.
- **Remote access:** If your server is reachable via Cloudflare
  Tunnel, set `GLOWUP_HOST` to your tunnel hostname.

