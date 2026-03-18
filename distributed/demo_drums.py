"""Drum synth demo — metronome master clock drives a two-light drum machine.

Proves the master clock concept: a fixed-BPM metronome with sub-beat
resolution triggers percussive hits on two LIFX lights and synthesized
drum audio simultaneously.

Light layout::

    10.0.0.23 (STRING 2, 36 zones / 12 bulbs):
        zones  0–11 = kick drum
        zones 12–23 = snare
        zones 24–35 = tom
    10.0.0.34 (Neon Indoor, 24 zones):
        zones  0–7  = hi-hat
        zones  8–15 = crash
        zones 16–23 = rim click

The metronome is a system clock, not a music clock.  It provides a
shared phase reference at configurable BPM with 16th-note resolution.
All drum hits, light flashes, and audio events lock to this grid.

Usage::

    ~/venv/bin/python3 -m distributed.demo_drums
    ~/venv/bin/python3 -m distributed.demo_drums --bpm 100
    ~/venv/bin/python3 -m distributed.demo_drums --bpm 140 --pattern funky
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import argparse
import math
import signal as sig
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import sounddevice as sd

from transport import LifxDevice

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Audio.
SAMPLE_RATE: int = 44100
AUDIO_BLOCK_SIZE: int = 256         # ~5.8 ms per callback at 44100 Hz.
AUDIO_CHANNELS: int = 1
AUDIO_DTYPE: str = "float32"
MASTER_VOLUME: float = 0.4         # Overall mix volume.
VOICE_EXPIRY_THRESHOLD: float = 0.001  # Amplitude below which a voice dies.

# Metronome.
DEFAULT_BPM: int = 120
STEPS_PER_BEAT: int = 4             # 16th-note resolution.
BEATS_PER_BAR: int = 4
STEPS_PER_BAR: int = STEPS_PER_BEAT * BEATS_PER_BAR  # 16 steps.

# Light addresses.
LIGHT_KICK_IP: str = "10.0.0.23"
LIGHT_NEON_IP: str = "10.0.0.34"

# STRING 2 zone layout (36 zones / 12 bulbs, 3 zones per bulb).
STRING_ZONE_COUNT: int = 36
ZONE_KICK_START: int = 0
ZONE_KICK_END: int = 12            # Exclusive — bulbs 1–4.
ZONE_SNARE_START: int = 12
ZONE_SNARE_END: int = 24           # Bulbs 5–8.
ZONE_TOM_START: int = 24
ZONE_TOM_END: int = 36             # Bulbs 9–12.

# Neon zone layout (24 zones total).
NEON_ZONE_COUNT: int = 24
ZONE_HIHAT_START: int = 0
ZONE_HIHAT_END: int = 8            # Exclusive.
ZONE_CRASH_START: int = 8
ZONE_CRASH_END: int = 16
ZONE_RIM_START: int = 16
ZONE_RIM_END: int = 24

# Light refresh rate (Hz) — LIFX LAN max is ~30.
LIGHT_FPS: int = 30

# Envelope decay per frame (multiplicative).  At 30 fps, 0.85 gives
# roughly a 150 ms visual decay — punchy but not abrupt.
ENVELOPE_DECAY: float = 0.85

# HSBK values (0–65535 scale).
MAX_HSBK: int = 65535
KELVIN_DEFAULT: int = 3500

# Neon brightness cap — the Neon is insanely bright at full power.
# Scale to ~15% to match the string light intensity.
NEON_BRIGHTNESS_CAP: float = 0.15

# Drum colors in HSBK.  Hue is 0–65535 mapped to 0–360°.
#   Red-orange ≈ 10°  → hue 1820
#   White      → sat 0
#   Blue       ≈ 210° → hue 38228
#   Green      ≈ 120° → hue 21845
#   Yellow     ≈ 50°  → hue 9102
#   Magenta    ≈ 300° → hue 54612
COLOR_KICK: tuple[int, int, int, int] = (1820, MAX_HSBK, MAX_HSBK, KELVIN_DEFAULT)
COLOR_SNARE: tuple[int, int, int, int] = (0, 0, MAX_HSBK, KELVIN_DEFAULT)
COLOR_TOM: tuple[int, int, int, int] = (38228, MAX_HSBK, MAX_HSBK, KELVIN_DEFAULT)
COLOR_HIHAT: tuple[int, int, int, int] = (21845, MAX_HSBK, MAX_HSBK, KELVIN_DEFAULT)
COLOR_CRASH: tuple[int, int, int, int] = (9102, MAX_HSBK, MAX_HSBK, KELVIN_DEFAULT)
COLOR_RIM: tuple[int, int, int, int] = (54612, MAX_HSBK, MAX_HSBK, KELVIN_DEFAULT)
COLOR_BLACK: tuple[int, int, int, int] = (0, 0, 0, KELVIN_DEFAULT)


# ---------------------------------------------------------------------------
# Drum voice parameters
# ---------------------------------------------------------------------------

@dataclass
class DrumParams:
    """Synthesis parameters for one type of drum.

    Attributes:
        freq_start:  Starting frequency of the pitch sweep (Hz).
        freq_end:    Ending frequency after the sweep (Hz).
        pitch_decay: Time constant for exponential pitch sweep (seconds).
        amp_decay:   Time constant for exponential amplitude decay (seconds).
        noise_mix:   Proportion of white noise in the mix (0.0–1.0).
    """

    freq_start: float
    freq_end: float
    pitch_decay: float
    amp_decay: float
    noise_mix: float = 0.0


# Classic analog drum synthesis recipes.
DRUM_PARAMS: dict[str, DrumParams] = {
    "kick":  DrumParams(freq_start=150.0, freq_end=40.0,
                        pitch_decay=0.045, amp_decay=0.30),
    "snare": DrumParams(freq_start=220.0, freq_end=110.0,
                        pitch_decay=0.030, amp_decay=0.15, noise_mix=0.55),
    "tom":   DrumParams(freq_start=200.0, freq_end=100.0,
                        pitch_decay=0.040, amp_decay=0.22),
    "hihat": DrumParams(freq_start=800.0, freq_end=800.0,
                        pitch_decay=1.000, amp_decay=0.05, noise_mix=0.90),
    "crash": DrumParams(freq_start=600.0, freq_end=400.0,
                        pitch_decay=0.100, amp_decay=0.40, noise_mix=0.70),
    "rim":   DrumParams(freq_start=1200.0, freq_end=900.0,
                        pitch_decay=0.010, amp_decay=0.06, noise_mix=0.30),
}


# ---------------------------------------------------------------------------
# Drum patterns — multi-bar sequences
# ---------------------------------------------------------------------------

# A bar is a dict mapping step index (0–15) to a list of drum names.
# A pattern is a list of bars that cycles.  Single-bar patterns just
# have one element.
Bar = dict[int, list[str]]

# --- Bar templates (composed into multi-bar patterns) ---

# Basic shuffle: kick 1+3, snare 2+4, 8th-note hi-hat.
_SHUFFLE: Bar = {
    0: ["kick", "hihat"], 2: ["hihat"],
    4: ["snare", "hihat"], 6: ["hihat"],
    8: ["kick", "hihat"], 10: ["hihat"],
    12: ["snare", "hihat"], 14: ["hihat"],
}

# Shuffle with crash on beat 1 (top of phrase / chord change).
_SHUFFLE_CRASH: Bar = {
    **_SHUFFLE, 0: ["kick", "hihat", "crash"],
}

# IV-chord variation — tom accents.
_SHUFFLE_IV: Bar = {
    0: ["kick", "hihat"], 2: ["hihat"],
    4: ["snare", "hihat"], 6: ["hihat", "tom"],
    8: ["kick", "hihat"], 10: ["hihat"],
    12: ["snare", "hihat"], 14: ["hihat"],
}

# V-chord — tension, rim clicks.
_SHUFFLE_V: Bar = {
    0: ["kick", "hihat", "crash"], 2: ["hihat"],
    4: ["snare", "hihat"], 6: ["hihat"],
    8: ["kick", "hihat"], 10: ["hihat", "rim"],
    12: ["snare", "hihat"], 14: ["hihat", "rim"],
}

# Turnaround — tom cascade leading back to the top.
_TURNAROUND: Bar = {
    0: ["kick", "hihat"], 2: ["hihat"],
    4: ["snare", "hihat"], 6: ["tom"],
    8: ["kick", "hihat"], 10: ["tom"],
    12: ["snare", "tom"], 14: ["tom", "rim"],
}

# Solo groove — 16th-note hi-hat, more energy.
_SOLO: Bar = {
    0: ["kick", "hihat"], 1: ["hihat"],
    2: ["hihat"], 3: ["hihat"],
    4: ["snare", "hihat"], 5: ["hihat"],
    6: ["hihat"], 7: ["hihat"],
    8: ["kick", "hihat"], 9: ["hihat"],
    10: ["hihat"], 11: ["hihat"],
    12: ["snare", "hihat"], 13: ["hihat"],
    14: ["hihat"], 15: ["hihat"],
}

# Solo with crash on 1.
_SOLO_CRASH: Bar = {**_SOLO, 0: ["kick", "hihat", "crash"]}

# Solo with tom fills — the drummer showing off.
_SOLO_FILL: Bar = {
    0: ["kick", "hihat", "crash"], 1: ["hihat"],
    2: ["hihat"], 3: ["tom"],
    4: ["snare", "hihat"], 5: ["hihat"],
    6: ["tom"], 7: ["hihat"],
    8: ["kick", "hihat"], 9: ["hihat"],
    10: ["tom"], 11: ["hihat"],
    12: ["snare", "hihat"], 13: ["tom"],
    14: ["tom"], 15: ["rim"],
}

# Solo climax — big turnaround, crash-heavy.
_SOLO_TURNAROUND: Bar = {
    0: ["kick", "crash"], 1: ["hihat"],
    2: ["tom"], 3: ["tom"],
    4: ["snare", "crash"], 5: ["hihat"],
    6: ["tom"], 7: ["rim"],
    8: ["kick", "crash"], 9: ["tom"],
    10: ["tom"], 11: ["snare"],
    12: ["kick", "crash"], 13: ["tom"],
    14: ["snare", "tom"], 15: ["rim", "crash"],
}

# --- Assembled patterns ---

PATTERNS: dict[str, list[Bar]] = {
    # Single-bar loops.
    "rock": [{
        0: ["kick", "hihat", "crash"], 2: ["hihat"],
        4: ["snare", "hihat"], 6: ["hihat"],
        8: ["kick", "hihat"], 10: ["hihat"],
        12: ["snare", "hihat"], 14: ["hihat", "rim"],
    }],
    "funky": [{
        0: ["kick", "hihat", "crash"], 2: ["hihat", "rim"],
        3: ["kick"],
        4: ["snare", "hihat"], 6: ["hihat", "tom"],
        8: ["kick", "hihat"], 10: ["hihat", "rim"],
        11: ["kick"],
        12: ["snare", "hihat"], 14: ["hihat", "tom"],
        15: ["tom", "rim"],
    }],
    "halftime": [{
        0: ["kick", "hihat", "crash"], 4: ["hihat"],
        6: ["tom"],
        8: ["snare", "hihat"], 10: ["tom", "rim"],
        12: ["hihat"], 14: ["hihat", "rim"],
    }],

    # 12-bar blues groove (12 bars) + 12-bar solo = 24-bar cycle.
    #   Bars  1–4:  I  chord (shuffle)
    #   Bars  5–6:  IV chord (tom accents)
    #   Bars  7–8:  I  chord
    #   Bar   9:    V  chord (tension)
    #   Bar  10:    IV chord
    #   Bars 11–12: I  chord (turnaround)
    #   Bars 13–24: Solo — 16th hi-hat, fills, climax, repeat.
    "blues": [
        # --- Groove (12 bars) ---
        _SHUFFLE_CRASH,     # 1:  I  — top of form
        _SHUFFLE,           # 2:  I
        _SHUFFLE,           # 3:  I
        _SHUFFLE,           # 4:  I
        _SHUFFLE_IV,        # 5:  IV
        _SHUFFLE_IV,        # 6:  IV
        _SHUFFLE,           # 7:  I
        _SHUFFLE,           # 8:  I
        _SHUFFLE_V,         # 9:  V  — tension
        _SHUFFLE_IV,        # 10: IV
        _SHUFFLE,           # 11: I
        _TURNAROUND,        # 12: I  — turnaround
        # --- Solo (12 bars) ---
        _SOLO_CRASH,        # 13: I  — solo top
        _SOLO,              # 14: I
        _SOLO,              # 15: I
        _SOLO_FILL,         # 16: I  — fill
        _SOLO_CRASH,        # 17: IV
        _SOLO,              # 18: IV
        _SOLO,              # 19: I
        _SOLO_FILL,         # 20: I  — fill
        _SOLO_CRASH,        # 21: V  — tension
        _SOLO,              # 22: IV
        _SOLO,              # 23: I
        _SOLO_TURNAROUND,   # 24: I  — big turnaround, back to top
    ],
}

DEFAULT_PATTERN: str = "rock"


# ---------------------------------------------------------------------------
# DrumVoice — single percussive voice with pitch sweep
# ---------------------------------------------------------------------------

class DrumVoice:
    """A single triggered drum voice that decays to silence.

    Uses exponential pitch sweep (analog-style) and exponential amplitude
    decay.  Optional white noise component for snare/hi-hat character.
    Renders audio via numpy vectorized operations for efficiency.

    Attributes:
        expired: True once amplitude drops below threshold.
    """

    def __init__(self, params: DrumParams) -> None:
        """Initialize a new drum voice from synthesis parameters.

        Args:
            params: The :class:`DrumParams` controlling this voice's sound.
        """
        self._freq_start: float = params.freq_start
        self._freq_end: float = params.freq_end
        self._pitch_decay: float = params.pitch_decay
        self._amp_decay: float = params.amp_decay
        self._noise_mix: float = params.noise_mix
        self._age: float = 0.0
        self._phase: float = 0.0
        self.expired: bool = False

    def render(self, frames: int) -> np.ndarray:
        """Render a block of audio samples.

        Args:
            frames: Number of samples to generate.

        Returns:
            Numpy array of float32 samples.
        """
        dt: float = 1.0 / SAMPLE_RATE
        t: np.ndarray = self._age + np.arange(frames, dtype=np.float64) * dt

        # Exponential pitch sweep.
        freq: np.ndarray = (
            self._freq_end
            + (self._freq_start - self._freq_end) * np.exp(-t / self._pitch_decay)
        )

        # Exponential amplitude decay.
        amp: np.ndarray = np.exp(-t / self._amp_decay)

        # Phase from cumulative frequency integration.
        phase_inc: np.ndarray = freq * dt
        phase: np.ndarray = self._phase + np.cumsum(phase_inc)

        # Tone component.
        tone: np.ndarray = np.sin(2.0 * np.pi * phase)

        # Noise component (if any).
        if self._noise_mix > 0.0:
            noise: np.ndarray = np.random.uniform(-1.0, 1.0, frames)
            samples: np.ndarray = (
                amp * ((1.0 - self._noise_mix) * tone + self._noise_mix * noise)
            )
        else:
            samples = amp * tone

        # Update state for next block.
        self._age = t[-1] + dt
        self._phase = phase[-1]

        # Check expiry.
        if amp[-1] < VOICE_EXPIRY_THRESHOLD:
            self.expired = True

        return samples.astype(np.float32)


# ---------------------------------------------------------------------------
# DrumSynth — multi-voice percussive synthesizer
# ---------------------------------------------------------------------------

class DrumSynth:
    """Multi-voice drum synthesizer with real-time audio output.

    Manages a pool of :class:`DrumVoice` instances, mixes them in a
    sounddevice callback, and handles triggering from the metronome.

    The audio stream runs continuously; triggering a drum adds a new
    voice to the active pool.  Expired voices are pruned each callback.
    """

    def __init__(self) -> None:
        """Initialize the synthesizer (stream not yet started)."""
        self._voices: list[DrumVoice] = []
        self._lock: threading.Lock = threading.Lock()
        self._stream: Optional[sd.OutputStream] = None

    def open(self) -> None:
        """Start the audio output stream."""
        self._stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            blocksize=AUDIO_BLOCK_SIZE,
            channels=AUDIO_CHANNELS,
            dtype=AUDIO_DTYPE,
            callback=self._audio_callback,
        )
        self._stream.start()

    def close(self) -> None:
        """Stop and close the audio output stream."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def trigger(self, drum_type: str) -> None:
        """Trigger a new drum hit.

        Args:
            drum_type: One of ``"kick"``, ``"snare"``, ``"tom"``, ``"hihat"``.

        Raises:
            KeyError: If *drum_type* is not recognized.
        """
        params: DrumParams = DRUM_PARAMS[drum_type]
        voice: DrumVoice = DrumVoice(params)
        with self._lock:
            self._voices.append(voice)

    def _audio_callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        """Mix all active voices into the output buffer.

        Called by the PortAudio thread at ~172 Hz (44100/256).

        Args:
            outdata:   Output buffer to fill (frames × channels).
            frames:    Number of sample frames requested.
            time_info: PortAudio timing info (unused).
            status:    Stream status flags.
        """
        buf: np.ndarray = np.zeros(frames, dtype=np.float32)

        with self._lock:
            for voice in self._voices:
                buf += voice.render(frames)
            # Prune expired voices.
            self._voices = [v for v in self._voices if not v.expired]

        # Apply master volume and soft-clip to prevent distortion.
        buf *= MASTER_VOLUME
        np.clip(buf, -1.0, 1.0, out=buf)
        outdata[:, 0] = buf


# ---------------------------------------------------------------------------
# LightController — drives kick (single) and neon (multizone) lights
# ---------------------------------------------------------------------------

class LightController:
    """Manages brightness envelopes and LIFX transmission for drum lights.

    Both lights are multizone.  STRING 2 (36 zones) carries kick, snare,
    and tom.  Neon (24 zones) carries hi-hat, crash, and rim click.
    Each drum voice has an envelope that flashes to 1.0 on trigger and
    decays multiplicatively each frame.
    """

    # Maps drum name → envelope attribute name for clean dispatch.
    _DRUM_NAMES: list[str] = ["kick", "snare", "tom", "hihat", "crash", "rim"]

    def __init__(self) -> None:
        """Initialize light controller (devices not yet connected)."""
        self._string_device: Optional[LifxDevice] = None
        self._neon_device: Optional[LifxDevice] = None
        # Brightness envelopes (0.0–1.0) for each drum voice.
        self._envelopes: dict[str, float] = {d: 0.0 for d in self._DRUM_NAMES}

    def connect(self) -> None:
        """Connect to LIFX devices and prepare for rendering."""
        print(f"  Connecting to STRING 2 at {LIGHT_KICK_IP}...")
        self._string_device = LifxDevice(LIGHT_KICK_IP)
        self._string_device.query_all()
        print(f"    -> {self._string_device.label or '?'} "
              f"[{self._string_device.zone_count} zones]")

        print(f"  Connecting to Neon at {LIGHT_NEON_IP}...")
        self._neon_device = LifxDevice(LIGHT_NEON_IP)
        self._neon_device.query_all()
        print(f"    -> {self._neon_device.label or '?'} "
              f"[{self._neon_device.zone_count} zones]")

        # Power on and clear both to black.
        self._string_device.set_power(on=True, duration_ms=0)
        self._neon_device.set_power(on=True, duration_ms=0)
        # Brief pause for power-on to take effect.
        time.sleep(0.3)
        black_string: list[tuple[int, int, int, int]] = [COLOR_BLACK] * STRING_ZONE_COUNT
        black_neon: list[tuple[int, int, int, int]] = [COLOR_BLACK] * NEON_ZONE_COUNT
        self._string_device.set_zones(black_string, duration_ms=0)
        self._neon_device.set_zones(black_neon, duration_ms=0)

    def close(self) -> None:
        """Fade to black and close device connections."""
        if self._string_device is not None:
            black: list[tuple[int, int, int, int]] = [COLOR_BLACK] * STRING_ZONE_COUNT
            self._string_device.set_zones(black, duration_ms=500)
            self._string_device.close()
        if self._neon_device is not None:
            black = [COLOR_BLACK] * NEON_ZONE_COUNT
            self._neon_device.set_zones(black, duration_ms=500)
            self._neon_device.close()

    def trigger(self, drum_type: str) -> None:
        """Flash the appropriate light zone for a drum hit.

        Args:
            drum_type: One of ``"kick"``, ``"snare"``, ``"tom"``,
                       ``"hihat"``, ``"crash"``, ``"rim"``.
        """
        if drum_type in self._envelopes:
            self._envelopes[drum_type] = 1.0

    def _scaled_color(
        self, color: tuple[int, int, int, int], envelope: float,
    ) -> tuple[int, int, int, int]:
        """Scale a color's brightness by an envelope value.

        Args:
            color:    Base HSBK color at full brightness.
            envelope: Brightness multiplier (0.0–1.0).

        Returns:
            HSBK tuple with brightness scaled, or COLOR_BLACK if below threshold.
        """
        if envelope < VOICE_EXPIRY_THRESHOLD:
            return COLOR_BLACK
        h, s, b, k = color
        return (h, s, int(b * envelope), k)

    def update(self) -> None:
        """Decay envelopes and send current state to both lights.

        Called once per frame at LIGHT_FPS.  Builds zone arrays for
        both multizone devices and transmits.
        """
        env: dict[str, float] = self._envelopes

        # --- STRING 2 (36 zones): kick / snare / tom ---
        if self._string_device is not None:
            zones: list[tuple[int, int, int, int]] = []
            for z in range(STRING_ZONE_COUNT):
                if ZONE_KICK_START <= z < ZONE_KICK_END:
                    zones.append(self._scaled_color(COLOR_KICK, env["kick"]))
                elif ZONE_SNARE_START <= z < ZONE_SNARE_END:
                    zones.append(self._scaled_color(COLOR_SNARE, env["snare"]))
                elif ZONE_TOM_START <= z < ZONE_TOM_END:
                    zones.append(self._scaled_color(COLOR_TOM, env["tom"]))
                else:
                    zones.append(COLOR_BLACK)
            self._string_device.set_zones(zones, duration_ms=0)

        # --- Neon (24 zones): hihat / crash / rim ---
        # Neon brightness is capped — it's blindingly bright at full power.
        if self._neon_device is not None:
            zones = []
            for z in range(NEON_ZONE_COUNT):
                if ZONE_HIHAT_START <= z < ZONE_HIHAT_END:
                    zones.append(self._scaled_color(
                        COLOR_HIHAT, env["hihat"] * NEON_BRIGHTNESS_CAP))
                elif ZONE_CRASH_START <= z < ZONE_CRASH_END:
                    zones.append(self._scaled_color(
                        COLOR_CRASH, env["crash"] * NEON_BRIGHTNESS_CAP))
                elif ZONE_RIM_START <= z < ZONE_RIM_END:
                    zones.append(self._scaled_color(
                        COLOR_RIM, env["rim"] * NEON_BRIGHTNESS_CAP))
                else:
                    zones.append(COLOR_BLACK)
            self._neon_device.set_zones(zones, duration_ms=0)

        # Decay all envelopes.
        for drum in self._DRUM_NAMES:
            self._envelopes[drum] *= ENVELOPE_DECAY


# ---------------------------------------------------------------------------
# Main loop — metronome drives pattern → synth + lights
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser.

    Returns:
        Configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        description="Drum synth demo — metronome master clock + LIFX lights",
    )
    parser.add_argument(
        "--bpm", type=int, default=DEFAULT_BPM,
        help=f"Beats per minute (default: {DEFAULT_BPM})",
    )
    parser.add_argument(
        "--pattern", choices=list(PATTERNS.keys()), default=DEFAULT_PATTERN,
        help=f"Drum pattern (default: {DEFAULT_PATTERN})",
    )
    parser.add_argument(
        "--no-lights", action="store_true",
        help="Audio only — skip LIFX light control",
    )
    parser.add_argument(
        "--no-audio", action="store_true",
        help="Lights only — skip audio synthesis",
    )
    return parser


def _step_label(step: int, hits: list[str]) -> str:
    """Format a step for terminal display.

    Args:
        step: Step index (0–15).
        hits: List of drum names triggered on this step.

    Returns:
        Formatted string like ``"[01] kick hihat"``.
    """
    names: str = " ".join(hits) if hits else "·"
    return f"[{step:02d}] {names}"


def main() -> None:
    """Entry point — run the drum synth demo."""
    args: argparse.Namespace = _build_parser().parse_args()
    bars: list[Bar] = PATTERNS[args.pattern]
    total_bars: int = len(bars)
    bpm: int = args.bpm

    # Seconds per 16th-note step.
    step_duration: float = 60.0 / bpm / STEPS_PER_BEAT

    print("╔══════════════════════════════════════════════════╗")
    print("║   Drum Synth Demo — Master Clock Test            ║")
    print("╠══════════════════════════════════════════════════╣")
    print(f"║   BPM: {bpm:>4d}    Pattern: {args.pattern:<12s}            ║")
    print(f"║   Bars: {total_bars:<3d}     Step: {step_duration*1000:.1f} ms                  ║")
    print("║   Ctrl-C to stop                                 ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    # --- Initialize subsystems ---
    synth: Optional[DrumSynth] = None
    lights: Optional[LightController] = None

    if not args.no_audio:
        print("  Starting audio synth...")
        synth = DrumSynth()
        synth.open()

    if not args.no_lights:
        print("  Connecting to lights...")
        lights = LightController()
        lights.connect()

    print()
    print("  Playing.  Ctrl-C to stop.")
    print()

    # --- Signal handling ---
    running: bool = True

    def _shutdown(signum: int, frame: object) -> None:
        nonlocal running
        running = False

    sig.signal(sig.SIGINT, _shutdown)
    sig.signal(sig.SIGTERM, _shutdown)

    # --- Metronome loop ---
    start_time: float = time.monotonic()
    last_step: int = -1
    frame_interval: float = 1.0 / LIGHT_FPS

    # Track when we last sent a light frame so we don't flood LIFX.
    last_light_time: float = start_time

    try:
        while running:
            now: float = time.monotonic()
            elapsed: float = now - start_time

            # Current step in the repeating multi-bar pattern.
            raw_step: int = int(elapsed / step_duration)
            step: int = raw_step % STEPS_PER_BAR
            bar_index: int = (raw_step // STEPS_PER_BAR) % total_bars

            # --- On new step: check pattern, trigger hits ---
            if raw_step != last_step:
                last_step = raw_step
                current_bar: Bar = bars[bar_index]
                hits: list[str] = current_bar.get(step, [])

                if hits:
                    # Terminal feedback.
                    display_bar: int = bar_index + 1
                    cycle: int = raw_step // (STEPS_PER_BAR * total_bars) + 1
                    beat: int = step // STEPS_PER_BEAT + 1
                    sub: int = step % STEPS_PER_BEAT + 1
                    label: str = " + ".join(hits)
                    # Show cycle/bar/beat for multi-bar patterns.
                    section: str = ""
                    if total_bars > 1:
                        section = f" [{display_bar:>2d}/{total_bars}]"
                    print(
                        f"  cycle {cycle:>2d} | bar {display_bar:>2d}{section}"
                        f" | beat {beat}.{sub} | {label}",
                        flush=True,
                    )

                    for drum in hits:
                        if synth is not None:
                            synth.trigger(drum)
                        if lights is not None:
                            lights.trigger(drum)

            # --- Update lights at capped frame rate ---
            if lights is not None and (now - last_light_time) >= frame_interval:
                lights.update()
                last_light_time = now

            # Sleep until next meaningful event.  We need to wake up
            # for both step boundaries and light frame boundaries.
            next_step_time: float = start_time + (raw_step + 1) * step_duration
            next_frame_time: float = last_light_time + frame_interval
            sleep_until: float = min(next_step_time, next_frame_time)
            sleep_for: float = sleep_until - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)

    finally:
        print()
        print("  Shutting down...")
        if synth is not None:
            synth.close()
        if lights is not None:
            lights.close()
        print("  Done.")


if __name__ == "__main__":
    main()
