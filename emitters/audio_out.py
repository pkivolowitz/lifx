"""Audio output emitter — plays synthesized tones via CoreAudio / PortAudio.

Receives scalar frames containing frequency and amplitude, and renders
them as a continuous audio tone through the system's default audio output.
Uses multi-harmonic waveform synthesis with portamento (pitch glide) for
smooth, Theremin-like timbre.

Frame format (dict)::

    {
        "frequency": 440.0,   # Hz (float)
        "amplitude": 0.8      # 0.0-1.0 (float)
    }

No external dependencies beyond ``sounddevice`` (PortAudio wrapper) and
``numpy`` (audio buffer math).  macOS ships with CoreAudio which PortAudio
wraps natively — no additional system packages needed.

Usage (standalone test)::

    python3 -m emitters.audio_out

Usage (via worker agent)::

    # agent.json declares the emitter, orchestrator assigns work
    python3 -m distributed.worker_agent agent.json
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import math
import threading
from typing import Any, Optional

import numpy as np
import sounddevice as sd

from emitters import Emitter, EmitterCapabilities, Param

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Audio stream parameters.
SAMPLE_RATE: int = 44100
AUDIO_BLOCK_SIZE: int = 256         # Samples per callback (~5.8 ms at 44100 Hz).
AUDIO_CHANNELS: int = 1             # Mono output.
AUDIO_DTYPE: str = "float32"        # 32-bit float samples.

# Frequency limits (Hz).
FREQ_MIN: float = 20.0              # Human hearing lower bound.
FREQ_MAX: float = 20000.0           # Human hearing upper bound.
FREQ_DEFAULT: float = 440.0         # A4 — concert pitch.

# Portamento (pitch glide) time constant in seconds.
# Smaller = faster response, larger = smoother glide.
PORTAMENTO_TC_DEFAULT: float = 0.05  # 50 ms — smooth but responsive.

# Master volume to prevent clipping (harmonics sum > 1.0).
MASTER_VOLUME_DEFAULT: float = 0.3

# Minimum amplitude to produce sound (noise gate).
AMPLITUDE_GATE: float = 0.01

# Vibrato defaults — the characteristic "sound of the ether" that makes
# a theremin sound like a theremin.  A slow LFO modulates both pitch and
# amplitude, producing the eerie warble.
VIBRATO_RATE_DEFAULT: float = 5.5    # Hz — typical theremin vibrato rate.
VIBRATO_DEPTH_DEFAULT: float = 0.015 # Fraction of frequency (±1.5% = ~quarter semitone).
VIBRATO_AMP_DEPTH_DEFAULT: float = 0.08  # Amplitude modulation depth (±8%).

# Default harmonic structure: (harmonic_number, relative_amplitude).
# Produces a warm, Theremin-like timbre.
DEFAULT_HARMONICS: list[tuple[int, float]] = [
    (1, 1.0),      # Fundamental
    (2, 0.5),      # 2nd harmonic — octave
    (3, 0.25),     # 3rd harmonic — fifth above octave
    (4, 0.1),      # 4th harmonic — subtle warmth
]

# Frame dict keys.
KEY_FREQUENCY: str = "frequency"
KEY_AMPLITUDE: str = "amplitude"

# Frame type accepted by this emitter.
FRAME_TYPE_SCALAR: str = "scalar"

# Maximum meaningful update rate (Hz).  The audio callback runs at
# SAMPLE_RATE/AUDIO_BLOCK_SIZE ≈ 172 Hz, but frame delivery is typically
# 15-30 Hz from the operator.
MAX_RATE_HZ: float = 60.0

# Module logger.
logger: logging.Logger = logging.getLogger("glowup.emitters.audio_out")


# ---------------------------------------------------------------------------
# AudioOutEmitter
# ---------------------------------------------------------------------------

class AudioOutEmitter(Emitter):
    """Play synthesized audio tones from frequency/amplitude frames.

    Generates a continuous multi-harmonic waveform through the system's
    default audio output.  Frequency and amplitude are smoothed with
    exponential portamento for natural-sounding pitch glide.

    The emitter runs a ``sounddevice.OutputStream`` in a callback model:
    the audio thread calls :meth:`_audio_callback` at ~172 Hz to fill
    output buffers.  Frame delivery from the pipeline updates the target
    frequency and amplitude, which the audio callback interpolates
    smoothly per-sample.

    Parameters:
        master_volume: Output volume multiplier (0.0-1.0).
        portamento:    Pitch glide time constant in seconds.
    """

    emitter_type: str = "audio_out"
    description: str = "Audio tone synthesizer via CoreAudio/PortAudio"

    # --- Configurable parameters ---

    master_volume = Param(
        MASTER_VOLUME_DEFAULT, min=0.0, max=1.0,
        description="Master output volume (0.0-1.0)",
    )

    portamento = Param(
        PORTAMENTO_TC_DEFAULT, min=0.001, max=2.0,
        description="Pitch glide time constant in seconds",
    )

    vibrato_rate = Param(
        VIBRATO_RATE_DEFAULT, min=0.0, max=20.0,
        description="Vibrato LFO rate in Hz (0 = off)",
    )

    vibrato_depth = Param(
        VIBRATO_DEPTH_DEFAULT, min=0.0, max=0.1,
        description="Vibrato pitch depth as fraction of frequency",
    )

    vibrato_amp_depth = Param(
        VIBRATO_AMP_DEPTH_DEFAULT, min=0.0, max=0.5,
        description="Vibrato amplitude modulation depth",
    )

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        """Initialize the audio output emitter.

        Args:
            name:   Instance name (e.g., ``"bed:speaker"``).
            config: Instance-specific configuration dict.
        """
        super().__init__(name, config)

        # Thread-safe target values (set by on_emit, read by audio callback).
        self._lock: threading.Lock = threading.Lock()
        self._target_freq: float = FREQ_DEFAULT
        self._target_amp: float = 0.0

        # Smoothed values (audio callback internal state).
        self._current_freq: float = FREQ_DEFAULT
        self._current_amp: float = 0.0

        # Phase accumulator for continuous waveform generation.
        self._phase: float = 0.0

        # Vibrato LFO phase (separate from waveform phase).
        self._vibrato_phase: float = 0.0

        # Mute flag — silences output without tearing down the stream.
        self._muted: bool = False

        # Harmonic structure — could be made configurable later.
        self._harmonics: list[tuple[int, float]] = list(DEFAULT_HARMONICS)
        self._harmonic_sum: float = sum(amp for _, amp in self._harmonics)

        # Audio stream handle.
        self._stream: Optional[sd.OutputStream] = None

    # --- Lifecycle ---------------------------------------------------------

    def on_configure(self, config: dict[str, Any]) -> None:
        """Accept pipeline-level configuration.

        Currently a no-op — all config comes from Param declarations.

        Args:
            config: Full pipeline configuration dict.
        """
        logger.info(
            "AudioOutEmitter '%s' configured: volume=%.2f, portamento=%.3fs",
            self.name, self.master_volume, self.portamento,
        )

    def on_open(self) -> None:
        """Open the audio output stream.

        Creates a ``sounddevice.OutputStream`` with a callback that
        generates the waveform.  The stream starts immediately — silence
        is produced until the first frame arrives with amplitude > 0.
        """
        logger.info(
            "AudioOutEmitter '%s' opening audio stream "
            "(%d Hz, block=%d, channels=%d)",
            self.name, SAMPLE_RATE, AUDIO_BLOCK_SIZE, AUDIO_CHANNELS,
        )

        self._stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            blocksize=AUDIO_BLOCK_SIZE,
            channels=AUDIO_CHANNELS,
            dtype=AUDIO_DTYPE,
            callback=self._audio_callback,
        )
        self._stream.start()
        self._is_open = True

        logger.info("AudioOutEmitter '%s' audio stream started", self.name)

    def on_emit(self, frame: Any, metadata: dict[str, Any]) -> bool:
        """Update the target frequency and amplitude from a frame.

        Accepts a dict with ``frequency`` (Hz) and ``amplitude`` (0.0-1.0)
        keys.  Missing keys leave the previous value unchanged — this
        allows partial updates.

        Args:
            frame:    Dict with ``frequency`` and/or ``amplitude`` keys.
            metadata: Per-frame context (unused by this emitter).

        Returns:
            ``True`` if the frame was accepted.
        """
        if not isinstance(frame, dict):
            logger.warning(
                "AudioOutEmitter '%s' received non-dict frame: %s",
                self.name, type(frame).__name__,
            )
            return False

        freq: Optional[float] = frame.get(KEY_FREQUENCY)
        amp: Optional[float] = frame.get(KEY_AMPLITUDE)

        with self._lock:
            if freq is not None:
                self._target_freq = max(FREQ_MIN, min(FREQ_MAX, float(freq)))
            if amp is not None:
                self._target_amp = max(0.0, min(1.0, float(amp)))

        return True

    def toggle_mute(self) -> bool:
        """Toggle mute state.  Audio stream stays open, output is zeroed.

        Returns:
            ``True`` if now muted, ``False`` if unmuted.
        """
        self._muted = not self._muted
        return self._muted

    @property
    def muted(self) -> bool:
        """Whether output is currently muted."""
        return self._muted

    def on_flush(self) -> None:
        """No-op — audio is real-time, nothing to flush."""

    def on_close(self) -> None:
        """Stop and close the audio stream.

        Ramps amplitude to zero before stopping to avoid clicks.
        """
        logger.info("AudioOutEmitter '%s' closing audio stream", self.name)

        # Silence the output before stopping.
        with self._lock:
            self._target_amp = 0.0

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                logger.warning(
                    "AudioOutEmitter '%s' error closing stream: %s",
                    self.name, exc,
                )
            self._stream = None

        self._is_open = False
        logger.info("AudioOutEmitter '%s' audio stream closed", self.name)

    # --- Introspection -----------------------------------------------------

    def capabilities(self) -> EmitterCapabilities:
        """Declare audio output capabilities.

        Returns:
            An :class:`EmitterCapabilities` accepting ``scalar`` frames.
        """
        return EmitterCapabilities(
            accepted_frame_types=[FRAME_TYPE_SCALAR],
            max_rate_hz=MAX_RATE_HZ,
            extra={
                "sample_rate": SAMPLE_RATE,
                "channels": AUDIO_CHANNELS,
            },
        )

    # --- Audio callback (runs on PortAudio thread) -------------------------

    def _audio_callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        """Generate audio samples for the output buffer.

        Called by the PortAudio thread at approximately
        SAMPLE_RATE / AUDIO_BLOCK_SIZE Hz.  Applies per-sample exponential
        smoothing to frequency and amplitude for glitch-free portamento.

        Args:
            outdata:   Output buffer to fill (frames x channels).
            frames:    Number of sample frames to generate.
            time_info: Timing information from PortAudio (unused).
            status:    Stream status flags.
        """
        if status:
            logger.debug("AudioOutEmitter '%s' stream status: %s",
                         self.name, status)

        # Muted — fill with silence immediately.
        if self._muted:
            outdata[:] = 0.0
            return

        # Snapshot targets under lock.
        with self._lock:
            target_freq: float = self._target_freq
            target_amp: float = self._target_amp

        # Per-sample smoothing coefficient.
        dt: float = 1.0 / SAMPLE_RATE
        alpha: float = 1.0 - math.exp(-dt / self.portamento)

        # Local copies for the inner loop (avoid attribute lookups).
        phase: float = self._phase
        freq: float = self._current_freq
        amp: float = self._current_amp
        harmonics: list[tuple[int, float]] = self._harmonics
        harmonic_sum: float = self._harmonic_sum
        volume: float = self.master_volume

        # Vibrato LFO state.
        vib_phase: float = self._vibrato_phase
        vib_rate: float = self.vibrato_rate
        vib_depth: float = self.vibrato_depth
        vib_amp_depth: float = self.vibrato_amp_depth
        two_pi: float = 2.0 * math.pi

        for i in range(frames):
            # Exponential smoothing toward targets.
            freq += alpha * (target_freq - freq)
            amp += alpha * (target_amp - amp)

            if amp < AMPLITUDE_GATE:
                outdata[i, 0] = 0.0
            else:
                # Vibrato LFO — sinusoidal modulation of pitch and amplitude.
                # This is the "sound of the ether" that makes a theremin
                # sound ethereal rather than like a plain sine oscillator.
                vib_lfo: float = math.sin(two_pi * vib_phase)
                mod_freq: float = freq * (1.0 + vib_depth * vib_lfo)
                mod_amp: float = amp * (1.0 + vib_amp_depth * vib_lfo)

                # Multi-harmonic waveform synthesis.
                sample: float = 0.0
                for harmonic_n, harmonic_amp in harmonics:
                    sample += harmonic_amp * math.sin(
                        two_pi * harmonic_n * phase
                    )
                # Normalize, apply modulated amplitude and master volume.
                outdata[i, 0] = sample / harmonic_sum * mod_amp * volume

                # Advance phase using modulated frequency.
                phase += mod_freq * dt

            if phase > 1.0:
                phase -= 1.0

            # Advance vibrato LFO phase.
            vib_phase += vib_rate * dt
            if vib_phase > 1.0:
                vib_phase -= 1.0

        # Persist state for next callback.
        self._phase = phase
        self._vibrato_phase = vib_phase
        self._current_freq = freq
        self._current_amp = amp


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

def _test() -> None:
    """Quick standalone test — sweep a tone to verify audio output works."""
    import signal as sig
    import time as tm

    print("╔══════════════════════════════════════════════╗")
    print("║   AudioOutEmitter — standalone test          ║")
    print("║   Sweeps A3 → A5 over 4 seconds             ║")
    print("╚══════════════════════════════════════════════╝")
    print()

    emitter = AudioOutEmitter("test:speaker", {})
    emitter.on_configure({})
    emitter.on_open()

    running: bool = True

    def _shutdown(signum: int, frame: object) -> None:
        nonlocal running
        running = False

    sig.signal(sig.SIGINT, _shutdown)
    sig.signal(sig.SIGTERM, _shutdown)

    # Sweep from A3 (220 Hz) to A5 (880 Hz) over 4 seconds.
    sweep_duration: float = 4.0
    freq_lo: float = 220.0
    freq_hi: float = 880.0
    start: float = tm.monotonic()

    print("  Playing tone sweep...")
    while running:
        elapsed: float = tm.monotonic() - start
        cycle: float = (elapsed % sweep_duration) / sweep_duration
        # Triangle wave sweep: up then down.
        t: float = cycle * 2.0
        if t > 1.0:
            t = 2.0 - t
        freq: float = freq_lo * (2.0 ** (t * math.log2(freq_hi / freq_lo)))
        amp: float = 0.6

        emitter.on_emit({"frequency": freq, "amplitude": amp}, {})
        tm.sleep(0.02)  # 50 Hz update rate

    print("\n  Shutting down...")
    emitter.on_close()
    print("  Done.")


if __name__ == "__main__":
    _test()
