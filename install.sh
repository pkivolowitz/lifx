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
# Step 5 — selection summary (Phase 1 stopping point)
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
    info "${C_DIM}Phase 1 skeleton: nothing written, nothing installed.${C_RESET}"
    info "${C_DIM}Phase 2 will add venv + pip install + site-settings + systemd.${C_RESET}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    detect_platform
    preflight
    welcome
    feature_picker
    summary
}

main "$@"
