"""GlowUp Theremin — laser rangefinder Theremin simulator and effect.

Two laser rangefinders (or simulated sliders) measure hand distance.
The right hand controls pitch, the left hand controls volume — just
like a real Theremin.

Architecture (sensor → operator → emitter):
    1. Sensor (Mac simulator or ESP32) publishes distances to SignalBus
       via the MQTT bridge.
    2. Operator (ThereminEffect on the Pi) reads distances from SignalBus,
       computes note/amplitude, renders antialiased zone colors, and
       writes note output back to SignalBus.
    3. Emitter (LIFX string light) receives zone colors from the engine.
    4. Mac synth subscribes to note signals from MQTT and generates tone.

Modules:
    simulator   — Mac tkinter slider GUI (sensor simulator)
    synth       — Mac audio synthesizer (continuous tone generation)
    display     — Mac display window (hand heights, note, volume)

The effect itself lives in ``effects/theremin.py`` (auto-registered).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
from typing import Final

from network_config import net

# ---------------------------------------------------------------------------
# SignalBus Signal Names
# ---------------------------------------------------------------------------
# All data flows through the SignalBus MQTT bridge.
# MQTT topic = "glowup/signals/" + signal_name.
# Signal naming convention: {source}:{domain}:{field}

# Sensor inputs (published by simulator or ESP32).
SIGNAL_PITCH: Final[str] = "theremin:sensor:pitch"      # float, cm
SIGNAL_VOLUME: Final[str] = "theremin:sensor:volume"     # float, cm

# Operator outputs (published by ThereminEffect via SignalBus).
SIGNAL_FREQUENCY: Final[str] = "theremin:note:frequency"   # float, Hz
SIGNAL_AMPLITUDE: Final[str] = "theremin:note:amplitude"   # float, 0.0-1.0

# MQTT topic prefix (for direct MQTT subscribers like synth/display).
SIGNAL_TOPIC_PREFIX: Final[str] = "glowup/signals/"

# ---------------------------------------------------------------------------
# MQTT Broker (Pi)
# ---------------------------------------------------------------------------

MQTT_BROKER: Final[str] = net.broker
MQTT_PORT: Final[int] = 1883

# ---------------------------------------------------------------------------
# Sensor Distance Range (centimeters)
# ---------------------------------------------------------------------------
# VL53L0X effective range for hand detection.  The sliders simulate this.

DISTANCE_MIN_CM: Final[float] = 5.0     # Hand closest to sensor
DISTANCE_MAX_CM: Final[float] = 80.0    # Hand furthest from sensor

# ---------------------------------------------------------------------------
# Theremin Frequency Range
# ---------------------------------------------------------------------------
# Real Theremin: roughly C2 to C6 (4 octaves).
# Close hand (small distance) → high pitch.
# Far hand (large distance) → low pitch.

FREQ_MIN: Final[float] = 65.41    # C2
FREQ_MAX: Final[float] = 1046.50  # C6
OCTAVE_SPAN: Final[float] = math.log2(FREQ_MAX / FREQ_MIN)  # ≈ 4.0

# ---------------------------------------------------------------------------
# Volume Mapping
# ---------------------------------------------------------------------------
# Real Theremin: close hand → quiet, far hand → loud.

VOLUME_CURVE_EXP: Final[float] = 2.0  # Quadratic curve for natural feel

# ---------------------------------------------------------------------------
# Antialiased Glow
# ---------------------------------------------------------------------------

# Gaussian glow width (zones on each side of center).
GLOW_HALF_WIDTH: Final[float] = 2.5

# Hue mapping: low frequency (C2) = deep red, high frequency (C6) = violet.
HUE_DEGREES_MIN: Final[float] = 0.0     # Red
HUE_DEGREES_MAX: Final[float] = 270.0   # Violet

# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

SAMPLE_RATE: Final[int] = 44100
AUDIO_BLOCK_SIZE: Final[int] = 256  # Samples per callback (~5.8 ms)

# Waveform: blend of sine + harmonics for Theremin-like timbre.
# Each tuple is (harmonic_number, relative_amplitude).
HARMONICS: Final[list[tuple[int, float]]] = [
    (1, 1.0),     # Fundamental
    (2, 0.5),     # 2nd harmonic
    (3, 0.25),    # 3rd harmonic
    (4, 0.1),     # 4th harmonic — subtle warmth
]

# Portamento (pitch glide) time constant in seconds.
PORTAMENTO_TC: Final[float] = 0.05  # 50 ms — smooth but responsive

# ---------------------------------------------------------------------------
# Update Rates
# ---------------------------------------------------------------------------

SLIDER_PUBLISH_HZ: Final[int] = 30   # Mac slider → MQTT
DISPLAY_HZ: Final[int] = 20          # Mac display refresh

# ---------------------------------------------------------------------------
# Note Names
# ---------------------------------------------------------------------------

NOTE_NAMES: Final[list[str]] = [
    "C", "C#", "D", "D#", "E", "F",
    "F#", "G", "G#", "A", "A#", "B",
]


def freq_to_note_name(freq: float) -> tuple[str, int]:
    """Convert a frequency to the nearest note name and octave.

    Args:
        freq: Frequency in Hz.

    Returns:
        Tuple of (note_name, octave), e.g. ("A", 4) for 440 Hz.
    """
    if freq <= 0:
        return ("—", 0)
    # MIDI note number: A4 = 440 Hz = MIDI 69.
    midi: float = 12.0 * math.log2(freq / 440.0) + 69.0
    midi_rounded: int = round(midi)
    note_index: int = midi_rounded % 12
    octave: int = (midi_rounded // 12) - 1
    return (NOTE_NAMES[note_index], octave)


def distance_to_freq(pitch_cm: float) -> float:
    """Map pitch-hand distance to frequency.

    Closer hand → higher frequency (like a real Theremin pitch antenna).
    Uses logarithmic mapping so equal distance changes produce equal
    musical intervals.

    Args:
        pitch_cm: Distance from pitch sensor in centimeters.

    Returns:
        Frequency in Hz, clamped to [FREQ_MIN, FREQ_MAX].
    """
    # Normalize to 0.0 (closest) .. 1.0 (farthest).
    t: float = (pitch_cm - DISTANCE_MIN_CM) / (DISTANCE_MAX_CM - DISTANCE_MIN_CM)
    t = max(0.0, min(1.0, t))

    # Invert: close = high pitch, far = low pitch.
    t = 1.0 - t

    # Logarithmic mapping across the octave span.
    freq: float = FREQ_MIN * (2.0 ** (t * OCTAVE_SPAN))
    return max(FREQ_MIN, min(FREQ_MAX, freq))


def distance_to_amplitude(volume_cm: float) -> float:
    """Map volume-hand distance to amplitude.

    Closer hand → quieter (like a real Theremin volume antenna).
    Uses a power curve for natural feel.

    Args:
        volume_cm: Distance from volume sensor in centimeters.

    Returns:
        Amplitude in [0.0, 1.0].
    """
    t: float = (volume_cm - DISTANCE_MIN_CM) / (DISTANCE_MAX_CM - DISTANCE_MIN_CM)
    t = max(0.0, min(1.0, t))

    # Power curve: closer = quieter, farther = louder.
    return t ** VOLUME_CURVE_EXP
