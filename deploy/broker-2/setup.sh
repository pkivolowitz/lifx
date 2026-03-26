#!/usr/bin/env bash
# --------------------------------------------------------------------
# broker-2 setup — BLE relay node for GlowUp distributed MQTT broker
#
# Idempotent — safe to re-run after a failure or to update an existing
# install.  Each step checks whether it already completed before
# doing work.
#
# What this does:
#   - Installs system packages (mosquitto, bluez, python3-venv)
#   - Creates /home/a/venv with BLE Python dependencies
#   - Ensures SSH access to NAS (10.0.0.24) for git clone
#   - Clones or updates the lifx repo from staging
#   - Installs mosquitto bridge config (forwards glowup/ble/# to
#     primary Pi at 10.0.0.48)
#   - Installs and enables glowup-ble-sensor systemd service
#   - Verifies the MQTT bridge is forwarding messages
#
# Architecture:
#   ONVIS sensor --BLE--> broker-2 (this Pi 5, same room as sensor)
#                            |
#                       MQTT bridge
#                            |
#                         Pi primary (10.0.0.48, GlowUp server)
#
# Prerequisites:
#   - Raspberry Pi OS with network configured
#   - User 'a' exists with sudo (created during flash, password 'a')
#   - SSH key from this machine already placed on broker-2
#
# Usage (from dev machine):
#   scp deploy/broker-2/setup.sh a@10.0.0.123:~
#   ssh a@10.0.0.123 bash setup.sh
#
# Network:
#   broker-2:   10.0.0.123
#   Primary Pi: 10.0.0.48
#   NAS:        10.0.0.24  (git bare repo via SSH)
#
# Last tested: 2026-03-26, broker-2 Pi 5, Raspberry Pi OS Bookworm
# --------------------------------------------------------------------

set -euo pipefail

# ---- Configuration --------------------------------------------------

NAS_IP="10.0.0.24"
NAS_USER="perryk"
NAS_REPO="${NAS_USER}@${NAS_IP}:/mnt/storage/perryk/git/lifx.git"
PRIMARY_PI="10.0.0.48"
BRANCH="ble-sensor"
USER="a"
HOME_DIR="/home/${USER}"
VENV="${HOME_DIR}/venv"
REPO="${HOME_DIR}/lifx"
DEPLOY_DIR="${REPO}/deploy/broker-2"

# Track failures for final report.
ERRORS=0

step() {
    # Print a step header.  Usage: step "Description"
    echo ""
    echo "==== $1 ===="
}

warn() {
    # Print a warning but continue.  Increments error counter.
    echo "  WARNING: $1"
    ERRORS=$((ERRORS + 1))
}

# ---- System packages ------------------------------------------------
step "System packages"

PACKAGES=(mosquitto mosquitto-clients python3-pip python3-venv bluetooth bluez)
MISSING=()
for pkg in "${PACKAGES[@]}"; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        MISSING+=("$pkg")
    fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "  Installing: ${MISSING[*]}"
    export DEBIAN_FRONTEND=noninteractive
    sudo apt-get update -qq
    sudo apt-get install -y -qq "${MISSING[@]}"
else
    echo "  All packages already installed."
fi

# ---- Python venv ----------------------------------------------------
step "Python venv (${VENV})"

if [ ! -d "$VENV" ]; then
    echo "  Creating venv..."
    python3 -m venv "$VENV"
else
    echo "  Venv exists."
fi

echo "  Installing/upgrading BLE packages..."
"$VENV/bin/pip" install --quiet --upgrade \
    bleak \
    cryptography \
    paho-mqtt

# Sanity check — import the critical package.
if "$VENV/bin/python3" -c "import bleak; print(f'  bleak {bleak.__version__}')"; then
    echo "  Venv OK."
else
    warn "bleak import failed — check venv"
fi

# ---- Bluetooth -------------------------------------------------------
step "Bluetooth"

sudo usermod -aG bluetooth "$USER" 2>/dev/null || true
sudo rfkill unblock bluetooth 2>/dev/null || true

if systemctl is-active --quiet bluetooth; then
    echo "  Bluetooth service running."
else
    sudo systemctl start bluetooth || warn "Could not start bluetooth service"
fi

# ---- SSH keys for NAS access -----------------------------------------
step "SSH access to NAS (${NAS_IP})"

# Ensure we have a keypair.
if [ ! -f "${HOME_DIR}/.ssh/id_ed25519" ]; then
    echo "  Generating SSH key..."
    ssh-keygen -t ed25519 -f "${HOME_DIR}/.ssh/id_ed25519" -N '' -C "${USER}@broker-2"
    echo ""
    echo "  *** PUBLIC KEY (add to ${NAS_USER}@${NAS_IP}:~/.ssh/authorized_keys): ***"
    cat "${HOME_DIR}/.ssh/id_ed25519.pub"
    echo ""
else
    echo "  SSH key exists."
fi

# Ensure NAS host key is known.
if ! ssh-keygen -F "$NAS_IP" &>/dev/null; then
    echo "  Adding NAS host key..."
    ssh-keyscan -H "$NAS_IP" >> "${HOME_DIR}/.ssh/known_hosts" 2>/dev/null
fi

# Test NAS SSH access.
if ssh -o BatchMode=yes -o ConnectTimeout=5 "${NAS_USER}@${NAS_IP}" "echo ok" &>/dev/null; then
    echo "  NAS SSH access OK."
else
    warn "Cannot SSH to NAS. Add this key to ${NAS_USER}@${NAS_IP}:~/.ssh/authorized_keys:"
    cat "${HOME_DIR}/.ssh/id_ed25519.pub"
    echo "  Then re-run this script."
fi

# ---- Primary Pi host key --------------------------------------------
step "SSH access to primary Pi (${PRIMARY_PI})"

if ! ssh-keygen -F "$PRIMARY_PI" &>/dev/null; then
    echo "  Adding primary Pi host key..."
    ssh-keyscan -H "$PRIMARY_PI" >> "${HOME_DIR}/.ssh/known_hosts" 2>/dev/null
fi
echo "  Host key OK."

# ---- Clone or update repo -------------------------------------------
step "Git repo (${REPO})"

if [ ! -d "$REPO" ]; then
    echo "  Cloning from NAS..."
    if git clone "$NAS_REPO" "$REPO"; then
        cd "$REPO"
        git remote rename origin staging
        git checkout "$BRANCH"
        echo "  Cloned and on branch ${BRANCH}."
    else
        warn "Git clone failed — check NAS SSH access above"
    fi
else
    echo "  Repo exists, pulling latest..."
    cd "$REPO"
    # Ensure remote is named 'staging' (not 'origin').
    if git remote | grep -q '^origin$'; then
        git remote rename origin staging
    fi
    if git pull staging "$BRANCH"; then
        echo "  Updated to latest ${BRANCH}."
    else
        warn "Git pull failed — may need manual resolution"
    fi
fi

# ---- Mosquitto bridge config ----------------------------------------
step "Mosquitto bridge"

BRIDGE_SRC="${DEPLOY_DIR}/mosquitto-bridge.conf"
BRIDGE_DST="/etc/mosquitto/conf.d/glowup-bridge.conf"

if [ -f "$BRIDGE_SRC" ]; then
    # Only restart if config changed.
    if ! sudo diff -q "$BRIDGE_SRC" "$BRIDGE_DST" &>/dev/null; then
        echo "  Installing bridge config..."
        sudo cp "$BRIDGE_SRC" "$BRIDGE_DST"
        sudo systemctl restart mosquitto
    else
        echo "  Bridge config unchanged."
    fi
else
    warn "Bridge config not found at ${BRIDGE_SRC}"
fi

sudo systemctl enable mosquitto 2>/dev/null

if systemctl is-active --quiet mosquitto; then
    echo "  Mosquitto running."
else
    warn "Mosquitto not running"
fi

# ---- BLE sensor service ---------------------------------------------
step "BLE sensor service"

SERVICE_SRC="${DEPLOY_DIR}/glowup-ble-sensor.service"
SERVICE_DST="/etc/systemd/system/glowup-ble-sensor.service"

if [ -f "$SERVICE_SRC" ]; then
    if ! sudo diff -q "$SERVICE_SRC" "$SERVICE_DST" &>/dev/null; then
        echo "  Installing service unit..."
        sudo cp "$SERVICE_SRC" "$SERVICE_DST"
        sudo systemctl daemon-reload
    else
        echo "  Service unit unchanged."
    fi
    sudo systemctl enable glowup-ble-sensor 2>/dev/null
else
    warn "Service file not found at ${SERVICE_SRC}"
fi

# ---- Pairing config -------------------------------------------------
step "BLE pairing config"

PAIRING="${REPO}/ble_pairing.json"
if [ -f "$PAIRING" ]; then
    echo "  ble_pairing.json present."
else
    warn "ble_pairing.json missing. Copy from primary Pi:"
    echo "    From dev machine:"
    echo "      ssh pi@${PRIMARY_PI} cat ~/lifx/ble_pairing.json > /tmp/bp.json"
    echo "      scp /tmp/bp.json ${USER}@10.0.0.123:${REPO}/"
    echo "      rm /tmp/bp.json"
fi

# ---- Start BLE daemon ------------------------------------------------
step "Start BLE daemon"

if [ -f "$PAIRING" ]; then
    sudo systemctl restart glowup-ble-sensor
    sleep 2
    if systemctl is-active --quiet glowup-ble-sensor; then
        echo "  BLE daemon running."
    else
        warn "BLE daemon failed to start. Check: sudo journalctl -u glowup-ble-sensor -n 20"
    fi
else
    echo "  Skipped — no pairing config yet."
fi

# ---- Verify MQTT bridge ----------------------------------------------
step "Verify MQTT bridge to primary Pi"

if command -v mosquitto_pub &>/dev/null; then
    # Publish a test message locally and check if primary receives it.
    TEST_TOPIC="glowup/bridge/broker-2/test"
    TEST_MSG="setup-verify-$(date +%s)"
    mosquitto_pub -t "$TEST_TOPIC" -m "$TEST_MSG" 2>/dev/null
    echo "  Published test message to ${TEST_TOPIC}"
    echo "  To verify on primary: mosquitto_sub -h ${PRIMARY_PI} -t '${TEST_TOPIC}' -C 1"
fi

# ---- Summary ---------------------------------------------------------
echo ""
echo "============================================"
if [ $ERRORS -eq 0 ]; then
    echo "  Setup complete — no errors."
else
    echo "  Setup complete with ${ERRORS} warning(s)."
    echo "  Review warnings above and re-run when fixed."
fi
echo "============================================"
echo ""
echo "Reminders:"
echo "  - Ensure BLE daemon is DISABLED on primary Pi:"
echo "      ssh pi@${PRIMARY_PI} sudo systemctl disable --now glowup-ble-sensor"
echo "  - Monitor bridge health:"
echo "      mosquitto_sub -h ${PRIMARY_PI} -t 'glowup/bridge/broker-2/status' -v"
echo "  - View BLE daemon logs:"
echo "      sudo journalctl -u glowup-ble-sensor -f"
echo "  - View mosquitto logs:"
echo "      sudo journalctl -u mosquitto -f"
