"""GlowUp REST API server — coordinator and API host for the local SOE runtime.

Provides an HTTP API for querying and controlling GlowUp from anywhere.
In the smallest deployment that means LIFX devices and schedules; in a
full deployment it also means sensors, operators, media, adapters,
voice, and distributed workers.  This server subsumes the role of the
standalone scheduler by managing effects directly through the
:class:`Controller` API instead of spawning subprocesses.

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
        ↓ local orchestration
    SignalBus / Controller / Operators / Adapters
        ↓ mixed transports
    LIFX / MQTT / Workers / Voice / Other emitters

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
    GET  /api/plugs                      List Zigbee plugs (cached state)
    POST /api/plugs/{label}/power        Turn a Zigbee plug on/off
    POST /api/plugs/refresh              Bulk live-state refresh from broker-2
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
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import unquote, parse_qs, urlparse
import logging
import math
import os
import re
import signal
import socket
import socketserver
import sys
import threading
import time
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
from infrastructure.mqtt_bridge import MqttBridge, PAHO_AVAILABLE as _MQTT_AVAILABLE
from infrastructure.ble_trigger import BleTriggerManager
# automation.py is largely retired (its AutomationManager was
# replaced by the trigger operator framework — see the comment on
# the call to migrate_ble_triggers in _background_startup), but
# the validation helper and the legacy-config migration helper are
# still load-bearing on every startup.
from automation import validate_automation, migrate_ble_triggers
from operators import OperatorManager
try:
    from infrastructure.lock_manager import LockManager
    _HAS_LOCK_MANAGER: bool = True
except ImportError:
    _HAS_LOCK_MANAGER = False

try:
    from contrib.adapters.vivint_adapter import VivintAdapter
    _HAS_VIVINT: bool = True
except ImportError:
    _HAS_VIVINT = False

try:
    from contrib.adapters.nvr_adapter import NvrAdapter
    _HAS_NVR: bool = True
except ImportError:
    _HAS_NVR = False
try:
    from contrib.adapters.printer_adapter import PrinterAdapter
    _HAS_PRINTER: bool = True
except ImportError:
    _HAS_PRINTER = False
try:
    from adapters.matter_adapter import MatterAdapter
    _HAS_MATTER: bool = True
except ImportError:
    _HAS_MATTER = False
# BLE is no longer an in-process adapter on the hub.  After the
# 2026-04-15 service-pattern pivot, glowup-ble-sensor on broker-2
# publishes cross-host directly to the hub mosquitto on
# glowup/signals/{label}:{prop} (consumed by _on_remote_signal)
# and glowup/ble/status/{label} (consumed by BleTriggerManager
# locally on the hub broker).  See:
#   - docs/35-service-vs-adapter.md
#   - docs/29-zigbee-service.md (the canonical service example)
#   - docs/28-ble-sensors.md
# Do NOT re-add a `from adapters.ble_adapter import BleAdapter`
# without first reading those docs and the feedback memories.

from media import MediaManager, SignalBus
from media.source import AudioStreamServer
from solar import SunTimes, sun_times
from transport import LifxDevice, SendMode, SOCKET_TIMEOUT, SINGLE_ZONE_COUNT

# Voice-subsystem constants — topic names and staleness thresholds
# used by the satellite health probe wiring below.  voice.constants
# is a pure-constants module (no side effects on import) so this is
# safe at top level and avoids duplicating magic strings across
# server.py, handlers/dashboard.py, and voice/satellite/daemon.py.
from voice import constants as _voice_c

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

# Server-side proxy for out-of-process adapters.
from infrastructure.adapter_proxy import (
    AdapterProxy, KeepaliveProxy, MatterProxyWrapper,
)

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
    CF_TUNNEL_HEADER,
    DEFAULT_CONFIG_PATH, EFFECT_DEFAULTS_FILENAME,
    GROUP_PREFIX, GRID_PREFIX, LOG_FORMAT, LOG_DATE_FORMAT,
    API_PREFIX, AUTH_RATE_LIMIT, AUTH_RATE_WINDOW,
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


def _json_default(value: Any) -> Any:
    """`json.dumps` fallback for types Postgres returns natively.

    Decimal arises from any aggregate over an integer column
    (`AVG(int_col)` → numeric → Decimal); without this, those endpoints
    500 with "Object of type Decimal is not JSON serializable".
    """
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


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
    _Route("GET", ("vivint",),
           "_handle_get_vivint_page", requires_auth=False),
    _Route("GET", ("api", "home", "vivint"),
           "_handle_get_home_vivint", requires_auth=False),
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
    _Route("GET", ("api", "home", "health"),
           "_handle_get_home_health", requires_auth=False),
    _Route("GET", ("api", "home", "all"),
           "_handle_get_home_all", requires_auth=False),
    _Route("GET", ("api", "io", "stats"),
           "_handle_get_io_stats", requires_auth=False),
    _Route("GET", ("io",),
           "_handle_get_io_page", requires_auth=False),
    _Route("GET", ("power",),
           "_handle_get_power_page", requires_auth=False),
    _Route("GET", ("api", "power", "readings"),
           "_handle_get_power_readings", requires_auth=False),
    _Route("GET", ("api", "power", "summary"),
           "_handle_get_power_summary", requires_auth=False),
    _Route("GET", ("api", "power", "devices"),
           "_handle_get_power_devices", requires_auth=False),
    _Route("GET", ("api", "power", "plug_states"),
           "_handle_get_power_plug_states", requires_auth=False),
    _Route("GET", ("thermal",),
           "_handle_get_thermal_page", requires_auth=False),
    _Route("GET", ("thermal", "host", "{node_id}"),
           "_handle_get_thermal_detail_page", requires_auth=False),
    _Route("GET", ("api", "thermal", "latest"),
           "_handle_get_thermal_latest", requires_auth=False),
    _Route("GET", ("api", "thermal", "hosts"),
           "_handle_get_thermal_hosts", requires_auth=False),
    _Route("GET", ("api", "thermal", "readings"),
           "_handle_get_thermal_readings", requires_auth=False),
    # POST /api/zigbee/set was removed in 2026-04-15 along with
    # the in-process Zigbee adapter.  Its successor lives under
    # /api/plugs — see handlers/plug.py and docs/29-zigbee-service.md.
    _Route("GET", ("api", "plugs"),
           "_handle_get_plugs"),
    _Route("POST", ("api", "plugs", "refresh"),
           "_handle_post_plugs_refresh"),
    _Route("POST", ("api", "plugs", "{label}", "power"),
           "_handle_post_plug_power",
           unquote_params=("label",)),
    _Route("GET", ("api", "operators"),
           "_handle_get_operators", requires_auth=True),
    _Route("GET", ("api", "signals", "bindings"),
           "_handle_get_bindings", requires_auth=True),
    _Route("POST", ("api", "signals", "bindings"),
           "_handle_post_binding", requires_auth=True),
    _Route("DELETE", ("api", "signals", "bindings", "{target}"),
           "_handle_delete_binding", requires_auth=True,
           unquote_params=("target",)),
    _Route("GET", ("api", "config", "nav"),
           "_handle_get_nav_config", requires_auth=False),
    # Satellite health — continuous view of every known satellite,
    # and on-demand deep check for a single room.  Both are
    # auth-free so the /home dashboard and future tooling can poll
    # without a token.  See handlers/dashboard.py for handler bodies.
    _Route("GET", ("api", "satellites", "health"),
           "_handle_get_satellites_health", requires_auth=False),
    _Route("POST", ("api", "satellites", "{room}", "health", "check"),
           "_handle_post_satellite_health_check",
           requires_auth=False, unquote_params=("room",)),
    _Route("GET", ("shopping",),
           "_handle_get_shopping_page", requires_auth=False),
    _Route("GET", ("api", "shopping"),
           "_handle_get_shopping", requires_auth=False),
    _Route("POST", ("api", "shopping"),
           "_handle_post_shopping", requires_auth=False),
    _Route("POST", ("api", "shopping", "{id}", "check"),
           "_handle_post_shopping_check", requires_auth=False),
    _Route("DELETE", ("api", "shopping", "{id}"),
           "_handle_delete_shopping_item", requires_auth=False,
           unquote_params=("id",)),
    _Route("DELETE", ("api", "shopping", "checked"),
           "_handle_delete_shopping_checked", requires_auth=False),

    # -- SDR (software-defined radio) -----------------------------------------
    _Route("GET", ("sdr",),
           "_handle_get_sdr_page", requires_auth=False),
    _Route("GET", ("api", "sdr", "status"),
           "_handle_get_sdr_status"),
    _Route("POST", ("api", "sdr", "frequency"),
           "_handle_post_sdr_frequency"),

    # -- ADS-B (aircraft tracking) ------------------------------------------
    _Route("GET", ("adsb",),
           "_handle_get_adsb_page", requires_auth=False),
    _Route("GET", ("api", "sdr", "adsb", "aircraft"),
           "_handle_get_adsb_aircraft"),

    # -- Ernie (.153) sniffer dashboard -------------------------------------
    # Public (LAN-only deployment — no reason to gate read-only sniffer
    # data behind auth, matches the intent of /ernie being public).
    _Route("GET", ("ernie",),
           "_handle_get_ernie_page", requires_auth=False),
    _Route("GET", ("api", "ernie", "ble"),
           "_handle_get_ernie_ble", requires_auth=False),
    _Route("GET", ("api", "ernie", "ble", "events"),
           "_handle_get_ernie_ble_events", requires_auth=False),
    _Route("GET", ("api", "ernie", "tpms"),
           "_handle_get_ernie_tpms", requires_auth=False),
    _Route("GET", ("api", "ernie", "thermal"),
           "_handle_get_ernie_thermal", requires_auth=False),

    _Route("GET", ("photos", "{filename}"),
           "_handle_get_photo", requires_auth=False),
    _Route("GET", ("js", "{filename}"),
           "_handle_get_static_js", requires_auth=False),

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
    # Voice gate status for the dashboard header banner.  Unauthed so
    # the banner renders before the user signs in — gate state is not
    # sensitive, and hiding it would defeat the "always visible"
    # contract of the banner.
    _Route("GET", ("api", "voice", "gates"),
           "_handle_get_voice_gates", requires_auth=False),
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
    _Route("POST", ("api", "adapters", "{name}", "restart"),
           "_handle_post_adapter_restart",
           unquote_params=("name",)),

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
    # Matter device control.
    _Route("GET", ("api", "matter", "devices"),
           "_handle_get_matter_devices"),
    _Route("POST", ("api", "matter", "{name}", "power"),
           "_handle_post_matter_power",
           unquote_params=("name",)),
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

# Plug manager — Zigbee smart plugs, parallel to DeviceManager
from plug_manager import PlugManager


# Scheduler thread — extracted to scheduling/scheduler_thread.py
from scheduling import SchedulerThread


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

from handlers import (
    DeviceHandlerMixin, PlugHandlerMixin, GroupHandlerMixin,
    SensorHandlerMixin,
    ScheduleHandlerMixin, MediaHandlerMixin, DiscoveryHandlerMixin,
    RegistryHandlerMixin, DashboardHandlerMixin, CalibrationHandlerMixin,
    DistributedHandlerMixin, DiagnosticsHandlerMixin, StaticHandlerMixin,
)
from handlers.shopping import ShoppingHandlerMixin, ShoppingStore
from handlers.sdr import SdrHandlerMixin
from handlers.ernie import ErnieHandlerMixin


class GlowUpRequestHandler(
    DeviceHandlerMixin,
    PlugHandlerMixin,
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
    ShoppingHandlerMixin,
    SdrHandlerMixin,
    ErnieHandlerMixin,
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
    # Zigbee smart-plug subsystem.  ``None`` when no plugs are
    # configured — handlers in handlers/plug.py degrade gracefully.
    plug_manager: Optional["PlugManager"] = None
    auth_token: str
    scheduler: Optional[SchedulerThread] = None
    config: dict[str, Any] = {}
    config_path: Optional[str] = None
    media_manager: Optional[MediaManager] = None
    orchestrator: Optional[Any] = None
    keepalive: Optional[KeepaliveProxy] = None
    registry: Optional[DeviceRegistry] = None
    operator_manager: Optional[OperatorManager] = None
    lock_manager: Optional[Any] = None
    # Hub-side BLE state.  After the 2026-04-15 service-pattern
    # pivot, BLE no longer has an in-process adapter.  The handler
    # dict that used to be `ble_adapter` is gone; /api/ble/sensors
    # reads from `infrastructure.ble_trigger.sensor_data` directly.
    # See docs/35-service-vs-adapter.md.
    signal_bus: Optional[SignalBus] = None
    power_logger: Optional[Any] = None
    thermal_logger: Optional[Any] = None
    tpms_logger: Optional[Any] = None
    ble_sniffer_logger: Optional[Any] = None
    # Timestamp of the most recent non-time signal seen on
    # glowup/signals/#.  Populated by the _on_remote_signal callback
    # in _background_startup below.  Used by _handle_get_home_health
    # as the liveness probe for broker-2 — currently the sole
    # producer of device-origin signals (glowup-zigbee-service and
    # glowup-ble-sensor).  time:* signals are excluded because they
    # originate from the hub's own scheduler and would mask a silent
    # broker-2 outage.  None means "never seen since server start."
    broker2_signals_last_ts: Optional[float] = None

    # -- Satellite health state (populated by the MQTT callbacks
    # wired in _background_startup and by the periodic prober
    # thread).  Handlers read these dicts to answer
    # GET /api/satellites/health, POST /api/satellites/{room}/health/check,
    # and the "satellites" block in /api/home/health.
    #
    # satellite_heartbeats: {room: {"ts": float, "payload": dict}}
    #     Updated every time a message lands on
    #     glowup/voice/status/{room}.  "ts" is server wall-clock
    #     receive time, not the satellite-published timestamp —
    #     we care about freshness relative to the hub's clock.
    #
    # satellite_health_replies: {room: dict}
    #     Full report dict published by the satellite in response
    #     to either a periodic hub probe or an on-demand request.
    #     Preserves the correlation id so on-demand handlers can
    #     match replies to requests.
    #
    # satellite_health_events: {corr_id: threading.Event}
    #     Used by on-demand POST handlers to block until the reply
    #     for their specific correlation id arrives (or the wait
    #     times out).  Entries are removed by the waiter once set.
    #
    # Locks protect concurrent mutation from the MQTT callback
    # thread, the periodic prober thread, and handler threads.
    satellite_heartbeats: dict[str, dict[str, Any]] = {}
    satellite_health_replies: dict[str, dict[str, Any]] = {}
    satellite_health_events: dict[str, threading.Event] = {}
    satellite_state_lock: threading.Lock = threading.Lock()
    # The proc_mqtt client used to publish health requests on
    # behalf of on-demand handlers.  Assigned in _background_startup
    # once proc_mqtt is up — handlers check for None before publishing.
    satellite_probe_client: Any = None

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
        ka: Optional[KeepaliveProxy] = self.keepalive
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

        Catches ConnectionResetError and BrokenPipeError so a
        disconnected client does not kill the handler thread.

        Args:
            code: HTTP status code.
            data: JSON-serializable response data.
        """
        # `default=` catches Postgres `numeric` columns that arrive as
        # Decimal — without this, any aggregate query (AVG/SUM over an
        # integer column) silently 500s the endpoint.
        body: bytes = json.dumps(data, indent=2, default=_json_default).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError, OSError):
            # Client disconnected mid-response — nothing to do.
            pass

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

            # Cloudflare tunnel gate.  The CF edge injects
            # ``CF-Connecting-IP`` on every request that came through the
            # public tunnel (lights.schoolio.net).  Policy: only
            # authenticated routes are allowed over the tunnel, so the
            # iOS app keeps working while every dashboard and unauthed
            # surface is LAN-only.  This is enforced at the server so a
            # stale/mis-edited cloudflared ingress config cannot
            # re-expose the dashboards.
            if (
                not route.requires_auth
                and self.headers.get(CF_TUNNEL_HEADER)
            ):
                self._send_json(
                    403,
                    {"error": "not available via public tunnel"},
                )
                return

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

            # Dispatch with error boundary and deadline logging.
            handler_fn: Callable = getattr(self, route.handler)
            t_handler: float = time.monotonic()
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
                except Exception as exc:
                    logging.debug("Error response send failed: %s", exc)
            finally:
                elapsed_s: float = time.monotonic() - t_handler
                if elapsed_s > HANDLER_DEADLINE_S:
                    logging.warning(
                        "SLOW HANDLER: %s took %.1fs from %s:%d",
                        route.handler, elapsed_s,
                        self.client_address[0], self.client_address[1],
                    )
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

# ---------------------------------------------------------------------------
# Thread heartbeat registry — each daemon thread writes its current
# activity here.  The watchdog reads them all on hang detection.
# Key = thread name, value = (description, monotonic timestamp).
#
# Gated by GLOWUP_TRACE env var.  Set GLOWUP_TRACE=1 to activate.
# When inactive, _hb() calls in adapters and infrastructure are no-ops.
# ---------------------------------------------------------------------------

TRACING_ENABLED: bool = os.environ.get("GLOWUP_TRACE", "") == "1"

_thread_heartbeats: dict[str, tuple[str, float]] = {}

# ---------------------------------------------------------------------------
# Single-step debugger — semaphore-gated thread stepping.
#
# When _debug_stepping is True, instrumented threads block on their
# semaphore before each operation.  The inspector releases the
# semaphore one click at a time, reads _thread_heartbeats to see
# what the thread did, then releases again.
#
# Activate:  set _debug_stepping = True (via watchdog or external probe)
# Step:      _debug_gates["thread-name"].release()
# Inspect:   _thread_heartbeats["thread-name"]
# Deactivate: set _debug_stepping = False (threads resume free-running)
# ---------------------------------------------------------------------------

_debug_stepping: bool = False
_debug_gates: dict[str, threading.Semaphore] = {}


def _gate(thread_name: str) -> None:
    """Block if single-step debugging is active.

    Called before each operation in instrumented threads.
    When ``_debug_stepping`` is False, returns immediately.
    When True, blocks until the inspector releases the semaphore.

    Args:
        thread_name: Name of the calling thread (used as gate key).
    """
    if not _debug_stepping:
        return
    if thread_name not in _debug_gates:
        _debug_gates[thread_name] = threading.Semaphore(0)
    _debug_gates[thread_name].acquire()


def _heartbeat(activity: str) -> None:
    """Record what the current thread is doing right now.

    No-op unless GLOWUP_TRACE=1 is set in the environment.

    Args:
        activity: Short description of current operation.
    """
    if TRACING_ENABLED:
        _thread_heartbeats[threading.current_thread().name] = (
            activity, time.monotonic(),
        )


# ---------------------------------------------------------------------------
# Thread pool and timeout constants — data-driven from production logs.
# ---------------------------------------------------------------------------

# Maximum concurrent handler threads.  Observed steady-state: ~15,
# peak: ~25 (24 during the stall).  48 provides headroom for bursts
# without allowing unbounded growth.
MAX_HANDLER_THREADS: int = 48

# Socket timeout for accepted HTTP connections (seconds).  A dead
# client (TCP RST lost, keepalive expired) can hold a handler thread
# hostage until the OS kills the connection.  15 seconds is generous
# for any single HTTP request/response cycle on a LAN.
HANDLER_SOCKET_TIMEOUT_S: float = 15.0

# Handler deadline — log a warning if any handler takes longer than
# this.  Does not kill the handler, just records the data so we can
# identify which handlers are slow and why.
HANDLER_DEADLINE_S: float = 5.0

# Listen backlog — how many TCP connections the kernel queues while
# all handler threads are busy.  Default is 5, which drops the 6th
# waiting connection.  16 keeps connections alive during brief bursts.
LISTEN_BACKLOG: int = 16


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """HTTP server with bounded thread pool and request timeouts.

    Each request gets its own thread (required for SSE long-lived
    streams).  Thread count is capped at MAX_HANDLER_THREADS.
    Every accepted socket gets a timeout so a hung client cannot
    hold a thread forever.  Thread usage is tracked and logged
    in SERVICE STALL diagnostics.

    daemon_threads ensures all handler threads die with the process.
    """

    daemon_threads: bool = True
    allow_reuse_address: bool = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize with thread tracking and stall detection."""
        super().__init__(*args, **kwargs)
        self._last_request_time: float = time.monotonic()
        self._stall_logged: bool = False
        self._active_threads: int = 0
        self._thread_lock: threading.Lock = threading.Lock()
        self._thread_high_water: int = 0

    def server_activate(self) -> None:
        """Increase listen backlog from the default 5."""
        self.socket.listen(LISTEN_BACKLOG)

    def process_request(
        self, request: socket.socket, client_address: tuple,
    ) -> None:
        """Apply socket timeout and thread cap before dispatching.

        Args:
            request:        The accepted socket.
            client_address: (ip, port) tuple.
        """
        # Socket timeout — dead clients can't hold threads forever.
        request.settimeout(HANDLER_SOCKET_TIMEOUT_S)

        # Thread cap — reject with 503 if pool is full.
        with self._thread_lock:
            if self._active_threads >= MAX_HANDLER_THREADS:
                logging.warning(
                    "THREAD POOL FULL (%d/%d): rejecting %s:%d",
                    self._active_threads, MAX_HANDLER_THREADS,
                    client_address[0], client_address[1],
                )
                try:
                    request.sendall(
                        b"HTTP/1.1 503 Service Unavailable\r\n"
                        b"Content-Length: 0\r\n\r\n"
                    )
                except Exception as exc:
                    logging.debug("503 send failed: %s", exc)
                try:
                    request.close()
                except Exception as exc:
                    logging.debug("Socket close failed: %s", exc)
                return
            self._active_threads += 1
            if self._active_threads > self._thread_high_water:
                self._thread_high_water = self._active_threads

        super().process_request(request, client_address)

    def process_request_thread(
        self, request: socket.socket, client_address: tuple,
    ) -> None:
        """Handle request with cleanup and timeout logging.

        Args:
            request:        The accepted socket.
            client_address: (ip, port) tuple.
        """
        try:
            super().process_request_thread(request, client_address)
        except socket.timeout:
            logging.warning(
                "HANDLER TIMEOUT (%.0fs): %s:%d — thread freed",
                HANDLER_SOCKET_TIMEOUT_S,
                client_address[0], client_address[1],
            )
        except Exception as exc:
            logging.debug(
                "Handler exception for %s:%d: %s",
                client_address[0], client_address[1], exc,
            )
        finally:
            with self._thread_lock:
                self._active_threads -= 1

    def service_actions(self) -> None:
        """Called every poll cycle by the stdlib serve_forever().

        Tracks time since last accepted request. Logs when the
        server goes silent (no requests accepted) for too long,
        and when it recovers. This instruments the stdlib loop
        without replacing the selector.
        """
        # _handle_request_noblock sets this via our get_request override.
        now: float = time.monotonic()
        idle: float = now - self._last_request_time

        # Log stall after 60 seconds of no accepted requests.
        # The kiosk polls every POLL_FAST (10s), so 60s means six
        # consecutive missed polls — a genuine problem, not jitter.
        if idle > 60.0 and not self._stall_logged:
            self._stall_logged = True
            last_step: str = getattr(self, "_last_step", "none")
            accept_seq: int = getattr(self, "_accept_seq", 0)
            try:
                fd: int = self.socket.fileno()
                listening: int = self.socket.getsockopt(
                    socket.SOL_SOCKET, socket.SO_ACCEPTCONN,
                )
                bound: tuple = self.socket.getsockname()
                sock_err: int = self.socket.getsockopt(
                    socket.SOL_SOCKET, socket.SO_ERROR,
                )
                logging.warning(
                    "SERVICE STALL: no requests accepted for %.1fs. "
                    "fd=%d, bound=%s, listening=%d, so_error=%d, "
                    "os_threads=%d, handler=%d/%d (hwm=%d), "
                    "last_step='%s', total_accepts=%d",
                    idle, fd, bound, listening, sock_err,
                    threading.active_count(),
                    self._active_threads, MAX_HANDLER_THREADS,
                    self._thread_high_water,
                    last_step, accept_seq,
                )
            except Exception as exc:
                logging.warning(
                    "SERVICE STALL: %.1fs idle, last_step='%s', "
                    "socket check failed: %s",
                    idle, last_step, exc,
                )

    def get_request(self) -> tuple:
        """Override to timestamp accepted connections for stall detection."""
        result = super().get_request()
        conn, addr = result
        now: float = time.monotonic()
        idle: float = now - self._last_request_time
        if self._stall_logged:
            logging.warning(
                "SERVICE RECOVERED after %.1fs stall. "
                "Accepted from %s:%d, fd=%d",
                idle, addr[0], addr[1], conn.fileno(),
            )
            self._stall_logged = False
        self._last_request_time = now
        return result

    def _handle_request_noblock(self) -> None:
        """Override to instrument each step of the accept cycle.

        Logs a monotonic sequence number at each step so we know
        exactly which step the server last completed before a hang.
        Written to a rotating counter in an instance variable —
        the watchdog reads it to report the last completed step.
        """
        self._accept_seq = getattr(self, "_accept_seq", 0) + 1
        seq: int = self._accept_seq
        self._last_step = f"#{seq} poll→accept"

        try:
            request, client_address = self.get_request()
        except OSError:
            self._last_step = f"#{seq} accept failed (OSError)"
            return

        self._last_step = f"#{seq} accept→verify"

        if self.verify_request(request, client_address):
            try:
                self._last_step = f"#{seq} verify→spawn"
                self.process_request(request, client_address)
                self._last_step = f"#{seq} spawn→done"
            except Exception:
                self.handle_error(request, client_address)
                self.shutdown_request(request)
                self._last_step = f"#{seq} spawn failed"
        else:
            self.shutdown_request(request)
            self._last_step = f"#{seq} verify rejected"


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
    keepalive: "KeepaliveProxy",
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
        keepalive:   Running :class:`KeepaliveProxy` instance whose
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
            # matter: prefixed identifiers pass through unchanged —
            # they are not LIFX devices and have no IP to resolve.
            if ident.startswith("matter:"):
                resolved.append(ident)
                continue
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
    # Zigbee smart-plug subsystem.  Constructs from the ``plugs`` and
    # ``zigbee`` sections of server.json; an empty/absent section
    # yields an empty manager (no plugs, no error).
    GlowUpRequestHandler.plug_manager = PlugManager(config)
    GlowUpRequestHandler.auth_token = config["auth_token"]
    GlowUpRequestHandler.scheduler = None          # patched after start
    GlowUpRequestHandler.config = config
    GlowUpRequestHandler.config_path = config_path

    # -- Shopping list store ---------------------------------------------------
    shopping_path: str = os.path.join(
        os.path.dirname(config_path), "shopping.json",
    )
    server_shopping_store: ShoppingStore = ShoppingStore(shopping_path)

    server: ThreadedHTTPServer = ThreadedHTTPServer(
        ("", port), GlowUpRequestHandler,
    )
    server._shopping_store = server_shopping_store

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
        target=_shutdown_watcher, daemon=True, name="Thread-1 (_shutdown_watcher)",
    )
    watcher.start()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # -- Self-health watchdog -------------------------------------------------
    # The server has a recurring bug where poll() on the listening socket
    # stops firing — HTTP dies while internal threads keep running.
    # Not fd exhaustion (verified).  This watchdog detects the condition,
    # logs diagnostic data for post-mortem, and forces a restart via
    # launchd KeepAlive.
    _WATCHDOG_INTERVAL: int = 30       # Check every 30 seconds.
    _WATCHDOG_MAX_FAILURES: int = 5    # 5 consecutive failures = hung (2.5 min).
    _WATCHDOG_TIMEOUT: float = 10.0    # Per-check connect timeout.

    # Minimum uptime before the watchdog is allowed to kill the
    # process.  Device loading can take 30-40 seconds (query-silent
    # bulbs timeout at 15s each).  Killing during startup creates a
    # crash loop that leaks NVR sessions and prevents adapters from
    # ever connecting.
    _WATCHDOG_GRACE_PERIOD: float = 120.0  # 2 minutes after start.

    def _watchdog() -> None:
        """Background thread: periodically verify HTTP is responsive."""
        import resource
        start_time: float = time.monotonic()
        consecutive_failures: int = 0
        while not shutdown_event.is_set():
            shutdown_event.wait(_WATCHDOG_INTERVAL)
            if shutdown_event.is_set():
                return

            # Don't count failures during the startup grace period.
            uptime: float = time.monotonic() - start_time
            in_grace: bool = uptime < _WATCHDOG_GRACE_PERIOD

            # Try connecting to our own listening socket.
            # Both sides of this connection are ours — instrument fully.
            t0: float = time.monotonic()
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(_WATCHDOG_TIMEOUT)
                t_connect: float = time.monotonic()
                sock.connect(("127.0.0.1", port))
                t_connected: float = time.monotonic()
                # Send minimal HTTP request.
                sock.sendall(b"GET /api/status HTTP/1.0\r\n\r\n")
                t_sent: float = time.monotonic()
                data = sock.recv(128)
                t_recv: float = time.monotonic()
                sock.close()
                if data:
                    consecutive_failures = 0
                    continue
                # Connected, sent, but no data back.
                logging.warning(
                    "WATCHDOG: connect OK (%.3fs), send OK, "
                    "recv empty (%.3fs wait). Server accepted "
                    "but did not respond.",
                    t_connected - t_connect,
                    t_recv - t_sent,
                )
            except socket.timeout:
                t_fail: float = time.monotonic()
                logging.warning(
                    "WATCHDOG: timeout after %.3fs. "
                    "connect=%.3fs",
                    t_fail - t0,
                    t_fail - t_connect if 't_connect' in dir() else -1,
                )
            except ConnectionRefusedError:
                logging.warning(
                    "WATCHDOG: connection REFUSED after %.3fs. "
                    "Socket not listening?",
                    time.monotonic() - t0,
                )
            except Exception as exc:
                logging.warning(
                    "WATCHDOG: probe failed after %.3fs: %s (%s)",
                    time.monotonic() - t0,
                    type(exc).__name__, exc,
                )

            if in_grace:
                # Log but don't count — server is still starting up.
                logging.info(
                    "WATCHDOG: HTTP unresponsive during startup "
                    "(%.0fs uptime, grace period %.0fs). Not counting.",
                    uptime, _WATCHDOG_GRACE_PERIOD,
                )
                continue

            consecutive_failures += 1
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            thread_count: int = threading.active_count()
            thread_names: str = ", ".join(
                t.name for t in threading.enumerate()
            )
            logging.error(
                "WATCHDOG: HTTP unresponsive (%d/%d failures). "
                "threads=%d fds_limit=%d names=[%s]",
                consecutive_failures, _WATCHDOG_MAX_FAILURES,
                thread_count, soft, thread_names,
            )
            if consecutive_failures >= _WATCHDOG_MAX_FAILURES:
                import os as _os
                import sys as _sys
                import traceback as _tb

                # Count open fds.
                fd_count: int = 0
                fd_types: list[str] = []
                for fd in range(min(_os.sysconf("SC_OPEN_MAX"), 1024)):
                    try:
                        st = _os.fstat(fd)
                        fd_count += 1
                    except OSError:
                        pass

                # Dump every thread's Python call stack — this is
                # the smoking gun for diagnosing GIL contention,
                # deadlocks, or stuck system calls.
                _last = getattr(server, "_last_step", "none")
                _seq = getattr(server, "_accept_seq", 0)
                logging.critical(
                    "WATCHDOG: Server hung — %d open fds, %d threads. "
                    "last_step='%s', total_accepts=%d. "
                    "Dumping all thread stacks.",
                    fd_count, thread_count, _last, _seq,
                )
                frames = _sys._current_frames()
                for thread in threading.enumerate():
                    frame = frames.get(thread.ident)
                    if frame is not None:
                        stack = "".join(_tb.format_stack(frame))
                        logging.critical(
                            "WATCHDOG STACK [%s (id=%d)]:\n%s",
                            thread.name, thread.ident, stack,
                        )

                # Check listening socket state — is it still alive?
                try:
                    fileno: int = server.socket.fileno()
                    # fileno() returns -1 if closed.
                    bound_addr = server.socket.getsockname()
                    # SO_ACCEPTCONN: is the socket still listening?
                    listening: int = server.socket.getsockopt(
                        socket.SOL_SOCKET, socket.SO_ACCEPTCONN,
                    )
                    # SO_ERROR: pending socket error (0 = no error).
                    sock_err: int = server.socket.getsockopt(
                        socket.SOL_SOCKET, socket.SO_ERROR,
                    )
                    logging.critical(
                        "WATCHDOG: Listening socket fd=%d, "
                        "family=%s, type=%s, bound=%s, "
                        "listening=%d, so_error=%d",
                        fileno, server.socket.family,
                        server.socket.type, bound_addr,
                        listening, sock_err,
                    )
                    if fileno == -1:
                        logging.critical(
                            "WATCHDOG: Socket fd is -1 — "
                            "SOCKET HAS BEEN CLOSED",
                        )
                    if not listening:
                        logging.critical(
                            "WATCHDOG: SO_ACCEPTCONN=0 — "
                            "SOCKET IS NOT LISTENING",
                        )
                    if sock_err != 0:
                        logging.critical(
                            "WATCHDOG: SO_ERROR=%d — "
                            "SOCKET HAS A PENDING ERROR",
                            sock_err,
                        )
                except Exception as sock_exc:
                    logging.critical(
                        "WATCHDOG: Listening socket inspection "
                        "failed: %s", sock_exc,
                    )

                # Dump thread heartbeats (if tracing was enabled).
                now_mono: float = time.monotonic()
                if _thread_heartbeats:
                    for tname, (act, ts) in sorted(
                        _thread_heartbeats.items()
                    ):
                        age: float = now_mono - ts
                        logging.critical(
                            "WATCHDOG HEARTBEAT [%s]: '%s' "
                            "(%.1fs ago)",
                            tname, act, age,
                        )

                # Check platform.
                logging.critical(
                    "WATCHDOG: platform=%s python=%s",
                    _sys.platform, _sys.version,
                )

                # Do NOT kill the server — leave threads alive so
                # they can be inspected from outside (ps, /proc,
                # or a follow-up watchdog probe).  The diagnostics
                # above are logged; killing destroys evidence.
                # Reset failure counter so the watchdog keeps
                # dumping diagnostics every cycle if it stays hung.
                logging.critical(
                    "WATCHDOG: diagnostics logged. Server left alive "
                    "for inspection. Next dump in %ds.",
                    _WATCHDOG_INTERVAL * _WATCHDOG_MAX_FAILURES,
                )
                consecutive_failures = 0

    watchdog_thread: threading.Thread = threading.Thread(
        target=_watchdog, daemon=True, name="watchdog",
    )
    watchdog_thread.start()

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
    # MediaManager reference — set by _background_startup if configured.
    media_mgr: Optional[MediaManager] = None
    # Orchestrator reference — set by _background_startup if configured.
    orch: Optional[Any] = None
    keepalive: Optional[KeepaliveProxy] = None
    # Adapter proxies — out-of-process adapters communicate via MQTT.
    # Set by _background_startup.  No zigbee proxy: Zigbee now runs
    # entirely on broker-2 (glowup-zigbee-service) and publishes
    # cross-host directly to the hub on glowup/signals/*.  See
    # zigbee_service/service.py header for the architecture.
    vivint_proxy: Optional[AdapterProxy] = None
    nvr_proxy: Optional[AdapterProxy] = None
    printer_proxy: Optional[AdapterProxy] = None
    matter_proxy: Optional[AdapterProxy] = None
    # Process communication MQTT client — used by all AdapterProxy instances.
    proc_mqtt: Any = None
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
        nonlocal mqtt_bridge, media_mgr, orch, keepalive, ble_trigger_mgr
        nonlocal vivint_proxy, nvr_proxy, printer_proxy, matter_proxy
        nonlocal proc_mqtt, operator_mgr, lock_mgr

        try:
            # -- Step 0: Process communication MQTT client -------------------
            # Must exist before KeepaliveProxy (and later, AdapterProxy).
            # Moved here from its original location so keepalive can
            # subscribe to MQTT topics before we wait for device data.
            mqtt_cfg_early: dict = config.get("mqtt", {})
            broker_addr_early: str = mqtt_cfg_early.get("broker", "localhost")
            broker_port_early: int = mqtt_cfg_early.get("port", 1883)

            if _MQTT_AVAILABLE:
                import paho.mqtt.client as _paho
                _paho_v2: bool = hasattr(_paho, "CallbackAPIVersion")
                _proc_id: str = f"glowup-server-proc-{int(time.time())}"
                if _paho_v2:
                    proc_mqtt = _paho.Client(
                        _paho.CallbackAPIVersion.VERSION2,
                        client_id=_proc_id,
                    )
                else:
                    proc_mqtt = _paho.Client(client_id=_proc_id)
                proc_mqtt.connect(broker_addr_early, broker_port_early)
                proc_mqtt.loop_start()
                logging.info(
                    "Process comm MQTT client connected to %s:%d",
                    broker_addr_early, broker_port_early,
                )

            # -- Step 1: Keepalive proxy (replaces in-process KeepaliveProxy) --
            # The keepalive process runs separately via systemd.
            # KeepaliveProxy subscribes to its MQTT topics and presents
            # the same interface (known_bulbs, known_bulbs_by_mac, etc.)
            # so all handlers and device_manager work unchanged.
            if proc_mqtt is not None:
                keepalive = KeepaliveProxy(proc_mqtt)
                GlowUpRequestHandler.keepalive = keepalive
            else:
                logging.warning(
                    "MQTT not available — keepalive proxy disabled, "
                    "device discovery will not work"
                )

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

            # Subscribe to power state updates from the keepalive process.
            # The isolated keepalive queries bulb power via UDP and
            # publishes results to glowup/device_state/{ip}/power.
            # We update the device manager's power cache directly —
            # no more dm.query_all_power_states() from the server.
            if proc_mqtt is not None:
                def _on_power_state_update(
                    client: Any, userdata: Any, message: Any,
                ) -> None:
                    # topic: glowup/device_state/{ip}/power
                    parts: list[str] = message.topic.split("/")
                    if len(parts) < 4:
                        return
                    ip_addr: str = parts[2]
                    try:
                        data: dict[str, Any] = json.loads(message.payload)
                    except (json.JSONDecodeError, ValueError):
                        return
                    power: Optional[bool] = data.get("power")
                    if power is not None:
                        with dm._lock:
                            dm._power_states[ip_addr] = power

                proc_mqtt.subscribe("glowup/device_state/+/power", qos=1)
                proc_mqtt.message_callback_add(
                    "glowup/device_state/+/power",
                    _on_power_state_update,
                )
                logging.info(
                    "Subscribed to power state updates from keepalive process",
                )

            # Track unresolved group members so we can re-resolve them
            # when keepalive discovers new devices on the network. The
            # same reconciliation path is driven by two triggers:
            #
            #  1. per-device new-bulb events (a bulb appears mid-run)
            #  2. whole-map updates from keepalive (startup retained
            #     snapshot + every subsequent scan cycle)
            #
            # Trigger (2) exists because bulbs that were already known
            # to keepalive before the server started never generate
            # new-bulb events — the retained map simply arrives with
            # them present. Without a map-update hook, the server's
            # unresolved backlog could never drain on a cold start
            # where the initial ARP wait expired before the retained
            # map landed.
            _pending_unresolved: list[tuple[str, str]] = list(unresolved)
            # Also track registered-but-unloaded devices. A device that
            # was offline during step 4b's auto-load gets captured here
            # and auto-loaded the moment keepalive sees it.
            _pending_registered: set[str] = {
                mac for mac in device_reg.all_devices()
                if mac not in mac_to_ip
            }
            _reconcile_lock: threading.Lock = threading.Lock()
            _existing_on_new: Optional[Callable] = keepalive._on_new_bulb

            def _reconcile_devices() -> None:
                """Drain the unresolved/unregistered backlogs.

                Called on every keepalive map update and every new-bulb
                event. Idempotent: re-running when nothing has changed
                is a no-op because resolved items are removed from the
                backlogs as they're loaded.
                """
                with _reconcile_lock:
                    needs_reload: bool = False

                    # Group-member backlog.
                    still_unresolved: list[tuple[str, str]] = []
                    for group_name, ident in _pending_unresolved:
                        resolved_ip: Optional[str] = (
                            device_reg.resolve_to_ip(ident, keepalive)
                        )
                        if resolved_ip is None:
                            still_unresolved.append((group_name, ident))
                            continue
                        with dm._lock:
                            members: list[str] = dm._group_config.get(
                                group_name, [],
                            )
                            if resolved_ip not in members:
                                members.append(resolved_ip)
                                dm._group_config[group_name] = members
                        if resolved_ip not in dm._device_ips:
                            dm._device_ips.append(resolved_ip)
                        needs_reload = True
                        logging.info(
                            "Late resolve: '%s' → %s (group '%s')",
                            ident, resolved_ip, group_name,
                        )
                    _pending_unresolved.clear()
                    _pending_unresolved.extend(still_unresolved)

                    # Registered-device backlog — devices that were
                    # offline at step 4b but are now in ARP.
                    mac_to_ip_now: dict[str, str] = (
                        keepalive.known_bulbs_by_mac
                    )
                    still_registered: set[str] = set()
                    for mac in _pending_registered:
                        ip_now: Optional[str] = mac_to_ip_now.get(mac)
                        if ip_now is None:
                            still_registered.add(mac)
                            continue
                        if ip_now not in dm._device_ips:
                            dm._device_ips.append(ip_now)
                        needs_reload = True
                        label: Optional[str] = device_reg.mac_to_label(
                            mac,
                        )
                        logging.info(
                            "Late auto-load: registered %s (%s) at %s",
                            label or "?", mac, ip_now,
                        )
                    _pending_registered.clear()
                    _pending_registered.update(still_registered)

                    if needs_reload:
                        try:
                            dm.load_devices()
                        except Exception as exc:
                            logging.warning(
                                "Late resolve reload failed: %s", exc,
                            )

            def _on_new_with_power(ip: str, mac: str) -> None:
                if _existing_on_new is not None:
                    _existing_on_new(ip, mac)
                dm.query_power_state(ip)
                _reconcile_devices()

            keepalive._on_new_bulb = _on_new_with_power

            # Whole-map reconciliation fires on every retained-map
            # delivery and every subsequent scan-cycle publish. This
            # is the hook that closes the startup cold-cache race:
            # even if the initial ARP wait timed out before the map
            # arrived, the map-update callback will drain the backlog
            # the moment it lands.
            def _on_device_map_reconcile(_data: dict[str, str]) -> None:
                _reconcile_devices()

            keepalive._on_device_map = _on_device_map_reconcile

            # Run one reconciliation right now in case the retained
            # map arrived between step 3 and here — a quick sweep
            # catches anything already waiting without waiting for the
            # next scan cycle.
            _reconcile_devices()

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
                    server._mqtt_bridge = mqtt_bridge

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

            # Start BLE subsystem.  This is unconditional even when
            # ble_triggers is empty, because BleTriggerManager has
            # two responsibilities:
            #   (1) ALWAYS hydrate BleSensorData from the
            #       glowup-ble-sensor producer's signal + status
            #       topics — this is the source of truth for
            #       /api/ble/sensors.
            #   (2) ONLY when a label has an entry in the ble_triggers
            #       config block, fire group power-on actions and
            #       run the watchdog timeout.
            # An empty ble_triggers block leaves the manager in
            # observe-only mode (data flows, no actions fire).
            # See docs/28-ble-sensors.md and the comment block on
            # BleTriggerManager.start().
            if _MQTT_AVAILABLE:
                ble_trigger_cfg: dict = config.get("ble_triggers", {})
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
            # Let DeviceManager publish group:{name}:any_on / all_off
            # signals on every power change so combinators can gate on
            # bulb state (see operators/combine.py).
            dm.attach_signal_bus(signal_bus)

            mqtt_cfg: dict = config.get("mqtt", {})
            broker_addr: str = mqtt_cfg.get("broker", "localhost")
            broker_port: int = mqtt_cfg.get("port", 1883)

            # -- Adapter proxies (out-of-process adapters) --------------------
            # Adapters run as separate processes via run_adapter.py.
            # The server creates AdapterProxy instances that subscribe
            # to MQTT heartbeats, status, and command responses.
            # proc_mqtt was created in Step 0 above.
            if proc_mqtt is not None:
                # Create proxies for each adapter type.  No zigbee proxy
                # — Zigbee runs as glowup-zigbee-service on broker-2
                # and publishes cross-host direct to the hub on
                # glowup/signals/*; the hub consumes it through the
                # _on_remote_signal callback below.
                vivint_proxy = AdapterProxy("vivint", proc_mqtt)
                nvr_proxy = AdapterProxy("nvr", proc_mqtt)
                printer_proxy = AdapterProxy("printer", proc_mqtt)
                matter_proxy = AdapterProxy("matter", proc_mqtt)
                # No `ble` AdapterProxy: the BLE pipeline is the
                # service-pattern producer glowup-ble-sensor on
                # broker-2.  See the comment block above the BLE
                # import at the top of this file, plus
                # docs/35-service-vs-adapter.md.

                # Store on server for handler access.
                server._vivint_adapter = vivint_proxy
                server._nvr_adapter = nvr_proxy
                server._matter_adapter = matter_proxy
                server._printer_adapter = printer_proxy

                # Subscribe to signals from adapter processes.
                # Adapter processes publish to glowup/signals/{name};
                # we feed those into the local SignalBus so operators
                # and automations continue to work.
                def _on_remote_signal(
                    client: Any, userdata: Any, message: Any,
                ) -> None:
                    # topic: glowup/signals/{signal_name}
                    # Use write_local — NOT write — to avoid
                    # republishing back to MQTT (infinite loop).
                    parts: list[str] = message.topic.split("/", 2)
                    if len(parts) < 3:
                        return
                    sig_name: str = parts[2]
                    # Liveness stamp for /api/home/health's "zigbee"
                    # probe.  Every non-time signal on this topic
                    # originates from a broker-2 producer (currently
                    # glowup-zigbee-service and glowup-ble-sensor).
                    # time:* signals come from the hub's own scheduler
                    # and would mask a silent broker-2 outage, so they
                    # are excluded.  Stamp before JSON parsing so
                    # malformed payloads still count as "broker-2 is
                    # alive and publishing."  See
                    # feedback_read_the_producer_first.md for why this
                    # probe lives on the consumer side of signals/#
                    # rather than on glowup/zigbee/# (which has no
                    # producer anywhere in the current architecture).
                    if not sig_name.startswith("time:"):
                        GlowUpRequestHandler.broker2_signals_last_ts = (
                            time.time()
                        )
                    try:
                        sig_value: Any = json.loads(message.payload)
                    except (json.JSONDecodeError, ValueError):
                        return
                    if signal_bus is not None:
                        signal_bus.write_local(sig_name, sig_value)

                    # Feed power-related signals to PowerLogger.
                    # Signal names from the Zigbee adapter are
                    # "{device}:{property}" (e.g. "ML_Power:power").
                    # PowerLogger.record() filters for power properties
                    # internally and throttles writes per device.
                    #
                    # Special case: "{device}:_availability" is a
                    # control signal from the Zigbee adapter indicating
                    # that Z2M's availability tracker has flipped the
                    # device online (1.0) or offline (0.0).  On an
                    # offline signal, call mark_offline() so the
                    # logger writes a NULL sentinel row and clears its
                    # carry-forward state — this is the defense
                    # against retained-MQTT replays reviving a plug
                    # that has actually been switched off.  See
                    # feedback_retained_mqtt_replays.md.
                    if GlowUpRequestHandler.power_logger is not None:
                        sig_parts: list[str] = sig_name.split(":", 1)
                        if len(sig_parts) == 2:
                            device_name: str = sig_parts[0]
                            prop_name: str = sig_parts[1]
                            if prop_name == "_availability":
                                try:
                                    if float(sig_value) == 0.0:
                                        GlowUpRequestHandler.power_logger.mark_offline(
                                            device_name,
                                        )
                                except (ValueError, TypeError):
                                    pass
                            else:
                                try:
                                    GlowUpRequestHandler.power_logger.record(
                                        device_name, prop_name,
                                        float(sig_value),
                                    )
                                except (ValueError, TypeError):
                                    pass

                proc_mqtt.subscribe("glowup/signals/#", qos=0)
                proc_mqtt.message_callback_add(
                    "glowup/signals/#", _on_remote_signal,
                )
                logging.info(
                    "Subscribed to remote signals — adapter processes "
                    "feed into local SignalBus",
                )

                # -- Satellite health subscriptions --------------------
                # Two topics, both feed into GlowUpRequestHandler's
                # class-level dicts for /api/satellites/health and the
                # "satellites" block in /api/home/health.
                #
                #   glowup/voice/status/{room}         — heartbeat
                #   glowup/voice/health/reply/{room}   — deep-check reply
                #
                # The heartbeat callback tracks liveness; the reply
                # callback stores the full subsystem report and wakes
                # any on-demand waiter keyed by correlation id.  Both
                # callbacks run on the paho network thread — they
                # must be cheap and must not raise.

                def _on_satellite_heartbeat(
                    client: Any, userdata: Any, message: Any,
                ) -> None:
                    # topic: glowup/voice/status/{room}
                    parts: list[str] = message.topic.split("/", 3)
                    if len(parts) < 4:
                        return
                    room: str = parts[3]
                    try:
                        payload: dict[str, Any] = json.loads(
                            message.payload,
                        )
                    except (json.JSONDecodeError, ValueError):
                        payload = {}
                    with GlowUpRequestHandler.satellite_state_lock:
                        GlowUpRequestHandler.satellite_heartbeats[room] = {
                            "ts": time.time(),
                            "payload": payload,
                        }

                proc_mqtt.subscribe(
                    f"{_voice_c.TOPIC_STATUS_PREFIX}/#", qos=0,
                )
                proc_mqtt.message_callback_add(
                    f"{_voice_c.TOPIC_STATUS_PREFIX}/#",
                    _on_satellite_heartbeat,
                )

                def _on_satellite_health_reply(
                    client: Any, userdata: Any, message: Any,
                ) -> None:
                    # topic: glowup/voice/health/reply/{room_slug}
                    try:
                        report: dict[str, Any] = json.loads(
                            message.payload,
                        )
                    except (json.JSONDecodeError, ValueError):
                        logging.warning(
                            "Unparseable satellite health reply on %s",
                            message.topic,
                        )
                        return
                    room: str = str(report.get("room", ""))
                    corr_id: str = str(report.get("id", ""))
                    if not room:
                        return
                    # Stash the report keyed by room (latest reply
                    # wins) and, if an on-demand waiter is watching
                    # this correlation id, wake it.
                    with GlowUpRequestHandler.satellite_state_lock:
                        GlowUpRequestHandler.satellite_health_replies[room] = (
                            report
                        )
                        waiter: Optional[threading.Event] = (
                            GlowUpRequestHandler.satellite_health_events
                            .get(corr_id)
                        )
                    if waiter is not None:
                        waiter.set()

                proc_mqtt.subscribe(
                    f"{_voice_c.TOPIC_HEALTH_REPLY_PREFIX}/#", qos=1,
                )
                proc_mqtt.message_callback_add(
                    f"{_voice_c.TOPIC_HEALTH_REPLY_PREFIX}/#",
                    _on_satellite_health_reply,
                )

                # Hand the proc_mqtt client to the handler class so
                # on-demand POST /api/satellites/{room}/health/check
                # and POST /api/sdr/frequency can publish requests
                # without re-opening a client.
                GlowUpRequestHandler.satellite_probe_client = proc_mqtt
                GlowUpRequestHandler._mqtt_client = proc_mqtt
                GlowUpRequestHandler._sdr_status = {}

                # -- SDR status subscription --------------------------
                # The SDR service publishes status blobs to
                # glowup/sdr/status/{label}.  Store the latest per
                # label for the GET /api/sdr/status endpoint.

                def _on_sdr_status(
                    client: Any, userdata: Any, message: Any,
                ) -> None:
                    parts: list[str] = message.topic.split("/", 3)
                    if len(parts) < 4:
                        return
                    label: str = parts[3]
                    try:
                        payload: dict = json.loads(message.payload)
                        payload["_received_at"] = time.time()
                        GlowUpRequestHandler._sdr_status[label] = payload
                    except (json.JSONDecodeError, ValueError):
                        pass

                proc_mqtt.subscribe("glowup/sdr/status/#", qos=0)
                proc_mqtt.message_callback_add(
                    "glowup/sdr/status/#", _on_sdr_status,
                )

                # -- ADS-B aircraft subscription ----------------------
                GlowUpRequestHandler._adsb_aircraft = {}

                def _on_adsb_aircraft(
                    client: Any, userdata: Any, message: Any,
                ) -> None:
                    try:
                        GlowUpRequestHandler._adsb_aircraft = json.loads(
                            message.payload,
                        )
                    except (json.JSONDecodeError, ValueError):
                        pass

                proc_mqtt.subscribe("glowup/sdr/adsb/aircraft", qos=0)
                proc_mqtt.message_callback_add(
                    "glowup/sdr/adsb/aircraft", _on_adsb_aircraft,
                )

                # -- Ernie (.153) sniffer persistence -------------------
                # BLE seen/events and TPMS decodes are now persisted in
                # PostgreSQL by dedicated logger modules under
                # `infrastructure/`.  Each logger owns its own paho
                # client + subscription, so this block no longer wires
                # the in-process ring/dict caches that used to live
                # here.  Handlers/ernie.py queries the loggers
                # directly — the dashboard is now durable across
                # server restarts.  Ernie's own thermal heartbeats are
                # captured by the generic `ThermalLogger`
                # (`glowup/hardware/thermal/+`) — see the handler for
                # the per-ernie lookup.

                # -- Periodic satellite prober --------------------------
                # Every HUB_SATELLITE_PROBE_INTERVAL_S the hub
                # broadcasts a TOPIC_HEALTH_REQUEST with room=null.
                # Satellites answer on their reply topic.  Nobody has
                # to remember to trigger it — continuous visibility.
                # The thread is a daemon so it dies with the server
                # process; no explicit shutdown needed.

                def _satellite_probe_loop() -> None:
                    """Broadcast a periodic satellite health request.

                    Runs until the server process exits.  Failures
                    are logged and retried on the next tick — the
                    loop must not die on transient errors.
                    """
                    import uuid
                    interval: float = (
                        _voice_c.HUB_SATELLITE_PROBE_INTERVAL_S
                    )
                    # Small initial delay so the first probe lands
                    # after satellites have had time to reconnect
                    # across a server restart.
                    time.sleep(15.0)
                    while True:
                        try:
                            corr_id: str = f"hub-{uuid.uuid4().hex[:12]}"
                            payload: bytes = json.dumps({
                                "id": corr_id,
                                "room": None,
                            }).encode("utf-8")
                            proc_mqtt.publish(
                                _voice_c.TOPIC_HEALTH_REQUEST,
                                payload, qos=1,
                            )
                            logging.debug(
                                "Satellite probe broadcast id=%s",
                                corr_id,
                            )
                        except Exception as exc:
                            logging.warning(
                                "Satellite probe broadcast failed: %s",
                                exc,
                            )
                        time.sleep(interval)

                probe_thread: threading.Thread = threading.Thread(
                    target=_satellite_probe_loop,
                    name="satellite-probe",
                    daemon=True,
                )
                probe_thread.start()
                logging.info(
                    "Satellite health prober running every %.0fs",
                    _voice_c.HUB_SATELLITE_PROBE_INTERVAL_S,
                )


                # Wire Matter proxy into scheduler for matter: groups.
                # MatterProxyWrapper provides the adapter-compatible
                # interface (power_on/off, get_device_names) that the
                # scheduler expects, translating to proxy commands.
                matter_wrapper: MatterProxyWrapper = MatterProxyWrapper(
                    matter_proxy,
                )
                server._matter_adapter = matter_wrapper
                if sched is not None:
                    sched.set_matter_adapter(matter_wrapper)

            # Power logger — SQLite storage for smart plug readings.
            # Power logger receives readings from signals via MQTT
            # now — the zigbee adapter publishes from its own process.
            try:
                from infrastructure.power_logger import PowerLogger, DEFAULT_DSN as _POWER_DEFAULT_DSN
                power_log = PowerLogger(
                    dsn=os.environ.get("GLOWUP_DIAG_DSN", _POWER_DEFAULT_DSN),
                )
                GlowUpRequestHandler.power_logger = power_log
            except Exception as exc:
                logging.warning("Power logger unavailable: %s", exc)
                power_log = None

            # Thermal logger — SQLite + paho-mqtt subscriber for the
            # hardware thermal telemetry published by the per-Pi
            # contrib/sensors/pi_thermal_sensor.py daemons on topic
            # glowup/hardware/thermal/<node_id>.  Guarded against
            # missing paho-mqtt per the optional-modules architecture
            # rule — if paho is not importable the subscriber is a
            # no-op and the /thermal dashboard degrades to whatever
            # historical data was already on disk.
            try:
                from infrastructure.thermal_logger import ThermalLogger, DEFAULT_DSN as _THERMAL_DEFAULT_DSN
                thermal_log = ThermalLogger(
                    dsn=os.environ.get("GLOWUP_DIAG_DSN", _THERMAL_DEFAULT_DSN),
                )
                thermal_log.start_subscriber(
                    broker_host=broker_addr,
                    broker_port=broker_port,
                )
                GlowUpRequestHandler.thermal_logger = thermal_log
            except Exception as exc:
                logging.warning("Thermal logger unavailable: %s", exc)
                thermal_log = None

            # TPMS logger — PG storage for rtl_433 tire-pressure decodes
            # published by ernie (.153).  See
            # infrastructure/tpms_logger.py.  Same guarded-import pattern
            # as thermal: missing psycopg2 or paho downgrades the
            # subscriber to a no-op without crashing the server.
            try:
                from infrastructure.tpms_logger import (
                    TpmsLogger,
                    DEFAULT_DSN as _TPMS_DEFAULT_DSN,
                )
                tpms_log = TpmsLogger(
                    dsn=os.environ.get("GLOWUP_DIAG_DSN", _TPMS_DEFAULT_DSN),
                )
                tpms_log.start_subscriber(
                    broker_host=broker_addr,
                    broker_port=broker_port,
                )
                GlowUpRequestHandler.tpms_logger = tpms_log
            except Exception as exc:
                logging.warning("TPMS logger unavailable: %s", exc)
                tpms_log = None

            # BLE sniffer logger — PG storage for ernie's v2 BLE
            # state/event streams.  See
            # infrastructure/ble_sniffer_logger.py.
            try:
                from infrastructure.ble_sniffer_logger import (
                    BleSnifferLogger,
                    DEFAULT_DSN as _BLE_DEFAULT_DSN,
                )
                ble_log = BleSnifferLogger(
                    dsn=os.environ.get("GLOWUP_DIAG_DSN", _BLE_DEFAULT_DSN),
                )
                ble_log.start_subscriber(
                    broker_host=broker_addr,
                    broker_port=broker_port,
                )
                GlowUpRequestHandler.ble_sniffer_logger = ble_log
            except Exception as exc:
                logging.warning("BLE sniffer logger unavailable: %s", exc)
                ble_log = None

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
                config["_mqtt_client"] = proc_mqtt
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
        # Adapters run as separate processes — no stop() calls needed.
        # Just disconnect the process communication MQTT client.
        if media_mgr is not None:
            media_mgr.shutdown()
        if orch is not None:
            orch.stop()
        # AutomationManager retired — its trigger logic moved to
        # the operator framework (see operators/triggers).  No
        # automation_mgr.stop() call here.
        if ble_trigger_mgr is not None:
            ble_trigger_mgr.stop()
        if mqtt_bridge is not None:
            mqtt_bridge.stop()
        if proc_mqtt is not None:
            proc_mqtt.loop_stop()
            proc_mqtt.disconnect()
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
