#!/usr/bin/env bash
# --------------------------------------------------------------------
# broker-2 setup — BLE relay node for GlowUp
#
# Run on broker-2 (Pi 5) as user 'a' after first boot.
# Installs mosquitto, Python BLE dependencies, clones the repo,
# and enables both services.
#
# Prerequisites:
#   - Raspberry Pi OS with network configured
#   - User 'a' exists (created during flash)
#   - SSH access working
#
# Usage:
#   ssh a@broker-2 "bash -s" < deploy/broker-2/setup.sh
#   — or —
#   scp deploy/broker-2/setup.sh a@broker-2:~ && ssh a@broker-2 bash setup.sh
# --------------------------------------------------------------------

set -euo pipefail

echo "=== broker-2 setup ==="

# ---- System packages ------------------------------------------------
echo "Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    mosquitto \
    mosquitto-clients \
    python3-pip \
    python3-venv \
    bluetooth \
    bluez

# ---- Python dependencies -------------------------------------------
echo "Installing Python BLE packages..."
sudo pip3 install --break-system-packages \
    bleak \
    cryptography \
    paho-mqtt

# ---- Bluetooth permissions ------------------------------------------
echo "Configuring Bluetooth..."
sudo usermod -aG bluetooth a 2>/dev/null || true
sudo rfkill unblock bluetooth

# ---- Clone repo -----------------------------------------------------
echo "Cloning lifx repo..."
if [ ! -d /home/a/lifx ]; then
    git clone perryk@10.0.0.24:/mnt/storage/perryk/git/lifx.git /home/a/lifx
    cd /home/a/lifx
    git checkout ble-sensor
else
    echo "  /home/a/lifx already exists, pulling latest..."
    cd /home/a/lifx
    git pull staging ble-sensor
fi

# ---- Mosquitto bridge config ----------------------------------------
echo "Installing mosquitto bridge config..."
sudo cp /home/a/lifx/deploy/broker-2/mosquitto-bridge.conf \
    /etc/mosquitto/conf.d/glowup-bridge.conf
sudo systemctl restart mosquitto
sudo systemctl enable mosquitto

# ---- BLE sensor service ---------------------------------------------
echo "Installing BLE sensor service..."
sudo cp /home/a/lifx/deploy/broker-2/glowup-ble-sensor.service \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable glowup-ble-sensor

# ---- Verify ----------------------------------------------------------
echo ""
echo "=== Setup complete ==="
echo ""
echo "Mosquitto status:"
sudo systemctl status mosquitto --no-pager -l || true
echo ""
echo "Next steps:"
echo ""
echo "  1. Stop BLE daemon on primary Pi (only one host can hold the"
echo "     GATT connection at a time):"
echo "     ssh pi@10.0.0.48 sudo systemctl stop glowup-ble-sensor"
echo "     ssh pi@10.0.0.48 sudo systemctl disable glowup-ble-sensor"
echo ""
echo "  2. Copy ble_pairing.json from the primary Pi (same controller"
echo "     identity — no re-pairing needed):"
echo "     scp pi@10.0.0.48:~/lifx/ble_pairing.json /home/a/lifx/"
echo ""
echo "  3. Start the BLE sensor daemon:"
echo "     sudo systemctl start glowup-ble-sensor"
echo ""
echo "  4. Verify messages reach the primary broker:"
echo "     mosquitto_sub -h 10.0.0.48 -t 'glowup/ble/#' -v"
