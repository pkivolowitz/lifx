"""Virtual multizone emitter -- wraps N emitters as one unified zone canvas.

Multizone emitters contribute all their zones; single-zone emitters
contribute one zone each.  The total zone count is the sum.

This is the emitter-layer replacement for :class:`VirtualMultizoneDevice`
which previously lived in ``engine.py``.  The zone routing logic is
preserved exactly, but operates on :class:`Emitter` instances instead
of :class:`LifxDevice` -- hardware-specific concerns (monochrome luma
conversion, power protocol) are pushed down to each member emitter.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

from typing import Any, Optional

from effects import HSBK, KELVIN_DEFAULT
from emitters import Emitter, EmitterInfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sentinel zone index for single-zone emitters in the zone map.
# Distinguishes single-zone emitters (send_color) from multizone zone
# indices (batched send_zones).
SINGLE_ZONE_SENTINEL: int = -1


class VirtualMultizoneEmitter(Emitter):
    """Wrap N emitters as a single multizone emitter.

    Multizone emitters contribute all their zones; single-zone emitters
    contribute one zone each.  The total virtual zone count is the sum
    across all members.

    For example, a group containing a 108-zone string light emitter and
    4 single-bulb emitters becomes a 112-zone virtual emitter.  Effects
    render all 112 zones in one ``render()`` call, and :meth:`send_zones`
    routes each virtual zone's color back to the correct member emitter --
    batching multizone updates into a single ``send_zones()`` call per
    emitter and dispatching single-zone colors via ``send_color()``.

    Hardware-specific concerns (monochrome luma conversion, protocol
    details) are handled by each member emitter's :meth:`send_color`
    implementation.

    Args:
        emitters:      Member :class:`Emitter` instances.  The list order
                       determines the virtual zone layout.
        name:          Optional group name for display and identification.
        owns_emitters: If ``True`` (default), :meth:`close` closes all
                       member emitters.  Set to ``False`` when the caller
                       manages emitter lifetimes separately.
    """

    def __init__(
        self,
        emitters: list[Emitter],
        name: str = "",
        owns_emitters: bool = True,
    ) -> None:
        """Initialize with a list of member emitters.

        Builds a zone map that records which member emitter and zone
        index each virtual zone corresponds to.

        Args:
            emitters:      Member :class:`Emitter` instances, each already
                           initialized.  The list order determines zone
                           assignment.
            name:          Optional group name (used for display and as the
                           emitter identifier).
            owns_emitters: If ``True`` (default), :meth:`close` closes all
                           member emitters.

        Raises:
            ValueError: If *emitters* is empty.
        """
        if not emitters:
            raise ValueError(
                "VirtualMultizoneEmitter requires at least one emitter."
            )

        self._emitters: list[Emitter] = list(emitters)
        self._owns_emitters: bool = owns_emitters
        self._name: str = name

        # Build the zone map: list of (emitter, zone_index) tuples.
        # For multizone emitters, zone_index is the physical zone number.
        # For single-zone emitters, zone_index is SINGLE_ZONE_SENTINEL.
        self._zone_map: list[tuple[Emitter, int]] = []
        for em in self._emitters:
            zones: int = em.zone_count if em.zone_count else 1
            if em.is_multizone:
                # Multizone emitter: each physical zone becomes a virtual zone.
                for z in range(zones):
                    self._zone_map.append((em, z))
            else:
                # Single-zone emitter: one virtual zone, sentinel index.
                self._zone_map.append((em, SINGLE_ZONE_SENTINEL))

        self._zone_count: int = len(self._zone_map)

    # --- Emitter properties ---

    @property
    def zone_count(self) -> Optional[int]:
        """Total number of virtual zones across all member emitters."""
        return self._zone_count

    @property
    def is_multizone(self) -> bool:
        """Always ``True`` -- a virtual group is always multizone."""
        return True

    @property
    def emitter_id(self) -> str:
        """Group-based identifier (e.g., ``'group:porch'``)."""
        if self._name:
            return f"group:{self._name}"
        return f"group({len(self._emitters)})"

    @property
    def label(self) -> str:
        """Group display name."""
        return self._name or "Virtual group"

    @property
    def product_name(self) -> str:
        """Description string with total zone count."""
        return f"{self._zone_count}-zone virtual multizone"

    # --- Frame dispatch ---

    def send_zones(
        self,
        colors: list[HSBK],
        duration_ms: int = 0,
        rapid: bool = True,
    ) -> None:
        """Route each virtual zone's color to the correct member emitter.

        Multizone members receive a single batched :meth:`send_zones`
        call.  Single-zone members receive :meth:`send_color` -- the
        member emitter handles any hardware-specific conversion
        (e.g., monochrome luma) internally.

        Args:
            colors:      One HSBK tuple per virtual zone.
            duration_ms: Transition time in milliseconds.
            rapid:       Passed through to multizone :meth:`send_zones`.
        """
        # Collect colors destined for each multizone emitter so we can
        # batch them into one send_zones() call per emitter.
        multizone_batches: dict[int, dict[str, Any]] = {}

        for vz, (em, zone_idx) in enumerate(self._zone_map):
            if vz >= len(colors):
                break

            if zone_idx == SINGLE_ZONE_SENTINEL:
                # Single-zone emitter -- dispatch immediately.
                # The emitter's send_color handles luma conversion if needed.
                h, s, b, k = colors[vz]
                em.send_color(h, s, b, k, duration_ms=duration_ms)
            else:
                # Multizone emitter -- accumulate colors for batching.
                em_id: int = id(em)
                if em_id not in multizone_batches:
                    # Pre-allocate the full zone list for this emitter.
                    multizone_batches[em_id] = {
                        "em": em,
                        "colors": [None] * (em.zone_count or 1),
                    }
                multizone_batches[em_id]["colors"][zone_idx] = colors[vz]

        # Flush batched multizone updates.
        for batch in multizone_batches.values():
            em = batch["em"]
            batch_colors: list = batch["colors"]
            # Fill any gaps (shouldn't happen, but be safe).
            for i in range(len(batch_colors)):
                if batch_colors[i] is None:
                    batch_colors[i] = (0, 0, 0, KELVIN_DEFAULT)
            em.send_zones(batch_colors, duration_ms=duration_ms, rapid=rapid)

    def send_color(
        self,
        hue: int,
        sat: int,
        bri: int,
        kelvin: int,
        duration_ms: int = 0,
    ) -> None:
        """Set all member emitters to the same color.

        Used by the Engine's fade-to-black on stop.

        Args:
            hue:         Hue (0--65535).
            sat:         Saturation (0--65535).
            bri:         Brightness (0--65535).
            kelvin:      Color temperature (1500--9000 K).
            duration_ms: Transition time in milliseconds.
        """
        for em in self._emitters:
            em.send_color(hue, sat, bri, kelvin, duration_ms=duration_ms)

    # --- Lifecycle ---

    def prepare_for_rendering(self) -> None:
        """Prepare all member emitters for rendering."""
        for em in self._emitters:
            em.prepare_for_rendering()

    def power_on(self, duration_ms: int = 0) -> None:
        """Power on all member emitters."""
        for em in self._emitters:
            em.power_on(duration_ms=duration_ms)

    def power_off(self, duration_ms: int = 0) -> None:
        """Power off all member emitters."""
        for em in self._emitters:
            em.power_off(duration_ms=duration_ms)

    def close(self) -> None:
        """Close all member emitters (if this group owns them)."""
        if self._owns_emitters:
            for em in self._emitters:
                em.close()

    # --- Group-specific ---

    def get_emitter_list(self) -> list[Emitter]:
        """Return a copy of the member emitter list.

        Returns:
            The underlying :class:`Emitter` list (defensive copy).
        """
        return list(self._emitters)

    def get_info(self) -> EmitterInfo:
        """Return group status with member details."""
        return {
            "id": self.emitter_id,
            "label": self.label,
            "product": self.product_name,
            "zones": self.zone_count,
            "members": [em.get_info() for em in self._emitters],
        }
