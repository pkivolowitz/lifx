"""Plug control handlers — hub API surface for Zigbee smart plugs.

Mirrors the shape of :class:`handlers.device.DeviceHandlerMixin` for
LIFX devices.  Plug transport itself lives in
:mod:`emitters.zigbee_plug`; orchestration in :class:`plug_manager.PlugManager`.

Endpoints::

    GET  /api/plugs                         List plugs with cached state
    POST /api/plugs/{label}/power           Turn plug on/off
    POST /api/plugs/refresh                 Bulk live-state query to broker-2

``GET /api/plugs`` is cache-only — it does not round-trip to broker-2
and therefore survives a broker-2 outage.  ``POST /api/plugs/refresh``
is the explicit path for populating the cache from live state (e.g.,
at dashboard load or after a broker-2 restart).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "0.1"

import logging
from typing import Any, Optional

from emitters.zigbee_plug import PlugCommandError

logger: logging.Logger = logging.getLogger("glowup.handlers.plug")


class PlugHandlerMixin:
    """Handler mixin for the ``/api/plugs`` endpoints.

    Expects :attr:`plug_manager` to be attached to the request handler
    class before the server starts (same convention as the LIFX
    :class:`DeviceManager`).  When :attr:`plug_manager` is ``None``
    (e.g., a hub without any Zigbee plugs configured), the handlers
    return well-formed empty responses rather than 500.
    """

    # --- GET handlers ------------------------------------------------------

    def _handle_get_plugs(self) -> None:
        """GET /api/plugs — list every configured plug with cached state.

        Returns the cached snapshot only — no HTTP to broker-2.  For
        fresh state, issue ``POST /api/plugs/refresh`` first or query
        a single plug's live state directly.

        Response shape::

            {
              "plugs": {
                "LRTV": {
                  "label": "LRTV",
                  "last_state": "ON",
                  "metadata": {"ieee": "0x...", "room": "Living Room"}
                },
                ...
              },
              "count": 4
            }
        """
        pm: Optional[Any] = getattr(self, "plug_manager", None)
        if pm is None:
            # Hub configured without plugs — return an empty manifest
            # so the dashboard can render "no plugs" uniformly.
            self._send_json(200, {"plugs": {}, "count": 0})
            return
        self._send_json(200, pm.get_status())

    # --- POST handlers -----------------------------------------------------

    def _handle_post_plug_power(self, label: str) -> None:
        """POST /api/plugs/{label}/power — turn a plug on/off.

        Request body mirrors the LIFX ``POST /api/devices/{ip}/power``
        contract so clients can call both through one body shape::

            {"on": true}

        Args:
            label: Plug friendly name (e.g., ``"LRTV"``).  URL-decoded
                   by the dispatch layer.
        """
        pm: Optional[Any] = getattr(self, "plug_manager", None)
        if pm is None:
            self._send_json(503, {"error": "Plug subsystem not configured"})
            return

        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        on: Any = body.get("on")
        if not isinstance(on, bool):
            self._send_json(400, {"error": "'on' must be a boolean"})
            return

        try:
            pm.set_power(label, on=on)
        except KeyError:
            self._send_json(404, {
                "error": f"Plug '{label}' not found",
                "known_plugs": pm.list_labels(),
            })
            return
        except PlugCommandError as exc:
            # Broker-2 unreachable, echo timeout, or device refused —
            # 502 Bad Gateway captures "upstream broker failed to
            # complete the request" more accurately than 500.
            logger.warning(
                "Plug power %s failed for '%s': %s",
                "on" if on else "off", label, exc,
            )
            self._send_json(502, {
                "error": str(exc),
                "label": label,
                "desired": "ON" if on else "OFF",
            })
            return

        logger.info(
            "API: power %s on plug %s", "on" if on else "off", label)
        plug: Any = pm.get_plug(label)
        self._send_json(200, {
            "label": label,
            "desired": "ON" if on else "OFF",
            "current_state": plug.last_state if plug is not None else None,
        })

    def _handle_post_plugs_refresh(self) -> None:
        """POST /api/plugs/refresh — query broker-2 for live state on every plug.

        Blocking: issues one HTTP GET per plug, sequentially.  Intended
        for dashboard load and post-restart cache warm-up, not per-click
        polling.  Per-plug failures are reported in the response rather
        than aborting the whole refresh.

        Response::

            {
              "refreshed": {
                "LRTV":     {"state": "ON",  "power_w": 5.4, "online": true,  ...},
                "ML_Power": {"error": "HTTP POST to ... failed: ..."}
              },
              "count": 4
            }
        """
        pm: Optional[Any] = getattr(self, "plug_manager", None)
        if pm is None:
            self._send_json(200, {"refreshed": {}, "count": 0})
            return

        result: dict[str, Any] = pm.refresh_all()
        self._send_json(200, {"refreshed": result, "count": len(result)})
