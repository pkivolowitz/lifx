#!/bin/bash
#
# Demo all self-contained 2D matrix effects in sequence for video
# capture.  Speaks each effect's name via macOS ``say`` first, then
# runs the effect for 30 seconds, kills it, and moves on to the
# next.  Cleans up on exit so a Ctrl-C doesn't leave the lights
# stuck on the last effect.
#
# Defaults:
#   - 20 seconds per effect.
#   - One-time intro spoken before the first effect.
#   - Each effect's announcement is "<name> for 20 seconds".
#   - All effects run with their default parameters (the new effects
#     default to --rate medium; the older effects use whatever their
#     own per-param defaults are).
#   - 1-second pause after each announcement before the visual starts
#     so the spoken word doesn't overlap the first lit frame.
#
# Usage:
#   tools/demo_2d_effects.sh <ip-or-label>
#
# Where:
#   <ip-or-label>  passed straight through to ``glowup.py play --ip``.
#                  Either an IPv4 address (10.0.0.214) or a registered
#                  device label / sub-device label.
#

set -u

if [ $# -lt 1 ]; then
    echo "Usage: $0 <ip-or-label>" >&2
    exit 1
fi

TARGET="$1"
RUN_SECONDS=20
BRIGHTNESS=20
GAP_AFTER_SAY_SECONDS=1
INTRO_TEXT="LIFX Ceiling 15 inch glow up effects by Perry Kivolowitz. \
All effects rendered at $BRIGHTNESS percent brightness"

# Repo root, resolved from this script's location so the runner works
# from any cwd.  Avoids the ``cd repo-root && ./tools/...`` ritual.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GLOWUP="$REPO_ROOT/glowup.py"

if [ ! -f "$GLOWUP" ]; then
    echo "ERROR: cannot find glowup.py at $GLOWUP" >&2
    exit 1
fi

# Effect → spoken name pairs.  Effect names match the registry; the
# spoken side is hand-tuned so ``say`` reads them naturally rather
# than spelling out underscores or "two-dee" suffixes.
EFFECTS=(
    "arcs|arcs"
    "boing_ball|boing ball"
    "breathe|breathe"
    "conway2d|Conway's Game of Life"
    "fireworks2d|fireworks"
    "matrix_rain|Matrix rain"
    "plasma2d|plasma"
    "pong2d|pong"
    "radar_scope|radar scope"
    "ripple2d|ripples"
    "twinkle|twinkle"
)

# PID of the currently-running glowup.py play process, captured so
# the cleanup trap can kill it on early exit.
GLOWUP_PID=""

cleanup() {
    if [ -n "$GLOWUP_PID" ]; then
        kill "$GLOWUP_PID" 2>/dev/null || true
        wait "$GLOWUP_PID" 2>/dev/null || true
    fi
    # Best-effort blackout so the recording ends on a clean dark frame.
    python3 "$GLOWUP" off --ip "$TARGET" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# One-time intro spoken before the first effect.  Followed by a
# pause so the recording has a moment of silence between the title
# card and the first effect's announcement.
say "$INTRO_TEXT"
sleep "$GAP_AFTER_SAY_SECONDS"

for entry in "${EFFECTS[@]}"; do
    name="${entry%%|*}"
    spoken="${entry##*|}"

    say "$spoken for $RUN_SECONDS seconds"
    sleep "$GAP_AFTER_SAY_SECONDS"

    python3 "$GLOWUP" play "$name" --ip "$TARGET" \
        --brightness "$BRIGHTNESS" &
    GLOWUP_PID=$!
    sleep "$RUN_SECONDS"

    kill "$GLOWUP_PID" 2>/dev/null || true
    wait "$GLOWUP_PID" 2>/dev/null || true
    GLOWUP_PID=""
done
