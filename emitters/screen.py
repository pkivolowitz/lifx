"""Terminal-based ANSI color emitter for development and headless testing.

Renders HSBK frames as colored block characters in a terminal with
24-bit (truecolor) support.  Each zone becomes two full-block characters,
producing a horizontal color strip that mirrors the physical light output.

No external dependencies — uses only ANSI escape sequences.

Not registered in the emitter registry — created programmatically for
development and demo use.

Usage::

    from emitters.screen import ScreenEmitter
    from engine import Controller

    emitter = ScreenEmitter(zone_count=50, label="dev-strip")
    ctrl = Controller([emitter])
    ctrl.play("cylon", speed=1.5)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.0"

import sys
from typing import Any, Optional, TextIO

from effects import HSBK, HSBK_MAX
from emitters import Emitter, EmitterCapabilities

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ANSI escape sequence prefix.
CSI: str = "\033["

# Terminal control sequences.
RESET: str = f"{CSI}0m"
CURSOR_HOME: str = f"{CSI}H"
CLEAR_SCREEN: str = f"{CSI}2J"
HIDE_CURSOR: str = f"{CSI}?25l"
SHOW_CURSOR: str = f"{CSI}?25h"

# Unicode full block character for zone display.
ZONE_CHAR: str = "\u2588"

# Number of block characters per zone (width of each zone on screen).
CHARS_PER_ZONE: int = 2

# HSB color space has 6 sextants (60 degrees each).
HUE_SEXTANTS: int = 6

# Maximum 8-bit RGB component value.
RGB_MAX: int = 255

# Frame type identifier.
_FRAME_TYPE_STRIP: str = "strip"


class ScreenEmitter(Emitter):
    """Render HSBK frames as ANSI-colored blocks in the terminal.

    Each zone becomes :data:`CHARS_PER_ZONE` colored block characters.
    Requires a terminal with 24-bit (truecolor) support — most modern
    terminals (iTerm2, Terminal.app, GNOME Terminal, Windows Terminal)
    support this.

    The emitter uses cursor-home repositioning so each frame overwrites
    the previous one in place, producing smooth animation.

    Not registered in the emitter registry (``emitter_type`` is ``None``).
    Created programmatically for development and demo use.

    Args:
        zone_count: Number of zones to display.
        label:      Human-readable name for this emitter.
        stream:     Output stream (defaults to ``sys.stdout``).
    """

    # Not registered — emitter_type stays None from the base class.

    def __init__(
        self,
        zone_count: int,
        label: str = "Screen",
        stream: Optional[TextIO] = None,
    ) -> None:
        """Initialize the terminal emitter.

        Args:
            zone_count: Number of addressable zones.
            label:      Display name for status reporting.
            stream:     Output stream (defaults to ``sys.stdout``).
        """
        # Initialize the Emitter base class with the label as name.
        super().__init__(label, {})
        self._zone_count: int = zone_count
        self._label: str = label
        self._stream: TextIO = stream or sys.stdout
        self._powered: bool = False

    # --- SOE lifecycle -----------------------------------------------------

    def on_open(self) -> None:
        """Clear the terminal and hide the cursor."""
        self.prepare_for_rendering()

    def on_emit(self, frame: Any, metadata: dict[str, Any]) -> bool:
        """Render a frame to the terminal.

        Args:
            frame:    ``list[HSBK]`` to render as colored blocks.
            metadata: Per-frame context dict.

        Returns:
            ``True`` on success.
        """
        if isinstance(frame, list):
            self.send_zones(frame)
            return True
        return False

    def on_close(self) -> None:
        """Restore the terminal if still powered."""
        if self._powered:
            self.power_off()

    def capabilities(self) -> EmitterCapabilities:
        """Declare terminal emitter capabilities.

        Returns:
            An :class:`EmitterCapabilities` for this terminal emitter.
        """
        return EmitterCapabilities(
            accepted_frame_types=[_FRAME_TYPE_STRIP],
            zones=self._zone_count,
        )

    # --- Engine-facing properties ------------------------------------------

    @property
    def zone_count(self) -> Optional[int]:
        """Number of addressable zones."""
        return self._zone_count

    @property
    def is_multizone(self) -> bool:
        """Whether this emitter has multiple zones."""
        return self._zone_count > 1

    @property
    def emitter_id(self) -> str:
        """Identifier string (e.g., ``'screen:dev-strip'``)."""
        return f"screen:{self._label}"

    @property
    def label(self) -> str:
        """Human-readable display name."""
        return self._label

    @property
    def product_name(self) -> str:
        """Description with zone count."""
        return f"Terminal ({self._zone_count} zones)"

    # --- Engine-facing frame dispatch --------------------------------------

    def send_zones(self, colors: list[HSBK], duration_ms: int = 0,
                   rapid: bool = True) -> None:
        """Render a multizone frame as colored blocks.

        Each zone is drawn as :data:`CHARS_PER_ZONE` full-block characters
        in the zone's RGB color.  The cursor is repositioned to the
        beginning of the line so subsequent frames overwrite in place.

        Args:
            colors:      One HSBK tuple per zone.
            duration_ms: Ignored (terminal has no transition support).
            rapid:       Ignored.
        """
        if not self._powered:
            return
        parts: list[str] = [CURSOR_HOME]
        for hsbk in colors:
            r, g, b = _hsbk_to_rgb(*hsbk)
            parts.append(f"{CSI}38;2;{r};{g};{b}m")
            parts.append(ZONE_CHAR * CHARS_PER_ZONE)
        parts.append(RESET)
        parts.append("\n")
        self._stream.write("".join(parts))
        self._stream.flush()

    def send_color(self, hue: int, sat: int, bri: int, kelvin: int,
                   duration_ms: int = 0) -> None:
        """Render a single color across all zones.

        Args:
            hue:         Hue (0--65535).
            sat:         Saturation (0--65535).
            bri:         Brightness (0--65535).
            kelvin:      Color temperature (ignored for RGB display).
            duration_ms: Ignored.
        """
        self.send_zones([(hue, sat, bri, kelvin)] * self._zone_count)

    # --- Engine-facing lifecycle -------------------------------------------

    def prepare_for_rendering(self) -> None:
        """Clear the terminal and hide the cursor."""
        self._stream.write(CLEAR_SCREEN + HIDE_CURSOR + CURSOR_HOME)
        self._stream.flush()

    def power_on(self, duration_ms: int = 0) -> None:
        """Enable rendering output.

        Args:
            duration_ms: Ignored.
        """
        self._powered = True

    def power_off(self, duration_ms: int = 0) -> None:
        """Disable rendering and restore the terminal cursor.

        Args:
            duration_ms: Ignored.
        """
        self._powered = False
        self._stream.write(SHOW_CURSOR + RESET + "\n")
        self._stream.flush()

    def close(self) -> None:
        """Restore the terminal if still powered."""
        self.on_close()

    def get_info(self) -> dict[str, Any]:
        """Return emitter status information.

        Returns:
            JSON-serializable dict with emitter identity.
        """
        return {
            "id": self.emitter_id,
            "label": self.label,
            "product": self.product_name,
            "zones": self.zone_count,
        }


# ---------------------------------------------------------------------------
# Color conversion
# ---------------------------------------------------------------------------

def _hsbk_to_rgb(
    hue: int,
    sat: int,
    bri: int,
    kelvin: int,
) -> tuple[int, int, int]:
    """Convert HSBK to 8-bit RGB for ANSI display.

    Uses the standard HSB-to-RGB sextant algorithm.  The kelvin
    (color temperature) component is ignored — terminals display
    pure RGB, not white-point-shifted color.

    Args:
        hue:    Hue (0--65535, maps to 0--360 degrees).
        sat:    Saturation (0--65535, maps to 0.0--1.0).
        bri:    Brightness (0--65535, maps to 0.0--1.0).
        kelvin: Color temperature (ignored).

    Returns:
        Tuple of ``(r, g, b)`` each in the range 0--255.
    """
    # Normalize to floating point.
    h: float = (hue / HSBK_MAX) * HUE_SEXTANTS  # 0.0 -- 6.0
    s: float = sat / HSBK_MAX                     # 0.0 -- 1.0
    b: float = bri / HSBK_MAX                     # 0.0 -- 1.0

    # HSB to RGB via the chroma / second-largest-component method.
    c: float = b * s              # chroma
    x: float = c * (1.0 - abs(h % 2.0 - 1.0))  # second component
    m: float = b - c              # match value (brightness floor)

    sextant: int = int(h) % HUE_SEXTANTS
    if sextant == 0:
        r, g, bl = c + m, x + m, m
    elif sextant == 1:
        r, g, bl = x + m, c + m, m
    elif sextant == 2:
        r, g, bl = m, c + m, x + m
    elif sextant == 3:
        r, g, bl = m, x + m, c + m
    elif sextant == 4:
        r, g, bl = x + m, m, c + m
    else:
        r, g, bl = c + m, m, x + m

    return (
        min(int(r * RGB_MAX), RGB_MAX),
        min(int(g * RGB_MAX), RGB_MAX),
        min(int(bl * RGB_MAX), RGB_MAX),
    )
