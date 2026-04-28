#!/usr/bin/env bash
# GlowUp installer — see installer/DESIGN.md (v3).
#
# Phase 1: platform detect, preflight, welcome + tier confirmation,
# feature picker. No destructive work yet — runs read-only and prints
# the selection. Phases 2+ add venv creation, pip install, site-settings,
# secrets, systemd units, and self-check.
#
# Bash 3.2-compatible (macOS default bash). No associative arrays, no
# mapfile, no ${var,,}. Tested by running on Mac + Linux.

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER_DIR="$REPO_ROOT/installer"

# Feature catalog — two parallel arrays (bash 3.2, no associative arrays).
# Indices correspond 1:1. Index 0 is always-on core.
FEATURE_KEYS=(
    "core"
    "dashboard"
    "scheduler"
    "vivint"
    "nvr"
    "voice"
    "kiosk"
    "power"
    "ble"
    "matter"
    "zigbee"
    "multi"
    "maritime"
    "adsb"
)
FEATURE_LABELS=(
    "LIFX light control (core)"
    "Dashboard web UI"
    "Scheduler (sunrise/sunset, timers)"
    "Vivint security (locks, alarm, sensors)"
    "NVR camera feeds"
    "Voice control (wake word, STT, TTS)"
    "Kiosk display (Pi wallclock)"
    "Power monitoring (Zigbee smart plugs)"
    "BLE sensors (temperature, humidity, motion)"
    "Matter adapter"
    "Zigbee adapter (Z2M — requires USB Zigbee coordinator dongle)"
    "Multi-computer mode (split roles across additional hosts)"
    "Maritime (NDBC buoys + AIS vessel tracking — AIS needs RTL-SDR or aisstream API key)"
    "ADS-B aircraft tracking (requires RTL-SDR dongle at 1090 MHz)"
)

# Feature dependencies — parallel arrays of (child, parent). When child is
# selected, parent is auto-added (and surfaced to the user).
DEP_CHILD=("voice" "kiosk")
DEP_PARENT=("dashboard" "dashboard")

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

# ANSI colors — disabled if stdout isn't a terminal.
if [ -t 1 ]; then
    C_BOLD=$(printf '\033[1m')
    C_DIM=$(printf '\033[2m')
    C_RED=$(printf '\033[31m')
    C_YELLOW=$(printf '\033[33m')
    C_GREEN=$(printf '\033[32m')
    C_RESET=$(printf '\033[0m')
else
    C_BOLD=""; C_DIM=""; C_RED=""; C_YELLOW=""; C_GREEN=""; C_RESET=""
fi

info()  { printf '%s\n' "$*"; }
hdr()   { printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }
warn()  { printf '%swarning:%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()   { printf '%serror:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
ok()    { printf '%s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
die()   { err "$*"; exit 1; }

# Prompt for a yes/no answer. Usage: ask "Do the thing?" "Y"  (default)
ask() {
    local msg="$1"
    local default="${2:-Y}"
    local reply
    local hint
    if [ "$default" = "Y" ]; then hint="[Y/n]"; else hint="[y/N]"; fi
    while :; do
        printf '%s %s ' "$msg" "$hint"
        read -r reply || return 1
        if [ -z "$reply" ]; then reply="$default"; fi
        case "$reply" in
            y|Y|yes|YES) return 0 ;;
            n|N|no|NO)   return 1 ;;
            *) warn "Please answer y or n." ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Step 1 — platform detection
# ---------------------------------------------------------------------------

detect_platform() {
    local uname_s
    uname_s="$(uname -s)"
    case "$uname_s" in
        Darwin) PLATFORM="darwin" ;;
        Linux)  PLATFORM="linux" ;;
        *)
            die "unsupported OS: $uname_s. GlowUp supports macOS and Linux. Windows users: see the README for the local-only stanza."
            ;;
    esac
    ok "Platform: $PLATFORM"
}

# ---------------------------------------------------------------------------
# Step 2 — preflight
# ---------------------------------------------------------------------------

check_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 not found."
        case "$PLATFORM" in
            darwin) info "Install: brew install python@3.12  (or download from python.org)" ;;
            linux)  info "Install: sudo apt install python3 python3-venv  (Debian/Ubuntu/Pi OS)" ;;
        esac
        exit 1
    fi
    local py_version py_major py_minor
    py_version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    py_major="${py_version%%.*}"
    py_minor="${py_version##*.}"
    if [ "$py_major" -lt "$MIN_PYTHON_MAJOR" ] || \
       { [ "$py_major" -eq "$MIN_PYTHON_MAJOR" ] && [ "$py_minor" -lt "$MIN_PYTHON_MINOR" ]; }; then
        die "Python $py_version is too old; need >= $MIN_PYTHON_MAJOR.$MIN_PYTHON_MINOR."
    fi
    ok "Python: $py_version"
}

check_git() {
    if ! command -v git >/dev/null 2>&1; then
        die "git not found. You cloned this repo somehow; install git before running install.sh."
    fi
    ok "git: $(git --version | awk '{print $3}')"
}

check_systemctl() {
    if ! command -v systemctl >/dev/null 2>&1; then
        die "systemctl not found. The Linux tier requires systemd."
    fi
    ok "systemctl: present"
}

preflight() {
    hdr "Preflight"
    check_python
    check_git
    if [ "$PLATFORM" = "linux" ]; then
        check_systemctl
    fi
}

# ---------------------------------------------------------------------------
# Step 3 — welcome + tier confirmation
# ---------------------------------------------------------------------------

welcome() {
    hdr "GlowUp installer"
    info "Install root: $REPO_ROOT"
    case "$PLATFORM" in
        darwin)
            info ""
            info "Detected macOS — this is a ${C_BOLD}local-only${C_RESET} install:"
            info "  - CLI lighting effects via glowup.py"
            info "  - Voice coordinator is supported on macOS but not in default Tier A."
            info "  - No server, no dashboard, no systemd services (macOS doesn't have systemd)."
            info "For the full server install, run this on Linux."
            info ""
            ;;
        linux)
            info ""
            info "Detected Linux — this is the ${C_BOLD}full install${C_RESET}:"
            info "  - All features available; pick which ones to enable."
            info "  - Systemd units under /etc/systemd/system/ (installer will prompt for sudo)."
            info ""
            ;;
    esac
    ask "Continue?" "Y" || die "Install aborted."
}

# ---------------------------------------------------------------------------
# Step 4 — feature picker (Linux only)
# ---------------------------------------------------------------------------

# Produces SELECTED_FEATURES as a space-separated list of keys.
feature_picker() {
    if [ "$PLATFORM" != "linux" ]; then
        SELECTED_FEATURES="core"
        return
    fi

    hdr "Features"
    info "Select features (numbers, comma-separated; blank = all):"
    info ""
    local n=${#FEATURE_KEYS[@]}
    local i
    for (( i=0; i<n; i++ )); do
        if [ "$i" -eq 0 ]; then
            printf "  %2d. [always] %s\n" $((i+1)) "${FEATURE_LABELS[$i]}"
        else
            printf "  %2d. [ ]      %s\n" $((i+1)) "${FEATURE_LABELS[$i]}"
        fi
    done
    info ""

    local raw
    printf "Enter choice [2-%d, or empty for all]: " "$n"
    read -r raw || die "Read failed."

    # Empty input = all features.
    if [ -z "$raw" ]; then
        SELECTED_FEATURES="${FEATURE_KEYS[*]}"
    else
        SELECTED_FEATURES="core"
        local item
        # Normalize commas / whitespace → spaces, iterate.
        set -- $(echo "$raw" | tr ',' ' ')
        for item in "$@"; do
            case "$item" in
                ''|*[!0-9]*)
                    warn "Skipping non-numeric entry: '$item'"
                    continue ;;
            esac
            if [ "$item" -lt 1 ] || [ "$item" -gt "$n" ]; then
                warn "Skipping out-of-range entry: '$item' (valid 1..$n)"
                continue
            fi
            if [ "$item" -eq 1 ]; then
                continue  # core is always in
            fi
            local key="${FEATURE_KEYS[$((item-1))]}"
            # Dedupe: skip if already in.
            case " $SELECTED_FEATURES " in
                *" $key "*) ;;
                *) SELECTED_FEATURES="$SELECTED_FEATURES $key" ;;
            esac
        done
    fi

    # Resolve dependencies — auto-add parents, surface the addition.
    local ndeps=${#DEP_CHILD[@]}
    local di
    for (( di=0; di<ndeps; di++ )); do
        local child="${DEP_CHILD[$di]}"
        local parent="${DEP_PARENT[$di]}"
        case " $SELECTED_FEATURES " in
            *" $child "*)
                case " $SELECTED_FEATURES " in
                    *" $parent "*) ;;
                    *)
                        SELECTED_FEATURES="$SELECTED_FEATURES $parent"
                        info "→ $child requires $parent; auto-enabled."
                        ;;
                esac
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Step 5 — create venv, upgrade pip + wheel
# ---------------------------------------------------------------------------

VENV="$REPO_ROOT/venv"
SITE_SETTINGS="$REPO_ROOT/site-settings"

create_venv() {
    hdr "Virtual environment"
    if [ -d "$VENV" ]; then
        # Both python and pip must be present. A failed prior venv
        # creation can leave the directory with python but no pip.
        if { [ -x "$VENV/bin/python" ] || [ -x "$VENV/bin/python3" ]; } \
           && [ -x "$VENV/bin/pip" ]; then
            ok "reusing existing venv at $VENV"
            return 0
        fi
        warn "$VENV exists but is incomplete (missing pip); rebuilding."
        rm -rf -- "$VENV"
    fi
    info "creating $VENV …"
    if ! python3 -m venv "$VENV" 2>/tmp/venv-err.$$; then
        err "venv creation failed:"
        cat /tmp/venv-err.$$ >&2
        rm -f /tmp/venv-err.$$
        info "fix: on Debian/Ubuntu: sudo apt install python3-venv"
        exit 1
    fi
    rm -f /tmp/venv-err.$$
    # Upgrade pip + wheel in one shot; --quiet keeps the log tidy but
    # errors still print.
    if ! "$VENV/bin/pip" install --quiet --upgrade pip wheel; then
        err "pip/wheel upgrade failed inside $VENV."
        info "fix: activate the venv manually ('source $VENV/bin/activate') and run 'pip install --upgrade pip wheel' to see the underlying error."
        exit 1
    fi
    ok "venv ready: $($VENV/bin/python --version)"
}

# ---------------------------------------------------------------------------
# Step 6 — install Python dependencies
#
# Phase 2a: installs whatever's in the repo's requirements.txt for every
# tier. Per-feature dependency splitting (tier A shouldn't need psycopg,
# voice needs faster-whisper + piper, etc.) is deferred to a later pass
# once we audit what each subsystem actually imports.
# ---------------------------------------------------------------------------

install_deps() {
    hdr "Python dependencies"
    local req="$REPO_ROOT/requirements.txt"
    if [ ! -f "$req" ]; then
        warn "requirements.txt missing; skipping pip install."
        return 0
    fi
    info "pip install -r requirements.txt …"
    if ! "$VENV/bin/pip" install --quiet -r "$req" 2>/tmp/pip-err.$$; then
        err "pip install failed. Last 20 lines:"
        tail -20 /tmp/pip-err.$$ >&2
        rm -f /tmp/pip-err.$$
        info "fix: activate the venv and re-run the failing install by hand to see the full error."
        exit 1
    fi
    rm -f /tmp/pip-err.$$
    ok "base deps installed"
}

# ---------------------------------------------------------------------------
# Step 7 — write site-settings/features.json
#
# Records the user's feature selection so re-runs can diff. This is the
# first site-settings file; per-feature JSONs (server.json, satellite.json,
# etc.) come in Phase 2b when schemas are drawn up.
# ---------------------------------------------------------------------------

write_features_file() {
    hdr "Recording selection"
    mkdir -p "$SITE_SETTINGS"
    local out="$SITE_SETTINGS/features.json"
    local tmp="$out.tmp.$$"

    # Build the JSON array manually — no jq dependency required. We
    # escape nothing special here because feature keys are fixed (no
    # user input lands in this file from Phase 1).
    {
        printf '{\n'
        printf '  "version": 1,\n'
        printf '  "platform": "%s",\n' "$PLATFORM"
        printf '  "features": [\n'
        local first=1
        local k
        for k in $SELECTED_FEATURES; do
            if [ "$first" -eq 1 ]; then first=0; else printf ',\n'; fi
            printf '    "%s"' "$k"
        done
        printf '\n  ]\n'
        printf '}\n'
    } > "$tmp"
    mv -- "$tmp" "$out"
    ok "wrote $out"
}

# ---------------------------------------------------------------------------
# Step 8b (Phase 2b) — write minimum /etc/glowup/ config files
#
# Three files land in /etc/glowup/:
#   site.json    — hub broker, contact email, etc.  Read by glowup_site.py.
#   server.json  — port, auth_token.  Read by server.py.
#   secrets.json — auth_token + per-feature secret stubs.  Mode 0600.
#
# Values written here are operator-overridable post-install — the
# installer never re-edits these files on a re-run unless explicitly
# asked, so hand edits survive.  Existence-check first so we don't
# clobber a real deployment's hand-curated config.
# ---------------------------------------------------------------------------

GLOWUP_ETC="${GLOWUP_ETC:-/etc/glowup}"

# Generate a fresh URL-safe token once; reused by server.json + secrets.json.
generate_auth_token() {
    "$VENV/bin/python" -c \
        'import secrets; print(secrets.token_urlsafe(32))'
}

# Write JSON via a tmp+install so a half-write can never leave the
# program loading partial config.  Files in /etc/glowup/ are root-owned
# by convention; the service group gets read on secret-bearing files
# (server.json, secrets.json) so the unit running as ${SERVICE_USER}
# can read them.  Defaults: root:root 0644 (no secrets).
write_etc_json() {
    local target="$1" content="$2" mode="${3:-0644}" group="${4:-root}"
    local tmp
    tmp="$(mktemp)"
    printf '%s\n' "$content" > "$tmp"
    sudo install -o root -g "$group" -m "$mode" "$tmp" "$target"
    rm -f -- "$tmp"
}

# Compute the install user / group up front so /etc/glowup/ files can
# be written with correct group ownership before systemd-unit rendering
# (which also depends on these).  Idempotent.
compute_service_identity() {
    : "${SERVICE_USER:=$(id -un)}"
    : "${SERVICE_GROUP:=$(id -gn)}"
    export SERVICE_USER SERVICE_GROUP
}

write_site_config() {
    hdr "Site config"
    sudo mkdir -p "$GLOWUP_ETC"
    if [ -f "$GLOWUP_ETC/site.json" ]; then
        info "${C_DIM}$GLOWUP_ETC/site.json exists — leaving alone${C_RESET}"
        return 0
    fi
    # Minimum viable site.json — single-host install where mosquitto and
    # glowup-server live on the same box.  Operators with a multi-host
    # layout edit hub_broker post-install.
    write_etc_json "$GLOWUP_ETC/site.json" '{
  "schema_version": 1,
  "hub_broker": "localhost",
  "hub_port": 1883,
  "contact_email": "operator@example.invalid"
}'
    ok "wrote $GLOWUP_ETC/site.json"
}

write_server_config() {
    if [ -f "$GLOWUP_ETC/server.json" ]; then
        info "${C_DIM}$GLOWUP_ETC/server.json exists — leaving alone${C_RESET}"
        # Pull existing token so secrets.json (also leave-alone-if-exists)
        # would match if it had to be regenerated.
        AUTH_TOKEN="$(sudo "$VENV/bin/python" -c \
            'import json,sys; print(json.load(open("'"$GLOWUP_ETC/server.json"'"))["auth_token"])' 2>/dev/null || echo)"
        return 0
    fi
    AUTH_TOKEN="$(generate_auth_token)"
    # The server refuses to start with an empty 'groups' section, so we
    # ship a placeholder pointing at RFC 5737 TEST-NET-1.  The bulb is
    # never reachable; the operator edits this section after install
    # to list real devices.  See deploy/server.json.example for shape.
    write_etc_json "$GLOWUP_ETC/server.json" "{
  \"port\": 8420,
  \"auth_token\": \"$AUTH_TOKEN\",
  \"groups\": {
    \"_comment\": \"PLACEHOLDER — edit to list real LIFX devices by IP, MAC, or label\",
    \"placeholder\": [\"192.0.2.1\"]
  }
}" 0640 "$SERVICE_GROUP"
    ok "wrote $GLOWUP_ETC/server.json (auth_token generated)"
}

# Read a value from stdin with a labelled prompt and an optional
# explanation hint.  Hidden mode (no echo) for passwords.  Pressing
# enter on a blank line accepts the empty string — operators who want
# to skip a credential and edit secrets.json by hand later can do so.
prompt_secret() {
    local label="$1" hint="$2" hidden="${3:-no}" var
    info "  ${C_DIM}$hint${C_RESET}"
    if [ "$hidden" = "yes" ]; then
        printf "  %s: " "$label"
        # -s suppresses echo; explicit newline after for readability.
        read -rs var
        printf "\n"
    else
        printf "  %s: " "$label"
        read -r var
    fi
    printf '%s' "$var"
}

# JSON-escape a string value for direct embedding in a JSON literal.
# Handles \, ", and control characters; passes UTF-8 through.
json_escape() {
    "$VENV/bin/python" -c \
        'import json, sys; sys.stdout.write(json.dumps(sys.stdin.read()))' \
        <<< "$1"
}

write_secrets_file() {
    if [ -f "$GLOWUP_ETC/secrets.json" ]; then
        info "${C_DIM}$GLOWUP_ETC/secrets.json exists — leaving alone${C_RESET}"
        return 0
    fi
    hdr "Secrets"
    # Non-interactive (piped, CI, automated VM test) — skip the prompts
    # and write stub creds so the file exists with the right schema.
    # Operator edits secrets.json after install or re-runs interactively.
    if [ ! -t 0 ]; then
        info "${C_DIM}stdin is not a tty — writing stubs.  Edit $GLOWUP_ETC/secrets.json after install.${C_RESET}"
        local stubs=""
        case " $SELECTED_FEATURES " in
            *" vivint "*) stubs="$stubs,
  \"vivint\": {\"username\": \"\", \"password\": \"\"}" ;;
        esac
        case " $SELECTED_FEATURES " in
            *" nvr "*) stubs="$stubs,
  \"nvr\": {\"username\": \"\", \"password\": \"\"}" ;;
        esac
        case " $SELECTED_FEATURES " in
            *" matter "*) stubs="$stubs,
  \"matter\": {\"fabric_id\": \"\", \"setup_code\": \"\"}" ;;
        esac
        write_etc_json "$GLOWUP_ETC/secrets.json" "{
  \"glowup_auth_token\": \"$AUTH_TOKEN\"$stubs
}" 0640 "$SERVICE_GROUP"
        ok "wrote $GLOWUP_ETC/secrets.json (stubs; root:$SERVICE_GROUP, 0640)"
        return 0
    fi
    info "Enter credentials for each enabled feature.  Press Enter to leave blank;"
    info "you can edit $GLOWUP_ETC/secrets.json post-install (mode 0640) at any time."
    info ""

    local stubs=""

    case " $SELECTED_FEATURES " in
        *" vivint "*)
            info "${C_BOLD}Vivint${C_RESET}"
            local v_user v_pass
            v_user="$(prompt_secret "username" "your Vivint SkyControl panel login" no)"
            v_pass="$(prompt_secret "password" "Vivint password (no-echo)" yes)"
            stubs="$stubs,
  \"vivint\": {\"username\": $(json_escape "$v_user"), \"password\": $(json_escape "$v_pass")}"
            info ""
            ;;
    esac

    case " $SELECTED_FEATURES " in
        *" nvr "*)
            info "${C_BOLD}NVR${C_RESET}"
            local n_user n_pass
            n_user="$(prompt_secret "username" "admin user you set when configuring the NVR" no)"
            n_pass="$(prompt_secret "password" "NVR admin password (no-echo)" yes)"
            stubs="$stubs,
  \"nvr\": {\"username\": $(json_escape "$n_user"), \"password\": $(json_escape "$n_pass")}"
            info ""
            ;;
    esac

    case " $SELECTED_FEATURES " in
        *" matter "*)
            info "${C_BOLD}Matter${C_RESET}"
            info "  ${C_DIM}fabric_id and setup_code are produced by python-matter-server"
            info "  on first device pairing — leave blank now and edit later, or paste"
            info "  values you already have.${C_RESET}"
            local m_fabric m_setup
            m_fabric="$(prompt_secret "fabric_id" "Matter fabric identifier (or blank)" no)"
            m_setup="$(prompt_secret "setup_code" "Matter pairing setup code (or blank)" yes)"
            stubs="$stubs,
  \"matter\": {\"fabric_id\": $(json_escape "$m_fabric"), \"setup_code\": $(json_escape "$m_setup")}"
            info ""
            ;;
    esac

    write_etc_json "$GLOWUP_ETC/secrets.json" "{
  \"glowup_auth_token\": \"$AUTH_TOKEN\"$stubs
}" 0640 "$SERVICE_GROUP"
    ok "wrote $GLOWUP_ETC/secrets.json (root:$SERVICE_GROUP, 0640)"
}

# ---------------------------------------------------------------------------
# Step 11 (Phase 2b) — enable + start units, then self-check
#
# Tries to enable + start every rendered unit.  Failures are EXPECTED on
# a VM with no hardware (BLE / SDR / Zigbee services have no radios to
# attach to) — captured as ok/fail rather than aborting.  A summary
# table prints at the end so the operator sees exactly which features
# came up and which need attention.
# ---------------------------------------------------------------------------

# Auto-start tier policy:
#   Tier A (this list, computed at install time)  — services that are
#   safe to start on a fresh box because they are config-driven and have
#   no external prerequisite (no hardware dongle, no operator-supplied
#   secret, no paired peer).  Composition:
#     - unconditional core: server + keepalive
#     - platform-detected:  pi-thermal (Raspberry Pi) or glowup-x86-thermal
#                           (other Linux) — picks one by hardware
#     - feature-conditional:
#         kiosk   → kiosk-health + clock-server
#         maritime → glowup-buoys (NDBC HTTPS scraper, no radio needed)
#
#   Tier B (every other template the operator picked — handled by
#   write_post_install_todo) — services that crash-loop without a
#   paired Matter device, an SDR dongle, an aisstream API key, or
#   similar.  Operator gets a per-feature checklist with the concrete
#   ``systemctl enable --now <unit>`` line and an example config snippet.

is_raspberry_pi() {
    grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null && return 0
    grep -qi "raspberry pi" /proc/cpuinfo 2>/dev/null
}

# Populates the AUTO_START_UNITS array based on PLATFORM and
# SELECTED_FEATURES.  Must run after feature_picker, before
# enable_and_start_units.
compute_auto_start_units() {
    AUTO_START_UNITS=(
        "glowup-server.service"
        "glowup-keepalive.service"
    )

    # Platform thermal — both templates ship; only one auto-starts.
    if is_raspberry_pi; then
        AUTO_START_UNITS+=("pi-thermal.service")
    else
        AUTO_START_UNITS+=("glowup-x86-thermal.service")
    fi

    # Feature-conditional Tier A additions.
    #
    # NOTE: ``kiosk`` is intentionally NOT here.  The kiosk-related
    # units (kiosk, kiosk-health, clock-server, clock-display)
    # depend on filesystem layouts that only exist on a dedicated
    # kiosk display host (``~/clock``, ``/opt/glowup-sensors``).
    # Auto-starting them when the operator picks ``kiosk`` on a
    # non-display host (e.g., the dashboard server) crashes
    # them in a Restart=on-failure flapping loop.  They live in
    # Tier B; the kiosk block in write_post_install_todo carries
    # the concrete enable lines for an actual kiosk Pi.
    case " $SELECTED_FEATURES " in
        *" maritime "*)
            AUTO_START_UNITS+=("glowup-buoys.service")
            ;;
    esac
}

enable_and_start_units() {
    [ "$PLATFORM" = "linux" ] || return 0
    compute_auto_start_units
    hdr "Starting services"
    local unit ok_units="" fail_units=""
    for unit in "${AUTO_START_UNITS[@]}"; do
        if [ ! -f "/etc/systemd/system/$unit" ]; then
            continue
        fi
        if sudo systemctl enable --now "$unit" >/dev/null 2>&1; then
            ok "$unit"
            ok_units="$ok_units $unit"
        else
            warn "$unit — see: sudo journalctl -u $unit"
            fail_units="$fail_units $unit"
        fi
    done
    AUTO_STARTED_OK="$ok_units"
    AUTO_STARTED_FAIL="$fail_units"
}

self_check() {
    [ "$PLATFORM" = "linux" ] || return 0
    [ -n "${AUTH_TOKEN:-}" ] || return 0
    case " $AUTO_STARTED_OK " in *" glowup-server.service "*) ;; *) return 0 ;; esac
    hdr "Self-check"
    local code
    # Five-second wait for the server's HTTP listener to bind — a fresh
    # process needs a moment between systemctl-start and accept().
    local i=0
    while [ "$i" -lt 5 ]; do
        code="$(curl -s -o /dev/null -w '%{http_code}' \
            -H "X-Auth-Token: $AUTH_TOKEN" \
            "http://127.0.0.1:8420/api/home/health" 2>/dev/null || echo)"
        if [ "$code" = "200" ]; then
            ok "/api/home/health → 200"
            return 0
        fi
        sleep 1
        i=$((i+1))
    done
    warn "/api/home/health → ${code:-unreachable} after 5s"
}

# ---------------------------------------------------------------------------
# Step 11b — post-install TODO file
#
# Tier B services (anything that needs an SDR dongle, a paired Matter
# device, an aisstream API key, a TOTP secret, etc.) deliberately did
# not auto-start.  Build a per-feature checklist with concrete config
# examples + the exact ``systemctl enable --now`` line, print it to
# the terminal at install end, AND persist it to /etc/glowup/
# POST_INSTALL_TODO.md so the operator can come back to it.
#
# Each block is one or more services that share a gating constraint;
# the block lists what's needed (with example values) and the start
# command.  Keep each block tight — five to ten lines each.
# ---------------------------------------------------------------------------

# Append a Tier B block for a feature when it's selected.  All output
# goes to stdout; caller redirects to file + tee for terminal echo.
_emit_todo_block_if_selected() {
    local feature="$1"
    case " $SELECTED_FEATURES " in
        *" $feature "*) ;;
        *) return 0 ;;
    esac

    case "$feature" in
        vivint)
            cat <<'EOF'

### vivint — Vivint security (locks, alarm, sensors)

Username + password are already in /etc/glowup/secrets.json (you
entered them during install).  The adapter also needs a TOTP secret
because Vivint requires MFA.  Pull the base32 secret off your TOTP
authenticator app's QR code and add it:

    "vivint": {
        "username": "you@example.com",
        "password": "<set-during-install>",
        "totp_secret": "JBSWY3DPEHPK3PXP"
    }

Then:

    sudo systemctl enable --now glowup-adapter@vivint
    sudo systemctl status glowup-adapter@vivint
EOF
            ;;
        nvr)
            cat <<'EOF'

### nvr — Reolink NVR camera feeds

Username + password are already in /etc/glowup/secrets.json.  The
adapter also needs the NVR's host/IP — add the field:

    "nvr": {
        "host": "10.0.0.50",
        "username": "<set-during-install>",
        "password": "<set-during-install>"
    }

Then:

    sudo systemctl enable --now glowup-adapter@nvr
EOF
            ;;
        voice)
            cat <<'EOF'

### voice — wake word + STT + TTS

Voice has two roles, often on different hosts:

  glowup-coordinator   Central STT+TTS+intent dispatch.  ONE per
                       household; typically lives on a beefy Linux
                       host or a Mac (on macOS this is a launchd
                       daemon, not a systemd unit — see
                       glowup-infra/services/launchd/).
  glowup-satellite     Per-room wake-word listener + utterance
                       capture.  ONE per room.  Reads
                       ~/satellite_config.json.

Both share configuration shape; the satellite's config goes at
~/satellite_config.json (NOT /etc/glowup/) for the SERVICE_USER:

    {
        "room": "Living Room",
        "wake_word": "hey_glowup",
        "wake_word_model": "/home/a/models/hey_glowup.onnx",
        "broker": {"host": "10.0.0.214", "port": 1883},
        "audio": {
            "alsa_capture_device": "plughw:CARD=Mic,DEV=0",
            "alsa_playback_device": "plughw:CARD=Speaker,DEV=0"
        }
    }

Then, depending on which role this host plays:

    sudo systemctl enable --now glowup-coordinator   # central role
    sudo systemctl enable --now glowup-satellite     # per-room role
EOF
            ;;
        kiosk)
            cat <<'EOF'

### kiosk — wallclock display

The kiosk units (kiosk, kiosk-health, clock-server, clock-display)
all assume the host IS a dedicated kiosk display Pi — they expect
``~/clock`` to exist (clock-server's WorkingDirectory) and
``/opt/glowup-sensors/kiosk_health_sensor.py`` to be installed
(kiosk-health's ExecStart).  None auto-start, because the
``kiosk`` picker entry is a feature flag, not a host-role
assertion.

If THIS host is a kiosk display, attach the HDMI monitor first,
make sure the X session comes up at boot, then on the kiosk-host:

    # Persistent user-mode systemd (so kiosk + kiosk-health can run
    # under the SERVICE_USER without a login session):
    sudo loginctl enable-linger ${SERVICE_USER}

    # System units for the clock backend + the on-screen display:
    sudo systemctl enable --now clock-server clock-display

    # User-mode units for the kiosk app + its watchdog:
    systemctl --user enable --now kiosk kiosk-health
EOF
            ;;
        power)
            cat <<'EOF'

### power — Zigbee smart-plug power monitoring

Needs a USB Zigbee coordinator dongle (SONOFF Zigbee 3.0 USB
Dongle Plus is the tested one).  Plug it in, confirm it
enumerates as /dev/ttyACM0 or /dev/serial/by-id/usb-Itead*,
edit /opt/zigbee2mqtt/data/configuration.yaml so the serial.port
matches, then pair your plugs through the zigbee2mqtt frontend.

    sudo systemctl enable --now zigbee2mqtt
    sudo systemctl enable --now glowup-zigbee-service
EOF
            ;;
        ble)
            cat <<'EOF'

### ble — BLE temperature/humidity/motion sensors

Needs a usable Bluetooth adapter (the Pi's built-in radio works;
a USB BT500 adapter works better at distance).  Pair your ONVIS
or compatible sensors first via:

    sudo systemctl enable --now ble-sniffer
    sudo systemctl enable --now glowup-ble-sensor

The sniffer surfaces nearby BLE advertisements on
glowup/ble/advert; pair sensors get a friendly name in
/etc/glowup/secrets.json under the "ble_pairings" key (the
sensor service writes them in as it observes them).
EOF
            ;;
        matter)
            cat <<'EOF'

### matter — Matter adapter

You need to pair Matter devices through python-matter-server
before the matter adapter has anything to talk to.  Start the
matter-server itself first, pair via the matter-server CLI, then
enable the adapter:

    sudo systemctl enable --now glowup-matter-server
    # ... pair your devices using python-matter-server's CLI ...
    sudo systemctl enable --now glowup-adapter@matter

The fabric_id + setup_code in /etc/glowup/secrets.json fill in
once pairing succeeds — leave them blank during install if you
haven't paired anything yet.
EOF
            ;;
        zigbee)
            cat <<'EOF'

### zigbee — Zigbee adapter (Z2M)

Same hardware setup as the "power" feature: SONOFF Zigbee 3.0
USB Dongle Plus.  After it enumerates and configuration.yaml's
serial.port matches:

    sudo systemctl enable --now zigbee2mqtt
    sudo systemctl enable --now glowup-zigbee-service
EOF
            ;;
        multi)
            cat <<'EOF'

### multi — distributed-compute worker mode

Multi-host mode requires worker-side credentials and a coordinator
URL.  Edit /etc/glowup/agent.json to point at the primary host:

    {
        "coordinator_url": "http://10.0.0.214:8420",
        "agent_id": "this-host-short-name",
        "auth_token": "<paste from primary's /etc/glowup/server.json>"
    }

Then:

    sudo systemctl enable --now glowup-agent
EOF
            ;;
        maritime)
            cat <<'EOF'

### maritime — NDBC buoys + AIS vessel tracking

NDBC buoy scraping (glowup-buoys) is auto-started — buoy data
should appear at /maritime within five minutes.

For live AIS vessel tracking, you have two paths (use either or
both):

  (a) Local RTL-SDR via AIS-catcher (best fidelity, requires
      hardware).  Install AIS-catcher (see lifx/maritime/README),
      drop the station UUID into /etc/glowup/maritime.conf:

          AISCATCHER_UUID=00000000-0000-0000-0000-000000000000

      Then:

          sudo systemctl enable --now glowup-maritime

  (b) aisstream.io WebSocket bridge (no hardware, free tier
      available at https://aisstream.io).  Drop the API key into
      /etc/glowup/aisstream.conf:

          AISSTREAM_API_KEY=<your-key>

      Then:

          sudo systemctl enable --now glowup-aisstream-bridge
EOF
            ;;
        adsb)
            cat <<'EOF'

### adsb — ADS-B aircraft tracking

Needs a 1090 MHz RTL-SDR dongle (an R820T2-based generic dongle
works; FlightAware Pro Stick is the upgrade path).  The wiedehopf
fork of readsb is built from source as part of this install and
serves /run/readsb/aircraft.json; the glowup-adsb publisher
forwards it onto MQTT for the /air dashboard.

After the dongle is plugged in:

    sudo systemctl enable --now readsb
    sudo systemctl enable --now glowup-adsb

Aircraft should appear at /air within ~30 s.
EOF
            ;;
    esac
}

write_post_install_todo() {
    [ "$PLATFORM" = "linux" ] || return 0

    local todo_path="$GLOWUP_ETC/POST_INSTALL_TODO.md"
    local now
    now="$(date '+%Y-%m-%d %H:%M %Z')"

    # Build the file in /tmp first, then sudo install — keeps the
    # write atomic and avoids a partial file on interrupt.
    local tmp
    tmp="$(mktemp -t glowup-post-install.XXXXXX)"

    {
        cat <<EOF
# GlowUp post-install TODO

Generated by install.sh at $now.
Selected features: $SELECTED_FEATURES

## What's running already

These services started automatically — they have no external
prerequisite (no hardware dongle, no operator-supplied secret):

EOF
        local u
        for u in "${AUTO_START_UNITS[@]}"; do
            printf "  - %s\n" "$u"
        done

        cat <<'EOF'

## Tier B services — start when ready

Each block below covers one selected feature whose service did
NOT auto-start because it needs hardware, a paired peer, or a
secret you'll add yourself.  Configs are concrete examples —
adjust the values, not the shape.
EOF

        # Order mirrors FEATURE_KEYS so the file's per-feature
        # sections appear in the same order as the picker menu.
        _emit_todo_block_if_selected "vivint"
        _emit_todo_block_if_selected "nvr"
        _emit_todo_block_if_selected "voice"
        _emit_todo_block_if_selected "kiosk"
        _emit_todo_block_if_selected "power"
        _emit_todo_block_if_selected "ble"
        _emit_todo_block_if_selected "matter"
        _emit_todo_block_if_selected "zigbee"
        _emit_todo_block_if_selected "multi"
        _emit_todo_block_if_selected "maritime"
        _emit_todo_block_if_selected "adsb"

        cat <<EOF

---

This file lives at $todo_path.  Re-run install.sh to regenerate
it (the installer overwrites it each run); manual edits will be
lost.  For the per-service classification rationale (why these
are Tier B and not auto-started) see installer/DESIGN.md.
EOF
    } > "$tmp"

    sudo install -o root -g "$SERVICE_GROUP" -m 0644 "$tmp" "$todo_path"
    rm -f "$tmp"

    hdr "Post-install TODO"
    info "Wrote $todo_path"
    info ""
    cat "$todo_path"
}

# ---------------------------------------------------------------------------
# Step 10 (Phase 2b) — render systemd unit templates
#
# Each .service file we install starts as installer/systemd/<unit>.template
# with ${VAR} placeholders.  render_template substitutes from the current
# environment using a tiny Python heredoc (Python is already a hard
# dependency, sed gets ugly with values that contain its delimiters).
#
# Variable map for Phase 2b:
#   ${SERVICE_USER}     — user the unit runs as
#   ${INSTALL_ROOT}     — repo checkout (e.g., /home/a/lifx)
#   ${VENV}             — Python venv (e.g., /home/a/venv)
#   ${SITE_CONFIG_DIR}  — site config root (default /etc/glowup)
#
# Currently scoped to one proven template (glowup-server.service); the
# remaining .service files in the repo will get .template companions in
# follow-up commits before Phase 2b is complete.
# ---------------------------------------------------------------------------

SYSTEMD_TEMPLATES=(
    "glowup-server.service"
    "glowup-keepalive.service"
    "glowup-agent.service"
    "glowup-adapter@.service"
    "glowup-coordinator.service"
    "glowup-matter-server.service"
    "glowup-aisstream-bridge.service"

    "glowup-ble-sensor.service"
    "broker-2-glowup-ble-sensor.service"
    "ble-sniffer.service"

    "glowup-buoys.service"
    "glowup-maritime.service"
    "glowup-meters.service"

    "glowup-adsb.service"
    "glowup-sdr.service"
    "glowup-x86-thermal.service"

    "pi-thermal.service"
    "legacy-pi-thermal.service"
    "kiosk-health.service"

    "zigbee2mqtt.service"
    "glowup-zigbee-service.service"

    "clock-display.service"
    "clock-server.service"

    "glowup-remote-hid.service"

    "glowup-satellite.service"
)

# Closed whitelist of placeholder names recognised by render_template.
# Any other ${VAR} occurrence in a template is left literal — that's how
# systemd EnvironmentFile-resolved variables (${AISCATCHER_UUID}) and
# shell-expanded variables in /bin/sh -c invocations (${CHROMIUM_BIN})
# pass through to the rendered unit unchanged.
TEMPLATE_VARS=(
    "SERVICE_USER" "SERVICE_GROUP"
    "INSTALL_ROOT" "VENV" "SITE_CONFIG_DIR"
    "AGENT_VENV" "CLOCK_ROOT"
    "SDR_ROOT" "SENSORS_ROOT" "REMOTE_HID_ROOT"
    "ZIGBEE_ROOT" "ZIGBEE2MQTT_ROOT" "ERNIE_ROOT"
)

# Render one template file by substituting only whitelisted ${VAR}
# occurrences against the current environment.  Fails loud (exit 1) on
# any *whitelisted* placeholder that isn't set — emitting a unit file
# with literal ${FOO} text for our own placeholders would only surface
# as a confusing systemd ExecStart error later.  Non-whitelisted ${VAR}
# tokens pass through unchanged for systemd / shell to resolve.
# Marker installed at the top of every unit install.sh renders.  The
# stale-unit cleanup uses this to decide what it owns: any unit file
# carrying this marker is fair game for cleanup; any unit lacking it
# (operator-installed, glowup-infra-deployed, OS-shipped, etc.) is
# left strictly alone, even if its name matches the glowup-* family.
LIFX_INSTALLER_MARKER="# X-Managed-By: lifx-installer"

render_template() {
    local tpl="$1" out="$2"
    LIFX_INSTALLER_MARKER="$LIFX_INSTALLER_MARKER" \
    TEMPLATE_VARS_CSV="$(IFS=,; echo "${TEMPLATE_VARS[*]}")" \
    python3 - "$tpl" "$out" <<'PY'
import os, re, sys
src, dst = sys.argv[1], sys.argv[2]
allowed = set(os.environ["TEMPLATE_VARS_CSV"].split(","))
marker = os.environ["LIFX_INSTALLER_MARKER"]
with open(src) as f:
    content = f.read()
def sub(m):
    var = m.group(1)
    if var not in allowed:
        return m.group(0)  # leave non-whitelisted tokens literal
    val = os.environ.get(var)
    if val is None:
        sys.stderr.write(
            "render_template: required variable ${%s} not set\n" % var
        )
        sys.exit(1)
    return val
content = re.sub(r'\$\{([A-Z_][A-Z0-9_]*)\}', sub, content)
# Prepend the ownership marker as the first line so the cleanup pass
# can identify install.sh-rendered units with a single grep.  The
# marker is a systemd comment (lines starting with # are ignored), so
# it has zero effect on unit semantics.
content = marker + "\n" + content
with open(dst, 'w') as f:
    f.write(content)
PY
}

# Render every template in SYSTEMD_TEMPLATES into site-settings/rendered-units/.
# Linux only (macOS uses launchd plists, handled separately).  Does not yet
# copy into /etc/systemd/system/ or daemon-reload — that lands once the full
# template set is converted.
# Remove install.sh-marked units on disk whose template no longer
# exists.  Runs AFTER install_systemd_units (so the new templates are
# landed first and a failed render can't accidentally take down
# working units) and BEFORE enable_and_start_units (so a stale one
# we're about to remove isn't first kicked into running state).
#
# Authority is via the LIFX_INSTALLER_MARKER comment that
# render_template prepends to every unit install.sh produces.  A unit
# carrying the marker is install.sh's to manage; one without the
# marker (operator-installed, glowup-infra-deployed, OS-shipped,
# etc.) is left strictly alone — even if its name matches the
# glowup-* pattern.  This lets glowup-infra cohabit /etc/systemd/
# system/ with services like glowup-morning-report.service without
# install.sh ever taking authority over them.
#
# Template-instance handling: a marked unit named
# ``glowup-adapter@vivint.service`` legitimately matches the
# ``glowup-adapter@.service`` template in SYSTEMD_TEMPLATES, so it is
# NOT stale.  We collapse the instance argument before checking.
cleanup_stale_units() {
    [ "$PLATFORM" = "linux" ] || return 0

    local owned_set="" tpl
    for tpl in "${SYSTEMD_TEMPLATES[@]}"; do
        owned_set="$owned_set $tpl"
    done

    local stale=() found base instance_template
    for found in /etc/systemd/system/*.service; do
        [ -e "$found" ] || continue

        # Honor the ownership marker.  Anything without it is not
        # ours; never touch.
        if ! head -1 "$found" 2>/dev/null | grep -qF "$LIFX_INSTALLER_MARKER"; then
            continue
        fi

        base="${found##*/}"

        case " $owned_set " in
            *" $base "*) continue ;;
        esac

        instance_template="$(printf '%s' "$base" \
            | sed -E 's/^(.*@)[^.]+(\.service)$/\1\2/')"
        if [ "$instance_template" != "$base" ]; then
            case " $owned_set " in
                *" $instance_template "*) continue ;;
            esac
        fi

        stale+=("$base")
    done

    if [ "${#stale[@]}" -eq 0 ]; then
        return 0
    fi

    hdr "Stale unit cleanup"
    info "Marked units below have no template in this install — removing:"
    local unit
    for unit in "${stale[@]}"; do
        info "  - $unit"
        sudo systemctl disable --now "$unit" >/dev/null 2>&1 || true
        sudo rm -f "/etc/systemd/system/$unit"
    done
    sudo systemctl daemon-reload >/dev/null 2>&1 || true
    ok "removed ${#stale[@]} stale unit(s)"
}

install_systemd_units() {
    [ "$PLATFORM" = "linux" ] || return 0
    hdr "Rendering systemd units"

    # SERVICE_USER / SERVICE_GROUP set earlier by compute_service_identity().
    : "${INSTALL_ROOT:=$REPO_ROOT}"
    : "${SITE_CONFIG_DIR:=/etc/glowup}"

    # Subsystem-specific roots — sensible fleet defaults.  Operators on
    # non-standard layouts can override by exporting these before
    # invoking install.sh.  See installer/systemd/README.md for the
    # full table.
    : "${AGENT_VENV:=$HOME/aeye_env}"
    : "${CLOCK_ROOT:=$HOME/clock}"
    : "${SDR_ROOT:=/opt/glowup-sdr}"
    : "${SENSORS_ROOT:=/opt/glowup-sensors}"
    : "${REMOTE_HID_ROOT:=/opt/glowup-remote-hid}"
    : "${ZIGBEE_ROOT:=/opt/glowup-zigbee}"
    : "${ZIGBEE2MQTT_ROOT:=/opt/zigbee2mqtt}"
    : "${ERNIE_ROOT:=/opt/ernie}"

    export SERVICE_USER SERVICE_GROUP INSTALL_ROOT VENV SITE_CONFIG_DIR \
           AGENT_VENV CLOCK_ROOT SDR_ROOT SENSORS_ROOT REMOTE_HID_ROOT \
           ZIGBEE_ROOT ZIGBEE2MQTT_ROOT ERNIE_ROOT

    local tpl_dir="$INSTALLER_DIR/systemd"
    local stage_dir="$REPO_ROOT/site-settings/rendered-units"
    mkdir -p "$stage_dir"

    local unit src dst
    for unit in "${SYSTEMD_TEMPLATES[@]}"; do
        src="$tpl_dir/$unit.template"
        dst="$stage_dir/$unit"
        if [ ! -f "$src" ]; then
            warn "missing template: $src — skipping"
            continue
        fi
        render_template "$src" "$dst"
        ok "rendered $unit"
    done

    info ""
    info "${C_DIM}Staged at $stage_dir.${C_RESET}"

    # Step 10b — sudo install rendered units into /etc/systemd/system/.
    # Skipped if the staging directory ended up empty (every template
    # missing) or if there's nothing for sudo to do.
    if [ -z "$(ls -A "$stage_dir" 2>/dev/null)" ]; then
        warn "no rendered units to install"
        return 0
    fi
    info ""
    info "Installing units to /etc/systemd/system/ (sudo)"
    if ! sudo install -o root -g root -m 0644 \
            "$stage_dir"/*.service /etc/systemd/system/; then
        die "failed to install rendered units to /etc/systemd/system/"
    fi
    ok "units installed"
    if ! sudo systemctl daemon-reload; then
        die "systemctl daemon-reload failed"
    fi
    ok "daemon-reload done"
}

# ---------------------------------------------------------------------------
# Step 8 — selection summary
# ---------------------------------------------------------------------------

summary() {
    hdr "Selection"
    local key label_idx
    for key in $SELECTED_FEATURES; do
        local i=0
        local n=${#FEATURE_KEYS[@]}
        while [ "$i" -lt "$n" ]; do
            if [ "${FEATURE_KEYS[$i]}" = "$key" ]; then
                ok "${FEATURE_LABELS[$i]}"
                break
            fi
            i=$((i+1))
        done
    done
    info ""
    info ""
    info "venv: $VENV"
    info "config: $SITE_SETTINGS/features.json"
    if [ "$PLATFORM" = "linux" ] && [ -n "${AUTO_STARTED_OK:-}" ]; then
        info ""
        info "Started: $AUTO_STARTED_OK"
        if [ -n "${AUTO_STARTED_FAIL:-}" ]; then
            info "Failed:  $AUTO_STARTED_FAIL"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Nuke It — return the install tree to a virgin state for testing.
#
# Removes: venv/, site-settings/, systemd glowup-* units (Linux), runtime
# state files (state.db*, DEPLOYED, ble_pairing.json). Leaves the repo
# clone itself (install.sh, installer/, source code) untouched so a
# follow-up `./install.sh` can reinstall.
# ---------------------------------------------------------------------------

# Runtime state files written by the server/adapters, not the installer
# itself, but which must be removed to reach a truly virgin state.
RUNTIME_STATE_FILES=(
    "state.db"
    "state.db-wal"
    "state.db-shm"
    "state.db-journal"
    "DEPLOYED"
    "ble_pairing.json"
    "shopping.json"
)

# Subset of RUNTIME_STATE_FILES preserved when --keep-state is passed to
# --nuke. These are expensive to regenerate (state.db = months of history;
# ble_pairing.json = every BLE sensor would need re-pairing; shopping.json
# = user's list) so keeping them across a reinstall is the common case.
# DEPLOYED is a deploy marker, not user data — always removed.
KEEP_STATE_PRESERVE=(
    "state.db"
    "state.db-wal"
    "state.db-shm"
    "state.db-journal"
    "ble_pairing.json"
    "shopping.json"
)

# Remove a file if it exists; report concisely either way. Silent when
# the file is absent (nothing to nuke).
nuke_file() {
    local target="$1"
    if [ -e "$target" ]; then
        rm -rf -- "$target"
        ok "removed $target"
    fi
}

# Stop + disable + remove every glowup-* systemd unit. Handles instance
# units (glowup-adapter@*) via pattern, timers as well as services.
nuke_systemd_units() {
    [ "$PLATFORM" = "linux" ] || return 0
    # systemctl returns non-zero when no unit files match the glob; with
    # pipefail that would kill the script. `|| true` swallows it so the
    # empty-result branch can handle the no-op case cleanly.
    local units
    units="$(systemctl list-unit-files --no-legend --no-pager \
                'glowup-*' 2>/dev/null | awk '{print $1}' || true)"
    if [ -z "$units" ]; then
        return 0
    fi

    info "Found $(echo "$units" | wc -l | tr -d ' ') glowup unit(s) to remove."

    # Stop running instances first. Same no-match-returns-nonzero dodge.
    local running
    running="$(systemctl list-units --no-legend --no-pager --state=active \
                'glowup-*' 2>/dev/null | awk '{print $1}' || true)"
    local u
    for u in $running; do
        if sudo systemctl stop "$u" 2>/tmp/nuke-err.$$; then
            ok "stopped $u"
        else
            warn "stop $u failed: $(cat /tmp/nuke-err.$$). Continuing."
        fi
    done

    # Disable + delete unit files.
    for u in $units; do
        sudo systemctl disable "$u" >/dev/null 2>/tmp/nuke-err.$$ || \
            warn "disable $u failed: $(cat /tmp/nuke-err.$$)"
        local path="/etc/systemd/system/$u"
        if [ -e "$path" ]; then
            sudo rm -f -- "$path"
            ok "removed $path"
        fi
    done

    sudo systemctl daemon-reload
    sudo systemctl reset-failed 2>/dev/null || true
    rm -f /tmp/nuke-err.$$
}

nuke() {
    local keep_state=0
    if [ "${1:-}" = "--keep-state" ]; then
        keep_state=1
    fi

    hdr "Nuke It"
    info "This will delete:"
    info "  - $REPO_ROOT/venv/"
    info "  - $REPO_ROOT/site-settings/   (includes secrets.json)"
    if [ "$keep_state" -eq 1 ]; then
        info "  - $REPO_ROOT/DEPLOYED   (deploy marker)"
        info ""
        info "${C_BOLD}Preserved${C_RESET} (because --keep-state):"
        info "  - state.db*             (server history)"
        info "  - ble_pairing.json      (BLE sensor keys)"
        info "  - shopping.json         (user's shopping list)"
    else
        info "  - $REPO_ROOT/{${RUNTIME_STATE_FILES[*]}}"
    fi
    if [ "$PLATFORM" = "linux" ]; then
        info "  - /etc/systemd/system/glowup-*   (stopped + disabled first)"
    fi
    info ""
    info "The git repo itself ($REPO_ROOT) stays — you can reinstall"
    info "immediately after by re-running ./install.sh."
    info ""
    ask "Continue?" "N" || die "Nuke aborted."

    local any_found=0

    # Systemd units (Linux only). Report count before/after so 'nothing
    # to do' is visible to the user, not a silent no-op.
    if [ "$PLATFORM" = "linux" ]; then
        nuke_systemd_units
    fi

    # venv + site-settings.
    if [ -e "$REPO_ROOT/venv" ]; then
        rm -rf -- "$REPO_ROOT/venv"
        ok "removed $REPO_ROOT/venv"
        any_found=1
    fi
    if [ -e "$REPO_ROOT/site-settings" ]; then
        rm -rf -- "$REPO_ROOT/site-settings"
        ok "removed $REPO_ROOT/site-settings"
        any_found=1
    fi

    # Runtime state files at the repo root. Honor --keep-state by
    # skipping anything in KEEP_STATE_PRESERVE.
    local f
    for f in "${RUNTIME_STATE_FILES[@]}"; do
        if [ "$keep_state" -eq 1 ]; then
            local preserve=0
            local k
            for k in "${KEEP_STATE_PRESERVE[@]}"; do
                if [ "$f" = "$k" ]; then preserve=1; break; fi
            done
            if [ "$preserve" -eq 1 ]; then
                if [ -e "$REPO_ROOT/$f" ]; then
                    info "- preserved $REPO_ROOT/$f"
                fi
                continue
            fi
        fi
        if [ -e "$REPO_ROOT/$f" ]; then
            nuke_file "$REPO_ROOT/$f"
            any_found=1
        fi
    done

    info ""
    if [ "$any_found" -eq 0 ]; then
        info "Nothing to nuke — install tree is already virgin."
    else
        ok "Nuke complete. Re-run ./install.sh to reinstall."
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

print_usage() {
    cat <<EOF
Usage: ./install.sh [OPTION]

Without arguments, runs the interactive installer.

Options:
  --nuke           Remove venv/, site-settings/, runtime state, and
                   glowup-* systemd units. Returns the tree to a virgin
                   state for testing. The repo clone itself is kept.

  --nuke --keep-state
                   Same as --nuke, but preserves state.db*,
                   ble_pairing.json, and shopping.json so a fresh
                   install keeps server history, BLE sensor keys, and
                   the shopping list. Use this on a real deployment;
                   skip it for virgin testing.

  -h, --help       Show this help.
EOF
}

main() {
    # Platform detection always first — every code path needs it.
    detect_platform

    case "${1:-}" in
        --nuke)
            nuke "${2:-}"
            return
            ;;
        -h|--help)
            print_usage
            return
            ;;
        "")
            : # fall through to install flow
            ;;
        *)
            err "Unknown option: $1"
            print_usage
            exit 64  # EX_USAGE
            ;;
    esac

    preflight
    welcome
    feature_picker
    create_venv
    install_deps
    write_features_file
    compute_service_identity
    write_site_config
    write_server_config
    write_secrets_file
    install_systemd_units
    cleanup_stale_units
    enable_and_start_units
    self_check
    write_post_install_todo
    summary
}

main "$@"
