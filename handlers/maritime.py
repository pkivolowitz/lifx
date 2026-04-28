"""Hub-side HTTP handlers for the /maritime dashboard.

Exposes the static map at ``/maritime`` and three JSON feeds that
power the live vessel layer and the vessel-table side panel:

- ``GET /api/maritime/vessels``       — current state of every
                                        vessel we've heard from
                                        (newest-active first)
- ``GET /api/maritime/vessel/<mmsi>`` — full state + breadcrumb
                                        track for one vessel
- ``GET /api/maritime/config``        — operator-set reference point
                                        (for the table's distance
                                        column / range filter)

Backed by an in-memory per-vessel state map maintained by
:class:`infrastructure.maritime_buffer.MaritimeBuffer`, which
subscribes to ``glowup/maritime/ais`` from the AIS-catcher receiver.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.1"

import json
import logging
import os
from typing import Any
from urllib.parse import urlparse, unquote


logger: logging.Logger = logging.getLogger("glowup.handlers.maritime")

# Default zoom for the home view when the operator hasn't expressed
# one explicitly.  11 is the value the dashboard used historically
# (former DEFAULT_ZOOM constant in maritime.html) — close enough to
# show a metro-sized area without losing detail.  Operators can
# override per-site by adding a ``zoom`` field to maritime_reference
# in /etc/glowup/site.json.
HOME_DEFAULT_ZOOM: int = 11

# Placeholder marker the maritime page injects the home location
# before.  The page is otherwise served as-is, so we splice the
# script tag in just ahead of </head> — keeps the static file valid
# for standalone editing while letting the server stamp the
# operator's home into the document at serve time.
HOME_INJECT_BEFORE: str = "</head>"


class MaritimeHandlerMixin:
    """Mixin attached to GlowUpRequestHandler.  All routes are GET."""

    # -- /maritime page ------------------------------------------------------

    def _handle_get_maritime_page(self) -> None:
        """GET /maritime — serve the dashboard HTML with the operator's
        home location stamped into ``window.__GLOWUP_HOME__``.

        The repo's static maritime.html carries no hardcoded coords —
        the home/center point lives only in /etc/glowup/site.json
        under ``maritime_reference``.  At serve time we splice a tiny
        ``<script>`` tag in just before ``</head>`` so the page boots
        already centered (no async re-center flash) and the recenter
        button has a single source of truth shared with the initial
        view.  When no maritime_reference is configured, no script is
        injected and the page falls back to a generic CONUS view.
        """
        static_dir: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "static",
        )
        path: str = os.path.join(static_dir, "maritime.html")
        try:
            with open(path, "r", encoding="utf-8") as f:
                html: str = f.read()
        except FileNotFoundError:
            self._send_json(404, {"error": "maritime.html not found"})
            return

        # Resolve the operator's home from the maritime buffer's
        # already-coerced reference (same path /api/maritime/config
        # uses, so the JS sees identical data here and there).  Going
        # through the buffer rather than reading site.json directly
        # gives us the validated lat/lon range checks for free.
        mb: Any = getattr(self, "maritime_buffer", None)
        ref: Any = mb.reference if mb is not None else None
        if isinstance(ref, dict):
            try:
                lat: float = float(ref["lat"])
                lon: float = float(ref["lon"])
                zoom: int = int(ref.get("zoom", HOME_DEFAULT_ZOOM))
            except (KeyError, TypeError, ValueError):
                # Malformed reference: log and serve unstamped — the
                # JS-side fallback will land the user on a generic
                # CONUS view rather than crashing on a NaN center.
                logger.warning(
                    "maritime_reference unusable for home injection: %r", ref,
                )
            else:
                inject: str = (
                    "<script>window.__GLOWUP_HOME__ = "
                    + json.dumps({"lat": lat, "lon": lon, "zoom": zoom})
                    + ";</script>\n"
                )
                html = html.replace(
                    HOME_INJECT_BEFORE,
                    inject + HOME_INJECT_BEFORE,
                    1,
                )

        body: bytes = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- /api/maritime/vessels -----------------------------------------------

    def _handle_get_maritime_vessels(self) -> None:
        """GET /api/maritime/vessels — current vessel state, all known.

        Returns ``{stats, vessels: [...]}`` where each vessel includes
        last-known position, identifiers (mmsi, shipname, callsign),
        type, course/speed/heading, last_seen + last_position_ts
        timestamps, and a recent breadcrumb ``track`` (list of
        ``{ts, lat, lon}`` newest-last).  Vessels we've never heard
        a valid position from are omitted by default.
        """
        mb: Any = getattr(self, "maritime_buffer", None)
        if mb is None:
            self._send_json(200, {
                "stats":   {"n_vessels": 0, "n_with_position": 0,
                            "msg_count": 0, "first_msg_ts": None,
                            "last_msg_ts": None, "stale_after_s": 0},
                "vessels": [],
            })
            return
        self._send_json(200, {
            "stats":   mb.stats(),
            "vessels": mb.vessels(with_position_only=True),
        })

    # -- /api/maritime/vessel/<mmsi> -----------------------------------------

    def _handle_get_maritime_vessel(self, mmsi: str) -> None:
        """GET /api/maritime/vessel/<mmsi> — full state for one vessel.

        ``mmsi`` is captured by the route dispatcher (which passes
        path-pattern ``{name}`` segments as positional args; see
        server.py:1118-1123).  Validated as numeric here; 400 on
        non-numeric, 404 on unknown.  Same shape as one entry in
        ``vessels()``, with the full breadcrumb the buffer is
        currently retaining for that vessel.
        """
        mb: Any = getattr(self, "maritime_buffer", None)
        try:
            mmsi_i: int = int(unquote(mmsi))
        except ValueError:
            self._send_json(400, {"error": "mmsi must be numeric"})
            return
        if mb is None:
            self._send_json(404, {"error": "buffer unavailable"})
            return
        v: Any = mb.vessel(mmsi_i)
        if v is None:
            self._send_json(404, {"error": "unknown mmsi"})
            return
        self._send_json(200, v)

    # -- /api/maritime/config ------------------------------------------------

    def _handle_get_maritime_config(self) -> None:
        """GET /api/maritime/config — dashboard-side configuration.

        Currently exposes only the operator-set reference point —
        ``{reference: {lat, lon, postal_code, country}}`` or
        ``{reference: null}`` when the operator hasn't set
        ``maritime_reference`` in /etc/glowup/site.json.  The
        dashboard fetches this once at page load to label the
        distance column / disable the range filter when no
        reference is configured.
        """
        mb: Any = getattr(self, "maritime_buffer", None)
        ref: Any = mb.reference if mb is not None else None
        self._send_json(200, {"reference": ref})
