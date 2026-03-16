"""MIDI file sensor — replay MIDI events onto the signal bus.

Reads a Standard MIDI File and emits structured events via MQTT at
the original tempo (or accelerated, for bulk ingest).  Downstream
operators and emitters receive the same event format regardless of
whether the source is a file, a live MIDI device, or a database
replay — the bus doesn't care about provenance.

This is a *sensor* in the SOE sense: it produces a signal.  A file
is a valid signal source, just like a microphone or a MIDI controller.

Usage::

    # Real-time replay (drives lights, audio, etc.)
    python3 -m distributed.midi_sensor --file song.mid --broker 10.0.0.48

    # Bulk / fast-forward (for data loading via persistence emitter)
    python3 -m distributed.midi_sensor --file song.mid --broker 10.0.0.48 --speed 0

The sensor publishes JSON events to ``glowup/signals/sensor:midi:events``
on the MQTT broker.  Any subscriber (persistence emitter, LIFX operator,
audio emitter) reacts to the same stream.

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
import time
from pathlib import Path
from typing import Optional

from .midi_parser import MidiEvent, MidiParser

logger: logging.Logger = logging.getLogger("glowup.midi_sensor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default MQTT broker (Pi).
DEFAULT_BROKER: str = "10.0.0.48"

# Default MQTT port.
DEFAULT_MQTT_PORT: int = 1883

# MQTT topic prefix for signals.
MQTT_SIGNAL_PREFIX: str = "glowup/signals/"

# Signal name for MIDI events on the bus.
DEFAULT_SIGNAL_NAME: str = "sensor:midi:events"

# Default replay speed multiplier (1.0 = real-time).
DEFAULT_SPEED: float = 1.0

# Speed value that means "as fast as possible" (no sleeps).
SPEED_UNLIMITED: float = 0.0

# MQTT QoS for MIDI events.  QoS 0 is fine — MIDI events are dense
# enough that dropping one is imperceptible.  For bulk ingest the
# persistence emitter uses its own QoS on its subscriptions.
MQTT_QOS: int = 0

# Progress reporting interval (events).
PROGRESS_INTERVAL: int = 500


# ---------------------------------------------------------------------------
# MidiSensor
# ---------------------------------------------------------------------------

class MidiSensor:
    """Replay MIDI file events onto the MQTT signal bus.

    Parses the MIDI file once, then iterates through events at the
    requested speed, publishing each as a JSON message.

    Args:
        file_path:   Path to the ``.mid`` file.
        broker:      MQTT broker hostname or IP.
        port:        MQTT broker port.
        signal_name: Signal name for the MQTT topic.
        speed:       Replay speed multiplier.  1.0 = real-time,
                     2.0 = double speed, 0.0 = as fast as possible.
    """

    def __init__(self, file_path: str, broker: str = DEFAULT_BROKER,
                 port: int = DEFAULT_MQTT_PORT,
                 signal_name: str = DEFAULT_SIGNAL_NAME,
                 speed: float = DEFAULT_SPEED) -> None:
        """Initialize the MIDI sensor.

        Args:
            file_path:   Path to the MIDI file.
            broker:      MQTT broker host.
            port:        MQTT broker port.
            signal_name: Bus signal name.
            speed:       Replay speed (0 = unlimited).
        """
        self._file_path: str = file_path
        self._broker: str = broker
        self._port: int = port
        self._signal_name: str = signal_name
        self._speed: float = speed
        self._client: Optional[object] = None
        self._connected: bool = False
        self._stop: bool = False

        # Stats.
        self._events_sent: int = 0
        self._start_time: float = 0.0

    def start(self) -> None:
        """Connect to MQTT, parse the file, and replay events.

        This method blocks until all events have been sent or
        :meth:`stop` is called.

        Raises:
            FileNotFoundError: If the MIDI file doesn't exist.
            ValueError:        If the file is not valid MIDI.
            ImportError:       If paho-mqtt is not installed.
        """
        # Parse the MIDI file.
        path: Path = Path(self._file_path)
        logger.info("Parsing MIDI file: %s", path)
        parser: MidiParser = MidiParser(path)
        summary: dict = parser.summary()
        logger.info(
            "MIDI: format %d, %d tracks, %d events, %.1f s, %.1f BPM",
            summary["format"], summary["tracks"], summary["total_events"],
            summary["duration_s"], summary["tempo_bpm"],
        )

        events: list[MidiEvent] = parser.events()
        if not events:
            logger.warning("No events in MIDI file")
            return

        # Connect to MQTT.
        self._connect_mqtt()
        if not self._connected:
            logger.error("Failed to connect to MQTT broker at %s:%d",
                         self._broker, self._port)
            return

        # Publish file metadata as a header event.
        self._publish_header(summary)

        # Replay events.
        self._start_time = time.monotonic()
        self._events_sent = 0
        speed_label: str = (
            "unlimited" if self._speed == SPEED_UNLIMITED
            else f"{self._speed}x"
        )
        logger.info(
            "Replaying %d events at %s speed → %s",
            len(events), speed_label, self._signal_name,
        )

        self._replay_events(events)

        # Publish end-of-stream marker.
        self._publish_end(summary)

        elapsed: float = time.monotonic() - self._start_time
        logger.info(
            "Replay complete: %d events in %.1f s (%.0f events/s)",
            self._events_sent, elapsed,
            self._events_sent / elapsed if elapsed > 0 else 0,
        )

        self._disconnect_mqtt()

    def stop(self) -> None:
        """Signal the replay loop to stop."""
        self._stop = True

    def _replay_events(self, events: list[MidiEvent]) -> None:
        """Iterate through events, sleeping to match tempo if needed.

        Args:
            events: Chronologically sorted list of MIDI events.
        """
        topic: str = MQTT_SIGNAL_PREFIX + self._signal_name
        wall_start: float = time.monotonic()
        realtime: bool = self._speed != SPEED_UNLIMITED

        for i, event in enumerate(events):
            if self._stop:
                logger.info("Replay stopped by user at event %d/%d",
                            i, len(events))
                break

            # Wait for the right time (if real-time mode).
            # Sleep for most of the interval, then busy-wait for
            # the last millisecond — time.sleep() granularity is
            # too coarse for tight MIDI timing, especially in VMs.
            if realtime and i > 0:
                target_wall: float = wall_start + (event.time_s / self._speed)
                now: float = time.monotonic()
                sleep_s: float = target_wall - now
                if sleep_s > 0.002:
                    time.sleep(sleep_s - 0.001)
                while time.monotonic() < target_wall:
                    pass

            # Publish the event.
            payload: str = json.dumps(event.to_dict(), separators=(",", ":"))
            self._client.publish(topic, payload, qos=MQTT_QOS)
            self._events_sent += 1

            # Progress reporting.
            if self._events_sent % PROGRESS_INTERVAL == 0:
                elapsed: float = time.monotonic() - self._start_time
                logger.info(
                    "  %d/%d events (%.1f s elapsed)",
                    self._events_sent, len(events), elapsed,
                )

    def _publish_header(self, summary: dict) -> None:
        """Publish a stream-start marker with file metadata.

        Args:
            summary: Parser summary dict.
        """
        header: dict = {
            "event_type": "stream_start",
            "source_file": Path(self._file_path).name,
            "format": summary["format"],
            "tracks": summary["tracks"],
            "total_events": summary["total_events"],
            "duration_s": summary["duration_s"],
            "tempo_bpm": summary["tempo_bpm"],
            "speed": self._speed,
        }
        topic: str = MQTT_SIGNAL_PREFIX + self._signal_name
        self._client.publish(
            topic, json.dumps(header, separators=(",", ":")),
            qos=MQTT_QOS,
        )

    def _publish_end(self, summary: dict) -> None:
        """Publish a stream-end marker.

        Args:
            summary: Parser summary dict.
        """
        end: dict = {
            "event_type": "stream_end",
            "source_file": Path(self._file_path).name,
            "events_sent": self._events_sent,
        }
        topic: str = MQTT_SIGNAL_PREFIX + self._signal_name
        self._client.publish(
            topic, json.dumps(end, separators=(",", ":")),
            qos=MQTT_QOS,
        )

    # -------------------------------------------------------------------
    # MQTT connection
    # -------------------------------------------------------------------

    def _connect_mqtt(self) -> None:
        """Connect to the MQTT broker using paho-mqtt.

        Raises:
            ImportError: If paho-mqtt is not installed.
        """
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise ImportError(
                "paho-mqtt is required for MidiSensor.  "
                "Install with: pip install paho-mqtt"
            )

        client_id: str = f"glowup-midi-sensor-{int(time.time())}"
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )

        # Synchronous connect with timeout.
        try:
            self._client.connect(self._broker, self._port)
            self._client.loop_start()
            self._connected = True
            logger.info("Connected to MQTT broker at %s:%d",
                        self._broker, self._port)
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


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Command-line entry point for the MIDI sensor."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="GlowUp MIDI Sensor — replay MIDI files onto the signal bus",
    )
    parser.add_argument(
        "--file", required=True,
        help="Path to a Standard MIDI File (.mid)",
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
        "--signal-name", default=DEFAULT_SIGNAL_NAME,
        help=f"Signal name on the bus (default: '{DEFAULT_SIGNAL_NAME}')",
    )
    parser.add_argument(
        "--speed", type=float, default=DEFAULT_SPEED,
        help=(
            f"Replay speed multiplier (default: {DEFAULT_SPEED}).  "
            f"0 = as fast as possible (bulk ingest mode)."
        ),
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

    sensor: MidiSensor = MidiSensor(
        file_path=args.file,
        broker=args.broker,
        port=args.port,
        signal_name=args.signal_name,
        speed=args.speed,
    )

    # Handle Ctrl+C gracefully.
    def _shutdown(signum: int, frame: object) -> None:
        """Signal handler for clean shutdown."""
        logger.info("Shutting down...")
        sensor.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    sensor.start()


if __name__ == "__main__":
    main()
