"""Hub-side HTTP handlers for the /airwaves dashboard.

Exposes the static page at ``/airwaves`` and three JSON feeds that
power the live RF activity dashboard:

- ``GET /api/airwaves/feed?limit=N``         — newest-first packet feed
- ``GET /api/airwaves/protocols``            — per-rtl_433-model aggregate
- ``GET /api/airwaves/transmitters?limit=N`` — per-transmitter aggregate

Backed by an in-memory ring buffer (no persistence) maintained by
:class:`infrastructure.airwaves_buffer.AirwavesBuffer`, which
subscribes to ``glowup/sub_ghz/raw`` from
:mod:`meters.publisher`.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import logging
import os
from typing import Any
from urllib.parse import parse_qs, urlparse


logger: logging.Logger = logging.getLogger("glowup.handlers.airwaves")


# Defaults / caps for the feed APIs.  Caps are intentionally generous
# but not unbounded — the ring is bounded too, but a malformed
# ``?limit=`` should never crash the page.
_DEFAULT_FEED_LIMIT: int = 50
_MAX_FEED_LIMIT: int = 500
_DEFAULT_TX_LIMIT: int = 30
_MAX_TX_LIMIT: int = 200


def _parse_int_qs(query: str, key: str, default: int, cap: int) -> int:
    """Parse a single integer query-string param, clamped to [0, cap]."""
    qs: dict[str, list[str]] = parse_qs(query or "")
    vals: list[str] = qs.get(key, [])
    if not vals:
        return default
    try:
        n: int = int(vals[0])
    except ValueError:
        return default
    if n < 0:
        return 0
    return min(n, cap)


class AirwavesHandlerMixin:
    """Mixin attached to GlowUpRequestHandler.  All routes are GET."""

    # -- /airwaves page ------------------------------------------------------

    def _handle_get_airwaves_page(self) -> None:
        """GET /airwaves — serve the static airwaves dashboard HTML."""
        static_dir: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "static",
        )
        path: str = os.path.join(static_dir, "airwaves.html")
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
            self._send_json(404, {"error": "airwaves.html not found"})

    # -- /api/airwaves/feed --------------------------------------------------

    def _handle_get_airwaves_feed(self) -> None:
        """GET /api/airwaves/feed?limit=N — newest-first packet feed.

        Each entry: ``{received_ts, model, friendly, transmitter_id,
        freq_MHz, rssi, snr, raw}``.  ``raw`` is the unmodified rtl_433
        packet so the page can surface protocol-specific fields
        (button codes, channel numbers, weather temp/humidity, etc.)
        without server-side schema decisions.
        """
        ab: Any = getattr(self, "airwaves_buffer", None)
        if ab is None:
            self._send_json(200, {
                "stats": {"ring_len": 0, "ring_size": 0,
                          "protocols": 0, "transmitters": 0,
                          "last_packet_ts": None},
                "feed": [],
            })
            return
        query: str = urlparse(self.path).query
        limit: int = _parse_int_qs(
            query, "limit", _DEFAULT_FEED_LIMIT, _MAX_FEED_LIMIT,
        )
        self._send_json(200, {
            "stats": ab.stats(),
            "feed":  ab.recent(limit),
        })

    # -- /api/airwaves/protocols ---------------------------------------------

    def _handle_get_airwaves_protocols(self) -> None:
        """GET /api/airwaves/protocols — per-rtl_433-model aggregate.

        One entry per distinct ``model`` seen this process lifetime,
        with ``count``, ``first_seen``, ``last_seen``, ``friendly``.
        Sorted newest-active first.
        """
        ab: Any = getattr(self, "airwaves_buffer", None)
        if ab is None:
            self._send_json(200, {"protocols": []})
            return
        self._send_json(200, {"protocols": ab.by_protocol()})

    # -- /api/airwaves/transmitters ------------------------------------------

    def _handle_get_airwaves_transmitters(self) -> None:
        """GET /api/airwaves/transmitters?limit=N — most-chatty leaderboard.

        One entry per (model, transmitter_id) pair seen this process
        lifetime.  Sorted by count desc, then last_seen desc — the
        chattiest transmitters lead.  Capped by ``limit``.
        """
        ab: Any = getattr(self, "airwaves_buffer", None)
        if ab is None:
            self._send_json(200, {"transmitters": []})
            return
        query: str = urlparse(self.path).query
        limit: int = _parse_int_qs(
            query, "limit", _DEFAULT_TX_LIMIT, _MAX_TX_LIMIT,
        )
        self._send_json(200, {"transmitters": ab.by_transmitter(limit)})
