"""Hub-side HTTP handlers for the /roads dashboard.

Two endpoint families:

- ``GET /api/traffic/incidents`` — JSON feed of TomTom traffic
  incidents (accidents, road closures, slow traffic) within the
  configured home bbox.  Polled by the /roads page's incidents
  layer; cached server-side at 10 min TTL.

- ``GET /api/traffic/flow/{z}/{x}/{y}.png`` — proxy for TomTom
  flow tiles, rendered into the map by Leaflet's tileLayer.  The
  TomTom API key never reaches the browser; the tile cache lives
  on the hub.

Both routes are public read-only — same rationale as the rest of
the maritime/buoy/aircraft surfaces (curiosity data, no actuation,
no operator credentials).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0.0"

import logging
from typing import Any


logger: logging.Logger = logging.getLogger("glowup.handlers.traffic")

# Browser cache hint for flow tiles.  Matches the server-side TTL
# (300 s) — the browser refreshes a tile at the same cadence the
# server would re-fetch from TomTom, keeping the rendered map
# roughly in sync with the upstream feed.
FLOW_TILE_BROWSER_MAX_AGE_S: int = 300

# A 1×1 transparent PNG used when the tile fetch fails.  Sending
# something Leaflet can render keeps the layer from logging a
# broken-tile error and the map from showing a missing-tile X
# during transient TomTom outages.  Pre-encoded so we don't pay a
# PNG-build per failure.
_BLANK_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63000100000005000100"
    "0d0a2db40000000049454e44ae426082"
)


class TrafficHandlerMixin:
    """Mixin attached to GlowUpRequestHandler.  All routes are GET."""

    # -- /api/traffic/incidents ---------------------------------------------

    def _handle_get_traffic_incidents(self) -> None:
        """GET /api/traffic/incidents — current incidents in the home bbox.

        Shape: ``{incidents: [...], fetched_at: ISO-8601 | null,
        stale: bool}``.  ``stale`` is true when the buffer is
        serving a cached payload after a failed refresh — the
        dashboard surfaces this in the layer's status badge.
        """
        tb: Any = getattr(self, "traffic_buffer", None)
        if tb is None:
            self._send_json(200, {
                "incidents": [], "fetched_at": None, "stale": True,
            })
            return
        self._send_json(200, tb.get_incidents())

    # -- /api/traffic/flow/{z}/{x}/{y}.png ----------------------------------

    def _handle_get_traffic_flow_tile(
        self, z: str, x: str, y: str,
    ) -> None:
        """GET /api/traffic/flow/{z}/{x}/{y}.png — proxy + cache.

        Path captures arrive as strings (the dispatcher passes
        ``{name}`` segments as positional args; see server.py
        :class:`_Route`).  Validated as integers here; 400 on
        non-numeric, then on to the buffer.

        The trailing ``.png`` is part of ``y``'s capture — strip
        it before parsing.  Keeping the extension in the URL is
        what tells Leaflet's tileLayer to treat this as an image
        tile and matches TomTom's own URL style; doing so also
        means dumb caches (browsers, CDNs) see "PNG" in the URL
        and behave correctly.
        """
        # ``y`` arrives as e.g. "419.png" — strip extension.
        y_str: str = y[:-4] if y.endswith(".png") else y
        try:
            zi: int = int(z)
            xi: int = int(x)
            yi: int = int(y_str)
        except ValueError:
            self._send_json(400, {"error": "tile coords must be numeric"})
            return
        # Sanity range — Web Mercator zoom 0 is the whole world,
        # zoom 22 is sub-meter.  TomTom flow tiles are useful
        # roughly 6–18; reject obviously broken zooms early so a
        # bad client URL doesn't reach the upstream and burn a
        # transaction.
        if zi < 0 or zi > 22:
            self._send_json(400, {"error": "zoom out of range"})
            return

        tb: Any = getattr(self, "traffic_buffer", None)
        if tb is None or not tb.enabled:
            self._send_blank_tile()
            return
        data: Any = tb.get_flow_tile(zi, xi, yi)
        if not data:
            # Either no API key, or the upstream fetch failed and
            # we have no cache fallback.  Send a blank tile rather
            # than a 502 — keeps the map intact and lets Leaflet
            # render the basemap underneath without a broken-tile
            # placeholder.
            self._send_blank_tile()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Cache-Control",
            f"public, max-age={FLOW_TILE_BROWSER_MAX_AGE_S}",
        )
        self.end_headers()
        self.wfile.write(data)

    def _send_blank_tile(self) -> None:
        """Send a 1×1 transparent PNG with a short cache hint.

        The cache hint is intentionally short (30 s) — we don't
        want a transient TomTom outage to fill the browser cache
        with blank tiles for the full FLOW_TILE_BROWSER_MAX_AGE_S.
        """
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(_BLANK_PNG)))
        self.send_header("Cache-Control", "public, max-age=30")
        self.end_headers()
        self.wfile.write(_BLANK_PNG)
