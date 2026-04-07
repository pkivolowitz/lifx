#!/usr/bin/env python3
"""Real-time token usage meter for Claude Code sessions.

Monitors the debug log written by the token shim wrapper and displays
conversation size, context utilization, growth rate, and projected
time until manual compaction.

Install the shim first::

    Set claudeCode.claudeProcessWrapper in VSCode settings to
    the absolute path of tools/token_shim.sh, then reload.

Then in a separate terminal::

    python tools/token_meter.py
    python tools/token_meter.py --once       # print once and exit
    python tools/token_meter.py --raw        # show raw matching lines
    python tools/token_meter.py --ceiling 60 # compact at 60% instead of 50%

The meter reads /tmp/claude-token-debug.log (or --file <path>) and
parses autocompact and API REQUEST lines from the Claude CLI's debug
output.  No Anthropic API key or network access required — all data
comes from the local debug log.

History is persisted to /tmp/claude-token-meter.jsonl so that state
survives across log truncations (the binary truncates every turn)
and across separate ``--once`` invocations.  Use ``--reset`` to
start fresh.

The ``--ceiling`` flag (default 50%) sets the fraction of the effective
window at which you plan to manually compact.  Projections target this
ceiling, not the binary's auto-compact threshold.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.

__version__: str = "2.2"

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default debug log path (must match token_shim.sh).
DEFAULT_LOG: str = "/tmp/claude-token-debug.log"

# Persistent state file — survives log truncations.
DEFAULT_STATE: str = "/tmp/claude-token-meter.jsonl"

# Poll interval for tail-follow mode (seconds).
POLL_INTERVAL: float = 1.0

# Default manual compaction ceiling as fraction of effective window.
# Full context window — percentage displayed is straight utilization.
DEFAULT_CEILING: float = 1.00

# Number of recent turns for windowed growth average.
GROWTH_WINDOW: int = 5

# Regex: ISO 8601 timestamp at the start of debug lines.
# Example: 2026-04-07T01:08:39.270Z
RE_TIMESTAMP: re.Pattern = re.compile(
    r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z'
)

# Regex: autocompact line emitted before each API call.
# Example: autocompact: tokens=63298 threshold=979000 effectiveWindow=992000
RE_AUTOCOMPACT: re.Pattern = re.compile(
    r'autocompact:\s*tokens=(\d+)\s+threshold=(\d+)\s+effectiveWindow=(\d+)'
)

# Regex: API REQUEST line — one per outbound call.
# Example: [API REQUEST] /v1/messages x-client-request-id=... source=sdk
RE_API_REQUEST: re.Pattern = re.compile(
    r'\[API REQUEST\]\s+/v1/messages\s+.*?source=(\S+)'
)

# ANSI color codes.
COLOR_GREEN: str = "\033[32m"
COLOR_YELLOW: str = "\033[33m"
COLOR_RED: str = "\033[31m"
COLOR_RESET: str = "\033[0m"
COLOR_DIM: str = "\033[2m"
COLOR_BOLD: str = "\033[1m"

# Utilization thresholds (fraction of ceiling).
WARN_THRESHOLD: float = 0.70   # yellow above 70% of ceiling
CRIT_THRESHOLD: float = 0.90   # red above 90% of ceiling

# UDP port for poking the overlay widget (must match token_overlay.py).
OVERLAY_UDP_PORT: int = 9147

# Reusable UDP socket for overlay updates — fire and forget.
_overlay_sock: socket.socket = socket.socket(
    socket.AF_INET, socket.SOCK_DGRAM,
)

# Quota scrape file written by token_scrape.py.
QUOTA_PATH: str = "/tmp/claude-usage-quota.json"

# How often to run the background scrape (seconds).
SCRAPE_INTERVAL: float = 180.0  # 3 minutes

# Staleness: ignore quota data older than this (seconds).
QUOTA_STALE: float = 600.0  # 10 minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_log_timestamp(line: str) -> Optional[float]:
    """Extract the ISO timestamp from a debug log line as Unix epoch seconds."""
    m = RE_TIMESTAMP.match(line)
    if not m:
        return None
    try:
        dt: datetime = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%f")
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Accumulator
# ---------------------------------------------------------------------------

@dataclass
class TokenAccumulator:
    """Running state of token usage for a session.

    History is the meter's own record — it does not depend on the
    debug log surviving across turns.  The log is an input stream;
    the accumulator is the persistent store.
    """

    # Manual compaction ceiling as fraction of effective window.
    ceiling_fraction: float = DEFAULT_CEILING

    # Path to the JSONL state file.
    state_path: str = DEFAULT_STATE

    # Current conversation state.
    current_tokens: int = 0
    threshold: int = 0
    effective_window: int = 0

    # History: (unix_timestamp, token_count) per autocompact event.
    token_history: list[tuple[float, int]] = field(default_factory=list)
    # Delta between consecutive token measurements.
    delta_history: list[int] = field(default_factory=list)

    # Counts.
    api_calls_sdk: int = 0
    api_calls_other: int = 0
    compactions: int = 0

    # Peaks.
    peak_tokens: int = 0

    # Timestamp of the latest event loaded from state, used for dedup.
    _latest_state_ts: float = 0.0

    def load_state(self) -> None:
        """Replay persisted events from the JSONL state file."""
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec: dict = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind: str = rec.get("k", "")
                    if kind == "ac":
                        # Autocompact event — replay without re-persisting.
                        self._replay_autocompact(
                            ts=rec["t"],
                            tokens=rec["tok"],
                            threshold=rec["thr"],
                            effective_window=rec["win"],
                        )
                    elif kind == "api":
                        source: str = rec.get("src", "sdk")
                        if source == "sdk":
                            self.api_calls_sdk += 1
                        else:
                            self.api_calls_other += 1
        except OSError:
            # State file unreadable — start fresh.
            pass

    def _replay_autocompact(self, ts: float, tokens: int,
                            threshold: int, effective_window: int) -> None:
        """Replay a single autocompact event from state (no disk write)."""
        if self.token_history and tokens < self.token_history[-1][1]:
            self.compactions += 1
        if self.token_history:
            self.delta_history.append(tokens - self.token_history[-1][1])

        self.current_tokens = tokens
        self.threshold = threshold
        self.effective_window = effective_window
        self.token_history.append((ts, tokens))

        if tokens > self.peak_tokens:
            self.peak_tokens = tokens
        if ts > self._latest_state_ts:
            self._latest_state_ts = ts

    def _append_state(self, record: dict) -> None:
        """Append one JSON record to the state file."""
        try:
            with open(self.state_path, "a") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def is_duplicate(self, log_ts: Optional[float]) -> bool:
        """Return True if this event was already loaded from state."""
        if log_ts is None:
            return False
        return log_ts <= self._latest_state_ts

    def record_autocompact(self, tokens: int, threshold: int,
                           effective_window: int,
                           log_ts: Optional[float] = None) -> None:
        """Record an autocompact measurement and persist it."""
        ts: float = log_ts if log_ts is not None else time.time()

        # Dedup: skip if already loaded from state.
        if self.is_duplicate(log_ts):
            return

        # Detect compaction: token count decreased from previous.
        if self.token_history and tokens < self.token_history[-1][1]:
            self.compactions += 1

        # Compute delta from previous measurement.
        if self.token_history:
            delta: int = tokens - self.token_history[-1][1]
            self.delta_history.append(delta)

        self.current_tokens = tokens
        self.threshold = threshold
        self.effective_window = effective_window
        self.token_history.append((ts, tokens))

        if tokens > self.peak_tokens:
            self.peak_tokens = tokens

        # Persist to state file.
        self._append_state({
            "k": "ac", "t": ts, "tok": tokens,
            "thr": threshold, "win": effective_window,
        })

    def record_api_call(self, source: str,
                        log_ts: Optional[float] = None) -> None:
        """Record an API request by source type and persist it."""
        if self.is_duplicate(log_ts):
            return
        if source == "sdk":
            self.api_calls_sdk += 1
        else:
            self.api_calls_other += 1
        self._append_state({"k": "api", "src": source})

    @property
    def total_api_calls(self) -> int:
        """Total API calls across all sources."""
        return self.api_calls_sdk + self.api_calls_other

    @property
    def ceiling_tokens(self) -> int:
        """Absolute token count at the manual compaction ceiling."""
        return int(self.effective_window * self.ceiling_fraction)

    def utilization_of_ceiling(self) -> float:
        """Fraction of the manual compaction ceiling currently used."""
        ceiling: int = self.ceiling_tokens
        if ceiling == 0:
            return 0.0
        return self.current_tokens / ceiling

    def last_delta(self) -> Optional[int]:
        """Most recent per-turn token delta, or None."""
        if not self.delta_history:
            return None
        return self.delta_history[-1]

    def growth_rate(self) -> Optional[float]:
        """Instantaneous growth rate: tokens per minute.

        Computed from the two most recent autocompact events where
        the token count increased (skips compaction drops).  This is
        the raw derivative d(tokens)/d(t).
        """
        hist = self.token_history
        if len(hist) < 2:
            return None
        for i in range(len(hist) - 1, 0, -1):
            t2, tok2 = hist[i]
            t1, tok1 = hist[i - 1]
            dt: float = t2 - t1
            if dt > 0 and tok2 > tok1:
                return (tok2 - tok1) / (dt / 60.0)
        return None

    def windowed_growth_rate(self, window: int = GROWTH_WINDOW) -> Optional[float]:
        """Smoothed growth rate over the last *window* growth intervals.

        Averages d(tokens)/d(t) across recent growth-only intervals,
        filtering out compaction drops.
        """
        hist = self.token_history
        if len(hist) < 2:
            return None
        rates: list[float] = []
        for i in range(len(hist) - 1, 0, -1):
            if len(rates) >= window:
                break
            t2, tok2 = hist[i]
            t1, tok1 = hist[i - 1]
            dt: float = t2 - t1
            if dt > 0 and tok2 > tok1:
                rates.append((tok2 - tok1) / (dt / 60.0))
        if not rates:
            return None
        return sum(rates) / len(rates)

    def minutes_to_ceiling(self) -> Optional[float]:
        """Estimated wall-clock minutes until manual compaction ceiling.

        Uses windowed growth rate (tokens/minute) directly.
        """
        rate: Optional[float] = self.windowed_growth_rate()
        if rate is None or rate <= 0:
            return None
        headroom: int = self.ceiling_tokens - self.current_tokens
        if headroom <= 0:
            return 0.0
        return headroom / rate

    def session_elapsed_minutes(self) -> Optional[float]:
        """Minutes elapsed since first autocompact event."""
        if len(self.token_history) < 2:
            return None
        first_ts: float = self.token_history[0][0]
        last_ts: float = self.token_history[-1][0]
        return (last_ts - first_ts) / 60.0


# ---------------------------------------------------------------------------
# Log parser
# ---------------------------------------------------------------------------

def parse_line(line: str, acc: TokenAccumulator, *,
               raw: bool = False) -> bool:
    """Parse one debug log line and update the accumulator.

    Returns True if the line contained meter-relevant data.
    """
    log_ts: Optional[float] = parse_log_timestamp(line)

    m = RE_AUTOCOMPACT.search(line)
    if m:
        acc.record_autocompact(
            tokens=int(m.group(1)),
            threshold=int(m.group(2)),
            effective_window=int(m.group(3)),
            log_ts=log_ts,
        )
        if raw:
            print(line.rstrip())
        return True

    m = RE_API_REQUEST.search(line)
    if m:
        acc.record_api_call(source=m.group(1), log_ts=log_ts)
        if raw:
            print(line.rstrip())
        return True

    return False


# ---------------------------------------------------------------------------
# Quota scraping
# ---------------------------------------------------------------------------

def read_quota() -> Optional[list[dict]]:
    """Read the quota file written by token_scrape.py.

    Returns the list of quota buckets, or None if the file is
    missing, stale, or unparseable.
    """
    if not os.path.exists(QUOTA_PATH):
        return None
    try:
        with open(QUOTA_PATH, "r") as f:
            data: dict = json.load(f)
        ts: int = data.get("ts", 0)
        if time.time() - ts > QUOTA_STALE:
            return None
        return data.get("pcts", [])
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _scrape_once() -> None:
    """Run token_scrape.py once, silently."""
    scrape_script: str = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "token_scrape.py",
    )
    if not os.path.exists(scrape_script):
        return
    try:
        subprocess.run(
            [sys.executable, scrape_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def start_scrape_thread() -> None:
    """Run the quota scraper periodically in a background thread."""
    def _loop() -> None:
        while True:
            _scrape_once()
            time.sleep(SCRAPE_INTERVAL)

    t: threading.Thread = threading.Thread(target=_loop, daemon=True)
    t.start()


# Regex to parse "Resets in X hr Y min" or "Resets in Y min" from quota.
RE_RESET_TIMER: re.Pattern = re.compile(
    r'Resets in\s+(?:(\d+)\s*hr?)?\s*(\d+)\s*min'
)


def parse_reset_minutes(reset_str: str) -> Optional[float]:
    """Parse a 'Resets in ...' string to minutes remaining."""
    m = RE_RESET_TIMER.search(reset_str)
    if not m:
        return None
    hours: int = int(m.group(1)) if m.group(1) else 0
    mins: int = int(m.group(2))
    return hours * 60.0 + mins


def will_hit_quota(quota: list[dict],
                   growth_rate_tpm: Optional[float],
                   current_tokens: int,
                   ceiling_tokens: int) -> Optional[str]:
    """Predict whether current growth will exhaust the session quota.

    Compares projected context growth over the remaining session window
    against the session quota headroom.  Returns a colored status string
    or None if insufficient data.
    """
    if growth_rate_tpm is None or growth_rate_tpm <= 0:
        return None

    # Find the "Current session" bucket.
    session: Optional[dict] = None
    for q in quota:
        if "session" in q.get("label", "").lower():
            session = q
            break
    if session is None:
        return None

    reset_min: Optional[float] = parse_reset_minutes(session.get("reset", ""))
    if reset_min is None:
        return None

    session_pct: int = session.get("pct", 0)
    headroom_pct: float = 100.0 - session_pct

    # Estimate pct consumed per minute based on growth rate.
    # Context tokens grow at growth_rate_tpm; each token costs quota.
    # Approximate: if current session is session_pct% used and we know
    # elapsed time, we can project.  But simpler: project session_pct
    # growth linearly from the scrape-time rate.
    # We use: pct_per_min = session_pct / (window_total - reset_min)
    # where window_total is 300 min (5 hours).
    window_total: float = 300.0  # 5-hour session window
    elapsed_min: float = window_total - reset_min
    if elapsed_min <= 0:
        return f"{COLOR_GREEN}OK{COLOR_RESET}"

    pct_per_min: float = session_pct / elapsed_min
    projected_at_reset: float = session_pct + (pct_per_min * reset_min)

    if projected_at_reset >= 95:
        return f"{COLOR_RED}WILL HIT LIMIT ({projected_at_reset:.0f}%){COLOR_RESET}"
    elif projected_at_reset >= 75:
        return f"{COLOR_YELLOW}~{projected_at_reset:.0f}% at reset{COLOR_RESET}"
    else:
        return f"{COLOR_GREEN}~{projected_at_reset:.0f}% at reset{COLOR_RESET}"


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def format_number(n: int) -> str:
    """Format an integer with comma separators."""
    return f"{n:,}"


def utilization_bar(fraction: float, width: int = 30) -> str:
    """Render a colored progress bar for context utilization."""
    filled: int = int(fraction * width)
    filled = min(filled, width)
    empty: int = width - filled

    if fraction >= CRIT_THRESHOLD:
        color = COLOR_RED
    elif fraction >= WARN_THRESHOLD:
        color = COLOR_YELLOW
    else:
        color = COLOR_GREEN

    bar: str = (color + "\u2588" * filled
                + COLOR_DIM + "\u2591" * empty + COLOR_RESET)
    return bar


def format_minutes(minutes: Optional[float]) -> str:
    """Format minutes as Xh Ym or just Ym."""
    if minutes is None:
        return "—"
    if minutes <= 0:
        return f"{COLOR_RED}now{COLOR_RESET}"
    if minutes < 10:
        return f"{COLOR_RED}{minutes:.0f}m{COLOR_RESET}"
    if minutes < 30:
        return f"{COLOR_YELLOW}{minutes:.0f}m{COLOR_RESET}"
    hours: int = int(minutes // 60)
    mins: int = int(minutes % 60)
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def display(acc: TokenAccumulator, *, no_overlay: bool = False) -> None:
    """Print the current meter state, overwriting previous output."""
    _ensure_overlay(no_overlay=no_overlay)
    util: float = acc.utilization_of_ceiling()
    util_pct: str = f"{util * 100:.1f}%"
    last_d: Optional[int] = acc.last_delta()
    instant_rate: Optional[float] = acc.growth_rate()
    smoothed_rate: Optional[float] = acc.windowed_growth_rate()
    minutes_est: Optional[float] = acc.minutes_to_ceiling()
    elapsed: Optional[float] = acc.session_elapsed_minutes()

    last_delta_str: str = "—"
    if last_d is not None:
        if last_d < 0:
            last_delta_str = f"{COLOR_YELLOW}{last_d:+,}{COLOR_RESET}"
        else:
            last_delta_str = f"+{last_d:,}"

    instant_str: str = "—"
    if instant_rate is not None:
        instant_str = f"{instant_rate:,.0f} tok/min"

    smoothed_str: str = "—"
    if smoothed_rate is not None:
        smoothed_str = f"{smoothed_rate:,.0f} tok/min"

    elapsed_str: str = "—"
    if elapsed is not None:
        elapsed_str = format_minutes(elapsed)

    minutes_str: str = format_minutes(minutes_est)

    # Number of display lines (for cursor repositioning).
    line_count: int = 14

    # ANSI: move cursor up to overwrite previous display.
    if hasattr(display, '_displayed'):
        sys.stdout.write(f"\033[{line_count}A")

    bar: str = utilization_bar(util)

    # Read quota data from scraper.
    quota: Optional[list[dict]] = read_quota()

    # Build quota display lines.
    quota_lines: list[str] = []
    quota_poke: list[dict] = []
    if quota:
        for q in quota:
            label: str = q.get("label", "?")
            pct: int = q.get("pct", 0)
            reset: str = q.get("reset", "")
            # Color by severity.
            if pct >= 90:
                qcolor = COLOR_RED
            elif pct >= 70:
                qcolor = COLOR_YELLOW
            else:
                qcolor = COLOR_GREEN
            quota_lines.append(
                f"  {label:<18} {qcolor}{pct:>3}%{COLOR_RESET}  {COLOR_DIM}{reset}{COLOR_RESET}"
            )
            quota_poke.append({"label": label, "pct": pct, "reset": reset})

    # ANSI: move cursor up to overwrite previous display.
    if hasattr(display, '_displayed'):
        sys.stdout.write(f"\033[{display._line_count}A")

    bar: str = utilization_bar(util)

    # Extract the 5hr session quota for prominent display.
    session_quota_str: str = ""
    if quota:
        for q in quota:
            if "session" in q.get("label", "").lower():
                spct: int = q.get("pct", 0)
                sreset: str = q.get("reset", "")
                if spct >= 90:
                    sqcolor = COLOR_RED
                elif spct >= 70:
                    sqcolor = COLOR_YELLOW
                else:
                    sqcolor = COLOR_GREEN
                session_quota_str = (
                    f"  5hr quota: {sqcolor}{spct}%{COLOR_RESET} used"
                    f"  {COLOR_DIM}{sreset}{COLOR_RESET}"
                )
                break

    lines: list[str] = [
        "",
        f"  {COLOR_BOLD}Claude Token Meter v{__version__}{COLOR_RESET}",
        "  ───────────────────────────────────────────────",
        f"  Context:  {format_number(acc.current_tokens)} / "
        f"{format_number(acc.ceiling_tokens)}  ({util_pct})",
        f"  {bar}",
    ]
    if session_quota_str:
        lines.append(session_quota_str)
    lines.extend([
        f"  Peak:     {format_number(acc.peak_tokens)}",
        "  ───────────────────────────────────────────────",
        f"  Last turn delta:     {last_delta_str:>12}",
        f"  Growth rate:      {instant_str:>16}",
        f"  Smoothed ({GROWTH_WINDOW}t):    {smoothed_str:>16}",
        "  ───────────────────────────────────────────────",
        f"  Session elapsed:     {elapsed_str:>12}",
        f"  ~Time to compact:    {minutes_str:>12}",
        f"  API calls: {acc.api_calls_sdk}  Compactions: {acc.compactions}",
    ])
    if quota_lines:
        lines.append("  ───────────────────────────────────────────────")
        lines.extend(quota_lines)
        # Session quota projection: will you hit the limit?
        if quota:
            projection: Optional[str] = will_hit_quota(
                quota, smoothed_rate, acc.current_tokens, acc.ceiling_tokens,
            )
            if projection:
                lines.append(f"  Forecast:        {projection}")
    lines.append("")

    # If the display grew since last render, clear leftover lines.
    prev_count: int = getattr(display, '_line_count', 0)
    if len(lines) < prev_count:
        for _ in range(prev_count - len(lines)):
            lines.append("")

    for line in lines:
        sys.stdout.write(f"\033[2K{line}\n")
    sys.stdout.flush()
    display._displayed = True
    display._line_count = len(lines)

    # Poke the overlay widget via UDP — fire and forget.
    # Strip ANSI escape codes — the overlay is tkinter, not a terminal.
    ansi_strip: re.Pattern = re.compile(r'\033\[[0-9;]*m')
    try:
        poke: dict = {
            "util": util,
            "current": acc.current_tokens,
            "ceiling": acc.ceiling_tokens,
            "time_left": ansi_strip.sub("", minutes_str),
            "smoothed": ansi_strip.sub("", smoothed_str),
            "last_delta": ansi_strip.sub("", last_delta_str),
            "quota": quota_poke if quota_poke else None,
        }
        _overlay_sock.sendto(
            json.dumps(poke).encode("utf-8"),
            ("127.0.0.1", OVERLAY_UDP_PORT),
        )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _read_all_lines(f, acc: TokenAccumulator, *,
                    raw: bool = False) -> None:
    """Read all lines from an open file and parse them."""
    for line in f:
        parse_line(line, acc, raw=raw)


def tail_follow(path: str, acc: TokenAccumulator, *,
                raw: bool = False, once: bool = False,
                no_overlay: bool = False) -> None:
    """Follow the debug log file and update the meter.

    The Claude binary truncates the debug file on every turn, so
    we must detect truncation (file shrinks or inode changes) and
    re-open.  The accumulator preserves its own history in a JSONL
    state file independent of the debug log.
    """
    if not os.path.exists(path):
        print(f"Waiting for log file: {path}")
        print("(Start a Claude Code session with the shim installed)")
        while not os.path.exists(path):
            time.sleep(POLL_INTERVAL)

    with open(path, "r") as f:
        _read_all_lines(f, acc, raw=raw)

        if once:
            display(acc, no_overlay=no_overlay)
            return

        display(acc, no_overlay=no_overlay)
        prev_size: int = os.path.getsize(path)
        prev_inode: int = os.stat(path).st_ino

        # Follow new content, surviving truncations.
        while True:
            try:
                stat = os.stat(path)
            except FileNotFoundError:
                time.sleep(POLL_INTERVAL)
                continue

            if stat.st_ino != prev_inode or stat.st_size < prev_size:
                # File was truncated or replaced — re-open.
                # Dedup in record_autocompact prevents double-counting.
                f.close()
                f = open(path, "r")
                _read_all_lines(f, acc, raw=raw)
                display(acc, no_overlay=no_overlay)
                prev_inode = stat.st_ino
                prev_size = stat.st_size
                continue

            line: str = f.readline()
            if line:
                prev_size = stat.st_size
                if parse_line(line, acc, raw=raw):
                    display(acc, no_overlay=no_overlay)
            else:
                time.sleep(POLL_INTERVAL)


def _overlay_is_running() -> bool:
    """Check if the overlay is already listening on its UDP port."""
    probe: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # If we can bind, nobody is listening — overlay is NOT running.
        probe.bind(("127.0.0.1", OVERLAY_UDP_PORT))
        probe.close()
        return False
    except OSError:
        # Bind failed — port in use, overlay IS running.
        probe.close()
        return True


def _ensure_overlay(no_overlay: bool = False) -> None:
    """Launch the overlay as a detached subprocess if not already running.

    Args:
        no_overlay: If True, skip overlay launch entirely.
    """
    if no_overlay:
        return
    if _overlay_is_running():
        return

    import subprocess
    # Resolve the overlay script path relative to this file.
    overlay_script: str = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "token_overlay.py",
    )
    if not os.path.exists(overlay_script):
        return

    # Launch detached — survives this process exiting.
    subprocess.Popen(
        [sys.executable, overlay_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main() -> None:
    """Entry point."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Real-time token usage meter for Claude Code.",
    )
    parser.add_argument(
        "--file", default=DEFAULT_LOG,
        help=f"Debug log file path (default: {DEFAULT_LOG})",
    )
    parser.add_argument(
        "--state", default=DEFAULT_STATE,
        help=f"Persistent state file path (default: {DEFAULT_STATE})",
    )
    parser.add_argument(
        "--ceiling", type=int, default=int(DEFAULT_CEILING * 100),
        help="Manual compaction ceiling as percent of effective window "
             f"(default: {int(DEFAULT_CEILING * 100)}).",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Print current totals and exit (no follow).",
    )
    parser.add_argument(
        "--raw", action="store_true",
        help="Also print raw matching log lines.",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Truncate the log and state files before monitoring.",
    )
    parser.add_argument(
        "--no-overlay", action="store_true",
        help="Do not launch the overlay widget.",
    )
    args: argparse.Namespace = parser.parse_args()

    if args.reset:
        for path in (args.file, args.state):
            if os.path.exists(path):
                open(path, "w").close()
                print(f"Truncated {path}")

    # Start background quota scraper (runs token_scrape.py every 3 min).
    if not args.once:
        _scrape_once()  # immediate first scrape
        start_scrape_thread()

    acc: TokenAccumulator = TokenAccumulator(
        ceiling_fraction=args.ceiling / 100.0,
        state_path=args.state,
    )

    # Load persisted history first — this is the meter's own record,
    # independent of the debug log.
    acc.load_state()

    try:
        tail_follow(args.file, acc, raw=args.raw, once=args.once,
                    no_overlay=args.no_overlay)
    except KeyboardInterrupt:
        print("\n")
        display(acc, no_overlay=True)
        print("  (final)")


if __name__ == "__main__":
    main()
