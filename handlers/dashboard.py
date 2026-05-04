"""Dashboard UI endpoint handlers.

Mixin class for GlowUpRequestHandler.  Extracted from server.py.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import logging

logger: logging.Logger = logging.getLogger("glowup.dashboard")
import math
import os
import socket
import struct
import threading
import time as time_mod
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from urllib.parse import unquote

# server_constants not used in this module.
from atomic_io import write_json_atomic
from operators import OperatorManager
from media import SignalBus
from schedule_utils import parse_time_spec as _parse_time_spec
from solar import sun_times

# Voice-subsystem constants — pulled in so the satellite health
# endpoints below can use the same thresholds and topic names as
# server.py's subscription wiring and voice/satellite/daemon.py's
# reply publisher.  Imported under short aliases to keep the
# handler bodies readable.
from voice.constants import (
    HUB_SATELLITE_PROBE_TIMEOUT_S as _SAT_PROBE_TIMEOUT_S,
    SAT_HEARTBEAT_STALE_S as _SAT_HEARTBEAT_STALE_S,
    TOPIC_HEALTH_REQUEST as _VOICE_TOPIC_HEALTH_REQUEST,
)

# Max seconds without a non-time signal on glowup/signals/# before
# broker-2 is reported unhealthy.  Zigbee and BLE both live on
# broker-2 now (glowup-zigbee-service and glowup-ble-sensor), and
# both publish cross-host to the hub using this topic prefix.  The
# hub's _on_remote_signal callback stamps a class-level timestamp
# on every non-time message.  120s covers the slowest expected
# publisher cadence — plugs report at least once a minute, soil
# sensors report less often, but any one producer being alive keeps
# the timestamp fresh.
BROKER2_SIGNALS_STALE_SEC: float = 120.0


# Node-id → IP map for the thermal dashboard.  Source of truth is
# /etc/glowup/site.json (see glowup_site); the renderer in glowup-
# infra populates the ``node_ips`` key from inventory.yaml.  This
# replaces the older /etc/glowup/node_ips.json drop and is the single
# place a fleet operator edits to add a host to the dashboard.  An
# absent or empty ``node_ips`` value renders the dashboard's IP
# column blank — no crash, no late failure.
from glowup_site import site as _site, SiteConfigError


def _load_node_ips() -> dict[str, str]:
    """Pull the lowercase ``node_id`` → IPv4 map from site config.

    Returns an empty dict if site.json is missing, has no
    ``node_ips`` key, or holds a non-dict value.  Defensive isinstance
    filters drop any non-string keys/values without crashing — the
    dashboard's IP column is a "show what you can" surface.

    Catches :class:`SiteConfigError` (raised on placeholder values) and
    logs at warning level so the operator sees an actionable message
    in the journal but the dashboard request itself doesn't fail.
    """
    try:
        raw: Any = _site.get("node_ips") or {}
    except SiteConfigError as exc:
        logger.warning("site.node_ips holds a placeholder: %s", exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("site.node_ips is not a JSON object: %r", type(raw).__name__)
        return {}
    return {
        str(k): str(v)
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str)
    }


# Loaded once at module import.  Restart of glowup-server picks up
# site.json edits; matches the existing /etc/glowup/server.json
# convention where edits require a service restart.
_NODE_IPS: dict[str, str] = _load_node_ips()


class DashboardHandlerMixin:
    """Dashboard UI endpoint handlers."""

    def _handle_get_root(self) -> None:
        """GET / — 302 redirect to /dashboard.

        The /dashboard page is the LIFX install's primary surface.
        A bare ``http://<host>:8420/`` previously returned 404, which
        reads as "this didn't install correctly" even when the server
        is healthy.  302 (rather than 301) so the browser revisits /
        on each load — keeps room for the redirect target to change
        without leaving permanent caches behind.
        """
        self.send_response(302)
        self.send_header("Location", "/dashboard")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_get_dashboard(self) -> None:
        """GET /dashboard — serve the static HTML dashboard page.

        Reads ``static/dashboard.html`` from the server's directory
        and returns it as ``text/html``.  Returns 404 if the file
        is missing.
        """
        dashboard_path: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "dashboard.html",
        )
        try:
            with open(dashboard_path, "r") as f:
                html: str = f.read()
            body: bytes = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # Prevent browser caching so dashboard updates deploy instantly.
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._send_json(404, {"error": "Dashboard page not found"})


    def _handle_get_vivint_page(self) -> None:
        """GET /vivint — serve the full Vivint status dashboard page.

        Reads ``static/vivint.html`` and returns it as text/html.
        """
        vivint_path: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "vivint.html",
        )
        try:
            with open(vivint_path, "r") as f:
                html: str = f.read()
            body: bytes = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._send_json(404, {"error": "Vivint dashboard page not found"})


    def _handle_get_operators(self) -> None:
        """GET /api/operators — list running operators with status.

        Response::

            {"operators": [{name, type, started, tick_mode, ...}, ...]}
        """
        om: Optional[OperatorManager] = self.operator_manager
        if om is not None:
            self._send_json(200, {"operators": om.get_status()})
        else:
            self._send_json(200, {"operators": []})


    # --- Binding CRUD endpoints -------------------------------------------

    def _handle_get_bindings(self) -> None:
        """GET /api/signals/bindings — list all active param bindings.

        Response::

            {"bindings": [
                {"operator": "occ", "param": "away_confirm_seconds",
                 "target": "occ:away_confirm_seconds",
                 "source": "house:occupancy:state",
                 "scale": [5.0, 1.0]},
                ...
            ]}
        """
        om: Optional[OperatorManager] = self.operator_manager
        if om is not None:
            self._send_json(200, {"bindings": om.get_all_bindings()})
        else:
            self._send_json(200, {"bindings": []})

    def _handle_post_binding(self) -> None:
        """POST /api/signals/bindings — create or replace a binding.

        Request body::

            {"operator": "cylon_runner", "param": "speed",
             "signal": "breathe_runner:speed",
             "scale": [0.1, 30.0], "reduce": "max"}

        Responds 400 if the binding would create a cycle, the operator
        is not found, or the param does not exist.
        """
        om: Optional[OperatorManager] = self.operator_manager
        if om is None:
            self._send_json(503, {"error": "Operator manager not running"})
            return
        body: dict = self._read_json_body()
        if not body:
            self._send_json(400, {"error": "Missing request body"})
            return
        op_name: str = body.get("operator", "")
        param_name: str = body.get("param", "")
        source: str = body.get("signal", "")
        if not op_name or not param_name or not source:
            self._send_json(400, {
                "error": "Required fields: operator, param, signal",
            })
            return
        spec: dict = {"signal": source}
        if "scale" in body:
            spec["scale"] = body["scale"]
        if "reduce" in body:
            spec["reduce"] = body["reduce"]
        try:
            om.create_binding(op_name, param_name, spec)
            self._send_json(200, {"ok": True, "binding": {
                "target": f"{op_name}:{param_name}",
                "source": source,
            }})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})

    def _handle_delete_binding(self, target: str) -> None:
        """DELETE /api/signals/bindings/{target} — remove a binding.

        The *target* path segment is ``operator:param`` (e.g.,
        ``cylon_runner:speed``).  Param keeps its last bound value.
        """
        om: Optional[OperatorManager] = self.operator_manager
        if om is None:
            self._send_json(503, {"error": "Operator manager not running"})
            return
        parts: list[str] = target.split(":", 1)
        if len(parts) != 2:
            self._send_json(400, {
                "error": "Target must be operator:param (e.g., occ:speed)",
            })
            return
        op_name, param_name = parts
        try:
            om.remove_binding(op_name, param_name)
            self._send_json(200, {"ok": True})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})

    def _handle_get_nav_config(self) -> None:
        """GET /api/config/nav — navigation links for the site nav bar.

        Returns the list of nav links from server.json ``nav_links``.
        Pages build the nav bar dynamically from this endpoint so
        no internal IPs are hardcoded in HTML.

        Default links (Dashboard, Power, Thermal, I/O, Shopping) are
        always included.  External links (e.g., Zigbee2MQTT) come
        from config.
        """
        # Built-in pages — always present.
        links: list[dict[str, str]] = [
            {"label": "Dashboard", "href": "/dashboard"},
            {"label": "Power", "href": "/power"},
            {"label": "Thermal", "href": "/thermal"},
            {"label": "I/O", "href": "/io"},
            {"label": "Shopping", "href": "/shopping"},
            {"label": "Vivint", "href": "/vivint"},
            {"label": "SDR", "href": "/sdr"},
            {"label": "ADS-B", "href": "/adsb"},
        ]
        # External links from config.
        extra: list[dict[str, str]] = self.config.get("nav_links", [])
        links.extend(extra)
        self._send_json(200, {"links": links})


    # ---------------------------------------------------------------------
    # Satellite health — continuous + on-demand deep probe
    # ---------------------------------------------------------------------
    #
    # The hub subscribes to glowup/voice/status/# and
    # glowup/voice/health/reply/# in server.py's _background_startup
    # and populates GlowUpRequestHandler class-level dicts:
    #
    #   satellite_heartbeats[room]     — {"ts": float, "payload": dict}
    #   satellite_health_replies[room] — full deep-check report dict
    #
    # These handlers derive their output entirely from those dicts
    # plus a wall-clock read.  On-demand handlers also publish a
    # fresh request via GlowUpRequestHandler.satellite_probe_client
    # and wait for the matching reply (correlation id).

    def _handle_get_satellites_health(self) -> None:
        """GET /api/satellites/health — full per-room health view.

        Combines heartbeat freshness with the latest deep-check
        reply for every room the hub has seen.  No authentication
        required — the payload is diagnostic only.

        Response::

            {
              "now": <unix-ts>,
              "rooms": {
                "<room>": {
                  "heartbeat": {
                    "age_s": float,
                    "ok": bool,
                    "payload": {... last heartbeat dict ...}
                  },
                  "last_deep_check": {
                    "age_s": float,
                    "ok": bool,
                    "checks": {... subsystem dict ...},
                    "recommended_action": str|null
                  }
                }
              }
            }

        Rooms with no heartbeat AND no deep-check reply are omitted.
        Entries where one of the two is missing have that key set
        to ``null`` — the consumer must tolerate both cases.
        """
        cls: Any = self.__class__
        now: float = time_mod.time()
        with cls.satellite_state_lock:
            heartbeats: dict[str, dict[str, Any]] = dict(
                cls.satellite_heartbeats,
            )
            replies: dict[str, dict[str, Any]] = dict(
                cls.satellite_health_replies,
            )
        rooms: set[str] = set(heartbeats.keys()) | set(replies.keys())
        out: dict[str, dict[str, Any]] = {}
        for room in sorted(rooms):
            hb: Optional[dict[str, Any]] = heartbeats.get(room)
            rp: Optional[dict[str, Any]] = replies.get(room)
            hb_block: Optional[dict[str, Any]] = None
            if hb is not None:
                hb_age: float = now - float(hb.get("ts", 0.0))
                hb_block = {
                    "age_s": hb_age,
                    "ok": hb_age < _SAT_HEARTBEAT_STALE_S,
                    "payload": hb.get("payload", {}),
                }
            deep_block: Optional[dict[str, Any]] = None
            if rp is not None:
                rp_ts: float = float(rp.get("timestamp", 0.0))
                deep_block = {
                    "age_s": now - rp_ts if rp_ts > 0 else None,
                    "ok": bool(rp.get("ok", False)),
                    "checks": rp.get("checks", {}),
                    "recommended_action": rp.get("recommended_action"),
                }
            out[room] = {
                "heartbeat": hb_block,
                "last_deep_check": deep_block,
            }
        self._send_json(200, {"now": now, "rooms": out})

    def _handle_post_satellite_health_check(self, room: str) -> None:
        """POST /api/satellites/{room}/health/check — on-demand probe.

        Publishes a request on ``glowup/voice/health/request`` with
        a fresh correlation id and the target ``room`` field, then
        blocks for up to ``HUB_SATELLITE_PROBE_TIMEOUT_S`` waiting
        for a reply correlated to that id.  Returns the full deep
        report, or a 504 with the most recent heartbeat age if no
        reply arrives — future-me reads the 504 body and already
        knows whether the room is dead or just slow.

        Args:
            room: Target room name (URL-decoded).  Must match a
                  room that has ever heartbeated; otherwise 404.
        """
        import uuid
        cls: Any = self.__class__
        if cls.satellite_probe_client is None:
            self._send_json(503, {
                "error": (
                    "satellite probe client not ready — server "
                    "MQTT is still initialising"
                ),
            })
            return
        # Tolerate callers that URL-encode the room name.  Match
        # against any known heartbeat room; 404 if we've never seen
        # the target room at all.  (A satellite that just booted
        # and hasn't heartbeated yet cannot be probed on-demand —
        # wait for the first heartbeat tick.)
        with cls.satellite_state_lock:
            known: set[str] = set(cls.satellite_heartbeats.keys())
        if room not in known:
            self._send_json(404, {
                "error": (
                    f"room {room!r} has never heartbeated; known "
                    f"rooms: {sorted(known)}"
                ),
            })
            return

        corr_id: str = f"ondemand-{uuid.uuid4().hex[:12]}"
        waiter: threading.Event = threading.Event()
        with cls.satellite_state_lock:
            cls.satellite_health_events[corr_id] = waiter
        try:
            payload: bytes = json.dumps({
                "id": corr_id, "room": room,
            }).encode("utf-8")
            try:
                cls.satellite_probe_client.publish(
                    _VOICE_TOPIC_HEALTH_REQUEST, payload, qos=1,
                )
            except Exception as exc:
                self._send_json(502, {
                    "error": f"publish failed: {exc!r}",
                })
                return
            # Wait for the reply callback to set() this event.
            arrived: bool = waiter.wait(
                timeout=_SAT_PROBE_TIMEOUT_S,
            )
            if not arrived:
                # Fall back to last-known heartbeat age.
                with cls.satellite_state_lock:
                    hb: Optional[dict[str, Any]] = (
                        cls.satellite_heartbeats.get(room)
                    )
                hb_age: Optional[float] = None
                if hb is not None:
                    hb_age = time_mod.time() - float(hb.get("ts", 0.0))
                self._send_json(504, {
                    "error": (
                        f"no deep-check reply from room {room!r} "
                        f"within {_SAT_PROBE_TIMEOUT_S:.0f}s"
                    ),
                    "last_heartbeat_age_s": hb_age,
                    "recommended_action": (
                        f"room {room!r} did not answer the deep "
                        "health check.  Verify the satellite host "
                        "is reachable and glowup-satellite is "
                        "active; inspect journalctl for errors."
                    ),
                })
                return
            # Reply arrived — fetch the stashed report.
            with cls.satellite_state_lock:
                report: Optional[dict[str, Any]] = (
                    cls.satellite_health_replies.get(room)
                )
            if report is None or report.get("id") != corr_id:
                # Another reply (periodic prober) landed between
                # wake and fetch.  Serve whatever is fresh — it is
                # still authoritative for the room's current state.
                self._send_json(200, report or {"error": "race"})
                return
            self._send_json(200, report)
        finally:
            # Always clear the waiter entry so the dict doesn't
            # leak correlation ids over time.
            with cls.satellite_state_lock:
                cls.satellite_health_events.pop(corr_id, None)

    def _handle_get_io_page(self) -> None:
        """GET /io — serve the I/O timing dashboard."""
        static_dir: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static",
        )
        path: str = os.path.join(static_dir, "io.html")
        try:
            with open(path, "rb") as f:
                content: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json(404, {"error": "io.html not found"})

    def _handle_get_io_stats(self) -> None:
        """GET /api/io/stats — timed I/O histogram data per label.

        Returns per-label statistics: call count, timeout count,
        min/max/avg/p50/p95/p99 in milliseconds, and the assigned
        IO class.  Used by the IO dashboard to visualize blocking
        operation performance.

        Response::

            {
              "labels": {
                "lanscan.arp": {
                  "class": "FAST",
                  "count": 342,
                  "timeouts": 2,
                  "min_ms": 0.1,
                  "max_ms": 1800.0,
                  "avg_ms": 12.3,
                  "p50_ms": 8.1,
                  "p95_ms": 45.2,
                  "p99_ms": 180.0
                }
              }
            }
        """
        from infrastructure.timed_io import get_all_stats, WINDOW_SECONDS
        all_stats = get_all_stats()
        result: dict[str, dict[str, Any]] = {}
        for label, stats in all_stats.items():
            result[label] = {
                "class": stats.io_class.name,
                "window": {
                    "seconds": WINDOW_SECONDS,
                    "count": stats.window_count(),
                    "exceeded": stats.window_exceeded(),
                    "min_ms": round(stats.window_min_ms(), 1),
                    "max_ms": round(stats.window_max_ms(), 1),
                    "avg_ms": round(stats.window_avg_ms(), 1),
                    "stddev_ms": round(stats.window_stddev_ms(), 1),
                    "p50_ms": round(stats.window_percentile(0.50), 1),
                    "p95_ms": round(stats.window_percentile(0.95), 1),
                    "p99_ms": round(stats.window_percentile(0.99), 1),
                },
                "lifetime": {
                    "count": stats.count,
                    "exceeded": stats.timeout_count,
                    "min_ms": round(stats.min_ms, 1)
                        if stats.min_ms != float("inf") else 0.0,
                    "max_ms": round(stats.max_ms, 1),
                    "avg_ms": round(stats.avg_ms(), 1),
                    "stddev_ms": round(stats.stddev_ms(), 1),
                },
            }
        self._send_json(200, {"labels": result})

    def _handle_get_static_js(self, filename: str) -> None:
        """GET /js/{filename} — serve a shared JavaScript file from static/js/.

        All dashboards share reusable client-side code (site nav bar,
        future shared widgets).  Mirrors ``_handle_get_photo`` for path
        validation and MIME handling.  Only ``.js`` files are served.
        Directory traversal is rejected.
        """
        # Reject any path traversal attempts.
        if "/" in filename or "\\" in filename or ".." in filename:
            self._send_json(400, {"error": "Invalid filename"})
            return
        # Only serve .js — this handler is not a general static server.
        if not filename.endswith(".js"):
            self._send_json(400, {"error": "Only .js files are served"})
            return
        js_path: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "js", filename,
        )
        try:
            with open(js_path, "rb") as f:
                data: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            # 5-minute cache — short enough for fast iteration,
            # long enough to matter on multi-tab clients.
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self._send_json(404, {"error": f"JS file not found: {filename}"})


    def _handle_get_photo(self, filename: str) -> None:
        """GET /photos/{filename} — serve a photo from static/photos/.

        Validates the filename to prevent directory traversal,
        then serves the image with appropriate content type.
        """
        # Content types by extension.
        CONTENT_TYPES: dict[str, str] = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp",
        }
        # Reject any path traversal attempts.
        if "/" in filename or "\\" in filename or ".." in filename:
            self._send_json(400, {"error": "Invalid filename"})
            return
        _, ext = os.path.splitext(filename)
        ctype: str = CONTENT_TYPES.get(ext.lower(), "")
        if not ctype:
            self._send_json(400, {"error": "Unsupported image type"})
            return
        photo_path: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "photos", filename,
        )
        try:
            with open(photo_path, "rb") as f:
                data: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            # Cache photos for 5 minutes — they change rarely.
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self._send_json(404, {"error": f"Photo not found: {filename}"})


    def _save_config_field(self, key: str, value: Any) -> None:
        """Persist a single config field to the config file.

        Reads the config JSON, updates the given key, and writes back.
        Schedule entries are saved to the external schedule file if
        one is configured (``_schedule_path``).

        Serialized by ``_config_save_lock`` so concurrent saves on
        different keys do not clobber each other.

        Args:
            key:   Top-level config key to update.
            value: The new value.
        """
        with self._config_save_lock:
            # All three write paths below use ``write_json_atomic`` so
            # that a SIGKILL or power loss during the write never
            # leaves the state file truncated / unparseable; the worst
            # case is the previous good contents survive.  See
            # atomic_io.py for the durability boundary this provides.

            # Route schedule writes to the schedule file if it exists.
            sched_path: Optional[str] = self.config.get("_schedule_path")
            if key == "schedule" and sched_path:
                try:
                    with open(sched_path, "r") as f:
                        sched_config: dict[str, Any] = json.load(f)
                    sched_config["schedule"] = value
                    write_json_atomic(sched_path, sched_config)
                except Exception as exc:
                    logging.exception(
                        "Failed to save schedule to '%s'",
                        sched_path,
                    )
                return

            # Route groups writes to the groups file if it exists.
            # Mirrors the schedule_file pattern above — when the
            # operator's server.json sets ``groups_file``, server.py's
            # ``_load_config`` stamps ``_groups_path`` into the live
            # config dict; we look for it here and write the registry
            # directly to that file rather than back into server.json.
            # The file's top-level shape is the groups dict itself
            # (``{name: [entries], ...}``), not wrapped in another
            # key, matching server.py's ``groups_data`` consumer.
            groups_path: Optional[str] = self.config.get("_groups_path")
            if key == "groups" and groups_path:
                try:
                    write_json_atomic(groups_path, value)
                except Exception as exc:
                    logging.exception(
                        "Failed to save groups to '%s'",
                        groups_path,
                    )
                return

            config_path: Optional[str] = self.config_path
            if config_path is None:
                return
            try:
                with open(config_path, "r") as f:
                    config: dict[str, Any] = json.load(f)
                config[key] = value
                write_json_atomic(config_path, config)
            except Exception as exc:
                logging.warning(
                    "Failed to save config field '%s': %s",
                    key, exc, exc_info=True,
                )

    # -- Power monitoring ---------------------------------------------------

    def _handle_get_power_page(self) -> None:
        """GET /power — serve the power monitoring dashboard."""
        static_dir: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static",
        )
        path: str = os.path.join(static_dir, "power.html")
        try:
            with open(path, "rb") as f:
                content: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json(404, {"error": "power.html not found"})

    def _handle_get_power_readings(self) -> None:
        """GET /api/power/readings?device=X&hours=N&resolution=N"""
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        device: str = params.get("device", [None])[0]
        hours: float = float(params.get("hours", ["1"])[0])
        resolution: int = int(params.get("resolution", ["60"])[0])

        pl = self.power_logger
        if pl is None:
            self._send_json(200, {"readings": []})
            return
        readings = pl.query(device=device, hours=hours, resolution=resolution)
        self._send_json(200, {"readings": readings})

    def _handle_get_power_summary(self) -> None:
        """GET /api/power/summary?device=X&days=N"""
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        device: str = params.get("device", [None])[0]
        days: int = int(params.get("days", ["7"])[0])

        pl = self.power_logger
        if pl is None:
            self._send_json(200, {})
            return
        summary = pl.summary(device=device, days=days)
        self._send_json(200, summary)

    def _handle_get_power_devices(self) -> None:
        """GET /api/power/devices"""
        pl = self.power_logger
        if pl is None:
            self._send_json(200, {"devices": []})
            return
        self._send_json(200, {"devices": pl.devices()})

    def _handle_get_power_plug_states(self) -> None:
        """GET /api/power/plug_states — live ON/OFF state for every smart plug.

        Proxies the zigbee_service REST endpoint on broker-2
        (``http://{broker}:8422/devices``) and distills its response
        into a device-keyed map.  The dashboard uses this to render
        the on/off toggle accurately — inferring state from power
        draw misreported any ON plug drawing under 1 W (dark TV, idle
        charger, empty outlet) as OFF on every refresh.

        The state chain (source of truth → UI) is::

            Zigbee plug relay (genOnOff attribute)
              → Z2M publishes on zigbee2mqtt/{device}
              → zigbee_service maintains in-memory DeviceState
              → HTTP /devices returns {state, power_w, online, ...}
              → this proxy strips to {state, power_w, online, age_sec}
              → /power.html renders the toggle

        Returns::

            {
              "plugs": {
                "LRTV":     {"state": "ON",  "power_w": 0.0, "online": true,  "age_sec": 12.3},
                "BYIR":     {"state": "ON",  "power_w": 2.3, "online": true,  "age_sec":  3.1},
                "ML_Power": {"state": null,  "power_w": null, "online": false, "age_sec": 24838.9}
              },
              "source": "http://<broker-2 host>:8422/devices"
            }

        On proxy failure the endpoint still returns 200 with
        ``{"plugs": {}, "error": "..."}`` so the dashboard degrades
        gracefully rather than breaking the whole page.
        """
        # Broker-2 owns Zigbee end-to-end (commit 1d3d8df).  Its
        # HTTP host is the same as its MQTT broker host in config.
        zigbee_cfg: dict[str, Any] = self.config.get("zigbee", {}) or {}
        broker_host: str = zigbee_cfg.get("broker", "localhost")
        # Port 8422 is the zigbee_service default (GLZ_HTTP_PORT).
        zigbee_http_port: int = int(zigbee_cfg.get("http_port", 8422))
        url: str = f"http://{broker_host}:{zigbee_http_port}/devices"

        import urllib.request
        import urllib.error
        plugs: dict[str, dict[str, Any]] = {}
        try:
            # Short timeout — the dashboard refreshes this; if broker-2
            # is unreachable we return an empty map rather than stall.
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                raw: bytes = resp.read()
            data: dict[str, Any] = json.loads(raw)
            for dev in data.get("devices", []):
                name: Optional[str] = dev.get("name")
                if not isinstance(name, str) or not name:
                    continue
                # Only expose plugs — devices that carry a ``state``
                # attribute at all (sensors do not).  A null state on
                # an offline device is still reported so the UI can
                # show it as greyed-out rather than dropping it.
                plugs[name] = {
                    "state": dev.get("state"),
                    "power_w": dev.get("power_w"),
                    "online": bool(dev.get("online", False)),
                    "age_sec": dev.get("age_sec"),
                }
            self._send_json(200, {"plugs": plugs, "source": url})
        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError, ValueError) as exc:
            # Fail open — dashboard keeps working, just without
            # authoritative state.
            self._send_json(200, {
                "plugs": {},
                "source": url,
                "error": f"{type(exc).__name__}: {exc}",
            })

    # ---- Thermal dashboard ------------------------------------------------

    def _handle_get_thermal_page(self) -> None:
        """GET /thermal — serve the fleet thermal grid HTML.

        Rigid columnar dashboard showing every node's most recent
        thermal sample, false-colored by CPU temperature.
        """
        static_dir: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "static",
        )
        path: str = os.path.join(static_dir, "thermal.html")
        try:
            with open(path, "rb") as f:
                content: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            # The dashboard HTML/JS evolves frequently and stale
            # browser cache silently hides fixes — Perry hit this
            # 2026-04-25 when a JS update for the dead-row indicator
            # was masked by his cached copy.  Force a fresh fetch
            # every time; the file is ~13 KB and the page polls a
            # JSON API for live data anyway.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json(404, {"error": "thermal.html not found"})

    def _handle_get_thermal_detail_page(self, node_id: str) -> None:
        """GET /thermal/host/{node_id} — per-host detail HTML.

        The HTML file is static; ``node_id`` is read client-side from
        ``location.pathname`` so there is no templating here.  We
        ignore the captured ``node_id`` — it is validated at query
        time by the ``/api/thermal/readings`` handler.

        Args:
            node_id: The captured URL segment (unused at this layer).
        """
        del node_id  # Consumed by the client-side script, not here.
        static_dir: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "static",
        )
        path: str = os.path.join(static_dir, "thermal_detail.html")
        try:
            with open(path, "rb") as f:
                content: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json(404, {"error": "thermal_detail.html not found"})

    def _handle_get_thermal_latest(self) -> None:
        """GET /api/thermal/latest — fleet snapshot.

        Returns a dict keyed by node_id with the most recent row for
        each known host.  The fleet dashboard polls this every 5
        seconds to refresh the grid.

        Each row is augmented with an ``ip`` field resolved from the
        module-level ``_NODE_IPS`` map.  This is a hub-side lookup
        rather than a self-reported sensor field; see ``_NODE_IPS``
        for the rationale.
        """
        tl: Any = getattr(self, "thermal_logger", None)
        if tl is None:
            self._send_json(200, {"hosts": {}})
            return
        hosts: dict[str, Any] = tl.latest()
        for node_id, reading in hosts.items():
            if isinstance(reading, dict):
                reading["ip"] = _NODE_IPS.get(node_id)
        self._send_json(200, {"hosts": hosts})

    def _handle_get_thermal_hosts(self) -> None:
        """GET /api/thermal/hosts — distinct node_ids with any data."""
        tl: Any = getattr(self, "thermal_logger", None)
        if tl is None:
            self._send_json(200, {"hosts": []})
            return
        self._send_json(200, {"hosts": tl.hosts()})

    def _handle_get_thermal_readings(self) -> None:
        """GET /api/thermal/readings?node=X&hours=N&resolution=N.

        Returns a time-bucketed history for a single node, for the
        per-host detail page charts.  ``hours`` and ``resolution``
        default to 1 hour at 60-second resolution to match the
        default dashboard range.
        """
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        node: Optional[str] = params.get("node", [None])[0]
        if not node:
            self._send_json(400, {"error": "node parameter required"})
            return
        hours: float = float(params.get("hours", ["1"])[0])
        resolution: int = int(params.get("resolution", ["60"])[0])

        tl: Any = getattr(self, "thermal_logger", None)
        if tl is None:
            self._send_json(200, {"readings": []})
            return
        readings = tl.query(node_id=node, hours=hours, resolution=resolution)
        self._send_json(200, {"readings": readings})

    # _handle_post_zigbee_set was removed in 2026-04-15.  It used
    # the deleted in-process Zigbee adapter proxy and had been
    # returning 503 on every call since the broker-2 service
    # pivot.  Plug control will return as a hub→broker-2 cross-
    # host publisher (the inverse of glowup-zigbee-service's data
    # path) — see docs/29-zigbee-service.md "What's broken
    # (follow-up)" and the entry in MEMORY.md.

    # -- Helpers ------------------------------------------------------------


