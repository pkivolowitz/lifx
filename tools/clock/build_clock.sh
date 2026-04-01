#!/usr/bin/env bash
# ======================================================================
# GlowUp Standalone Clock — Pi Setup Script
#
# Configures a freshly-flashed Raspberry Pi as a self-healing kiosk
# clock display.  Runs over SSH from the build machine (Conway).
#
# Usage:
#   ./build_clock.sh --host <ip> --user <user> --profile <profile_id>
#
# Prerequisites:
#   - Pi flashed with Pi OS Trixie Lite (64-bit)
#   - SSH enabled, user created, WiFi configured (via Raspberry Pi Imager)
#   - SSH key copied (ssh-copy-id)
#   - index.html, config.json, server.py in the same directory as this script
#
# This script is idempotent — safe to run multiple times.
# ======================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ------------------------------------------------------------------
# Defaults
# ------------------------------------------------------------------
HOST=""
USER="a"
PROFILE=""
TIMEZONE="America/Chicago"
HOSTNAME_CLOCK=""
ROTATE=""

# ------------------------------------------------------------------
# Parse arguments
# ------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)      HOST="$2"; shift 2 ;;
        --user)      USER="$2"; shift 2 ;;
        --profile)   PROFILE="$2"; shift 2 ;;
        --timezone)  TIMEZONE="$2"; shift 2 ;;
        --hostname)  HOSTNAME_CLOCK="$2"; shift 2 ;;
        --rotate)    ROTATE="$2"; shift 2 ;;
        *)           echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [[ -z "$HOST" ]]; then
    echo "Usage: $0 --host <ip> [--user a] [--profile pi3b_hdmi_portrait] [--timezone America/Chicago] [--hostname clock-name] [--rotate 90]"
    exit 1
fi

SSH_CMD="ssh ${USER}@${HOST}"
SCP_CMD="scp"

echo "==> Target: ${USER}@${HOST}"
echo "==> Profile: ${PROFILE:-auto}"
echo "==> Timezone: ${TIMEZONE}"

# ------------------------------------------------------------------
# Step 1: Remove desktop bloat
# ------------------------------------------------------------------
echo "==> Removing desktop bloat..."
$SSH_CMD "sudo apt-get remove -y --purge \
    libreoffice* vlc* thonny* geany* mousepad* \
    rpd-planner rpd-wallpaper \
    2>/dev/null || true" > /dev/null 2>&1
$SSH_CMD "sudo apt-get autoremove -y > /dev/null 2>&1 || true"

# ------------------------------------------------------------------
# Step 2: Install Chromium and fonts
# ------------------------------------------------------------------
echo "==> Installing Chromium and emoji font..."
$SSH_CMD "sudo apt-get update -qq && \
    sudo apt-get install -y -qq chromium fonts-noto-color-emoji"

# ------------------------------------------------------------------
# Step 3: Determine Chromium binary name
# ------------------------------------------------------------------
CHROMIUM_BIN=$($SSH_CMD "which chromium 2>/dev/null || which chromium-browser 2>/dev/null || echo chromium")
echo "==> Chromium binary: ${CHROMIUM_BIN}"

# ------------------------------------------------------------------
# Step 4: Set timezone and hostname
# ------------------------------------------------------------------
echo "==> Setting timezone to ${TIMEZONE}..."
$SSH_CMD "sudo timedatectl set-timezone '${TIMEZONE}'"

if [[ -n "$HOSTNAME_CLOCK" ]]; then
    echo "==> Setting hostname to ${HOSTNAME_CLOCK}..."
    $SSH_CMD "sudo hostnamectl set-hostname '${HOSTNAME_CLOCK}'"
fi

# ------------------------------------------------------------------
# Step 5: Disable WiFi power save
# ------------------------------------------------------------------
echo "==> Disabling WiFi power save..."
$SSH_CMD "sudo tee /etc/NetworkManager/conf.d/wifi-powersave.conf > /dev/null << 'NMEOF'
# Disable WiFi power save — prevents overnight network drops.
# Created by GlowUp build_clock.sh.
[connection]
wifi.powersave = 2
NMEOF
" 2>/dev/null || \
$SSH_CMD "sudo iw wlan0 set power_save off 2>/dev/null || true"

# ------------------------------------------------------------------
# Step 6: Enable hardware watchdog
# ------------------------------------------------------------------
echo "==> Enabling hardware watchdog..."
$SSH_CMD "grep -q 'dtparam=watchdog=on' /boot/firmware/config.txt 2>/dev/null || \
    echo 'dtparam=watchdog=on' | sudo tee -a /boot/firmware/config.txt > /dev/null"

# ------------------------------------------------------------------
# Step 7: Deploy clock files
# ------------------------------------------------------------------
echo "==> Deploying clock files..."
$SSH_CMD "mkdir -p ~/clock"
$SCP_CMD "${SCRIPT_DIR}/index.html" "${USER}@${HOST}:~/clock/"
$SCP_CMD "${SCRIPT_DIR}/server.py" "${USER}@${HOST}:~/clock/"

# Config.json — only copy if it doesn't exist (don't overwrite bespoke config).
$SSH_CMD "test -f ~/clock/config.json" 2>/dev/null || \
    $SCP_CMD "${SCRIPT_DIR}/config.json" "${USER}@${HOST}:~/clock/" 2>/dev/null || \
    $SCP_CMD "${SCRIPT_DIR}/config.example.json" "${USER}@${HOST}:~/clock/config.json"

# ------------------------------------------------------------------
# Step 8: Install systemd services
# ------------------------------------------------------------------
echo "==> Installing systemd services..."
$SCP_CMD "${SCRIPT_DIR}/clock-server.service" "${USER}@${HOST}:/tmp/"
$SCP_CMD "${SCRIPT_DIR}/clock-display.service" "${USER}@${HOST}:/tmp/"
$SSH_CMD "sudo cp /tmp/clock-server.service /etc/systemd/system/ && \
    sudo cp /tmp/clock-display.service /etc/systemd/system/ && \
    sudo sed -i 's|CHROMIUM_BIN=chromium|CHROMIUM_BIN=${CHROMIUM_BIN}|' \
        /etc/systemd/system/clock-display.service && \
    sudo systemctl daemon-reload && \
    sudo systemctl enable clock-server clock-display"

# ------------------------------------------------------------------
# Step 9: Transparent cursor (hide mouse on Wayland kiosk)
# ------------------------------------------------------------------
echo "==> Installing transparent cursor theme..."
$SSH_CMD "mkdir -p ~/.local/share/icons/transparent/cursors && \
    printf '\x00\x00\x02\x00\x01\x00\x01\x01\x00\x00\x01\x00\x01\x00\x30\x00\x00\x00\x16\x00\x00\x00\x28\x00\x00\x00\x01\x00\x00\x00\x02\x00\x00\x00\x01\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00' \
    > ~/.local/share/icons/transparent/cursors/default 2>/dev/null || true"

# Link all standard cursor names to the transparent one.
$SSH_CMD "cd ~/.local/share/icons/transparent/cursors && \
    for name in pointer left_ptr crosshair text watch hand2 grab; do \
        ln -sf default \$name 2>/dev/null || true; \
    done"

# Set cursor theme via environment.
$SSH_CMD "mkdir -p ~/.config/labwc && \
    grep -q 'XCURSOR_THEME' ~/.config/labwc/environment 2>/dev/null || \
    printf 'XCURSOR_THEME=transparent\nXCURSOR_SIZE=1\n' >> ~/.config/labwc/environment"

# ------------------------------------------------------------------
# Step 10: Screen rotation (if requested)
# ------------------------------------------------------------------
if [[ -n "$ROTATE" ]]; then
    echo "==> Configuring screen rotation (${ROTATE} degrees)..."
    # labwc autostart handles wlr-randr rotation.
    $SSH_CMD "mkdir -p ~/.config/labwc"
    case "$ROTATE" in
        90)  TRANSFORM="90" ;;
        180) TRANSFORM="180" ;;
        270) TRANSFORM="270" ;;
        *)   TRANSFORM="" ;;
    esac
    if [[ -n "$TRANSFORM" ]]; then
        $SSH_CMD "cat > ~/.config/labwc/autostart << 'ASEOF'
# GlowUp clock — rotate display and start Chromium.
# Created by build_clock.sh.
sleep 2
wlr-randr --output \$(wlr-randr | head -1 | awk '{print \$1}') --transform ${TRANSFORM}
ASEOF
"
    fi
fi

# ------------------------------------------------------------------
# Step 11: Auto-login to desktop session (for Wayland/labwc)
# ------------------------------------------------------------------
echo "==> Configuring auto-login..."
$SSH_CMD "sudo raspi-config nonint do_boot_behaviour B4 2>/dev/null || true"

# ------------------------------------------------------------------
# Done
# ------------------------------------------------------------------
echo ""
echo "==> Clock setup complete on ${HOST}."
echo "    Reboot to start the clock display."
echo "    Config file: ~/clock/config.json"
echo "    To reboot:  ssh ${USER}@${HOST} sudo reboot"
