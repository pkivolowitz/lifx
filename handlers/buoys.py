"""Hub-side HTTP handlers for the buoy dashboards.

Three routes:

- ``GET /buoys/<station_id>``       static history page (chart cards)
- ``GET /api/buoys/current``        latest obs for every station the
                                    BuoyBuffer has heard from
- ``GET /api/buoys/history/<sid>``  time-bucketed series from postgres
                                    via :class:`infrastructure.
                                    buoy_logger.BuoyLogger`

Live current state is fetched from the in-memory
:class:`infrastructure.buoy_buffer.BuoyBuffer`; history is queried
from postgres.  Two parallel MQTT subscribers, one for each, behind
the same ``glowup/maritime/buoy/+`` topic pattern.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse, parse_qs


logger: logging.Logger = logging.getLogger("glowup.handlers.buoys")


# Station id is fairly free-form in NDBC's namespace (alphanumeric,
# 5-character station ids dominant — e.g. 42012, DPIA1, BURL1).
# Validate at the path-tail boundary so we don't pass arbitrary
# attacker input straight into a SQL parameter.  The DB layer is
# parameter-bound so SQL injection would already be blocked, but
# the explicit gate is a cheap second line + lets us 400 cleanly
# before touching the database.
_STATION_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9]{1,16}$")


class BuoysHandlerMixin:
    """Mixin attached to GlowUpRequestHandler.  All routes are GET."""

    # -- /buoys/<station_id> -------------------------------------------------

    def _handle_get_buoys_page(self) -> None:
        """GET /buoys/<station_id> — serve the history dashboard HTML.

        The same static HTML is used for every station — the page
        reads the trailing path segment in JS at load time and
        queries the appropriate /api/buoys/history endpoint.
        """
        static_dir: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "static",
        )
        path: str = os.path.join(static_dir, "buoys.html")
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
            self._send_json(404, {"error": "buoys.html not found"})

    # -- /api/buoys/current --------------------------------------------------

    def _handle_get_api_buoys_current(self) -> None:
        """GET /api/buoys/current — latest obs for every known station."""
        bb: Any = getattr(self, "buoy_buffer", None)
        if bb is None:
            self._send_json(200, {"stats": {"n_stations": 0,
                                            "msg_count": 0,
                                            "last_msg_ts": None},
                                  "stations": []})
            return
        self._send_json(200, {
            "stats":    bb.stats(),
            "stations": bb.stations(),
        })

    # -- /api/buoys/history/<station_id> ------------------------------------

    def _handle_get_api_buoys_history(self) -> None:
        """GET /api/buoys/history/<station_id>?hours=N&resolution=S

        Query parameters
        ----------------
        hours       window size, default 24, max 90 * 24 (matches
                    the logger's 90-day retention).
        resolution  bucket size in seconds.  ``0`` (default) returns
                    raw rows; > 0 averages.  Caller is expected to
                    pick a resolution that keeps the chart cardinal-
                    ity sane (24h × 60s = 1440 raw rows is fine; 30d
                    × 60s = 43200 is not — use 600s or 3600s for
                    longer windows).
        """
        bl: Any = getattr(self, "buoy_logger", None)
        parsed: Any = urlparse(self.path)
        path_parts: list[str] = [p for p in parsed.path.split("/") if p]
        # Routed as ("api", "buoys", "history", "*") — path_parts[3] is
        # the station-id capture.
        if len(path_parts) < 4:
            self._send_json(400, {"error": "missing station id"})
            return
        sid: str = path_parts[3]
        if not _STATION_ID_RE.match(sid):
            self._send_json(400, {"error": "invalid station id"})
            return
        if bl is None:
            self._send_json(200, {"station_id": sid,
                                  "hours": 0,
                                  "resolution_s": 0,
                                  "rows": []})
            return
        qs: dict[str, list[str]] = parse_qs(parsed.query)
        # Bound hours to the logger's retention window — anything
        # beyond returns no rows but we'd rather 400-ish-fast than
        # let an operator query a meaningless range and wonder why
        # the chart is empty.
        try:
            hours: float = float(qs.get("hours", ["24"])[0])
        except ValueError:
            self._send_json(400, {"error": "hours must be numeric"})
            return
        if hours <= 0 or hours > 90 * 24:
            self._send_json(400, {"error": "hours out of range (0, 90d]"})
            return
        try:
            resolution_s: int = int(qs.get("resolution", ["0"])[0])
        except ValueError:
            self._send_json(400, {"error": "resolution must be integer seconds"})
            return
        if resolution_s < 0 or resolution_s > 24 * 3600:
            self._send_json(400, {"error": "resolution out of range"})
            return
        rows: list[dict[str, Any]] = bl.history(
            station_id=sid,
            hours=hours,
            resolution_s=resolution_s,
        )
        self._send_json(200, {
            "station_id":   sid,
            "hours":        hours,
            "resolution_s": resolution_s,
            "rows":         rows,
        })
