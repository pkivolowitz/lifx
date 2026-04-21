# GlowUp Installer — Design (v3)

Status: **design draft — open for review**
Session: Conway, 2026-04-21.
Supersedes: v2 (web UI + uv + bootstrap.py, 2026-04-12). v2 is preserved in
git history.

This v3 supersedes v2 on: web UI dropped, bootstrap.py dropped (users clone
first), uv dropped (stock `python3 -m venv` + `pip` is enough), single shell
script drives the whole Mac/Linux flow including the feature picker, Windows
is a README stanza not an installer.

v3 inherits from v2 unchanged: installer-owns-config principle,
`site-settings/` gitignored overlay pattern, secrets in
`site-settings/secrets.json` mode 0600, bulb labeling via the dashboard ARP
panel, passive satellite registration via retained MQTT, multi-computer
registration, schema migration on re-run.

---

## Controlling principle

**The installer owns configuration. User hand-edits are unsupported.**

Installer creates, enables, and controls every artifact: venv, service files,
`site-settings/`, `secrets.json`, ports, auth tokens, feature gates. A user
who hand-edits a systemd unit or swaps in their own JSON is out of scope.
This is the only way to promise a working install across macOS, Linux, and
multiple Pi models.

See `feedback_installer_owns_config.md`.

---

## Distribution

Users arrive at the repo by cloning:

```
git clone https://github.com/pkivolowitz/lifx.git glowup
cd glowup
./install.sh
```

There is no separate bootstrap. The installer is part of the repo. If the
user can clone, they can install. No `curl … | sh`, no standalone download
of a fetcher stub, no PyPI publishing. The repo is the distribution.

Windows users do not run `install.sh`. They follow a three-line README stanza
(manual venv + pip + run glowup.py). Windows is local-only by policy — there
is no full server install on Windows, so a real installer for that platform
is not warranted.

---

## Platform tiers

Two tiers, chosen by OS:

### Tier A — local-only (macOS, Windows)

- `glowup.py` CLI lighting effects against LIFX bulbs on the LAN.
- No server, no scheduler, no adapters, no voice, no dashboard, no kiosk.
- Minimal deps — whatever `glowup.py`'s requirements are, installed into a
  venv at `<INSTALL_ROOT>/venv/`.
- On macOS: `install.sh` drives this path.
- On Windows: no installer; README gives three manual steps.

### Tier B — full install (Linux)

- Everything: server, dashboard, scheduler, adapters (Vivint, NVR, printer,
  Matter, Zigbee), voice satellite, kiosk, power logging, BLE sensors.
- Pi OS (Raspberry Pi), Debian 12+, Ubuntu 22.04+ are the primary targets.
- systemd is required; units live under `/etc/systemd/system/` and are
  managed by the installer.
- `install.sh` drives the full feature picker.

**Single-box vs. multi-box is a deployment choice, not a requirement.** On a
capable Linux host (x86 thin client, NUC, even a Pi 5 4GB) every feature —
including Zigbee (via a USB Zigbee coordinator dongle) and BLE sensors (via
the host's BlueZ stack) — runs on the same machine. Multi-box mode exists
for constraint cases: radio placement, weak hardware, or users who already
have a dedicated Pi running Zigbee2MQTT. The installer lets you pick either
path at install time.

Rule of thumb for why the Mac/Win vs. Linux split exists: **anything that
requires systemd is Linux-only.** Pure Python lighting effects and direct
LAN bulb control work anywhere Python 3.10+ runs.

---

## Entry point: `install.sh`

Repo root. `bash`-compatible (no POSIX-only constraints — macOS `bash 3.2`
and Debian `bash 5.x` both supported). The script is the installer. No
hand-off to a Python driver. Python is only used for: creating the venv,
running `pip`, running `glowup.py` itself.

### install.sh responsibilities

In order, with explicit failure modes:

1. **Detect platform** — `uname -s` → `darwin` | `linux`; fail clearly on
   anything else (Windows users are told they're in the wrong place).
2. **Preflight** — confirm `python3 --version` ≥ 3.10, confirm `git`,
   confirm `systemctl` (Linux only). Fail clearly with install hints per
   OS when anything is missing.
3. **Welcome + tier confirmation** — on macOS, state that this is a
   local-only install and confirm; on Linux, state that this is the full
   install and offer `--minimal` flag as an escape hatch.
4. **Feature picker** (Linux only) — numbered list, comma-separated input.
   See §Feature picker.
5. **Paths** — confirm install root (default `$PWD` from the clone), venv
   path, state dir, log dir.
6. **Create venv** — `python3 -m venv $VENV` and
   `$VENV/bin/pip install --upgrade pip wheel`.
7. **Install dependencies** — one `pip install -r` per enabled feature.
   The base `requirements.txt` is always installed; per-feature files in
   `installer/requirements/*.txt` are added conditionally.
8. **Write `site-settings/`** — overlay JSONs for each enabled feature.
9. **Write `site-settings/secrets.json`** — prompt for Vivint, NVR, Matter
   (whatever was enabled). Mode 0600.
10. **Install systemd units** (Linux only) — copy from `installer/systemd/`
    to `/etc/systemd/system/`, templating paths and user. Enable + start.
11. **Self-check** — poll the server's `/api/home/health` if the server
    was enabled. Print a success line per service.
12. **Next steps** — URL to dashboard, URL to API docs, hint about bulb
    labeling.

### install.sh non-responsibilities

- Not a build tool. Does not compile anything. Source is Python.
- Does not manage upgrades implicitly (re-running `./install.sh` offers
  explicit choices — see §Re-run handling).
- Does not edit files outside `$INSTALL_ROOT` and `/etc/systemd/system/`.
  No touching `~/.bashrc`, `/etc/hosts`, `$PATH`, or anything else.
- Does not `sudo` silently. Anything needing root is prompted with the
  exact command that will run.

---

## Feature picker

Linux only. Presented after platform confirmation.

```
Select features (numbers, comma-separated; press enter for "all"):

  1. [always] LIFX light control (core)
  2. [ ] Dashboard web UI
  3. [ ] Scheduler (sunrise/sunset, timers)
  4. [ ] Vivint security (locks, alarm, sensors)
  5. [ ] NVR camera feeds
  6. [ ] Voice control (wake word, STT, TTS)
  7. [ ] Kiosk display (Pi wallclock)
  8. [ ] Power monitoring (Zigbee smart plugs)
  9. [ ] BLE sensors (temperature, humidity, motion)
 10. [ ] Matter adapter
 11. [ ] Zigbee adapter (Z2M — requires USB Zigbee coordinator dongle)
 12. [ ] Multi-computer mode (split roles across additional hosts)

Enter choice [2-12, or empty for all]:
```

Always-on: LIFX light control. You cannot deselect core.

Dependencies enforced: Voice requires Dashboard (TTS status shown there).
Kiosk requires Dashboard (it fetches from `/api/home/`). Validation happens
before the script proceeds — the user is told which dependency is missing
and asked to add it.

Feature selections are persisted to `site-settings/features.json` so a
re-run can diff and apply only the changes.

---

## Platform / feature matrix

| Feature              | macOS | Windows | Linux |
|----------------------|:-----:|:-------:|:-----:|
| LIFX CLI effects     | ✓     | ✓       | ✓     |
| Dashboard            |       |         | ✓     |
| Scheduler            |       |         | ✓     |
| Vivint adapter       |       |         | ✓     |
| NVR adapter          |       |         | ✓     |
| Voice satellite      |       |         | ✓     |
| Voice coordinator    | ✓*    |         | ✓     |
| Kiosk                |       |         | ✓ (Pi) |
| Power monitoring     |       |         | ✓     |
| BLE sensors          |       |         | ✓ (BlueZ) |
| Matter adapter       |       |         | ✓     |
| Zigbee adapter       |       |         | ✓ (needs USB dongle) |

*Voice coordinator on macOS is supported because Perry runs it on Daedalus;
it uses launchd, not systemd. Documented but not part of the default Tier A
flow — enabled via `./install.sh --voice-coordinator`.

---

## Directory layout

Installer owns:

```
<INSTALL_ROOT>/
├── install.sh                    # entry point
├── installer/
│   ├── DESIGN.md                 # this doc
│   ├── requirements/             # per-feature pip requirement files
│   │   ├── base.txt
│   │   ├── voice.txt
│   │   ├── dashboard.txt
│   │   └── ...
│   ├── systemd/                  # systemd unit templates
│   │   ├── glowup-server.service.template
│   │   ├── glowup-satellite.service.template
│   │   └── ...
│   └── schemas/                  # JSON Schema files for validation
│       ├── features.schema.json
│       ├── site-settings.schema.json
│       └── secrets.schema.json
├── site-settings/                # gitignored, installer-written overlays
│   ├── features.json
│   ├── server.json
│   ├── satellite.json
│   └── secrets.json              # mode 0600
├── venv/                         # created by install.sh
└── <rest of repo>                # glowup.py, voice/, handlers/, ...
```

`site-settings/` is gitignored and per-host. `secrets.json` is separate and
mode 0600. Both are touched only by the installer; the server reads them at
runtime.

---

## Schema migration

`installer/schemas/*.schema.json` is the canonical shape of each config
file. On re-run:

1. Load existing `site-settings/*.json`.
2. Validate against current schema.
3. If validation fails, present a migration plan: new keys default-filled,
   removed keys noted, type conflicts flagged as errors the user must
   resolve manually.
4. Write back only after user confirms.

Migration policy: **additive-safe always applied**, **destructive never
without confirmation**. If a key disappears, the installer keeps it in a
`.previous/` sibling file with a timestamp.

---

## Secrets file

`site-settings/secrets.json`, mode 0600, owned by the install user.

```json
{
    "glowup_auth_token": "<generated on first run>",
    "vivint": {
        "username": "...",
        "password": "..."
    },
    "nvr": {
        "username": "...",
        "password": "..."
    },
    "matter": {
        "fabric_id": "...",
        "setup_code": "..."
    }
}
```

Only the secrets for enabled features are prompted for. Every prompt
explains what the secret is used for and where the user gets it (for
Vivint: "your Vivint SkyControl panel login"; for NVR: "admin credentials
you set when first configuring the NVR").

---

## Re-run handling

Running `./install.sh` in an already-installed tree is supported. The
installer detects the existing venv + site-settings and offers:

```
Existing install detected at /home/a/glowup.

  1. Update — pull new deps, apply schema migrations, restart services
  2. Reconfigure — run the feature picker again
  3. Reinstall — wipe venv and site-settings, start fresh (destructive)
  4. Abort

Choice [1]:
```

Option 1 is the common path. It is equivalent to:
- `pip install -r` for every currently-enabled feature (no-op on
  already-installed)
- Schema migration on every `site-settings/*.json`
- `systemctl restart` each glowup-* unit

The installer does **not** run `git pull`. Users pull their own code; the
installer adapts to whatever commit is checked out.

---

## Multi-computer registration — optional

Single-box users skip this entirely. A one-host install is the simplest
path and fully supported: one Linux box runs server + dashboard + every
adapter + voice coordinator + the voice satellite for whatever room it
sits in. That is the default.

Multi-box mode is for three situations:

- Radio placement — Zigbee or BLE coverage needs a host in a different
  room than the server.
- Weak hardware — the primary server is a low-power Pi that can't also
  run Zigbee2MQTT, voice STT, etc.
- Additional rooms — each extra room with a voice satellite needs its own
  Pi (mic + speaker + wake-word detection runs there).

A secondary host runs `./install.sh` and selects **only** the features it
provides (typically voice satellite + whichever radio adapter). The server
host is prompted for (broker IP). The satellite publishes a retained MQTT
message on startup (`glowup/voice/satellite/<room>/hello`); the server's
satellite registry picks this up passively. No per-host edits on the
server side.

---

## What gets deleted from the repo on purge

Carried forward from v2 with v3 adjustments:

- `installer/static/` — no web UI (dropped in v3).
- The HTTP server code in `installer/install.py` — replaced by
  `install.sh`. The file is deleted.
- `bootstrap.py` — never existed; would have been v2; now not needed.

---

## Open (deferred to post-v1)

- Upgrade-only CLI flag (`./install.sh --update-deps-only`) that skips
  prompts entirely. Useful for cron-driven fleet updates.
- Unattended install via `site-settings/answers.json` — ship-in-the-tree
  answers for automated testing / fleet rebuild.
- Homebrew formula / `.deb` package — real OS-level distribution. Out of
  scope for v1 because the repo-clone flow is adequate.
- `install.ps1` for Windows that mirrors the Tier A macOS flow. Deferred
  because the README stanza is adequate for local-only.

---

## Not restated in v3 (carried from v2)

The following subsystem details from v2 remain the authoritative spec for
those areas and are not reproduced here. Consult v2 via git history
(`git log --oneline installer/DESIGN.md` to find the v2 commit) if you
need them:

- LIFX bulb labeling via dashboard ARP panel
- Passive satellite registration via retained MQTT (protocol details)
- JSON schema strawman for `server.json` and `satellite.json`
- Voice SoC table (which Pi model supports which STT engine)
