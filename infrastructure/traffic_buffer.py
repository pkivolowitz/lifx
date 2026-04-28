"""On-demand caching proxy for TomTom Traffic data.

The TomTom free tier allows 2,500 transactions/day across the
account.  This module fronts both endpoints we use — incidents
(GeoJSON for accident/closure pins) and flow tiles (raster
congestion overlay) — with a TTL cache so a long-lived /roads
session stays comfortably inside the budget.

Design choices
--------------

- **On-demand, not background polling.**  Refreshes happen when a
  request arrives and the cache is stale.  When nobody is viewing
  the page, no API calls fire at all.  A 24-hour idle session
  costs zero transactions instead of hundreds.

- **Stateful, not stateless.**  The cache is held on this object
  rather than module-level so tests can instantiate a fresh buffer
  per case without dirtying global state.  The handler holds one
  long-lived instance attached to the request handler class.

- **One key, two endpoints.**  Both flow tiles and incidents come
  from the same TomTom Maps & Traffic key, so a single secret in
  ``/etc/glowup/secrets.json :: tomtom_api_key`` covers both.

- **Bbox computed from home.**  Incidents are fetched within a
  square bounding box centered on ``maritime_reference.lat/lon``
  with a configurable half-side in km.  A fixed bbox (rather than
  the user's current viewport) keeps cache hit rate high and the
  daily budget predictable: every viewer of /roads sees the same
  cached payload.

- **Tile cache is keyed by (z, x, y).**  Every tile is cached
  independently with the same TTL.  Eviction is LRU once the
  total tile count reaches the cap.  Memory bound is small —
  TomTom 256×256 flow tiles average ~20 KB, so the default 200-
  tile cap is ~4 MB.

Budget arithmetic
-----------------

At the chosen defaults (incidents TTL 600 s / flow TTL 300 s,
~9 tiles per Mobile-metro viewport):

- Incidents: at most 1 fetch per 600 s while the page is open →
  144 fetches/day worst case.

- Flow tiles: at most 9 fetches per 300 s while the user pans the
  metro view → 2,592 fetches/day worst case for tiles alone.

Worst case is over budget; typical case (page opened a few times
a day, user mostly stationary on the bay view) is well under
budget.  The TTL knobs are exposed so an operator can tighten if
TomTom flags overage.

License / attribution: TomTom requires the "© TomTom" attribution
on any rendered tile.  Set on the Leaflet tileLayer in
maritime.html, not here.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0.0"

import json
import logging
import math
import threading
import time
from collections import OrderedDict
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


logger: logging.Logger = logging.getLogger("glowup.traffic_buffer")


# ─── Tunables ─────────────────────────────────────────────────────────

# Cache TTLs.  Picked to fit a free-tier 2500/day budget at typical
# /roads usage; see module docstring for the arithmetic.  Operators
# who hit the rate limit can raise these to spread fetches further.
INCIDENTS_TTL_S: int = 600   # 10 min
FLOW_TILE_TTL_S: int = 300   # 5 min

# Bbox half-side around the home center, in km.  TomTom's
# incidents API rejects any bbox whose area is >= 10,000 km², so a
# half-side of 50 km (100×100 = 10,000) is exactly at the limit
# and gets bounced with HTTP 400 INVALID_REQUEST.  45 km gives a
# ~8,100 km² bbox — comfortable margin under the cap and still
# covers all of metro Mobile + the bay + the southern interstate
# stretch.  Operators with a different metro footprint can
# override via the ``radius_km`` constructor argument; setting
# anything that produces a bbox >= 10,000 km² will silently fail
# with no incidents (logged at WARNING).
DEFAULT_RADIUS_KM: float = 45.0

# LRU eviction threshold for the flow-tile cache.  At ~20 KB per
# 256×256 PNG, 200 tiles is ~4 MB — small enough to keep in-process
# without worrying, large enough to cover several zoom levels of a
# metro-sized viewport.
FLOW_TILE_CACHE_CAP: int = 200

# TomTom endpoints.  Pinned to v4 (flow tiles) / v5 (incidents) —
# the URL scheme is stable across these majors, but bumping if the
# response shape ever changes is safer than silent breakage.
TT_FLOW_TILE_URL: str = (
    "https://api.tomtom.com/traffic/map/4/tile/flow/relative/"
    "{z}/{x}/{y}.png"
)
TT_INCIDENTS_URL: str = (
    "https://api.tomtom.com/traffic/services/5/incidentDetails"
)

# Field selector for the incidents endpoint.  Asks for everything
# the dashboard needs to render a useful pin: position, category
# icon, severity, road number, free-text event description, and
# the start/end timestamps.  Dropping fields shrinks the payload
# but TomTom prices by transaction not bytes, so we ask for what
# we want once.  Casing is significant — ``startTime`` not
# ``starttime`` (the API rejects lowercase with 400 INVALID_REQUEST).
TT_INCIDENT_FIELDS: str = (
    "{incidents{type,geometry{type,coordinates},"
    "properties{iconCategory,magnitudeOfDelay,"
    "events{description,code},"
    "startTime,endTime,delay,length,roadNumbers}}}"
)

# Earth radius used for the lat/lon ↔ km conversion when computing
# the incident-query bbox.  Mean radius is fine for a 50-km bbox;
# the spheroidal correction would move the bbox edge by tens of
# meters at most.
EARTH_RADIUS_KM: float = 6371.0

# Network timeout for outbound TomTom calls.  Short enough that a
# stalled fetch doesn't pin a request thread for minutes; long
# enough that a momentary TomTom hiccup doesn't flap the cache.
HTTP_TIMEOUT_S: float = 10.0

# User-Agent for outbound calls.  TomTom doesn't currently require
# one but a named UA helps if they ever add tracing on the account.
HTTP_USER_AGENT: str = "glowup/lifx (TomTom traffic adapter)"


class TrafficBuffer:
    """Caching proxy for TomTom traffic incidents + flow tiles.

    Construct once at server startup with the API key and the home
    coordinates; attach to the request handler class.  Each call
    site uses one of the two public methods; both return cached
    data when fresh and trigger a TomTom fetch only when stale.

    Thread-safety: a single :class:`threading.Lock` serializes
    cache reads and writes.  Network fetches are issued under the
    lock to avoid duplicate concurrent requests for the same key
    — at this traffic volume the lost concurrency is invisible
    and the protection against API call duplication is more
    valuable than parallel fetches.
    """

    def __init__(
        self,
        api_key: Optional[str],
        center_lat: Optional[float] = None,
        center_lon: Optional[float] = None,
        radius_km: float = DEFAULT_RADIUS_KM,
        incidents_ttl_s: int = INCIDENTS_TTL_S,
        flow_tile_ttl_s: int = FLOW_TILE_TTL_S,
    ) -> None:
        """Initialize the buffer.

        Args:
            api_key:        TomTom Maps & Traffic API key, or None to
                            disable the buffer (every method returns
                            an empty/error response without dialling
                            out).  Lets the dashboard load without a
                            key configured — /roads just shows no
                            traffic data.
            center_lat:     Home latitude in WGS-84 decimal degrees.
                            Required when ``api_key`` is set.
            center_lon:     Home longitude.  Same.
            radius_km:      Half-side of the incidents bbox, in km.
            incidents_ttl_s: Incidents cache TTL.
            flow_tile_ttl_s: Flow-tile cache TTL.
        """
        self._api_key: Optional[str] = api_key
        self._center_lat: Optional[float] = center_lat
        self._center_lon: Optional[float] = center_lon
        self._radius_km: float = radius_km
        self._incidents_ttl_s: int = incidents_ttl_s
        self._flow_tile_ttl_s: int = flow_tile_ttl_s
        # Cache state.  ``_incidents_cache`` holds (timestamp, payload)
        # for the single bbox we query; ``_tile_cache`` is an OrderedDict
        # keyed by (z, x, y) for LRU eviction.
        self._incidents_cache: Optional[tuple[float, dict[str, Any]]] = None
        self._tile_cache: "OrderedDict[tuple[int, int, int], tuple[float, bytes]]" = (
            OrderedDict()
        )
        self._lock: threading.Lock = threading.Lock()

    # ── Public API ──────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """True iff the buffer can actually fetch from TomTom."""
        return bool(
            self._api_key and self._center_lat is not None
            and self._center_lon is not None,
        )

    def get_incidents(self) -> dict[str, Any]:
        """Return the current incidents payload, fetching if stale.

        Returns:
            A dict with ``incidents`` (list of incident features in
            the GeoJSON shape TomTom returns) and ``fetched_at``
            (ISO-8601 timestamp of the cache entry — the dashboard
            shows this so the operator knows the data's age).  When
            the buffer is disabled (no API key) or a fetch fails,
            returns an empty incident list with the most recent
            ``fetched_at`` we have, or epoch zero.

        Idempotent and safe to call frequently — the TTL keeps
        cache hits cheap and stale calls infrequent.
        """
        if not self.enabled:
            return {"incidents": [], "fetched_at": None, "stale": True}
        with self._lock:
            now: float = time.time()
            if (self._incidents_cache is not None
                    and (now - self._incidents_cache[0]) < self._incidents_ttl_s):
                ts, payload = self._incidents_cache
                return {
                    "incidents": payload.get("incidents", []),
                    "fetched_at": _iso(ts),
                    "stale": False,
                }
            # Stale or empty — fetch.
            payload = self._fetch_incidents()
            if payload is None:
                # Fetch failed; serve last good cache if we have one,
                # flagged stale.  Better than an empty layer.
                if self._incidents_cache is not None:
                    ts, last = self._incidents_cache
                    return {
                        "incidents": last.get("incidents", []),
                        "fetched_at": _iso(ts),
                        "stale": True,
                    }
                return {"incidents": [], "fetched_at": None, "stale": True}
            self._incidents_cache = (now, payload)
            return {
                "incidents": payload.get("incidents", []),
                "fetched_at": _iso(now),
                "stale": False,
            }

    def get_flow_tile(self, z: int, x: int, y: int) -> Optional[bytes]:
        """Return PNG bytes for a flow tile, fetching if stale.

        Args:
            z:  Web Mercator zoom level (0–22).
            x:  Tile X.
            y:  Tile Y.

        Returns:
            Raw PNG bytes, or None if the buffer is disabled or
            the fetch failed.  Callers should send 503 / a blank
            tile in that case rather than 500.
        """
        if not self._api_key:
            return None
        key: tuple[int, int, int] = (z, x, y)
        with self._lock:
            now: float = time.time()
            entry: Optional[tuple[float, bytes]] = self._tile_cache.get(key)
            if entry is not None and (now - entry[0]) < self._flow_tile_ttl_s:
                # LRU bump on hit.
                self._tile_cache.move_to_end(key)
                return entry[1]
            data: Optional[bytes] = self._fetch_flow_tile(z, x, y)
            if data is None:
                # Fetch failed; serve stale tile if we have one.
                if entry is not None:
                    return entry[1]
                return None
            self._tile_cache[key] = (now, data)
            self._tile_cache.move_to_end(key)
            self._evict_if_needed()
            return data

    # ── Internal: fetch ─────────────────────────────────────────────

    def _fetch_incidents(self) -> Optional[dict[str, Any]]:
        """Fetch and parse the incidents JSON.  None on failure."""
        bbox: str = self._bbox_str()
        # Empirical TomTom incidents API encoding rules (parity-
        # tested against curl, both 200/400 cases):
        #   - bbox       commas MUST stay raw — percent-encoded
        #                ``%2C`` is rejected with HTTP 400.
        #   - fields     braces and commas MUST be percent-encoded
        #                — Python's ``urlopen`` does not normalise
        #                raw ``{}`` and TomTom's gateway rejects
        #                them.
        # So we build the query string by hand with the right safe
        # set per parameter rather than relying on a single
        # ``urlencode`` call.
        qs: str = (
            "key=" + quote(self._api_key or "", safe="")
            + "&bbox=" + quote(bbox, safe=",")
            + "&fields=" + quote(TT_INCIDENT_FIELDS, safe="")
            + "&language=en-US"
        )
        url: str = TT_INCIDENTS_URL + "?" + qs
        try:
            req: Request = Request(url, headers={"User-Agent": HTTP_USER_AGENT})
            with urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                body: bytes = resp.read()
            return json.loads(body.decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            logger.warning("TomTom incidents fetch failed: %s", exc)
            return None

    def _fetch_flow_tile(self, z: int, x: int, y: int) -> Optional[bytes]:
        """Fetch a single flow tile.  None on failure."""
        url: str = (
            TT_FLOW_TILE_URL.format(z=z, x=x, y=y)
            + "?" + urlencode({"key": self._api_key or ""})
        )
        try:
            req: Request = Request(url, headers={"User-Agent": HTTP_USER_AGENT})
            with urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                if resp.status != 200:
                    logger.warning(
                        "TomTom flow tile %d/%d/%d returned HTTP %d",
                        z, x, y, resp.status,
                    )
                    return None
                return resp.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            logger.warning(
                "TomTom flow tile %d/%d/%d fetch failed: %s",
                z, x, y, exc,
            )
            return None

    # ── Internal: bbox + LRU ────────────────────────────────────────

    def _bbox_str(self) -> str:
        """Build the TomTom bbox parameter from home + radius.

        TomTom expects ``minLon,minLat,maxLon,maxLat``.  At Mobile's
        latitude (~30°N) one degree of latitude is ~111 km and one
        degree of longitude is ~95 km — the cosine correction
        below handles that so the bbox is the requested square.
        """
        # ``enabled`` guarantees these are populated when this is called.
        assert self._center_lat is not None and self._center_lon is not None
        lat_delta: float = self._radius_km / (math.pi * EARTH_RADIUS_KM / 180.0)
        lon_delta: float = lat_delta / max(
            math.cos(math.radians(self._center_lat)), 0.01,
        )
        min_lon: float = self._center_lon - lon_delta
        max_lon: float = self._center_lon + lon_delta
        min_lat: float = self._center_lat - lat_delta
        max_lat: float = self._center_lat + lat_delta
        # Round to 4 decimal places (~11 m at the equator) — finer
        # than the bbox cares about, coarser than float jitter that
        # would cache-miss on every request.
        return f"{min_lon:.4f},{min_lat:.4f},{max_lon:.4f},{max_lat:.4f}"

    def _evict_if_needed(self) -> None:
        """Drop oldest tiles when the cache exceeds the cap."""
        while len(self._tile_cache) > FLOW_TILE_CACHE_CAP:
            self._tile_cache.popitem(last=False)


def _iso(ts: float) -> str:
    """Format a UNIX timestamp as ISO-8601 UTC seconds."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
