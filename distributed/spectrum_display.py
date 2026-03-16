"""Terminal spectrum display — ANSI truecolor frequency visualizer.

Subscribes to frequency band signals on the MQTT broker and renders
a live spectrum analyzer in the terminal using Unicode block characters
and 24-bit ANSI color.

Each of the 8 frequency bands is drawn as a vertical bar with color
mapped from bass (red/orange) through mid (green/yellow) to treble
(cyan/blue).  Beat pulses flash the display.  RMS level shown as a
horizontal meter.

Usage::

    python3 -m distributed.spectrum_display --broker 10.0.0.48 --source conway

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.spectrum_display")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic prefix for GlowUp signals.
SIGNAL_TOPIC_PREFIX: str = "glowup/signals/"

# Display dimensions.
BAR_WIDTH: int = 6          # Characters wide per band.
BAR_GAP: int = 1            # Gap between bars.
MAX_BAR_HEIGHT: int = 20    # Maximum bar height in rows.
METER_WIDTH: int = 40       # RMS meter width.

# Refresh rate (Hz).
DISPLAY_FPS: int = 15

# Band labels (8-band default).
BAND_LABELS: list[str] = [
    " Sub ", " Bass", " Low ", " Mid ",
    "HiMid", " Pres", " Bril", " Air ",
]

# Band colors — HSB-inspired gradient from warm (bass) to cool (treble).
# Each entry is (R, G, B) for the band's peak color.
BAND_COLORS: list[tuple[int, int, int]] = [
    (255, 40, 40),     # Sub — deep red
    (255, 120, 20),    # Bass — orange
    (255, 200, 0),     # Low — gold
    (180, 255, 0),     # Mid — chartreuse
    (0, 255, 100),     # Hi-mid — green
    (0, 200, 255),     # Presence — cyan
    (60, 100, 255),    # Brilliance — blue
    (140, 60, 255),    # Air — violet
]

# Beat flash color.
BEAT_COLOR: tuple[int, int, int] = (255, 255, 255)

# Minimum terminal width/height.
MIN_TERM_WIDTH: int = 60
MIN_TERM_HEIGHT: int = 10

# Peak hold decay rate (bars per second).
PEAK_HOLD_DECAY: float = 8.0

# ANSI escape sequences.
ESC: str = "\033"
CLEAR_SCREEN: str = f"{ESC}[2J"
HOME: str = f"{ESC}[H"
HIDE_CURSOR: str = f"{ESC}[?25l"
SHOW_CURSOR: str = f"{ESC}[?25h"
RESET: str = f"{ESC}[0m"
BOLD: str = f"{ESC}[1m"
DIM: str = f"{ESC}[2m"


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def _fg(r: int, g: int, b: int) -> str:
    """ANSI 24-bit foreground color escape."""
    return f"{ESC}[38;2;{r};{g};{b}m"


def _bg(r: int, g: int, b: int) -> str:
    """ANSI 24-bit background color escape."""
    return f"{ESC}[48;2;{r};{g};{b}m"


def _lerp_color(c1: tuple[int, int, int], c2: tuple[int, int, int],
                t: float) -> tuple[int, int, int]:
    """Linear interpolation between two RGB colors.

    Args:
        c1: Start color.
        c2: End color.
        t:  Interpolation factor [0.0, 1.0].

    Returns:
        Interpolated (R, G, B).
    """
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _dim_color(c: tuple[int, int, int],
               brightness: float) -> tuple[int, int, int]:
    """Scale a color by a brightness factor.

    Args:
        c:          Base (R, G, B) color.
        brightness: Scale factor [0.0, 1.0].

    Returns:
        Dimmed (R, G, B).
    """
    return (
        int(c[0] * brightness),
        int(c[1] * brightness),
        int(c[2] * brightness),
    )


# ---------------------------------------------------------------------------
# SpectrumDisplay
# ---------------------------------------------------------------------------

class SpectrumDisplay:
    """ANSI terminal spectrum analyzer driven by MQTT signals.

    Subscribes to ``{source}:audio:bands``, ``{source}:audio:beat``,
    ``{source}:audio:rms``, and ``{source}:audio:centroid`` on the
    MQTT broker and renders a live visualization.

    Args:
        broker:      MQTT broker hostname or IP.
        port:        MQTT broker port.
        source_name: Signal source name prefix (e.g. ``"conway"``).
    """

    def __init__(self, broker: str, port: int = 1883,
                 source_name: str = "conway") -> None:
        """Initialize the spectrum display.

        Args:
            broker:      MQTT broker address.
            port:        MQTT port.
            source_name: Signal source prefix.
        """
        self._broker: str = broker
        self._port: int = port
        self._source: str = source_name

        # Signal state (updated by MQTT thread).
        self._bands: list[float] = [0.0] * 8
        self._beat: float = 0.0
        self._rms: float = 0.0
        self._centroid: float = 0.0
        self._bass: float = 0.0
        self._mid: float = 0.0
        self._treble: float = 0.0
        self._lock: threading.Lock = threading.Lock()

        # Peak hold state.
        self._peaks: list[float] = [0.0] * 8
        self._last_render: float = time.monotonic()

        # MQTT client.
        self._client: Optional[Any] = None
        self._connected: bool = False

        # Control.
        self._stop_event: threading.Event = threading.Event()
        self._frames_rendered: int = 0

    def start(self) -> None:
        """Connect to MQTT and start the display loop."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            print("Error: paho-mqtt required. Install: pip install paho-mqtt",
                  file=sys.stderr)
            sys.exit(1)

        # Create MQTT client (v2 API).
        if hasattr(mqtt, "CallbackAPIVersion"):
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"glowup-spectrum-{int(time.time())}",
            )
        else:
            self._client = mqtt.Client(
                client_id=f"glowup-spectrum-{int(time.time())}",
            )

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        self._client.connect_async(self._broker, self._port)
        self._client.loop_start()

        # Set up terminal.
        sys.stdout.write(HIDE_CURSOR + CLEAR_SCREEN)
        sys.stdout.flush()

        self._render_loop()

    def stop(self) -> None:
        """Clean up terminal and disconnect."""
        self._stop_event.set()
        sys.stdout.write(SHOW_CURSOR + RESET + CLEAR_SCREEN + HOME)
        sys.stdout.flush()
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass

    def _on_connect(self, client: Any, userdata: Any, *args: Any) -> None:
        """Subscribe to signal topics on connect."""
        prefix: str = f"{SIGNAL_TOPIC_PREFIX}{self._source}:audio:"
        topics: list[str] = [
            f"{prefix}bands",
            f"{prefix}beat",
            f"{prefix}rms",
            f"{prefix}centroid",
            f"{prefix}bass",
            f"{prefix}mid",
            f"{prefix}treble",
        ]
        for topic in topics:
            client.subscribe(topic, qos=0)
        self._connected = True
        logger.info("Subscribed to %d signal topics", len(topics))

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Update signal state from MQTT message."""
        if not msg.topic.startswith(SIGNAL_TOPIC_PREFIX):
            return
        signal_name: str = msg.topic[len(SIGNAL_TOPIC_PREFIX):]
        try:
            value: Any = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            return

        with self._lock:
            if signal_name.endswith(":bands") and isinstance(value, list):
                self._bands = [float(v) for v in value[:8]]
            elif signal_name.endswith(":beat"):
                self._beat = float(value)
            elif signal_name.endswith(":rms"):
                self._rms = float(value)
            elif signal_name.endswith(":centroid"):
                self._centroid = float(value)
            elif signal_name.endswith(":bass"):
                self._bass = float(value)
            elif signal_name.endswith(":mid"):
                self._mid = float(value)
            elif signal_name.endswith(":treble"):
                self._treble = float(value)

    def _render_loop(self) -> None:
        """Main display loop — renders at DISPLAY_FPS."""
        interval: float = 1.0 / DISPLAY_FPS

        while not self._stop_event.is_set():
            now: float = time.monotonic()
            self._render_frame(now)
            self._frames_rendered += 1

            elapsed: float = time.monotonic() - now
            sleep_time: float = interval - elapsed
            if sleep_time > 0:
                self._stop_event.wait(sleep_time)

    def _render_frame(self, now: float) -> None:
        """Render one frame of the spectrum display.

        Args:
            now: Current monotonic time.
        """
        # Get terminal size.
        try:
            cols, rows = os.get_terminal_size()
        except OSError:
            cols, rows = 80, 24

        # Read signal state atomically.
        with self._lock:
            bands: list[float] = list(self._bands)
            beat: float = self._beat
            rms: float = self._rms
            centroid: float = self._centroid
            bass: float = self._bass
            mid: float = self._mid
            treble: float = self._treble

        # Compute time delta for peak decay.
        dt: float = now - self._last_render
        self._last_render = now

        # Update peak hold.
        for i in range(len(bands)):
            if bands[i] >= self._peaks[i]:
                self._peaks[i] = bands[i]
            else:
                self._peaks[i] = max(
                    bands[i],
                    self._peaks[i] - PEAK_HOLD_DECAY * dt,
                )

        # Calculate bar dimensions.
        n_bands: int = len(bands)
        total_bar_width: int = n_bands * BAR_WIDTH + (n_bands - 1) * BAR_GAP
        bar_height: int = min(MAX_BAR_HEIGHT, rows - 6)  # Leave room for labels.
        if bar_height < 3:
            bar_height = 3

        # Left margin to center the display.
        margin: int = max(0, (cols - total_bar_width) // 2)

        # Build frame buffer.
        lines: list[str] = []

        # Title line.
        title: str = f"  SPECTRUM  ·  {self._source}  ·  Judy FFT  "
        if beat > 0.3:
            title_color: str = _fg(*BEAT_COLOR) + BOLD
        else:
            title_color = _fg(120, 120, 120)
        title_line: str = title_color + title.center(cols) + RESET
        lines.append(title_line)
        lines.append("")

        # Spectrum bars (top to bottom).
        for row in range(bar_height, 0, -1):
            threshold: float = row / bar_height
            line: str = " " * margin

            for i in range(n_bands):
                level: float = bands[i]
                peak: float = self._peaks[i]
                color: tuple[int, int, int] = BAND_COLORS[i]

                if i > 0:
                    line += " " * BAR_GAP

                if level >= threshold:
                    # Filled bar segment — brightness varies with height.
                    brightness: float = 0.3 + 0.7 * (row / bar_height)
                    if beat > 0.3:
                        # Flash on beat.
                        brightness = min(1.0, brightness + beat * 0.3)
                    c: tuple[int, int, int] = _dim_color(color, brightness)
                    line += _bg(*c) + " " * BAR_WIDTH + RESET
                elif abs(peak - threshold) < (1.0 / bar_height):
                    # Peak hold indicator.
                    line += _fg(*color) + "▔" * BAR_WIDTH + RESET
                else:
                    # Empty segment — dim grid line.
                    if row == bar_height // 2:
                        line += _fg(40, 40, 40) + "·" * BAR_WIDTH + RESET
                    else:
                        line += " " * BAR_WIDTH

            lines.append(line)

        # Band labels.
        label_line: str = " " * margin
        for i in range(n_bands):
            if i > 0:
                label_line += " " * BAR_GAP
            c = BAND_COLORS[i]
            label: str = BAND_LABELS[i] if i < len(BAND_LABELS) else f" B{i} "
            # Pad or truncate to BAR_WIDTH.
            label = label[:BAR_WIDTH].center(BAR_WIDTH)
            label_line += _fg(*c) + label + RESET
        lines.append(label_line)

        # Value line.
        val_line: str = " " * margin
        for i in range(n_bands):
            if i > 0:
                val_line += " " * BAR_GAP
            val_str: str = f"{bands[i]:.2f}".center(BAR_WIDTH)
            val_line += _fg(80, 80, 80) + val_str + RESET
        lines.append(val_line)

        # Status line: RMS meter + beat + centroid.
        lines.append("")
        rms_filled: int = int(rms * METER_WIDTH)
        rms_bar: str = (
            _fg(0, 200, 100) + "█" * rms_filled
            + _fg(40, 40, 40) + "░" * (METER_WIDTH - rms_filled)
            + RESET
        )
        beat_indicator: str = (
            _fg(*BEAT_COLOR) + BOLD + " ● BEAT" + RESET
            if beat > 0.3 else _fg(50, 50, 50) + " ○     " + RESET
        )
        centroid_label: str = _fg(100, 100, 100) + f"  ◈ {centroid:.2f}" + RESET

        status: str = (
            " " * margin + _fg(100, 100, 100) + "RMS " + RESET
            + rms_bar + beat_indicator + centroid_label
        )
        lines.append(status)

        # Bass / Mid / Treble summary.
        summary: str = (
            " " * margin
            + _fg(*BAND_COLORS[0]) + f"Bass {bass:.2f}" + RESET
            + "  "
            + _fg(*BAND_COLORS[3]) + f"Mid {mid:.2f}" + RESET
            + "  "
            + _fg(*BAND_COLORS[6]) + f"Treble {treble:.2f}" + RESET
            + _fg(60, 60, 60)
            + f"  │  {self._frames_rendered} frames"
            + RESET
        )
        lines.append(summary)

        # Connection status.
        if not self._connected:
            lines.append(
                " " * margin
                + _fg(255, 60, 60) + "  ⚠ Connecting to MQTT..." + RESET
            )

        # Write frame.
        output: str = HOME + "\n".join(lines)
        # Clear remaining rows.
        remaining: int = max(0, rows - len(lines) - 1)
        if remaining > 0:
            output += "\n" + ("\033[K\n" * remaining)

        sys.stdout.write(output)
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Command-line entry point for the spectrum display."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="GlowUp Spectrum Display — ANSI terminal visualizer",
    )
    parser.add_argument(
        "--broker", default="10.0.0.48",
        help="MQTT broker address (default: 10.0.0.48)",
    )
    parser.add_argument(
        "--port", type=int, default=1883,
        help="MQTT broker port (default: 1883)",
    )
    parser.add_argument(
        "--source", default="conway",
        help="Signal source name (default: conway)",
    )

    args: argparse.Namespace = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    display: SpectrumDisplay = SpectrumDisplay(
        broker=args.broker,
        port=args.port,
        source_name=args.source,
    )

    def _shutdown(signum: int, frame: object) -> None:
        """Signal handler for clean shutdown."""
        display.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    display.start()


if __name__ == "__main__":
    main()
