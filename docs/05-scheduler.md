# Scheduler (Daemon)

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

The scheduler (`scheduler.py`) runs effects on a timed schedule, with
sunrise/sunset awareness. It manages multiple independent device groups,
each running its own effect on its own schedule. Designed to run as a
systemd service on a Raspberry Pi (or any Linux box).

```bash
python3 scheduler.py /etc/glowup/schedule.json
```

The scheduler polls every 30 seconds to determine which schedule entry
should be active for each group. When the active entry changes, it
gracefully stops the old effect (SIGTERM → fade to black) and starts
the new one. Crashed subprocesses are automatically restarted.

### Configuration File

The config file is JSON with three sections: `location`, `groups`, and
`schedule`.

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
python3 scheduler.py --dry-run schedule.json.example
```

This prints solar event times for your location, all device groups, and
the resolved schedule with concrete times. Active entries are flagged.
Use this to verify your config before deploying.

### Installing as a systemd Service

1. **Clone the repository** to the target machine:

```bash
git clone https://github.com/pkivolowitz/lifx.git /home/a/lifx
```

2. **Create the config file** at `/etc/glowup/schedule.json`:

```bash
sudo mkdir -p /etc/glowup
sudo cp /home/a/lifx/schedule.json.example /etc/glowup/schedule.json
sudo nano /etc/glowup/schedule.json   # edit for your location, devices, schedule
```

3. **Test with dry run** to verify times resolve correctly:

```bash
python3 /home/a/lifx/scheduler.py --dry-run /etc/glowup/schedule.json
```

4. **Test live** before installing the service:

```bash
python3 /home/a/lifx/scheduler.py /etc/glowup/schedule.json
# Watch the logs, Ctrl+C to stop
```

5. **Install the systemd service**:

```bash
sudo cp /home/a/lifx/deploy/glowup-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable glowup-scheduler
sudo systemctl start glowup-scheduler
```

If your install path is not `/home/a/lifx`, edit the service file first:

```bash
sudo nano /etc/systemd/system/glowup-scheduler.service
# Update ExecStart and WorkingDirectory to match your paths
```

### Controlling the Service

```bash
# Check status and recent logs
sudo systemctl status glowup-scheduler

# View full logs
sudo journalctl -u glowup-scheduler -f          # follow live
sudo journalctl -u glowup-scheduler --since today

# Stop / start / restart
sudo systemctl stop glowup-scheduler
sudo systemctl start glowup-scheduler
sudo systemctl restart glowup-scheduler

# Disable (won't start on boot)
sudo systemctl disable glowup-scheduler

# After editing the config file, restart to pick up changes
sudo systemctl restart glowup-scheduler
```

The scheduler logs to the systemd journal, including solar event times
(recalculated daily), schedule transitions, subprocess starts/stops,
and any errors.
