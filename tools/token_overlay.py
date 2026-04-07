#!/usr/bin/env python3
"""Always-on-top token meter overlay for Claude Code sessions.

A compact, borderless window that displays context utilization and
time-to-compact.  Receives updates from token_meter.py via UDP on
localhost.

Usage::

    python tools/token_overlay.py           # default position (top-right)
    python tools/token_overlay.py --x 100 --y 100  # custom position

The overlay listens on UDP port 9147 for JSON packets from
token_meter.py.  If no updates arrive, the display dims after 10
seconds to indicate stale data.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.

__version__: str = "1.0"

import argparse
import json
import socket
import threading
import time
import tkinter as tk
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# UDP port for receiving updates from token_meter.
UDP_PORT: int = 9147

# Window dimensions.
WIN_WIDTH: int = 260
WIN_HEIGHT: int = 88

# Stale data threshold (seconds since last update).
STALE_THRESHOLD: float = 10.0

# Poll interval for checking staleness (milliseconds).
STALE_POLL_MS: int = 2000

# Color scheme.
BG_COLOR: str = "#1e1e1e"
TEXT_COLOR: str = "#e0e0e0"
DIM_COLOR: str = "#666666"
GREEN: str = "#4ec94e"
YELLOW: str = "#e6c84e"
RED: str = "#e64e4e"

# Utilization thresholds (must match token_meter.py).
WARN_THRESHOLD: float = 0.70
CRIT_THRESHOLD: float = 0.90


# ---------------------------------------------------------------------------
# Overlay window
# ---------------------------------------------------------------------------

class TokenOverlay:
    """Compact always-on-top overlay showing token meter state."""

    def __init__(self, x: int, y: int, opacity: float) -> None:
        self._last_update: float = 0.0

        # Build the window.
        self._root: tk.Tk = tk.Tk()
        self._root.title("Token Meter")
        self._root.overrideredirect(True)       # borderless
        self._root.attributes("-topmost", True) # always on top
        self._root.configure(bg=BG_COLOR)
        self._root.geometry(f"{WIN_WIDTH}x{WIN_HEIGHT}+{x}+{y}")

        # Transparency (macOS).
        try:
            self._root.attributes("-alpha", opacity)
        except tk.TclError:
            pass

        # Main label — percentage and time.
        self._label: tk.Label = tk.Label(
            self._root,
            text="waiting...",
            font=("Menlo", 14, "bold"),
            fg=TEXT_COLOR,
            bg=BG_COLOR,
            anchor="w",
        )
        self._label.pack(fill="x", padx=8, pady=(6, 0))

        # Detail label — growth rate.
        self._detail: tk.Label = tk.Label(
            self._root,
            text="",
            font=("Menlo", 10),
            fg=DIM_COLOR,
            bg=BG_COLOR,
            anchor="w",
        )
        self._detail.pack(fill="x", padx=8, pady=(0, 0))

        # Quota label — 5hr session rate limit.
        self._quota: tk.Label = tk.Label(
            self._root,
            text="",
            font=("Menlo", 10),
            fg=DIM_COLOR,
            bg=BG_COLOR,
            anchor="w",
        )
        self._quota.pack(fill="x", padx=8, pady=(0, 0))

        # Progress bar canvas.
        self._canvas: tk.Canvas = tk.Canvas(
            self._root,
            height=6,
            bg=BG_COLOR,
            highlightthickness=0,
        )
        self._canvas.pack(fill="x", padx=8, pady=(2, 6))

        # Allow dragging the window.
        self._drag_x: int = 0
        self._drag_y: int = 0
        self._root.bind("<Button-1>", self._start_drag)
        self._root.bind("<B1-Motion>", self._do_drag)

        # Start UDP listener thread.
        self._sock: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM,
        )
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", UDP_PORT))
        self._listener: threading.Thread = threading.Thread(
            target=self._udp_listen, daemon=True,
        )
        self._listener.start()

        # Staleness checker.
        self._root.after(STALE_POLL_MS, self._check_stale)

    def _start_drag(self, event: tk.Event) -> None:
        """Record drag start position."""
        self._drag_x = event.x
        self._drag_y = event.y

    def _do_drag(self, event: tk.Event) -> None:
        """Move window during drag."""
        x: int = self._root.winfo_x() + event.x - self._drag_x
        y: int = self._root.winfo_y() + event.y - self._drag_y
        self._root.geometry(f"+{x}+{y}")

    def _udp_listen(self) -> None:
        """Background thread: receive UDP updates from token_meter."""
        while True:
            try:
                data, _ = self._sock.recvfrom(4096)
                msg: dict = json.loads(data.decode("utf-8"))
                # Schedule UI update on the main thread.
                self._root.after(0, self._apply_update, msg)
            except Exception:
                pass

    def _apply_update(self, msg: dict) -> None:
        """Update the display from a received message."""
        self._last_update = time.monotonic()

        util: float = msg.get("util", 0.0)
        current: int = msg.get("current", 0)
        ceiling: int = msg.get("ceiling", 0)
        time_left: Optional[str] = msg.get("time_left")
        smoothed: Optional[str] = msg.get("smoothed")
        last_delta: Optional[str] = msg.get("last_delta")

        # Pick color based on utilization.
        if util >= CRIT_THRESHOLD:
            color = RED
        elif util >= WARN_THRESHOLD:
            color = YELLOW
        else:
            color = GREEN

        # Main text: percentage and time remaining.
        pct_str: str = f"{util * 100:.0f}%"
        time_str: str = time_left if time_left else "—"
        self._label.config(
            text=f"{pct_str}  {time_str}",
            fg=color,
        )

        # Detail text.
        detail_parts: list[str] = []
        if last_delta:
            detail_parts.append(last_delta)
        if smoothed:
            detail_parts.append(smoothed)
        self._detail.config(
            text="  ".join(detail_parts) if detail_parts else "",
            fg=DIM_COLOR,
        )

        # 5hr session quota from scraper.
        quota: Optional[list] = msg.get("quota")
        if quota:
            for q in quota:
                if "session" in q.get("label", "").lower():
                    qpct: int = q.get("pct", 0)
                    qreset: str = q.get("reset", "")
                    if qpct >= 90:
                        qcolor = RED
                    elif qpct >= 70:
                        qcolor = YELLOW
                    else:
                        qcolor = GREEN
                    self._quota.config(
                        text=f"5hr: {qpct}%  {qreset}",
                        fg=qcolor,
                    )
                    break

        # Progress bar.
        self._canvas.delete("all")
        bar_width: int = self._canvas.winfo_width()
        if bar_width < 10:
            bar_width = WIN_WIDTH - 16
        filled: int = int(util * bar_width)
        filled = min(filled, bar_width)
        # Background track.
        self._canvas.create_rectangle(
            0, 0, bar_width, 6, fill="#333333", outline="",
        )
        # Filled portion.
        if filled > 0:
            self._canvas.create_rectangle(
                0, 0, filled, 6, fill=color, outline="",
            )

    def _check_stale(self) -> None:
        """Dim the display if no updates have arrived recently."""
        if self._last_update > 0:
            age: float = time.monotonic() - self._last_update
            if age > STALE_THRESHOLD:
                self._label.config(fg=DIM_COLOR)
        self._root.after(STALE_POLL_MS, self._check_stale)

    def run(self) -> None:
        """Start the tkinter main loop."""
        self._root.mainloop()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Always-on-top token meter overlay.",
    )
    parser.add_argument(
        "--x", type=int, default=50,
        help="Window X position (default: 50).",
    )
    parser.add_argument(
        "--y", type=int, default=50,
        help="Window Y position (default: 50).",
    )
    parser.add_argument(
        "--opacity", type=float, default=0.90,
        help="Window opacity 0.0-1.0 (default: 0.90).",
    )
    args: argparse.Namespace = parser.parse_args()

    overlay: TokenOverlay = TokenOverlay(
        x=args.x, y=args.y, opacity=args.opacity,
    )
    overlay.run()


if __name__ == "__main__":
    main()
