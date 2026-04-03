#!/usr/bin/env bash
# setup_clock.sh — Turn a Raspberry Pi OS Desktop into a GlowUp kiosk clock.
#
# Transforms a fresh Raspberry Pi into a fullscreen browser displaying
# the GlowUp /home page.  Idempotent — safe to re-run.
#
# Usage:
#   ./setup_clock.sh <server_ip> [options]
#
# Required:
#   server_ip           IP of the GlowUp server (e.g. 10.0.0.191)
#
# Options:
#   --hostname NAME     Set the Pi hostname (default: clock)
#   --rotate DEGREES    Screen rotation: 0, 90, 180, 270 (default: 0)
#   --ble               Deploy BLE sensor daemon (requires bleak, paho-mqtt)
#   --reboot "Day HH:MM"  Weekly reboot schedule (default: "Sun 04:00")
#   --help              Show this message
#
# Examples:
#   ./setup_clock.sh 10.0.0.191
#   ./setup_clock.sh 10.0.0.191 --hostname bedroom --rotate 90 --ble
#   ./setup_clock.sh 10.0.0.191 --reboot "Wed 03:00"
#
# Run on the Pi itself, or pipe via SSH:
#   ssh a@10.0.0.148 "bash -s" < tools/setup_clock.sh 10.0.0.191 --ble
#
# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GLOWUP_PORT=8420
KIOSK_URL_PATH="/home"
AUTOSTART_DIR="/etc/xdg/autostart"
KIOSK_DESKTOP_FILE="${AUTOSTART_DIR}/glowup-kiosk.desktop"
UNCLUTTER_DESKTOP_FILE="${AUTOSTART_DIR}/unclutter.desktop"
LIGHTDM_CONF="/etc/lightdm/lightdm.conf"
WAYFIRE_INI_GLOBAL="/etc/wayfire.ini"
CRON_MARKER="# glowup-weekly-reboot"

# Screen rotation values for Wayland (wlr-randr) and X11 (xrandr).
declare -A WAYLAND_ROTATE=([0]="normal" [90]="90" [180]="180" [270]="270")
declare -A X11_ROTATE=([0]="normal" [90]="left" [180]="inverted" [270]="right")

# Packages to purge — bloat that a kiosk clock does not need.
PURGE_PACKAGES=(
    # Browsers (Chromium stays).
    firefox rpi-firefox-mods
    # Media players and tools.
    vlc vlc-bin vlc-data vlc-l10n 'vlc-plugin-*'
    mkvtoolnix
    # IDEs and editors.
    geany geany-common thonny
    # Printing.
    cups 'cups-*' printer-driver-hpcups
    # Compilers (not building anything).
    gcc-14-aarch64-linux-gnu g++-14-aarch64-linux-gnu
    cpp-14-aarch64-linux-gnu
    # Remote access (SSH stays, VNC goes).
    realvnc-vnc-server rpi-connect
    # Wrong-board firmware and kernels.
    firmware-atheros firmware-mediatek
    # Documentation and wallpapers.
    rpi-userguide rpd-wallpaper-trixie rpd-wallpaper
    # Speech recognition.
    pocketsphinx-en-us libflite1
    # SD card flasher (not needed on the Pi itself).
    rpi-imager
    # Dev headers and i18n data.
    libpython3.13-dev python-babel-localedata iso-codes
    # Kernel headers (not compiling modules).
    'linux-headers-*'
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

SERVER_IP=""
HOSTNAME_NEW="clock"
ROTATE=0
ENABLE_BLE=false
REBOOT_SCHEDULE="Sun 04:00"

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

usage() {
    head -27 "$0" | tail -24 | sed 's/^# \?//'
    exit 0
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

if [[ $# -lt 1 ]]; then
    echo "ERROR: server_ip is required." >&2
    usage
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)    usage ;;
        --hostname)   HOSTNAME_NEW="$2"; shift 2 ;;
        --rotate)     ROTATE="$2"; shift 2 ;;
        --ble)        ENABLE_BLE=true; shift ;;
        --reboot)     REBOOT_SCHEDULE="$2"; shift 2 ;;
        -*)           echo "Unknown option: $1" >&2; exit 1 ;;
        *)
            if [[ -z "$SERVER_IP" ]]; then
                SERVER_IP="$1"; shift
            else
                echo "Unexpected argument: $1" >&2; exit 1
            fi
            ;;
    esac
done

if [[ -z "$SERVER_IP" ]]; then
    echo "ERROR: server_ip is required." >&2
    exit 1
fi

# Validate rotation.
if [[ "$ROTATE" != "0" && "$ROTATE" != "90" && "$ROTATE" != "180" && "$ROTATE" != "270" ]]; then
    echo "ERROR: --rotate must be 0, 90, 180, or 270." >&2
    exit 1
fi

KIOSK_URL="http://${SERVER_IP}:${GLOWUP_PORT}${KIOSK_URL_PATH}"

# ---------------------------------------------------------------------------
# Root check
# ---------------------------------------------------------------------------

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (sudo)." >&2
    exit 1
fi

# Detect the login user (the one who will run the desktop session).
LOGIN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
LOGIN_HOME="$(eval echo ~"${LOGIN_USER}")"

echo "=========================================="
echo " GlowUp Clock Setup"
echo "=========================================="
echo "  Server:    ${KIOSK_URL}"
echo "  Hostname:  ${HOSTNAME_NEW}"
echo "  Rotation:  ${ROTATE} degrees"
echo "  BLE:       ${ENABLE_BLE}"
echo "  Reboot:    ${REBOOT_SCHEDULE}"
echo "  User:      ${LOGIN_USER}"
echo "=========================================="
echo ""

# ---------------------------------------------------------------------------
# Phase 1: System configuration
# ---------------------------------------------------------------------------

echo "--- Phase 1: System configuration ---"

# Hostname.
CURRENT_HOSTNAME=$(hostname)
if [[ "$CURRENT_HOSTNAME" != "$HOSTNAME_NEW" ]]; then
    echo "${HOSTNAME_NEW}" > /etc/hostname
    sed -i "s/127.0.1.1.*$/127.0.1.1\t${HOSTNAME_NEW}/" /etc/hosts
    hostnamectl set-hostname "${HOSTNAME_NEW}" 2>/dev/null || true
    echo "  Hostname: ${CURRENT_HOSTNAME} -> ${HOSTNAME_NEW}"
else
    echo "  Hostname: already ${HOSTNAME_NEW}"
fi

# Timezone — Central Time (Perry's location).
CURRENT_TZ=$(timedatectl show --property=Timezone --value 2>/dev/null || cat /etc/timezone)
TARGET_TZ="America/Chicago"
if [[ "$CURRENT_TZ" != "$TARGET_TZ" ]]; then
    timedatectl set-timezone "$TARGET_TZ"
    echo "  Timezone: ${CURRENT_TZ} -> ${TARGET_TZ}"
else
    echo "  Timezone: already ${TARGET_TZ}"
fi

# Disable swap to reduce SD card wear.
if swapon --show | grep -q .; then
    dphys-swapfile swapoff 2>/dev/null || swapoff -a
    dphys-swapfile uninstall 2>/dev/null || true
    systemctl disable dphys-swapfile 2>/dev/null || true
    echo "  Swap: disabled"
else
    echo "  Swap: already off"
fi

echo ""

# ---------------------------------------------------------------------------
# Phase 2: Bloat removal
# ---------------------------------------------------------------------------

echo "--- Phase 2: Removing unnecessary packages ---"

# Build list of actually-installed packages from the purge list.
TO_PURGE=()
for pkg in "${PURGE_PACKAGES[@]}"; do
    # Handle glob patterns (e.g., vlc-plugin-*).
    if [[ "$pkg" == *"*"* ]]; then
        while IFS= read -r match; do
            TO_PURGE+=("$match")
        done < <(dpkg --list "$pkg" 2>/dev/null | awk '/^ii/ {print $2}')
    else
        if dpkg -s "$pkg" &>/dev/null; then
            TO_PURGE+=("$pkg")
        fi
    fi
done

if [[ ${#TO_PURGE[@]} -gt 0 ]]; then
    echo "  Purging ${#TO_PURGE[@]} package(s)..."
    apt-get purge -y "${TO_PURGE[@]}" >/dev/null 2>&1
    apt-get autoremove -y >/dev/null 2>&1
    apt-get clean
    echo "  Done."
else
    echo "  Nothing to purge."
fi

# Install unclutter if missing (hides the mouse cursor).
if ! command -v unclutter &>/dev/null; then
    echo "  Installing unclutter..."
    apt-get install -y unclutter >/dev/null 2>&1
fi

echo ""

# ---------------------------------------------------------------------------
# Phase 3: Kiosk configuration
# ---------------------------------------------------------------------------

echo "--- Phase 3: Kiosk configuration ---"

# --- Detect display server ---
# Raspberry Pi OS Trixie+ uses labwc (Wayland).  Older versions use
# X11 with openbox/lxsession.  Detect which is available.
USE_LABWC=false
if dpkg -s labwc &>/dev/null || [[ -f /usr/bin/labwc ]]; then
    USE_LABWC=true
fi

# Find Chromium binary — "chromium" on Trixie, "chromium-browser" on older.
CHROMIUM_BIN=""
if command -v chromium &>/dev/null; then
    CHROMIUM_BIN="chromium"
elif command -v chromium-browser &>/dev/null; then
    CHROMIUM_BIN="chromium-browser"
else
    echo "  ERROR: Chromium not found — install it first" >&2
    exit 1
fi
echo "  Display server: $(${USE_LABWC} && echo labwc/Wayland || echo X11)"
echo "  Chromium binary: ${CHROMIUM_BIN}"

# --- Auto-login to desktop ---
# lightdm auto-login.
if [[ -f "$LIGHTDM_CONF" ]]; then
    if ! grep -q "^autologin-user=" "$LIGHTDM_CONF"; then
        sed -i "/^\[Seat:\*\]/a autologin-user=${LOGIN_USER}" "$LIGHTDM_CONF"
    else
        sed -i "s/^autologin-user=.*/autologin-user=${LOGIN_USER}/" "$LIGHTDM_CONF"
    fi
fi

# raspi-config auto-login (works across display managers).
raspi-config nonint do_boot_behaviour B4 2>/dev/null || true
echo "  Auto-login: desktop auto-login enabled"

# --- Force HDMI hotplug (needed for headless boot / HDMI dummies) ---
if ! grep -q "^hdmi_force_hotplug=1" /boot/firmware/config.txt 2>/dev/null; then
    echo "hdmi_force_hotplug=1" >> /boot/firmware/config.txt
    echo "  HDMI: force hotplug enabled"
else
    echo "  HDMI: force hotplug already set"
fi

# --- Ensure rpd-wayland-core is installed (labwc session) ---
if [[ "$USE_LABWC" == "true" ]]; then
    if ! dpkg -s rpd-wayland-core &>/dev/null; then
        echo "  Installing rpd-wayland-core (may take a while on Pi 3B)..."
        apt-get install -y rpd-wayland-core >/dev/null 2>&1
        echo "  rpd-wayland-core: installed"
    fi
fi

# --- Screen rotation ---
ROTATE_CMD=""
if [[ "$ROTATE" != "0" ]]; then
    WLR_TRANSFORM="${WAYLAND_ROTATE[$ROTATE]}"
    X11_TRANSFORM="${X11_ROTATE[$ROTATE]}"
    if [[ "$USE_LABWC" == "true" ]]; then
        ROTATE_CMD="wlr-randr --output HDMI-A-1 --transform ${WLR_TRANSFORM} 2>/dev/null;"
    else
        ROTATE_CMD="xrandr --output HDMI-1 --rotate ${X11_TRANSFORM} 2>/dev/null;"
    fi

    # Also set in config.txt for console/boot rotation.
    DISPLAY_ROTATE_VAL=0
    case "$ROTATE" in
        90)  DISPLAY_ROTATE_VAL=1 ;;
        180) DISPLAY_ROTATE_VAL=2 ;;
        270) DISPLAY_ROTATE_VAL=3 ;;
    esac
    if ! grep -q "^display_rotate=" /boot/firmware/config.txt 2>/dev/null; then
        echo "display_rotate=${DISPLAY_ROTATE_VAL}" >> /boot/firmware/config.txt
    else
        sed -i "s/^display_rotate=.*/display_rotate=${DISPLAY_ROTATE_VAL}/" /boot/firmware/config.txt
    fi
    echo "  Rotation: ${ROTATE} degrees"
else
    sed -i '/^display_rotate=/d' /boot/firmware/config.txt 2>/dev/null || true
    echo "  Rotation: none"
fi

# --- Chromium flags ---
# Wayland needs --ozone-platform=wayland.  X11 works without it.
CHROMIUM_FLAGS="--password-store=basic --noerrdialogs --disable-infobars --disable-session-crashed-bubble --disable-restore-session-state --kiosk"
if [[ "$USE_LABWC" == "true" ]]; then
    CHROMIUM_FLAGS="--ozone-platform=wayland ${CHROMIUM_FLAGS}"
fi

# --- Write autostart ---
if [[ "$USE_LABWC" == "true" ]]; then
    # labwc uses ~/.config/labwc/autostart (shell script, not .desktop files).
    LABWC_DIR="${LOGIN_HOME}/.config/labwc"
    mkdir -p "$LABWC_DIR"
    cat > "${LABWC_DIR}/autostart" << LEOF
# GlowUp kiosk autostart — generated by setup_clock.sh.
# Do not edit manually; re-run setup_clock.sh to regenerate.

# Screen rotation (if configured).
${ROTATE_CMD:+sleep 2; ${ROTATE_CMD}}

# GlowUp kiosk — fullscreen Chromium.
sleep 5 && ${CHROMIUM_BIN} ${CHROMIUM_FLAGS} '${KIOSK_URL}' &
LEOF
    chmod +x "${LABWC_DIR}/autostart"
    chown -R "${LOGIN_USER}:${LOGIN_USER}" "${LABWC_DIR}"

    # Clean up X11 autostart files if they exist from a previous run.
    rm -f "$KIOSK_DESKTOP_FILE" "$UNCLUTTER_DESKTOP_FILE"
    rm -f "${AUTOSTART_DIR}/disable-blanking.desktop"
    rm -f "${AUTOSTART_DIR}/screen-rotate.desktop"
    echo "  Autostart: labwc (~/.config/labwc/autostart)"
else
    # X11 path: .desktop files in /etc/xdg/autostart/.

    # Hide cursor.
    cat > "$UNCLUTTER_DESKTOP_FILE" << 'UEOF'
[Desktop Entry]
Type=Application
Name=Hide Cursor
Exec=unclutter -idle 0.5 -root
Hidden=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
UEOF

    # Disable screen blanking.
    cat > "${AUTOSTART_DIR}/disable-blanking.desktop" << 'XEOF'
[Desktop Entry]
Type=Application
Name=Disable Screen Blanking
Exec=sh -c "xset s off; xset -dpms; xset s noblank"
Hidden=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
XEOF

    # Screen rotation.
    if [[ -n "$ROTATE_CMD" ]]; then
        cat > "${AUTOSTART_DIR}/screen-rotate.desktop" << REOF
[Desktop Entry]
Type=Application
Name=Screen Rotation
Exec=sh -c "sleep 2; ${ROTATE_CMD}"
Hidden=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
REOF
    else
        rm -f "${AUTOSTART_DIR}/screen-rotate.desktop"
    fi

    # Chromium kiosk.
    cat > "$KIOSK_DESKTOP_FILE" << KEOF
[Desktop Entry]
Type=Application
Name=GlowUp Kiosk
Comment=Fullscreen browser displaying GlowUp /home
Exec=sh -c "sleep 3; ${CHROMIUM_BIN} ${CHROMIUM_FLAGS} '${KIOSK_URL}'"
Hidden=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
KEOF
    echo "  Autostart: X11 (/etc/xdg/autostart/)"
fi

echo "  Kiosk URL: ${KIOSK_URL}"

echo ""

# ---------------------------------------------------------------------------
# Phase 4: Optional BLE sensor daemon
# ---------------------------------------------------------------------------

if [[ "$ENABLE_BLE" == "true" ]]; then
    echo "--- Phase 4: BLE sensor daemon ---"

    # Unblock Bluetooth.
    rfkill unblock bluetooth 2>/dev/null || true
    echo "  Bluetooth: unblocked"

    # Create Python venv if missing.
    VENV_DIR="${LOGIN_HOME}/venv"
    if [[ ! -d "$VENV_DIR" ]]; then
        sudo -u "$LOGIN_USER" python3 -m venv "$VENV_DIR"
        echo "  Python venv: created at ${VENV_DIR}"
    else
        echo "  Python venv: already exists"
    fi

    # Install BLE dependencies.
    sudo -u "$LOGIN_USER" "${VENV_DIR}/bin/pip" install --quiet \
        bleak paho-mqtt 2>/dev/null
    echo "  Dependencies: bleak, paho-mqtt installed"

    # Deploy BLE sensor module (must be copied separately — this script
    # only creates the systemd service).  The user deploys code via rsync
    # or the main deploy.sh script.

    # Create systemd service.
    BLE_SERVICE="/etc/systemd/system/glowup-ble-sensor.service"
    cat > "$BLE_SERVICE" << BEOF
[Unit]
Description=GlowUp BLE Sensor Daemon
After=network-online.target bluetooth.target
Wants=network-online.target

[Service]
Type=simple
User=${LOGIN_USER}
WorkingDirectory=${LOGIN_HOME}/lifx
ExecStartPre=/usr/sbin/rfkill unblock bluetooth
ExecStart=${VENV_DIR}/bin/python3 -m ble.sensor --config ${LOGIN_HOME}/lifx/ble_pairing.json --broker localhost
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
BEOF
    systemctl daemon-reload
    systemctl enable glowup-ble-sensor
    echo "  BLE service: installed and enabled"
    echo "  NOTE: Deploy ble/ module and ble_pairing.json before starting"

    echo ""
fi

# ---------------------------------------------------------------------------
# Phase 5: Weekly reboot cron
# ---------------------------------------------------------------------------

echo "--- Phase 5: Weekly reboot schedule ---"

# Parse "Day HH:MM" into cron fields.
REBOOT_DAY=$(echo "$REBOOT_SCHEDULE" | awk '{print $1}')
REBOOT_TIME=$(echo "$REBOOT_SCHEDULE" | awk '{print $2}')
REBOOT_HOUR=$(echo "$REBOOT_TIME" | cut -d: -f1)
REBOOT_MIN=$(echo "$REBOOT_TIME" | cut -d: -f2)

# Map day name to cron day-of-week number.
declare -A DAY_MAP=(
    [Sun]=0 [Mon]=1 [Tue]=2 [Wed]=3 [Thu]=4 [Fri]=5 [Sat]=6
    [sun]=0 [mon]=1 [tue]=2 [wed]=3 [thu]=4 [fri]=5 [sat]=6
    [Sunday]=0 [Monday]=1 [Tuesday]=2 [Wednesday]=3
    [Thursday]=4 [Friday]=5 [Saturday]=6
)
CRON_DOW="${DAY_MAP[$REBOOT_DAY]:-0}"

# Remove any existing glowup reboot cron, then add the new one.
crontab -l 2>/dev/null | grep -v "$CRON_MARKER" | crontab -
(crontab -l 2>/dev/null; echo "${REBOOT_MIN} ${REBOOT_HOUR} * * ${CRON_DOW} /sbin/reboot ${CRON_MARKER}") | crontab -
echo "  Reboot: ${REBOOT_DAY} at ${REBOOT_HOUR}:${REBOOT_MIN} (cron DOW=${CRON_DOW})"

echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

DISK_AVAIL=$(df -h / | awk 'NR==2 {print $4}')
DISK_PCT=$(df -h / | awk 'NR==2 {print $5}')

echo "=========================================="
echo " Setup complete!"
echo "=========================================="
echo "  Kiosk URL:  ${KIOSK_URL}"
echo "  Hostname:   ${HOSTNAME_NEW}"
echo "  Rotation:   ${ROTATE} degrees"
echo "  BLE:        ${ENABLE_BLE}"
echo "  Reboot:     ${REBOOT_SCHEDULE}"
echo "  Disk free:  ${DISK_AVAIL} (${DISK_PCT} used)"
echo ""
echo "  Reboot now to activate the kiosk:"
echo "    sudo reboot"
echo "=========================================="
