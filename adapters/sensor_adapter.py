"""SensorAdapter — abstract base class for sensor transport adapters.

All transport adapters (BLE, Zigbee, Vivint, rtl_433, etc.) follow the
same lifecycle:

    1. Receive data from transport (MQTT, polling, subprocess, etc.)
    2. Normalize values to floats (0.0–1.0 for booleans/battery, raw for
       temperature/humidity)
    3. Register signal metadata with transport identifier
    4. Write signals to the SignalBus

This base class formalizes that pattern, providing a common ``_write_signal``
helper and enforcing the ``start``/``stop`` lifecycle.

Example::

    class Rtl433Adapter(SensorAdapter):
        TRANSPORT = "433mhz"

        def start(self) -> None:
            self._proc = subprocess.Popen(["rtl_433", "-F", "json"], ...)

        def stop(self) -> None:
            self._proc.terminate()

        def _on_event(self, data: dict) -> None:
            self._write_signal(
                name=f"{data['model']}:temperature",
                value=data["temperature_C"],
                source_name=data["model"],
                description=f"433 MHz {data['model']} temperature",
            )
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import logging
from typing import Any, Optional

from .adapter_base import AdapterBase
from media import SignalMeta

logger: logging.Logger = logging.getLogger("glowup.sensor_adapter")


class SensorAdapter(AdapterBase):
    """Abstract base class for sensor transport adapters.

    Extends :class:`~adapter_base.AdapterBase` with a signal bus
    reference and the ``_write_signal`` helper.  Subclasses implement
    :meth:`start` and :meth:`stop` (inherited as abstract from
    ``AdapterBase``) for their specific transport and call
    :meth:`_write_signal` to publish normalized data to the bus.

    Args:
        bus: The shared :class:`~media.SignalBus`.
    """

    # Subclasses must set this to their transport identifier
    # (e.g., "ble", "zigbee", "vivint", "433mhz").
    TRANSPORT: str = ""

    def __init__(self, bus: Any) -> None:
        """Initialize the adapter with a signal bus reference.

        Args:
            bus: SignalBus instance for signal writes.
        """
        super().__init__()
        self._bus: Any = bus

    def _write_signal(
        self,
        name: str,
        value: float,
        source_name: str,
        description: str = "",
    ) -> None:
        """Register metadata and write a signal value to the bus.

        This is the standard write path for all adapters.  It ensures
        metadata (including the transport identifier) is registered
        before the first write, and the value is atomically published.

        Args:
            name:        Signal name (``{device}:{property}`` format).
            value:       Normalized float value.
            source_name: Human-readable source identifier.
            description: Optional description for the signal metadata.
        """
        if hasattr(self._bus, "register"):
            self._bus.register(name, SignalMeta(
                signal_type="scalar",
                description=description or f"{self.TRANSPORT} {source_name}",
                source_name=source_name,
                transport=self.TRANSPORT,
            ))
        self._bus.write(name, value)
