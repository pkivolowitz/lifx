# GlowUp — User Manual

Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
Licensed under the MIT License. See [LICENSE](../LICENSE) for details.

This project utilizes AI assistance (Claude 4.6) for boilerplate and logic
expansion. All final architectural decisions, algorithmic validation, and
code integration are performed by Perry Kivolowitz, the sole Human Author.

---

## What GlowUp Basic Does

GlowUp Basic (hereafter GlowUp) drives LIFX lights. It runs animated effects (a Larson scanner,
an aurora, fireworks, a slow breathe), it groups bulbs together so an
effect spans a whole room as one virtual strip, and it gives every
bulb a human-readable name so you never type IP addresses.

GlowUp ships in two flavors. You pick one. You can move from the
first to the second later without losing your work.

---

## Which Mode

**Standalone** runs on the machine in front of you — your Mac, your laptop, your
Windows desktop. You type a command, the lights respond. When you exit the
program or close the terminal, GlowUp stops. Nothing runs in the background.
There is no server, no service, no `sudo`, no daemon. Your bulb names and groups
are stored in a small folder under your home directory. Standalone is the right
choice if you want to play with effects, drive a few bulbs from the keyboard, or
get a feel for GlowUp before deciding whether you want more.

**Server** runs on an always-on Linux box — a Raspberry Pi, a small
NUC, an old desktop. The server keeps your bulb registry, your
groups, your schedules, and a web dashboard alive even when your
laptop is asleep or off. You gain sunrise/sunset schedules, a dashboard
in your browser, and the ability to drive lights from any device on
your network. You give up portability — the server wants to live
on Linux with `systemd`, owns config under `/etc/glowup`, and asks
for `sudo` during install. Server is the right choice if you want
your lights to do something at sunset whether you're home or not.

You don't have to decide forever. The bulb names and groups you
build in standalone mode upgrade cleanly into a server install —
just copy the files. Start standalone, switch when you're ready.

---

## Standalone

### What You Need

A computer running macOS, Linux, or Windows with Python 3.11 or
later. Any LIFX bulb on the same Wi-Fi network. That's it.

The optional simulator (a preview window that shows what an effect
looks like without sending packets to real lights) needs `tkinter`,
which ships with macOS and Windows Python and is one apt-install on
Linux (`sudo apt install python3-tk`).

### Installing

Clone the repository and run the installer:

```bash
git clone https://github.com/pkivolowitz/glowup.git
cd glowup
./install.sh
```

The installer creates a Python virtual environment at
`~/.glowup/venv/` (per-user, no `sudo` for standalone), installs the
small set of packages GlowUp needs, and drops a small launcher
script at `~/.local/bin/glowup` that knows how to find the venv.
The installer asks permission before adding `~/.local/bin` to your
shell's PATH — if you say yes, it backs up your shell rc file
(`.zshrc`, `.bashrc`, etc.) and appends one line. If you say no, you
can run GlowUp by its full path and add it to PATH yourself later.

When the installer finishes, open a new terminal (or `source` your
shell rc) and `glowup discover` should work from anywhere.

The clone itself is just source code — the venv lives outside it,
so you can `git pull` and re-run `./install.sh` to upgrade without
rebuilding the venv from scratch. Running `./install.sh` a second
time is the upgrade path: it re-syncs the venv against the new
`requirements.txt`, leaves your data in `~/.glowup/` alone, and
reports what changed.

On macOS, that's the whole story.

On Linux, `./install.sh` defaults to the server flavor (see below)
because the typical Linux box running GlowUp is a Pi or a small
server. If you're on a Linux laptop and want standalone, the
installer asks once at the top of the run — answer "standalone" or
press `s`. If you're scripting the install (Ansible, cloud-init,
etc.) and need to skip the prompt, pass `--standalone` or
`--server` on the command line.

On Windows there is a small PowerShell installer:

```powershell
git clone https://github.com/pkivolowitz/glowup.git
cd glowup
.\install.ps1
```

`install.ps1` does the same thing the macOS/Linux standalone
installer does — creates `%USERPROFILE%\.glowup\venv\`, installs
GlowUp's packages into it, and drops a `glowup.cmd` shim into
`%USERPROFILE%\bin\` (asking permission before adding that
directory to your user PATH via the registry). No Administrator
rights, no Windows service, no files outside your user profile.

### Finding Your Lights

Run discover:

```bash
glowup discover
```

Discover broadcasts on your local network and listens for replies. It
also walks the system ARP cache and runs a short ARP sweep to catch
bulbs that ignored the broadcast — LIFX bulbs frequently
don't answer broadcasts, especially on mesh routers, so the
sweep is there to fill in the gaps.

A typical run prints a table:

```
LABEL                    PRODUCT       IP            MAC                ZONES
A19 KITCHEN              A19           192.168.1.41  d0:73:d5:01:23:ab     1
PORCH STRING LIGHTS      String 36     192.168.1.42  d0:73:d5:04:56:cd    36
DESK NEON                Neon          192.168.1.43  d0:73:d5:07:89:ef    36
```

If discover finishes and prints a line like *"some lights may have
been missed — check your router's client list for unknown
`50:C7:BF` or `D0:73:D5` MAC addresses,"* take it seriously. LIFX
bulbs sometimes hide from discovery and the only way to find them is
the router's admin page. Note their IPs and pass them with `--ip`
on later commands.

### Naming Your Lights

After discover gives you IPs, you walk through the house and tell
GlowUp which bulb is which. Pick an IP, pulse it, watch for the
breathing bulb, give it a name:

```bash
glowup identify --ip 192.168.1.41
```

The bulb breathes warm white until you press Ctrl+C. Once you've
spotted it, name it:

```bash
glowup name --ip 192.168.1.41 "Kitchen Bulb"
```

The IP, MAC, and label are stored in `~/.glowup/devices.json`.
The schema for that file (and for `groups.json` once you start
making groups) is documented in `~/.glowup/README.md`, dropped
there by the installer alongside the JSON files. The runtime
preserves keys starting with `_` on read but never writes new
ones, so you can drop hand-written notes into the JSON files
and the dashboard or CLI won't strip them.

From now on you can address that bulb by name instead of by IP:

```bash
glowup play breathe --device "Kitchen Bulb"
```

Repeat for each bulb. The file accumulates as you go.

### Grouping Lights

A group is a named set of bulbs that an effect can animate together
as if they were one long strip:

```bash
glowup group add bedroom "Kitchen Bulb" "Hallway Bulb"
glowup group list
glowup group show bedroom
glowup group rm bedroom
```

Group definitions live in `~/.glowup/groups.json`. Order matters —
the first bulb in the group is the leftmost zone of the virtual
strip, the next bulb is to its right, and so on. If an effect
animates in the wrong direction, change the order.

A group of one bulb is fine. A group of one multi-zone string light
is also fine — the effect will animate across the string's zones.

### Running Effects

To list every effect GlowUp knows:

```bash
glowup effects
```

To run one:

```bash
glowup play cylon --device "Kitchen Bulb"
glowup play aurora --group bedroom
glowup play breathe --ip 192.168.1.42 --speed 8 --hue 240
```

Each effect has its own parameters. The default behavior is sane;
if you want to tune, ask the effect what it accepts:

```bash
glowup play cylon --help
```

That prints every parameter, its default, and its valid range.

Press Ctrl+C to stop. The lights fade to black over half a second.

### Previewing Without Bulbs

If you want to see what an effect looks like before pointing it at
real lights — or you don't have lights handy — open the simulator:

```bash
glowup play aurora --sim-only --zones 36
```

A small window opens showing the rendered animation. No packets are
sent to any device.

### When Things Go Wrong

**Discover finds nothing.** You're probably on a mesh router that
eats UDP broadcasts. TP-Link Deco does this, and so do some Eero
configurations. Plug the bulbs into a flat unsegmented network if you
can. Otherwise, look up bulb IPs in your router's client list (LIFX
MAC prefixes start with `50:C7:BF` or `D0:73:D5`) and use `--ip`.

**A bulb shows up but won't respond to play.** Power-cycle it from
the wall switch. LIFX bulbs occasionally get into a state where they
ignore commands until they're rebooted. Once they come back up,
they'll work normally.

**Discover prints stale IPs.** Your router reassigned addresses
after a reboot. Re-run discover and re-name any bulbs whose IP
changed. The MAC stays the same, so the names stick.

To stop this from happening, set a static DHCP reservation in your
router for each bulb. A bulb's MAC address never changes, so most
home routers can be told to hand out the same IP every time that MAC
asks for one. Once reserved, the bulb's IP is permanent across
reboots and network outages — and you never have to re-run discover
to chase a moved address.

**Effects look fine in the simulator but wrong on the lights.** Check
the bulb count and zone count in the discover table. If a 36-zone
string light is showing up as a 1-zone bulb, the device is in a weird
firmware state — power-cycle it.

---

## Server

### What You Gain Over Standalone

A web dashboard you can pull up from any device on your network. A
scheduler that turns on the porch lights at sunset, runs a flag
animation in the morning, fades to a soft glow at bedtime. The
dashboard manages your bulb registry and groups for you — point,
click, name, group — instead of editing JSON. Effects keep
running even when your laptop is closed and on a plane.

### What It Costs

You need an always-on Linux box. A Raspberry Pi 4 is plenty; a
retired desktop with Ubuntu works just as well. Debian and
Ubuntu derivatives are supported in this release; other
distributions (Fedora, Arch, etc.) may work but aren't tested.

The installer asks for `sudo` because it writes `systemd` unit
files, creates `/etc/glowup/` for read-only config, creates
`/var/lib/glowup/` for the dashboard's writable data, and creates
a dedicated `glowup` system user that owns the service. The
server runs as that `glowup` user — you won't see it in your
terminal, you'll see it in `systemctl status glowup-server`. If
you've never used `systemctl`, the install will walk you through
the few commands you need.

There is no Windows or macOS server flavor. The server is Linux only.

### Installing

On the Linux box, clone and run the installer:

```bash
git clone https://github.com/pkivolowitz/glowup.git
cd glowup
./install.sh
```

The installer creates a virtual environment, writes read-only
site config to `/etc/glowup/site.json` and `/etc/glowup/server.json`,
seeds writable state files at `/var/lib/glowup/devices.json` and
`/var/lib/glowup/groups.json` (both empty `{}` to start), drops a
matching `~/.glowup/README.md`-style schema doc into
`/var/lib/glowup/`, drops a `systemd` unit, and starts the server.
When it finishes, it prints a URL — point your browser at it.

Re-running `./install.sh` after a `git pull` is the upgrade path.
It re-syncs the venv against the new `requirements.txt`, re-renders
the systemd unit, and leaves `/etc/glowup/server.json` and
everything in `/var/lib/glowup/` alone unless the schema has
changed.

If you used GlowUp standalone first, copy your bulb registry and
groups across before the server starts. The server reads them from
`/var/lib/glowup/`, not `/etc/glowup/`, because they're data the
service writes to (schedules edited in the dashboard, new bulbs
named via the CLI):

```bash
sudo install -o glowup -g glowup -m 0640 \
    ~/.glowup/devices.json /var/lib/glowup/devices.json
sudo install -o glowup -g glowup -m 0640 \
    ~/.glowup/groups.json  /var/lib/glowup/groups.json
sudo systemctl restart glowup-server
```

`install` (rather than `cp`) sets ownership and permissions in one
shot so the `glowup` service user can read and write the migrated
files. The server reads the same file shape standalone wrote, fills
in the extra fields it cares about with defaults, and your names
and groups appear in the dashboard.

### Using The Dashboard

The dashboard lives at the URL the installer printed (typically
`http://<your-server>:8420/`). From there you can:

- See every bulb the server knows about, with current status.
- Run identify on a bulb (the bulb breathes; you walk over and look
  at it).
- Rename bulbs.
- Create, edit, and delete groups.
- Browse the effect catalog and launch effects against bulbs or
  groups.
- View the schedule, edit entries, add new ones.

The dashboard covers the normal registry, group, effect, and
schedule workflows. The CLI remains available for scripting and
direct control.

### Telling The Server Where You Are

If you want sunrise and sunset to mean anything, the server has to
know where it is. The installer asks for your latitude and longitude
during setup; you can also edit them later in
`/etc/glowup/site.json`. Decimal degrees, four or five places of
precision is plenty. Without them, symbolic times like `sunset-30m`
have nothing to compute against.

### Schedules

A schedule entry says *"between time X and time Y, run effect E on
group G with parameters P."* Times can be wall-clock (`07:00`,
`23:30`) or symbolic (`sunset-30m`, `sunrise+15m`). The server uses
your latitude and longitude — entered during install — to compute
sunrise and sunset for the day.

Add an entry from the dashboard, or edit
`/var/lib/glowup/schedules.json` directly (the dashboard writes
back to that file, so hand-edits and dashboard edits live in the
same place). A typical entry looks like:

```json
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
}
```

Multiple groups can run different schedules at the same time. The
server checks every thirty seconds whether the active entry should
change; when it does, the old effect fades out and the new one
starts.

If you want to override a schedule manually — *"forget the schedule,
turn the porch off right now"* — use the dashboard's override panel
or run a `play` command from the CLI. The override holds until you
release it.

### When Things Go Wrong

**The dashboard URL doesn't load.** Check the server is running:

```bash
sudo systemctl status glowup-server
```

If it's not, look at the logs:

```bash
sudo journalctl -u glowup-server -n 50
```

**A schedule entry doesn't fire.** The server polls every thirty
seconds, so allow a minute. If the entry still doesn't run, check
the entry's `days` field (if set) and confirm your latitude and
longitude in `/etc/glowup/site.json` are right — sunrise/sunset
math depends on them.

**A bulb that worked yesterday is unreachable today.** Your router
probably reassigned its IP. The server's ARP keepalive should pick
the new IP up automatically; give it a minute. If it doesn't, run
`glowup discover` from the server and re-confirm the bulb is on the
network. For lights that vanish repeatedly, set a static DHCP
reservation in your router so the bulb's IP stops changing in the
first place — most home routers can pin an IP to a MAC address with
a few clicks.

**The server starts but no bulbs respond.** `/var/lib/glowup/groups.json`
may still be empty (or only contain a placeholder entry pointing at
the unreachable `192.0.2.1`). Use the dashboard, or `glowup name`
and `glowup group add` from the CLI on the server, to populate
real bulbs and groups. Restart the server if you edited the file
by hand instead of via dashboard or CLI.

---

## Where To Go Next

Stop here if standalone or a basic server install is enough for
you. GlowUp does more — sensor adapters, voice control, distributed
workers, MIDI pipelines, screen-reactive lighting, kiosk displays,
custom effect development — but none of that is necessary to drive
your lights.

If you want any of those, the project's deeper documentation
covers them. Open the `docs/` folder and look at `ADVANCED.md`.
Know, however, more sophisticated installers have not been provided
as yet.
