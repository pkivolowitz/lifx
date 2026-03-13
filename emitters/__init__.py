"""Emitter abstraction layer for GlowUp.

An Emitter is any output device that can display a frame of HSBK colors.
The :class:`Engine` renders effects and dispatches frames to Emitters
without knowing the underlying protocol (LIFX UDP, ANSI terminal, ArtNet,
GPIO, etc.).

This is the architectural seam that turns GlowUp from a LIFX effect
engine into a domain-agnostic site runner.  The existing LIFX transport
becomes one emitter driver among many.

Example::

    from emitters.lifx import LifxEmitter
    from transport import LifxDevice
    from engine import Controller

    device = LifxDevice("10.0.0.62")
    device.query_all()

    emitter = LifxEmitter(device)
    ctrl = Controller([emitter])
    ctrl.play("cylon", speed=1.5)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

from abc import ABC, abstractmethod
from typing import Any, Optional

from effects import HSBK

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Version string for the emitter subsystem.
EMITTER_VERSION: str = __version__

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# JSON-serializable dict returned by Emitter.get_info() for status/API use.
EmitterInfo = dict[str, Any]


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------

class Emitter(ABC):
    """Abstract base class for all output devices.

    Subclasses must implement the frame-dispatch methods that the Engine's
    send thread calls every frame, plus lifecycle methods for startup and
    shutdown.

    The interface is deliberately minimal -- it captures only what the
    Engine's render and send threads need.  Protocol-specific queries
    (LIFX zone readback, DMX status, etc.) belong on concrete
    subclasses, not here.

    Properties read by Engine every frame:
        zone_count:   Number of addressable zones (or ``None`` if unknown).
        is_multizone: ``True`` when :meth:`send_zones` should be used.

    Identity properties for status reporting:
        emitter_id:   Unique identifier (IP, ``"screen:0"``, etc.).
        label:        Human-readable display name.
        product_name: Hardware / driver description string.

    Frame dispatch (called by Engine send thread):
        send_zones:   Send a full frame of per-zone colors.
        send_color:   Send a single color to the entire emitter.

    Lifecycle:
        prepare_for_rendering: One-time setup before the render loop.
        power_on / power_off:  Activate / deactivate the output.
        close:                 Release resources.
    """

    # --- Properties read by Engine every frame ---

    @property
    @abstractmethod
    def zone_count(self) -> Optional[int]:
        """Number of addressable zones, or ``None`` if not yet known."""

    @property
    @abstractmethod
    def is_multizone(self) -> bool:
        """Whether this emitter has multiple independently addressable zones.

        When ``True``, the Engine calls :meth:`send_zones`.
        When ``False``, the Engine calls :meth:`send_color`.
        """

    # --- Identity properties for status / API ---

    @property
    @abstractmethod
    def emitter_id(self) -> str:
        """Unique identifier for this emitter.

        Replaces ``dev.ip`` -- a screen emitter has no IP.  Examples:
        ``"10.0.0.62"``, ``"screen:0"``, ``"group:porch"``.
        """

    @property
    @abstractmethod
    def label(self) -> str:
        """Human-readable display name."""

    @property
    @abstractmethod
    def product_name(self) -> str:
        """Hardware or driver description string."""

    # --- Frame dispatch (called by Engine send thread) ---

    @abstractmethod
    def send_zones(self, colors: list[HSBK], duration_ms: int = 0,
                   rapid: bool = True) -> None:
        """Send a full frame of per-zone colors.

        Called by the Engine for multizone emitters (:attr:`is_multizone`
        is ``True``).

        Args:
            colors:      One HSBK tuple per zone.
            duration_ms: Firmware transition duration in milliseconds.
            rapid:       If ``True``, use fire-and-forget (no ack).
        """

    @abstractmethod
    def send_color(self, hue: int, sat: int, bri: int, kelvin: int,
                   duration_ms: int = 0) -> None:
        """Send a single color to the entire emitter.

        Called by the Engine for single-zone emitters, and also during
        fade-to-black on stop.  Implementations must handle any
        hardware-specific concerns internally (e.g., monochrome luma
        conversion for LIFX white-only bulbs).

        Args:
            hue:         Hue component (0--65535).
            sat:         Saturation component (0--65535).
            bri:         Brightness component (0--65535).
            kelvin:      Color temperature (1500--9000 K).
            duration_ms: Firmware transition duration in milliseconds.
        """

    # --- Lifecycle ---

    @abstractmethod
    def prepare_for_rendering(self) -> None:
        """One-time setup before the render loop starts.

        Each emitter knows its own startup ritual:
        - LifxEmitter: clears firmware committed state to black.
        - ScreenEmitter: clears the terminal.
        - A DMX emitter might send a zero-frame.
        """

    @abstractmethod
    def power_on(self, duration_ms: int = 0) -> None:
        """Activate the output device.

        Args:
            duration_ms: Transition duration in milliseconds.
        """

    @abstractmethod
    def power_off(self, duration_ms: int = 0) -> None:
        """Deactivate the output device.

        Args:
            duration_ms: Transition duration in milliseconds.
        """

    @abstractmethod
    def close(self) -> None:
        """Release resources (sockets, file handles, etc.)."""

    # --- Optional: override for richer status reporting ---

    def get_info(self) -> EmitterInfo:
        """Return a JSON-serializable dict for status / API responses.

        Subclasses may override to include protocol-specific fields
        (e.g., IP address, MAC, member count for virtual groups).
        """
        return {
            "id": self.emitter_id,
            "label": self.label,
            "product": self.product_name,
            "zones": self.zone_count,
        }
