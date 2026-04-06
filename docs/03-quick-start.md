# Quick Start

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

GlowUp is **server-preferred**: when a coordinator is reachable, it
handles device resolution, effect execution, packet delivery, and the
shared control plane.  This gives you label-based device addressing,
ARP-based discovery, keepalive, operator/runtime services, and
scheduling.  If the server is unreachable, the CLI falls back to
direct UDP for standalone device control.

If you want label addressing, groups, scheduling, operators, remote
control, and a foundation for distributed sensors or voice — install
the server (see [REST API Server](11-rest-api.md)).  Without the
server, `--ip` still works for direct device control.

### With a Server (recommended)

```bash
# 1. Find your LIFX devices (routes via server automatically)
python3 glowup.py discover

# 2. See what effects are available
python3 glowup.py effects

# 3. Run an effect by device label (server resolves label → IP)
python3 glowup.py play cylon --device "PORCH STRING LIGHTS"

# 4. Or animate a group of bulbs as a virtual multizone surface
python3 glowup.py play aurora --group porch

# 5. Preview an effect in the simulator (fetches real geometry from server)
python3 glowup.py play cylon --device "PORCH STRING LIGHTS" --sim-only

# 6. Press Ctrl+C to stop (fades to black gracefully)
```

From there, you can add sensors, operators, MQTT, distributed workers,
or voice components without changing the core model.

### Standalone (no server)

```bash
# 1. Find your LIFX devices (direct UDP broadcast)
python3 glowup.py discover

# 2. Run an effect by IP address
python3 glowup.py play cylon --ip <device-ip>

# 3. Or animate a group from a local config file
python3 glowup.py play cylon --config schedule.json --group office

# 4. Press Ctrl+C to stop (fades to black gracefully)
```
