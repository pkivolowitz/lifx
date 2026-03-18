"""Virtual multizone emitter — wraps N emitters as one unified zone canvas.

Multizone emitters contribute all their zones; single-zone emitters
contribute one zone each.  The total zone count is the sum.

This is the emitter-layer replacement for :class:`VirtualMultizoneDevice`
which previously lived in ``engine.py``.  The zone routing logic is
preserved exactly, but operates on :class:`Emitter` instances instead
of :class:`LifxDevice` — hardware-specific concerns (monochrome luma
conversion, power protocol) are pushed down to each member emitter.

Not registered in the emitter registry — created programmatically by
the server when grouping multiple physical emitters into a single
addressable canvas.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.3"

import logging
import time
from typing import Any, Optional

from effects import HSBK, KELVIN_DEFAULT
from emitters import Emitter, EmitterCapabilities
from transport import SendMode, broadcast_wake

# Module logger.
logger: logging.Logger = logging.getLogger("glowup.emitters.virtual")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sentinel zone index for single-zone emitters in the zone map.
# Distinguishes single-zone emitters (send_color) from multizone zone
# indices (batched send_zones).
SINGLE_ZONE_SENTINEL: int = -1

# Log fan-out timing stats every N frames.
FANOUT_STATS_INTERVAL: int = 500

# Warn if a single fan-out exceeds this threshold (seconds).
FANOUT_WARN_THRESHOLD_S: float = 0.010  # 10 ms

# Frame type identifiers.
_FRAME_TYPE_STRIP: str = "strip"
_FRAME_TYPE_SINGLE: str = "single"


class VirtualMultizoneEmitter(Emitter):
    """Wrap N emitters as a single multizone emitter.

    Multizone emitters contribute all their zones; single-zone emitters
    contribute one zone each.  The total virtual zone count is the sum
    across all members.

    For example, a group containing a 108-zone string light emitter and
    4 single-bulb emitters becomes a 112-zone virtual emitter.  Effects
    render all 112 zones in one ``render()`` call, and :meth:`send_zones`
    routes each virtual zone's color back to the correct member emitter —
    batching multizone updates into a single ``send_zones()`` call per
    emitter and dispatching single-zone colors via ``send_color()``.

    Hardware-specific concerns (monochrome luma conversion, protocol
    details) are handled by each member emitter's :meth:`send_color`
    implementation.

    Not registered in the emitter registry (``emitter_type`` is ``None``).
    Created programmatically by the server for multi-device groups.

    Args:
        emitters:      Member :class:`Emitter` instances.
        name:          Optional group name for display and identification.
        owns_emitters: If ``True`` (default), :meth:`close` closes all
                       member emitters.
    """

    # Not registered — emitter_type stays None from the base class.

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

        # Initialize the Emitter base class.  The virtual emitter has no
        # Param declarations and no per-instance config — it's a pure
        # composition wrapper.
        super().__init__(name or "virtual", {})

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

        # Fan-out timing statistics.
        self._fanout_count: int = 0
        self._fanout_sum_s: float = 0.0
        self._fanout_min_s: float = float("inf")
        self._fanout_max_s: float = 0.0
        self._fanout_warns: int = 0
        self._fanout_last_t: float = 0.0  # monotonic time of last send
        self._interval_sum_s: float = 0.0
        self._interval_min_s: float = float("inf")
        self._interval_max_s: float = 0.0

    # --- SOE lifecycle -----------------------------------------------------

    def on_open(self) -> None:
        """Prepare all member emitters for rendering."""
        self.prepare_for_rendering()

    def on_emit(self, frame: Any, metadata: dict[str, Any]) -> bool:
        """Route a frame to member emitters via :meth:`send_zones`.

        Args:
            frame:    ``list[HSBK]`` with one color per virtual zone.
            metadata: Per-frame context dict.

        Returns:
            ``True`` on successful dispatch.
        """
        if isinstance(frame, list):
            self.send_zones(frame)
            return True
        return False

    def on_close(self) -> None:
        """Close all member emitters (if this group owns them)."""
        if self._owns_emitters:
            for em in self._emitters:
                if hasattr(em, "on_close"):
                    em.on_close()

    def capabilities(self) -> EmitterCapabilities:
        """Declare virtual group capabilities.

        Always accepts ``"strip"`` and ``"single"`` frame types.  The
        zone count is the sum across all member emitters.

        Returns:
            An :class:`EmitterCapabilities` for this virtual group.
        """
        return EmitterCapabilities(
            accepted_frame_types=[_FRAME_TYPE_STRIP, _FRAME_TYPE_SINGLE],
            zones=self._zone_count,
            variable_topology=True,
        )

    # --- Engine-facing properties ------------------------------------------

    @property
    def zone_count(self) -> Optional[int]:
        """Total number of virtual zones across all member emitters."""
        return self._zone_count

    @property
    def is_multizone(self) -> bool:
        """Always ``True`` — a virtual group is always multizone."""
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

    # --- Engine-facing frame dispatch --------------------------------------

    def send_zones(
        self,
        colors: list[HSBK],
        duration_ms: int = 0,
        mode: Optional[SendMode] = None,
    ) -> None:
        """Route each virtual zone's color to the correct member emitter.

        Multizone members receive a single batched :meth:`send_zones`
        call with :attr:`SendMode.IMMEDIATE` so all devices in the
        group receive their frames simultaneously — no per-device ack
        pacing drift.  Single-zone members receive :meth:`send_color`.

        The *mode* parameter is accepted for API consistency but is
        always overridden to :attr:`SendMode.IMMEDIATE` for member
        dispatch.  Group fan-out is inherently fire-and-forget.

        Args:
            colors:      One HSBK tuple per virtual zone.
            duration_ms: Transition time in milliseconds.
            mode:        Ignored — member dispatch always uses
                         :attr:`SendMode.IMMEDIATE`.
        """
        # Collect colors destined for each multizone emitter so we can
        # batch them into one send_zones() call per emitter.
        multizone_batches: dict[int, dict[str, Any]] = {}

        for vz, (em, zone_idx) in enumerate(self._zone_map):
            if vz >= len(colors):
                break

            if zone_idx == SINGLE_ZONE_SENTINEL:
                # Single-zone emitter — dispatch immediately.
                # The emitter's send_color handles luma conversion if needed.
                h, s, b, k = colors[vz]
                em.send_color(h, s, b, k, duration_ms=duration_ms)
            else:
                # Multizone emitter — accumulate colors for batching.
                em_id: int = id(em)
                if em_id not in multizone_batches:
                    # Pre-allocate the full zone list for this emitter.
                    multizone_batches[em_id] = {
                        "em": em,
                        "colors": [None] * (em.zone_count or 1),
                    }
                multizone_batches[em_id]["colors"][zone_idx] = colors[vz]

        # Flush batched multizone updates — IMMEDIATE for simultaneous
        # delivery to all group members.
        t_start: float = time.monotonic()
        for batch in multizone_batches.values():
            em = batch["em"]
            batch_colors: list = batch["colors"]
            # Fill any gaps (shouldn't happen, but be safe).
            for i in range(len(batch_colors)):
                if batch_colors[i] is None:
                    batch_colors[i] = (0, 0, 0, KELVIN_DEFAULT)
            em.send_zones(batch_colors, duration_ms=duration_ms,
                         mode=SendMode.IMMEDIATE)
        t_end: float = time.monotonic()

        # Record fan-out timing.
        fanout_s: float = t_end - t_start
        self._fanout_count += 1
        self._fanout_sum_s += fanout_s
        if fanout_s < self._fanout_min_s:
            self._fanout_min_s = fanout_s
        if fanout_s > self._fanout_max_s:
            self._fanout_max_s = fanout_s
        if fanout_s > FANOUT_WARN_THRESHOLD_S:
            self._fanout_warns += 1

        # Record frame-to-frame interval.
        if self._fanout_last_t > 0.0:
            interval_s: float = t_start - self._fanout_last_t
            self._interval_sum_s += interval_s
            if interval_s < self._interval_min_s:
                self._interval_min_s = interval_s
            if interval_s > self._interval_max_s:
                self._interval_max_s = interval_s
        self._fanout_last_t = t_start

        # Periodic stats dump.
        if self._fanout_count % FANOUT_STATS_INTERVAL == 0:
            n: int = self._fanout_count
            avg_fanout_ms: float = (self._fanout_sum_s / n) * 1000.0
            avg_interval_ms: float = (
                (self._interval_sum_s / (n - 1)) * 1000.0 if n > 1 else 0.0
            )
            msg: str = (
                f"{self._name or 'group'} — {n} frames | "
                f"fanout avg {avg_fanout_ms:.2f} ms, "
                f"min {self._fanout_min_s * 1000.0:.2f} ms, "
                f"max {self._fanout_max_s * 1000.0:.2f} ms | "
                f"interval avg {avg_interval_ms:.1f} ms, "
                f"min {self._interval_min_s * 1000.0:.1f} ms, "
                f"max {self._interval_max_s * 1000.0:.1f} ms | "
                f"slow (>{int(FANOUT_WARN_THRESHOLD_S * 1000)} ms): "
                f"{self._fanout_warns}"
            )
            # Print to stderr so it appears in both CLI and systemd journal.
            print(msg, file=__import__("sys").stderr, flush=True)
            logger.info(msg)

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

    # --- Engine-facing lifecycle -------------------------------------------

    def prepare_for_rendering(self) -> None:
        """Broadcast-wake once, then prepare all member emitters.

        A single broadcast wake prods all bulbs on the network.  Each
        member then clears its committed layer individually.
        """
        broadcast_wake()
        for em in self._emitters:
            # Members skip their own broadcast_wake — already done above.
            if em._device is not None and em.is_multizone and em.zone_count:
                em._device.set_color(0, 0, 0, KELVIN_DEFAULT, duration_ms=0)

    def power_on(self, duration_ms: int = 0) -> None:
        """Power on all member emitters.

        Args:
            duration_ms: Transition duration in milliseconds.
        """
        for em in self._emitters:
            em.power_on(duration_ms=duration_ms)

    def power_off(self, duration_ms: int = 0) -> None:
        """Power off all member emitters.

        Args:
            duration_ms: Transition duration in milliseconds.
        """
        for em in self._emitters:
            em.power_off(duration_ms=duration_ms)

    def close(self) -> None:
        """Close all member emitters (if this group owns them).

        Delegates to :meth:`on_close`.
        """
        self.on_close()

    # --- Group-specific ----------------------------------------------------

    def get_emitter_list(self) -> list[Emitter]:
        """Return a copy of the member emitter list.

        Returns:
            The underlying :class:`Emitter` list (defensive copy).
        """
        return list(self._emitters)

    def get_info(self) -> dict[str, Any]:
        """Return group status with member details.

        Returns:
            JSON-serializable dict with group identity and member info.
        """
        return {
            "id": self.emitter_id,
            "label": self.label,
            "product": self.product_name,
            "zones": self.zone_count,
            "members": [em.get_info() for em in self._emitters],
        }
