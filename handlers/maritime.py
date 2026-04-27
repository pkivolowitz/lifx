"""Hub-side HTTP handlers for the /maritime dashboard.

Exposes the static map at ``/maritime`` and two JSON feeds that
power the live vessel layer:

- ``GET /api/maritime/vessels``       — current state of every
                                        vessel we've heard from
                                        (newest-active first)
- ``GET /api/maritime/vessel/<mmsi>`` — full state + breadcrumb
                                        track for one vessel

Backed by an in-memory per-vessel state map maintained by
:class:`infrastructure.maritime_buffer.MaritimeBuffer`, which
subscribes to ``glowup/maritime/ais`` from the AIS-catcher receiver.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import logging
import os
from typing import Any
from urllib.parse import urlparse


logger: logging.Logger = logging.getLogger("glowup.handlers.maritime")


class MaritimeHandlerMixin:
    """Mixin attached to GlowUpRequestHandler.  All routes are GET."""

    # -- /maritime page ------------------------------------------------------

    def _handle_get_maritime_page(self) -> None:
        """GET /maritime — serve the static maritime dashboard HTML."""
        static_dir: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "static",
        )
        path: str = os.path.join(static_dir, "maritime.html")
        try:
            with open(path, "rb") as f:
                content: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json(404, {"error": "maritime.html not found"})

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

    def _handle_get_maritime_vessel(self) -> None:
        """GET /api/maritime/vessel/<mmsi> — full state for one vessel.

        Path-tail digit is the MMSI.  Returns 404 if unknown, 400 if
        non-numeric.  Same shape as one entry in ``vessels()``, with
        the full breadcrumb the buffer is currently retaining for
        that vessel.
        """
        mb: Any = getattr(self, "maritime_buffer", None)
        path_parts: list[str] = [
            p for p in urlparse(self.path).path.split("/") if p
        ]
        # Routed as ("api", "maritime", "vessel", "*") — path_parts[3] is
        # the MMSI capture.
        if len(path_parts) < 4:
            self._send_json(400, {"error": "missing mmsi"})
            return
        try:
            mmsi: int = int(path_parts[3])
        except ValueError:
            self._send_json(400, {"error": "mmsi must be numeric"})
            return
        if mb is None:
            self._send_json(404, {"error": "buffer unavailable"})
            return
        v: Any = mb.vessel(mmsi)
        if v is None:
            self._send_json(404, {"error": "unknown mmsi"})
            return
        self._send_json(200, v)
