"""Theremin audio synthesizer — generates tone from SignalBus note data.

Subscribes to the MQTT broker for note frequency and amplitude signals
published by the ThereminEffect on the Pi.  Generates a continuous
audio tone with Theremin-like timbre using sounddevice.

Signal input (via MQTT):
    ``glowup/signals/theremin:note:frequency``  — float (Hz)
    ``glowup/signals/theremin:note:amplitude``   — float (0.0-1.0)

Usage::

    python3 -m theremin.synth

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import math
import signal
import sys
import threading
from typing import Any, Optional

import numpy as np
import sounddevice as sd
import paho.mqtt.client as mqtt

from . import (
    AUDIO_BLOCK_SIZE,
    FREQ_MIN,
    HARMONICS,
    MQTT_BROKER,
    MQTT_PORT,
    PORTAMENTO_TC,
    SAMPLE_RATE,
    SIGNAL_AMPLITUDE,
    SIGNAL_FREQUENCY,
    SIGNAL_TOPIC_PREFIX,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Master volume to prevent clipping (harmonics sum > 1.0).
MASTER_VOLUME: float = 0.3

# Minimum amplitude to produce sound (noise gate).
AMPLITUDE_GATE: float = 0.01

# paho v2 detection.
_PAHO_V2: bool = hasattr(mqtt, "CallbackAPIVersion")


# ---------------------------------------------------------------------------
# Synthesizer
# ---------------------------------------------------------------------------

class ThereminSynth:
    """Real-time audio synthesizer driven by MQTT note signals.

    Uses a sounddevice output stream with a callback that generates
    a multi-harmonic waveform at the current frequency and amplitude.
    Frequency changes are smoothed with portamento (exponential glide).
    """

    def __init__(self) -> None:
        """Initialize the synthesizer."""
        self._running: bool = True
        self._lock: threading.Lock = threading.Lock()

        # Current targets (set by MQTT callback).
        self._target_freq: float = FREQ_MIN
        self._target_amp: float = 0.0

        # Smoothed values (used by audio callback).
        self._current_freq: float = FREQ_MIN
        self._current_amp: float = 0.0

        # Phase accumulator for continuous waveform.
        self._phase: float = 0.0

        # Pre-compute harmonic normalization factor.
        self._harmonic_sum: float = sum(amp for _, amp in HARMONICS)

        # MQTT client.
        self._client: Optional[mqtt.Client] = None

        # Audio stream.
        self._stream: Optional[sd.OutputStream] = None

    def _connect_mqtt(self) -> None:
        """Connect to MQTT broker and subscribe to note signals."""
        if _PAHO_V2:
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id="theremin-synth",
            )
        else:
            self._client = mqtt.Client(
                client_id="theremin-synth",
                protocol=mqtt.MQTTv311,
            )

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        self._client.loop_start()

    def _on_connect(self, *args: Any) -> None:
        """MQTT connect callback — subscribe to note signals."""
        freq_topic: str = SIGNAL_TOPIC_PREFIX + SIGNAL_FREQUENCY
        amp_topic: str = SIGNAL_TOPIC_PREFIX + SIGNAL_AMPLITUDE
        self._client.subscribe([(freq_topic, 0), (amp_topic, 0)])
        print(f"  Subscribed to note signals")

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """MQTT message callback — update target frequency/amplitude."""
        try:
            value: float = float(json.loads(msg.payload.decode("utf-8")))
        except (json.JSONDecodeError, ValueError, TypeError):
            return

        signal_name: str = msg.topic[len(SIGNAL_TOPIC_PREFIX):]

        with self._lock:
            if signal_name == SIGNAL_FREQUENCY:
                self._target_freq = value
            elif signal_name == SIGNAL_AMPLITUDE:
                self._target_amp = value

    def _audio_callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        """Sounddevice callback — generate audio samples.

        Called by the audio thread for each block of samples.  Applies
        portamento smoothing to frequency and amplitude, then generates
        a multi-harmonic waveform.

        Args:
            outdata: Output buffer to fill (frames × channels).
            frames:  Number of frames to generate.
            time_info: Timing information (unused).
            status:  Stream status flags.
        """
        with self._lock:
            target_freq: float = self._target_freq
            target_amp: float = self._target_amp

        # Portamento smoothing per sample.
        dt: float = 1.0 / SAMPLE_RATE
        alpha: float = 1.0 - math.exp(-dt / PORTAMENTO_TC)

        phase: float = self._phase
        freq: float = self._current_freq
        amp: float = self._current_amp

        for i in range(frames):
            # Smooth frequency and amplitude.
            freq += alpha * (target_freq - freq)
            amp += alpha * (target_amp - amp)

            if amp < AMPLITUDE_GATE:
                outdata[i, 0] = 0.0
            else:
                # Multi-harmonic waveform.
                sample: float = 0.0
                for harmonic_n, harmonic_amp in HARMONICS:
                    sample += harmonic_amp * math.sin(
                        2.0 * math.pi * harmonic_n * phase
                    )
                # Normalize and apply amplitude + master volume.
                outdata[i, 0] = (
                    sample / self._harmonic_sum * amp * MASTER_VOLUME
                )

            # Advance phase.
            phase += freq * dt
            if phase > 1.0:
                phase -= 1.0

        self._phase = phase
        self._current_freq = freq
        self._current_amp = amp

    def start(self) -> None:
        """Start MQTT subscription and audio output stream."""
        print(f"  Connecting to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}...",
              end="", flush=True)
        try:
            self._connect_mqtt()
            print(" connected ✓")
        except Exception as exc:
            print(f" failed: {exc}", file=sys.stderr)
            sys.exit(1)

        print(f"  Starting audio stream ({SAMPLE_RATE} Hz)...",
              end="", flush=True)
        self._stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            blocksize=AUDIO_BLOCK_SIZE,
            channels=1,
            dtype="float32",
            callback=self._audio_callback,
        )
        self._stream.start()
        print(" streaming ✓")
        print()
        print("  Synth running — Ctrl+C to stop")

        # Block until stopped.
        while self._running:
            sd.sleep(100)

    def stop(self) -> None:
        """Stop audio stream and MQTT."""
        self._running = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Launch the Theremin synthesizer."""
    print("╔══════════════════════════════════════════════╗")
    print("║   GlowUp Theremin — Audio Synthesizer       ║")
    print("║   Listening for note signals from Pi         ║")
    print("╚══════════════════════════════════════════════╝")
    print()

    synth: ThereminSynth = ThereminSynth()

    def _shutdown(signum: int, frame: object) -> None:
        """Handle Ctrl+C."""
        print("\n  Shutting down synth...")
        synth.stop()
        print("  Synth stopped.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    synth.start()


if __name__ == "__main__":
    main()
