"""LIFX protocol emitter — drives LIFX devices via UDP LAN protocol.

First concrete implementation of the SOE Emitter ABC.  All LIFX-specific
protocol concerns live here:

- Extended multizone dispatch (type 510, up to 82 zones per packet)
- Single-zone color dispatch (type 102)
- Monochrome BT.709 luma conversion for white-only bulbs
- Firmware committed-state clearing (prevents glitch on dropped UDP)
- Power on/off via LIFX type 117

Two creation paths are supported:

**Config-based** (via EmitterManager / ``create_emitter()``)::

    emitter = create_emitter("lifx", "porch", {"ip": "10.0.0.62"})

**Programmatic** (via factory classmethod)::

    from emitters.lifx import LifxEmitter
    from transport import LifxDevice

    device = LifxDevice("10.0.0.62")
    device.query_all()
    emitter = LifxEmitter.from_device(device)

The Engine calls the legacy methods (``send_zones``, ``send_color``,
``prepare_for_rendering``, ``power_on``, ``power_off``) directly.  The
EmitterManager calls the SOE lifecycle (``on_open``, ``on_emit``,
``on_close``).  Both paths work simultaneously.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.2"

import logging
import struct
import time
from typing import Any, Optional

from effects import HSBK, KELVIN_DEFAULT, hsbk_to_luminance
from emitters import Emitter, EmitterCapabilities
from transport import (
    LifxDevice, MSG_LIGHT_SET_POWER, POWER_OFF, POWER_ON,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default color temperature used when clearing the committed layer.
_CLEAR_KELVIN: int = KELVIN_DEFAULT

# Frame type identifiers for capabilities advertisement.
_FRAME_TYPE_STRIP: str = "strip"     # list[HSBK] — multizone
_FRAME_TYPE_SINGLE: str = "single"   # HSBK or single-element list

# LIFX devices animate at up to 30 Hz over the LAN protocol.
_MAX_RATE_HZ: float = 30.0
# Pause between power-off and power-on during the wake cycle (seconds).
# Gives the firmware time to fully process the state transition.
_POWER_CYCLE_PAUSE_S: float = 0.5

# Module logger.
logger: logging.Logger = logging.getLogger("glowup.emitters.lifx")


class LifxEmitter(Emitter):
    """Emitter that drives a single LIFX device via UDP LAN protocol.

    Bridges the SOE Emitter ABC lifecycle with the existing Engine's
    direct-method interface.  The engine calls ``send_zones`` and
    ``send_color``; the EmitterManager calls ``on_emit``.  Both paths
    route to the same :class:`LifxDevice` transport underneath.

    Typical creation via factory::

        device = LifxDevice("10.0.0.62")
        device.query_all()
        emitter = LifxEmitter.from_device(device)

    Config-based creation (via ``create_emitter``)::

        emitter = create_emitter("lifx", "porch", {"ip": "10.0.0.62"})
        emitter.on_configure(full_server_config)
    """

    emitter_type: str = "lifx"
    description: str = "LIFX device driver via UDP LAN protocol"

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        """Initialize from a name and configuration dict.

        The ``"ip"`` key in *config* is required for config-based creation
        (the device is created in :meth:`on_configure`).  When using
        :meth:`from_device`, the device is injected after construction.

        Args:
            name:   Instance name (typically the device label or IP).
            config: Instance configuration dict.  Expected keys:

                    * ``"ip"`` — device IP address (required for
                      config-based creation).
        """
        super().__init__(name, config)
        # Device reference — set by from_device() or on_configure().
        self._device: Optional[LifxDevice] = None

    @classmethod
    def from_device(cls, device: LifxDevice) -> "LifxEmitter":
        """Create a LifxEmitter from a pre-queried LifxDevice.

        This is the standard creation path for the existing server and
        engine code.  The device must have been queried via
        :meth:`LifxDevice.query_all` so that ``zone_count``, ``product``,
        ``label``, etc. are populated.

        Args:
            device: A fully-queried :class:`LifxDevice`.

        Returns:
            A :class:`LifxEmitter` wrapping the device, ready for use
            by the Engine without further configuration.
        """
        name: str = device.label or device.ip
        config: dict[str, Any] = {"ip": device.ip}
        instance: LifxEmitter = cls(name, config)
        instance._device = device
        return instance

    # --- SOE lifecycle (called by EmitterManager) --------------------------

    def on_configure(self, config: dict[str, Any]) -> None:
        """Create and query the LifxDevice if not already provided.

        Called by the EmitterManager after construction.  When the emitter
        was created via :meth:`from_device`, the device is already set and
        this is a no-op.

        Args:
            config: Full server configuration dict (for context).

        Raises:
            ValueError: If no IP address is available.
        """
        if self._device is not None:
            return  # Already connected (from_device path).

        ip: str = self._config.get("ip", "")
        if not ip:
            raise ValueError(
                f"LifxEmitter '{self.name}' requires 'ip' in config"
            )
        logger.info("Connecting to LIFX device at %s", ip)
        self._device = LifxDevice(ip)
        self._device.query_all()
        logger.info(
            "Connected: %s — %s [%s zones]",
            self._device.label or "?",
            self._device.product_name or "?",
            self._device.zone_count or "?",
        )

    def on_open(self) -> None:
        """Prepare the device for rendering.

        Clears the LIFX firmware committed state to black on multizone
        devices.  The overlay protocol (type 510) writes to a temporary
        buffer; if a UDP frame is lost the firmware reveals the committed
        layer.  Setting it to black makes those glitches invisible.
        """
        self.prepare_for_rendering()

    def on_emit(self, frame: Any, metadata: dict[str, Any]) -> bool:
        """Dispatch a frame to the LIFX device.

        Accepts ``list[HSBK]`` (multizone strip) or a single ``HSBK``
        tuple.  Routes to :meth:`send_zones` or :meth:`send_color` based
        on device topology and frame shape.

        Args:
            frame:    ``list[HSBK]`` for multizone, or ``HSBK`` tuple
                      for single-zone.
            metadata: Per-frame context dict (see :class:`Emitter`).

        Returns:
            ``True`` on successful transmission, ``False`` on failure.
        """
        if self._device is None:
            return False
        try:
            duration_ms: int = int(metadata.get("duration_ms", 0))
            if isinstance(frame, list):
                if self.is_multizone:
                    self.send_zones(frame, duration_ms=duration_ms)
                elif len(frame) > 0:
                    h, s, b, k = frame[0]
                    self.send_color(h, s, b, k, duration_ms=duration_ms)
                else:
                    return False
            elif isinstance(frame, tuple) and len(frame) == 4:
                h, s, b, k = frame
                self.send_color(h, s, b, k, duration_ms=duration_ms)
            else:
                logger.warning(
                    "LifxEmitter '%s' received unsupported frame type: %s",
                    self.name, type(frame).__name__,
                )
                return False
            return True
        except Exception as exc:
            logger.warning(
                "LifxEmitter '%s' send failed: %s", self.name, exc)
            return False

    def on_close(self) -> None:
        """Close the underlying UDP socket."""
        if self._device is not None:
            self._device.close()

    def capabilities(self) -> EmitterCapabilities:
        """Declare LIFX device capabilities.

        Multizone devices accept ``"strip"`` and ``"single"`` frame types.
        Single-zone bulbs accept only ``"single"``.  Zone count is
        populated after device query.

        Returns:
            An :class:`EmitterCapabilities` for this device.
        """
        if self._device is not None and self.is_multizone:
            frame_types: list[str] = [_FRAME_TYPE_STRIP, _FRAME_TYPE_SINGLE]
        else:
            frame_types = [_FRAME_TYPE_SINGLE]
        return EmitterCapabilities(
            accepted_frame_types=frame_types,
            max_rate_hz=_MAX_RATE_HZ,
            zones=self.zone_count or 0,
        )

    # --- Engine-facing properties ------------------------------------------
    # The Engine reads these directly each frame.  They delegate to the
    # underlying LifxDevice and return safe defaults when not connected.

    @property
    def zone_count(self) -> Optional[int]:
        """Number of addressable zones on the LIFX device."""
        if self._device is None:
            return None
        return self._device.zone_count

    @property
    def is_multizone(self) -> bool:
        """Whether this device is a multizone strip or beam."""
        if self._device is None:
            return False
        return bool(self._device.is_multizone)

    @property
    def emitter_id(self) -> str:
        """IP address of the LIFX device."""
        if self._device is None:
            return self._config.get("ip", "unknown")
        return self._device.ip

    @property
    def label(self) -> str:
        """Device label (e.g., 'Porch String Lights')."""
        if self._device is None:
            return self.name
        return self._device.label or self._device.ip

    @property
    def product_name(self) -> str:
        """Product description (e.g., 'LIFX Z')."""
        if self._device is None:
            return "LIFX (not connected)"
        return self._device.product_name or "Unknown LIFX"

    @property
    def is_neon(self) -> bool:
        """Whether this device is a Neon-class strip.

        Neon firmware requires lower FPS and longer transition times
        for smooth animation.
        """
        if self._device is None:
            return False
        return bool(self._device.is_neon)

    # --- Engine-facing frame dispatch --------------------------------------
    # The Engine's send thread calls these directly.  They are NOT part
    # of the Emitter ABC — they are LIFX-specific public methods.

    def send_zones(self, colors: list[HSBK], duration_ms: int = 0,
                   mode: Optional["SendMode"] = None) -> None:
        """Send a multizone frame via LIFX extended protocol (type 510).

        Chunks colors into packets of up to 82 zones.  The final packet
        carries the atomic-apply flag to prevent visual tearing.

        Args:
            colors:      One HSBK tuple per zone.
            duration_ms: Firmware transition duration in milliseconds.
            mode:        :class:`SendMode` delivery strategy.  Defaults
                         to :attr:`SendMode.ACK_PACED`.
        """
        if self._device is not None:
            from transport import SendMode
            send_mode: SendMode = mode if mode is not None else SendMode.ACK_PACED
            self._device.set_zones(colors, duration_ms=duration_ms,
                                   mode=send_mode)

    def send_color(self, hue: int, sat: int, bri: int, kelvin: int,
                   duration_ms: int = 0) -> None:
        """Send a single color to the device (type 102).

        Handles monochrome devices internally: if the device is not
        polychrome, applies BT.709 luma conversion so colored effects
        produce correct perceptual brightness on white-only bulbs.

        Args:
            hue:         Hue (0--65535).
            sat:         Saturation (0--65535).
            bri:         Brightness (0--65535).
            kelvin:      Color temperature (1500--9000 K).
            duration_ms: Firmware transition duration in milliseconds.
        """
        if self._device is None:
            return
        if self._device.is_polychrome is False:
            # Monochrome device: convert to perceptual luminance.
            hue, sat, bri, kelvin = hsbk_to_luminance(hue, sat, bri, kelvin)
        self._device.set_color(hue, sat, bri, kelvin,
                               duration_ms=duration_ms)

    # --- Engine-facing lifecycle -------------------------------------------
    # Called by the Engine and Controller at various pipeline stages.

    def prepare_for_rendering(self) -> None:
        """Power-cycle the device, then clear committed state to black.

        The power off/on cycle ensures the bulb's firmware is in a
        responsive state before we start sending effect frames.  LIFX
        bulbs can become unresponsive to unicast commands after a
        factory reset or network disruption even while still answering
        discovery broadcasts.

        After the wake cycle, the extended multizone committed layer is
        set to black so that dropped UDP frames reveal black instead of
        stale colors.
        """
        if self._device is not None:
            # Wake cycle: power off, pause, power on.
            # Fire-and-forget — if the bulb is unresponsive we must not
            # block server startup waiting for an ack that may never come.
            try:
                self._device.fire_and_forget(
                    MSG_LIGHT_SET_POWER,
                    struct.pack("<HI", POWER_OFF, 0),
                )
            except OSError:
                pass
            time.sleep(_POWER_CYCLE_PAUSE_S)
            try:
                self._device.fire_and_forget(
                    MSG_LIGHT_SET_POWER,
                    struct.pack("<HI", POWER_ON, 0),
                )
            except OSError:
                pass
            time.sleep(_POWER_CYCLE_PAUSE_S)
            # Clear the committed layer to black on multizone devices.
            if self.is_multizone and self.zone_count:
                self._device.set_color(0, 0, 0, _CLEAR_KELVIN, duration_ms=0)

    def power_on(self, duration_ms: int = 0) -> None:
        """Turn the LIFX device on (type 117).

        Args:
            duration_ms: Transition duration in milliseconds.
        """
        if self._device is not None:
            self._device.set_power(on=True, duration_ms=duration_ms)

    def power_off(self, duration_ms: int = 0) -> None:
        """Turn the LIFX device off (type 117).

        Args:
            duration_ms: Transition duration in milliseconds.
        """
        if self._device is not None:
            self._device.set_power(on=False, duration_ms=duration_ms)

    def close(self) -> None:
        """Close the underlying UDP socket.

        Delegates to :meth:`on_close`.  Provided for backward
        compatibility with engine code that calls ``close()`` directly.
        """
        self.on_close()

    # --- LIFX-specific (not part of Emitter ABC) --------------------------
    # Protocol operations beyond the emitter rendering contract.

    @property
    def transport(self) -> Optional[LifxDevice]:
        """Access the underlying LifxDevice for protocol-specific queries.

        Returns ``None`` if the device has not been connected yet.

        Use this for operations outside the emitter rendering contract:

        - ``transport.query_zone_colors()`` — read back current colors
        - ``transport.query_light_state()`` — single-bulb state query
        - ``transport.clear_firmware_effect()`` — disable firmware effects
        - ``transport.label`` — raw device label
        - ``transport.group`` — device group name
        - ``transport.mac_str`` — MAC address string
        """
        return self._device

    def clear_firmware_effect(self) -> None:
        """Disable any firmware-level multizone effect on the device.

        Sends MSG_SET_MULTIZONE_EFFECT (type 508) with effect_type=OFF.
        Only meaningful on multizone devices.
        """
        if self._device is not None and self.is_multizone:
            self._device.clear_firmware_effect()

    def get_info(self) -> dict[str, Any]:
        """Return LIFX-specific status information.

        Includes IP, MAC address, and ack-pacing statistics alongside
        the standard fields.  Compatible with the Engine's
        :meth:`Controller.get_status` reporting interface.

        Returns:
            JSON-serializable dict with device identity and metadata.
        """
        info: dict[str, Any] = {
            "id": self.emitter_id,
            "label": self.label,
            "product": self.product_name,
            "zones": self.zone_count,
        }
        if self._device is not None:
            info["ip"] = self._device.ip
            info["mac"] = self._device.mac_str
            ack_stats: dict = self._device.ack_stats
            if ack_stats:
                info["ack_stats"] = ack_stats
        return info
