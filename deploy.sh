#!/usr/bin/env bash
# deploy.sh — Deploy GlowUp working tree to a target device
#
# Usage:   ./deploy.sh <target> [--dry-run]
# Targets: pi, judy
#
# Deploys the current working tree (including uncommitted changes) to the
# target device via rsync. A clean git state is NOT required — deploy freely
# during active development and commit only when the feature is done.
#
# Machine-local configs (/etc/glowup/, ~/.glowup/) are outside the repo
# and are never touched by this script.
#
# Pi services are restarted automatically (passwordless sudo).
# Judy services must be restarted manually — Jetsons require interactive sudo.

set -euo pipefail

# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

PI_HOST="pi@10.0.0.48"
PI_DEST="/home/pi/lifx"

JUDY_HOST="a@10.0.0.63"
JUDY_DEST="/home/a/lifx"

# ---------------------------------------------------------------------------
# Rsync exclusions — dev artifacts, docs, test suite, deploy templates.
# Machine-local configs live outside the repo and are never affected.
# DEPLOYED is written by this script on the remote; excluded from sync so it
# survives subsequent deploys from any machine.
# ---------------------------------------------------------------------------

RSYNC_EXCLUDES=(
    --exclude='.git'
    --exclude='.claude'       # Claude Code settings — never leave dev machine
    --exclude='.pytest_cache'
    --exclude='__pycache__'
    --exclude='*.pyc'
    --exclude='*.pyo'
    --exclude='.DS_Store'
    --exclude='tests/'
    --exclude='docs/'
    --exclude='deploy/'
    --exclude='tools/'
    --exclude='ios/'          # Xcode project
    --exclude='shortcuts/'    # macOS .command scripts
    --exclude='*.example'
    --exclude='DEPLOYED'      # written by this script on the remote; not source-controlled
    --exclude='ble_pairing.json'  # machine-local BLE pairing data; gitignored, never in working tree
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

dry_run=false
[[ "${2:-}" == "--dry-run" ]] && dry_run=true

version_stamp() {
    # Appends -dirty when the working tree has uncommitted changes,
    # so DEPLOYED always reflects exact source state.
    git describe --tags --always --dirty 2>/dev/null || echo "untagged"
}

do_rsync() {
    local host="$1" dest="$2"
    local opts=(-avz --delete "${RSYNC_EXCLUDES[@]}")
    $dry_run && opts+=(--dry-run)
    rsync "${opts[@]}" ./ "$host:$dest/"
}

write_deployed() {
    local host="$1" dest="$2"
    local stamp
    stamp="$(version_stamp) deployed from $(hostname -s) at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    $dry_run && { echo "[dry-run] DEPLOYED would contain: $stamp"; return; }
    ssh "$host" "echo '$stamp' > '$dest/DEPLOYED'"
}

# ---------------------------------------------------------------------------
# Pi
# ---------------------------------------------------------------------------

deploy_pi() {
    echo "==> pi: syncing to $PI_HOST:$PI_DEST"
    ssh "$PI_HOST" "mkdir -p '$PI_DEST'"
    do_rsync "$PI_HOST" "$PI_DEST"
    write_deployed "$PI_HOST" "$PI_DEST"

    if $dry_run; then
        echo "[dry-run] would restart: glowup-server glowup-scheduler (+ ble-sensor if active)"
        return
    fi

    ssh "$PI_HOST" "sudo systemctl restart glowup-server glowup-scheduler"

    # ble-sensor may not be installed or enabled everywhere; only restart it
    # if it is currently active so we don't fail on headless Pi installs.
    ssh "$PI_HOST" \
        "systemctl is-active --quiet glowup-ble-sensor \
         && sudo systemctl restart glowup-ble-sensor \
         || true"

    echo "==> pi: $(ssh "$PI_HOST" "cat '$PI_DEST/DEPLOYED'")"
    echo "==> pi: deploy complete"
}

# ---------------------------------------------------------------------------
# Judy
# ---------------------------------------------------------------------------

deploy_judy() {
    echo "==> judy: syncing to $JUDY_HOST:$JUDY_DEST"
    ssh "$JUDY_HOST" "mkdir -p '$JUDY_DEST'"
    do_rsync "$JUDY_HOST" "$JUDY_DEST"
    write_deployed "$JUDY_HOST" "$JUDY_DEST"

    if $dry_run; then
        echo "[dry-run] would print restart reminder"
        return
    fi

    echo "==> judy: $(ssh "$JUDY_HOST" "cat '$JUDY_DEST/DEPLOYED'")"
    echo "==> judy: files deployed"
    echo ""
    echo "    Judy requires interactive sudo — run this on Judy to restart the agent:"
    echo "    sudo systemctl restart glowup-agent"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

case "${1:-}" in
    pi)   deploy_pi   ;;
    judy) deploy_judy ;;
    *)
        echo "Usage: $0 <target> [--dry-run]"
        echo "Targets: pi, judy"
        exit 1
        ;;
esac
