"""glowup-top — real-time infrastructure monitor for GlowUp processes.

Curses TUI that subscribes to MQTT heartbeats and LWT status from
all GlowUp adapter processes.  Shows PID, uptime, RSS, heartbeat
age, and adapter-specific detail for each process.

Designed to run on the Pi via SSH.  Connects to localhost:1883 by
default.  Can also run from any machine that can reach the MQTT
broker.

Usage::

    python tools/glowup_top.py
    python tools/glowup_top.py --broker <hub-broker>
    python tools/glowup_top.py --broker <hub-broker> --port 1883

Keys:
    q / Ctrl-C  Quit
    r           Force refresh
    s           Toggle sort (name / heartbeat age / RSS)

Color coding:
    GREEN   — heartbeat received within 30 seconds
    YELLOW  — heartbeat stale (30-60 seconds)
    RED     — offline (LWT) or heartbeat > 60 seconds
    CYAN    — header/labels
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import argparse
import curses
import json
import logging
import subprocess
import sys
import threading
import time
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.glowup_top")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default MQTT broker (Pi 5 runs mosquitto on localhost).
DEFAULT_BROKER: str = "localhost"

# Default MQTT broker port.
DEFAULT_PORT: int = 1883

# MQTT topics to subscribe to.
TOPIC_HEARTBEAT: str = "glowup/adapter/+/heartbeat"
TOPIC_STATUS: str = "glowup/adapter/+/status"

# Heartbeat staleness thresholds (seconds).
STALE_WARN_S: float = 30.0
STALE_CRIT_S: float = 60.0

# Screen refresh interval (seconds).
REFRESH_INTERVAL: float = 1.0

# Known adapter IDs — display order.
ADAPTER_ORDER: list[str] = [
    "keepalive", "zigbee", "vivint", "nvr",
    "printer", "matter", "ble",
]

# Column widths.
COL_NAME: int = 12
COL_PID: int = 8
COL_STATUS: int = 10
COL_UPTIME: int = 12
COL_RSS: int = 10
COL_HB_AGE: int = 10
COL_DETAIL: int = 0  # fills remaining width

# Systemd service name patterns.
# The template unit is glowup-adapter@{name}.service.
SVC_TEMPLATE: str = "glowup-adapter@{name}.service"
SVC_KEEPALIVE: str = "glowup-keepalive.service"
SVC_SERVER: str = "glowup-server.service"

# Sort modes.
SORT_NAME: int = 0
SORT_HB_AGE: int = 1
SORT_RSS: int = 2
SORT_LABELS: list[str] = ["name", "heartbeat age", "RSS"]


# ---------------------------------------------------------------------------
# ProcessInfo — per-adapter state from MQTT
# ---------------------------------------------------------------------------

class ProcessInfo:
    """Cached state for one adapter process from MQTT heartbeats."""

    def __init__(self, adapter_id: str) -> None:
        """Initialize with unknown state."""
        self.adapter_id: str = adapter_id
        self.online: bool = False
        self.pid: int = 0
        self.uptime_s: float = 0.0
        self.rss_mb: float = 0.0
        self.state: str = "unknown"
        self.detail: dict[str, Any] = {}
        self.last_heartbeat_ts: float = 0.0
        self.heartbeat_age: float = 999.0

    def update_heartbeat(self, data: dict[str, Any]) -> None:
        """Update from a heartbeat message."""
        self.pid = data.get("pid", 0)
        self.uptime_s = data.get("uptime_s", 0.0)
        self.rss_mb = data.get("rss_mb", 0.0)
        self.state = data.get("state", "unknown")
        self.detail = data.get("detail", {})
        self.last_heartbeat_ts = time.monotonic()
        self.online = True

    def update_status(self, online: bool) -> None:
        """Update from an LWT status message."""
        self.online = online
        if not online:
            self.state = "offline"

    def refresh_age(self) -> None:
        """Recompute heartbeat age from current time."""
        if self.last_heartbeat_ts > 0:
            self.heartbeat_age = time.monotonic() - self.last_heartbeat_ts
        else:
            self.heartbeat_age = 999.0


# ---------------------------------------------------------------------------
# GlowUpTop — the TUI
# ---------------------------------------------------------------------------

class GlowUpTop:
    """Curses-based infrastructure monitor.

    Subscribes to MQTT heartbeats and displays process health
    in a continuously updating terminal UI.

    Args:
        broker: MQTT broker address.
        port:   MQTT broker port.
    """

    def __init__(self, broker: str, port: int) -> None:
        """Initialize the monitor."""
        self._broker: str = broker
        self._port: int = port
        self._lock: threading.Lock = threading.Lock()
        self._processes: dict[str, ProcessInfo] = {}
        self._server_info: dict[str, Any] = {}
        self._sort_mode: int = SORT_NAME
        self._running: bool = True
        self._mqtt_connected: bool = False
        self._client: Any = None

        # Pre-populate known adapters.
        for aid in ADAPTER_ORDER:
            self._processes[aid] = ProcessInfo(aid)

    def run(self, stdscr: Any) -> None:
        """Main loop — subscribe to MQTT and render the TUI.

        Args:
            stdscr: Curses standard screen.
        """
        # Curses setup.
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(int(REFRESH_INTERVAL * 1000))

        # Color pairs.
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)   # healthy
        curses.init_pair(2, curses.COLOR_YELLOW, -1)  # stale
        curses.init_pair(3, curses.COLOR_RED, -1)     # offline/critical
        curses.init_pair(4, curses.COLOR_CYAN, -1)    # headers
        curses.init_pair(5, curses.COLOR_WHITE, -1)   # normal

        # Start MQTT listener thread.
        mqtt_thread: threading.Thread = threading.Thread(
            target=self._mqtt_loop, daemon=True, name="mqtt-sub",
        )
        mqtt_thread.start()

        # Render loop.
        while self._running:
            self._refresh_ages()
            self._render(stdscr)
            key: int = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):  # q, Q, ESC
                self._running = False
            elif key in (ord("r"), ord("R")):
                pass  # Force refresh — next iteration redraws
            elif key in (ord("s"), ord("S")):
                self._sort_mode = (self._sort_mode + 1) % len(SORT_LABELS)

        # Cleanup.
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()

    # ------------------------------------------------------------------
    # MQTT
    # ------------------------------------------------------------------

    def _mqtt_loop(self) -> None:
        """Connect to MQTT and subscribe to heartbeat/status topics."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            return

        paho_v2: bool = hasattr(mqtt, "CallbackAPIVersion")
        client_id: str = f"glowup-top-{int(time.time())}"

        if paho_v2:
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
            )
        else:
            self._client = mqtt.Client(client_id=client_id)

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        try:
            self._client.connect(self._broker, self._port)
            self._client.loop_forever()
        except Exception as exc:
            logger.debug("MQTT connection failed: %s", exc)

    def _on_connect(self, client: Any, userdata: Any, *args: Any) -> None:
        """Subscribe to heartbeat and status topics."""
        client.subscribe(TOPIC_HEARTBEAT, qos=0)
        client.subscribe(TOPIC_STATUS, qos=1)
        self._mqtt_connected = True

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Route incoming MQTT messages to process state."""
        topic: str = msg.topic
        parts: list[str] = topic.split("/")
        # glowup/adapter/{id}/heartbeat or glowup/adapter/{id}/status
        if len(parts) < 4:
            return
        adapter_id: str = parts[2]

        with self._lock:
            if adapter_id not in self._processes:
                self._processes[adapter_id] = ProcessInfo(adapter_id)
            proc: ProcessInfo = self._processes[adapter_id]

            if parts[3] == "heartbeat":
                try:
                    data: dict[str, Any] = json.loads(msg.payload)
                    proc.update_heartbeat(data)
                except (json.JSONDecodeError, ValueError):
                    pass
            elif parts[3] == "status":
                payload: str = msg.payload.decode("utf-8", errors="replace")
                proc.update_status(payload.strip().lower() == "online")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _refresh_ages(self) -> None:
        """Update heartbeat ages for all processes."""
        with self._lock:
            for proc in self._processes.values():
                proc.refresh_age()

    def _get_sorted_processes(self) -> list[ProcessInfo]:
        """Return processes sorted by current sort mode."""
        with self._lock:
            procs: list[ProcessInfo] = list(self._processes.values())

        if self._sort_mode == SORT_NAME:
            # Use ADAPTER_ORDER for known, alphabetical for unknown.
            def name_key(p: ProcessInfo) -> tuple[int, str]:
                """Sort by ADAPTER_ORDER position, then alphabetically."""
                try:
                    idx: int = ADAPTER_ORDER.index(p.adapter_id)
                except ValueError:
                    idx = len(ADAPTER_ORDER)
                return (idx, p.adapter_id)
            procs.sort(key=name_key)
        elif self._sort_mode == SORT_HB_AGE:
            procs.sort(key=lambda p: p.heartbeat_age, reverse=True)
        elif self._sort_mode == SORT_RSS:
            procs.sort(key=lambda p: p.rss_mb, reverse=True)

        return procs

    def _color_for_process(self, proc: ProcessInfo) -> int:
        """Return curses color pair for process health state."""
        if not proc.online or proc.state == "offline":
            return curses.color_pair(3)  # red
        if proc.heartbeat_age > STALE_CRIT_S:
            return curses.color_pair(3)  # red
        if proc.heartbeat_age > STALE_WARN_S:
            return curses.color_pair(2)  # yellow
        return curses.color_pair(1)  # green

    def _format_uptime(self, seconds: float) -> str:
        """Format uptime in human-readable form."""
        if seconds < 60:
            return f"{seconds:.0f}s"
        if seconds < 3600:
            return f"{seconds / 60:.0f}m"
        if seconds < 86400:
            h: int = int(seconds // 3600)
            m: int = int((seconds % 3600) // 60)
            return f"{h}h{m:02d}m"
        d: int = int(seconds // 86400)
        h = int((seconds % 86400) // 3600)
        return f"{d}d{h}h"

    def _format_hb_age(self, age: float) -> str:
        """Format heartbeat age."""
        if age > 900:
            return "never"
        if age < 1:
            return "<1s"
        return f"{age:.0f}s"

    def _format_detail(self, proc: ProcessInfo, max_width: int) -> str:
        """Format adapter-specific detail for display."""
        if not proc.detail:
            return ""
        # Pick the most interesting keys to show.
        parts: list[str] = []
        for key, val in proc.detail.items():
            if key == "running":
                continue
            if isinstance(val, float):
                parts.append(f"{key}={val:.1f}")
            elif isinstance(val, bool):
                parts.append(f"{key}={'Y' if val else 'N'}")
            elif isinstance(val, int):
                parts.append(f"{key}={val}")
            elif isinstance(val, str):
                parts.append(f"{key}={val}")
            elif isinstance(val, dict):
                parts.append(f"{key}={{...}}")
            elif isinstance(val, list):
                parts.append(f"{key}=[{len(val)}]")
        text: str = "  ".join(parts)
        if len(text) > max_width:
            text = text[:max_width - 1] + "\u2026"
        return text

    def _render(self, stdscr: Any) -> None:
        """Draw the full screen."""
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        header_color: int = curses.color_pair(4)
        normal_color: int = curses.color_pair(5)

        # Title bar.
        title: str = f" glowup-top v{__version__}"
        mqtt_status: str = (
            f"MQTT: {self._broker}:{self._port} "
            + ("\u2713" if self._mqtt_connected else "\u2717")
        )
        sort_label: str = f"sort: {SORT_LABELS[self._sort_mode]}"
        now_str: str = time.strftime("%H:%M:%S")
        title_line: str = (
            f"{title}  |  {mqtt_status}  |  {sort_label}  |  {now_str}"
        )
        try:
            stdscr.addnstr(0, 0, title_line.ljust(width), width,
                           header_color | curses.A_BOLD)
        except curses.error:
            pass

        # Column headers.
        row: int = 2
        detail_width: int = max(width - COL_NAME - COL_PID - COL_STATUS
                                - COL_UPTIME - COL_RSS - COL_HB_AGE - 2, 10)
        header: str = (
            f"{'ADAPTER':<{COL_NAME}}"
            f"{'PID':>{COL_PID}}"
            f"{'STATE':>{COL_STATUS}}"
            f"{'UPTIME':>{COL_UPTIME}}"
            f"{'RSS MB':>{COL_RSS}}"
            f"{'HB AGE':>{COL_HB_AGE}}"
            f"  {'DETAIL':<{detail_width}}"
        )
        try:
            stdscr.addnstr(row, 0, header[:width], width,
                           header_color | curses.A_UNDERLINE)
        except curses.error:
            pass

        # Process rows.
        procs: list[ProcessInfo] = self._get_sorted_processes()
        for i, proc in enumerate(procs):
            row = 3 + i
            if row >= height - 2:
                break

            color: int = self._color_for_process(proc)
            detail_str: str = self._format_detail(proc, detail_width)

            pid_str: str = str(proc.pid) if proc.pid else "-"
            state_str: str = proc.state[:COL_STATUS - 1]
            uptime_str: str = self._format_uptime(proc.uptime_s)
            rss_str: str = (
                f"{proc.rss_mb:.1f}" if proc.rss_mb > 0 else "-"
            )
            hb_str: str = self._format_hb_age(proc.heartbeat_age)

            line: str = (
                f"{proc.adapter_id:<{COL_NAME}}"
                f"{pid_str:>{COL_PID}}"
                f"{state_str:>{COL_STATUS}}"
                f"{uptime_str:>{COL_UPTIME}}"
                f"{rss_str:>{COL_RSS}}"
                f"{hb_str:>{COL_HB_AGE}}"
                f"  {detail_str}"
            )
            try:
                stdscr.addnstr(row, 0, line[:width], width, color)
            except curses.error:
                pass

        # Footer.
        footer_row: int = height - 1
        footer: str = " q:quit  s:sort  r:refresh"
        try:
            stdscr.addnstr(footer_row, 0, footer.ljust(width), width,
                           normal_color | curses.A_DIM)
        except curses.error:
            pass

        stdscr.refresh()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse args and launch the TUI."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="glowup-top — GlowUp infrastructure monitor",
    )
    parser.add_argument(
        "--broker", default=DEFAULT_BROKER,
        help="MQTT broker address (default: localhost)",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help="MQTT broker port (default: 1883)",
    )
    args: argparse.Namespace = parser.parse_args()

    top: GlowUpTop = GlowUpTop(args.broker, args.port)
    try:
        curses.wrapper(top.run)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
