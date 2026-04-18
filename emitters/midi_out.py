"""MIDI output emitter — plays MIDI events through a pluggable synth backend.

Subscribes to ``sensor:midi:events`` on the MQTT bus and routes incoming
MIDI events to a synthesizer backend.  The backend is pluggable:

* **fluidsynth** — software synth using SoundFont2 files.  Standalone,
  no external app needed.  ``brew install fluid-synth`` +
  ``pip install pyfluidsynth``.

* **rtmidi** — routes MIDI to any system MIDI destination (virtual ports,
  DAWs, hardware synths).  ``pip install python-rtmidi``.

The emitter handles bus subscription and event dispatch; the backend
handles sound production.  Same separation as TransportAdapter.

Usage (standalone)::

    python3 -m emitters.midi_out --backend fluidsynth --soundfont /path/to/gm.sf2

Usage (via worker agent)::

    # agent.json declares the emitter, orchestrator assigns work
    python3 -m distributed.worker_agent agent.json
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import json
import logging
import signal
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from network_config import net

logger: logging.Logger = logging.getLogger("glowup.emitters.midi_out")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default MQTT broker (Pi).
DEFAULT_BROKER: str = net.broker

# Default MQTT port.
DEFAULT_MQTT_PORT: int = 1883

# MQTT topic prefix for signals.
MQTT_SIGNAL_PREFIX: str = "glowup/signals/"

# Default signal name to subscribe to.
DEFAULT_SIGNAL_NAME: str = "sensor:midi:events"

# MQTT QoS for subscription.
MQTT_QOS: int = 0

# Control channel topic — emitter listens for station-switch commands.
# Publish JSON like {"tune": "sensor:midi:jazz"} to switch stations,
# or {"list": true} to request the current station name.
CONTROL_TOPIC: str = "glowup/midi_emitter/control"

# Response topic — emitter publishes status here.
STATUS_TOPIC: str = "glowup/midi_emitter/status"

# Available backend names.
BACKEND_FLUIDSYNTH: str = "fluidsynth"
BACKEND_RTMIDI: str = "rtmidi"
VALID_BACKENDS: list[str] = [BACKEND_FLUIDSYNTH, BACKEND_RTMIDI]

# Default FluidSynth gain (0.0 - 10.0).
DEFAULT_FLUID_GAIN: float = 0.8

# Default MIDI velocity scaling (some soundfonts are quiet).
DEFAULT_VELOCITY_SCALE: float = 1.0

# Maximum MIDI value.
MIDI_MAX: int = 127

# Pitch bend center.
PITCH_BEND_CENTER: int = 8192


# ---------------------------------------------------------------------------
# SynthBackend ABC
# ---------------------------------------------------------------------------

class SynthBackend(ABC):
    """Abstract interface for MIDI synthesis backends.

    Each backend translates high-level MIDI operations into its
    native API.  The emitter calls these methods; the backend
    produces sound (or routes MIDI elsewhere).
    """

    @abstractmethod
    def start(self) -> None:
        """Initialize the backend and acquire audio resources."""

    @abstractmethod
    def stop(self) -> None:
        """Release all resources and stop producing sound."""

    @abstractmethod
    def note_on(self, channel: int, note: int, velocity: int) -> None:
        """Start a note.

        Args:
            channel:  MIDI channel (0-15).
            note:     Note number (0-127).
            velocity: Velocity (1-127, 0 = note_off by convention).
        """

    @abstractmethod
    def note_off(self, channel: int, note: int) -> None:
        """Stop a note.

        Args:
            channel: MIDI channel (0-15).
            note:    Note number (0-127).
        """

    @abstractmethod
    def control_change(self, channel: int, cc: int, value: int) -> None:
        """Send a control change message.

        Args:
            channel: MIDI channel (0-15).
            cc:      Controller number (0-127).
            value:   Controller value (0-127).
        """

    @abstractmethod
    def program_change(self, channel: int, program: int) -> None:
        """Change the instrument on a channel.

        Args:
            channel: MIDI channel (0-15).
            program: Program number (0-127).
        """

    @abstractmethod
    def pitch_bend(self, channel: int, value: int) -> None:
        """Send a pitch bend message.

        Args:
            channel: MIDI channel (0-15).
            value:   14-bit value (0-16383, center=8192).
        """

    def all_notes_off(self) -> None:
        """Silence all notes on all channels.

        Default implementation sends CC 123 (All Notes Off) on
        all 16 channels.  Backends may override for native support.
        """
        for ch in range(16):
            self.control_change(ch, 123, 0)


# ---------------------------------------------------------------------------
# FluidSynthBackend
# ---------------------------------------------------------------------------

class FluidSynthBackend(SynthBackend):
    """FluidSynth software synthesizer backend.

    Uses the ``pyfluidsynth`` Python binding to render MIDI through
    a SoundFont2 file directly to the system audio output.
    Self-contained — no external app or routing needed.

    Args:
        soundfont: Path to a ``.sf2`` SoundFont file.
        gain:      Master volume (0.0-10.0).
    """

    def __init__(self, soundfont: str, gain: float = DEFAULT_FLUID_GAIN) -> None:
        """Initialize with a SoundFont path.

        Args:
            soundfont: Path to the .sf2 file.
            gain:      FluidSynth master gain.
        """
        self._soundfont_path: str = soundfont
        self._gain: float = gain
        self._fs: Optional[Any] = None
        self._sfid: int = -1

    def start(self) -> None:
        """Initialize FluidSynth and load the SoundFont.

        Raises:
            ImportError: If pyfluidsynth is not installed.
            FileNotFoundError: If the SoundFont file doesn't exist.
        """
        try:
            import fluidsynth
        except ImportError:
            raise ImportError(
                "pyfluidsynth is required for the fluidsynth backend.  "
                "Install with: brew install fluid-synth && "
                "pip install pyfluidsynth"
            )

        from pathlib import Path
        if not Path(self._soundfont_path).exists():
            raise FileNotFoundError(
                f"SoundFont not found: {self._soundfont_path}"
            )

        self._fs = fluidsynth.Synth(gain=self._gain)
        self._fs.start(driver="coreaudio")
        self._sfid = self._fs.sfload(self._soundfont_path, update_midi_preset=1)
        if self._sfid < 0:
            raise RuntimeError(
                f"FluidSynth failed to load SoundFont: {self._soundfont_path}"
            )

        # Don't pre-assign programs — let the MIDI file's own
        # program_change and bank select CCs configure everything.
        # Pre-setting with program_select can conflict with later
        # program_change calls that use different internal paths.

        logger.info(
            "FluidSynth started — soundfont=%s, gain=%.1f",
            self._soundfont_path, self._gain,
        )

    def stop(self) -> None:
        """Shut down FluidSynth."""
        if self._fs is not None:
            self.all_notes_off()
            try:
                self._fs.delete()
            except Exception as exc:
                logger.debug("Error deleting FluidSynth instance: %s", exc)
            self._fs = None

    def note_on(self, channel: int, note: int, velocity: int) -> None:
        """Play a note via FluidSynth."""
        if self._fs is not None:
            self._fs.noteon(channel, note, min(velocity, MIDI_MAX))

    def note_off(self, channel: int, note: int) -> None:
        """Stop a note via FluidSynth."""
        if self._fs is not None:
            self._fs.noteoff(channel, note)

    def control_change(self, channel: int, cc: int, value: int) -> None:
        """Send CC via FluidSynth."""
        if self._fs is not None:
            self._fs.cc(channel, cc, min(value, MIDI_MAX))

    def program_change(self, channel: int, program: int) -> None:
        """Change program via FluidSynth using standard MIDI program change.

        Uses program_change (not program_select) so FluidSynth
        respects bank select CCs already sent on the channel.
        This matters for channel 9 (drums) which uses a different bank.
        """
        if self._fs is not None:
            self._fs.program_change(channel, program)

    def pitch_bend(self, channel: int, value: int) -> None:
        """Send pitch bend via FluidSynth.

        The MIDI wire format uses 0-16383 (center=8192).
        pyfluidsynth expects -8192 to +8191 (center=0) and
        adds 8192 internally.  We convert here.
        """
        if self._fs is not None:
            self._fs.pitch_bend(channel, value - 8192)

    def all_notes_off(self) -> None:
        """Silence all channels via FluidSynth system reset."""
        if self._fs is not None:
            for ch in range(16):
                self._fs.cc(ch, 123, 0)


# ---------------------------------------------------------------------------
# RtMidiBackend
# ---------------------------------------------------------------------------

class RtMidiBackend(SynthBackend):
    """rtmidi backend — routes MIDI to system MIDI destinations.

    Creates a virtual MIDI output port that any DAW, synth app,
    or hardware synth can connect to.  Does not produce sound
    itself — it's a router.

    Args:
        port_name: Name for the virtual MIDI port.
    """

    def __init__(self, port_name: str = "GlowUp MIDI Out") -> None:
        """Initialize with a virtual port name.

        Args:
            port_name: Name visible in MIDI routing apps.
        """
        self._port_name: str = port_name
        self._midi_out: Optional[Any] = None

    def start(self) -> None:
        """Create the virtual MIDI output port.

        Raises:
            ImportError: If python-rtmidi is not installed.
        """
        try:
            import rtmidi
        except ImportError:
            raise ImportError(
                "python-rtmidi is required for the rtmidi backend.  "
                "Install with: pip install python-rtmidi"
            )

        self._midi_out = rtmidi.MidiOut()
        self._midi_out.open_virtual_port(self._port_name)
        logger.info("rtmidi virtual port opened: %s", self._port_name)

    def stop(self) -> None:
        """Close the MIDI port."""
        if self._midi_out is not None:
            self.all_notes_off()
            self._midi_out.close_port()
            self._midi_out = None

    def _send(self, message: list[int]) -> None:
        """Send a raw MIDI message.

        Args:
            message: List of MIDI bytes.
        """
        if self._midi_out is not None:
            self._midi_out.send_message(message)

    def note_on(self, channel: int, note: int, velocity: int) -> None:
        """Send note-on via rtmidi."""
        self._send([0x90 | (channel & 0x0F), note & 0x7F, velocity & 0x7F])

    def note_off(self, channel: int, note: int) -> None:
        """Send note-off via rtmidi."""
        self._send([0x80 | (channel & 0x0F), note & 0x7F, 0])

    def control_change(self, channel: int, cc: int, value: int) -> None:
        """Send CC via rtmidi."""
        self._send([0xB0 | (channel & 0x0F), cc & 0x7F, value & 0x7F])

    def program_change(self, channel: int, program: int) -> None:
        """Send program change via rtmidi."""
        self._send([0xC0 | (channel & 0x0F), program & 0x7F])

    def pitch_bend(self, channel: int, value: int) -> None:
        """Send pitch bend via rtmidi."""
        lsb: int = value & 0x7F
        msb: int = (value >> 7) & 0x7F
        self._send([0xE0 | (channel & 0x0F), lsb, msb])


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------

def create_backend(name: str, **kwargs: Any) -> SynthBackend:
    """Create a synth backend by name.

    Args:
        name:     Backend name (``"fluidsynth"`` or ``"rtmidi"``).
        **kwargs: Backend-specific arguments.

    Returns:
        An initialized (but not started) :class:`SynthBackend`.

    Raises:
        ValueError: If the backend name is not recognized.
    """
    if name == BACKEND_FLUIDSYNTH:
        soundfont: str = kwargs.get("soundfont", "")
        if not soundfont:
            raise ValueError(
                "FluidSynth backend requires --soundfont /path/to/file.sf2"
            )
        gain: float = kwargs.get("gain", DEFAULT_FLUID_GAIN)
        return FluidSynthBackend(soundfont=soundfont, gain=gain)

    elif name == BACKEND_RTMIDI:
        port_name: str = kwargs.get("port_name", "GlowUp MIDI Out")
        return RtMidiBackend(port_name=port_name)

    else:
        raise ValueError(
            f"Unknown synth backend '{name}'.  "
            f"Available: {', '.join(VALID_BACKENDS)}"
        )


# ---------------------------------------------------------------------------
# MidiOutEmitter — standalone bus subscriber
# ---------------------------------------------------------------------------

class MidiOutEmitter:
    """Subscribe to MIDI events on the bus and play them through a backend.

    This is a standalone component (not managed by EmitterManager)
    because it subscribes to MQTT directly and dispatches events
    to the synth backend in real time.

    Args:
        backend:     A :class:`SynthBackend` instance.
        broker:      MQTT broker hostname or IP.
        port:        MQTT broker port.
        signal_name: Signal name to subscribe to on the bus.
        velocity_scale: Multiply all velocities by this factor.
    """

    def __init__(self, backend: SynthBackend,
                 broker: str = DEFAULT_BROKER,
                 port: int = DEFAULT_MQTT_PORT,
                 signal_name: str = DEFAULT_SIGNAL_NAME,
                 velocity_scale: float = DEFAULT_VELOCITY_SCALE) -> None:
        """Initialize the MIDI output emitter.

        Args:
            backend:        Synth backend for sound production.
            broker:         MQTT broker host.
            port:           MQTT broker port.
            signal_name:    Bus signal to subscribe to.
            velocity_scale: Velocity multiplier.
        """
        self._backend: SynthBackend = backend
        self._broker: str = broker
        self._port: int = port
        self._signal_name: str = signal_name
        self._velocity_scale: float = velocity_scale
        self._client: Optional[Any] = None
        self._connected: bool = False
        self._stop: bool = False

        # Stats.
        self._events_received: int = 0
        self._notes_played: int = 0
        self._start_time: float = 0.0

    def start(self) -> None:
        """Start the backend, connect to MQTT, and block until stopped.

        Raises:
            ImportError: If paho-mqtt or the backend dependency is missing.
        """
        # Start the synth backend.
        logger.info("Starting synth backend...")
        self._backend.start()

        # Connect to MQTT.
        self._connect_mqtt()
        if not self._connected:
            logger.error("Failed to connect to MQTT broker at %s:%d",
                         self._broker, self._port)
            self._backend.stop()
            return

        self._start_time = time.monotonic()
        logger.info(
            "MidiOutEmitter listening on %s — press Ctrl+C to stop",
            self._signal_name,
        )

        # Block until stopped.
        try:
            while not self._stop:
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass

        self._shutdown()

    def stop(self) -> None:
        """Signal the emitter to stop."""
        self._stop = True

    def tune(self, new_signal: str) -> None:
        """Switch to a different station (signal name) at runtime.

        Silences all current notes, unsubscribes from the old topic,
        and subscribes to the new one.  No restart needed.

        Args:
            new_signal: New signal name (e.g. ``"sensor:midi:jazz"``).
        """
        if new_signal == self._signal_name:
            logger.info("Already tuned to %s", new_signal)
            return

        old_signal: str = self._signal_name
        old_topic: str = MQTT_SIGNAL_PREFIX + old_signal
        new_topic: str = MQTT_SIGNAL_PREFIX + new_signal

        # Silence everything currently playing.
        self._backend.all_notes_off()

        # Unsubscribe from old, subscribe to new.
        if self._client and self._connected:
            self._client.unsubscribe(old_topic)
            self._client.subscribe(new_topic, qos=MQTT_QOS)

        self._signal_name = new_signal
        logger.info("Tuned: %s → %s", old_signal, new_signal)

        # Publish status so other tools can see what we're listening to.
        self._publish_status()

    def _publish_status(self) -> None:
        """Publish current station info to the status topic."""
        if self._client and self._connected:
            status: dict = {
                "station": self._signal_name,
                "events_received": self._events_received,
                "notes_played": self._notes_played,
            }
            self._client.publish(
                STATUS_TOPIC,
                json.dumps(status, separators=(",", ":")),
                qos=MQTT_QOS,
            )

    def _shutdown(self) -> None:
        """Clean shutdown — silence notes, disconnect, stop backend."""
        logger.info("Shutting down...")
        self._backend.all_notes_off()
        self._disconnect_mqtt()
        self._backend.stop()

        elapsed: float = time.monotonic() - self._start_time
        logger.info(
            "MidiOutEmitter stopped — %d events, %d notes in %.1f s",
            self._events_received, self._notes_played, elapsed,
        )

    def _on_midi_event(self, event: dict) -> None:
        """Dispatch a single MIDI event to the backend.

        Args:
            event: Parsed JSON event dict from the bus.
        """
        self._events_received += 1
        event_type: str = event.get("event_type", "")

        if event_type == "note_on":
            channel: int = event.get("channel", 0)
            note: int = event.get("note", 60)
            velocity: int = event.get("velocity", 100)
            # Apply velocity scaling.
            velocity = min(
                int(velocity * self._velocity_scale), MIDI_MAX,
            )
            self._backend.note_on(channel, note, velocity)
            self._notes_played += 1

        elif event_type == "note_off":
            self._backend.note_off(
                event.get("channel", 0),
                event.get("note", 60),
            )

        elif event_type == "control_change":
            self._backend.control_change(
                event.get("channel", 0),
                event.get("cc_number", 0),
                event.get("cc_value", 0),
            )

        elif event_type == "program_change":
            ch = event.get("channel", 0)
            prog = event.get("program", 0)
            logger.info("Program change: ch=%d prog=%d", ch, prog)
            self._backend.program_change(ch, prog,
            )

        elif event_type == "pitch_bend":
            self._backend.pitch_bend(
                event.get("channel", 0),
                event.get("pitch_bend", PITCH_BEND_CENTER),
            )

        elif event_type == "stream_start":
            source: str = event.get("source_file", "unknown")
            logger.info("Stream started: %s", source)

        elif event_type == "stream_end":
            logger.info("Stream ended — %d events received",
                        self._events_received)

        # Meta events, sysex, etc. are silently ignored.

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

        client_id: str = f"glowup-midi-emitter-{int(time.time())}"
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
        """Handle MQTT connection — subscribe to signal and control topics."""
        if reason_code == 0:
            # Subscribe to the MIDI event stream.
            topic: str = MQTT_SIGNAL_PREFIX + self._signal_name
            client.subscribe(topic, qos=MQTT_QOS)
            logger.info("Subscribed to %s", topic)

            # Subscribe to the control channel for station switching.
            client.subscribe(CONTROL_TOPIC, qos=1)
            logger.info("Listening for control commands on %s", CONTROL_TOPIC)

            # Announce current station.
            self._publish_status()
        else:
            logger.error("MQTT connect refused: %s", reason_code)

    def _on_disconnect(self, client: Any, userdata: Any, flags: Any,
                       reason_code: Any, properties: Any = None) -> None:
        """Handle MQTT disconnect."""
        if reason_code != 0:
            logger.warning("MQTT disconnected unexpectedly (rc=%s)",
                           reason_code)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Handle incoming MQTT messages — MIDI events or control commands."""
        # Control channel — station switching.
        if msg.topic == CONTROL_TOPIC:
            self._handle_control(msg.payload)
            return

        # MIDI event stream.
        try:
            event: dict = json.loads(msg.payload.decode("utf-8"))
            self._on_midi_event(event)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.debug("Bad MIDI event payload: %s", exc)

    def _handle_control(self, payload: bytes) -> None:
        """Process a control channel command.

        Supported commands (JSON):

        * ``{"tune": "sensor:midi:jazz"}`` — switch station.
        * ``{"status": true}`` — publish current station to status topic.

        Args:
            payload: Raw MQTT payload bytes.
        """
        try:
            cmd: dict = json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            logger.debug("Bad control payload")
            return

        if "tune" in cmd:
            new_signal: str = str(cmd["tune"])
            logger.info("Control: tune → %s", new_signal)
            self.tune(new_signal)

        if "status" in cmd:
            self._publish_status()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Command-line entry point for the MIDI output emitter."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="GlowUp MIDI Emitter — play MIDI events from the signal bus",
    )
    parser.add_argument(
        "--backend", required=True, choices=VALID_BACKENDS,
        help="Synth backend: fluidsynth (standalone) or rtmidi (routing)",
    )
    parser.add_argument(
        "--soundfont", default="",
        help="Path to a SoundFont2 (.sf2) file (required for fluidsynth)",
    )
    parser.add_argument(
        "--gain", type=float, default=DEFAULT_FLUID_GAIN,
        help=f"FluidSynth master gain (default: {DEFAULT_FLUID_GAIN})",
    )
    parser.add_argument(
        "--port-name", dest="port_name", default="GlowUp MIDI Out",
        help="Virtual MIDI port name (rtmidi backend, default: 'GlowUp MIDI Out')",
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
        "--velocity-scale", dest="velocity_scale",
        type=float, default=DEFAULT_VELOCITY_SCALE,
        help=f"Velocity multiplier (default: {DEFAULT_VELOCITY_SCALE})",
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

    # Create the backend.
    backend: SynthBackend = create_backend(
        args.backend,
        soundfont=args.soundfont,
        gain=args.gain,
        port_name=args.port_name,
    )

    # Create and start the emitter.
    emitter: MidiOutEmitter = MidiOutEmitter(
        backend=backend,
        broker=args.broker,
        port=args.port,
        signal_name=args.signal_name,
        velocity_scale=args.velocity_scale,
    )

    # Handle Ctrl+C.
    def _shutdown(signum: int, frame: object) -> None:
        """Signal handler for clean shutdown."""
        emitter.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    emitter.start()


if __name__ == "__main__":
    main()
