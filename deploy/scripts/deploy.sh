#!/usr/bin/env bash
# deploy.sh — Deploy GlowUp working tree to a target device
#
# Usage:   ./deploy.sh <target> [--dry-run]
# Targets: daedalus, pi, judy, glowup, mbclock
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

DAEDALUS_HOST="perrykivolowitz@10.0.0.191"
DAEDALUS_DEST="/Users/perrykivolowitz/lifx"

PI_HOST="pi@10.0.0.48"
PI_DEST="/home/pi/lifx"

JUDY_HOST="a@10.0.0.63"
JUDY_DEST="/home/a/lifx"

GLOWUP_HOST="a@10.0.0.214"
GLOWUP_DEST="/home/a/lifx"

# mbclock — Pi 4 bedroom kiosk (10.0.0.220).
# No systemd unit; the kiosk launches from ~/.config/labwc/autostart as a
# child of the labwc session.  deploy_mbclock() captures the running command
# line, rsyncs, kills the kiosk process, relaunches with the same args, and
# verifies all three services (kiosk, satellite, thermal) are healthy.
MBCLOCK_HOST="a@10.0.0.220"
MBCLOCK_DEST="/home/a/lifx"
# Venv lives at ~/venv (NOT under $MBCLOCK_DEST) — the kiosk's default
# command line uses this absolute path when no prior kiosk is found.
MBCLOCK_PY="/home/a/venv/bin/python"

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

# Daedalus gets tests/ so the morning report can run the test suite
# remotely.  tests/boneyard is still excluded (dead tests).
RSYNC_EXCLUDES_DAEDALUS=(
    --exclude='.git'
    --exclude='.claude'
    --exclude='.pytest_cache'
    --exclude='__pycache__'
    --exclude='*.pyc'
    --exclude='*.pyo'
    --exclude='.DS_Store'
    --exclude='tests/boneyard'
    --exclude='docs/'
    --exclude='deploy/'
    --exclude='tools/'
    --exclude='ios/'
    --exclude='shortcuts/'
    --exclude='*.example'
    --exclude='DEPLOYED'
    --exclude='ble_pairing.json'
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
# Daedalus (Mac Studio — production GlowUp server)
# ---------------------------------------------------------------------------

deploy_daedalus() {
    echo "==> daedalus: syncing to $DAEDALUS_HOST:$DAEDALUS_DEST"
    # Daedalus-specific rsync — includes tests/ for morning report.
    local opts=(-avz --delete "${RSYNC_EXCLUDES_DAEDALUS[@]}")
    $dry_run && opts+=(--dry-run)
    rsync "${opts[@]}" ./ "$DAEDALUS_HOST:$DAEDALUS_DEST/"
    write_deployed "$DAEDALUS_HOST" "$DAEDALUS_DEST"

    if $dry_run; then
        echo "[dry-run] would restart server on Daedalus"
        return
    fi

    # Kill the running server; it will be restarted by the launchd/nohup
    # wrapper.  If no server is running, that's fine — just deploy files.
    ssh "$DAEDALUS_HOST" \
        "pkill -f 'server.py.*server.json' || true"

    # Give the old process a moment to release the port.
    sleep 2

    # Start the server.
    ssh "$DAEDALUS_HOST" \
        "cd '$DAEDALUS_DEST' && nohup ~/venv/bin/python server.py ~/glowup_config/server.json > ~/glowup_config/server.log 2>&1 &"

    sleep 3

    # Verify it came up.
    local status
    status=$(ssh "$DAEDALUS_HOST" "curl -s -o /dev/null -w '%{http_code}' http://localhost:8420/api/status" 2>/dev/null || echo "000")
    if [ "$status" = "401" ] || [ "$status" = "200" ]; then
        echo "==> daedalus: server running (HTTP $status)"
    else
        echo "==> daedalus: WARNING — server may not have started (HTTP $status)"
    fi

    echo "==> daedalus: $(ssh "$DAEDALUS_HOST" "cat '$DAEDALUS_DEST/DEPLOYED'")"
    echo "==> daedalus: deploy complete"
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

    # Restart Zigbee2MQTT if installed and active.
    ssh "$PI_HOST" \
        "systemctl is-active --quiet zigbee2mqtt \
         && sudo systemctl restart zigbee2mqtt \
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
# GlowUp (Pi 5 — primary server when Daedalus retires)
# ---------------------------------------------------------------------------

deploy_glowup() {
    echo "==> glowup: syncing to $GLOWUP_HOST:$GLOWUP_DEST"
    ssh "$GLOWUP_HOST" "mkdir -p '$GLOWUP_DEST'"
    do_rsync "$GLOWUP_HOST" "$GLOWUP_DEST"
    write_deployed "$GLOWUP_HOST" "$GLOWUP_DEST"

    if $dry_run; then
        echo "[dry-run] would restart: glowup-server glowup-satellite"
        return
    fi

    ssh "$GLOWUP_HOST" "sudo systemctl restart glowup-server"

    # Satellite may not be enabled yet (needs mic hardware).
    ssh "$GLOWUP_HOST" \
        "systemctl is-enabled --quiet glowup-satellite \
         && sudo systemctl restart glowup-satellite \
         || true"

    sleep 3

    # Verify server came up.
    local status
    status=$(ssh "$GLOWUP_HOST" "curl -s -o /dev/null -w '%{http_code}' http://localhost:8420/api/status" 2>/dev/null || echo "000")
    if [ "$status" = "401" ] || [ "$status" = "200" ]; then
        echo "==> glowup: server running (HTTP $status)"
    else
        echo "==> glowup: WARNING — server may not have started (HTTP $status)"
    fi

    echo "==> glowup: $(ssh "$GLOWUP_HOST" "cat '$GLOWUP_DEST/DEPLOYED'")"
    echo "==> glowup: deploy complete"
}

# ---------------------------------------------------------------------------
# mbclock (Pi 4 bedroom kiosk)
# ---------------------------------------------------------------------------

deploy_mbclock() {
    # 1. Capture the running kiosk command line before touching anything.
    #    The kiosk runs as "python -m kiosk ..." under the labwc session.
    #    /proc/<pid>/cmdline uses NUL separators; tr converts to spaces.
    # Match ONLY the real python kiosk process — not the bash wrapper
    # that launched it.  A previous implementation used `pgrep -f
    # 'python.*-m kiosk'` which matched both the python child AND the
    # enclosing `bash -c 'cd ... && nohup ... python -m kiosk ...'`
    # wrapper (because the wrapper's cmdline literally contains the
    # regex).  `head -1` then picked the bash PID, the kill killed the
    # wrapper, and the python child was orphaned — every deploy left
    # another zombie kiosk on the framebuffer.  Requiring the basename
    # to start with `python` fixes it.
    local kiosk_pid kiosk_cmd
    kiosk_pid=$(ssh "$MBCLOCK_HOST" \
        "pgrep -f '^[^ ]*python[^ ]* -m kiosk'" 2>/dev/null || true)
    if [ -n "$kiosk_pid" ]; then
        kiosk_pid=$(echo "$kiosk_pid" | head -1)
        kiosk_cmd=$(ssh "$MBCLOCK_HOST" "tr '\0' ' ' < /proc/$kiosk_pid/cmdline")
        echo "==> mbclock: captured running kiosk (pid $kiosk_pid): $kiosk_cmd"
    else
        echo "==> mbclock: WARNING — no running kiosk process found, using default"
        kiosk_cmd="$MBCLOCK_PY -m kiosk --api http://10.0.0.214:8420 --rotate 0 --mode wallclock"
    fi

    # 2. Rsync (full tree, --delete, same as every other target).
    echo "==> mbclock: syncing to $MBCLOCK_HOST:$MBCLOCK_DEST"
    ssh "$MBCLOCK_HOST" "mkdir -p '$MBCLOCK_DEST'"
    do_rsync "$MBCLOCK_HOST" "$MBCLOCK_DEST"
    write_deployed "$MBCLOCK_HOST" "$MBCLOCK_DEST"

    if $dry_run; then
        echo "[dry-run] would kill and relaunch kiosk"
        return
    fi

    # 3. Kill the running kiosk.
    if [ -n "$kiosk_pid" ]; then
        echo "==> mbclock: killing kiosk (pid $kiosk_pid)"
        ssh "$MBCLOCK_HOST" "kill $kiosk_pid" 2>/dev/null || true
        sleep 1
    fi

    # 4. Relaunch with the captured command line.
    #    Run from the lifx directory, backgrounded, stdout/stderr to log.
    #    ssh -n disconnects the ssh client's stdin so the backgrounded
    #    kiosk doesn't inherit it; `< /dev/null` on the remote command
    #    ensures the kiosk has no stdin to keep the ssh channel open.
    #    Without both, ssh waits forever on the inherited stdin FD and
    #    the deploy script hangs indefinitely after the relaunch.
    echo "==> mbclock: relaunching kiosk"
    ssh -n "$MBCLOCK_HOST" \
        "cd '$MBCLOCK_DEST' && nohup $kiosk_cmd < /dev/null > /tmp/kiosk.log 2>&1 &"
    sleep 2

    # 5. Verify all three processes are healthy.
    local ok=true

    # Kiosk process.
    if ssh "$MBCLOCK_HOST" "pgrep -f 'python.*-m kiosk'" > /dev/null 2>&1; then
        echo "==> mbclock: kiosk running"
    else
        echo "==> mbclock: WARNING — kiosk did not come back"
        ok=false
    fi

    # Voice satellite (systemd).
    if ssh "$MBCLOCK_HOST" "systemctl is-active --quiet glowup-satellite"; then
        echo "==> mbclock: satellite running"
    else
        echo "==> mbclock: WARNING — glowup-satellite not active"
        ok=false
    fi

    # Thermal sensor (systemd).
    if ssh "$MBCLOCK_HOST" "systemctl is-active --quiet pi-thermal"; then
        echo "==> mbclock: thermal running"
    else
        echo "==> mbclock: WARNING — pi-thermal not active"
        ok=false
    fi

    if $ok; then
        echo "==> mbclock: deploy complete — all services healthy"
    else
        echo "==> mbclock: deploy complete — CHECK WARNINGS ABOVE"
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

case "${1:-}" in
    daedalus) deploy_daedalus ;;
    pi)       deploy_pi       ;;
    judy)     deploy_judy     ;;
    glowup)   deploy_glowup   ;;
    mbclock)  deploy_mbclock  ;;
    *)
        echo "Usage: $0 <target> [--dry-run]"
        echo "Targets: daedalus, pi, judy, glowup, mbclock"
        exit 1
        ;;
esac
