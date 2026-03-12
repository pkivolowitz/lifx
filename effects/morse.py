"""Morse code effect — spells out a message in flashing light.

The entire string flashes in unison, encoding a message in International
Morse Code.  Timing follows the standard ratio: dot = 1 unit, dash = 3 units,
intra-char gap = 1 unit, inter-char gap = 3 units, word gap = 7 units.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

from typing import Optional

from . import (
    Effect, Param, HSBK,
    KELVIN_DEFAULT, KELVIN_MIN, KELVIN_MAX,
    hue_to_u16, pct_to_u16,
)

# ---------------------------------------------------------------------------
# Constants — Morse timing in abstract "units"
# ---------------------------------------------------------------------------

# Duration of a dot in units.
DOT_UNITS: int = 1

# Duration of a dash in units.
DASH_UNITS: int = 3

# Gap between symbols within a character.
INTRA_CHAR_GAP: int = 1

# Gap between characters within a word.
INTER_CHAR_GAP: int = 3

# Gap between words.
WORD_GAP: int = 7

# ---------------------------------------------------------------------------
# International Morse Code lookup table
# ---------------------------------------------------------------------------

MORSE: dict[str, str] = {
    'A': '.-',    'B': '-...',  'C': '-.-.',  'D': '-..',
    'E': '.',     'F': '..-.',  'G': '--.',   'H': '....',
    'I': '..',    'J': '.---',  'K': '-.-',   'L': '.-..',
    'M': '--',    'N': '-.',    'O': '---',   'P': '.--.',
    'Q': '--.-',  'R': '.-.',   'S': '...',   'T': '-',
    'U': '..-',   'V': '...-',  'W': '.--',   'X': '-..-',
    'Y': '-.--',  'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--',
    '4': '....-', '5': '.....', '6': '-....', '7': '--...',
    '8': '---..', '9': '----.',
    '.': '.-.-.-', ',': '--..--', '?': '..--..', '!': '-.-.--',
    "'": '.----.', '/': '-..-.', '(': '-.--.', ')': '-.--.-',
    '&': '.-...',  ':': '---...', ';': '-.-.-.', '=': '-...-',
    '+': '.-.-.',  '-': '-....-', '_': '..--.-', '"': '.-..-.',
    '$': '...-..-', '@': '.--.-.',
}


def _message_to_timeline(message: str) -> list[tuple[bool, int]]:
    """Convert a message string into a list of (on, units) tuples.

    Each tuple represents a segment of the Morse signal: ``True`` means
    the light is on (dot or dash), ``False`` means a gap.

    Args:
        message: The text to encode (case-insensitive).

    Returns:
        A list of ``(is_on, duration_in_units)`` tuples.
    """
    timeline: list[tuple[bool, int]] = []
    words: list[str] = message.upper().split()

    for wi, word in enumerate(words):
        for ci, char in enumerate(word):
            code: Optional[str] = MORSE.get(char)
            if code is None:
                # Skip characters that have no Morse representation.
                continue

            for si, symbol in enumerate(code):
                # Dot = 1 unit on, dash = 3 units on.
                timeline.append((True, DOT_UNITS if symbol == '.' else DASH_UNITS))

                # Intra-character gap: 1 unit between symbols within a char.
                if si < len(code) - 1:
                    timeline.append((False, INTRA_CHAR_GAP))

            # Inter-character gap: 3 units between characters within a word.
            if ci < len(word) - 1:
                timeline.append((False, INTER_CHAR_GAP))

        # Word gap: 7 units between words.
        if wi < len(words) - 1:
            timeline.append((False, WORD_GAP))

    return timeline


class Morse(Effect):
    """Flash a message in Morse code across the entire string.

    The whole string lights up in unison for dots and dashes, going
    dark during gaps.  After the complete message plays, a configurable
    pause elapses before the message repeats.
    """

    name: str = "morse"
    description: str = "Flashes a message in Morse code"

    message = Param("HELLO WORLD", description="Message to transmit")
    unit = Param(0.15, min=0.05, max=2.0,
                 description="Duration of one dot in seconds")
    hue = Param(0.0, min=0.0, max=360.0,
                description="Flash hue in degrees")
    saturation = Param(0, min=0, max=100,
                       description="Flash saturation (0=white)")
    brightness = Param(100, min=0, max=100,
                       description="Flash brightness percent")
    bg_bri = Param(0, min=0, max=100,
                   description="Background brightness percent (off between flashes)")
    pause = Param(5.0, min=0.0, max=30.0,
                  description="Pause in seconds before repeating")
    kelvin = Param(KELVIN_DEFAULT, min=KELVIN_MIN, max=KELVIN_MAX,
                   description="Color temperature in Kelvin")

    def __init__(self, **overrides: dict) -> None:
        """Initialize Morse timeline cache.

        Args:
            **overrides: Parameter names mapped to override values.
        """
        super().__init__(**overrides)
        # Cached timeline — rebuilt when the message parameter changes.
        self._timeline: Optional[list[tuple[bool, int]]] = None
        self._total_units: Optional[int] = None
        self._cached_message: Optional[str] = None

    def period(self) -> float:
        """One full message cycle: Morse pattern duration + pause."""
        if self._timeline is None:
            self._build_timeline()
        return self._total_units * self.unit + self.pause

    def _build_timeline(self) -> None:
        """Parse the current message into a Morse timeline.

        Called on first render and whenever the message parameter
        changes at runtime.
        """
        self._timeline = _message_to_timeline(self.message)
        self._total_units = sum(units for _, units in self._timeline)
        self._cached_message = self.message

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame — all zones show the same on/off state.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* identical HSBK tuples.
        """
        # Rebuild timeline if message changed or not yet built.
        if self._cached_message != self.message or self._timeline is None:
            self._build_timeline()

        flash_hue: int = hue_to_u16(self.hue)
        flash_sat: int = pct_to_u16(self.saturation)
        max_bri: int = pct_to_u16(self.brightness)
        bg_b: int = pct_to_u16(self.bg_bri)

        # Total cycle = Morse pattern duration + inter-message pause.
        cycle_time: float = self._total_units * self.unit + self.pause
        if cycle_time <= 0:
            return [(flash_hue, flash_sat, bg_b, self.kelvin)] * zone_count

        # Position within the current cycle.
        pos: float = t % cycle_time
        morse_duration: float = self._total_units * self.unit

        # During the pause gap after the message completes, show background.
        if pos >= morse_duration:
            return [(flash_hue, flash_sat, bg_b, self.kelvin)] * zone_count

        # Walk the timeline to find which segment we're currently in.
        elapsed: float = 0.0
        on: bool = False
        for is_on, units in self._timeline:
            seg_duration: float = units * self.unit
            if elapsed + seg_duration > pos:
                on = is_on
                break
            elapsed += seg_duration

        if on:
            color: HSBK = (flash_hue, flash_sat, max_bri, self.kelvin)
        else:
            color = (flash_hue, flash_sat, bg_b, self.kelvin)

        return [color] * zone_count
