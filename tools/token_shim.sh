#!/bin/bash
# token_shim.sh — Claude Code process wrapper for token metering.
#
# This script is pointed to by the VSCode setting:
#   claudeCode.claudeProcessWrapper
#
# It finds the real Claude binary, passes all arguments through,
# strips --debug-to-stderr (which prevents file output), and
# appends --debug-file so token usage data is written to a
# parseable log file.  The companion tool (token_meter.py) monitors
# that log.
#
# Setup (one time, survives extension updates):
#   1. Open VSCode Settings (Cmd+,)
#   2. Search: claudeProcessWrapper
#   3. Set to the absolute path of this script, e.g.:
#      /Users/mortimer.snerd/lifx/tools/token_shim.sh
#   4. Reload VSCode window (Cmd+Shift+P → Reload Window)
#
# Then in a separate terminal:
#   python tools/token_meter.py
#
# To disable: clear the claudeProcessWrapper setting and reload.
#
# The wrapper auto-discovers the Claude binary by scanning the
# extension directory, so extension updates are handled automatically.
#
# Why strip --debug-to-stderr?
#   The VSCode extension adds --debug-to-stderr to every launch.
#   When present, the binary routes ALL debug output to stderr and
#   never opens the --debug-file.  Stripping it lets --debug-file
#   take effect.  VSCode communicates with Claude via stdin/stdout
#   (stream-json), not stderr, so this is safe.

set -euo pipefail

LOG_FILE="/tmp/claude-token-debug.log"

# Find the most recent Claude Code extension binary.
find_binary() {
    local ext_dir
    ext_dir=$(ls -dt "$HOME/.vscode/extensions"/anthropic.claude-code-*-darwin-arm64 2>/dev/null | head -1)
    if [[ -z "$ext_dir" ]]; then
        ext_dir=$(ls -dt "$HOME/.vscode/extensions"/anthropic.claude-code-*-darwin-x64 2>/dev/null | head -1)
    fi
    if [[ -z "$ext_dir" ]]; then
        echo "ERROR: Claude Code extension not found" >&2
        exit 1
    fi
    echo "$ext_dir/resources/native-binary/claude"
}

BINARY=$(find_binary)

if [[ ! -x "$BINARY" ]]; then
    echo "ERROR: Claude binary not executable: $BINARY" >&2
    exit 1
fi

# Filter out --debug-to-stderr and -d2e from arguments.
# When present, the binary sends debug output to stderr only,
# ignoring --debug-file entirely.
FILTERED_ARGS=()
for arg in "$@"; do
    if [[ "$arg" != "--debug-to-stderr" && "$arg" != "-d2e" ]]; then
        FILTERED_ARGS+=("$arg")
    fi
done

exec "$BINARY" "${FILTERED_ARGS[@]}" --debug-file "$LOG_FILE"
