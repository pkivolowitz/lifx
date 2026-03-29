#!/usr/bin/env bash
# deploy-broker2.sh — Set up and maintain broker-2 (Zigbee + BLE relay node)
#
# Usage:   ./deploy-broker2.sh <command>
# Commands:
#   setup       Full first-time setup: Node.js, Zigbee2MQTT, udev, systemd, MQTT bridge
#   update-z2m  Update Zigbee2MQTT to latest version
#   status      Show service status and device health
#   restart     Restart Zigbee2MQTT and Mosquitto
#
# broker-2 has no repo.  This script runs from Conway (or any dev machine)
# and manages broker-2 entirely over SSH.
#
# Architecture:
#   SONOFF MG24 dongle (/dev/ttyUSB0) → Zigbee2MQTT → mosquitto (local)
#                                                          |
#                                                     MQTT bridge
#                                                          |
#                                                   Pi primary (10.0.0.48)
#
# broker-2 details:
#   Host:    a@broker-2.local  (10.0.0.123)
#   OS:      Debian 13 (trixie), aarch64
#   Dongle:  SONOFF Zigbee 3.0 USB Dongle Plus MG24 (CP210x, 10c4:ea60)
#   Sudo:    passwordless
#   MQTT:    mosquitto running, bridge to 10.0.0.48 already configured

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BROKER2_HOST="a@broker-2.local"
Z2M_DIR="/opt/zigbee2mqtt"
Z2M_DATA="$Z2M_DIR/data"

# CP210x USB-to-serial on the SONOFF MG24 dongle.
# Different from the CH343 chipset in the udev template (deploy/99-zigbee-dongle.rules).
DONGLE_VENDOR="10c4"
DONGLE_PRODUCT="ea60"

# Primary Pi MQTT broker — where zigbee topics are bridged.
PRIMARY_PI="10.0.0.48"

# Node.js major version to install (LTS).
NODE_MAJOR=22

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

remote() {
    ssh "$BROKER2_HOST" "$@"
}

remote_sudo() {
    ssh "$BROKER2_HOST" "sudo $*"
}

check_connectivity() {
    echo "==> Checking connectivity to broker-2..."
    if ! ssh -o ConnectTimeout=5 "$BROKER2_HOST" "echo ok" >/dev/null 2>&1; then
        echo "ERROR: Cannot reach $BROKER2_HOST"
        exit 1
    fi
    echo "    Connected."
}

# ---------------------------------------------------------------------------
# setup — Full first-time installation
# ---------------------------------------------------------------------------

do_setup() {
    check_connectivity

    echo ""
    echo "==> Step 1/6: Install Node.js $NODE_MAJOR.x"
    if remote "node --version" >/dev/null 2>&1; then
        echo "    Node.js already installed: $(remote 'node --version')"
    else
        echo "    Installing Node.js ${NODE_MAJOR}.x from NodeSource..."
        remote_sudo "apt-get update -qq"
        remote_sudo "apt-get install -y -qq ca-certificates curl gnupg"
        remote "curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | sudo -E bash -"
        remote_sudo "apt-get install -y -qq nodejs"
        echo "    Installed: $(remote 'node --version')"
    fi

    echo ""
    echo "==> Step 2/6: Install udev rule for SONOFF dongle"
    # Creates stable /dev/ttyZigbee symlink regardless of USB enumeration order.
    local udev_rule="SUBSYSTEM==\"tty\", ATTRS{idVendor}==\"${DONGLE_VENDOR}\", ATTRS{idProduct}==\"${DONGLE_PRODUCT}\", SYMLINK+=\"ttyZigbee\", MODE=\"0660\", GROUP=\"dialout\""
    remote_sudo "bash -c 'cat > /etc/udev/rules.d/99-zigbee-dongle.rules << UDEV
# udev rule for SONOFF Zigbee 3.0 USB Dongle Plus (EFR32MG24, CP210x bridge).
# Creates /dev/ttyZigbee symlink.  Vendor 10c4 / Product ea60 = Silicon Labs CP210x.
# Installed by deploy-broker2.sh from Conway.
${udev_rule}
UDEV'"
    remote_sudo "udevadm control --reload-rules"
    remote_sudo "udevadm trigger"
    # Wait briefly for symlink to appear.
    sleep 1
    if remote "test -e /dev/ttyZigbee"; then
        echo "    /dev/ttyZigbee symlink created."
    else
        echo "    WARNING: /dev/ttyZigbee not found — dongle may not be plugged in."
    fi

    echo ""
    echo "==> Step 3/6: Install Zigbee2MQTT"
    if remote "test -d $Z2M_DIR"; then
        echo "    Zigbee2MQTT directory already exists at $Z2M_DIR"
    else
        remote_sudo "mkdir -p $Z2M_DIR"
        remote_sudo "chown a:a $Z2M_DIR"
        echo "    Cloning Zigbee2MQTT..."
        remote "git clone --depth 1 https://github.com/Koenkk/zigbee2mqtt.git $Z2M_DIR"
        # Z2M 2.x requires pnpm and a TypeScript build step.
        echo "    Installing pnpm (if needed)..."
        remote "which pnpm >/dev/null 2>&1 || sudo npm install -g pnpm"
        echo "    Installing dependencies and building (this takes a few minutes on Pi)..."
        remote "cd $Z2M_DIR && pnpm install && pnpm run build"
    fi

    echo ""
    echo "==> Step 4/6: Write Zigbee2MQTT configuration"
    remote "mkdir -p $Z2M_DATA"
    # Only write config if it does not exist — preserves network key after first run.
    if remote "test -f $Z2M_DATA/configuration.yaml"; then
        echo "    Configuration already exists — preserving network key."
    else
        remote "cat > $Z2M_DATA/configuration.yaml << 'Z2MCONF'
# Zigbee2MQTT configuration for GlowUp on broker-2.
#
# SONOFF Zigbee 3.0 USB Dongle Plus (EFR32MG24) uses the \"ember\" adapter.
# Wrong adapter = silent failure.
#
# After first run, Zigbee2MQTT generates a network key in this file.
# Do NOT copy this file to another machine or commit it.
#
# Installed by deploy-broker2.sh from Conway.

homeassistant: false
permit_join: false

mqtt:
  base_topic: zigbee2mqtt
  server: mqtt://localhost:1883

serial:
  # Stable symlink created by udev rule (99-zigbee-dongle.rules).
  # CP210x bridge on the SONOFF MG24: vendor 10c4, product ea60.
  port: /dev/ttyZigbee
  adapter: ember

frontend:
  # Web UI for pairing and device management.
  port: 8099

advanced:
  log_level: info
  # GENERATE on first run — Zigbee2MQTT creates a random network key.
  network_key: GENERATE

ota:
  # Enable over-the-air firmware updates for Zigbee devices.
  update_available_check: true
Z2MCONF"
        echo "    Configuration written."
    fi

    echo ""
    echo "==> Step 5/6: Install systemd service"
    remote_sudo "bash -c 'cat > /etc/systemd/system/zigbee2mqtt.service << SERVICE
# Zigbee2MQTT systemd service for GlowUp on broker-2.
# User is \"a\" (broker-2 uses user a, not pi).
# Installed by deploy-broker2.sh from Conway.

[Unit]
Description=Zigbee2MQTT — Zigbee to MQTT bridge for GlowUp
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
Type=simple
User=a
ExecStart=/usr/bin/npm start --prefix $Z2M_DIR
WorkingDirectory=$Z2M_DIR
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=zigbee2mqtt

[Install]
WantedBy=multi-user.target
SERVICE'"
    remote_sudo "systemctl daemon-reload"
    remote_sudo "systemctl enable zigbee2mqtt"
    echo "    Service installed and enabled."

    echo ""
    echo "==> Step 6/6: Update MQTT bridge to forward Zigbee topics"
    # The existing bridge forwards glowup/ble/# only.
    # Add zigbee2mqtt/# so the primary Pi receives Zigbee sensor data.
    if remote "grep -q 'zigbee2mqtt/#' /etc/mosquitto/conf.d/glowup-bridge.conf"; then
        echo "    Zigbee topic forwarding already configured."
    else
        remote_sudo "bash -c 'cat >> /etc/mosquitto/conf.d/glowup-bridge.conf << BRIDGE

# Forward Zigbee2MQTT topics upstream so the primary Pi receives
# Zigbee sensor data (soil moisture, temperature, motion, etc.).
# Added by deploy-broker2.sh.
topic zigbee2mqtt/# out 1
BRIDGE'"
        remote_sudo "systemctl restart mosquitto"
        echo "    Zigbee topic bridge added and mosquitto restarted."
    fi

    echo ""
    echo "==> Starting Zigbee2MQTT..."
    remote_sudo "systemctl start zigbee2mqtt"
    sleep 3
    if remote "systemctl is-active --quiet zigbee2mqtt"; then
        echo "    Zigbee2MQTT is running."
    else
        echo "    WARNING: Zigbee2MQTT failed to start. Check logs:"
        echo "    ssh $BROKER2_HOST 'journalctl -u zigbee2mqtt -n 30'"
    fi

    echo ""
    echo "==> Setup complete."
    echo "    Zigbee2MQTT frontend: http://broker-2.local:8099"
    echo "    MQTT bridge: zigbee2mqtt/# → $PRIMARY_PI:1883"
    echo ""
    echo "    To pair a device:"
    echo "      1. Open http://broker-2.local:8099"
    echo "      2. Click 'Permit join' (or: mosquitto_pub -t zigbee2mqtt/bridge/request/permit_join -m '{\"value\": true}')"
    echo "      3. Put device in pairing mode"
    echo "      4. Device appears in the frontend"
    echo "      5. Disable permit_join when done"
}

# ---------------------------------------------------------------------------
# update-z2m — Pull latest Zigbee2MQTT and rebuild
# ---------------------------------------------------------------------------

do_update_z2m() {
    check_connectivity

    echo "==> Stopping Zigbee2MQTT..."
    remote_sudo "systemctl stop zigbee2mqtt"

    echo "==> Updating Zigbee2MQTT..."
    remote "cd $Z2M_DIR && git pull"
    echo "==> Rebuilding dependencies..."
    remote "cd $Z2M_DIR && pnpm install && pnpm run build"

    echo "==> Starting Zigbee2MQTT..."
    remote_sudo "systemctl start zigbee2mqtt"
    sleep 3
    if remote "systemctl is-active --quiet zigbee2mqtt"; then
        echo "    Zigbee2MQTT is running."
    else
        echo "    WARNING: Zigbee2MQTT failed to start after update."
        echo "    Check: ssh $BROKER2_HOST 'journalctl -u zigbee2mqtt -n 30'"
    fi
}

# ---------------------------------------------------------------------------
# status — Show service and device health
# ---------------------------------------------------------------------------

do_status() {
    check_connectivity

    echo "==> broker-2 status"
    echo ""
    echo "--- Services ---"
    remote "systemctl is-active zigbee2mqtt 2>/dev/null && echo 'zigbee2mqtt: active' || echo 'zigbee2mqtt: inactive'"
    remote "systemctl is-active mosquitto 2>/dev/null && echo 'mosquitto:   active' || echo 'mosquitto:   inactive'"
    echo ""
    echo "--- Zigbee dongle ---"
    if remote "test -e /dev/ttyZigbee"; then
        echo "/dev/ttyZigbee: present"
    else
        echo "/dev/ttyZigbee: MISSING"
    fi
    remote "ls -la /dev/ttyUSB* 2>/dev/null || echo 'No /dev/ttyUSB* devices'"
    echo ""
    echo "--- Bluetooth ---"
    remote "hciconfig 2>/dev/null | grep -E 'hci|BD Address|UP' || echo 'No BT adapters'"
    echo ""
    echo "--- MQTT bridge health ---"
    remote "mosquitto_sub -t 'glowup/bridge/broker-2/status' -C 1 -W 3 2>/dev/null || echo 'Bridge status: no response (may be OK if just started)'"
    echo ""
    echo "--- Recent Zigbee2MQTT logs ---"
    remote "journalctl -u zigbee2mqtt -n 10 --no-pager 2>/dev/null || echo 'No logs available'"
}

# ---------------------------------------------------------------------------
# restart — Restart services
# ---------------------------------------------------------------------------

do_restart() {
    check_connectivity

    echo "==> Restarting Zigbee2MQTT and Mosquitto on broker-2..."
    remote_sudo "systemctl restart mosquitto"
    remote_sudo "systemctl restart zigbee2mqtt"
    sleep 3
    remote "systemctl is-active zigbee2mqtt && echo 'zigbee2mqtt: active' || echo 'zigbee2mqtt: FAILED'"
    remote "systemctl is-active mosquitto && echo 'mosquitto:   active' || echo 'mosquitto:   FAILED'"
    echo "==> Done."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

case "${1:-}" in
    setup)      do_setup      ;;
    update-z2m) do_update_z2m ;;
    status)     do_status     ;;
    restart)    do_restart    ;;
    *)
        echo "Usage: $0 <command>"
        echo ""
        echo "Commands:"
        echo "  setup       Full first-time setup (Node.js, Zigbee2MQTT, udev, systemd, MQTT bridge)"
        echo "  update-z2m  Update Zigbee2MQTT to latest version"
        echo "  status      Show service and device health"
        echo "  restart     Restart Zigbee2MQTT and Mosquitto"
        exit 1
        ;;
esac
