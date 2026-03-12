#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# glowup.sh — GlowUp remote control from macOS
#
# A single script that handles play, stop, resume, and power commands
# for any device or group.  Designed to be called directly from Terminal,
# wrapped in an Apple Shortcut via "Run Shell Script", or pinned to the
# Dock as a .command file.
#
# Usage:
#   glowup.sh play   porch aurora [param=value ...]
#   glowup.sh stop   porch
#   glowup.sh resume porch
#   glowup.sh off    porch
#   glowup.sh on     porch
#   glowup.sh status [device]
#   glowup.sh list
#
# Environment:
#   GLOWUP_HOST   Server hostname  (default: raspberrypi.local)
#   GLOWUP_PORT   Server port      (default: 8420)
#   GLOWUP_TOKEN  Auth token        (default: reads from ~/.glowup_token)
# ---------------------------------------------------------------------------

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST="${GLOWUP_HOST:-10.0.0.48}"
PORT="${GLOWUP_PORT:-8420}"
BASE="http://${HOST}:${PORT}"

# Token: environment variable, or file, or bail.
if [ -n "${GLOWUP_TOKEN:-}" ]; then
    TOKEN="$GLOWUP_TOKEN"
elif [ -f "$HOME/.glowup_token" ]; then
    TOKEN="$(cat "$HOME/.glowup_token" | tr -d '[:space:]')"
else
    echo "Error: No auth token found."
    echo "Set GLOWUP_TOKEN or put your token in ~/.glowup_token"
    exit 1
fi

# ---------------------------------------------------------------------------
# Groups → device ID mapping
# ---------------------------------------------------------------------------

resolve_target() {
    local name="$1"
    case "$name" in
        porch)       echo "group:porch" ;;
        living-room) echo "group:living-room" ;;
        all)         echo "group:all" ;;
        testing)     echo "group:testing" ;;
        group:*)     echo "$name" ;;
        10.*)        echo "$name" ;;
        *)           echo "$name" ;;
    esac
}

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

auth_header="Authorization: Bearer ${TOKEN}"

api_get() {
    curl -sf -H "$auth_header" "${BASE}$1"
}

api_post() {
    local url="$1"
    local body="${2:-}"
    if [ -n "$body" ]; then
        curl -sf -X POST -H "$auth_header" \
             -H "Content-Type: application/json" \
             -d "$body" "${BASE}${url}"
    else
        curl -sf -X POST -H "$auth_header" "${BASE}${url}"
    fi
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_play() {
    local target
    target=$(resolve_target "${1:?Usage: glowup.sh play TARGET EFFECT [param=value ...]}")
    local effect="${2:?Usage: glowup.sh play TARGET EFFECT [param=value ...]}"
    shift 2

    # Build params JSON from remaining key=value args.
    local params="{"
    local first=true
    for arg in "$@"; do
        local key="${arg%%=*}"
        local val="${arg#*=}"
        if [ "$first" = true ]; then first=false; else params+=","; fi
        # Try to keep numbers unquoted.
        if [[ "$val" =~ ^[0-9]+\.?[0-9]*$ ]]; then
            params+="\"${key}\":${val}"
        else
            params+="\"${key}\":\"${val}\""
        fi
    done
    params+="}"

    local body="{\"effect\":\"${effect}\",\"params\":${params}}"
    echo "Playing ${effect} on ${target}..."
    api_post "/api/devices/${target}/play" "$body"
    echo "OK"
}

cmd_stop() {
    local target
    target=$(resolve_target "${1:?Usage: glowup.sh stop TARGET}")
    echo "Stopping ${target}..."
    api_post "/api/devices/${target}/stop"
    echo "OK"
}

cmd_resume() {
    local target
    target=$(resolve_target "${1:?Usage: glowup.sh resume TARGET}")
    echo "Resuming schedule on ${target}..."
    api_post "/api/devices/${target}/resume"
    echo "OK"
}

cmd_power() {
    local on_off="$1"
    local target
    target=$(resolve_target "${2:?Usage: glowup.sh on|off TARGET}")
    echo "Powering ${on_off} ${target}..."
    api_post "/api/devices/${target}/power" "{\"on\":${on_off}}"
    echo "OK"
}

cmd_status() {
    if [ -n "${1:-}" ]; then
        local target
        target=$(resolve_target "$1")
        api_get "/api/devices/${target}/status" | python3 -m json.tool
    else
        api_get "/api/devices" | python3 -m json.tool
    fi
}

cmd_list() {
    api_get "/api/effects" | python3 -m json.tool
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

case "${1:-help}" in
    play)    shift; cmd_play "$@" ;;
    stop)    shift; cmd_stop "$@" ;;
    resume)  shift; cmd_resume "$@" ;;
    on)      shift; cmd_power true "$@" ;;
    off)     shift; cmd_power false "$@" ;;
    status)  shift; cmd_status "$@" ;;
    list)    cmd_list ;;
    help|*)
        echo "GlowUp Remote Control"
        echo ""
        echo "Usage:"
        echo "  glowup.sh play TARGET EFFECT [param=value ...]"
        echo "  glowup.sh stop TARGET"
        echo "  glowup.sh resume TARGET"
        echo "  glowup.sh on TARGET"
        echo "  glowup.sh off TARGET"
        echo "  glowup.sh status [TARGET]"
        echo "  glowup.sh list"
        echo ""
        echo "Targets: porch, living-room, all, testing, or any IP"
        echo ""
        echo "Examples:"
        echo "  glowup.sh play porch aurora speed=10 brightness=80"
        echo "  glowup.sh play living-room cylon hue=240"
        echo "  glowup.sh stop porch"
        echo "  glowup.sh resume porch"
        echo "  glowup.sh off all"
        echo "  glowup.sh status porch"
        ;;
esac
