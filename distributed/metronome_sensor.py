"""Metronome sensor — publish a steady clock signal to the bus.

Publishes beat ticks at a fixed BPM to the MQTT signal bus.  Any
effect, operator, or emitter can subscribe to synchronize its
animation cycle to the shared clock.

Two effects on two devices started at different times will animate
in lockstep if they both follow the metronome instead of their own
``time.time()``.

The metronome publishes a JSON message on every beat::

    {"beat": 42, "bpm": 120, "phase": 0.0, "time_s": 21.0}

* ``beat`` — monotonic beat count (integer, from 0).
* ``bpm`` — current tempo.
* ``phase`` — position within the current beat (0.0–1.0).
  Published at sub-beat resolution for smooth animation.
* ``time_s`` — seconds since the metronome started.

Effects read ``phase`` for smooth animation and ``beat`` for
discrete triggers (e.g., flash on the downbeat).

Usage::

    python3 -m distributed.metronome_sensor --bpm 120
    python3 -m distributed.metronome_sensor --bpm 90 --subdivide 4

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
from typing import Any, Optional

from network_config import net

logger: logging.Logger = logging.getLogger("glowup.metronome_sensor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default MQTT broker (from centralized network config).
DEFAULT_BROKER: str = net.broker

# Default MQTT port.
DEFAULT_MQTT_PORT: int = 1883

# MQTT topic prefix.
MQTT_SIGNAL_PREFIX: str = "glowup/signals/"

# Signal name for the metronome.
DEFAULT_SIGNAL_NAME: str = "sensor:metronome:tick"

# MQTT QoS.
MQTT_QOS: int = 0

# Default BPM.
DEFAULT_BPM: float = 120.0

# Minimum and maximum BPM.
BPM_MIN: float = 20.0
BPM_MAX: float = 300.0

# Default publish rate — how many messages per beat.
# Higher = smoother phase updates for effects.
# 1 = one message per beat (coarse).
# 4 = four per beat (quarter-beat resolution).
# 16 = sixteen per beat (smooth phase for animation).
DEFAULT_SUBDIVIDE: int = 16

# Seconds per minute.
SECONDS_PER_MINUTE: float = 60.0


# ---------------------------------------------------------------------------
# MetronomeSensor
# ---------------------------------------------------------------------------

class MetronomeSensor:
    """Publish steady clock ticks to the MQTT bus.

    Runs a tight loop that publishes beat/phase messages at
    sub-beat resolution.  Uses hybrid sleep + busy-wait for
    accurate timing.

    Args:
        bpm:         Tempo in beats per minute.
        broker:      MQTT broker host.
        port:        MQTT broker port.
        signal_name: Signal name on the bus.
        subdivide:   Messages per beat (phase resolution).
    """

    def __init__(self, bpm: float = DEFAULT_BPM,
                 broker: str = DEFAULT_BROKER,
                 port: int = DEFAULT_MQTT_PORT,
                 signal_name: str = DEFAULT_SIGNAL_NAME,
                 subdivide: int = DEFAULT_SUBDIVIDE) -> None:
        """Initialize the metronome sensor.

        Args:
            bpm:         Beats per minute.
            broker:      MQTT broker host.
            port:        MQTT broker port.
            signal_name: Bus signal name.
            subdivide:   Sub-beat resolution.
        """
        self._bpm: float = max(BPM_MIN, min(BPM_MAX, bpm))
        self._broker: str = broker
        self._port: int = port
        self._signal_name: str = signal_name
        self._subdivide: int = max(1, subdivide)
        self._client: Optional[Any] = None
        self._connected: bool = False
        self._stop: bool = False

    def start(self) -> None:
        """Connect to MQTT and run the metronome loop.

        Blocks until stopped.
        """
        self._connect_mqtt()
        if not self._connected:
            logger.error("Failed to connect to MQTT broker at %s:%d",
                         self._broker, self._port)
            return

        beat_duration: float = SECONDS_PER_MINUTE / self._bpm
        tick_interval: float = beat_duration / self._subdivide
        topic: str = MQTT_SIGNAL_PREFIX + self._signal_name

        logger.info(
            "Metronome started — %.1f BPM, %d subdivisions, "
            "tick every %.1f ms → %s",
            self._bpm, self._subdivide,
            tick_interval * 1000.0, self._signal_name,
        )

        t0: float = time.monotonic()
        tick_count: int = 0

        while not self._stop:
            # Current time since start.
            elapsed: float = time.monotonic() - t0

            # Beat and phase.
            beat_float: float = elapsed / beat_duration
            beat: int = int(beat_float)
            phase: float = beat_float - beat

            # Publish.
            msg: dict = {
                "beat": beat,
                "bpm": self._bpm,
                "phase": round(phase, 4),
                "time_s": round(elapsed, 4),
            }
            self._client.publish(
                topic,
                json.dumps(msg, separators=(",", ":")),
                qos=MQTT_QOS,
            )

            tick_count += 1

            # Log every 4 beats.
            if tick_count % (self._subdivide * 4) == 0:
                logger.info("Beat %d (%.1f s)", beat, elapsed)

            # Wait for next tick — hybrid sleep + busy-wait.
            next_tick: float = t0 + tick_count * tick_interval
            now: float = time.monotonic()
            sleep_s: float = next_tick - now
            if sleep_s > 0.002:
                time.sleep(sleep_s - 0.001)
            while time.monotonic() < next_tick:
                pass

        self._disconnect_mqtt()
        logger.info("Metronome stopped after %d ticks (%.1f s)",
                     tick_count, time.monotonic() - t0)

    def stop(self) -> None:
        """Signal the metronome to stop."""
        self._stop = True

    # -------------------------------------------------------------------
    # MQTT connection
    # -------------------------------------------------------------------

    def _connect_mqtt(self) -> None:
        """Connect to the MQTT broker."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise ImportError(
                "paho-mqtt is required.  Install with: pip install paho-mqtt"
            )

        client_id: str = f"glowup-metronome-{int(time.time())}"
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )

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
    """Command-line entry point for the metronome sensor."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="GlowUp Metronome — publish steady beat clock to the bus",
    )
    parser.add_argument(
        "--bpm", type=float, default=DEFAULT_BPM,
        help=f"Beats per minute (default: {DEFAULT_BPM})",
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
        "--signal-name", dest="signal_name",
        default=DEFAULT_SIGNAL_NAME,
        help=f"Signal name (default: '{DEFAULT_SIGNAL_NAME}')",
    )
    parser.add_argument(
        "--subdivide", type=int, default=DEFAULT_SUBDIVIDE,
        help=f"Messages per beat (default: {DEFAULT_SUBDIVIDE})",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )

    args: argparse.Namespace = parser.parse_args()

    level: int = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    metro: MetronomeSensor = MetronomeSensor(
        bpm=args.bpm,
        broker=args.broker,
        port=args.port,
        signal_name=args.signal_name,
        subdivide=args.subdivide,
    )

    def _shutdown(signum: int, frame: object) -> None:
        metro.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    metro.start()


if __name__ == "__main__":
    main()
