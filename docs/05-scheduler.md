# Schedule Configuration

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

The scheduler runs **in-process under `glowup-server`** — there is no
separate scheduler binary or service.  This document covers the
schedule configuration JSON shape (the `schedule:` block inside
`server.json`); for the server itself see
[11-rest-api.md](11-rest-api.md), and for the live REST endpoints
that read and mutate schedule entries
(`GET`/`POST`/`PUT`/`DELETE` `/api/schedule`) see the `Scheduling`
section of that document.

The in-process scheduler polls every 30 seconds to determine which
schedule entry should be active for each group.  When the active
entry changes, it gracefully stops the old effect (SIGTERM → fade
to black) and starts the new one.  Crashed effect threads are
automatically restarted.

> **Historical note:** A standalone `scheduler.py` + companion
> `glowup-scheduler.service` shipped through 2026-04.  Both were
> retired during the Phase 2b installer cleanup once the
> `scheduling/` package was integrated into `glowup-server`.
> Existing fleet hosts that still have `/etc/systemd/system/
> glowup-scheduler.service` from a pre-Phase-2b install can
> safely `systemctl disable --now glowup-scheduler` and remove
> the unit file; the next `install.sh` run will not put it back.

### Where the schedule lives

The schedule is part of `server.json` (typically
`/etc/glowup/server.json` once installed) and is loaded when
`glowup-server` starts.  Three top-level sections drive scheduling:
`location`, `groups`, and `schedule`.  Live edits go through the
REST endpoints — `POST /api/schedule` to add, `PUT
/api/schedule/{index}` to modify, `DELETE /api/schedule/{index}`
to remove — see [11-rest-api.md](11-rest-api.md) for the full
endpoint reference.

The example below shows the relevant slice of `server.json`; in a
real config these keys sit alongside `auth_token`, `adapters`,
`emitters`, etc.

```json
{
    "location": {
        "latitude": 43.07,
        "longitude": -89.40,
        "_comment": "Your coordinates — needed for sunrise/sunset"
    },
    "groups": {
        "porch": ["porch_string_lights"],
        "living-room": ["192.0.2.10", "192.0.2.12"]
    },
    "schedule": [
        {
            "name": "porch evening aurora",
            "group": "porch",
            "start": "sunset-30m",
            "stop": "23:00",
            "effect": "aurora",
            "params": {
                "speed": 10.0,
                "brightness": 60
            }
        },
        {
            "name": "porch weekday morning",
            "days": "MTWRF",
            "group": "porch",
            "start": "sunrise-30m",
            "stop": "sunrise+30m",
            "effect": "flag",
            "params": {
                "country": "us",
                "brightness": 70
            }
        },
        {
            "name": "porch overnight clock",
            "group": "porch",
            "start": "23:00",
            "stop": "sunrise-30m",
            "effect": "binclock",
            "params": {
                "brightness": 40
            }
        }
    ]
}
```

**`location`** — Your latitude and longitude in decimal degrees. Required
for resolving symbolic times (sunrise, sunset, etc.).

**`groups`** — Named collections of device IPs or hostnames. Each group
is managed independently — multiple groups can run different effects at
the same time. Use hostnames if you have DNS/mDNS set up, or raw IPs.

Groups with two or more devices are automatically combined into a
**virtual multizone device** — a single unified zone canvas spanning
all member devices.  Effects render across the combined zone count
as if all the lights were one long strip.

> **IP order matters:** The order of IPs in the group array determines
> the left-to-right zone layout on the virtual canvas.  The first IP's
> zones come first (leftmost), the second IP's zones follow, and so on.
> If an animation runs in the wrong direction, swap the IPs in the
> array and restart the server.

**`schedule`** — Ordered list of schedule entries. Each entry specifies:

| Field    | Required | Description                                      |
|----------|----------|--------------------------------------------------|
| `name`   | yes      | Human-readable label (used in logs)              |
| `group`  | yes      | Which device group to target                     |
| `start`  | yes      | When to start (fixed time or symbolic)           |
| `stop`   | yes      | When to stop (fixed time or symbolic)            |
| `effect` | yes      | Effect name (e.g., `"aurora"`, `"cylon"`)        |
| `params` | no       | Effect parameter overrides (e.g., `{"speed": 5}`) |
| `days`   | no       | Day-of-week filter (e.g., `"MTWRF"` for weekdays) |

**Day-of-week filtering** — The `days` field restricts an entry to specific
days using the academic letter convention:

| Letter | Day       |
|--------|-----------|
| M      | Monday    |
| T      | Tuesday   |
| W      | Wednesday |
| R      | Thursday  |
| F      | Friday    |
| S      | Saturday  |
| U      | Sunday    |

Examples: `"MTWRF"` = weekdays, `"SU"` = weekends, `"MWF"` = Mon/Wed/Fri.
Omitting the field (or setting it to `""`) means every day. Letters can
appear in any order but must not repeat.

When multiple entries for the same group overlap, the first match in
config file order wins (put higher-priority entries first).

Overnight entries work automatically — if `stop` is earlier than `start`,
the scheduler adds a day to the stop time (e.g., `"start": "23:00",
"stop": "06:00"` runs from 11 PM to 6 AM the next morning).

### Symbolic Times

Start and stop times can be fixed (`"14:30"`) or symbolic:

| Symbol     | Meaning                                          |
|------------|--------------------------------------------------|
| `sunrise`  | Sun crosses the horizon (upper limb visible)     |
| `sunset`   | Sun crosses the horizon (upper limb disappears)  |
| `dawn`     | Civil twilight begins (sun 6° below horizon)     |
| `dusk`     | Civil twilight ends (sun 6° below horizon)       |
| `noon`     | Solar noon (sun at highest point)                |
| `midnight` | 00:00 local time                                 |

Add offsets with `+` or `-`:

```
sunset-30m       30 minutes before sunset
sunrise+1h       1 hour after sunrise
dawn+1h30m       1 hour 30 minutes after dawn
noon-2h          2 hours before solar noon
```

Solar calculations use the NOAA algorithm (built-in, no dependencies)
and are recalculated daily.

### Dry Run

Preview the resolved schedule without running any effects:

```bash
python3 server.py --dry-run server.json
```

This prints solar event times for your location, all device groups,
and the resolved schedule with concrete times.  Active entries are
flagged.  Use this to verify your config before starting
`glowup-server` against it.

### Where the running scheduler logs

The in-process scheduler logs to `glowup-server`'s systemd journal
alongside the rest of the server's output:

```bash
sudo journalctl -u glowup-server -f                          # follow live
sudo journalctl -u glowup-server --since today | grep -i sched
```

After editing the schedule entries via REST or by hand-editing
`server.json`, the in-process scheduler picks up changes
automatically — no `systemctl restart` needed for REST-driven
edits, and a server restart for hand edits:

```bash
sudo systemctl restart glowup-server
```

For the full `glowup-server` install/control reference (which
also covers the scheduler since the two share a process now)
see [24-persistent-services.md](24-persistent-services.md).
