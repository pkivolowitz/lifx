#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# test_mqtt.sh — Integration tests for the GlowUp MQTT bridge
#
# Requires:
#   - GlowUp server running with an "mqtt" section in server.json
#   - Mosquitto client tools (mosquitto_pub, mosquitto_sub)
#   - A device IP to test against (default: 10.0.0.23)
#
# Usage:
#   ./test_mqtt.sh                     # test against default device
#   ./test_mqtt.sh 10.0.0.62           # test against a specific device
#   BROKER=192.168.1.50 ./test_mqtt.sh # test against a remote broker
#
# The script plays a short effect, checks state, stops it, resumes,
# and verifies each step via MQTT retained state messages.  It does
# NOT test long-running effects or color streaming.
# ---------------------------------------------------------------------------

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BROKER="${BROKER:-localhost}"
PORT="${PORT:-1883}"
PREFIX="${PREFIX:-glowup}"
DEVICE="${1:-10.0.0.23}"
SUB_TIMEOUT=5          # seconds to wait for retained messages
SETTLE_TIME=3          # seconds to let state publisher catch up

PASS=0
FAIL=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Colors (only if stdout is a terminal).
if [ -t 1 ]; then
    GREEN="\033[32m"
    RED="\033[31m"
    YELLOW="\033[33m"
    BOLD="\033[1m"
    RESET="\033[0m"
else
    GREEN="" RED="" YELLOW="" BOLD="" RESET=""
fi

info()  { echo -e "${BOLD}[INFO]${RESET}  $*"; }
pass()  { echo -e "${GREEN}[PASS]${RESET}  $*"; PASS=$((PASS + 1)); }
fail()  { echo -e "${RED}[FAIL]${RESET}  $*"; FAIL=$((FAIL + 1)); }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }

# Subscribe to a topic and return the first message received.
# Returns empty string (and non-zero exit) on timeout.
mqtt_get() {
    local topic="$1"
    mosquitto_sub -h "$BROKER" -p "$PORT" -t "$topic" -W "$SUB_TIMEOUT" -C 1 2>/dev/null || true
}

# Publish a message.
mqtt_pub() {
    local topic="$1"
    local payload="${2:-}"
    mosquitto_pub -h "$BROKER" -p "$PORT" -t "$topic" -m "$payload"
}

# Extract a JSON field value (simple grep — avoids jq dependency).
# Usage: json_field '{"effect":"cylon"}' "effect"  →  cylon
json_field() {
    local json="$1"
    local field="$2"
    echo "$json" | grep -o "\"$field\":[^,}]*" | head -1 | sed 's/.*://' | tr -d '"  '
}

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

echo ""
echo -e "${BOLD}GlowUp MQTT Bridge — Integration Tests${RESET}"
echo "Broker:  $BROKER:$PORT"
echo "Prefix:  $PREFIX"
echo "Device:  $DEVICE"
echo ""

# Check that mosquitto client tools are installed.
for cmd in mosquitto_pub mosquitto_sub; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found.  Install mosquitto-clients."
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# Test 1: Availability — glowup/status should be "online"
# ---------------------------------------------------------------------------

info "Test 1: Checking availability (${PREFIX}/status)..."
status=$(mqtt_get "${PREFIX}/status")
if [ "$status" = "online" ]; then
    pass "Status is 'online'"
else
    fail "Expected 'online', got '${status:-<empty>}'"
fi

# ---------------------------------------------------------------------------
# Test 2: Device list — glowup/devices should be a non-empty JSON array
# ---------------------------------------------------------------------------

info "Test 2: Checking device list (${PREFIX}/devices)..."
devices=$(mqtt_get "${PREFIX}/devices")
if echo "$devices" | grep -q '"ip"'; then
    count=$(echo "$devices" | grep -o '"ip"' | wc -l | tr -d ' ')
    pass "Device list contains $count device(s)"
else
    fail "Device list missing or empty: '${devices:-<empty>}'"
fi

# ---------------------------------------------------------------------------
# Test 3: Device state — should have a retained state message
# ---------------------------------------------------------------------------

info "Test 3: Checking device state (${PREFIX}/device/${DEVICE}/state)..."
state=$(mqtt_get "${PREFIX}/device/${DEVICE}/state")
if echo "$state" | grep -q '"running"'; then
    pass "Device state published for $DEVICE"
else
    fail "No state for $DEVICE: '${state:-<empty>}'"
fi

# ---------------------------------------------------------------------------
# Test 4: Play command — start an effect via MQTT
# ---------------------------------------------------------------------------

info "Test 4: Playing 'cylon' on $DEVICE via MQTT..."
mqtt_pub "${PREFIX}/device/${DEVICE}/command/play" \
    '{"effect":"cylon","params":{"speed":4.0,"brightness":60}}'

sleep "$SETTLE_TIME"

state=$(mqtt_get "${PREFIX}/device/${DEVICE}/state")
effect=$(json_field "$state" "effect")
running=$(json_field "$state" "running")
overridden=$(json_field "$state" "overridden")

if [ "$effect" = "cylon" ] && [ "$running" = "true" ]; then
    pass "Effect 'cylon' is running on $DEVICE"
else
    fail "Expected running cylon, got effect='${effect}' running='${running}'"
fi

if [ "$overridden" = "true" ]; then
    pass "Device is overridden (scheduler paused)"
else
    fail "Expected overridden=true, got '${overridden}'"
fi

# ---------------------------------------------------------------------------
# Test 5: Stop command — stop the effect
# ---------------------------------------------------------------------------

info "Test 5: Stopping effect on $DEVICE via MQTT..."
mqtt_pub "${PREFIX}/device/${DEVICE}/command/stop" ""

sleep "$SETTLE_TIME"

state=$(mqtt_get "${PREFIX}/device/${DEVICE}/state")
running=$(json_field "$state" "running")
overridden=$(json_field "$state" "overridden")

if [ "$running" = "false" ]; then
    pass "Effect stopped on $DEVICE"
else
    fail "Expected running=false, got '${running}'"
fi

if [ "$overridden" = "true" ]; then
    pass "Override preserved after stop (scheduler still paused)"
else
    fail "Expected overridden=true after stop, got '${overridden}'"
fi

# ---------------------------------------------------------------------------
# Test 6: Resume command — clear override
# ---------------------------------------------------------------------------

info "Test 6: Resuming $DEVICE via MQTT (clearing override)..."
mqtt_pub "${PREFIX}/device/${DEVICE}/command/resume" ""

sleep "$SETTLE_TIME"

state=$(mqtt_get "${PREFIX}/device/${DEVICE}/state")
overridden=$(json_field "$state" "overridden")

if [ "$overridden" = "false" ]; then
    pass "Override cleared — scheduler can resume"
else
    fail "Expected overridden=false after resume, got '${overridden}'"
fi

# ---------------------------------------------------------------------------
# Test 7: Power command — turn device off and back on
# ---------------------------------------------------------------------------

info "Test 7: Powering off $DEVICE via MQTT..."
mqtt_pub "${PREFIX}/device/${DEVICE}/command/power" '{"on":false}'

sleep "$SETTLE_TIME"

state=$(mqtt_get "${PREFIX}/device/${DEVICE}/state")
overridden=$(json_field "$state" "overridden")

if [ "$overridden" = "true" ]; then
    pass "Power off set override on $DEVICE"
else
    fail "Expected overridden=true after power off, got '${overridden}'"
fi

info "Powering $DEVICE back on..."
mqtt_pub "${PREFIX}/device/${DEVICE}/command/power" '{"on":true}'
sleep 1

info "Resuming $DEVICE to clear override..."
mqtt_pub "${PREFIX}/device/${DEVICE}/command/resume" ""
sleep "$SETTLE_TIME"

state=$(mqtt_get "${PREFIX}/device/${DEVICE}/state")
overridden=$(json_field "$state" "overridden")

if [ "$overridden" = "false" ]; then
    pass "Device restored to scheduler control"
else
    fail "Expected overridden=false after cleanup, got '${overridden}'"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
TOTAL=$((PASS + FAIL))
echo -e "${BOLD}Results: ${GREEN}${PASS} passed${RESET}, ${RED}${FAIL} failed${RESET} (${TOTAL} total)"
echo ""

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
