"""Theremin display — shows note, frequency, amplitude, and hand heights.

Subscribes to all Theremin signals from the MQTT broker and renders a
live display in a tkinter window.

Signal input (via MQTT):
    ``glowup/signals/theremin:sensor:pitch``    — float (cm)
    ``glowup/signals/theremin:sensor:volume``   — float (cm)
    ``glowup/signals/theremin:note:frequency``  — float (Hz)
    ``glowup/signals/theremin:note:amplitude``  — float (0.0-1.0)

Usage::

    python3 -m theremin.display

Close the window or press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import sys
import threading
from typing import Any, Optional

import tkinter as tk

import paho.mqtt.client as mqtt

from . import (
    DISPLAY_HZ,
    DISTANCE_MAX_CM,
    DISTANCE_MIN_CM,
    MQTT_BROKER,
    MQTT_PORT,
    SIGNAL_AMPLITUDE,
    SIGNAL_FREQUENCY,
    SIGNAL_PITCH,
    SIGNAL_TOPIC_PREFIX,
    SIGNAL_VOLUME,
    freq_to_note_name,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW_TITLE: str = "GlowUp Theremin — Display"
WINDOW_WIDTH: int = 380
WINDOW_HEIGHT: int = 400

BG_COLOR: str = "#0d0d1a"
FG_COLOR: str = "#e0e0e0"
PITCH_COLOR: str = "#ff6b6b"
VOLUME_COLOR: str = "#4ecdc4"
NOTE_COLOR: str = "#ffd93d"
FREQ_COLOR: str = "#c084fc"

TITLE_FONT: tuple[str, int, str] = ("Helvetica", 16, "bold")
NOTE_FONT: tuple[str, int, str] = ("Menlo", 72, "bold")
DETAIL_FONT: tuple[str, int] = ("Menlo", 16)
LABEL_FONT: tuple[str, int] = ("Helvetica", 11)
BAR_HEIGHT: int = 20
BAR_MAX_WIDTH: int = 300

# paho v2 detection.
_PAHO_V2: bool = hasattr(mqtt, "CallbackAPIVersion")


# ---------------------------------------------------------------------------
# Display Window
# ---------------------------------------------------------------------------

class ThereminDisplay:
    """Tkinter window showing live Theremin state from MQTT signals."""

    def __init__(self) -> None:
        """Initialize the display window and MQTT client."""
        self._running: bool = True
        self._lock: threading.Lock = threading.Lock()

        # Signal values (updated by MQTT callback).
        self._pitch_cm: float = 0.0
        self._volume_cm: float = 0.0
        self._frequency: float = 0.0
        self._amplitude: float = 0.0

        # MQTT client.
        self._client: Optional[mqtt.Client] = None
        self._connect_mqtt()

        # Tkinter setup.
        self._root: tk.Tk = tk.Tk()
        self._root.title(WINDOW_TITLE)
        self._root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self._root.configure(bg=BG_COLOR)
        self._root.resizable(False, False)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._schedule_refresh()

    def _connect_mqtt(self) -> None:
        """Connect to MQTT and subscribe to all Theremin signals."""
        try:
            if _PAHO_V2:
                self._client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
                    client_id="theremin-display",
                )
            else:
                self._client = mqtt.Client(
                    client_id="theremin-display",
                    protocol=mqtt.MQTTv311,
                )
            self._client.on_connect = self._on_connect
            self._client.on_message = self._on_message
            self._client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            self._client.loop_start()
            print(f"  MQTT connected to {MQTT_BROKER}:{MQTT_PORT}")
        except Exception as exc:
            print(f"  Warning: MQTT connection failed: {exc}", file=sys.stderr)
            self._client = None

    def _on_connect(self, *args: Any) -> None:
        """Subscribe to all Theremin signal topics."""
        topics: list[tuple[str, int]] = [
            (SIGNAL_TOPIC_PREFIX + SIGNAL_PITCH, 0),
            (SIGNAL_TOPIC_PREFIX + SIGNAL_VOLUME, 0),
            (SIGNAL_TOPIC_PREFIX + SIGNAL_FREQUENCY, 0),
            (SIGNAL_TOPIC_PREFIX + SIGNAL_AMPLITUDE, 0),
        ]
        self._client.subscribe(topics)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Update signal values from MQTT messages."""
        try:
            value: float = float(json.loads(msg.payload.decode("utf-8")))
        except (json.JSONDecodeError, ValueError, TypeError):
            return

        signal_name: str = msg.topic[len(SIGNAL_TOPIC_PREFIX):]

        with self._lock:
            if signal_name == SIGNAL_PITCH:
                self._pitch_cm = value
            elif signal_name == SIGNAL_VOLUME:
                self._volume_cm = value
            elif signal_name == SIGNAL_FREQUENCY:
                self._frequency = value
            elif signal_name == SIGNAL_AMPLITUDE:
                self._amplitude = value

    def _build_ui(self) -> None:
        """Build the display interface."""
        # Title.
        tk.Label(
            self._root, text="Theremin", font=TITLE_FONT,
            bg=BG_COLOR, fg=FG_COLOR,
        ).pack(pady=(15, 10))

        # Note name (big).
        self._note_label: tk.Label = tk.Label(
            self._root, text="—", font=NOTE_FONT,
            bg=BG_COLOR, fg=NOTE_COLOR,
        )
        self._note_label.pack()

        # Frequency.
        self._freq_label: tk.Label = tk.Label(
            self._root, text="0.0 Hz", font=DETAIL_FONT,
            bg=BG_COLOR, fg=FREQ_COLOR,
        )
        self._freq_label.pack(pady=(0, 15))

        # --- Bars section ---
        bar_frame: tk.Frame = tk.Frame(self._root, bg=BG_COLOR)
        bar_frame.pack(fill=tk.X, padx=30)

        # Volume bar.
        tk.Label(
            bar_frame, text="Volume", font=LABEL_FONT,
            bg=BG_COLOR, fg=VOLUME_COLOR, anchor=tk.W,
        ).pack(fill=tk.X)

        self._vol_canvas: tk.Canvas = tk.Canvas(
            bar_frame, height=BAR_HEIGHT, bg="#1a1a2e",
            highlightthickness=0,
        )
        self._vol_canvas.pack(fill=tk.X, pady=(2, 10))

        # Pitch hand height bar.
        tk.Label(
            bar_frame, text="Pitch Hand", font=LABEL_FONT,
            bg=BG_COLOR, fg=PITCH_COLOR, anchor=tk.W,
        ).pack(fill=tk.X)

        self._pitch_canvas: tk.Canvas = tk.Canvas(
            bar_frame, height=BAR_HEIGHT, bg="#1a1a2e",
            highlightthickness=0,
        )
        self._pitch_canvas.pack(fill=tk.X, pady=(2, 10))

        # Volume hand height bar.
        tk.Label(
            bar_frame, text="Volume Hand", font=LABEL_FONT,
            bg=BG_COLOR, fg=VOLUME_COLOR, anchor=tk.W,
        ).pack(fill=tk.X)

        self._vol_hand_canvas: tk.Canvas = tk.Canvas(
            bar_frame, height=BAR_HEIGHT, bg="#1a1a2e",
            highlightthickness=0,
        )
        self._vol_hand_canvas.pack(fill=tk.X, pady=(2, 10))

        # Detail labels.
        self._detail_label: tk.Label = tk.Label(
            self._root, text="", font=("Menlo", 11),
            bg=BG_COLOR, fg="#888888",
        )
        self._detail_label.pack(pady=(5, 10))

    def _draw_bar(self, canvas: tk.Canvas, fraction: float,
                  color: str) -> None:
        """Draw a horizontal bar on a canvas.

        Args:
            canvas:   Target canvas.
            fraction: Fill fraction in [0.0, 1.0].
            color:    Bar fill color.
        """
        canvas.delete("all")
        width: int = canvas.winfo_width()
        if width < 10:
            width = BAR_MAX_WIDTH
        bar_w: int = int(fraction * width)
        canvas.create_rectangle(
            0, 0, bar_w, BAR_HEIGHT, fill=color, outline="",
        )

    def _schedule_refresh(self) -> None:
        """Schedule periodic UI refresh."""
        if not self._running:
            return
        self._refresh()
        interval_ms: int = 1000 // DISPLAY_HZ
        self._root.after(interval_ms, self._schedule_refresh)

    def _refresh(self) -> None:
        """Update all display elements from current signal values."""
        with self._lock:
            pitch_cm: float = self._pitch_cm
            volume_cm: float = self._volume_cm
            frequency: float = self._frequency
            amplitude: float = self._amplitude

        # Note name.
        note_name, octave = freq_to_note_name(frequency)
        if frequency > 0:
            self._note_label.configure(text=f"{note_name}{octave}")
            self._freq_label.configure(text=f"{frequency:.1f} Hz")
        else:
            self._note_label.configure(text="—")
            self._freq_label.configure(text="— Hz")

        # Volume bar.
        self._draw_bar(self._vol_canvas, amplitude, VOLUME_COLOR)

        # Hand height bars (normalized to sensor range).
        dist_range: float = DISTANCE_MAX_CM - DISTANCE_MIN_CM
        pitch_frac: float = max(0.0, min(1.0,
            (pitch_cm - DISTANCE_MIN_CM) / dist_range))
        vol_frac: float = max(0.0, min(1.0,
            (volume_cm - DISTANCE_MIN_CM) / dist_range))

        self._draw_bar(self._pitch_canvas, pitch_frac, PITCH_COLOR)
        self._draw_bar(self._vol_hand_canvas, vol_frac, VOLUME_COLOR)

        # Detail text.
        self._detail_label.configure(
            text=f"Pitch: {pitch_cm:.1f} cm  |  Vol: {volume_cm:.1f} cm"
        )

    def _on_close(self) -> None:
        """Handle window close."""
        self._running = False
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
        self._root.destroy()

    def run(self) -> None:
        """Start the tkinter main loop."""
        self._root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Launch the Theremin display."""
    print("╔══════════════════════════════════════════════╗")
    print("║   GlowUp Theremin — Display                 ║")
    print("║   Showing live note, frequency, amplitude    ║")
    print("╚══════════════════════════════════════════════╝")
    print()

    display: ThereminDisplay = ThereminDisplay()
    display.run()
    print("  Display stopped.")


if __name__ == "__main__":
    main()
