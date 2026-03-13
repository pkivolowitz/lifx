"""LIFX protocol emitter -- wraps LifxDevice for the Emitter interface.

All LIFX-specific concerns live here:
- Monochrome BT.709 luma conversion
- Extended multizone protocol dispatch
- Firmware committed-state clearing
- Power on / off via LIFX type 117

The Engine never touches LIFX protocol details -- it calls
:meth:`send_zones` and :meth:`send_color`, and this driver translates.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

from typing import Any, Optional

from effects import HSBK, KELVIN_DEFAULT, hsbk_to_luminance
from emitters import Emitter, EmitterInfo
from transport import LifxDevice


class LifxEmitter(Emitter):
    """Emitter that drives a single LIFX device via UDP.

    Wraps an existing :class:`LifxDevice` (already queried via
    :meth:`LifxDevice.query_all`).  The underlying device is accessible
    via the :attr:`transport` property for LIFX-specific operations
    that fall outside the emitter rendering contract (zone readback,
    firmware effect clearing, etc.).

    Args:
        device: A :class:`LifxDevice` with metadata already populated.
    """

    def __init__(self, device: LifxDevice) -> None:
        """Initialize with an existing LifxDevice.

        Args:
            device: The LIFX device to wrap.  Must have been queried
                    via :meth:`LifxDevice.query_all` so that
                    ``zone_count``, ``product``, etc. are populated.
        """
        self._device: LifxDevice = device

    # --- Emitter properties ---

    @property
    def zone_count(self) -> Optional[int]:
        """Number of addressable zones on the LIFX device."""
        return self._device.zone_count

    @property
    def is_multizone(self) -> bool:
        """Whether this device is a multizone strip or beam."""
        return bool(self._device.is_multizone)

    @property
    def emitter_id(self) -> str:
        """IP address of the LIFX device."""
        return self._device.ip

    @property
    def label(self) -> str:
        """Device label (e.g., 'Porch String Lights')."""
        return self._device.label or self._device.ip

    @property
    def product_name(self) -> str:
        """Product description (e.g., 'LIFX Z')."""
        return self._device.product_name or "Unknown LIFX"

    # --- Frame dispatch ---

    def send_zones(self, colors: list[HSBK], duration_ms: int = 0,
                   rapid: bool = True) -> None:
        """Send a multizone frame via LIFX extended protocol (type 510).

        Args:
            colors:      One HSBK tuple per zone.
            duration_ms: Firmware transition duration.
            rapid:       Fire-and-forget if ``True``.
        """
        self._device.set_zones(colors, duration_ms=duration_ms, rapid=rapid)

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
            duration_ms: Firmware transition duration.
        """
        if self._device.is_polychrome is False:
            # Monochrome device: convert to perceptual luminance.
            hue, sat, bri, kelvin = hsbk_to_luminance(hue, sat, bri, kelvin)
        self._device.set_color(hue, sat, bri, kelvin,
                               duration_ms=duration_ms)

    # --- Lifecycle ---

    def prepare_for_rendering(self) -> None:
        """Clear the LIFX firmware committed state to black.

        The extended multizone protocol (type 510) writes to a temporary
        overlay.  If a UDP frame is lost, the firmware briefly reveals the
        committed layer underneath.  Setting it to black makes those
        glitches invisible.
        """
        if self.is_multizone and self.zone_count:
            self._device.set_color(0, 0, 0, KELVIN_DEFAULT, duration_ms=0)

    def power_on(self, duration_ms: int = 0) -> None:
        """Turn the LIFX device on (type 117)."""
        self._device.set_power(on=True, duration_ms=duration_ms)

    def power_off(self, duration_ms: int = 0) -> None:
        """Turn the LIFX device off (type 117)."""
        self._device.set_power(on=False, duration_ms=duration_ms)

    def close(self) -> None:
        """Close the underlying UDP socket."""
        self._device.close()

    # --- LIFX-specific (not part of Emitter ABC) ---

    @property
    def transport(self) -> LifxDevice:
        """Access the underlying LifxDevice for protocol-specific queries.

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
        """Disable any firmware-level multizone effect on the device."""
        if self.is_multizone:
            self._device.clear_firmware_effect()

    def get_info(self) -> EmitterInfo:
        """Return LIFX-specific status information.

        Includes IP and MAC address alongside the standard fields.
        """
        return {
            "id": self.emitter_id,
            "ip": self._device.ip,
            "mac": self._device.mac_str,
            "label": self.label,
            "product": self.product_name,
            "zones": self.zone_count,
        }
