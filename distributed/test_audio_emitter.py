"""Quick integration test — MQTT theremin signals → AudioOutEmitter.

Subscribes to the theremin note signals on the Pi's MQTT broker and
feeds them directly into the AudioOutEmitter.  This validates the
emitter works end-to-end without needing the full worker agent
assignment machinery.

Requires:
    - Pi running with MQTT broker (192.0.2.48:1883)
    - Theremin effect active (or simulator publishing note signals)
    - sounddevice, numpy, paho-mqtt installed

Usage::

    ~/venv/bin/python3 -m distributed.test_audio_emitter

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import json
import select
import signal
import sys
import termios
import tty
from typing import Any

import paho.mqtt.client as mqtt

from emitters import create_emitter
from emitters.audio_out import AudioOutEmitter
from network_config import net

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT broker (from centralized network config).
MQTT_BROKER: str = net.broker
MQTT_PORT: int = 1883

# Signal topics (must match theremin/__init__.py).
SIGNAL_TOPIC_PREFIX: str = "glowup/signals/"
SIGNAL_FREQUENCY: str = "theremin:note:frequency"
SIGNAL_AMPLITUDE: str = "theremin:note:amplitude"

# paho v2 detection.
_PAHO_V2: bool = hasattr(mqtt, "CallbackAPIVersion")


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MQTT-to-audio-emitter integration test."""
    print("╔══════════════════════════════════════════════╗")
    print("║   AudioOutEmitter — MQTT Integration Test    ║")
    print("║   Listening for theremin note signals         ║")
    print("╚══════════════════════════════════════════════╝")
    print()

    # Create and open the audio emitter.
    emitter: AudioOutEmitter = create_emitter("audio_out", "test:speaker", {
        "master_volume": 0.3,
    })
    emitter.on_configure({})
    emitter.on_open()
    print(f"  Audio emitter opened: {emitter.name}")
    print(f"  Vibrato: rate={emitter.vibrato_rate:.1f} Hz, "
          f"depth={emitter.vibrato_depth:.3f}, "
          f"amp_depth={emitter.vibrato_amp_depth:.2f}")

    # Track latest values for display.
    current_freq: float = 0.0
    current_amp: float = 0.0

    # MQTT client.
    if _PAHO_V2:
        client: mqtt.Client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="test-audio-emitter",
        )
    else:
        client = mqtt.Client(
            client_id="test-audio-emitter",
            protocol=mqtt.MQTTv311,
        )

    def on_connect(*args: Any) -> None:
        """Subscribe to theremin note signals on connect."""
        freq_topic: str = SIGNAL_TOPIC_PREFIX + SIGNAL_FREQUENCY
        amp_topic: str = SIGNAL_TOPIC_PREFIX + SIGNAL_AMPLITUDE
        client.subscribe([(freq_topic, 0), (amp_topic, 0)])
        print(f"  Subscribed to note signals on {MQTT_BROKER}")
        print(f"    {freq_topic}")
        print(f"    {amp_topic}")
        print()
        print("  Keys:  h = hush/unmute   q = quit")
        print()
        print("  Waiting for signals...")

    def on_message(client_obj: Any, userdata: Any, msg: Any) -> None:
        """Feed MQTT signals into the emitter."""
        nonlocal current_freq, current_amp
        try:
            value: float = float(json.loads(msg.payload.decode("utf-8")))
        except (json.JSONDecodeError, ValueError, TypeError):
            return

        signal_name: str = msg.topic[len(SIGNAL_TOPIC_PREFIX):]

        if signal_name == SIGNAL_FREQUENCY:
            current_freq = value
        elif signal_name == SIGNAL_AMPLITUDE:
            current_amp = value

        # Feed to emitter as a scalar frame.
        emitter.on_emit(
            {"frequency": current_freq, "amplitude": current_amp},
            {},
        )

    client.on_connect = on_connect
    client.on_message = on_message

    print(f"  Connecting to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}...",
          end="", flush=True)
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    except Exception as exc:
        print(f" failed: {exc}", file=sys.stderr)
        emitter.on_close()
        sys.exit(1)
    print(" connected")

    client.loop_start()

    # Raw terminal mode for single-keypress input.
    old_settings = termios.tcgetattr(sys.stdin)
    running: bool = True

    def _shutdown(signum: int, frame_obj: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        tty.setraw(sys.stdin.fileno())

        while running:
            # Poll stdin with 100 ms timeout (non-blocking).
            if select.select([sys.stdin], [], [], 0.1)[0]:
                ch: str = sys.stdin.read(1)
                if ch in ("h", "H"):
                    muted: bool = emitter.toggle_mute()
                    # Write status on a fresh line (raw mode needs \r\n).
                    status: str = "MUTED" if muted else "LIVE"
                    sys.stdout.write(f"\r  [{status}]\r\n")
                    sys.stdout.flush()
                elif ch in ("q", "Q", "\x03"):
                    # q or Ctrl+C — quit.
                    running = False

    finally:
        # Restore terminal before any output.
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

    print("\n  Shutting down...")
    client.loop_stop()
    client.disconnect()
    emitter.on_close()
    print("  Done.")


if __name__ == "__main__":
    main()
