#!/usr/bin/env python3
"""Scrape Claude usage quota from Safari.

Uses AppleScript to execute JavaScript in the Safari tab showing
claude.ai/settings/usage.  Extracts percentage values with surrounding
text context and writes them to a JSON file for token_meter.py.

Prerequisites:
    Safari → Develop menu → "Allow JavaScript from Apple Events"
    (Enable Develop menu: Safari → Settings → Advanced →
    "Show features for web developers".)

Usage::

    python tools/token_scrape.py            # scrape once, write to /tmp
    python tools/token_scrape.py --dump     # also print result to stdout

Output: /tmp/claude-usage-quota.json
    {"ts": <epoch>, "pcts": [{"before": "...", "pct": 65, "after": "..."}, ...]}
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.

__version__: str = "1.0"

import json
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Output file — token_meter.py reads this.
OUTPUT_PATH: str = "/tmp/claude-usage-quota.json"

# URL of the usage page.
USAGE_URL: str = "https://claude.ai/settings/usage"

# JavaScript: split page text into lines, find each "N% used" and
# look backwards for the associated label and reset timer.
# Collapsed to one line to avoid AppleScript multiline string issues.
JS_EXTRACT: str = (
    "(function(){"
    "var lines=document.body.innerText.split('\\n');"
    "var results=[];"
    "for(var i=0;i<lines.length;i++){"
    "var m=lines[i].match(/^(\\d+)%\\s*used/);"
    "if(m){"
    "var pct=parseInt(m[1],10);"
    "var reset='',label='';"
    "for(var j=i-1;j>=Math.max(0,i-8);j--){"
    "var ln=lines[j].trim();"
    "if(!ln)continue;"
    "if(ln.match(/^Resets/)&&!reset){reset=ln;}"
    "else if(!ln.match(/^Resets/)&&!ln.match(/^Learn/)&&!ln.match(/^\\$/)&&!ln.match(/^Turn/)&&!label){label=ln;}"
    "if(reset&&label)break;"
    "}"
    "results.push({label:label,reset:reset,pct:pct});"
    "}}"
    "return JSON.stringify(results);"
    "})()"
)


# ---------------------------------------------------------------------------
# AppleScript builder
# ---------------------------------------------------------------------------

def build_applescript(js: str) -> str:
    """Build the AppleScript that drives Safari.

    Escapes the JavaScript for safe embedding inside an AppleScript
    string literal (backslashes and double quotes).
    """
    # Escape for AppleScript double-quoted string.
    escaped: str = js.replace("\\", "\\\\").replace('"', '\\"')

    # Note: AppleScript curly braces in record literals are literal,
    # not Python f-string interpolation — doubled here to escape.
    return f'''tell application "Safari"
    set usageURL to "{USAGE_URL}"
    set foundTab to false
    set targetTab to missing value
    set targetWindow to missing value

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

    if not foundTab then
        if (count of windows) = 0 then
            make new document with properties {{URL:usageURL}}
            set targetWindow to window 1
            set targetTab to current tab of targetWindow
        else
            tell window 1
                set current tab to (make new tab with properties {{URL:usageURL}})
                set targetTab to current tab
            end tell
            set targetWindow to window 1
        end if
    end if

    repeat 20 times
        delay 0.5
        try
            if (do JavaScript "document.readyState" in targetTab) is "complete" then
                exit repeat
            end if
        end try
    end repeat

    delay 1.5

    set jsResult to do JavaScript "{escaped}" in targetTab
    return jsResult
end tell'''


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point."""
    dump: bool = "--dump" in sys.argv
    raw_text: bool = "--raw-text" in sys.argv

    # In raw-text mode, just dump the page's innerText for inspection.
    js: str = "document.body.innerText" if raw_text else JS_EXTRACT
    script: str = build_applescript(js)

    try:
        result: subprocess.CompletedProcess = subprocess.run(
            ["osascript"],
            input=script,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("ERROR: osascript timed out after 30s", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(f"ERROR: osascript failed: {result.stderr.strip()}",
              file=sys.stderr)
        sys.exit(1)

    raw: str = result.stdout.strip()

    if raw_text:
        print(raw)
        return

    # Parse the JSON array returned by the JavaScript.
    try:
        pcts: list = json.loads(raw)
    except json.JSONDecodeError:
        # If parsing fails, preserve the raw output for debugging.
        pcts = [{"raw": raw, "error": "JSON parse failed"}]

    output: dict = {"ts": int(time.time()), "pcts": pcts}

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f)

    if dump:
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
