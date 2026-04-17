#!/bin/bash
# token_scrape.sh — scrape Claude usage quota from Safari.
#
# Uses AppleScript to execute JavaScript in the Safari tab showing
# claude.ai/settings/usage.  Extracts all percentage values with
# surrounding text context and writes them to a JSON file for
# token_meter.py to consume.
#
# Prerequisites:
#   Safari → Develop menu → "Allow JavaScript from Apple Events" must
#   be enabled.  (Enable the Develop menu in Safari → Settings →
#   Advanced → "Show features for web developers".)
#
# Usage:
#   bash tools/token_scrape.sh          # scrape once, write to /tmp
#   bash tools/token_scrape.sh --dump   # also print raw result to stdout
#
# Output: /tmp/claude-usage-quota.json
#   {"ts": <epoch>, "pcts": [{"before": "...", "pct": 65, "after": "..."}, ...]}

set -euo pipefail

OUTPUT="/tmp/claude-usage-quota.json"
USAGE_URL="https://claude.ai/settings/usage"
DUMP=false
[[ "${1:-}" == "--dump" ]] && DUMP=true

# JavaScript to extract all percentages with surrounding context.
# Returns a JSON array of {before, pct, after} objects.
read -r -d '' JS_EXTRACT << 'JSEOF' || true
(function() {
    var body = document.body.innerText;
    var results = [];
    var re = /(.{0,50})(\d+)\s*%(.{0,50})/g;
    var m;
    while ((m = re.exec(body)) !== null) {
        results.push({
            before: m[1].trim(),
            pct: parseInt(m[2], 10),
            after: m[3].trim()
        });
    }
    return JSON.stringify(results);
})()
JSEOF

# AppleScript: find or open the usage tab, refresh, wait, scrape.
RESULT=$(osascript << ASEOF
tell application "Safari"
    set usageURL to "${USAGE_URL}"
    set foundTab to false
    set targetTab to missing value
    set targetWindow to missing value

    -- Search all windows/tabs for an existing usage page.
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t starts with usageURL then
                set current tab of w to t
                set URL of t to usageURL
                set foundTab to true
                set targetTab to t
                set targetWindow to w
                exit repeat
            end if
        end repeat
        if foundTab then exit repeat
    end repeat

    -- If no existing tab, open one.
    if not foundTab then
        if (count of windows) = 0 then
            make new document with properties {URL:usageURL}
            set targetWindow to window 1
            set targetTab to current tab of targetWindow
        else
            tell window 1
                set current tab to (make new tab with properties {URL:usageURL})
                set targetTab to current tab
            end tell
            set targetWindow to window 1
        end if
    end if

    -- Wait for the page to finish loading (up to 10 seconds).
    repeat 20 times
        delay 0.5
        try
            if (do JavaScript "document.readyState" in targetTab) is "complete" then
                exit repeat
            end if
        end try
    end repeat

    -- Small extra delay for JS-rendered content.
    delay 1

    -- Extract percentage data.
    set jsResult to do JavaScript "${JS_EXTRACT}" in targetTab
    return jsResult
end tell
ASEOF
)

# Wrap with timestamp and write to output file.
EPOCH=$(date +%s)
echo "{\"ts\":${EPOCH},\"pcts\":${RESULT}}" > "$OUTPUT"

if $DUMP; then
    echo "$RESULT" | python3 -m json.tool 2>/dev/null || echo "$RESULT"
fi
