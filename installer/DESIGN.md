# GlowUp Installer — Design (v2)

Status: **design locked**, awaiting implementation.
Session: Conway, 2026-04-12.
Supersedes: the 2026-04-08 design doc, preserved verbatim in Claude memory
at `project_installer_design_2026-04-08.md`.

This v2 supersedes v1 on: unified single/multi flow, uv-backed venv, Linux-only
feature gating, Windows standalone-only punt, SoC table instead of voice
benchmark, schema migration on re-run, shopping list folded into voice,
bootstrap.py as the entry point, all-WIP framing for optional features, and
re-run handling via explicit prompt.

v2 inherits from v1 unchanged: installer-owns-config principle, site-settings
gitignored overlay pattern, secrets in `site-settings/secrets.json` mode 0600,
`tools/label_bulbs.py` as separate walk-through, bulb labeling via the dashboard
ARP panel, passive satellite registration via retained MQTT, no clone-vs-fork
question, `git clone` as the distribution base (now wrapped by `bootstrap.py`).

---

## Controlling principle

**The installer owns configuration. User hand-edits are unsupported.**

Installer creates, enables, and controls every artifact: venv, service files,
`site-settings/`, `secrets.json`, ports, auth tokens, feature gates. A user who
hand-edits a systemd unit or swaps in their own JSON is out of scope. This is
the only way to promise a working install across macOS, Linux, multiple Pi
models, and user skill levels.

See `feedback_installer_owns_config.md`.

---

## Distribution

**`bootstrap.py`** is the entry point. Users download it standalone (browser
download or `curl -O`) and run `python3 bootstrap.py`. It:

1. Checks `sys.version_info >= (3, 8)` so it can even run
2. Detects platform triple: `{linux,darwin,win32} × {x86_64,aarch64}`
3. Downloads pinned **uv** binary from GitHub releases via `urllib.request` —
   never `curl | sh`, never `irm | iex`. Six triples supported:
   - `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`
   - `x86_64-apple-darwin`, `aarch64-apple-darwin`
   - `x86_64-pc-windows-msvc`, `aarch64-pc-windows-msvc`
4. Extracts uv to `~/.local/share/glowup/uv/uv` (or `%LOCALAPPDATA%\glowup\uv\uv.exe`)
5. Checks for `git`; on failure, prints install hint for detected OS and exits
6. `git clone https://github.com/pkivolowitz/lifx.git glowup` (or user's
   `--repo-url <url>` for power users who pre-forked)
7. `cd glowup`
8. `uv python install 3.11` — downloads a standalone python-build-standalone
   binary; never touches system Python
9. `uv venv --python 3.11 ./venv`
10. `exec` into `./venv/.../python installer/install.py` for the rest of the flow

All runtime code (including `installer/install.py`) targets Python **3.11+**.
Only `bootstrap.py` is 3.8-compatible.

uv version is pinned as a module-level constant `UV_VERSION = "0.x.y"`. Audited
and bumped manually, never auto-updated.

---

## Directory layout

Tracked in git:

    installer/
      install.py                              — the terminal installer entry point
      DESIGN.md                               — this document
      templates/
        glowup-server.service.in              — systemd unit template (hardened)
        glowup-satellite.service.in           — systemd unit template
        net.glowup.server.plist.in            — launchd plist template
    sample-site-settings/
      settings.json                           — sample shape (see JSON schema below)
      secrets.json                            — sample shape, all values null
    tools/
      label_bulbs.py                          — walk-through LIFX labeling tool
      reconfigure.py                          — post-v1 reconfigure tool (stub)
    bootstrap.py                              — downloadable standalone installer launcher

Gitignored (created by the installer on first run):

    site-settings/
      settings.json                           — features, role, ports, per-feature config
      secrets.json                            — mode 0600, credentials only
      bulbs.json                              — managed by tools/label_bulbs.py
    venv/                                     — uv-created venv, python 3.11

`.gitignore` lists `site-settings/`, `venv/`, and `bootstrap.py` should NOT be
in the repo — it lives as a GitHub release artifact or is linked from the
README, not carried inside the repo tree.

---

## Flow — unified single/multi-computer

No separate single vs multi branch. Role is a preset over the feature picker.

**Step 0: License.** Read `LICENSE` from repo root, display summary, require
explicit confirmation. Decline → exit, no side effects.

**Step 1: Platform gate.**
- If `sys.platform == "win32"` → jump to Windows standalone flow (below)
- Else continue

**Step 2: Role prompt.**

    Is this the only computer running GlowUp, or one of several?
      1. Only computer (single-computer install)
      2. One of several (multi-computer install)
    Choice:

If multi:

    What role does this computer play?
      1. Central hub
      2. Voice satellite
      3. Kiosk
      4. Custom (pick features manually)
    Choice:

Role maps to a pre-checked set in the feature picker (see `ROLE_PRESETS` in
implementation).

**Step 3: Feature picker.**

    Select features (numbers, comma-separated):

      1. [always]   LIFX light control (core)
      2. [ WIP  ]   Zigbee device control
      3. [ WIP  ]   Voice control (incl. shopping list)
      4. [ WIP  ]   Kiosk display
      5. [ WIP  ]   Power monitoring
      6. [ WIP  ]   BLE sensors
      7. [ WIP  ]   NVR camera feeds
      8. [ WIP  ]   Vivint security integration

      a. Select all
      k. Know more about a feature (enter number)

Features are platform-gated per `FEATURES` dict. Features unsupported on the
current OS are hidden entirely. On macOS, Zigbee/BLE/Kiosk do not appear. On
Windows, only LIFX and the standalone flow are offered.

**WIP ack** — every optional feature prints a warning at select time:

    Zigbee is a WORK IN PROGRESS.

    - Active development. Interfaces and behavior change without notice.
    - No warranty. No support. No guarantees of any kind.
    - You may hit bugs, regressions, or outright broken state on any given day.
    - File issues if you want, but no response is promised.
    - If you cannot debug this yourself and accept that things may break,
      do not install this feature.

    The MIT license in the repo root already disclaims all warranty. This
    is a reminder, not a change.

    Install Zigbee anyway? [y/N]

`N` is the default. `y` adds the feature; `N` leaves it unchecked and the
picker redraws.

**Step 4: Per-feature hardware probes (Linux only).**

For each selected feature, call its probe function. Probes return
`(found: bool, detail: str, hint: str)`.

| Feature       | Probe                                          | Hint on miss                                          |
|---------------|------------------------------------------------|-------------------------------------------------------|
| Zigbee        | `lsusb` for known coordinator VIDs             | "No coordinator found. Plan to buy one? [y/N]"        |
| BLE           | `hciconfig -a` shows UP interface              | "No BLE adapter. Plan to add a BT500 dongle? [y/N]"   |
| Kiosk         | `$DISPLAY` set, `/dev/fb0` exists              | "No display detected. Kiosk needs HDMI + keyboard."   |
| Voice (mic)   | `arecord -l` lists capture device              | "No microphone. Plan to add a USB mic? [y/N]"         |
| Voice (spkr)  | `aplay -l` lists a card                        | "No audio output. Plan to add speakers? [y/N]"        |
| Power         | no hardware probe (MQTT-tested at cred step)  | n/a                                                   |
| NVR           | no hardware probe                              | n/a — prompt for IP                                   |
| Vivint        | no hardware probe (cloud-only)                 | n/a                                                   |
| LIFX          | LAN broadcast scan (~2s bounded)               | "No bulbs found. Install anyway? [y/N]"               |

Probes are Linux-only. macOS skips probes entirely (features that need them
are already hidden by platform gating). Probes never block on network longer
than 2 seconds. "I plan to buy one" is always a valid answer — never blocks
install. Probe failure is never fatal.

**Step 5: Voice hardware tier check** (if voice selected).

See `VOICE_CAPABILITY` table below. Match `/proc/cpuinfo` / `sysctl` against
the table, then apply RAM and AVX2 downgrades. Tier drives behavior:

- **overkill** — install normally, print one random compliment from
  `OVERKILL_COMPLIMENTS` pool
- **good** — install normally, no warning
- **marginal** — install, soft warning, default to tiny whisper model
- **bad** — hard warning, default to skip, escape hatch offers "install
  anyway" or "I'll upgrade hardware later"
- **reject** — refuse to install voice runtime. Only option: "I'll install on
  better hardware later." Print the reason (armv6 / no AVX2 / <1GB RAM)

**Step 6: Configuration interview.**

For each selected feature, prompt for values. Non-secret values go to
`site-settings/settings.json`, credentials go to `site-settings/secrets.json`.

Every credential prompt is preceded by a friendly explanation:

- *what* the credential is for
- *why* it's needed
- *where* it will be stored (path + mode + ownership)
- "Nothing leaves this machine. Never logged."

Users can press Enter to **skip** any credential. Skipped = `null` in
`secrets.json`, feature disabled at runtime with a log message until filled.

Where cheap, **verify-before-write**: for MQTT creds, try `mqtt.Client.connect`
and show `✓ connected` or `✗ auth failed, re-enter?`. For Reolink, test HTTP
auth. Skip verification for rate-limited / slow services like Vivint.
`--no-verify` disables all verification checks for offline installs.

All password prompts use `getpass.getpass()`, never `input()`.

If multi-computer and role ≠ hub: ask for hub IP, ping it, probe TCP 1883 and
TCP 8420. Fail fast with a clear error if unreachable.

**Step 7: Environment setup.**

- uv-created venv already exists (bootstrap.py did it)
- `uv pip install -r requirements.txt` (base)
- For each enabled feature, `uv pip install <feature-deps>` per the
  `FEATURE_DEPS` dict
- All feature deps remain **guarded imports** in runtime code

**Step 8: Service setup.**

    Set up GlowUp to start automatically at boot? [Y/n]

On yes:

- **Linux**: generate `/etc/systemd/system/glowup-server.service` from
  `installer/templates/glowup-server.service.in`. Substitute invoking user,
  venv path, working directory. Unit includes hardening directives:
  ```
  [Service]
  User=<invoking_user>
  NoNewPrivileges=true
  ProtectSystem=strict
  ProtectHome=read-only
  PrivateTmp=true
  ReadWritePaths=<repo>/site-settings <repo>/venv /var/log/glowup
  SystemCallFilter=@system-service
  SystemCallErrorNumber=EPERM
  RestrictNamespaces=true
  ```
  Run `systemctl daemon-reload`, `enable`, `start`, verify `is-active`.
- **macOS**: generate `~/Library/LaunchAgents/net.glowup.server.plist`,
  `launchctl load`, verify. No extra sandboxing for v1.
- **Satellite**: also install `glowup-satellite.service` unit with the same
  hardening.

Declining service setup prints the manual commands and writes NO partial
systemd state.

**Step 9: First-run verification.** Start the server, hit `/api/status`,
report what's online.

**Step 10: Summary.**

- Print what was installed
- Print location of `site-settings/` with the warning: installer-managed,
  do not hand-edit
- Global WIP reminder: "You installed these WIP features: <list>. Things
  will break. You accepted this per-feature above. Check `journalctl -u
  glowup-server`. Open issues with logs. Response is best-effort."
- Next steps:
  - **Run `python3 tools/label_bulbs.py` to label your LIFX bulbs**
  - Open `http://localhost:8420/dashboard`
  - Tools provided: `tools/restart.sh`, `tools/health.sh`

Installer exits.

---

## Windows standalone flow

If `sys.platform == "win32"`:

1. License ack (same as linux/mac)
2. Print punt message:

        GlowUp on Windows supports standalone LIFX control only.
        Server features (Zigbee, voice, kiosk, power, cameras, Vivint)
        require Linux or macOS. To run the full server, use a Raspberry Pi
        or a Mac. See docs/pi-shopping.md for hardware suggestions.

3. Offer to install standalone:
   - uv venv (already done by bootstrap.py on Windows too)
   - `uv pip install -r requirements-standalone.txt`
   - Add a `glowup` alias to PowerShell `$PROFILE`: `function glowup {
     & <venv>\python.exe <repo>\glowup.py @args }`
4. Note SmartScreen: "If Windows blocks uv.exe, click 'More info' → 'Run
   anyway'. We fetched it straight from Astral's signed GitHub release."
5. Print next step: `glowup --discover`
6. Exit.

No feature picker, no site-settings, no services, no probes. ~30 lines of
Windows branch inside install.py.

---

## Platform / feature matrix

Single source of truth in `install.py`:

```python
FEATURES = {
    "lifx":    {"platforms": {"linux", "darwin", "win32"}, "probe": probe_lifx,    "deps": [...]},
    "zigbee":  {"platforms": {"linux"},                    "probe": probe_zigbee,  "deps": ["paho-mqtt"]},
    "ble":     {"platforms": {"linux"},                    "probe": probe_ble,     "deps": ["bleak"]},
    "kiosk":   {"platforms": {"linux"},                    "probe": probe_kiosk,   "deps": ["pygame"]},
    "voice":   {"platforms": {"linux", "darwin"},          "probe": probe_voice,   "deps": ["faster-whisper","openwakeword","piper-tts","paho-mqtt","numpy","sounddevice"]},
    "power":   {"platforms": {"linux", "darwin"},          "probe": None,          "deps": ["paho-mqtt"]},
    "nvr":     {"platforms": {"linux", "darwin"},          "probe": None,          "deps": ["reolink-aio"]},
    "vivint":  {"platforms": {"linux", "darwin"},          "probe": None,          "deps": ["vivintpy"]},
}
```

Adding a feature = one new row. Platform gate = list the platforms. No
conditional probe logic anywhere else. Shopping list is not a separate key —
it lives inside voice.

---

## Voice SoC table

```python
VOICE_CAPABILITY = [
    # (regex, tier, note)
    (r"Raspberry Pi 5",                 "good",     "tiny + base comfortably"),
    (r"Raspberry Pi 4",                 "marginal", "tiny only; base stutters"),
    (r"Raspberry Pi 3",                 "bad",      "too slow"),
    (r"Raspberry Pi Zero 2",            "bad",      "RAM + CPU insufficient"),
    (r"Raspberry Pi Zero(?! 2)",        "reject",   "armv6, not supported by faster-whisper"),
    (r"Raspberry Pi 1",                 "reject",   "armv6, not supported by faster-whisper"),
    (r"Orange Pi Zero 3",               "bad",      "Pi Zero 2 class"),
    (r"Orange Pi 3B|Orange Pi 5",       "good",     "RK3566 / RK3588"),
    (r"Intel.*Atom",                    "reject",   "no AVX2, insufficient"),
    (r"Intel.*Celeron",                 "marginal", "variable, prompt user"),
    (r"Intel.*Core.*i[3-9]",            "good",     "AVX2 assumed"),
    (r"AMD.*Ryzen",                     "good",     ""),
    (r"AMD.*(A[4-9]|FX)",               "marginal", "old, no AVX2 on some"),
    (r"Threadripper|EPYC|Xeon",         "good",     "(and overkill-trigger)"),
    (r"Apple M",                        "good",     "Metal + unified memory"),
]

OVERKILL_COMPLIMENTS = [
    "This machine could transcribe the whole neighborhood in real time.",
    "Your rig could run voice for a small call center. Nice.",
    "We're going to run voice on this? It's going to be bored.",
    "This hardware deserves a harder problem than wake-word detection.",
    "Detected: a machine that could beat Whisper at its own game and still have cycles to spare.",
    "Installing voice on this is like hiring a neurosurgeon to open a jar.",
    "Voice stack will barely notice it's running. Respect.",
]
```

**Tier downgrades (applied after match):**
- RAM < 1 GB → **reject** (hard floor, independent of CPU match)
- RAM < 2 GB → downgrade one tier (good → marginal → bad → reject)
- Linux x86_64 missing `avx2` in `/proc/cpuinfo` flags → downgrade one tier

**Overkill promotion (applied after match):**
- Total RAM ≥ 32 GB, OR
- CPU matches `Apple M[2-9].*(Pro|Max|Ultra)`, OR
- CPU matches `Threadripper|EPYC|Xeon`, OR
- Discrete NVIDIA GPU detected via `lspci` (Linux) or `system_profiler
  SPDisplaysDataType` (macOS)

Promotes `good` → `overkill`. Unlocks a random compliment from
`OVERKILL_COMPLIMENTS` on print.

**Detection per OS:**

| OS      | CPU model                                          | RAM                                                                 |
|---------|----------------------------------------------------|---------------------------------------------------------------------|
| Linux   | `/proc/cpuinfo` → "model name" or "Hardware"       | `/proc/meminfo` → "MemTotal"                                        |
| macOS   | `sysctl -n machdep.cpu.brand_string`               | `sysctl -n hw.memsize`                                              |
| Windows | not reached (voice is punted on Windows)           | not reached                                                         |

Unknown CPU → print `"Unknown CPU: <name>. Voice may or may not work.
Proceed?"` and let the user choose.

---

## Schema migration

Every time `install.py` runs (first install OR re-run), AND every time
`glowup-server` starts, call `merge_settings(sample, live)`:

```python
def merge_settings(sample: dict, live: dict) -> tuple[dict, list[str]]:
    """Recursively add missing keys from sample into live with sample
    defaults. Never overwrite existing values. Returns (merged, added_paths)."""
    added: list[str] = []
    def _walk(s: dict, l: dict, path: str) -> None:
        for key, sv in s.items():
            full = f"{path}.{key}" if path else key
            if key not in l:
                l[key] = sv
                added.append(full)
            elif isinstance(sv, dict) and isinstance(l[key], dict):
                _walk(sv, l[key], full)
    _walk(sample, live, "")
    return live, added
```

If `added` is non-empty, print the friendly summary:

    GlowUp added 3 new settings since your last install:

      ble_stranger_detection.enabled       → false
      ble_stranger_detection.scan_interval → 30
      voice.wake_word_model                → "hey_glowup"

    These are defaults only. Your existing settings were not changed.
    Edit site-settings/settings.json to customize, then restart glowup-server.

Zero added → print nothing. Existing values are never touched. Deleted keys
in sample are left alone in live. Type changes need a manual migration,
handled by bumping `schema_version` when it happens.

---

## JSON schemas — strawman

### `sample-site-settings/settings.json`

```json
{
  "schema_version": 1,
  "machine": {
    "role": "hub",
    "hostname": "glowup",
    "dashboard_port": 8420,
    "location": {
      "latitude": null,
      "longitude": null,
      "timezone": null
    }
  },
  "mqtt": {
    "host": "127.0.0.1",
    "port": 1883,
    "client_id_prefix": "glowup"
  },
  "features": {
    "lifx": {
      "enabled": true,
      "subnet": "auto",
      "discovery_timeout_s": 2.0
    },
    "zigbee": {
      "enabled": false,
      "coordinator": "local_usb",
      "device": "/dev/ttyUSB0",
      "z2m_topic_prefix": "zigbee2mqtt"
    },
    "voice": {
      "enabled": false,
      "wake_word": "hey_glowup",
      "whisper_model": "tiny.en",
      "piper_voice": "en_US-amy-low",
      "ollama_host": "http://127.0.0.1:11434",
      "ollama_model": "llama3.2:3b",
      "shopping_list": {
        "enabled": false,
        "path": "site-settings/shopping.json"
      }
    },
    "kiosk": {
      "enabled": false,
      "display": ":0",
      "rotation": 0,
      "brightness": 100
    },
    "power": {
      "enabled": false,
      "plugs": []
    },
    "ble": {
      "enabled": false,
      "adapter": "hci0",
      "sensors": []
    },
    "nvr": {
      "enabled": false,
      "cameras": []
    },
    "vivint": {
      "enabled": false
    }
  },
  "satellites": {
    "expected_hostnames": []
  }
}
```

### `sample-site-settings/secrets.json`

```json
{
  "schema_version": 1,
  "dashboard_auth_token": null,
  "mqtt": {
    "username": null,
    "password": null
  },
  "vivint": {
    "username": null,
    "password": null
  },
  "nvr": {
    "default_username": null,
    "default_password": null,
    "per_camera": {}
  }
}
```

Notes:
- `schema_version`: integer, independent per file. Used by schema migration.
- `features` is a dict (key = identity), never an array.
- `lifx.enabled` is always true but encoded explicitly for uniform iteration.
- `dashboard_auth_token` lives in secrets, not settings. Everything a bad
  actor wants is in one 0600 file.
- `mqtt` at top level, not under any feature. It's shared infrastructure.
- Secrets default to `null`; server code treats null as "feature cannot
  authenticate, disable with log message."

---

## Secrets file, friendly prompts

Every credential prompt follows this shape:

    Vivint stores lock/alarm/sensor state we can read. I'll save your
    username and password to site-settings/secrets.json, owned by the
    service user, mode 0600. Nothing leaves this machine. Never logged.

    Press Enter to skip and fill in later.

    Vivint username: ___
    Vivint password: (getpass)

Skip → `null` in secrets.json, feature disabled at runtime with a log line.

Verify-before-write on cheap checks (MQTT, Reolink). Skip verify on
Vivint. `--no-verify` disables all verification.

All password prompts use `getpass.getpass()`.

---

## Re-run handling

If `site-settings/settings.json` exists when `install.py` runs:

    GlowUp is already installed on this machine.
      1. Resume — finish the install (re-runs pip, service setup, verify)
      2. Start over — delete site-settings/ and venv/ and re-run from scratch
      3. Exit
    Choice:

**Resume** = skip the interview, re-run every post-interview step. All
post-interview steps are idempotent by construction:
- `uv pip install -r requirements.txt` → no-op on already-installed
- `systemctl daemon-reload`, `enable`, `start` → idempotent
- `systemctl is-active` → read-only

No state-machine, no checkpoint file. If resume fails again, user can re-run
and pick "start over."

**Start over** = `rm -rf site-settings/ venv/` after typed confirmation:

    This will delete site-settings/ and venv/. Type "yes, wipe" to confirm:

Typo-proof against destructive accidents.

**Exit** = no side effects, exit 0.

This also provides a crude reconfigure path (start over → re-pick features)
until `tools/reconfigure.py` is built post-v1.

---

## LIFX bulb labeling

**`tools/label_bulbs.py`** — walk-through CLI tool, separate from the
installer. Called out in the install summary as the next step.

- Broadcast-scan for LIFX devices
- Load `site-settings/bulbs.json`, skip entries where `logical_label` is
  non-null
- For each candidate, call `device_manager.identify()`, prompt:

      A bulb is flashing. Is it in front of you? [y/n/skip/quit]

- On `y`, prompt for logical label, write to JSON, move on
- **JSON only by default.** `--write-bulb-label` flag opts in to also
  calling `SetLabel` on the bulb (updates the bulb's factory label to the
  logical name).
- `--all` flag walks every bulb regardless of labeling state (for renaming)

**`site-settings/bulbs.json`** — keyed by MAC:

```json
{
  "d0:73:d5:xx:xx:xx": {
    "mac":           "d0:73:d5:xx:xx:xx",
    "lifx_label":    "LIFX Color A19",
    "logical_label": "living room lamp",
    "ip":             "10.0.0.73"
  }
}
```

Source of truth is `logical_label`. `ip` is an informational cache.

**Dashboard inline labeling** — in the existing "Discovered Devices (ARP
Keepalive)" panel at `static/dashboard.html:523`, add per row:
- Identify button (POST `/api/bulbs/identify`)
- Inline-editable label control (POST `/api/bulbs/label`)
- Read-only factory `lifx_label` alongside

`GET /api/discovered_bulbs` is extended to join against `bulbs.json`, adding
`logical_label` and `lifx_label` fields to each row. Backwards-compatible.

Groups remain dashboard-only (existing group CRUD), not part of labeling.

---

## Multi-computer registration

**Passive from the hub.** Nothing on the hub is touched during a satellite
install.

- Satellite installer asks user for hub IP, writes to its own
  `site-settings/settings.json` under `mqtt.host`
- Satellite service connects to the hub's MQTT broker on first boot
- Satellite publishes a **retained** message on
  `glowup/registry/<hostname>` containing role, enabled features, version,
  and identity
- Hub subscribes to `glowup/registry/#` and adds to fleet view on first
  retained message
- De-registration = publish empty retained message (tombstone). A
  `tools/deregister.sh` will be provided.

Not discovery — the satellite must be explicitly told the hub address at
install time.

---

## What gets deleted from the repo

Already done in this session:
- `installer/DESIGN.md` v1 — moved to Claude memory
  (`project_installer_design_2026-04-08.md`), then `git rm` (staged)

To be deleted in the implementation commit:
- `installer/install.py` v1 (323-line web-installer stub) — replaced wholesale
- `installer/static/` entire tree — dead
- Any `CLI_BOM` file and sparse-checkout machinery — dead

To be added in the implementation commit:
- `bootstrap.py` at repo root (new)
- `installer/install.py` rewritten
- `installer/DESIGN.md` v2 (this file)
- `installer/templates/*.in`
- `sample-site-settings/settings.json`
- `sample-site-settings/secrets.json`
- `tools/label_bulbs.py`
- `.gitignore` entries for `site-settings/` and `venv/`

---

## Open (deferred to post-v1)

- `tools/reconfigure.py` — full reconfigure without "start over." Must-build
  post-v1, not a v1 blocker (start-over provides a crude path).
- Windows voice support — tracked as a separate subproject in
  `project_voice_on_windows.md`. Not in v1.
- Air-gapped / offline install — v1 assumes network for uv + Python +
  packages. Offline-install story is post-v1.
- The sentinel `feedback_installer_owns_config.md` and related feedback
  memories remain authoritative for implementation decisions not covered here.
