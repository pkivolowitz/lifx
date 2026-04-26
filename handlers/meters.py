"""Hub-side HTTP handlers for the /meters dashboard.

Exposes the static page at ``/meters`` and the JSON feed at
``/api/meters/latest`` that powers the live dashboard render.

The latest-reading API returns the snapshot pre-split into ``mine``
vs ``others`` so the page can render two sections without having to
fetch the owned-meters config separately.  Owned metadata
(utility, account, type label, class) is joined in server-side from
``/etc/glowup/meters_owned.json`` — same file the
:mod:`infrastructure.meter_logger` already consults for the
``ours BOOLEAN`` flag.

Civic motivation lives with the project memory and the meter logger
docstring; this module just renders.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0.0"

import json
import logging
import os
from typing import Any, Optional


logger: logging.Logger = logging.getLogger("glowup.handlers.meters")


# Path to the owned-meters config — must match the path used by
# infrastructure.meter_logger.DEFAULT_OWNED_PATH.  Hand-maintained
# by the operator; not in the repo.  Schema documented inline in
# meter_logger.py.
_OWNED_PATH: str = "/etc/glowup/meters_owned.json"


def _load_owned_metadata(path: str = _OWNED_PATH) -> dict[str, dict[str, Any]]:
    """Read the owned-meters config and return a meter_id → metadata map.

    Returns an empty dict on any failure (logged) so that the
    dashboard degrades to "every reading shown as Other" rather than
    crashing.  Same defensive philosophy as
    :func:`infrastructure.meter_logger.load_owned_meter_ids`.

    Args:
        path: Filesystem path to the owned-meters JSON.

    Returns:
        Dict mapping ``meter_id`` (str) to the full entry dict
        (``type``, ``class``, ``utility``, ``account``, etc.).
    """
    try:
        with open(path, "r") as f:
            doc: dict[str, Any] = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "owned-meters config %s unreadable: %s — dashboard "
            "will show every meter as Other",
            path, exc,
        )
        return {}

    owned_raw: Any = doc.get("owned")
    if not isinstance(owned_raw, list):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for entry in owned_raw:
        if not isinstance(entry, dict):
            continue
        mid: Any = entry.get("meter_id")
        if isinstance(mid, str) and mid:
            out[mid] = entry
    return out


class MetersHandlerMixin:
    """Mixin attached to GlowUpRequestHandler.  All routes are GET."""

    # -- /meters page --------------------------------------------------------

    def _handle_get_meters_page(self) -> None:
        """GET /meters — serve the static meters dashboard HTML."""
        static_dir: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "static",
        )
        path: str = os.path.join(static_dir, "meters.html")
        try:
            with open(path, "rb") as f:
                content: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            # Same no-store rationale as /thermal — the dashboard
            # HTML/JS evolves and stale cache silently hides fixes.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json(404, {"error": "meters.html not found"})

    # -- /api/meters/latest --------------------------------------------------

    def _handle_get_meters_latest(self) -> None:
        """GET /api/meters/latest — fleet snapshot, split by ownership.

        Returns::

            {
              "mine":   {"<meter_id>": {<reading...>, "owned_meta": {...}}, ...},
              "others": {"<meter_id>": {<reading...>}, ...}
            }

        ``owned_meta`` is the full entry from meters_owned.json (type,
        class, utility, account) — joined here so the page does not
        have to fetch the config separately.  ``ours`` boolean on
        each reading still wins for the split decision; the meta
        is just enrichment for rendering.
        """
        ml: Any = getattr(self, "meter_logger", None)
        if ml is None:
            self._send_json(200, {"mine": {}, "others": {}})
            return

        latest: dict[str, dict[str, Any]] = ml.latest()
        owned_meta: dict[str, dict[str, Any]] = _load_owned_metadata()

        mine: dict[str, dict[str, Any]] = {}
        others: dict[str, dict[str, Any]] = {}
        for mid, reading in latest.items():
            if reading.get("ours"):
                enriched: dict[str, Any] = dict(reading)
                meta: Optional[dict[str, Any]] = owned_meta.get(mid)
                if meta is not None:
                    enriched["owned_meta"] = meta
                mine[mid] = enriched
            else:
                others[mid] = reading

        self._send_json(200, {"mine": mine, "others": others})
