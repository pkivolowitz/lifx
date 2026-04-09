# GlowUp Installer — Design

Status: **design in progress**, not yet implemented.
Last updated: Conway, 2026-04-08.

This document is the working design for the GlowUp installer rewrite.
It supersedes the browser-based installer currently on disk in this
directory (`install.py` + `static/installer.js`), which will be
replaced wholesale once this design is locked in.

This is a living design doc, not a precompact. Update it as
decisions are made. When sections become stable they graduate into
user-facing docs (`docs/03-quick-start.md`, `docs/24-persistent-services.md`).

---

## Why the rewrite

The previous installer was a stdlib HTTP server that launched a
browser UI and performed a **sparse checkout** of a hand-maintained
`CLI_BOM` file list (see the old `install.py` for context). That
approach is being abandoned because:

- **Version inconsistency.** Sparse checkout + file list means
  the user doesn't have a real clone — they have a snapshot. There
  is no `git pull` path to upgrades. Every update is a full re-install.
- **BOM maintenance tax.** Any file added to the project has to be
  manually added to `CLI_BOM` or it silently stops shipping. This is
  a future bug factory.
- **Two installers in one.** "Browser UI for configuration" and
  "terminal installer for the server stack" are different problems;
  trying to solve both in one tool made both halves worse.

The new approach: a plain terminal installer invoked inside a real
git clone, with user configuration living in a **gitignored overlay
directory** that survives `git pull`.

---

## Controlling principle

**The installer owns configuration. User hand-edits are unsupported.**

The installer creates, enables, and controls every configuration
artifact: venv, service files, `site-settings/`, `secrets.json`,
ports, auth tokens, feature gates, everything. If a user deviates
— hand-edits a systemd unit, points at their own venv, swaps in
their own JSON — we do not owe them support. Perry may still feel
bad when it breaks, but the license disclaims it, and the installer's
implicit promise is "if you let us do it, it works."

This is the only way we can promise a working install across
macOS, Linux, multiple Pi models, and user skill levels. It is
documented as a project feedback memory
(`feedback_installer_owns_config.md`).

Corollaries:

- Escape hatches like "skip venv creation, bring your own" are
  **not** added.
- Reconfiguration goes through a tool *we* provide (see Section
  "Re-run / reconfigure"), not hand-editing.
- User-facing docs state clearly that `site-settings/` is installer-
  managed and hand-edits are out of scope.

---

## Distribution and bootstrap

The installer lives **inside the repository** at `installer/install.py`.
It is distributed by `git clone`, never by `curl | python3`. The full
first-install command is:

    git clone https://github.com/pkivolowitz/lifx.git glowup
    cd glowup
    python3 installer/install.py

Consequences:

- The user already has a working `git` by the time they reach the
  installer. No "git missing? exit" gate is needed — that contradiction
  is resolved. (Original concern: "they might have enough git to get
  started but not enough to finish." The clone-first distribution
  model makes that impossible — if `git clone` worked, the toolchain
  is complete enough.)
- Upgrades are `git pull` inside the clone, followed by `pip install
  -r requirements.txt` inside the venv, followed by a service restart.
  Site-settings are gitignored and survive the pull.
- The installer can upgrade itself (just by pulling), because it is
  part of the repo.

### Step zero — license acknowledgement

Before anything else, the installer reads `LICENSE` from the repo
root, displays the copyright and license summary, and asks the user
to confirm they want to continue. Decline → exit cleanly, no side
effects.

### Developer / contributor framing

This is **out of the install flow**. The installer does not ask
"are you a developer?" or "clone vs fork?" Fork is a GitHub-account
action the installer cannot perform anyway. After successful install,
the summary screen points the user at `docs/CONTRIB.md` (or a new
section of it) which explains how to fork on GitHub and add their
fork as a second remote if they plan to contribute upstream. Most
users want a clone and nothing more; don't make them answer a
question that doesn't apply to them.

---

## Directory layout

Tracked in git (part of the repo, shipped to every user):

    installer/
      install.py             — the terminal installer entry point
      DESIGN.md              — this document
      templates/
        glowup-server.service.in    — systemd unit template
        glowup-satellite.service.in — systemd unit template
        net.glowup.server.plist.in  — launchd plist template
      (more as needed)
    sample-site-settings/
      local.json             — sample shape for a single-computer install
      hub.json               — sample shape for a multi-computer hub
      satellite.json         — sample shape for a multi-computer satellite
      secrets.json           — sample shape for credentials (commented out)
    tools/
      label_bulbs.py         — walk-through LIFX labeling tool (separate, see below)

Gitignored (created by the installer on first run):

    site-settings/           — active configuration for this machine
      local.json             — features, role, ports, auth token, machine IPs
      secrets.json           — mode 0600, credentials only
      bulbs.json             — managed by the label tool, not the installer
    venv/                    — installer-created virtualenv

`.gitignore` must list both `site-settings/` and `venv/` at the
repo-root level. `sample-site-settings/` is **not** gitignored.

On first install, the installer:

1. Confirms `site-settings/` does not exist (see Re-run section).
2. Copies `sample-site-settings/` to `site-settings/`.
3. Walks the feature picker and interview.
4. Rewrites `site-settings/local.json` (or `hub.json` / `satellite.json`)
   with the user's real answers.
5. Chmods `site-settings/secrets.json` to `0600`.

---

## Single-computer flow

**Step 0: License.** See above.

**Step 1: Python version check.**
Minimum version TBD — open question (see open questions).
If below minimum, print install instructions for the detected OS
and exit.

**Step 2: Single vs multi-computer.**

    How are you installing GlowUp?
      1. Single computer — this machine runs everything
      2. Multi-computer — I have a hub and one or more satellites
    Choice:

Single → continue here. Multi → jump to the multi-computer flow.

**Step 3: Feature picker.**
Checkbox-style menu of optional features. LIFX core is always on;
everything else is opt-in. Exact list is an open question (see below)
but is roughly the old spec's nine features:

    Select features (comma-separated numbers, or 'a' for all):
      1. [always] LIFX light control (core)
      2. [ ] Zigbee device control
      3. [ ] Vivint security integration
      4. [ ] NVR camera feeds
      5. [ ] Voice control
      6. [ ] Kiosk display
      7. [ ] Power monitoring
      8. [ ] BLE sensors
      9. [ ] Shopping list

    a. Select all
    k. Know more about a feature (enter number)

"Know more" prints a short description and a URL to the relevant
docs page for that feature.

**Step 4: Feature preflight.** For every selected feature that needs
special hardware or a heavy runtime, the installer does a targeted
sanity check:

- **Voice** → run the voice sizing probe. If hardware cannot pin one
  Ollama model with headroom, hard-warn and offer to skip. If it
  cannot pin two simultaneously, warn that full voice is degraded.
  See "Voice sizing" below.
- **Zigbee** → look for a local USB coordinator via `lsusb` / macOS
  equivalent, or ask for an existing Z2M MQTT broker address. If
  neither present, warn and offer to skip *or* proceed (user may be
  planning to buy hardware).
- **BLE** → check for Bluetooth adapter via `hciconfig` / macOS
  system profiler.
- **NVR / Vivint** → no detection possible without credentials.
  Install proceeds; credentials are asked for at configuration time.
- **Kiosk** → check we're on Linux with a framebuffer or X (this
  feature is Pi-oriented).

**Step 5: Configuration interview.** For each selected feature,
prompt for the values the feature needs. Every answer goes into
`site-settings/local.json` except for passwords / tokens, which go
into `site-settings/secrets.json`.

Values asked for (still being finalized):

- Dashboard port (default `8420`)
- Dashboard auth token (generate random, or prompt)
- MQTT broker address if Zigbee / Power / BLE selected
- Vivint credentials (secrets.json)
- NVR host, credentials (secrets.json)
- Voice: wake word, Ollama model names, coordinator host
- Kiosk: display device, rotation, brightness
- Location (lat/lon) for scheduling — optional, can skip
- Device groups — **not asked here**, see LIFX labeling note below

**Step 6: Environment setup.** Installer creates `venv/`, activates
it, installs `requirements.txt`, then installs feature-specific
requirements per the old spec's matrix (Zigbee → `paho-mqtt`,
Voice → `faster-whisper` + `openwakeword` + `piper-tts` + ...,
BLE → `bleak`, etc.). All feature deps remain **guarded imports**
per the existing architecture rule — base system still runs with
zero optional deps.

**Step 7: Service setup.**
Asks once, then owns the service files.

    Set up GlowUp to start automatically at boot? [Y/n]

On yes:

- **Linux**: generates `/etc/systemd/system/glowup-server.service`
  from `installer/templates/glowup-server.service.in`, substituting
  venv path, working directory, and service user. Runs `systemctl
  daemon-reload`, `enable`, `start`, verifies `systemctl is-active`.
- **macOS**: generates `~/Library/LaunchAgents/net.glowup.server.plist`
  from the template, runs `launchctl load`, verifies.

User-scope launchd on macOS needs no sudo. Linux systemd needs sudo
for the unit install — on the Pi this is passwordless, on a fresh
install we prompt. If the user declines service setup, the installer
still prints the manual command but does NOT write partial state.

**Step 8: First-run verification.** Start the server, hit
`/api/status`, report what's online. Print the summary:

- What was installed
- Where site-settings lives (and the warning: installer-managed, do
  not hand-edit)
- Next steps:
  - **Run `python3 tools/label_bulbs.py` to label your LIFX bulbs.**
  - Open `http://localhost:<port>/dashboard` to assign groups and
    configure the rest.
  - Tools we provide: `tools/restart.sh`, `tools/health.sh`, etc.
  - Where docs live.

Installer exits.

---

## Multi-computer flow

Triggered when the user answers "multi-computer" at Step 2.

**Step M1: What role does *this* machine play?**

    What role does this computer have?
      1. Hub — central server (MQTT broker, dashboard, adapters)
      2. Satellite — peripheral (voice room, kiosk, etc.)
    Choice:

**Step M2a: Hub role.** Essentially the same as the single-computer
flow: feature picker, interview, venv, services, verification. The
hub does not need to know about its satellites up front — they
announce themselves later (see registration model).

**Step M2b: Satellite role.** Reduced feature picker (only
satellite-meaningful features: voice, kiosk, BLE, etc.). Asks for:

- **Hub IP address** (required). Installer pings it, tries TCP 1883
  and TCP 8420 to sanity-check reachability. Fails fast with a clear
  error if the hub isn't reachable.
- MQTT broker auth token if the hub has one set.
- Feature-specific values per the selected features.

Writes `site-settings/satellite.json` with role, hub IP, features,
and this machine's hostname.

### Registration model

Satellites register themselves with the hub **passively** — the hub
does not need to be modified during a satellite install, and no
cross-machine SSH or hub credentials are required.

Mechanism:

- On first boot after install, the satellite service reads
  `site-settings/satellite.json`, finds the hub's MQTT broker address
  (which the installer wrote there when it asked the user), and
  connects.
- The satellite publishes a **retained** message on
  `glowup/registry/<hostname>` containing its role, enabled features,
  GlowUp version, and identity.
- The hub is subscribed to `glowup/registry/#` and adds the satellite
  to its fleet view when the retained message arrives. Dashboard
  reflects this automatically.
- Removing a satellite is done by publishing an empty retained
  message on its topic (tombstone) — can be a `tools/deregister.sh`
  script.

This is not *discovery* — the satellite must be explicitly told the
hub's address during install. "Passive" here means "the hub is
passive — nothing on the hub side needs to be touched during a
satellite install."

---

## Feature picker and per-feature details

The exact v1 feature list is an open question. Candidate features
(from the old spec):

- LIFX core (always on)
- Zigbee — requires a Z2M instance or a local USB coordinator
- Vivint — requires account credentials
- NVR — requires camera host + credentials
- Voice — requires sizing probe, needs wake word + Ollama + Whisper + Piper
- Kiosk — requires a display
- Power monitoring — requires ThirdReality plugs via Z2M
- BLE sensors — requires Bluetooth adapter
- Shopping list — stdlib only, no extra deps

Each feature has a `docs/features/<name>.md` one-pager the installer
links to via the "know more" option. These docs are tracked in the
repo and are part of the installer's UX.

---

## Voice sizing

Voice is the most demanding optional feature — it pulls in
`faster-whisper`, `openwakeword`, `piper-tts`, and expects Ollama to
be reachable with one or two models resident in RAM.

Tiers (Perry's words, 2026-04-08):

- **Full voice (beefy).** Machine can pin **both** production Ollama
  models in RAM simultaneously, with a strong CPU. Full voice stack
  runs.
- **Minimum.** Machine can pin a single smaller Ollama model with
  enough headroom for whisper + piper + the rest. Voice runs, but
  degraded.
- **Below minimum.** Hard warning. Installer offers to skip voice or
  proceed anyway (the user's call).

Probe design:

- Check total RAM (informational).
- Check available RAM after a fresh boot (the real constraint).
- Optionally run a short RTF (real-time factor) benchmark by
  transcribing a small bundled WAV with `faster-whisper`. If RTF >
  some threshold, warn the user.

Exact thresholds are an open question — Perry approved the benchmark
idea in principle but didn't pick numbers.

---

## Secrets

Credentials that the code needs to read on every startup (Vivint,
NVR, MQTT broker auth, etc.) live in **`site-settings/secrets.json`**:

- Gitignored (the whole `site-settings/` directory is).
- Mode `0600`, owned by the service user.
- Loaded by the code at startup alongside `local.json`.
- **Never logged, never printed in error messages, never included in
  diagnostic bundles.** Anything that writes diagnostics must
  explicitly strip secrets.

Rejected alternatives:
- OS keyring (`python-keyring`) — non-starter for headless systemd
  on a Pi, since the service user has no login session to unlock
  the keyring. Adds a guarded-import dep. Opaque to debug.
- Environment variable file (`/etc/glowup/env` read by systemd
  `EnvironmentFile=`) — works, but splits configuration across two
  conceptual homes. Rejected for keeping everything in one place.

Plaintext-on-disk is not a meaningful downgrade on a single-user
home server: if an attacker has file read on the service user,
they already have everything. The real security boundary is "don't
commit to git" and "don't print in logs," both of which this
approach enforces.

---

## LIFX bulb labeling — separate tool

Labeling is explicitly **not** part of the installer. A fresh user
has bulbs named `LIFX Color A19` / `LIFX White 800` — factory
defaults from which no useful logical name can be derived. The
installer cannot guess what physical room any bulb is in. Instead,
the installer points the user at a walk-through labeling tool:

### `tools/label_bulbs.py`

Purpose: one-time (and occasionally re-run) interactive tool that
walks the user around the house, identifies each bulb via the
existing `device_manager.identify()` path
(`device_manager.py:1079`), and captures a user-chosen logical
label per bulb.

Flow:

- Broadcast-scan for all LIFX devices on the local subnet.
- Load `site-settings/bulbs.json`. For each discovered bulb, match
  by MAC. If an entry with a non-null `logical_label` exists, skip
  it (already labeled). Otherwise it's a candidate.
- For each candidate, send identify (existing call), then ask:

      A bulb is flashing. Is it in front of you? [y/n/skip/quit]

- On `y`, prompt for a logical label ("living room lamp", "hallway
  sconce", etc.), write it to `bulbs.json` via `SetLabel` or local
  JSON only (see open question below), and move to the next
  candidate.
- On `n`, leave the entry alone and move to the next candidate.
- On `skip`, cancel identify and move to the next candidate.
- On `quit`, write current state and exit.
- Loop until no candidates remain.
- `--all` flag ignores the skip-already-labeled rule and walks
  every bulb (for renaming rooms).

### `site-settings/bulbs.json` schema

JSON object keyed by MAC address (stable canonical identity). Each
value:

    {
      "mac":           "d0:73:d5:xx:xx:xx",
      "lifx_label":    "LIFX Color A19",      // as found on the bulb
      "logical_label": "living room lamp",    // user-assigned, or null if not yet labeled
      "ip":             "10.0.0.73"            // last-observed, may drift
    }

Definition of "already labeled": the entry exists AND
`logical_label` is a non-null, non-empty string. No regex, no
factory-prefix list, no tombstone file — the JSON itself is the
tombstone and carries the useful metadata.

The engine continues to resolve *labels → IPs* at runtime via
broadcast discovery; `ip` in `bulbs.json` is an informational
cache, not a source of truth.

Groups are still assigned via the dashboard (group CRUD already
exists per `reference_dashboard_features`), not via this tool.

### Dashboard labeling — inline, in the ARP panel

The CLI walk-through tool is the right UX for "I'm wandering through
the house with my laptop relabeling every bulb." It is NOT the right
UX for "I'm already staring at the dashboard and I can see two
unlabeled devices in the ARP table and I just want to name them
right now."

For that second case, the dashboard grows inline labeling directly
in its existing discovered-devices panel.

**Hook point (already exists):**

- `static/dashboard.html:523` — the collapsible section titled
  "Discovered Devices (ARP Keepalive)" with container
  `#discovered-bulbs`.
- Backed by `GET /api/discovered_bulbs` in `handlers/discovery.py:39`,
  which returns the keepalive daemon's live ARP view.

**What gets added per row** in the discovered-devices panel:

- An **Identify** button. Clicking it calls the existing identify
  path via REST; the bulb flashes on the user's desk. No argument
  needed beyond the bulb's IP (already known to the row).
- A **Label** control, inline-editable. Shows the current
  `logical_label` if one exists in `bulbs.json`, or a placeholder
  like `(unlabeled — click to name)` otherwise. Editing and
  confirming (Enter or blur) writes the new label to `bulbs.json`
  via a new REST endpoint.
- The row also displays the bulb's **factory LIFX label** (the
  `lifx_label` field — e.g. `LIFX Color A19`) as read-only metadata
  next to the editable logical label, so the user can tell whether
  this is a fresh-out-of-box bulb or one that's been renamed.

**New REST endpoints:**

- `POST /api/bulbs/label` — body `{"mac": "...", "logical_label": "..."}`.
  Writes to `site-settings/bulbs.json`. Creates the entry if it
  doesn't exist. Returns the updated entry.
- `POST /api/bulbs/identify` — body `{"ip": "..."}` or
  `{"mac": "..."}`. Triggers the existing identify path
  (`device_manager.identify`). Returns 200 on dispatch. (May already
  exist as a side effect of `handlers/device.py:563` — verify and
  reuse rather than duplicate.)

**Single source of truth.** Both `tools/label_bulbs.py` and the
dashboard inline labeling write to the same `site-settings/bulbs.json`
using the same schema. There is exactly one JSON file, one schema,
one set of semantics for "already labeled." The two interfaces are
just different front-ends to the same state.

**Interaction with `GET /api/discovered_bulbs`.** The existing
endpoint returns ARP-derived rows (IP, MAC, last-seen, etc.). It
should be extended to **join** against `bulbs.json` so each row
includes `logical_label` (nullable) and `lifx_label` fields. That
way the dashboard renders the current labeling state without a
second round-trip. This is a small, backwards-compatible addition:
existing consumers that ignore the new fields keep working.

**Why this is in the design doc and not a follow-up ticket:**
Perry asked for it directly on 2026-04-08 while designing the
installer, specifically because he could already see two unlabeled
devices in the ARP table on the live dashboard and wanted to name
them without leaving the page. It's part of the same labeling
story as the CLI tool.

---

## Re-run / reconfigure (v1 concession)

**v1 behavior — refuse and defer.** If `site-settings/` already
exists when the installer is run, it prints:

    GlowUp is already installed on this machine.
    To change features, use (coming soon) tools/reconfigure.py.
    To start over completely, delete site-settings/ and re-run.

...and exits. No destructive action.

**This is temporary.** A real reconfigure tool is a
**must-build follow-up**, not a nice-to-have. Tracked explicitly in
this document so it doesn't get forgotten:

- `tools/reconfigure.py` — same prompts as the installer's interview
  step, but pre-fills current values from `site-settings/local.json`
  and lets the user change any of them. Does not re-run venv setup
  or service installation. Writes updated JSON, restarts the service.

Until that exists, re-runs are manual (delete site-settings, redo
interview). Document this prominently in the summary screen and
user docs so it's not a surprise.

---

## What gets deleted from the current installer directory

When this design is implemented, the following go away:

- `installer/install.py` — rewritten wholesale. Same path, totally
  different contents.
- `installer/static/installer.js` — dead. Delete.
- `installer/static/` as a concept — dead. Delete unless we find
  another use for static assets under the installer directory (we
  won't; the dashboard has its own static tree).
- `CLI_BOM` and all sparse-checkout machinery inside `install.py`
  — gone. The new model is a full clone, there is no BOM.

New additions:

- `installer/DESIGN.md` (this file)
- `installer/templates/*.in` (systemd/launchd templates)
- `sample-site-settings/*.json` (tracked sample configs)
- `tools/label_bulbs.py` (walk-through labeling)
- `.gitignore` entries for `site-settings/` and `venv/`

---

## Open questions (resume here next session)

These need Perry's call before implementation can start. Ordered
by approximate blocking priority:

- **Python version floor.** Current `install.py` says 3.8, old spec
  said 3.10. Pi OS Bookworm ships 3.11, Bullseye 3.9. Targeting
  3.10 rules out Bullseye. Pick one.
- **Service user.** Does the service run as root, a dedicated
  `glowup` user, or the invoking user? Has security implications
  but was deferred during the service-setup discussion.
- **sample-site-settings/ concrete file shapes.** The exact JSON
  schemas for `local.json`, `hub.json`, `satellite.json`,
  `secrets.json`. Worth a strawman proposal to push back on.
- **Voice sizing thresholds.** Exact RAM numbers for "beefy" vs
  "minimum" tiers, what the bundled benchmark WAV is, what RTF
  threshold trips the warning.
- **Feature list for v1.** Is it all nine from the old spec, or
  trimmed? Which features are v1 blockers vs post-v1 follow-ups?
- **Auth token.** Generate random UUID on first install? Let user
  set? Stored in `local.json` (since it gates the dashboard, not a
  third-party credential) or in `secrets.json` (since it's a
  secret)?
- **Port default.** 8420 from the old spec — confirm. Still offered
  as a prompt?
- **Labeling — does `tools/label_bulbs.py` also call `SetLabel` on
  the bulb**, or only write to local JSON and leave the bulb's own
  label at its factory default? Arguments both ways: writing to the
  bulb persists the logical name across config loss; not writing
  keeps the bulb's native label diagnostic ("this bulb factory-ships
  as LIFX Color A19, and our JSON says the user calls it 'living
  room lamp'").
- **Partial-install failure handling.** If Step 5 succeeds but
  Step 6 (pip install) fails, do we roll back site-settings? Leave
  it and resume? Block and prompt? "Refuse and defer" on re-run
  makes resume hard — this is an interaction between those two
  design choices.
- **Where / how the copyright + license text is sourced.** Assume
  `LICENSE` in the repo root, full text printed to stdout. Confirm.

---

## Decided — do not re-litigate

These are locked in. Listed here so next session can skip them.

- Distribution model: `git clone` + `python3 installer/install.py`.
  Not `curl | python3`.
- Site-settings is a gitignored overlay copied from a tracked
  `sample-site-settings/`.
- Secrets live in `site-settings/secrets.json`, mode 0600.
- Bulb IPs are not stored in site-settings — bulbs are discovered
  at runtime by label. The labeling tool owns `bulbs.json`.
- LIFX discovery is not in the installer; `tools/label_bulbs.py`
  handles it post-install.
- Bulb "already labeled" definition: `bulbs.json` entry with
  non-null `logical_label`.
- Developer / fork framing is out of the install flow entirely.
- Multi-computer registration is passive-from-the-hub: satellites
  announce themselves via retained `glowup/registry/<hostname>`.
- Satellite installer asks for hub IP, writes it to local
  site-settings, and the satellite runtime uses that to reach the
  broker. No mDNS, no magic discovery.
- v1 re-run behavior is refuse-and-defer. A reconfigure tool is a
  must-build follow-up, not a v1 blocker — but it IS on the hook
  for post-v1.
- Installer creates, enables, and starts services. Asks once, then
  owns the files.
- Installer controls all configuration; user hand-edits unsupported.
- The old browser installer (`install.py` web UI, `static/installer.js`,
  `CLI_BOM`) is fully retired.
- Labeling has two front-ends, both writing to the same
  `site-settings/bulbs.json`: the CLI walk-through tool
  (`tools/label_bulbs.py`) for room-to-room renaming, and inline
  dashboard controls added to the existing Discovered Devices (ARP
  Keepalive) panel for quick in-place labeling from the web UI.
