"""Theremin sensor simulator — two vertical sliders publishing to SignalBus.

Simulates two ESP32 laser rangefinders by providing tkinter vertical
sliders for pitch (right hand) and volume (left hand).  Publishes
distance readings as individual SignalBus signals via the MQTT bridge,
exactly as the real ESP32 sensors will.

Signal output:
    ``glowup/signals/theremin:sensor:pitch``  — float (cm)
    ``glowup/signals/theremin:sensor:volume`` — float (cm)

Usage::

    python3 -m theremin.simulator

The slider window stays open until closed.  Press Ctrl+C in the
terminal or close the window to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import sys
import threading
import time
import tkinter as tk
from typing import Any, Optional

import paho.mqtt.client as mqtt

from . import (
    DISTANCE_MAX_CM,
    DISTANCE_MIN_CM,
    MQTT_BROKER,
    MQTT_PORT,
    SIGNAL_PITCH,
    SIGNAL_TOPIC_PREFIX,
    SIGNAL_VOLUME,
    SLIDER_PUBLISH_HZ,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW_TITLE: str = "GlowUp Theremin — Sensor Simulator"
WINDOW_WIDTH: int = 400
WINDOW_HEIGHT: int = 500

SLIDER_LENGTH: int = 350       # Pixel height of each slider
SLIDER_WIDTH: int = 40         # Pixel width of slider trough
SLIDER_RESOLUTION: float = 0.1  # Step size in cm

# Initial hand positions (cm from sensor).
INITIAL_PITCH_CM: float = 40.0   # Mid-range
INITIAL_VOLUME_CM: float = 40.0  # Mid-range

# Colors.
BG_COLOR: str = "#1a1a2e"
FG_COLOR: str = "#e0e0e0"
PITCH_COLOR: str = "#ff6b6b"
VOLUME_COLOR: str = "#4ecdc4"
LABEL_FONT: tuple[str, int] = ("Helvetica", 14)
VALUE_FONT: tuple[str, int, str] = ("Menlo", 18, "bold")

# paho v2 detection.
_PAHO_V2: bool = hasattr(mqtt, "CallbackAPIVersion")


# ---------------------------------------------------------------------------
# Simulator Window
# ---------------------------------------------------------------------------

class ThereminSimulator:
    """Tkinter window with two vertical sliders for Theremin simulation.

    Publishes each slider's distance as an individual SignalBus signal
    via the MQTT bridge, matching the format the ESP32 sensors will use.
    """

    def __init__(self) -> None:
        """Initialize the simulator window and MQTT client."""
        self._running: bool = True
        self._client: Optional[mqtt.Client] = None

        # --- MQTT setup ---
        self._connect_mqtt()

        # --- Tkinter setup ---
        self._root: tk.Tk = tk.Tk()
        self._root.title(WINDOW_TITLE)
        self._root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self._root.configure(bg=BG_COLOR)
        self._root.resizable(False, False)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()

        # --- Publish thread ---
        self._pub_thread: threading.Thread = threading.Thread(
            target=self._publish_loop,
            daemon=True,
        )
        self._pub_thread.start()

    def _connect_mqtt(self) -> None:
        """Connect to the MQTT broker on the Pi."""
        try:
            if _PAHO_V2:
                self._client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
                    client_id="theremin-simulator",
                )
            else:
                self._client = mqtt.Client(
                    client_id="theremin-simulator",
                    protocol=mqtt.MQTTv311,
                )
            self._client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            self._client.loop_start()
            print(f"  MQTT connected to {MQTT_BROKER}:{MQTT_PORT}")
        except Exception as exc:
            print(f"  Warning: MQTT connection failed: {exc}", file=sys.stderr)
            print("  Sliders will run but data won't reach the Pi.",
                  file=sys.stderr)
            self._client = None

    def _build_ui(self) -> None:
        """Build the slider interface."""
        # Title.
        title_label: tk.Label = tk.Label(
            self._root,
            text="Theremin Sensor Simulator",
            font=("Helvetica", 16, "bold"),
            bg=BG_COLOR,
            fg=FG_COLOR,
        )
        title_label.pack(pady=(15, 5))

        # Container for the two slider columns.
        container: tk.Frame = tk.Frame(self._root, bg=BG_COLOR)
        container.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        # --- Volume slider (left hand) ---
        vol_frame: tk.Frame = tk.Frame(container, bg=BG_COLOR)
        vol_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        tk.Label(
            vol_frame, text="Left Hand", font=LABEL_FONT,
            bg=BG_COLOR, fg=VOLUME_COLOR,
        ).pack(pady=(0, 2))
        tk.Label(
            vol_frame, text="VOLUME", font=("Helvetica", 10),
            bg=BG_COLOR, fg=VOLUME_COLOR,
        ).pack()

        self._vol_var: tk.DoubleVar = tk.DoubleVar(value=INITIAL_VOLUME_CM)
        self._vol_slider: tk.Scale = tk.Scale(
            vol_frame,
            variable=self._vol_var,
            from_=DISTANCE_MIN_CM,
            to=DISTANCE_MAX_CM,
            orient=tk.VERTICAL,
            length=SLIDER_LENGTH,
            width=SLIDER_WIDTH,
            resolution=SLIDER_RESOLUTION,
            bg=BG_COLOR,
            fg=VOLUME_COLOR,
            troughcolor="#2a2a4a",
            highlightthickness=0,
            font=("Menlo", 10),
            showvalue=False,
        )
        self._vol_slider.pack(pady=5)

        self._vol_label: tk.Label = tk.Label(
            vol_frame, text="40.0 cm", font=VALUE_FONT,
            bg=BG_COLOR, fg=VOLUME_COLOR,
        )
        self._vol_label.pack()

        # --- Pitch slider (right hand) ---
        pitch_frame: tk.Frame = tk.Frame(container, bg=BG_COLOR)
        pitch_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        tk.Label(
            pitch_frame, text="Right Hand", font=LABEL_FONT,
            bg=BG_COLOR, fg=PITCH_COLOR,
        ).pack(pady=(0, 2))
        tk.Label(
            pitch_frame, text="PITCH", font=("Helvetica", 10),
            bg=BG_COLOR, fg=PITCH_COLOR,
        ).pack()

        self._pitch_var: tk.DoubleVar = tk.DoubleVar(value=INITIAL_PITCH_CM)
        self._pitch_slider: tk.Scale = tk.Scale(
            pitch_frame,
            variable=self._pitch_var,
            from_=DISTANCE_MIN_CM,
            to=DISTANCE_MAX_CM,
            orient=tk.VERTICAL,
            length=SLIDER_LENGTH,
            width=SLIDER_WIDTH,
            resolution=SLIDER_RESOLUTION,
            bg=BG_COLOR,
            fg=PITCH_COLOR,
            troughcolor="#2a2a4a",
            highlightthickness=0,
            font=("Menlo", 10),
            showvalue=False,
        )
        self._pitch_slider.pack(pady=5)

        self._pitch_label: tk.Label = tk.Label(
            pitch_frame, text="40.0 cm", font=VALUE_FONT,
            bg=BG_COLOR, fg=PITCH_COLOR,
        )
        self._pitch_label.pack()

        # Instruction label.
        tk.Label(
            self._root,
            text="Top = hand close to sensor · Bottom = hand far away",
            font=("Helvetica", 9),
            bg=BG_COLOR,
            fg="#888888",
        ).pack(pady=(0, 10))

        # Update value labels on slider move.
        self._vol_var.trace_add("write", self._update_labels)
        self._pitch_var.trace_add("write", self._update_labels)

    def _update_labels(self, *_args: Any) -> None:
        """Update the cm readout labels when sliders move."""
        self._vol_label.configure(text=f"{self._vol_var.get():.1f} cm")
        self._pitch_label.configure(text=f"{self._pitch_var.get():.1f} cm")

    def _publish_loop(self) -> None:
        """Background thread: publish slider values as SignalBus signals.

        Each signal is published as a JSON scalar to the MQTT topic
        ``glowup/signals/{signal_name}``, matching the SignalBus bridge
        format.  The Pi's SignalBus ingests these automatically.
        """
        interval: float = 1.0 / SLIDER_PUBLISH_HZ
        pitch_topic: str = SIGNAL_TOPIC_PREFIX + SIGNAL_PITCH
        volume_topic: str = SIGNAL_TOPIC_PREFIX + SIGNAL_VOLUME

        while self._running:
            if self._client is not None:
                try:
                    self._client.publish(
                        pitch_topic,
                        json.dumps(self._pitch_var.get()),
                        qos=0,
                    )
                    self._client.publish(
                        volume_topic,
                        json.dumps(self._vol_var.get()),
                        qos=0,
                    )
                except Exception:
                    pass  # Non-critical — next publish will retry.
            time.sleep(interval)

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
    """Launch the Theremin sensor simulator."""
    print("╔══════════════════════════════════════════════╗")
    print("║   GlowUp Theremin — Sensor Simulator        ║")
    print("║   Publishing to SignalBus via MQTT bridge    ║")
    print("╚══════════════════════════════════════════════╝")
    print()

    sim: ThereminSimulator = ThereminSimulator()
    sim.run()
    print("  Simulator stopped.")


if __name__ == "__main__":
    main()
