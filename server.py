"""GlowUp REST API server — remote control daemon for LIFX devices.

Provides an HTTP API for querying and controlling LIFX devices from
anywhere.  Designed to be the single daemon running on a Raspberry Pi
(or Mac), this server subsumes the role of the standalone scheduler by
managing effects directly through the :class:`Controller` API instead
of spawning subprocesses.

The server does **not** perform broadcast discovery.  All device IPs
must be listed in the ``groups`` section of the configuration file.
Direct per-IP queries are both faster and more reliable than broadcast
discovery, which requires multiple retries with long timeouts and is
defeated by mesh routers that filter broadcast packets between nodes.

Architecture::

    iPhone App (SwiftUI)
        ↓ HTTPS
    Cloudflare Edge (TLS termination)
        ↓ encrypted tunnel
    cloudflared on Pi
        ↓ localhost
    server.py (this file)
        ↓ in-process
    Controller / Engine / Transport
        ↓ UDP
    LIFX Devices

Endpoints::

    GET  /api/status                     Server readiness and version
    GET  /api/devices                    List configured devices
    GET  /api/effects                    List effects with param metadata
    GET  /api/devices/{ip}/status        Current effect and params
    GET  /api/devices/{ip}/colors        Snapshot of zone HSBK values
    GET  /api/devices/{ip}/colors/stream SSE stream at 4 Hz
    POST /api/devices/{ip}/play          Start an effect
    POST /api/devices/{ip}/stop          Stop current effect
    POST /api/devices/{ip}/resume        Clear phone override, resume schedule
    POST /api/devices/{ip}/power         Turn device on/off
    POST /api/devices/{ip}/identify      Pulse brightness to locate device
    POST /api/devices/{ip}/nickname      Set a custom display name
    POST /api/effects/{name}/defaults    Save tuned params as effect defaults
    GET  /api/groups                     Device groups from config
    GET  /api/schedule                   Schedule entries with resolved times
    POST /api/schedule                   Create a new schedule entry
    POST /api/schedule/{index}/enabled   Enable or disable a schedule entry
    GET  /api/media/sources              List media sources with status
    GET  /api/media/signals              List available signal names
    POST /api/media/sources/{name}/start Manually start a media source
    POST /api/media/sources/{name}/stop  Manually stop a media source
    POST /api/media/signals/ingest       Write signals from external source
    GET  /api/diagnostics/now_playing    Currently playing effects (from DB)
    GET  /api/diagnostics/history        Recent effect history (from DB)
    GET  /dashboard                      Web dashboard (HTML)
    GET  /home                           Sensor display dashboard (HTML)
    GET  /api/home/photos                List available photos for home display
    GET  /photos/{filename}              Serve a photo from static/photos/
Usage::

    python3 server.py server.json
    python3 server.py --dry-run server.json   # preview schedule
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "2.0"

import argparse
import hmac
import http.server
import ipaddress
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import unquote, parse_qs, urlparse
import logging
import math
import os
import re
import signal
import socketserver
import sys
import threading
import time as time_mod
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from effects import (
    get_registry, create_effect, MediaEffect,
    HSBK, HSBK_MAX, KELVIN_DEFAULT,
)
from emitters import Emitter
from emitters.lifx import LifxEmitter
from emitters.virtual import VirtualMultizoneEmitter
from emitters.virtual_grid import VirtualGridEmitter
from engine import Controller
from mqtt_bridge import MqttBridge, PAHO_AVAILABLE as _MQTT_AVAILABLE
from ble_trigger import BleTriggerManager
from automation import (
    AutomationManager, sensor_data as ble_sensor_data,
    validate_automation, migrate_ble_triggers,
)
from operators import OperatorManager
try:
    from lock_manager import LockManager
    _HAS_LOCK_MANAGER: bool = True
except ImportError:
    _HAS_LOCK_MANAGER = False

try:
    from zigbee_adapter import ZigbeeAdapter
    _HAS_ZIGBEE: bool = True
except ImportError:
    _HAS_ZIGBEE = False

try:
    from vivint_adapter import VivintAdapter
    _HAS_VIVINT: bool = True
except ImportError:
    _HAS_VIVINT = False

try:
    from nvr_adapter import NvrAdapter
    _HAS_NVR: bool = True
except ImportError:
    _HAS_NVR = False
try:
    from printer_adapter import PrinterAdapter
    _HAS_PRINTER: bool = True
except ImportError:
    _HAS_PRINTER = False
try:
    from ble_adapter import BleAdapter
    _HAS_BLE_ADAPTER: bool = True
except ImportError:
    _HAS_BLE_ADAPTER = False
from media import MediaManager, SignalBus
from media.source import AudioStreamServer
from solar import SunTimes, sun_times
from transport import LifxDevice, SendMode, SOCKET_TIMEOUT, SINGLE_ZONE_COUNT

# Optional distributed compute subsystem.
try:
    from distributed.orchestrator import Orchestrator
    _HAS_DISTRIBUTED: bool = True
except ImportError:
    Orchestrator = None  # type: ignore[assignment,misc]
    _HAS_DISTRIBUTED = False

# Optional diagnostics subsystem (requires psycopg2 + PostgreSQL).
try:
    from diagnostics import DiagnosticsLogger
    _HAS_DIAGNOSTICS: bool = True
except ImportError:
    DiagnosticsLogger = None  # type: ignore[assignment,misc]
    _HAS_DIAGNOSTICS = False

# SQLite state store — records which brain owns each bulb and why.
# Degrades gracefully if the module is missing or the DB is unwritable.
try:
    from state_store import StateStore
    _HAS_STATE_STORE: bool = True
except ImportError:
    StateStore = None  # type: ignore[assignment,misc]
    _HAS_STATE_STORE = False

# ARP-based bulb discovery and keepalive daemon.
from bulb_keepalive import BulbKeepAlive

# MAC-based device identity registry.
from device_registry import DeviceRegistry

# ---------------------------------------------------------------------------
# Constants — imported from server_constants.py
# ---------------------------------------------------------------------------

from server_constants import (
    DEFAULT_PORT, SSE_POLL_HZ, SSE_POLL_INTERVAL, MAX_REQUEST_BODY,
    SCHEDULER_POLL_SECONDS, DEFAULT_FADE_MS, BRIGHTNESS_PERCENTAGE_SCALE,
    DEVICE_WAKEUP_DELAY_SECONDS, HSTS_MAX_AGE_SECONDS,
    AUDIO_QUEUE_TIMEOUT_SECONDS, CALIBRATION_SOCKET_TIMEOUT_SECONDS,
    CALIBRATION_PULSE_DELAY_SECONDS, AUTH_HEADER, BEARER_PREFIX,
    DEFAULT_CONFIG_PATH, EFFECT_DEFAULTS_FILENAME,
    GROUP_PREFIX, GRID_PREFIX, LOG_FORMAT, LOG_DATE_FORMAT,
    API_PREFIX, AUTH_RATE_LIMIT, AUTH_RATE_WINDOW, SSE_TIMEOUT_SECONDS,
    IDENTIFY_DURATION_SECONDS, IDENTIFY_CYCLE_SECONDS,
    IDENTIFY_FRAME_INTERVAL, IDENTIFY_MIN_BRI,
    COMMAND_DISCOVER_TIMEOUT_SECONDS, COMMAND_IDENTIFY_MAX_DURATION,
    DEVICE_RESOLVE_ERROR,
)

# Schedule parsing regex and constants — canonical source: schedule_utils.py.
from schedule_utils import (
    SYMBOLIC_RE as _SYMBOLIC_RE,
    FIXED_TIME_RE as _FIXED_TIME_RE,
    resolve_entries as _resolve_entries,
    parse_time_spec as _parse_time_spec,
    entry_runs_on_day as _entry_runs_on_day,
    find_active_entry as _find_active_entry,
    validate_days as _validate_days,
    days_display as _days_display,
    VALID_DAY_LETTERS as _VALID_DAY_LETTERS,
)


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Route:
    """Declarative HTTP route definition.

    Each route describes a URL pattern, the HTTP method it responds to,
    and the handler method (by name) to dispatch to.  Flags control
    authentication, device identifier resolution, URL decoding, and
    parameter type coercion.

    Attributes:
        method:         HTTP method (``"GET"``, ``"POST"``, ``"DELETE"``).
        pattern:        URL path segments.  Literal strings must match
                        exactly; ``{name}`` placeholders capture the
                        segment value into a positional arg for the handler.
        handler:        Name of the handler method on the request handler
                        class (looked up via ``getattr``).
        requires_auth:  If ``False``, skip authentication (e.g. dashboard).
        device_param:   Placeholder name to resolve and validate as a
                        device identifier (URL-decode → resolve → validate).
        unquote_params: Placeholder names to URL-decode before dispatch.
        param_types:    Placeholder names mapped to a callable for type
                        coercion (e.g. ``{"index": int}``).  Coercion
                        failure returns HTTP 400.
    """

    method: str
    pattern: tuple[str, ...]
    handler: str
    requires_auth: bool = True
    device_param: Optional[str] = None
    unquote_params: tuple[str, ...] = ()
    param_types: dict[str, type] = field(default_factory=dict)


# Placeholder prefix/suffix for pattern matching.
_PARAM_OPEN: str = "{"
_PARAM_CLOSE: str = "}"

# All API routes.  Order within a (method, segment-count) bucket matters
# only when patterns could overlap — currently none do.
_ROUTES: tuple[_Route, ...] = (
    # -- Pre-auth routes -----------------------------------------------------
    _Route("GET", ("dashboard",),
           "_handle_get_dashboard", requires_auth=False),
    _Route("GET", ("home",),
           "_handle_get_home", requires_auth=False),
    _Route("GET", ("api", "home", "photos"),
           "_handle_get_home_photos", requires_auth=False),
    _Route("GET", ("api", "home", "lights"),
           "_handle_get_home_lights", requires_auth=False),
    _Route("GET", ("api", "home", "locks"),
           "_handle_get_home_locks", requires_auth=False),
    _Route("GET", ("api", "home", "security"),
           "_handle_get_home_security", requires_auth=False),
    _Route("GET", ("api", "home", "cameras"),
           "_handle_get_home_cameras", requires_auth=False),
    _Route("GET", ("api", "home", "camera", "{channel}"),
           "_handle_get_home_camera_snapshot", requires_auth=False),
    _Route("GET", ("api", "home", "occupancy"),
           "_handle_get_home_occupancy", requires_auth=False),
    _Route("GET", ("api", "home", "mode"),
           "_handle_get_home_mode", requires_auth=False),
    _Route("GET", ("api", "home", "printer"),
           "_handle_get_home_printer", requires_auth=False),
    _Route("GET", ("api", "home", "soil"),
           "_handle_get_home_soil", requires_auth=False),
    _Route("GET", ("power",),
           "_handle_get_power_page", requires_auth=False),
    _Route("GET", ("api", "power", "readings"),
           "_handle_get_power_readings", requires_auth=False),
    _Route("GET", ("api", "power", "summary"),
           "_handle_get_power_summary", requires_auth=False),
    _Route("GET", ("api", "power", "devices"),
           "_handle_get_power_devices", requires_auth=False),
    _Route("GET", ("api", "operators"),
           "_handle_get_operators", requires_auth=True),
    _Route("GET", ("photos", "{filename}"),
           "_handle_get_photo", requires_auth=False),

    # -- GET: static ---------------------------------------------------------
    _Route("GET", ("api", "status"),
           "_handle_get_status"),
    _Route("GET", ("api", "devices"),
           "_handle_get_devices"),
    _Route("GET", ("api", "effects"),
           "_handle_get_effects"),
    _Route("GET", ("api", "groups"),
           "_handle_get_groups"),
    _Route("GET", ("api", "schedule"),
           "_handle_get_schedule"),
    _Route("GET", ("api", "media", "sources"),
           "_handle_get_media_sources"),
    _Route("GET", ("api", "media", "signals"),
           "_handle_get_media_signals"),
    _Route("GET", ("api", "media", "stream", "{source_name}"),
           "_handle_get_media_stream",
           requires_auth=False,
           unquote_params=("source_name",)),
    _Route("GET", ("api", "calibrate", "time_sync"),
           "_handle_get_calibrate_time_sync",
           requires_auth=False),
    _Route("POST", ("api", "calibrate", "start", "{device_id}"),
           "_handle_post_calibrate_start",
           device_param="device_id"),
    _Route("POST", ("api", "calibrate", "result", "{device_id}"),
           "_handle_post_calibrate_result",
           device_param="device_id"),
    _Route("GET", ("api", "fleet"),
           "_handle_get_fleet"),
    _Route("GET", ("api", "diagnostics", "now_playing"),
           "_handle_get_diag_now_playing"),
    _Route("GET", ("api", "diagnostics", "history"),
           "_handle_get_diag_history"),
    _Route("GET", ("api", "state"),
           "_handle_get_state"),
    _Route("GET", ("api", "discovered_bulbs"),
           "_handle_get_discovered_bulbs"),
    _Route("GET", ("api", "registry"),
           "_handle_get_registry"),
    _Route("GET", ("api", "command", "discover"),
           "_handle_get_command_discover"),
    _Route("GET", ("api", "command", "identify", "cancel-all"),
           "_handle_get_command_identify_cancel_all"),

    # -- GET: device ---------------------------------------------------------
    _Route("GET", ("api", "devices", "{id}", "status"),
           "_handle_get_device_status", device_param="id"),
    _Route("GET", ("api", "devices", "{id}", "colors"),
           "_handle_get_device_colors", device_param="id"),
    _Route("GET", ("api", "devices", "{id}", "colors", "stream"),
           "_handle_get_device_colors_stream", device_param="id"),

    # -- POST: device --------------------------------------------------------
    _Route("POST", ("api", "devices", "{id}", "play"),
           "_handle_post_play", device_param="id"),
    _Route("POST", ("api", "devices", "{id}", "stop"),
           "_handle_post_stop", device_param="id"),
    _Route("POST", ("api", "devices", "{id}", "power"),
           "_handle_post_power", device_param="id"),
    _Route("POST", ("api", "devices", "{id}", "brightness"),
           "_handle_post_brightness", device_param="id"),
    _Route("POST", ("api", "devices", "{id}", "identify"),
           "_handle_post_identify", device_param="id"),
    _Route("POST", ("api", "devices", "{id}", "resume"),
           "_handle_post_resume", device_param="id"),
    _Route("POST", ("api", "devices", "{id}", "reintrospect"),
           "_handle_post_reintrospect", device_param="id"),
    _Route("POST", ("api", "devices", "{id}", "reset"),
           "_handle_post_reset", device_param="id"),
    _Route("POST", ("api", "devices", "{id}", "nickname"),
           "_handle_post_nickname", device_param="id"),

    # -- POST: parameterized -------------------------------------------------
    _Route("POST", ("api", "effects", "{name}", "defaults"),
           "_handle_post_effect_defaults"),
    _Route("POST", ("api", "groups"),
           "_handle_post_group_create"),
    _Route("PUT", ("api", "groups", "{name}"),
           "_handle_put_group_update"),
    _Route("POST", ("api", "schedule"),
           "_handle_post_schedule_create"),
    _Route("POST", ("api", "schedule", "{index}", "enabled"),
           "_handle_post_schedule_enabled",
           param_types={"index": int}),
    _Route("PUT", ("api", "schedule", "{index}"),
           "_handle_put_schedule_entry",
           param_types={"index": int}),
    _Route("POST", ("api", "media", "sources", "{name}", "start"),
           "_handle_post_media_source_start"),
    _Route("POST", ("api", "media", "sources", "{name}", "stop"),
           "_handle_post_media_source_stop"),
    _Route("POST", ("api", "assign", "{node_id}", "cancel", "{assignment_id}"),
           "_handle_post_cancel_assignment"),

    # -- POST: static --------------------------------------------------------
    _Route("POST", ("api", "media", "signals", "ingest"),
           "_handle_post_signal_ingest"),
    _Route("POST", ("api", "assign"),
           "_handle_post_assign"),
    _Route("POST", ("api", "registry", "device"),
           "_handle_post_registry_device"),
    _Route("POST", ("api", "registry", "push-labels"),
           "_handle_post_registry_push_labels"),
    _Route("POST", ("api", "registry", "push-label"),
           "_handle_post_registry_push_label"),
    _Route("POST", ("api", "command", "identify"),
           "_handle_post_command_identify"),
    _Route("POST", ("api", "server", "power-off-all"),
           "_handle_post_server_power_off_all"),
    _Route("POST", ("api", "server", "rediscover"),
           "_handle_post_server_rediscover"),

    # -- DELETE --------------------------------------------------------------
    _Route("DELETE", ("api", "registry", "device", "{mac}"),
           "_handle_delete_registry_device",
           unquote_params=("mac",)),
    _Route("DELETE", ("api", "command", "identify", "{id}"),
           "_handle_delete_command_identify",
           device_param="id", unquote_params=("id",)),
    _Route("DELETE", ("api", "schedule", "{index}"),
           "_handle_delete_schedule_entry",
           unquote_params=("index",),
           param_types={"index": int}),
    _Route("DELETE", ("api", "groups", "{name}"),
           "_handle_delete_group",
           unquote_params=("name",)),
    # BLE sensor data — no auth, read-only ambient data for displays.
    _Route("GET", ("api", "ble", "sensors"),
           "_handle_get_ble_sensors", requires_auth=False),
    _Route("GET", ("api", "ble", "sensors", "{label}"),
           "_handle_get_ble_sensor_detail",
           unquote_params=("label",), requires_auth=False),
    _Route("PUT", ("api", "ble", "sensors", "{label}", "location"),
           "_handle_put_sensor_location",
           unquote_params=("label",)),
    # Automations — sensor-driven light rules with full CRUD.
    _Route("GET", ("api", "automations"),
           "_handle_get_automations"),
    _Route("POST", ("api", "automations"),
           "_handle_post_automation_create"),
    _Route("PUT", ("api", "automations", "{index}"),
           "_handle_put_automation",
           param_types={"index": int}),
    _Route("POST", ("api", "automations", "{index}", "enabled"),
           "_handle_post_automation_enabled",
           param_types={"index": int}),
    _Route("DELETE", ("api", "automations", "{index}"),
           "_handle_delete_automation",
           unquote_params=("index",),
           param_types={"index": int}),
)

# Pre-built index: (method, segment_count) → list of candidate routes.
# Narrows per-request matching to a handful of candidates.
_ROUTE_INDEX: dict[tuple[str, int], list[_Route]] = {}
for _r in _ROUTES:
    _key = (_r.method, len(_r.pattern))
    _ROUTE_INDEX.setdefault(_key, []).append(_r)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Utilities — imported from server_utils.py
# ---------------------------------------------------------------------------

from server_utils import (
    validate_ip as _validate_ip,
    is_group_id as _is_group_id,
    group_name_from_id as _group_name_from_id,
    group_id_from_name as _group_id_from_name,
    is_grid_id as _is_grid_id,
    grid_name_from_id as _grid_name_from_id,
    grid_id_from_name as _grid_id_from_name,
    validate_device_id as _validate_device_id,
    RateLimiter as _RateLimiter,
    get_groups as _get_groups,
)

# Singleton rate limiter — shared across all handler threads.
_rate_limiter: _RateLimiter = _RateLimiter(
    max_failures=AUTH_RATE_LIMIT, window_seconds=AUTH_RATE_WINDOW,
)



# Device manager — extracted to device_manager.py
from device_manager import DeviceManager


# Scheduler thread — extracted to scheduling/scheduler_thread.py
from scheduling import SchedulerThread


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

from handlers import (
    DeviceHandlerMixin, GroupHandlerMixin, SensorHandlerMixin,
    ScheduleHandlerMixin, MediaHandlerMixin, DiscoveryHandlerMixin,
    RegistryHandlerMixin, DashboardHandlerMixin, CalibrationHandlerMixin,
    DistributedHandlerMixin, DiagnosticsHandlerMixin, StaticHandlerMixin,
)


class GlowUpRequestHandler(
    DeviceHandlerMixin,
    GroupHandlerMixin,
    SensorHandlerMixin,
    ScheduleHandlerMixin,
    MediaHandlerMixin,
    DiscoveryHandlerMixin,
    RegistryHandlerMixin,
    DashboardHandlerMixin,
    CalibrationHandlerMixin,
    DistributedHandlerMixin,
    DiagnosticsHandlerMixin,
    StaticHandlerMixin,
    http.server.BaseHTTPRequestHandler,
):
    """HTTP request handler for the GlowUp REST API.

    Class-level attributes are set before the server starts:

    Attributes:
        device_manager: Shared :class:`DeviceManager`.
        auth_token:     Expected bearer token for authentication.
        scheduler:      The :class:`SchedulerThread` (for override tracking).
        config:         Parsed server configuration dict.
    """

    # These are set by main() before the server starts.
    device_manager: DeviceManager
    auth_token: str
    scheduler: Optional[SchedulerThread] = None
    config: dict[str, Any] = {}
    config_path: Optional[str] = None
    media_manager: Optional[MediaManager] = None
    automation_manager: Optional[AutomationManager] = None
    orchestrator: Optional[Any] = None
    keepalive: Optional[BulbKeepAlive] = None
    registry: Optional[DeviceRegistry] = None
    operator_manager: Optional[OperatorManager] = None
    lock_manager: Optional[Any] = None
    ble_adapter: Optional[Any] = None
    signal_bus: Optional[SignalBus] = None
    power_logger: Optional[Any] = None

    # Active /api/command/identify pulses: {ip: stop_event}.
    # Populated by _handle_post_command_identify; cleared when pulse ends.
    # DELETE /api/command/identify/{ip} sets the event to cancel early.
    _command_identifies: dict[str, threading.Event] = {}
    _command_identifies_lock: threading.Lock = threading.Lock()

    # Guards config file read-modify-write in _save_config_field().
    # Without this, concurrent saves on different keys clobber each other.
    _config_save_lock: threading.Lock = threading.Lock()

    # Silence per-request logging from BaseHTTPRequestHandler.
    def log_message(self, format: str, *args: Any) -> None:
        """Route HTTP access logs through the logging module.

        Args:
            format: Format string.
            *args:  Format arguments.
        """
        logging.debug("HTTP %s", format % args)

    # -- Device identifier resolution ----------------------------------------

    def _resolve_device_id(self, identifier: str) -> Optional[str]:
        """Resolve a device identifier to an internal IP or group key.

        Accepts any of:
        - Raw IP address — returned as-is.
        - ``group:name`` — returned as-is.
        - Registry label (e.g. ``PORCH STRING LIGHTS``) — resolved
          via registry label→MAC, then keepalive MAC→IP.
        - MAC address (e.g. ``d0:73:d5:69:70:db``) — resolved via
          keepalive MAC→IP.

        Args:
            identifier: Raw device identifier from the URL path.

        Returns:
            The resolved IP address or ``group:name`` string, or
            ``None`` if the identifier cannot be resolved.
        """
        # Group and grid identifiers pass through unchanged.
        if _is_group_id(identifier):
            return identifier if len(identifier) > len(GROUP_PREFIX) else None
        if _is_grid_id(identifier):
            return identifier if len(identifier) > len(GRID_PREFIX) else None

        # Raw IP addresses pass through unchanged.
        if _validate_ip(identifier):
            return identifier

        # Try registry + keepalive resolution (label or MAC → IP).
        reg: Optional[DeviceRegistry] = self.registry
        ka: Optional[BulbKeepAlive] = self.keepalive
        if reg is not None and ka is not None:
            ip: Optional[str] = reg.resolve_to_ip(identifier, ka)
            if ip is not None:
                return ip

        return None

    # -- Authentication -----------------------------------------------------

    def _authenticate(self) -> bool:
        """Check the Authorization header for a valid bearer token.

        Sends a 401 or 429 response if authentication fails.
        Rate-limits clients that repeatedly fail authentication.

        Returns:
            ``True`` if the request is authenticated, ``False`` otherwise.
        """
        client_ip: str = self.client_address[0]

        # Check rate limit before processing the token.
        if _rate_limiter.is_blocked(client_ip):
            self._send_json(429, {"error": "Too many failed attempts"})
            return False

        auth: Optional[str] = self.headers.get(AUTH_HEADER)
        if auth is None or not auth.startswith(BEARER_PREFIX):
            _rate_limiter.record_failure(client_ip)
            self._send_json(401, {"error": "Missing or invalid token"})
            self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            return False

        token: str = auth[len(BEARER_PREFIX):]
        if not hmac.compare_digest(token, self.auth_token):
            _rate_limiter.record_failure(client_ip)
            self._send_json(401, {"error": "Invalid token"})
            return False

        # Successful auth — clear any prior failures.
        _rate_limiter.clear(client_ip)
        return True

    # -- JSON helpers -------------------------------------------------------

    def _read_json_body(self) -> Optional[dict[str, Any]]:
        """Read and parse the JSON request body.

        Sends a 400 response if the body is missing, too large, or
        not valid JSON.

        Returns:
            The parsed dict, or ``None`` on error.
        """
        length_str: Optional[str] = self.headers.get("Content-Length")
        if length_str is None:
            self._send_json(400, {"error": "Missing Content-Length"})
            return None

        try:
            length: int = int(length_str)
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length"})
            return None

        if length > MAX_REQUEST_BODY:
            self._send_json(413, {"error": "Request body too large"})
            return None

        raw: bytes = self.rfile.read(length)
        try:
            body: Any = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON"})
            return None

        if not isinstance(body, dict):
            self._send_json(400, {"error": "Expected JSON object"})
            return None

        return body

    def _send_json(self, code: int, data: Any) -> None:
        """Send a JSON response with security headers.

        Args:
            code: HTTP status code.
            data: JSON-serializable response data.
        """
        body: bytes = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_headers(self) -> None:
        """Send HTTP headers for a Server-Sent Events stream."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._send_security_headers()
        self.end_headers()

    # -- Security headers ---------------------------------------------------

    def _send_security_headers(self) -> None:
        """Append standard security headers to the current response.

        Called by :meth:`_send_json` and :meth:`_send_sse_headers` so
        every response carries a consistent security posture.
        """
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Strict-Transport-Security",
                         f"max-age={HSTS_MAX_AGE_SECONDS}; includeSubDomains")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'")

    # -- Routing ------------------------------------------------------------

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight requests.

        Requires authentication to prevent unauthenticated endpoint
        enumeration.  The iOS app does not use CORS (native HTTP), so
        this is only needed for browser-based clients.
        """
        if not self._authenticate():
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "3600")
        self._send_security_headers()
        self.end_headers()

    def do_GET(self) -> None:
        """Route GET requests to the appropriate handler."""
        self._dispatch("GET")

    def do_POST(self) -> None:
        """Route POST requests to the appropriate handler."""
        self._dispatch("POST")

    def do_PUT(self) -> None:
        """Route PUT requests to the appropriate handler."""
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        """Route DELETE requests to the appropriate handler."""
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        """Match the request path against the route table and dispatch.

        Handles authentication, device identifier resolution, URL
        decoding, and parameter type coercion based on the matched
        route's flags.  Sends 404 if no route matches.

        Args:
            method: HTTP method (``"GET"``, ``"POST"``, ``"DELETE"``).
        """
        path: str = self.path.split("?")[0]
        parts: list[str] = path.strip("/").split("/")
        n_parts: int = len(parts)

        # Look up candidate routes by (method, segment count).
        candidates: list[_Route] = _ROUTE_INDEX.get((method, n_parts), [])

        for route in candidates:
            # Match literal segments; collect placeholder values.
            params: dict[str, str] = {}
            matched: bool = True
            for seg, pat in zip(parts, route.pattern):
                if pat.startswith(_PARAM_OPEN) and pat.endswith(_PARAM_CLOSE):
                    # Placeholder — capture the value.
                    param_name: str = pat[1:-1]
                    params[param_name] = seg
                elif seg != pat:
                    matched = False
                    break

            if not matched:
                continue

            # --- Route matched ---

            # Authentication gate.
            if route.requires_auth and not self._authenticate():
                return

            # URL-decode specified params.
            for pname in route.unquote_params:
                if pname in params:
                    params[pname] = unquote(params[pname])

            # Device identifier resolution and validation.
            if route.device_param is not None:
                dp: str = route.device_param
                raw: str = params.get(dp, "")
                # URL-decode if not already handled by unquote_params.
                if dp not in route.unquote_params:
                    raw = unquote(raw)
                # Resolve labels/MACs to internal IPs.
                resolved: Optional[str] = self._resolve_device_id(raw)
                if resolved is not None:
                    raw = resolved
                # Validate.
                if not _validate_device_id(raw):
                    self._send_json(400, {"error": DEVICE_RESOLVE_ERROR})
                    return
                params[dp] = raw

            # Type coercion for non-string params.
            for pname, ptype in route.param_types.items():
                try:
                    params[pname] = ptype(params[pname])
                except (ValueError, TypeError):
                    self._send_json(
                        400,
                        {"error": f"Invalid {pname}: {params[pname]!r}"},
                    )
                    return

            # Collect handler args in pattern order.
            handler_args: list[Any] = [
                params[pat[1:-1]]
                for pat in route.pattern
                if pat.startswith(_PARAM_OPEN) and pat.endswith(_PARAM_CLOSE)
            ]

            # Dispatch with error boundary — return 500 JSON on crash.
            handler_fn: Callable = getattr(self, route.handler)
            try:
                handler_fn(*handler_args)
            except Exception as exc:
                logging.exception(
                    "Handler %s crashed: %s", route.handler, exc,
                )
                try:
                    self._send_json(500, {
                        "error": f"Internal error: {type(exc).__name__}: {exc}",
                    })
                except Exception:
                    pass  # Response already started or connection dead.
            return

        # No route matched.
        self._send_json(404, {"error": "Not found"})

    # -- GET handlers -------------------------------------------------------

    # ------------------------------------------------------------------
    # BLE sensor endpoints
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Automation endpoints
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------

    # -- POST handlers ------------------------------------------------------

    # -- Media handlers -----------------------------------------------------

    # -- Calibration handlers -------------------------------------------------

    # -- Fleet handler ---------------------------------------------------------

    # -- Diagnostics endpoints ----------------------------------------------

    # -- Registry handlers ---------------------------------------------------

    # -- Command handlers --------------------------------------------------

    # -- Helpers ------------------------------------------------------------

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """HTTP server that handles each request in a new thread.

    Required for SSE: long-lived streaming connections must not block
    other requests from being served.
    """

    daemon_threads: bool = True
    allow_reuse_address: bool = True


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_config(config_path: str) -> dict[str, Any]:
    """Load and validate the server configuration file.

    The config must contain ``port``, ``auth_token``, and ``groups``
    sections.  Group entries may be registry labels, MAC addresses, or
    raw IP addresses — they are resolved to live IPs at startup via
    :func:`_resolve_config_groups`.  The ``schedule`` and ``location``
    sections are optional (server works without a schedule in
    API-only mode).

    **Schedule file support:** If ``schedule_file`` is present in the
    config, the schedule, location, and groups are loaded from that
    external file and merged into the server config.  This allows a
    single ``schedule.json`` to be shared between the server and the
    standalone ``scheduler.py``.  Schedule-file groups are added to
    (not replacing) any groups already in ``server.json``.

    Args:
        config_path: Path to the JSON configuration file.

    Returns:
        The parsed configuration dictionary.

    Raises:
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
        ValueError: If required fields are missing or invalid.
    """
    with open(config_path, "r") as f:
        config: dict[str, Any] = json.load(f)

    # --- Merge external schedule file if referenced ---
    # This is the unification point: schedule.json is the single source
    # of truth for schedule entries, location, and schedule-specific
    # groups.  Both scheduler.py and the server read the same file.
    schedule_file: Optional[str] = config.get("schedule_file")
    if schedule_file:
        schedule_path: str = schedule_file
        # Resolve relative paths against the config file's directory.
        if not os.path.isabs(schedule_path):
            config_dir: str = os.path.dirname(os.path.abspath(config_path))
            schedule_path = os.path.join(config_dir, schedule_path)
        if not os.path.exists(schedule_path):
            raise FileNotFoundError(
                f"schedule_file '{schedule_path}' not found "
                f"(referenced from {config_path})"
            )
        with open(schedule_path, "r") as sf:
            sched_config: dict[str, Any] = json.load(sf)
        # Store resolved path for live schedule editing via API.
        config["_schedule_path"] = schedule_path
        logging.info(
            "Loaded schedule from external file: %s", schedule_path,
        )
        # Merge location (schedule file wins if server.json doesn't have one).
        if "location" not in config and "location" in sched_config:
            config["location"] = sched_config["location"]
        # Merge schedule entries (schedule file is the source of truth).
        if "schedule" in sched_config:
            config["schedule"] = sched_config["schedule"]
        # Merge groups — schedule file groups are added to server groups.
        # Schedule groups may use IPs, labels, or MACs; the server's
        # resolution chain handles all three.
        if "groups" in sched_config:
            if "groups" not in config:
                config["groups"] = {}
            for gname, entries in sched_config["groups"].items():
                if gname.startswith("_"):
                    continue
                if gname not in config["groups"]:
                    config["groups"][gname] = entries
                else:
                    # Merge: add any devices from schedule file not already
                    # in the server group (no duplicates).
                    existing: set[str] = set(config["groups"][gname])
                    for entry in entries:
                        if entry not in existing:
                            config["groups"][gname].append(entry)

    # Validate auth token.
    token: Any = config.get("auth_token")
    if not token or not isinstance(token, str) or token == "CHANGE_ME":
        raise ValueError(
            "Config must contain a non-default 'auth_token' string.  "
            "Generate one with: python3 -c "
            "\"import secrets; print(secrets.token_urlsafe(32))\""
        )

    # Validate port.
    port: Any = config.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or port < 1 or port > 65535:
        raise ValueError(f"'port' must be 1-65535, got {port!r}")
    config["port"] = port

    # Location is required if schedule is present.
    if "schedule" in config and config["schedule"]:
        if "location" not in config:
            raise ValueError("Config missing 'location' (required for schedule)")
        loc: dict = config["location"]
        if "latitude" not in loc or "longitude" not in loc:
            raise ValueError(
                "Config location must have 'latitude' and 'longitude'"
            )

    # Validate groups — required, since the server has no other source
    # of devices (broadcast discovery has been removed).  Entries may
    # be registry labels, MAC addresses, or raw IP addresses — they
    # are resolved to live IPs in _background_startup().
    if "groups" not in config or not config["groups"]:
        raise ValueError(
            "Config must contain a non-empty 'groups' section with "
            "device identifiers (labels, MACs, or IPs).  The server "
            "does not perform broadcast discovery — all devices must "
            "be listed explicitly."
        )
    groups: dict[str, list[str]] = config["groups"]
    empty_groups: list[str] = []
    for group_name, entries in list(groups.items()):
        if group_name.startswith("_"):
            continue
        if not isinstance(entries, list):
            raise ValueError(
                f"Group '{group_name}' must be a list of device "
                f"identifiers (labels, MACs, or IPs)"
            )
        # Validate that every entry is a non-empty string.
        for i, entry in enumerate(entries):
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(
                    f"Group '{group_name}' entry {i} must be a "
                    f"non-empty string, got {entry!r}"
                )
        if not entries:
            logging.warning("Group '%s' is empty — skipping", group_name)
            empty_groups.append(group_name)
    # Remove empty groups so downstream code never encounters them.
    for name in empty_groups:
        del groups[name]

    # After pruning, check that at least one non-comment group remains.
    real_groups: list[str] = [
        k for k in groups if not k.startswith("_")
    ]
    if not real_groups:
        raise ValueError(
            "Config must contain a non-empty 'groups' section with "
            "device identifiers (labels, MACs, or IPs).  The server "
            "does not perform broadcast discovery — all devices must "
            "be listed explicitly."
        )

    if "schedule" in config:
        known_groups: set[str] = set()
        if "groups" in config:
            known_groups = {
                k for k in config["groups"] if not k.startswith("_")
            }
        for i, entry in enumerate(config["schedule"]):
            label: str = entry.get("name", f"entry_{i}")
            for req_field in ("start", "stop", "effect", "group"):
                if req_field not in entry:
                    raise ValueError(
                        f"Schedule entry '{label}' missing '{req_field}'"
                    )
            if known_groups and entry["group"] not in known_groups:
                raise ValueError(
                    f"Schedule entry '{label}' references unknown group "
                    f"'{entry['group']}'"
                )
            days: str = entry.get("days", "")
            if days and not _validate_days(days):
                raise ValueError(
                    f"Schedule entry '{label}' has invalid 'days' value "
                    f"'{days}' — use letters from MTWRFSU (no repeats)"
                )

    # Validate optional MQTT section.
    if "mqtt" in config:
        mqtt_cfg: Any = config["mqtt"]
        if not isinstance(mqtt_cfg, dict):
            raise ValueError("'mqtt' must be a JSON object")
        mqtt_port: Any = mqtt_cfg.get("port", 1883)
        if not isinstance(mqtt_port, int) or mqtt_port < 1 or mqtt_port > 65535:
            raise ValueError(
                f"mqtt 'port' must be 1-65535, got {mqtt_port!r}"
            )
        mqtt_prefix: Any = mqtt_cfg.get("topic_prefix", "glowup")
        if not isinstance(mqtt_prefix, str) or not mqtt_prefix:
            raise ValueError("mqtt 'topic_prefix' must be a non-empty string")
        color_interval: Any = mqtt_cfg.get("color_interval", 1.0)
        if not isinstance(color_interval, (int, float)) or color_interval <= 0:
            raise ValueError(
                f"mqtt 'color_interval' must be a positive number, "
                f"got {color_interval!r}"
            )

    return config


def _get_groups(config: dict[str, Any]) -> dict[str, list[str]]:
    """Extract device groups from config, excluding comment keys.

    Args:
        config: Parsed configuration dictionary.

    Returns:
        A dict mapping group names to lists of identifiers (labels,
        MACs, or IPs).
    """
    groups: dict = config.get("groups", {})
    return {
        name: entries
        for name, entries in groups.items()
        if not name.startswith("_")
    }


def _resolve_config_groups(
    raw_groups: dict[str, list[str]],
    registry: "DeviceRegistry",
    keepalive: "BulbKeepAlive",
) -> tuple[dict[str, list[str]], list[str], list[tuple[str, str]]]:
    """Resolve config group entries to live IP addresses.

    Each entry in a group list may be a registry label, a MAC address,
    or a raw IP address.  Labels are resolved via the device registry
    to a MAC, then via the keepalive daemon's ARP table to a live IP.
    MACs go straight to ARP lookup.  IPs pass through unchanged.

    Args:
        raw_groups:  Group name to list-of-identifiers mapping from
                     the config file.
        registry:    Loaded :class:`DeviceRegistry` instance.
        keepalive:   Running :class:`BulbKeepAlive` instance whose
                     initial ARP scan has completed.

    Returns:
        A three-tuple of ``(resolved_groups, device_ips, unresolved)``
        where:

        - *resolved_groups* maps group name to a list of resolved IPs
          (entries that could not be resolved are omitted).
        - *device_ips* is a flat, sorted, de-duplicated list of every
          resolved IP across all groups.
        - *unresolved* is a list of ``(group_name, identifier)`` pairs
          for entries that could not be resolved (offline, not in
          registry, etc.).
    """
    from device_registry import IP_PATTERN

    resolved_groups: dict[str, list[str]] = {}
    all_ips: set[str] = set()
    unresolved: list[tuple[str, str]] = []

    for group_name, entries in raw_groups.items():
        resolved: list[str] = []
        for ident in entries:
            ip: Optional[str] = registry.resolve_to_ip(ident, keepalive)
            if ip is not None:
                resolved.append(ip)
                all_ips.add(ip)
            else:
                unresolved.append((group_name, ident))
        resolved_groups[group_name] = resolved

    return resolved_groups, sorted(all_ips), unresolved


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _log_sun_times(sun: SunTimes, d: date) -> None:
    """Log computed solar event times for a date.

    Args:
        sun: Computed solar event times.
        d:   Calendar date.
    """
    fmt: str = "%H:%M"
    logging.info("Sun times for %s:", d)
    logging.info(
        "  Dawn:    %s", sun.dawn.strftime(fmt) if sun.dawn else "N/A",
    )
    logging.info(
        "  Sunrise: %s", sun.sunrise.strftime(fmt) if sun.sunrise else "N/A",
    )
    logging.info("  Noon:    %s", sun.noon.strftime(fmt))
    logging.info(
        "  Sunset:  %s", sun.sunset.strftime(fmt) if sun.sunset else "N/A",
    )
    logging.info(
        "  Dusk:    %s", sun.dusk.strftime(fmt) if sun.dusk else "N/A",
    )


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

def _dry_run(config: dict[str, Any]) -> None:
    """Print the resolved schedule without running any effects.

    Args:
        config: Parsed configuration dictionary.
    """
    if "location" not in config:
        print("No location configured — nothing to preview.")
        return
    if "schedule" not in config or not config["schedule"]:
        print("No schedule entries — nothing to preview.")
        return

    lat: float = config["location"]["latitude"]
    lon: float = config["location"]["longitude"]
    groups: dict[str, list[str]] = _get_groups(config)
    specs: list[dict[str, Any]] = config["schedule"]

    now: datetime = datetime.now(timezone.utc).astimezone()
    utc_offset: timedelta = now.utcoffset()
    today: date = now.date()

    sun: SunTimes = sun_times(lat, lon, today, utc_offset)

    print(f"Location:   {lat}°N, {lon}°E")
    print(f"Date:       {today}")
    print(f"UTC offset: {utc_offset}")
    print(f"Server:     localhost:{config.get('port', DEFAULT_PORT)}")
    print()

    print("Solar events:")
    fmt: str = "%H:%M:%S"
    print(f"  Dawn:    {sun.dawn.strftime(fmt) if sun.dawn else 'N/A'}")
    print(f"  Sunrise: {sun.sunrise.strftime(fmt) if sun.sunrise else 'N/A'}")
    print(f"  Noon:    {sun.noon.strftime(fmt)}")
    print(f"  Sunset:  {sun.sunset.strftime(fmt) if sun.sunset else 'N/A'}")
    print(f"  Dusk:    {sun.dusk.strftime(fmt) if sun.dusk else 'N/A'}")
    print()

    if groups:
        print("Device groups:")
        for group_name in sorted(groups):
            ips: list[str] = groups[group_name]
            print(f"  {group_name}: {', '.join(ips)}")
        print()

    for group_name in sorted(groups):
        resolved = _resolve_entries(
            specs, lat, lon, today, utc_offset, group_filter=group_name,
        )
        print(f"Schedule — {group_name}:")
        if not resolved:
            print("  (no entries)")
            print()
            continue

        for start, stop, spec in resolved:
            status: str = ""
            if start <= now < stop:
                status = " <-- ACTIVE NOW"
            stop_str: str = (
                stop.strftime("%m/%d %H:%M")
                if stop.date() != start.date()
                else stop.strftime("%H:%M")
            )
            days_str: str = ""
            days_raw: str = spec.get("days", "")
            if days_raw:
                days_str = f"  ({_days_display(days_raw)})"
            params_str: str = ""
            if spec.get("params"):
                params_str = f"  params: {spec['params']}"
            print(
                f"  {spec.get('name', '?'):20s}  "
                f"{start.strftime('%H:%M')} -> {stop_str}  "
                f"[{spec['effect']}]{days_str}{params_str}{status}"
            )
        print()


# ---------------------------------------------------------------------------
# CLI and main
# ---------------------------------------------------------------------------

def _build_parser() -> "argparse.ArgumentParser":
    """Build the command-line argument parser.

    Returns:
        A configured argument parser.
    """
    parser = argparse.ArgumentParser(
        prog="glowup-server",
        description="GlowUp REST API server — remote control daemon "
                    "for LIFX devices with optional scheduling",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to server config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resolved schedule and exit without running",
    )
    parser.add_argument(
        "--lerp",
        type=str,
        default=None,
        choices=["lab", "hsb"],
        help="Color interpolation method: lab (perceptually uniform) "
             "or hsb (cheap). Overrides config file. Default: lab",
    )
    return parser


def _migrate_automations_to_triggers(config: dict[str, Any]) -> None:
    """Auto-migrate ``automations`` list to trigger-type operators.

    If the config has an ``automations`` section but no trigger-type entries
    in ``operators``, converts each automation into a trigger operator config
    and appends it to the ``operators`` list.  The ``automations`` key is
    removed after migration.

    Args:
        config: Server config dict (modified in place).
    """
    automations: list[dict] = config.get("automations", [])
    if not automations:
        return

    # Check if trigger operators already exist.
    operators: list[dict] = config.get("operators", [])
    has_triggers: bool = any(
        op.get("type") == "trigger" for op in operators
    )
    if has_triggers:
        # Already migrated — don't duplicate.
        return

    migrated: int = 0
    for auto in automations:
        name: str = auto.get("name", f"migrated_{migrated}")
        trigger_entry: dict[str, Any] = {
            "type": "trigger",
            "name": name,
            "enabled": auto.get("enabled", True),
            "sensor": auto.get("sensor", {}),
            "trigger": auto.get("trigger", {}),
            "action": auto.get("action", {}),
            "off_trigger": auto.get("off_trigger", {}),
            "off_action": auto.get("off_action", {}),
            "schedule_conflict": auto.get("schedule_conflict", "defer"),
        }
        operators.append(trigger_entry)
        migrated += 1

    config["operators"] = operators
    # Remove the old automations key — trigger operators are the authority.
    del config["automations"]

    logging.info(
        "Migrated %d automation(s) to trigger operators", migrated,
    )

    # Persist the migration to disk.
    config_path: Optional[str] = config.get("_config_path")
    if not config_path:
        return
    try:
        with open(config_path, "r") as f:
            disk_cfg: dict = json.load(f)
        disk_cfg["operators"] = operators
        disk_cfg.pop("automations", None)
        with open(config_path, "w") as f:
            json.dump(disk_cfg, f, indent=4)
            f.write("\n")
        logging.info("Persisted trigger migration to %s", config_path)
    except Exception as exc:
        logging.warning("Failed to persist trigger migration: %s", exc)


def main() -> None:
    """Entry point for the GlowUp REST API server."""
    parser: argparse.ArgumentParser = _build_parser()
    args: argparse.Namespace = parser.parse_args()

    # Set up logging to stderr (systemd journal captures this).
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )

    config_path: str = args.config
    try:
        config: dict[str, Any] = _load_config(config_path)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        logging.error("Configuration error: %s", exc)
        sys.exit(1)

    # -- Color interpolation method -----------------------------------------
    # CLI --lerp overrides config "lerp" key; default is "lab".
    from colorspace import set_lerp_method
    lerp_method: str = (
        args.lerp
        or config.get("lerp", "lab")
    )
    set_lerp_method(lerp_method)
    logging.info("Color interpolation: %s", lerp_method)

    if args.dry_run:
        _dry_run(config)
        sys.exit(0)

    # -- Device manager -------------------------------------------------------
    # Create the DeviceManager with empty IPs.  Config groups contain
    # labels, MACs, or IPs — these are resolved to live IP addresses
    # in _background_startup() after the keepalive daemon has populated
    # its ARP table.  The DeviceManager is safe to use (returns empty
    # device list) until load_devices() is called.
    nicknames: dict[str, str] = config.get("nicknames", {})
    dm: DeviceManager = DeviceManager(
        device_ips=[], nicknames=nicknames,
        config_dir=os.path.dirname(os.path.abspath(config_path)),
        groups={},
        grids=config.get("grids", {}),
    )

    # -- HTTP server (bind immediately) -------------------------------------
    # Start accepting connections BEFORE device loading so the
    # Cloudflare tunnel (or any health-check) never sees "connection
    # refused".  API requests arriving before loading completes see
    # an empty device list — harmless and self-correcting.
    port: int = config.get("port", DEFAULT_PORT)

    GlowUpRequestHandler.device_manager = dm
    GlowUpRequestHandler.auth_token = config["auth_token"]
    GlowUpRequestHandler.scheduler = None          # patched after start
    GlowUpRequestHandler.config = config
    GlowUpRequestHandler.config_path = config_path

    server: ThreadedHTTPServer = ThreadedHTTPServer(
        ("", port), GlowUpRequestHandler,
    )

    # Graceful shutdown on SIGINT / SIGTERM.
    # server.shutdown() must be called from a different thread than
    # serve_forever() to avoid deadlock, so the signal handler sets
    # an event and a watcher thread performs the actual shutdown.
    shutdown_event: threading.Event = threading.Event()

    def _handle_signal(signum: int, frame: Any) -> None:
        """Signal handler — just sets the event, avoids deadlock."""
        if not shutdown_event.is_set():
            logging.info("Received signal %d, shutting down...", signum)
            shutdown_event.set()

    def _shutdown_watcher() -> None:
        """Background thread that waits for shutdown signal."""
        shutdown_event.wait()
        server.shutdown()

    watcher: threading.Thread = threading.Thread(
        target=_shutdown_watcher, daemon=True,
    )
    watcher.start()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logging.info("GlowUp server v%s listening on port %d", __version__, port)
    logging.info(
        "API base: http://localhost:%d/api/", port,
    )

    # -- Background device loading -------------------------------------------
    # Load devices in a daemon thread so the HTTP server is already
    # accepting connections while we query each device.
    # MQTT bridge reference — set by _background_startup if configured.
    mqtt_bridge: Optional[MqttBridge] = None
    ble_trigger_mgr: Optional[BleTriggerManager] = None
    automation_mgr: Optional[AutomationManager] = None
    # MediaManager reference — set by _background_startup if configured.
    media_mgr: Optional[MediaManager] = None
    # Orchestrator reference — set by _background_startup if configured.
    orch: Optional[Any] = None
    keepalive: Optional[BulbKeepAlive] = None
    # Zigbee/Vivint/Operator/Lock references — set by _background_startup.
    zigbee_adapter: Optional[Any] = None
    vivint_adapter: Optional[Any] = None
    nvr_adapter: Optional[Any] = None
    ble_adpt: Optional[Any] = None
    operator_mgr: Optional[OperatorManager] = None
    lock_mgr: Optional[Any] = None

    def _background_startup() -> None:
        """Resolve config identifiers, load devices, start services.

        Startup order:

        1. Start the keepalive daemon (ARP discovery).
        2. Load the device registry (MAC→label mapping).
        3. Wait for the keepalive's initial ARP scan.
        4. Resolve config group entries (labels/MACs) → live IPs.
        5. Populate DeviceManager and load devices.
        6. Start scheduler, MQTT, orchestrator, media pipeline.
        """
        nonlocal mqtt_bridge, media_mgr, orch, keepalive, ble_trigger_mgr, automation_mgr
        nonlocal zigbee_adapter, vivint_adapter, nvr_adapter, ble_adpt, operator_mgr, lock_mgr

        try:
            # -- Step 1: Start the ARP-based bulb keepalive daemon --------
            # Must run BEFORE device loading so the ARP table is
            # populated for label/MAC → IP resolution.
            keepalive = BulbKeepAlive()
            GlowUpRequestHandler.keepalive = keepalive
            keepalive.start()

            # -- Step 2: Load the device registry -------------------------
            device_reg: DeviceRegistry = DeviceRegistry()
            if device_reg.load():
                logging.info(
                    "Device registry loaded: %d device(s)",
                    device_reg.device_count,
                )
            else:
                logging.info(
                    "No device registry found — labels unavailable "
                    "until devices are registered"
                )
            GlowUpRequestHandler.registry = device_reg

            # -- Step 3: Wait for initial ARP scan ------------------------
            logging.info("Waiting for initial ARP scan...")
            if not keepalive.wait_initial_scan(timeout=30.0):
                logging.warning(
                    "Initial ARP scan timed out — some devices may "
                    "be unresolvable until the next scan"
                )

            # -- Step 4: Resolve config groups ----------------------------
            raw_groups: dict[str, list[str]] = _get_groups(config)
            resolved_groups: dict[str, list[str]]
            device_ips: list[str]
            unresolved: list[tuple[str, str]]
            resolved_groups, device_ips, unresolved = (
                _resolve_config_groups(raw_groups, device_reg, keepalive)
            )

            for group_name, ident in unresolved:
                logging.warning(
                    "Group '%s': cannot resolve '%s' to IP — "
                    "device offline or not in registry",
                    group_name, ident,
                )

            if device_ips:
                logging.info(
                    "Resolved %d device(s) from config groups",
                    len(device_ips),
                )
            else:
                logging.warning(
                    "No devices resolved from config groups — "
                    "check registry and device connectivity"
                )

            # -- Step 4b: Auto-load registered devices not in groups ------
            # Registration implies management.  Any registered device
            # that is online (in ARP) but not already in a config group
            # gets added to the managed set automatically.
            group_ip_set: set[str] = set(device_ips)
            mac_to_ip: dict[str, str] = keepalive.known_bulbs_by_mac
            registered_extras: int = 0
            for mac in device_reg.all_devices():
                ip: Optional[str] = mac_to_ip.get(mac)
                if ip is not None and ip not in group_ip_set:
                    device_ips.append(ip)
                    group_ip_set.add(ip)
                    label: Optional[str] = device_reg.mac_to_label(mac)
                    logging.info(
                        "Auto-loading registered device %s (%s) at %s",
                        label or "?", mac, ip,
                    )
                    registered_extras += 1
            if registered_extras:
                device_ips.sort()
                logging.info(
                    "Added %d registered device(s) not in any group",
                    registered_extras,
                )

            # -- Step 5: Populate DeviceManager and load ------------------
            dm._device_ips = device_ips
            dm._group_config = resolved_groups
            # Wire registry + keepalive so DeviceManager can resolve
            # labels for unreachable (query-silent) devices.
            dm._registry = device_reg
            dm._keepalive = keepalive
            devices: list[dict[str, Any]] = dm.load_devices()
            logging.info("Loaded %d device(s)", len(devices))
            dm.query_all_power_states()

            # Wire keepalive → DeviceManager power state queries.
            # Every 2nd ARP cycle (~2 min), query all device power states.
            keepalive._on_power_query = dm.query_all_power_states
            # On new bulb discovery, query that device's power state.
            _existing_on_new: Optional[Callable] = keepalive._on_new_bulb

            def _on_new_with_power(ip: str, mac: str) -> None:
                if _existing_on_new is not None:
                    _existing_on_new(ip, mac)
                dm.query_power_state(ip)

            keepalive._on_new_bulb = _on_new_with_power

            # Start the scheduler now that devices are available.
            sched: Optional[SchedulerThread] = None
            if config.get("schedule"):
                sched = SchedulerThread(config, dm)
                GlowUpRequestHandler.scheduler = sched
                sched.start()
            else:
                logging.info("No schedule configured — API-only mode")

            # Start the MQTT bridge if configured.
            if config.get("mqtt"):
                if not _MQTT_AVAILABLE:
                    logging.error(
                        "MQTT section found in config but paho-mqtt is "
                        "not installed.  Install with: pip install "
                        "paho-mqtt"
                    )
                else:
                    mqtt_bridge = MqttBridge(
                        dm, config, scheduler=sched,
                    )
                    mqtt_bridge.start()

            # Auto-migrate ble_triggers → automations if needed.
            if migrate_ble_triggers(config):
                config_path_local: Optional[str] = GlowUpRequestHandler.config_path
                if config_path_local:
                    try:
                        with open(config_path_local, "r") as f:
                            disk_cfg: dict = json.load(f)
                        disk_cfg["automations"] = config["automations"]
                        with open(config_path_local, "w") as f:
                            json.dump(disk_cfg, f, indent=4)
                            f.write("\n")
                        logging.info("Persisted migrated automations to %s",
                                     config_path_local)
                    except Exception as exc:
                        logging.warning("Failed to persist migration: %s", exc)

            # Start BLE trigger manager if configured (legacy path).
            ble_trigger_cfg: dict = config.get("ble_triggers", {})
            if ble_trigger_cfg and _MQTT_AVAILABLE:
                mqtt_cfg = config.get("mqtt", {})
                ble_trigger_mgr = BleTriggerManager(
                    config=ble_trigger_cfg,
                    device_manager=dm,
                    broker=mqtt_cfg.get("broker", "localhost"),
                    port=mqtt_cfg.get("port", 1883),
                )
                ble_trigger_mgr.start()

            # AutomationManager retired — trigger operators handle this now.
            # Automations are auto-migrated to trigger operators in
            # _migrate_automations_to_triggers() above.

            # Start the distributed orchestrator if configured.
            if config.get("distributed") and _HAS_DISTRIBUTED:
                if mqtt_bridge is not None and mqtt_bridge._client is not None:
                    orch = Orchestrator(
                        mqtt_bridge._client,
                        config.get("distributed", {}),
                    )
                    orch.start()
                    GlowUpRequestHandler.orchestrator = orch
                    logging.info("Distributed orchestrator started")
                else:
                    logging.warning(
                        "Distributed section found but MQTT bridge is not "
                        "active — orchestrator requires MQTT"
                    )
            elif config.get("distributed") and not _HAS_DISTRIBUTED:
                logging.warning(
                    "Distributed section found but distributed module "
                    "not available"
                )

            # Start the media pipeline if configured.  Also start the
            # SignalBus MQTT bridge if signal_bus.mqtt is true — this allows
            # the server to ingest signals from remote compute nodes (e.g.
            # Judy's AudioExtractor output) even without local media sources.
            if config.get("media_sources") or config.get("signal_bus"):
                media_mgr = MediaManager()
                media_mgr.configure(config)
                GlowUpRequestHandler.media_manager = media_mgr
                source_count: int = len(config.get("media_sources", {}))
                if source_count:
                    logging.info(
                        "Media pipeline configured — %d source(s)",
                        source_count,
                    )
                else:
                    logging.info(
                        "SignalBus started (no local sources — "
                        "listening for remote signals)"
                    )
            else:
                logging.info("No media_sources configured — media idle")

            # -- Sensor adapters / Operators / Lock manager ------------------
            # All adapters write to the SignalBus, which may be from the
            # MediaManager or a standalone instance.
            signal_bus: Optional[SignalBus] = None
            if media_mgr is not None:
                signal_bus = media_mgr.bus
            if signal_bus is None:
                signal_bus = SignalBus()
                logging.info("Created standalone SignalBus for operators")
            GlowUpRequestHandler.signal_bus = signal_bus

            mqtt_cfg: dict = config.get("mqtt", {})
            broker_addr: str = mqtt_cfg.get("broker", "localhost")
            broker_port: int = mqtt_cfg.get("port", 1883)

            # BLE adapter (bridges glowup/ble/# MQTT → SignalBus).
            # BLE sensor daemon runs on broker-2 — use its MQTT broker
            # if configured, otherwise fall back to global MQTT broker.
            if _MQTT_AVAILABLE and _HAS_BLE_ADAPTER:
                ble_cfg: dict = config.get("ble", {})
                ble_broker: str = ble_cfg.get("broker", broker_addr)
                ble_port: int = ble_cfg.get("port", broker_port)
                ble_adpt = BleAdapter(
                    bus=signal_bus,
                    broker=ble_broker,
                    port=ble_port,
                    config=ble_cfg,
                )
                ble_adpt.start()
                GlowUpRequestHandler.ble_adapter = ble_adpt

            # Power logger — SQLite storage for smart plug readings.
            try:
                from power_logger import PowerLogger
                config_dir_local: str = os.path.dirname(
                    GlowUpRequestHandler.config_path or "/etc/glowup/server.json"
                )
                power_db_path: str = os.path.join(config_dir_local, "power.db")
                power_log = PowerLogger(db_path=power_db_path)
                GlowUpRequestHandler.power_logger = power_log
            except Exception as exc:
                logging.warning("Power logger unavailable: %s", exc)
                power_log = None

            # Zigbee adapter (for Z2M devices — motion, contact, temp).
            z_cfg: dict = config.get("zigbee", {})
            if z_cfg.get("enabled") and _MQTT_AVAILABLE and _HAS_ZIGBEE:
                # Zigbee adapter connects to the Z2M MQTT broker,
                # which may be on a different machine (e.g., broker-2).
                z_broker: str = z_cfg.get("broker", broker_addr)
                z_port: int = z_cfg.get("port", broker_port)
                zigbee_adapter = ZigbeeAdapter(
                    config=z_cfg,
                    bus=signal_bus,
                    broker=z_broker,
                    port=z_port,
                )
                # Attach power logger so plug readings get stored.
                if power_log is not None:
                    zigbee_adapter._power_logger = power_log
                zigbee_adapter.start()

            # Vivint adapter (for lock state — read-only cloud API).
            v_cfg: dict = config.get("vivint", {})
            if v_cfg.get("enabled") and _HAS_VIVINT:
                vivint_adapter = VivintAdapter(
                    config=v_cfg,
                    bus=signal_bus,
                    mqtt_client=(
                        mqtt_bridge._client if mqtt_bridge else None
                    ),
                )
                vivint_adapter.start()
                server._vivint_adapter = vivint_adapter

            # NVR camera adapter (Reolink snapshot proxy).
            nvr_cfg: dict = config.get("nvr", {})
            if nvr_cfg.get("host") and _HAS_NVR:
                nvr_adapter = NvrAdapter(nvr_cfg)
                nvr_adapter.start()
                server._nvr_adapter = nvr_adapter

            # Printer monitor (Brother CSV endpoint).
            printer_cfg: dict = config.get("printer", {})
            if printer_cfg.get("host") and _HAS_PRINTER:
                printer_adapter = PrinterAdapter(
                    config=printer_cfg,
                    bus=signal_bus,
                    mqtt_client=(mqtt_bridge._client if mqtt_bridge else None),
                )
                printer_adapter.start()
                server._printer_adapter = printer_adapter

            # Auto-migrate automations[] → trigger operators in operators[].
            config["_config_path"] = GlowUpRequestHandler.config_path
            _migrate_automations_to_triggers(config)

            # Operator manager — handles all operator types including
            # trigger operators (which replace AutomationManager).
            op_cfgs: list = config.get("operators", [])
            if op_cfgs:
                operator_mgr = OperatorManager(signal_bus)
                operator_mgr.configure(op_cfgs)
                config["_config_path"] = GlowUpRequestHandler.config_path
                config["_device_manager"] = dm
                operator_mgr.start(full_config=config)
                GlowUpRequestHandler.operator_manager = operator_mgr

            # Lock manager (presentation layer for /home dashboard).
            if config.get("locks") and _HAS_LOCK_MANAGER:
                config_path_local2: str = GlowUpRequestHandler.config_path or ""
                db_dir: str = os.path.dirname(os.path.abspath(config_path_local2)) if config_path_local2 else "."
                lock_mgr = LockManager(
                    config=config,
                    server=server,
                    db_path=os.path.join(db_dir, "state.db"),
                    broker=broker_addr,
                    port=broker_port,
                    bus=signal_bus,
                )
                lock_mgr.start()
                GlowUpRequestHandler.lock_manager = lock_mgr

        except Exception:
            logging.exception("Background startup failed")

    startup_thread: threading.Thread = threading.Thread(
        target=_background_startup, daemon=True, name="startup-loader",
    )
    startup_thread.start()

    try:
        server.serve_forever()
    finally:
        logging.info("Shutting down...")
        # Stop in reverse startup order.
        if lock_mgr is not None:
            lock_mgr.stop()
        if operator_mgr is not None:
            operator_mgr.stop()
        if nvr_adapter is not None:
            nvr_adapter.stop()
        if vivint_adapter is not None:
            vivint_adapter.stop()
        if ble_adpt is not None:
            ble_adpt.stop()
        if zigbee_adapter is not None:
            zigbee_adapter.stop()
        if media_mgr is not None:
            media_mgr.shutdown()
        if orch is not None:
            orch.stop()
        if automation_mgr is not None:
            automation_mgr.stop()
        if ble_trigger_mgr is not None:
            ble_trigger_mgr.stop()
        if mqtt_bridge is not None:
            mqtt_bridge.stop()
        if keepalive is not None:
            keepalive.stop()
            keepalive.join(timeout=3.0)
        scheduler: Optional[SchedulerThread] = GlowUpRequestHandler.scheduler
        if scheduler is not None:
            scheduler.stop()
            scheduler.join(timeout=5.0)
        dm.shutdown()
        server.server_close()
        logging.info("Server stopped")


if __name__ == "__main__":
    main()
