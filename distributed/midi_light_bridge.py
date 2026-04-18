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

    python3 -m distributed.midi_light_bridge --ip 192.0.2.62

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import json
import logging
import signal
import threading
import time
from typing import Any, Optional

from network_config import net

logger: logging.Logger = logging.getLogger("glowup.midi_light_bridge")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default MQTT broker (from centralized network config).
DEFAULT_BROKER: str = net.broker

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
DEFAULT_DECAY: float = 0.0

# Minimum brightness before snapping to zero (prevents dim tails).
BRIGHTNESS_FLOOR: float = 0.0

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

    def __init__(self, device_ips: list[str],
                 broker: str = DEFAULT_BROKER,
                 port: int = DEFAULT_MQTT_PORT,
                 signal_name: str = DEFAULT_SIGNAL_NAME,
                 fps: int = DEFAULT_FPS,
                 decay: float = DEFAULT_DECAY) -> None:
        """Initialize the MIDI light bridge.

        Args:
            device_ips:  List of LIFX device IPs (combined as virtual multizone).
            broker:      MQTT broker host.
            port:        MQTT broker port.
            signal_name: Bus signal name.
            fps:         LIFX update rate.
            decay:       Brightness decay factor.
        """
        self._device_ips: list[str] = device_ips
        self._broker: str = broker
        self._port: int = port
        self._signal_name: str = signal_name
        self._fps: int = fps
        self._decay: float = decay

        self._client: Optional[Any] = None
        self._connected: bool = False
        self._stop_event: threading.Event = threading.Event()

        # Discovered devices and their zone ranges in the virtual strip.
        # Each entry: (device, start_zone, zone_count)
        self._devices: list[tuple[Any, int, int]] = []
        self._zone_count: int = 0

        # Active notes: (channel, note) → {velocity, zone, hue}.
        # Notes stay active until note_off — no decay timer.
        self._active_notes: dict[tuple[int, int], dict] = {}
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
        # Discover all LIFX devices.
        self._discover_devices()
        if not self._devices:
            logger.error("No LIFX devices found")
            return

        # Clear active notes.
        self._active_notes = {}

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
            "MidiLightBridge running — %d devices, %d zones at %d fps",
            len(self._devices), self._zone_count, self._fps,
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

        # Black out all devices.
        for dev, _start, count in self._devices:
            try:
                black: list[tuple[int, int, int, int]] = [
                    (0, 0, 0, KELVIN_DEFAULT)
                ] * count
                dev.set_zones(black, duration_ms=500)
            except Exception as exc:
                logger.debug("Failed to black out device on stop: %s", exc)

        self._disconnect_mqtt()

        elapsed: float = time.monotonic() - self._start_time
        logger.info(
            "MidiLightBridge stopped — %d notes, %d frames in %.1f s",
            self._notes_received, self._frames_pushed, elapsed,
        )

    # -------------------------------------------------------------------
    # LIFX device discovery
    # -------------------------------------------------------------------

    def _discover_devices(self) -> None:
        """Find and connect to all LIFX devices, building a virtual strip."""
        from transport import discover_devices

        offset: int = 0
        for ip in self._device_ips:
            logger.info("Discovering LIFX device at %s...", ip)
            found = discover_devices(target_ip=ip, timeout=5)
            if not found:
                logger.warning("Device not found at %s — skipping", ip)
                continue

            dev = found[0]
            dev.set_power(True)
            zones: int = dev.zone_count
            self._devices.append((dev, offset, zones))
            logger.info(
                "  %s — %d zones (offset %d, powered on)",
                dev.label, zones, offset,
            )
            offset += zones

        self._zone_count = offset
        if self._zone_count > 0:
            logger.info("Virtual strip: %d total zones across %d devices",
                        self._zone_count, len(self._devices))

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
            self._notes_received += 1
            if self._notes_received <= 3:
                logger.info("Note %d: ch=%d note=%d vel=%d",
                            self._notes_received, channel, note, velocity)
            self._note_on(channel, note, velocity)

        elif event_type == "note_off":
            channel: int = event.get("channel", 0)
            note: int = event.get("note", 60)
            self._note_off(channel, note)

    def _note_on(self, channel: int, note: int, velocity: int) -> None:
        """Register an active note — light stays on until note_off.

        Args:
            channel:  MIDI channel (determines hue).
            note:     MIDI note number (determines zone position).
            velocity: Note velocity (determines brightness).
        """
        if self._zone_count == 0:
            return

        # Map note to zone position.
        note_frac: float = (note - MIDI_NOTE_LOW) / max(
            MIDI_NOTE_HIGH - MIDI_NOTE_LOW, 1,
        )
        note_frac = max(0.0, min(1.0, note_frac))
        center_zone: int = int(note_frac * (self._zone_count - 1))

        hue: int = CHANNEL_HUES[channel & 0x0F]
        brightness: float = velocity / 127.0

        with self._lock:
            self._active_notes[(channel, note)] = {
                "zone": center_zone,
                "hue": hue,
                "brightness": brightness,
            }

    def _note_off(self, channel: int, note: int) -> None:
        """Remove an active note — zone goes dark.

        Args:
            channel: MIDI channel.
            note:    MIDI note number.
        """
        with self._lock:
            self._active_notes.pop((channel, note), None)

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

            # Build the color frame from active notes.
            with self._lock:
                # Start with all zones black.
                zone_bri: list[float] = [0.0] * self._zone_count
                zone_hue: list[int] = [0] * self._zone_count

                # Light zones for every held note.
                for info in self._active_notes.values():
                    z: int = info["zone"]
                    if 0 <= z < self._zone_count:
                        if info["brightness"] > zone_bri[z]:
                            zone_bri[z] = info["brightness"]
                            zone_hue[z] = info["hue"]

                colors: list[tuple[int, int, int, int]] = []
                for i in range(self._zone_count):
                    if zone_bri[i] > 0.0:
                        colors.append((
                            zone_hue[i], NOTE_SATURATION,
                            int(zone_bri[i] * BRI_MAX), KELVIN_DEFAULT,
                        ))
                    else:
                        colors.append((0, 0, 0, KELVIN_DEFAULT))

            # Push each device's slice of the virtual strip.
            for dev, start, count in self._devices:
                device_colors = colors[start:start + count]
                try:
                    dev.set_zones(device_colors, duration_ms=0)
                except Exception as exc:
                    logger.warning("set_zones failed on %s: %s",
                                   dev.label, exc)
            self._frames_pushed += 1

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
            except Exception as exc:
                logger.debug("MQTT disconnect failed: %s", exc)
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
        "--ip", required=True, nargs="+",
        help="LIFX device IP address(es) — multiple IPs form a virtual strip",
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
        device_ips=args.ip,
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
