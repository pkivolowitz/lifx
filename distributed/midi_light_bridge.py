"""MIDI-to-LIFX light bridge — visualize MIDI events on string lights.

Subscribes to ``sensor:midi:events`` on the MQTT bus (same topic as
the audio emitter) and translates MIDI note events into colors on a
LIFX multizone device.  Runs as a third process alongside the MIDI
sensor and audio emitter — all three subscribe to the same bus topic
and the broker fans out to all of them simultaneously.

Mapping:

* **Note pitch** → zone position (low notes left, high notes right).
* **Velocity** → brightness.
* **Channel** → hue (each MIDI channel gets a distinct color).
* **Note off** → zone fades toward black.

The bridge maintains a zone buffer and pushes frames to the device
at a configurable rate, decoupling the MIDI event rate from the
LIFX update rate (LIFX string lights top out around 15-20 fps).

Usage::

    python3 -m distributed.midi_light_bridge --ip 10.0.0.62

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import json
import logging
import signal
import sys
import threading
import time
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.midi_light_bridge")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default MQTT broker (Pi).
DEFAULT_BROKER: str = "10.0.0.48"

# Default MQTT port.
DEFAULT_MQTT_PORT: int = 1883

# MQTT topic prefix.
MQTT_SIGNAL_PREFIX: str = "glowup/signals/"

# Signal name to subscribe to.
DEFAULT_SIGNAL_NAME: str = "sensor:midi:events"

# MQTT QoS.
MQTT_QOS: int = 0

# Default frame rate for pushing colors to the LIFX device (Hz).
# String lights handle ~15 fps reliably.
DEFAULT_FPS: int = 15

# LIFX color space constants.
HUE_MAX: int = 65535
SAT_MAX: int = 65535
BRI_MAX: int = 65535
KELVIN_DEFAULT: int = 3500

# MIDI note range for mapping to zones.
# Organ literature spans roughly MIDI 36 (C2) to 96 (C7).
MIDI_NOTE_LOW: int = 24
MIDI_NOTE_HIGH: int = 108

# Brightness decay per frame (multiplicative).
# 0.92 means ~8% decay per frame at 15 fps — notes fade in ~1s.
DEFAULT_DECAY: float = 0.92

# Minimum brightness before snapping to zero (prevents dim tails).
BRIGHTNESS_FLOOR: float = 0.02

# Hue assignments per MIDI channel (0-15).
# Spread around the color wheel so channels are visually distinct.
# Hue is 0-65535 mapping to 0-360 degrees.
CHANNEL_HUES: list[int] = [
    0,          # Ch 0  — Red (Swell organ)
    43690,      # Ch 1  — Blue (Great organ)
    21845,      # Ch 2  — Green (Pedal organ)
    54613,      # Ch 3  — Purple
    10922,      # Ch 4  — Yellow-green
    32768,      # Ch 5  — Cyan
    5461,       # Ch 6  — Orange
    49152,      # Ch 7  — Violet
    16384,      # Ch 8  — Yellow
    38229,      # Ch 9  — Teal (drums in GM)
    8192,       # Ch 10 — Amber
    27307,      # Ch 11 — Spring green
    46080,      # Ch 12 — Indigo
    13653,      # Ch 13 — Gold
    35498,      # Ch 14 — Sky blue
    57344,      # Ch 15 — Magenta
]

# Saturation for note colors (full saturation).
NOTE_SATURATION: int = SAT_MAX


# ---------------------------------------------------------------------------
# MidiLightBridge
# ---------------------------------------------------------------------------

class MidiLightBridge:
    """Bridge MIDI events from the bus to LIFX string light colors.

    Maintains a per-zone brightness/hue buffer, applies decay each
    frame, and pushes the result to the LIFX device at a steady
    frame rate via a dedicated render thread.

    Args:
        device_ip:   LIFX device IP address.
        broker:      MQTT broker host.
        port:        MQTT broker port.
        signal_name: Bus signal to subscribe to.
        fps:         Target frame rate for LIFX updates.
        decay:       Per-frame brightness decay factor (0-1).
    """

    def __init__(self, device_ip: str,
                 broker: str = DEFAULT_BROKER,
                 port: int = DEFAULT_MQTT_PORT,
                 signal_name: str = DEFAULT_SIGNAL_NAME,
                 fps: int = DEFAULT_FPS,
                 decay: float = DEFAULT_DECAY) -> None:
        """Initialize the MIDI light bridge.

        Args:
            device_ip:   Target LIFX device IP.
            broker:      MQTT broker host.
            port:        MQTT broker port.
            signal_name: Bus signal name.
            fps:         LIFX update rate.
            decay:       Brightness decay factor.
        """
        self._device_ip: str = device_ip
        self._broker: str = broker
        self._port: int = port
        self._signal_name: str = signal_name
        self._fps: int = fps
        self._decay: float = decay

        self._client: Optional[Any] = None
        self._connected: bool = False
        self._stop_event: threading.Event = threading.Event()

        # LIFX device handle.
        self._device: Optional[Any] = None
        self._zone_count: int = 0

        # Per-zone state: brightness (0.0-1.0) and hue (0-65535).
        self._zone_brightness: list[float] = []
        self._zone_hue: list[int] = []
        self._lock: threading.Lock = threading.Lock()

        # Render thread.
        self._render_thread: Optional[threading.Thread] = None

        # Stats.
        self._notes_received: int = 0
        self._frames_pushed: int = 0
        self._start_time: float = 0.0

    def start(self) -> None:
        """Discover device, connect to MQTT, and run the render loop.

        Blocks until stopped.
        """
        # Discover the LIFX device.
        self._discover_device()
        if self._device is None:
            logger.error("No LIFX device found at %s", self._device_ip)
            return

        # Initialize zone buffers.
        self._zone_brightness = [0.0] * self._zone_count
        self._zone_hue = [0] * self._zone_count

        # Connect to MQTT.
        self._connect_mqtt()
        if not self._connected:
            logger.error("Failed to connect to MQTT broker at %s:%d",
                         self._broker, self._port)
            return

        # Start the render thread.
        self._start_time = time.monotonic()
        self._stop_event.clear()
        self._render_thread = threading.Thread(
            target=self._render_loop,
            daemon=True,
            name="midi-light-render",
        )
        self._render_thread.start()

        logger.info(
            "MidiLightBridge running — %s (%d zones) at %d fps",
            self._device_ip, self._zone_count, self._fps,
        )

        # Block until stopped.
        try:
            while not self._stop_event.is_set():
                self._stop_event.wait(0.1)
        except KeyboardInterrupt:
            pass

        self._shutdown()

    def stop(self) -> None:
        """Signal the bridge to stop."""
        self._stop_event.set()

    def _shutdown(self) -> None:
        """Clean shutdown — black out lights, disconnect."""
        logger.info("Shutting down...")

        # Stop render thread.
        self._stop_event.set()
        if self._render_thread is not None:
            self._render_thread.join(timeout=3)

        # Black out the device.
        if self._device is not None:
            try:
                black: list[tuple[int, int, int, int]] = [
                    (0, 0, 0, KELVIN_DEFAULT)
                ] * self._zone_count
                self._device.set_zones(black, duration_ms=500)
            except Exception:
                pass

        self._disconnect_mqtt()

        elapsed: float = time.monotonic() - self._start_time
        logger.info(
            "MidiLightBridge stopped — %d notes, %d frames in %.1f s",
            self._notes_received, self._frames_pushed, elapsed,
        )

    # -------------------------------------------------------------------
    # LIFX device discovery
    # -------------------------------------------------------------------

    def _discover_device(self) -> None:
        """Find and connect to the LIFX device."""
        from transport import discover_devices

        logger.info("Discovering LIFX device at %s...", self._device_ip)
        devices = discover_devices(target_ip=self._device_ip, timeout=5)

        if not devices:
            return

        self._device = devices[0]
        self._zone_count = self._device.zone_count
        logger.info(
            "Found: %s — %d zones",
            self._device.label, self._zone_count,
        )

    # -------------------------------------------------------------------
    # MIDI event handling
    # -------------------------------------------------------------------

    def _on_midi_event(self, event: dict) -> None:
        """Map a MIDI event to zone colors.

        Args:
            event: Parsed JSON event dict from the bus.
        """
        event_type: str = event.get("event_type", "")

        if event_type == "note_on":
            channel: int = event.get("channel", 0)
            note: int = event.get("note", 60)
            velocity: int = event.get("velocity", 100)
            self._note_on(channel, note, velocity)

        elif event_type == "note_off":
            channel: int = event.get("channel", 0)
            note: int = event.get("note", 60)
            self._note_off(channel, note)

    def _note_on(self, channel: int, note: int, velocity: int) -> None:
        """Light up zones corresponding to a note.

        Maps the note pitch to a zone range and sets brightness
        based on velocity.

        Args:
            channel:  MIDI channel (determines hue).
            note:     MIDI note number (determines zone position).
            velocity: Note velocity (determines brightness).
        """
        if self._zone_count == 0:
            return

        self._notes_received += 1

        # Map note to zone position (0.0 - 1.0).
        note_frac: float = (note - MIDI_NOTE_LOW) / max(
            MIDI_NOTE_HIGH - MIDI_NOTE_LOW, 1,
        )
        note_frac = max(0.0, min(1.0, note_frac))

        # Map to zone index.
        center_zone: int = int(note_frac * (self._zone_count - 1))

        # Light up a small spread of zones around the center
        # (3 zones = 1 bulb on a string light with zpb=3).
        spread: int = 3
        hue: int = CHANNEL_HUES[channel & 0x0F]
        # Boost brightness — minimum 50%, scale velocity into top half.
        brightness: float = 0.5 + (velocity / 127.0) * 0.5

        with self._lock:
            for offset in range(-spread // 2, spread // 2 + 1):
                zone: int = center_zone + offset
                if 0 <= zone < self._zone_count:
                    # Brightest takes priority (don't dim an active zone).
                    if brightness > self._zone_brightness[zone]:
                        self._zone_brightness[zone] = brightness
                        self._zone_hue[zone] = hue

    def _note_off(self, channel: int, note: int) -> None:
        """Mark zones for a note to begin decaying.

        The actual fade happens in the render loop via the decay
        factor — note_off just lets the decay take over naturally.
        No immediate action needed.

        Args:
            channel: MIDI channel.
            note:    MIDI note number.
        """
        # Decay handles the fade — nothing to do here.
        pass

    # -------------------------------------------------------------------
    # Render loop
    # -------------------------------------------------------------------

    def _render_loop(self) -> None:
        """Push zone colors to the device at the target frame rate.

        Applies brightness decay each frame to create fade-out on
        notes that have stopped.
        """
        frame_interval: float = 1.0 / self._fps

        while not self._stop_event.is_set():
            frame_start: float = time.monotonic()

            # Build the color frame.
            with self._lock:
                colors: list[tuple[int, int, int, int]] = []
                for i in range(self._zone_count):
                    bri: float = self._zone_brightness[i]
                    if bri < BRIGHTNESS_FLOOR:
                        # Below floor — snap to black.
                        self._zone_brightness[i] = 0.0
                        colors.append((0, 0, 0, KELVIN_DEFAULT))
                    else:
                        hue: int = self._zone_hue[i]
                        bri_int: int = int(bri * BRI_MAX)
                        colors.append(
                            (hue, NOTE_SATURATION, bri_int, KELVIN_DEFAULT),
                        )
                        # Apply decay for next frame.
                        self._zone_brightness[i] *= self._decay

            # Push to device.
            try:
                self._device.set_zones(colors, duration_ms=0, rapid=True)
                self._frames_pushed += 1
            except Exception as exc:
                logger.debug("set_zones failed: %s", exc)

            # Sleep for remainder of frame interval.
            elapsed: float = time.monotonic() - frame_start
            sleep_time: float = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    # -------------------------------------------------------------------
    # MQTT connection
    # -------------------------------------------------------------------

    def _connect_mqtt(self) -> None:
        """Connect to the MQTT broker and subscribe to MIDI events."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise ImportError(
                "paho-mqtt is required.  Install with: pip install paho-mqtt"
            )

        client_id: str = f"glowup-midi-light-{int(time.time())}"
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        try:
            self._client.connect(self._broker, self._port)
            self._client.loop_start()
            self._connected = True
        except Exception as exc:
            logger.error("MQTT connect failed: %s", exc)
            self._connected = False

    def _disconnect_mqtt(self) -> None:
        """Disconnect from the MQTT broker."""
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
            self._connected = False

    def _on_connect(self, client: Any, userdata: Any, flags: Any,
                    reason_code: Any, properties: Any = None) -> None:
        """Handle MQTT connection — subscribe to MIDI events."""
        if reason_code == 0:
            topic: str = MQTT_SIGNAL_PREFIX + self._signal_name
            client.subscribe(topic, qos=MQTT_QOS)
            logger.info("Subscribed to %s", topic)
        else:
            logger.error("MQTT connect refused: %s", reason_code)

    def _on_disconnect(self, client: Any, userdata: Any, flags: Any,
                       reason_code: Any, properties: Any = None) -> None:
        """Handle MQTT disconnect."""
        if reason_code != 0:
            logger.warning("MQTT disconnected unexpectedly (rc=%s)",
                           reason_code)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Handle incoming MQTT message — parse and dispatch."""
        try:
            event: dict = json.loads(msg.payload.decode("utf-8"))
            self._on_midi_event(event)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.debug("Bad MIDI event payload: %s", exc)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Command-line entry point for the MIDI light bridge."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="GlowUp MIDI Light Bridge — MIDI events to LIFX colors",
    )
    parser.add_argument(
        "--ip", required=True,
        help="LIFX device IP address",
    )
    parser.add_argument(
        "--broker", default=DEFAULT_BROKER,
        help=f"MQTT broker host (default: {DEFAULT_BROKER})",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_MQTT_PORT,
        help=f"MQTT broker port (default: {DEFAULT_MQTT_PORT})",
    )
    parser.add_argument(
        "--signal-name", dest="signal_name", default=DEFAULT_SIGNAL_NAME,
        help=f"Signal name to subscribe to (default: '{DEFAULT_SIGNAL_NAME}')",
    )
    parser.add_argument(
        "--fps", type=int, default=DEFAULT_FPS,
        help=f"LIFX update rate in Hz (default: {DEFAULT_FPS})",
    )
    parser.add_argument(
        "--decay", type=float, default=DEFAULT_DECAY,
        help=f"Per-frame brightness decay (default: {DEFAULT_DECAY})",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )

    args: argparse.Namespace = parser.parse_args()

    # Configure logging.
    level: int = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    bridge: MidiLightBridge = MidiLightBridge(
        device_ip=args.ip,
        broker=args.broker,
        port=args.port,
        signal_name=args.signal_name,
        fps=args.fps,
        decay=args.decay,
    )

    # Handle Ctrl+C.
    def _shutdown(signum: int, frame: object) -> None:
        """Signal handler for clean shutdown."""
        bridge.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    bridge.start()


if __name__ == "__main__":
    main()
