#!/usr/bin/env bash
# Rotate the clock display remotely from any machine.
#
# Usage:
#   ./tools/clock_rotate.sh 0      # normal (landscape)
#   ./tools/clock_rotate.sh 90     # portrait, rotated right
#   ./tools/clock_rotate.sh 180    # upside down
#   ./tools/clock_rotate.sh 270    # portrait, rotated left
#   ./tools/clock_rotate.sh        # show current rotation
#
# Clock Pi: a@10.0.0.148 (Wayland + wlr-randr)
# Perry Kivolowitz, 2026. MIT License.

set -euo pipefail

CLOCK_HOST="a@10.0.0.148"
DISPLAY_NAME="HDMI-A-1"

if [ $# -eq 0 ]; then
    # Show current rotation.
    ssh "${CLOCK_HOST}" "wlr-randr" 2>/dev/null | grep -A1 Transform
    exit 0
fi

case "${1}" in
    0)   TRANSFORM="normal" ;;
    90)  TRANSFORM="90"     ;;
    180) TRANSFORM="180"    ;;
    270) TRANSFORM="270"    ;;
    *)   echo "Usage: $0 [0|90|180|270]"; exit 1 ;;
esac

ssh "${CLOCK_HOST}" "wlr-randr --output ${DISPLAY_NAME} --transform ${TRANSFORM}"
echo "Clock rotated to ${1}°"
