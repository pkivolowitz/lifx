"""Plug manager — centralized management of Zigbee smart plugs.

Parallel to :class:`device_manager.DeviceManager` for LIFX devices.
Plugs live on broker-2's zigbee_service (HTTP :8422) and are
controlled by POST to ``/devices/{name}/state``; this manager wraps
the per-plug :class:`emitters.zigbee_plug.ZigbeePlugEmitter` with a
label-keyed lookup surface matching the LIFX side.

Configuration
-------------

Plugs are declared in ``server.json`` under the ``plugs`` section,
with transport config shared under the existing ``zigbee`` section
(the dashboard proxy already reads broker/port from there, so keeping
it in one place avoids drift)::

    {
      "zigbee": {
        "broker":    "<broker-2 host or IP>",
        "http_port": 8422
      },
      "plugs": {
        "devices": {
          "LRTV":     {"ieee": "0x4ce175525c6b0000", "room": "Living Room"},
          "MBTV":     {"ieee": "0x4ce17552545b0000", "room": "Main Bedroom"},
          "BYIR":     {"ieee": "0x4ce1755254980000", "room": "Upstairs"},
          "ML_Power": {"ieee": "0x4ce17552549a0000", "room": "ML server"}
        }
      }
    }

Identity
--------

The dictionary key (e.g., ``"LRTV"``) is the Z2M friendly name and the
stable identifier used throughout the hub.  The IEEE address is
stored as secondary metadata — useful for cross-referencing and
lineage when a device is renamed at Z2M, but not used as a lookup
key.  Unknown keys beyond ``ieee``/``room`` are preserved in the
metadata dict so operators can annotate plugs without this manager
needing to know the schema.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "0.1"

import logging
import threading
from typing import Any, Optional

from emitters.zigbee_plug import (
    PlugCommandError,
    ZigbeePlugEmitter,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default broker host when ``config["zigbee"]["broker"]`` is absent.
# ``"localhost"`` rarely does the right thing for a hub-side service
# (broker-2 is not on the hub) so this is intentionally a bad default
# that fails fast during HTTP calls rather than silently routing to
# the wrong mosquitto.  Real deployments set broker explicitly.
_DEFAULT_BROKER: str = "localhost"

# Default HTTP port — matches GLZ_HTTP_PORT in zigbee_service/service.py.
_DEFAULT_HTTP_PORT: int = 8422

# Module logger.
logger: logging.Logger = logging.getLogger("glowup.plug_manager")


class PlugManager:
    """Orchestrate Zigbee smart plugs loaded from the server configuration.

    Thread-safe: a single lock protects the plug and metadata dicts.
    Emitter I/O happens outside the lock so long HTTP round-trips do
    not block unrelated lookups.

    Attributes:
        No public attributes.  All state is accessed through methods.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize from a parsed ``server.json``-shaped config dict.

        Builds one :class:`ZigbeePlugEmitter` per entry in
        ``config["plugs"]["devices"]``.  Missing or empty sections
        produce an empty manager — valid, since a hub with no plugs
        configured should not fail to start.

        Args:
            config: Full server configuration (typically loaded from
                    ``/etc/glowup/server.json``).  Reads
                    ``config["zigbee"]`` for transport and
                    ``config["plugs"]["devices"]`` for the manifest.
        """
        self._lock: threading.Lock = threading.Lock()
        self._plugs: dict[str, ZigbeePlugEmitter] = {}
        self._metadata: dict[str, dict[str, Any]] = {}

        zigbee_cfg: dict[str, Any] = config.get("zigbee", {}) or {}
        plugs_cfg: dict[str, Any] = config.get("plugs", {}) or {}

        broker_host: str = str(zigbee_cfg.get("broker", _DEFAULT_BROKER))
        http_port: int = int(zigbee_cfg.get("http_port", _DEFAULT_HTTP_PORT))

        devices: dict[str, Any] = plugs_cfg.get("devices", {}) or {}
        for name, meta in devices.items():
            if not isinstance(name, str) or not name:
                logger.warning(
                    "Skipping plug with non-string/empty name: %r", name)
                continue
            metadata: dict[str, Any] = dict(meta) if isinstance(meta, dict) \
                else {}
            try:
                emitter: ZigbeePlugEmitter = ZigbeePlugEmitter.from_plug(
                    name=name,
                    broker_host=broker_host,
                    http_port=http_port,
                )
                emitter.on_configure(config)
            except ValueError as exc:
                # Malformed broker config — surface clearly and skip
                # this plug rather than aborting the whole hub.
                logger.error(
                    "Failed to configure plug '%s': %s", name, exc)
                continue
            self._plugs[name] = emitter
            self._metadata[name] = metadata
            logger.info(
                "Configured plug: %s (ieee=%s, room=%s)",
                name,
                metadata.get("ieee", "?"),
                metadata.get("room", "?"),
            )

        logger.info(
            "PlugManager ready: %d plugs, broker=%s:%d",
            len(self._plugs), broker_host, http_port,
        )

    # --- Lookup ------------------------------------------------------------

    def has_plug(self, label: str) -> bool:
        """Return True if *label* is a known plug friendly name.

        Args:
            label: Z2M friendly name (e.g., ``"LRTV"``).

        Returns:
            ``True`` if the plug was declared in config and configured
            successfully.
        """
        with self._lock:
            return label in self._plugs

    def get_plug(self, label: str) -> Optional[ZigbeePlugEmitter]:
        """Return the emitter for *label*, or ``None`` if not known.

        Args:
            label: Z2M friendly name.

        Returns:
            The :class:`ZigbeePlugEmitter` or ``None``.
        """
        with self._lock:
            return self._plugs.get(label)

    def list_labels(self) -> list[str]:
        """Return the sorted list of known plug labels."""
        with self._lock:
            return sorted(self._plugs.keys())

    def get_metadata(self, label: str) -> dict[str, Any]:
        """Return a copy of the metadata dict for *label*.

        Args:
            label: Z2M friendly name.

        Returns:
            A shallow copy of the metadata (``ieee``, ``room``, any
            operator-added keys).  Empty dict if *label* is unknown.
        """
        with self._lock:
            return dict(self._metadata.get(label, {}))

    # --- Control -----------------------------------------------------------

    def power_on(self, label: str) -> None:
        """Turn the named plug on.

        Args:
            label: Z2M friendly name.

        Raises:
            KeyError:           If *label* is not a known plug.
            PlugCommandError:   On any HTTP, JSON, or echo-timeout
                                failure propagated from the emitter.
        """
        emitter: ZigbeePlugEmitter = self._require_plug(label)
        emitter.power_on()

    def power_off(self, label: str) -> None:
        """Turn the named plug off.

        Args:
            label: Z2M friendly name.

        Raises:
            KeyError, PlugCommandError: As :meth:`power_on`.
        """
        emitter: ZigbeePlugEmitter = self._require_plug(label)
        emitter.power_off()

    def set_power(self, label: str, on: bool) -> None:
        """Command the plug to the requested state.

        Args:
            label: Z2M friendly name.
            on:    ``True`` for ON, ``False`` for OFF.

        Raises:
            KeyError, PlugCommandError: As :meth:`power_on`.
        """
        emitter: ZigbeePlugEmitter = self._require_plug(label)
        emitter.set_power(on=on)

    # --- Introspection -----------------------------------------------------

    def query_state(self, label: str) -> dict[str, Any]:
        """Fetch live state for a single plug from broker-2.

        Issues an HTTP GET.  Prefer :meth:`get_status` for dashboard
        refreshes that should not block on a slow broker.

        Args:
            label: Z2M friendly name.

        Returns:
            The JSON body from ``/devices/{label}``.

        Raises:
            KeyError, PlugCommandError: As :meth:`power_on`.
        """
        emitter: ZigbeePlugEmitter = self._require_plug(label)
        return emitter.query_state()

    def get_status(self) -> dict[str, Any]:
        """Return a cached snapshot of every plug's last-known state.

        No HTTP calls — safe for frequent polling.  ``last_state`` will
        be ``None`` for a plug that has not been commanded or queried
        since the process started.

        Returns:
            Dict with a per-label summary and a count.
        """
        with self._lock:
            snapshot: list[tuple[str, ZigbeePlugEmitter, dict[str, Any]]] = [
                (name, self._plugs[name], dict(self._metadata.get(name, {})))
                for name in sorted(self._plugs.keys())
            ]
        return {
            "plugs": {
                name: {
                    "label": name,
                    "last_state": emitter.last_state,
                    "metadata": metadata,
                }
                for name, emitter, metadata in snapshot
            },
            "count": len(snapshot),
        }

    def refresh_all(self) -> dict[str, dict[str, Any]]:
        """Query broker-2 for live state on every plug.

        Blocking: sequential HTTP calls, bounded by the emitter's
        per-call timeout.  Intended for a periodic background refresh
        or an explicit ``/api/plugs/refresh`` endpoint, not per-request
        dashboard polling.

        Returns:
            Dict mapping each plug label to either the live state body
            or ``{"error": "..."}`` for plugs that could not be
            reached.  Unreachable plugs are logged but do not abort
            the refresh.
        """
        with self._lock:
            snapshot: list[tuple[str, ZigbeePlugEmitter]] = list(
                self._plugs.items())

        result: dict[str, dict[str, Any]] = {}
        for name, emitter in snapshot:
            try:
                result[name] = emitter.query_state()
            except PlugCommandError as exc:
                logger.warning(
                    "Live-state refresh failed for plug '%s': %s",
                    name, exc,
                )
                result[name] = {"error": str(exc)}
        return result

    # --- Private helpers ---------------------------------------------------

    def _require_plug(self, label: str) -> ZigbeePlugEmitter:
        """Return the emitter for *label* or raise :class:`KeyError`.

        Args:
            label: Z2M friendly name.

        Returns:
            The :class:`ZigbeePlugEmitter` for *label*.

        Raises:
            KeyError: If *label* is not a known plug.  The message
                      includes the sorted list of known labels to
                      make typos obvious in logs.
        """
        with self._lock:
            emitter: Optional[ZigbeePlugEmitter] = self._plugs.get(label)
            if emitter is None:
                known: str = ", ".join(sorted(self._plugs.keys())) or "(none)"
                raise KeyError(
                    f"Unknown plug '{label}'. Known plugs: {known}")
            return emitter
