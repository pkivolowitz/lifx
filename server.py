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
    POST /api/schedule/{index}/enabled   Enable or disable a schedule entry
    GET  /api/media/sources              List media sources with status
    GET  /api/media/signals              List available signal names
    POST /api/media/sources/{name}/start Manually start a media source
    POST /api/media/sources/{name}/stop  Manually stop a media source
    POST /api/media/signals/ingest       Write signals from external source
    GET  /api/diagnostics/now_playing    Currently playing effects (from DB)
    GET  /api/diagnostics/history        Recent effect history (from DB)
    GET  /dashboard                      Web dashboard (HTML)
Usage::

    python3 server.py server.json
    python3 server.py --dry-run server.json   # preview schedule
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "2.0"

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

from effects import get_registry, create_effect, HSBK, HSBK_MAX, KELVIN_DEFAULT
from emitters import Emitter
from emitters.lifx import LifxEmitter
from emitters.virtual import VirtualMultizoneEmitter
from engine import Controller
from mqtt_bridge import MqttBridge, PAHO_AVAILABLE as _MQTT_AVAILABLE
from media import MediaManager, SignalBus
from solar import SunTimes, sun_times
from transport import LifxDevice, SendMode

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

# ARP-based bulb discovery and keepalive daemon.
from bulb_keepalive import BulbKeepAlive

# MAC-based device identity registry.
from device_registry import DeviceRegistry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default HTTP port for the REST API.
DEFAULT_PORT: int = 8420

# Server-Sent Events polling rate (Hz) for live color streaming.
SSE_POLL_HZ: float = 4.0

# Computed interval between SSE polls (seconds).
SSE_POLL_INTERVAL: float = 1.0 / SSE_POLL_HZ

# Maximum allowed size of an HTTP request body (bytes).
MAX_REQUEST_BODY: int = 65536

# How often the scheduler thread checks for schedule transitions (seconds).
SCHEDULER_POLL_SECONDS: int = 30

# Default fade-to-black duration when stopping an effect (ms).
DEFAULT_FADE_MS: int = 500

# HTTP Authorization header name.
AUTH_HEADER: str = "Authorization"

# Bearer token prefix in the Authorization header.
BEARER_PREFIX: str = "Bearer "

# Default configuration file path (for Pi deployment).
DEFAULT_CONFIG_PATH: str = "/etc/glowup/server.json"

# Filename for user-saved effect parameter defaults (co-located with config).
EFFECT_DEFAULTS_FILENAME: str = "effect_defaults.json"

# Prefix used to distinguish group identifiers from IP addresses in
# the API path and internal device dictionaries.
GROUP_PREFIX: str = "group:"

# Logging format matching scheduler.py.
LOG_FORMAT: str = "%(asctime)s %(levelname)s %(message)s"
LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"

# Regex for symbolic time specifications (reused from scheduler.py).
_SYMBOLIC_RE: re.Pattern[str] = re.compile(
    r"^(sunrise|sunset|dawn|dusk|noon|midnight)"
    r"(?:([+-])"
    r"(?:(\d+)h)?"
    r"(?:(\d+)m)?"
    r")?$"
)

# Regex for fixed HH:MM time specifications.
_FIXED_TIME_RE: re.Pattern[str] = re.compile(r"^(\d{1,2}):(\d{2})$")

# Valid hours range for fixed time specs.
MAX_HOUR: int = 23
MAX_MINUTE: int = 59

# API path prefix.
API_PREFIX: str = "/api"

# Maximum failed authentication attempts per IP before throttling.
AUTH_RATE_LIMIT: int = 10

# Time window for the auth rate limiter (seconds).
AUTH_RATE_WINDOW: int = 60

# SSE stream timeout — close idle connections after this many seconds.
SSE_TIMEOUT_SECONDS: float = 3600.0

# Identify pulse duration (seconds).
IDENTIFY_DURATION_SECONDS: float = 10.0

# Seconds per full brightness cycle during identify.
IDENTIFY_CYCLE_SECONDS: float = 3.0

# Seconds between brightness updates during identify (20 fps).
IDENTIFY_FRAME_INTERVAL: float = 0.05

# Minimum brightness fraction during identify pulse (5%).
IDENTIFY_MIN_BRI: float = 0.05

# Per-device UDP query timeout for /api/command/discover (seconds).
COMMAND_DISCOVER_TIMEOUT_SECONDS: float = 4.0

# Maximum identify duration accepted from /api/command/identify (seconds).
COMMAND_IDENTIFY_MAX_DURATION: float = 60.0

# Day-of-week letter to weekday index (Monday=0 .. Sunday=6).
# Matches Python's date.weekday() convention.
DAY_LETTER_TO_WEEKDAY: dict[str, int] = {
    "M": 0, "T": 1, "W": 2, "R": 3, "F": 4, "S": 5, "U": 6,
}

# All valid day letters (for validation).
VALID_DAY_LETTERS: str = "MTWRFSU"

# Error message for device identifier resolution failures.
DEVICE_RESOLVE_ERROR: str = "Cannot resolve device identifier"


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
    _Route("GET", ("api", "fleet"),
           "_handle_get_fleet"),
    _Route("GET", ("api", "diagnostics", "now_playing"),
           "_handle_get_diag_now_playing"),
    _Route("GET", ("api", "diagnostics", "history"),
           "_handle_get_diag_history"),
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
    _Route("POST", ("api", "devices", "{id}", "identify"),
           "_handle_post_identify", device_param="id"),
    _Route("POST", ("api", "devices", "{id}", "resume"),
           "_handle_post_resume", device_param="id"),
    _Route("POST", ("api", "devices", "{id}", "reset"),
           "_handle_post_reset", device_param="id"),
    _Route("POST", ("api", "devices", "{id}", "nickname"),
           "_handle_post_nickname", device_param="id"),

    # -- POST: parameterized -------------------------------------------------
    _Route("POST", ("api", "effects", "{name}", "defaults"),
           "_handle_post_effect_defaults"),
    _Route("POST", ("api", "schedule", "{index}", "enabled"),
           "_handle_post_schedule_enabled",
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

    # -- DELETE --------------------------------------------------------------
    _Route("DELETE", ("api", "registry", "device", "{mac}"),
           "_handle_delete_registry_device",
           unquote_params=("mac",)),
    _Route("DELETE", ("api", "command", "identify", "{id}"),
           "_handle_delete_command_identify",
           device_param="id", unquote_params=("id",)),
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

class _RateLimiter:
    """Simple per-IP rate limiter for authentication failures.

    Tracks failed auth attempts within a sliding time window.  When a
    client exceeds :data:`AUTH_RATE_LIMIT` failures within
    :data:`AUTH_RATE_WINDOW` seconds, further requests are rejected
    with HTTP 429 until the window expires.
    """

    def __init__(self, limit: int = AUTH_RATE_LIMIT,
                 window: int = AUTH_RATE_WINDOW) -> None:
        self._limit: int = limit
        self._window: int = window
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._lock: threading.Lock = threading.Lock()

    def record_failure(self, ip: str) -> None:
        """Record a failed authentication attempt from *ip*."""
        now: float = time_mod.time()
        with self._lock:
            self._failures[ip].append(now)

    def is_blocked(self, ip: str) -> bool:
        """Return ``True`` if *ip* has exceeded the failure limit."""
        now: float = time_mod.time()
        cutoff: float = now - self._window
        with self._lock:
            attempts: list[float] = self._failures.get(ip, [])
            # Prune old entries.
            attempts[:] = [t for t in attempts if t > cutoff]
            return len(attempts) >= self._limit

    def clear(self, ip: str) -> None:
        """Clear failure history for *ip* (e.g. after a successful auth)."""
        with self._lock:
            self._failures.pop(ip, None)


# Singleton rate limiter — shared across all handler threads.
_rate_limiter: _RateLimiter = _RateLimiter()


# ---------------------------------------------------------------------------
# IP validation
# ---------------------------------------------------------------------------

def _validate_ip(ip_str: str) -> bool:
    """Return ``True`` if *ip_str* is a valid IPv4 or IPv6 address.

    Args:
        ip_str: The string to validate.

    Returns:
        ``True`` if valid, ``False`` otherwise.
    """
    try:
        ipaddress.ip_address(ip_str)
        return True
    except ValueError:
        return False


def _is_group_id(device_id: str) -> bool:
    """Return ``True`` if *device_id* is a group identifier."""
    return device_id.startswith(GROUP_PREFIX)


def _group_name_from_id(device_id: str) -> str:
    """Extract the group name from a ``group:name`` identifier."""
    return device_id[len(GROUP_PREFIX):]


def _group_id_from_name(group_name: str) -> str:
    """Build a ``group:name`` identifier from a group name."""
    return GROUP_PREFIX + group_name


def _validate_device_id(device_id: str) -> bool:
    """Return ``True`` if *device_id* is a valid IP or group identifier.

    Args:
        device_id: IP address string or ``group:name`` identifier.

    Returns:
        ``True`` if valid, ``False`` otherwise.
    """
    if _is_group_id(device_id):
        return len(device_id) > len(GROUP_PREFIX)
    return _validate_ip(device_id)


# ---------------------------------------------------------------------------
# Device manager
# ---------------------------------------------------------------------------

class DeviceManager:
    """Manage LIFX devices loaded from the configuration file.

    The server does not perform broadcast discovery.  All device IPs
    come from the ``groups`` section of the config file.  Each device
    is contacted directly via a single UDP query — much faster and more
    reliable than broadcast discovery, which requires multiple retries
    with long timeouts and is defeated by mesh routers that filter
    broadcast packets between nodes.

    Thread-safe: all public methods acquire the internal lock before
    modifying shared state.  The :class:`Controller` instances themselves
    are already thread-safe.

    Attributes:
        devices:     Dict mapping device IP to :class:`LifxDevice`.
        controllers: Dict mapping device IP to :class:`Controller`.
    """

    def __init__(self, device_ips: list[str],
                 nicknames: Optional[dict[str, str]] = None,
                 config_dir: Optional[str] = None,
                 groups: Optional[dict[str, list[str]]] = None) -> None:
        """Initialize with the device IPs from the config file.

        Args:
            device_ips: List of device IPs extracted from the config
                        ``groups`` section.  These are the *only* devices
                        the server will manage.
            nicknames:  Optional mapping of device IP to user-assigned
                        display name, loaded from the config file.
            config_dir: Directory containing the config file, used to
                        locate ``effect_defaults.json``.
            groups:     Group name → IP list mapping from the config.
                        Multi-device groups are exposed as virtual
                        multizone devices with a unified zone canvas.
        """
        self._devices: dict[str, LifxDevice] = {}
        self._emitters: dict[str, Emitter] = {}
        self._controllers: dict[str, Controller] = {}
        self._lock: threading.Lock = threading.Lock()
        # Override tracking: maps device ID (IP or group:name) to the
        # schedule entry name that was active when the phone took over.
        self._overrides: dict[str, Optional[str]] = {}
        # Device IPs from config groups — the only source of devices.
        self._device_ips: list[str] = device_ips
        # Group config: group name → ordered list of member IPs.
        self._group_config: dict[str, list[str]] = groups or {}
        # User-assigned nicknames: IP → display name.
        self._nicknames: dict[str, str] = nicknames or {}
        # User-saved effect parameter defaults: effect name → {param: value}.
        self._effect_defaults: dict[str, dict[str, Any]] = {}
        self._defaults_path: Optional[str] = None
        if config_dir is not None:
            self._defaults_path = os.path.join(
                config_dir, EFFECT_DEFAULTS_FILENAME,
            )
            self._load_effect_defaults()
        # Readiness flag: False until initial load completes.
        self._ready: bool = False
        # Optional diagnostics logger (None if psycopg2 or DB unavailable).
        self._diag: Optional[Any] = None
        if _HAS_DIAGNOSTICS:
            self._diag = DiagnosticsLogger.from_env()
            if self._diag is not None:
                self._diag.close_stale_records()

    def load_devices(self) -> list[dict[str, Any]]:
        """Query each configured device IP and cache the results.

        Creates a :class:`LifxDevice` for every IP in the config,
        queries its metadata (version, label, group, zone count),
        and populates the internal device map.  Unreachable devices
        are logged as warnings but do not prevent the server from
        starting.

        After loading individual devices, wraps each in a
        :class:`LifxEmitter` and creates a
        :class:`VirtualMultizoneEmitter` for every multi-device group
        in the config.  The virtual emitter combines member zones into
        a single unified canvas.  The order of IPs in the group array
        determines the zone layout (first IP's zones come first).

        Returns:
            A list of JSON-serializable device info dicts.
        """
        new_map: dict[str, LifxDevice] = {}
        for ip in self._device_ips:
            try:
                dev: LifxDevice = LifxDevice(ip)
                dev.query_all()
                if dev.product is not None:
                    new_map[dev.ip] = dev
                    logging.info(
                        "  loaded %s — %s (%s) [%s zones]",
                        dev.label or "?", dev.product_name or "?",
                        dev.ip, dev.zone_count or "?",
                    )
                else:
                    logging.warning("Device %s responded but returned no product info", ip)
                    dev.close()
            except Exception as exc:
                logging.warning("Device %s unreachable: %s", ip, exc)

        with self._lock:
            # Close sockets for devices no longer in the config.
            gone_ips: set[str] = set(self._devices) - set(new_map)
            for ip in gone_ips:
                self._stop_and_remove(ip)

            # Replace old device objects with freshly queried ones.
            for ip, new_dev in new_map.items():
                old_dev: Optional[LifxDevice] = self._devices.get(ip)
                if old_dev is not None and old_dev is not new_dev:
                    ctrl: Optional[Controller] = self._controllers.get(ip)
                    if ctrl is not None:
                        ctrl.stop(fade_ms=0)
                        del self._controllers[ip]
                    old_dev.close()

            self._devices = new_map

            # Build emitter wrappers for all individual devices.
            new_emitters: dict[str, Emitter] = {}
            for ip, dev in new_map.items():
                new_emitters[ip] = LifxEmitter.from_device(dev)

            # Build VirtualMultizoneEmitters for multi-device groups.
            for group_name, ips in self._group_config.items():
                if len(ips) < 2:
                    continue
                member_emitters: list[Emitter] = [
                    new_emitters[ip] for ip in ips
                    if ip in new_emitters
                ]
                if len(member_emitters) < 2:
                    logging.warning(
                        "Group '%s' has fewer than 2 reachable devices "
                        "(%d/%d) — skipping virtual emitter",
                        group_name, len(member_emitters), len(ips),
                    )
                    continue
                group_id: str = _group_id_from_name(group_name)
                # Stop any existing controller for this group.
                old_ctrl: Optional[Controller] = self._controllers.get(
                    group_id,
                )
                if old_ctrl is not None:
                    old_ctrl.stop(fade_ms=0)
                    del self._controllers[group_id]
                vem: VirtualMultizoneEmitter = VirtualMultizoneEmitter(
                    member_emitters, name=group_name, owns_emitters=False,
                )
                new_emitters[group_id] = vem
                logging.info(
                    "  group '%s' — %d emitters, %d zones",
                    group_name, len(member_emitters), vem.zone_count,
                )

            # Clean up controllers for emitter IDs that no longer exist
            # (e.g., groups whose members all went offline).
            gone_ids: set[str] = set(self._emitters) - set(new_emitters)
            for eid in gone_ids:
                stale_ctrl: Optional[Controller] = self._controllers.pop(
                    eid, None,
                )
                if stale_ctrl is not None:
                    stale_ctrl.stop(fade_ms=0)
                self._overrides.pop(eid, None)

            self._emitters = new_emitters
            self._ready = True

        return self._devices_as_list()

    @property
    def ready(self) -> bool:
        """Return ``True`` once initial device loading has completed."""
        return self._ready

    def get_device(self, ip: str) -> Optional[LifxDevice]:
        """Look up a cached LIFX device by IP.

        Only returns individual :class:`LifxDevice` instances — not
        virtual groups.  Use :meth:`get_emitter` for the universal
        registry that includes groups.

        Args:
            ip: Device IP address.

        Returns:
            The :class:`LifxDevice`, or ``None`` if not found.
        """
        with self._lock:
            return self._devices.get(ip)

    def get_emitter(self, device_id: str) -> Optional[Emitter]:
        """Look up an emitter by device ID (IP or group identifier).

        This is the universal lookup — covers both individual
        :class:`LifxEmitter` instances and :class:`VirtualMultizoneEmitter`
        groups.

        Args:
            device_id: Device IP address or group identifier
                       (e.g., ``"group:porch"``).

        Returns:
            The :class:`Emitter`, or ``None`` if not found.
        """
        with self._lock:
            return self._emitters.get(device_id)

    def get_controller(self, ip: str) -> Optional[Controller]:
        """Look up an existing Controller by IP (does not create one).

        Args:
            ip: Device IP address.

        Returns:
            The :class:`Controller`, or ``None`` if none exists.
        """
        with self._lock:
            return self._controllers.get(ip)

    def get_or_create_controller(self, ip: str) -> Optional[Controller]:
        """Get or lazily create a Controller for an emitter.

        Args:
            ip: Device IP address or group identifier.

        Returns:
            A :class:`Controller`, or ``None`` if the emitter is unknown.
        """
        with self._lock:
            if ip not in self._emitters:
                return None
            if ip not in self._controllers:
                em: Emitter = self._emitters[ip]
                self._controllers[ip] = Controller([em])
            return self._controllers[ip]

    def play(
        self,
        ip: str,
        effect_name: str,
        params: dict[str, Any],
        bindings: Optional[dict[str, Any]] = None,
        signal_bus: Optional[SignalBus] = None,
    ) -> dict[str, Any]:
        """Start an effect on a device.

        Args:
            ip:          Device IP address.
            effect_name: Registered effect name.
            params:      Parameter overrides.
            bindings:    Optional signal-to-param bindings for media
                         reactivity.  Each key is a param name, each
                         value is a dict with ``signal``, optional
                         ``reduce``, and optional ``scale`` fields.
            signal_bus:  Optional :class:`SignalBus` instance for
                         reading media signals during rendering.

        Returns:
            A status dict for the device.

        Raises:
            KeyError: If the device IP is not configured.
            ValueError: If the effect name is invalid.
        """
        ctrl: Optional[Controller] = self.get_or_create_controller(ip)
        if ctrl is None:
            raise KeyError(f"Unknown device: {ip}")
        # Merge user-saved defaults under explicit params.  Explicit
        # params from the API call take priority; saved defaults fill
        # in anything the caller didn't specify.
        saved: dict[str, Any] = self._effect_defaults.get(effect_name, {})
        if saved:
            merged: dict[str, Any] = dict(saved)
            merged.update(params)
            params = merged
        # Power on the emitter before playing.  The persistent committed
        # state is managed by stop() and reset() — not here — so the
        # render loop's rapid writes don't flicker against a black fallback.
        em: Optional[Emitter] = self.get_emitter(ip)
        if em is not None:
            try:
                em.power_on(duration_ms=0)
            except Exception:
                pass
        # Close the previous effect's diagnostics record before starting
        # a new one, so replaced effects get a proper stop_reason.
        if self._diag is not None:
            self._diag.log_stop(ip, stop_reason="replaced")
        ctrl.play(effect_name, bindings=bindings,
                  signal_bus=signal_bus, **params)
        if self._diag is not None:
            em_info: dict[str, Any] = em.get_info() if em else {}
            self._diag.log_play(
                device_ip=ip,
                device_label=em_info.get("label"),
                effect_name=effect_name,
                params=params,
                started_by="api",
            )
        result: dict[str, Any] = ctrl.get_status()
        result["overridden"] = self.is_overridden(ip)
        return result

    def stop(self, ip: str) -> dict[str, Any]:
        """Stop the current effect on a device and power it off.

        Mirrors the glowup.py CLI behaviour: stop the engine (which snaps
        the overlay to black), then power off the device so it does not
        remain lit on the committed firmware layer.

        Args:
            ip: Device IP address.

        Returns:
            A status dict for the device.

        Raises:
            KeyError: If the device IP is not configured.
        """
        ctrl: Optional[Controller] = self.get_or_create_controller(ip)
        if ctrl is None:
            raise KeyError(f"Unknown device: {ip}")
        ctrl.stop(fade_ms=DEFAULT_FADE_MS)
        ctrl.set_power(on=False, duration_ms=DEFAULT_FADE_MS)
        if self._diag is not None:
            self._diag.log_stop(ip, stop_reason="user")
        result: dict[str, Any] = ctrl.get_status()
        result["overridden"] = self.is_overridden(ip)
        return result

    def get_status(self, ip: str) -> dict[str, Any]:
        """Get the current effect status for a device.

        Args:
            ip: Device IP address or group identifier.

        Returns:
            A status dict, or a minimal dict if no controller exists.

        Raises:
            KeyError: If the device IP is not configured.
        """
        with self._lock:
            if ip not in self._emitters:
                raise KeyError(f"Unknown device: {ip}")
            ctrl: Optional[Controller] = self._controllers.get(ip)

        overridden: bool = self.is_overridden(ip)

        if ctrl is not None:
            result: dict[str, Any] = ctrl.get_status()
            result["overridden"] = overridden
            return result

        # No controller yet — return idle status from the emitter.
        em: Emitter = self._emitters[ip]
        return {
            "running": False,
            "effect": None,
            "params": {},
            "fps": 0,
            "overridden": overridden,
            "devices": [em.get_info()],
        }

    def set_power(self, ip: str, on: bool) -> dict[str, Any]:
        """Turn a device on or off.

        When powering off, writes black to all zones first so the LIFX
        firmware doesn't retain stale colors in non-volatile memory.
        Without this, the device shows old effect colors the next time
        it powers on — even hours or days later.

        Works for both individual devices and virtual groups — the
        emitter interface handles fan-out to group members.

        Args:
            ip: Device IP address or group identifier.
            on: ``True`` to power on, ``False`` to power off.

        Returns:
            A dict confirming the action.

        Raises:
            KeyError: If the device IP is not configured.
        """
        em: Optional[Emitter] = self.get_emitter(ip)
        if em is None:
            raise KeyError(f"Unknown device: {ip}")

        # Blank all zones before powering off so the firmware's stored
        # state is clean.  Without this, the device flashes stale colors
        # the next time it powers on.
        if not on and em.is_multizone and em.zone_count:
            blank: list[HSBK] = [
                (0, 0, 0, KELVIN_DEFAULT)
            ] * em.zone_count
            em.send_zones(blank, duration_ms=0,
                         mode=SendMode.GUARANTEED)

        if on:
            em.power_on(duration_ms=DEFAULT_FADE_MS)
        else:
            em.power_off(duration_ms=DEFAULT_FADE_MS)
        return {"ip": ip, "power": "on" if on else "off"}

    def reset(self, ip: str) -> dict[str, Any]:
        """Deep-reset a device: stop effects, clear firmware state, blank zones.

        This is the nuclear option for cleaning a device that has stale
        zone colors or a firmware-level multizone effect running inside
        the hardware.  For virtual groups, resets each member device
        individually.

        Steps per device:

        1. Stop any running software effect (immediate, no fade).
        2. Disable any firmware-level multizone effect (type 508 OFF).
        3. Power on the device (so zone writes are accepted).
        4. Write black to all zones with acknowledgment (non-rapid).
        5. Power off.

        Args:
            ip: Device IP address or group identifier.

        Returns:
            A dict confirming the reset.

        Raises:
            KeyError: If the device IP is not configured.
        """
        em: Optional[Emitter] = self.get_emitter(ip)
        if em is None:
            raise KeyError(f"Unknown device: {ip}")

        # 1. Stop any running software effect immediately.
        ctrl: Optional[Controller] = self.get_controller(ip)
        if ctrl is not None:
            ctrl.stop(fade_ms=0)

        # Collect the LifxEmitters to reset (single device or group members).
        if isinstance(em, VirtualMultizoneEmitter):
            lifx_members: list[LifxEmitter] = [
                m for m in em.get_emitter_list()
                if isinstance(m, LifxEmitter)
            ]
        elif isinstance(em, LifxEmitter):
            lifx_members = [em]
        else:
            # Non-LIFX emitters don't need firmware reset.
            return {"ip": ip, "reset": False}

        for lem in lifx_members:
            dev: LifxDevice = lem.transport

            # 2. Clear any firmware-level multizone effect.
            if dev.is_multizone:
                try:
                    dev.clear_firmware_effect()
                    logging.info("Reset %s: firmware effect cleared", dev.ip)
                except Exception as exc:
                    logging.warning(
                        "Reset %s: clear_firmware_effect failed: %s",
                        dev.ip, exc,
                    )

            # 3. Power on so zone writes are accepted.
            dev.set_power(on=True, duration_ms=0)
            time_mod.sleep(0.1)  # Brief delay for device to wake up.

            # 4. Clear the persistent committed state with set_color
            # (type 102) and also blank zones with set_zones (type 510).
            dev.set_color(0, 0, 0, KELVIN_DEFAULT, duration_ms=0)
            if dev.is_multizone and dev.zone_count:
                blank: list[HSBK] = [
                    (0, 0, 0, KELVIN_DEFAULT)
                ] * dev.zone_count
                dev.set_zones(blank, duration_ms=0,
                              mode=SendMode.GUARANTEED)

            # 5. Power off.
            dev.set_power(on=False, duration_ms=0)

        logging.info("Reset %s: device(s) cleaned and powered off", ip)
        return {"ip": ip, "reset": True}

    def identify(
        self,
        ip: str,
        *,
        on_complete: Optional[Callable[[], None]] = None,
    ) -> None:
        """Pulse a device's brightness for a fixed duration to locate it.

        Runs in a background thread so the HTTP request returns immediately.
        Stops any running effect first, then pulses warm white brightness
        in a sine wave for :data:`IDENTIFY_DURATION_SECONDS`, then powers
        the device off.

        Works for both individual devices and virtual groups — the
        emitter interface handles fan-out to group members.

        Args:
            ip:          Device IP address or group identifier.
            on_complete: Optional callback invoked when the pulse finishes
                         (e.g. to clear a phone override).

        Raises:
            KeyError: If the device IP is not configured.
        """
        em: Optional[Emitter] = self.get_emitter(ip)
        if em is None:
            raise KeyError(f"Unknown device: {ip}")

        # Stop any running effect so identify is visible.
        ctrl: Optional[Controller] = self.get_controller(ip)
        if ctrl is not None:
            ctrl.stop(fade_ms=0)

        def _pulse() -> None:
            """Background pulse loop."""
            try:
                em.power_on(duration_ms=0)
                start: float = time_mod.monotonic()
                while time_mod.monotonic() - start < IDENTIFY_DURATION_SECONDS:
                    elapsed: float = time_mod.monotonic() - start
                    phase: float = (
                        math.sin(2.0 * math.pi * elapsed / IDENTIFY_CYCLE_SECONDS)
                        + 1.0
                    ) / 2.0
                    bri_frac: float = (
                        IDENTIFY_MIN_BRI + phase * (1.0 - IDENTIFY_MIN_BRI)
                    )
                    bri: int = int(bri_frac * HSBK_MAX)

                    if em.is_multizone:
                        color: HSBK = (0, 0, bri, KELVIN_DEFAULT)
                        colors: list[HSBK] = [color] * (em.zone_count or 1)
                        em.send_zones(colors, duration_ms=0)
                    else:
                        em.send_color(0, 0, bri, KELVIN_DEFAULT, duration_ms=0)

                    time_mod.sleep(IDENTIFY_FRAME_INTERVAL)

                em.power_off(duration_ms=DEFAULT_FADE_MS)
            except Exception as exc:
                logging.warning("Identify pulse failed for %s: %s", ip, exc)
            finally:
                if on_complete is not None:
                    on_complete()

        thread: threading.Thread = threading.Thread(
            target=_pulse, daemon=True, name=f"identify-{ip}",
        )
        thread.start()

    def get_colors(self, ip: str) -> Optional[list[dict[str, int]]]:
        """Get a snapshot of the current zone colors.

        For individual devices, creates a temporary :class:`LifxDevice`
        for the read-only query to avoid socket contention with the
        engine's device.

        For virtual groups, queries each member device and concatenates
        the results in group order.

        Args:
            ip: Device IP or group identifier.

        Returns:
            A list of ``{h, s, b, k}`` dicts, or ``None`` on failure.

        Raises:
            KeyError: If the device/group is not configured.
        """
        em: Optional[Emitter] = self.get_emitter(ip)
        if em is None:
            raise KeyError(f"Unknown device: {ip}")

        # Virtual group: query each member device and concatenate.
        if isinstance(em, VirtualMultizoneEmitter):
            return self._get_group_colors(em)

        # Individual device: use a temporary device to avoid contention.
        tmp: LifxDevice = LifxDevice(ip)
        try:
            tmp.query_version()
            if tmp.is_multizone:
                tmp.query_zone_count()
            else:
                tmp.zone_count = 1
            colors = tmp.query_zone_colors() if tmp.is_multizone else None
            if colors is None:
                # Single bulb — query light state instead.
                state = tmp.query_light_state()
                if state is not None:
                    h, s, b, k, _power = state
                    colors = [(h, s, b, k)]
            if colors is not None:
                return [
                    {"h": h, "s": s, "b": b, "k": k}
                    for h, s, b, k in colors
                ]
            return None
        finally:
            tmp.close()

    def _get_group_colors(
        self, vem: VirtualMultizoneEmitter,
    ) -> Optional[list[dict[str, int]]]:
        """Query each member device of a virtual group and concatenate.

        Iterates member emitters, accesses the LIFX transport for each,
        and queries zone colors directly from the hardware.

        Args:
            vem: The virtual multizone emitter.

        Returns:
            A list of ``{h, s, b, k}`` dicts across all members, or
            ``None`` if all queries fail.
        """
        all_colors: list[dict[str, int]] = []
        for member_em in vem.get_emitter_list():
            if not isinstance(member_em, LifxEmitter):
                continue
            member_ip: str = member_em.transport.ip
            tmp: LifxDevice = LifxDevice(member_ip)
            try:
                tmp.query_version()
                if tmp.is_multizone:
                    tmp.query_zone_count()
                    colors = tmp.query_zone_colors()
                else:
                    tmp.zone_count = 1
                    state = tmp.query_light_state()
                    colors = [(state[0], state[1], state[2], state[3])] if state else None
                if colors:
                    all_colors.extend(
                        {"h": h, "s": s, "b": b, "k": k}
                        for h, s, b, k in colors
                    )
            except Exception as exc:
                logging.warning(
                    "get_colors member %s failed: %s", member_ip, exc,
                )
            finally:
                tmp.close()
        return all_colors if all_colors else None

    def list_effects(self) -> dict[str, Any]:
        """Return available effects with parameter metadata.

        Delegates to :meth:`Controller.list_effects` (static data,
        does not require a device).

        Returns:
            A dict mapping effect names to descriptions and params.
        """
        # Use a throwaway controller-like approach — list_effects is
        # a class-level query that doesn't need a device.  We can call
        # the underlying function directly.
        result: dict[str, Any] = {}
        for name, cls in get_registry().items():
            params: dict[str, Any] = {}
            for pname, pdef in cls.get_param_defs().items():
                params[pname] = {
                    "default": pdef.default,
                    "min": pdef.min,
                    "max": pdef.max,
                    "description": pdef.description,
                    "type": type(pdef.default).__name__,
                }
                if pdef.choices:
                    params[pname]["choices"] = pdef.choices
            # Overlay user-saved defaults so the app sees them.
            saved: dict[str, Any] = self._effect_defaults.get(name, {})
            for pname, sval in saved.items():
                if pname in params:
                    params[pname]["default"] = sval
            result[name] = {
                "description": cls.description,
                "params": params,
                "hidden": name.startswith("_"),
            }
        return result

    def _load_effect_defaults(self) -> None:
        """Load user-saved effect defaults from disk."""
        if self._defaults_path is None:
            return
        try:
            with open(self._defaults_path, "r") as f:
                self._effect_defaults = json.load(f)
            logging.info(
                "Loaded effect defaults for %d effects from %s",
                len(self._effect_defaults), self._defaults_path,
            )
        except FileNotFoundError:
            self._effect_defaults = {}
        except (json.JSONDecodeError, ValueError) as exc:
            logging.warning("Bad effect_defaults.json: %s", exc)
            self._effect_defaults = {}

    def _save_effect_defaults(self) -> None:
        """Persist user-saved effect defaults to disk."""
        if self._defaults_path is None:
            logging.warning("No config directory — cannot save defaults")
            return
        with open(self._defaults_path, "w") as f:
            json.dump(self._effect_defaults, f, indent=2)
        logging.info(
            "Saved effect defaults to %s", self._defaults_path,
        )

    def save_effect_defaults(
        self, effect_name: str, params: dict[str, Any],
    ) -> None:
        """Save user-tuned parameter values as the defaults for an effect.

        These override class-level Param defaults when the effect is
        created without explicit params (e.g., from the scheduler).

        Args:
            effect_name: Registered effect name.
            params:      Parameter values to save as defaults.

        Raises:
            ValueError: If the effect name is not registered.
        """
        registry: dict = get_registry()
        if effect_name not in registry:
            raise ValueError(f"Unknown effect: {effect_name}")
        self._effect_defaults[effect_name] = dict(params)
        self._save_effect_defaults()

    def devices_as_list(self) -> list[dict[str, Any]]:
        """Return configured devices as a JSON-serializable list.

        Returns:
            A list of device info dicts.
        """
        return self._devices_as_list()

    # -- Override management ------------------------------------------------

    def mark_override(self, ip: str, entry_name: Optional[str]) -> None:
        """Mark a device as phone-overridden.

        Args:
            ip:         Device IP address.
            entry_name: The schedule entry name that was active when the
                        override began, or ``None`` if none was active.
        """
        with self._lock:
            self._overrides[ip] = entry_name

    def clear_override(self, ip: str) -> None:
        """Clear the phone override for a device.

        Args:
            ip: Device IP address.
        """
        with self._lock:
            self._overrides.pop(ip, None)

    def is_overridden(self, ip: str) -> bool:
        """Check if a device is under phone control.

        Args:
            ip: Device IP address.

        Returns:
            ``True`` if the device has an active phone override.
        """
        with self._lock:
            return ip in self._overrides

    def is_overridden_or_member(self, device_id: str) -> bool:
        """Check if a device or any member of its group is overridden.

        For group identifiers (``group:name``), returns ``True`` if the
        group itself is overridden *or* any of its individual member
        devices are overridden.  For individual IPs, behaves identically
        to :meth:`is_overridden`.

        This prevents the scheduler from clobbering an individually
        targeted device that belongs to a group.  Without this, playing
        an effect on ``192.0.2.62`` while the scheduler manages
        ``group:porch`` (which includes ``192.0.2.62``) would be
        overwritten on the next scheduler poll.

        Args:
            device_id: Device IP address or group identifier.

        Returns:
            ``True`` if the device or any of its members has an
            active override.
        """
        with self._lock:
            if device_id in self._overrides:
                return True
            # Check group members if this is a group device.
            if _is_group_id(device_id):
                group_name: str = _group_name_from_id(device_id)
                member_ips: list[str] = self._group_config.get(
                    group_name, [],
                )
                return any(ip in self._overrides for ip in member_ips)
            return False

    def get_override_entry(self, ip: str) -> Optional[str]:
        """Get the schedule entry name that was active when override began.

        Args:
            ip: Device IP address.

        Returns:
            The entry name, or ``None``.
        """
        with self._lock:
            return self._overrides.get(ip)

    # -- Nickname management ------------------------------------------------

    def set_nickname(self, ip: str, nickname: str) -> None:
        """Assign a custom display name to a device.

        An empty nickname removes the override, reverting to the
        protocol label.

        Args:
            ip:       Device IP address.
            nickname: The custom name, or empty string to clear.
        """
        with self._lock:
            if nickname:
                self._nicknames[ip] = nickname
            else:
                self._nicknames.pop(ip, None)

    def get_nickname(self, ip: str) -> Optional[str]:
        """Look up a device's custom display name.

        Args:
            ip: Device IP address.

        Returns:
            The nickname, or ``None`` if none is set.
        """
        with self._lock:
            return self._nicknames.get(ip)

    def get_nicknames(self) -> dict[str, str]:
        """Return a copy of the full nickname mapping.

        Returns:
            A dict mapping device IP to nickname.
        """
        with self._lock:
            return dict(self._nicknames)

    # -- Internal helpers ---------------------------------------------------

    def _devices_as_list(self) -> list[dict[str, Any]]:
        """Build a JSON-safe list of emitter info dicts.

        Virtual group emitters include ``is_group: true`` and a
        ``member_ips`` array.  Individual emitters have
        ``is_group: false``.

        Returns:
            A sorted list of emitter metadata dicts.
        """
        result: list[dict[str, Any]] = []
        for dev_id, em in sorted(self._emitters.items()):
            with self._lock:
                ctrl: Optional[Controller] = self._controllers.get(dev_id)
            current_effect: Optional[str] = None
            if ctrl is not None:
                status: dict[str, Any] = ctrl.get_status()
                current_effect = status.get("effect")
            is_group: bool = isinstance(em, VirtualMultizoneEmitter)
            nickname: Optional[str] = self._nicknames.get(dev_id)
            entry: dict[str, Any] = {
                "ip": dev_id,
                "label": em.label,
                "nickname": nickname,
                "product": em.product_name,
                "zones": em.zone_count,
                "is_multizone": em.is_multizone,
                "current_effect": current_effect,
                "overridden": self.is_overridden(dev_id),
                "is_group": is_group,
            }
            # LIFX-specific fields from the transport layer.
            if isinstance(em, LifxEmitter):
                entry["mac"] = em.transport.mac_str
                entry["group"] = em.transport.group
            elif is_group:
                entry["mac"] = ""
                entry["group"] = em.label
                entry["member_ips"] = [
                    m.emitter_id for m in em.get_emitter_list()
                ]
            result.append(entry)
        return result

    def _stop_and_remove(self, ip: str) -> None:
        """Stop controller and close device socket for a given IP.

        Must be called with ``_lock`` held.

        Args:
            ip: Device IP address.
        """
        ctrl: Optional[Controller] = self._controllers.pop(ip, None)
        if ctrl is not None:
            try:
                ctrl.stop(fade_ms=0)
            except Exception:
                pass
        dev: Optional[LifxDevice] = self._devices.pop(ip, None)
        if dev is not None:
            dev.close()
        # Remove from emitter registry (socket already closed via dev).
        self._emitters.pop(ip, None)
        self._overrides.pop(ip, None)

    def shutdown(self) -> None:
        """Stop all controllers and close all device sockets."""
        with self._lock:
            for ip in list(self._devices.keys()):
                self._stop_and_remove(ip)


# ---------------------------------------------------------------------------
# Schedule time parsing (ported from scheduler.py)
# ---------------------------------------------------------------------------

def _parse_time_spec(
    spec: str,
    sun: SunTimes,
    d: date,
    utc_offset: timedelta,
) -> Optional[datetime]:
    """Parse a time specification into a timezone-aware datetime.

    Supports fixed times (``"14:30"``), symbolic solar times
    (``"sunrise"``, ``"sunset"``), and offsets (``"sunset+30m"``).

    Args:
        spec:       The time specification string.
        sun:        Precomputed solar event times for date *d*.
        d:          Calendar date for resolving the time.
        utc_offset: Local UTC offset as a timedelta.

    Returns:
        A timezone-aware datetime, or ``None`` if the symbolic sun event
        does not occur on this date.
    """
    tz: timezone = timezone(utc_offset)

    # Try fixed time first (e.g., "14:30").
    match = _FIXED_TIME_RE.match(spec)
    if match:
        hours: int = int(match.group(1))
        mins: int = int(match.group(2))
        if hours > MAX_HOUR or mins > MAX_MINUTE:
            logging.error("Invalid fixed time: %s", spec)
            return None
        return datetime(d.year, d.month, d.day, hours, mins, 0, tzinfo=tz)

    # Try symbolic time (e.g., "sunset+30m").
    match = _SYMBOLIC_RE.match(spec)
    if not match:
        logging.error("Invalid time specification: %r", spec)
        return None

    symbol: str = match.group(1)
    sign: Optional[str] = match.group(2)
    offset_hours: int = int(match.group(3) or 0)
    offset_mins: int = int(match.group(4) or 0)

    # Resolve symbolic name to a datetime.
    base_time: Optional[datetime] = None
    if symbol == "midnight":
        base_time = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)
    elif symbol == "noon":
        base_time = sun.noon
    elif symbol == "sunrise":
        base_time = sun.sunrise
    elif symbol == "sunset":
        base_time = sun.sunset
    elif symbol == "dawn":
        base_time = sun.dawn
    elif symbol == "dusk":
        base_time = sun.dusk

    if base_time is None:
        logging.warning(
            "Sun event '%s' does not occur on %s (polar day/night?)",
            symbol, d,
        )
        return None

    # Apply offset.
    if sign:
        delta: timedelta = timedelta(hours=offset_hours, minutes=offset_mins)
        if sign == "-":
            delta = -delta
        base_time = base_time + delta

    return base_time


def _entry_runs_on_day(spec: dict[str, Any], d: date) -> bool:
    """Check whether a schedule entry runs on a given calendar date.

    If the ``days`` key is absent or empty, the entry runs every day.
    Otherwise it must be a string of day letters from ``MTWRFSU``
    (Monday through Sunday, academic convention).

    Args:
        spec: Schedule entry dict (may contain a ``days`` key).
        d:    Calendar date to check.

    Returns:
        ``True`` if the entry should run on date *d*.
    """
    days_str: str = spec.get("days", "")
    if not days_str:
        return True
    # Convert the date's weekday (0=Mon .. 6=Sun) to our letter set.
    weekday: int = d.weekday()
    for letter, idx in DAY_LETTER_TO_WEEKDAY.items():
        if idx == weekday:
            return letter in days_str.upper()
    return False


def _validate_days(days_str: str) -> bool:
    """Validate a day-of-week string.

    Args:
        days_str: String of day letters (e.g. ``"MTWRF"``).

    Returns:
        ``True`` if all characters are valid day letters with no repeats.
    """
    upper: str = days_str.upper()
    return (
        all(ch in VALID_DAY_LETTERS for ch in upper)
        and len(upper) == len(set(upper))
    )


def _days_display(days_str: str) -> str:
    """Format a days string for human display.

    Returns smart labels for common patterns, otherwise the
    letter string itself.

    Args:
        days_str: Day letter string (e.g. ``"MTWRF"``).

    Returns:
        A display string like ``"Weekdays"``, ``"Weekends"``, ``"Daily"``,
        or the sorted letter string.
    """
    if not days_str:
        return "Daily"
    upper: str = days_str.upper()
    # Sort into canonical order.
    canonical: str = "".join(ch for ch in VALID_DAY_LETTERS if ch in upper)
    if canonical == VALID_DAY_LETTERS:
        return "Daily"
    if canonical == "MTWRF":
        return "Weekdays"
    if canonical == "SU":
        return "Weekends"
    return canonical


def _resolve_entries(
    specs: list[dict[str, Any]],
    lat: float,
    lon: float,
    d: date,
    utc_offset: timedelta,
    group_filter: Optional[str] = None,
) -> list[tuple[datetime, datetime, dict[str, Any]]]:
    """Resolve schedule entries for a specific date.

    Args:
        specs:        List of raw schedule entry dicts.
        lat:          Observer latitude in degrees.
        lon:          Observer longitude in degrees.
        d:            Calendar date for sun time resolution.
        utc_offset:   Local UTC offset.
        group_filter: If set, only include entries matching this group.

    Returns:
        A list of ``(start_datetime, stop_datetime, spec_dict)`` tuples.
    """
    sun: SunTimes = sun_times(lat, lon, d, utc_offset)
    resolved: list[tuple[datetime, datetime, dict[str, Any]]] = []

    for spec in specs:
        if group_filter is not None and spec.get("group") != group_filter:
            continue

        # Enabled filter: skip disabled entries (default: enabled).
        if not spec.get("enabled", True):
            continue

        # Day-of-week filter: skip entries that don't run on this date.
        if not _entry_runs_on_day(spec, d):
            continue

        start: Optional[datetime] = _parse_time_spec(
            spec["start"], sun, d, utc_offset,
        )
        stop: Optional[datetime] = _parse_time_spec(
            spec["stop"], sun, d, utc_offset,
        )

        if start is None or stop is None:
            logging.warning(
                "Skipping entry '%s': could not resolve times",
                spec.get("name", "?"),
            )
            continue

        # Overnight entries: stop is before start → add a day to stop.
        if stop <= start:
            stop += timedelta(days=1)

        resolved.append((start, stop, spec))

    return resolved


def _find_active_entry(
    specs: list[dict[str, Any]],
    lat: float,
    lon: float,
    now: datetime,
    group_name: str,
) -> Optional[dict[str, Any]]:
    """Find the first schedule entry active for a group at time *now*.

    Args:
        specs:      Raw schedule entry dicts.
        lat:        Observer latitude in degrees.
        lon:        Observer longitude in degrees.
        now:        Current timezone-aware datetime.
        group_name: Only consider entries targeting this group.

    Returns:
        The matching spec dict, or ``None`` if no entry is active.
    """
    utc_offset: timedelta = now.utcoffset()
    today: date = now.date()
    yesterday: date = today - timedelta(days=1)

    today_resolved = _resolve_entries(
        specs, lat, lon, today, utc_offset, group_filter=group_name,
    )
    yesterday_resolved = _resolve_entries(
        specs, lat, lon, yesterday, utc_offset, group_filter=group_name,
    )

    for start, stop, spec in today_resolved:
        if start <= now < stop:
            return spec

    for start, stop, spec in yesterday_resolved:
        if start <= now < stop:
            return spec

    return None


# ---------------------------------------------------------------------------
# Scheduler thread
# ---------------------------------------------------------------------------

class SchedulerThread(threading.Thread):
    """Background thread that manages scheduled effects.

    Replaces scheduler.py's subprocess-based approach with direct
    :class:`Controller` calls through the :class:`DeviceManager`.
    Respects phone overrides: skips devices that have been overridden
    by the REST API, and clears overrides at schedule transitions.
    """

    def __init__(
        self,
        config: dict[str, Any],
        device_manager: DeviceManager,
    ) -> None:
        """Initialize the scheduler thread.

        Args:
            config:         Parsed server configuration dict.
            device_manager: Shared :class:`DeviceManager` instance.
        """
        super().__init__(daemon=True, name="scheduler")
        self._config: dict[str, Any] = config
        self._dm: DeviceManager = device_manager
        self._stop_event: threading.Event = threading.Event()

        # Per-group state: tracks which schedule entry is currently active.
        self._group_entries: dict[str, Optional[str]] = {}

    def run(self) -> None:
        """Scheduler main loop — poll for schedule transitions."""
        lat: float = self._config["location"]["latitude"]
        lon: float = self._config["location"]["longitude"]
        groups: dict[str, list[str]] = _get_groups(self._config)
        specs: list[dict[str, Any]] = self._config.get("schedule", [])

        if not specs:
            logging.info("No schedule entries — scheduler idle")
            return

        # Initialize per-group state.
        for group_name in groups:
            self._group_entries[group_name] = None

        last_logged_date: Optional[date] = None

        total_devices: int = sum(len(ips) for ips in groups.values())
        logging.info(
            "Scheduler started — %d groups, %d devices, %d entries",
            len(groups), total_devices, len(specs),
        )

        while not self._stop_event.is_set():
            now: datetime = datetime.now(timezone.utc).astimezone()
            today: date = now.date()

            # Log sun times once per day.
            if today != last_logged_date:
                utc_offset: timedelta = now.utcoffset()
                sun: SunTimes = sun_times(lat, lon, today, utc_offset)
                _log_sun_times(sun, today)
                last_logged_date = today

            # Per-group scheduling.
            for group_name, ips in groups.items():
                active: Optional[dict[str, Any]] = _find_active_entry(
                    specs, lat, lon, now, group_name,
                )
                active_name: Optional[str] = (
                    active.get("name") if active else None
                )
                prev_name: Optional[str] = self._group_entries.get(
                    group_name,
                )

                # Device ID for this group: virtual device for multi-IP
                # groups, individual IP for single-device groups.
                if len(ips) >= 2:
                    device_id: str = _group_id_from_name(group_name)
                else:
                    device_id = ips[0]

                if active_name != prev_name:
                    # Schedule transition — clear overrides only if
                    # the override was set against the outgoing entry.
                    if self._dm.is_overridden(device_id):
                        override_entry: Optional[str] = (
                            self._dm.get_override_entry(device_id)
                        )
                        if override_entry == prev_name:
                            logging.info(
                                "[%s] Clearing phone override on "
                                "%s (schedule transition from "
                                "'%s' to '%s')",
                                group_name, device_id,
                                prev_name, active_name,
                            )
                            self._dm.clear_override(device_id)
                        else:
                            logging.info(
                                "[%s] Preserving phone override "
                                "on %s (override entry '%s' != "
                                "outgoing '%s')",
                                group_name, device_id,
                                override_entry, prev_name,
                            )

                    # Stop previous effect if not overridden.
                    # Use is_overridden_or_member so that an override
                    # on an individual member device (e.g. 192.0.2.62)
                    # prevents the scheduler from clobbering it when
                    # the group (e.g. group:porch) transitions.
                    if prev_name is not None:
                        if not self._dm.is_overridden_or_member(
                            device_id,
                        ):
                            logging.info(
                                "[%s] Stopping '%s'",
                                group_name, prev_name,
                            )
                            try:
                                self._dm.stop(device_id)
                            except (KeyError, Exception) as exc:
                                logging.warning(
                                    "[%s] Error stopping %s: %s",
                                    group_name, device_id, exc,
                                )

                    # Start new effect if not overridden.
                    if active is not None:
                        if not self._dm.is_overridden_or_member(
                            device_id,
                        ):
                            effect: str = active["effect"]
                            params: dict[str, Any] = active.get(
                                "params", {},
                            )
                            # Pass bindings from schedule entry if present.
                            sched_bindings: Optional[dict] = active.get(
                                "bindings",
                            )
                            sched_bus: Optional[SignalBus] = None
                            mm: Optional[MediaManager] = (
                                GlowUpRequestHandler.media_manager
                            )
                            if sched_bindings and mm is not None:
                                sched_bus = mm.bus
                            logging.info(
                                "[%s] Starting '%s' (%s)",
                                group_name, active_name, effect,
                            )
                            try:
                                self._dm.play(
                                    device_id, effect, params,
                                    bindings=sched_bindings,
                                    signal_bus=sched_bus,
                                )
                            except (KeyError, ValueError, Exception) as exc:
                                logging.warning(
                                    "[%s] Error starting %s on %s: %s",
                                    group_name, effect, device_id, exc,
                                )
                    else:
                        logging.info(
                            "[%s] No active entry — idle", group_name,
                        )

                    self._group_entries[group_name] = active_name

                elif active is not None:
                    # Same entry still active — ensure running
                    # (restart if crashed).  Check members too so
                    # an individual device override isn't clobbered.
                    if self._dm.is_overridden_or_member(device_id):
                        continue
                    ctrl: Optional[Controller] = (
                        self._dm.get_or_create_controller(device_id)
                    )
                    if ctrl is not None:
                        status: dict[str, Any] = ctrl.get_status()
                        if not status.get("running"):
                            effect_name: str = active["effect"]
                            params_restart: dict = active.get(
                                "params", {},
                            )
                            restart_bindings: Optional[dict] = (
                                active.get("bindings")
                            )
                            restart_bus: Optional[SignalBus] = None
                            rmm: Optional[MediaManager] = (
                                GlowUpRequestHandler.media_manager
                            )
                            if restart_bindings and rmm is not None:
                                restart_bus = rmm.bus
                            logging.info(
                                "[%s] Restarting '%s' on %s",
                                group_name, active_name, device_id,
                            )
                            try:
                                self._dm.play(
                                    device_id, effect_name,
                                    params_restart,
                                    bindings=restart_bindings,
                                    signal_bus=restart_bus,
                                )
                            except Exception as exc:
                                logging.warning(
                                    "[%s] Restart error on %s: %s",
                                    group_name, device_id, exc,
                                )

            # Sleep until next poll, checking for stop every second.
            self._stop_event.wait(SCHEDULER_POLL_SECONDS)

        logging.info("Scheduler stopped")

    def stop(self) -> None:
        """Signal the scheduler to stop."""
        self._stop_event.set()


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class GlowUpRequestHandler(http.server.BaseHTTPRequestHandler):
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
    orchestrator: Optional[Any] = None
    keepalive: Optional[BulbKeepAlive] = None
    registry: Optional[DeviceRegistry] = None

    # Active /api/command/identify pulses: {ip: stop_event}.
    # Populated by _handle_post_command_identify; cleared when pulse ends.
    # DELETE /api/command/identify/{ip} sets the event to cancel early.
    _command_identifies: dict[str, threading.Event] = {}
    _command_identifies_lock: threading.Lock = threading.Lock()

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
        # Group identifiers pass through unchanged.
        if _is_group_id(identifier):
            return identifier if len(identifier) > len(GROUP_PREFIX) else None

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
                         "max-age=31536000; includeSubDomains")
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
                         "GET, POST, OPTIONS")
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

            # Dispatch.
            handler_fn: Callable = getattr(self, route.handler)
            handler_fn(*handler_args)
            return

        # No route matched.
        self._send_json(404, {"error": "Not found"})

    # -- GET handlers -------------------------------------------------------

    def _handle_get_status(self) -> None:
        """GET /api/status — server readiness and version.

        Returns a status object indicating whether initial device
        loading has completed.  Clients can poll this endpoint on
        connect and show a "loading devices" message until
        ``ready`` becomes ``true``.
        """
        ready: bool = self.device_manager.ready
        status: str = "ready" if ready else "loading"
        self._send_json(200, {
            "status": status,
            "ready": ready,
            "version": __version__,
        })

    def _handle_get_devices(self) -> None:
        """GET /api/devices — list all configured devices."""
        devices: list[dict[str, Any]] = self.device_manager.devices_as_list()
        self._send_json(200, {"devices": devices})

    def _handle_get_effects(self) -> None:
        """GET /api/effects — list effects with param metadata."""
        effects: dict[str, Any] = self.device_manager.list_effects()
        self._send_json(200, {"effects": effects})

    def _handle_get_groups(self) -> None:
        """GET /api/groups — device groups from config.

        Returns all device groups defined in the server config,
        excluding comment keys (those starting with ``_``).

        Response::

            {
                "groups": {
                    "porch": ["192.0.2.25", "192.0.2.26"],
                    "office": ["192.0.2.30"]
                }
            }
        """
        groups: dict[str, list[str]] = _get_groups(self.config)
        self._send_json(200, {"groups": groups})

    def _handle_get_schedule(self) -> None:
        """GET /api/schedule — schedule entries with resolved times.

        Returns the schedule entries from the config, each enriched
        with resolved start/stop times for today and an ``active``
        flag indicating whether the entry is running right now.
        """
        config: dict[str, Any] = self.config
        specs: list[dict[str, Any]] = config.get("schedule", [])
        if not specs:
            self._send_json(200, {"entries": []})
            return

        lat: float = config.get("location", {}).get("latitude", 0.0)
        lon: float = config.get("location", {}).get("longitude", 0.0)

        now: datetime = datetime.now(timezone.utc).astimezone()
        utc_offset: timedelta = now.utcoffset()
        today: date = now.date()

        # Resolve times for today to determine active status and
        # display times.  We resolve without group filter to get all.
        sun: SunTimes = sun_times(lat, lon, today, utc_offset)

        entries: list[dict[str, Any]] = []
        for i, spec in enumerate(specs):
            enabled: bool = spec.get("enabled", True)
            days_raw: str = spec.get("days", "")

            # Resolve start/stop for display.
            start_resolved: Optional[datetime] = _parse_time_spec(
                spec["start"], sun, today, utc_offset,
            )
            stop_resolved: Optional[datetime] = _parse_time_spec(
                spec["stop"], sun, today, utc_offset,
            )

            start_str: Optional[str] = None
            stop_str: Optional[str] = None
            active: bool = False

            if start_resolved is not None and stop_resolved is not None:
                # Handle overnight entries.
                if stop_resolved <= start_resolved:
                    stop_resolved += timedelta(days=1)
                start_str = start_resolved.strftime("%H:%M")
                stop_str = stop_resolved.strftime("%H:%M")
                if stop_resolved.date() != start_resolved.date():
                    stop_str = stop_resolved.strftime("%H:%M (+1)")

                # Active if enabled, runs today, and we're in the window.
                if (enabled
                        and _entry_runs_on_day(spec, today)
                        and start_resolved <= now < stop_resolved):
                    active = True

            entry: dict[str, Any] = {
                "index": i,
                "name": spec.get("name", f"entry_{i}"),
                "group": spec.get("group", ""),
                "effect": spec.get("effect", ""),
                "start": spec.get("start", ""),
                "stop": spec.get("stop", ""),
                "start_resolved": start_str,
                "stop_resolved": stop_str,
                "days": days_raw,
                "days_display": _days_display(days_raw),
                "enabled": enabled,
                "active": active,
            }
            entries.append(entry)

        self._send_json(200, {"entries": entries})

    def _handle_get_device_status(self, ip: str) -> None:
        """GET /api/devices/{ip}/status — device effect status."""
        try:
            status: dict[str, Any] = self.device_manager.get_status(ip)
            self._send_json(200, status)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})

    def _handle_get_device_colors(self, ip: str) -> None:
        """GET /api/devices/{ip}/colors — zone color snapshot."""
        try:
            colors = self.device_manager.get_colors(ip)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})
            return

        if colors is None:
            self._send_json(503, {"error": "Could not query device colors"})
            return

        self._send_json(200, {"zones": colors})

    def _handle_get_device_colors_stream(self, ip: str) -> None:
        """GET /api/devices/{ip}/colors/stream — SSE color stream at 4 Hz.

        Creates a temporary :class:`LifxDevice` for read-only zone color
        queries to avoid socket contention with the engine.  The stream
        runs until the client disconnects.
        """
        em: Optional[Emitter] = self.device_manager.get_emitter(ip)
        if em is None:
            self._send_json(404, {"error": "Device not found"})
            return

        self._send_sse_headers()

        # Send an initial padding comment to flush Cloudflare's response
        # buffer.  Cloudflare Tunnel buffers small chunks; a ~4KB initial
        # payload forces the proxy to begin streaming immediately.
        padding: str = ": " + " " * 4096 + "\n\n"
        try:
            self.wfile.write(padding.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        stream_start: float = time_mod.time()
        try:
            while True:
                # Enforce maximum stream lifetime to prevent resource
                # exhaustion from abandoned connections.
                if time_mod.time() - stream_start > SSE_TIMEOUT_SECONDS:
                    break

                # Read colors from the engine's in-memory frame buffer.
                # Zero UDP overhead, zero socket contention.  When no
                # effect is running the frame is None and we skip the
                # event — the app shows "Connecting..." which is accurate.
                ctrl: Optional[Controller] = self.device_manager.get_controller(ip)
                if ctrl is not None:
                    colors = ctrl.get_last_frame()
                    if colors is not None:
                        payload: str = json.dumps({
                            "zones": [
                                {"h": h, "s": s, "b": b, "k": k}
                                for h, s, b, k in colors
                            ],
                        })
                        self.wfile.write(
                            f"data: {payload}\n\n".encode("utf-8"),
                        )
                        self.wfile.flush()

                time_mod.sleep(SSE_POLL_INTERVAL)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client disconnected — clean exit.
            pass

    # -- POST handlers ------------------------------------------------------

    def _handle_post_play(self, ip: str) -> None:
        """POST /api/devices/{ip}/play — start an effect.

        Request body::

            {
                "effect": "cylon",
                "params": {"speed": 2.0, "hue": 120},
                "bindings": {
                    "brightness": {
                        "signal": "backyard:audio:bass",
                        "scale": [20, 100]
                    }
                }
            }

        The optional ``bindings`` field maps parameter names to media
        signals.  Each binding specifies a signal name and optional
        ``scale`` (output range) and ``reduce`` (for array signals:
        ``"max"``, ``"mean"``, or ``"sum"``).
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        effect_name: Optional[str] = body.get("effect")
        if not effect_name or not isinstance(effect_name, str):
            self._send_json(400, {"error": "Missing or invalid 'effect'"})
            return

        params: dict[str, Any] = body.get("params", {})
        if not isinstance(params, dict):
            self._send_json(400, {"error": "'params' must be an object"})
            return

        # Extract optional signal bindings for media-reactive effects.
        bindings: Optional[dict[str, Any]] = body.get("bindings")
        if bindings is not None and not isinstance(bindings, dict):
            self._send_json(400, {"error": "'bindings' must be an object"})
            return

        # Resolve signal bus — pass if we have bindings OR a media manager
        # (MediaEffects need the bus even without explicit bindings).
        signal_bus: Optional[SignalBus] = None
        if self.media_manager is not None:
            signal_bus = self.media_manager.bus

        try:
            # Track override so the scheduler knows to back off.
            active_entry: Optional[str] = self._get_active_entry_for_ip(ip)
            self.device_manager.mark_override(ip, active_entry)

            status: dict[str, Any] = self.device_manager.play(
                ip, effect_name, params,
                bindings=bindings, signal_bus=signal_bus,
            )
            logging.info(
                "API: playing '%s' on %s (params: %s, bindings: %s)",
                effect_name, ip, params,
                list(bindings.keys()) if bindings else None,
            )
            self._send_json(200, status)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})
        except ValueError:
            self._send_json(400, {"error": "Invalid effect or parameters"})

    def _handle_post_stop(self, ip: str) -> None:
        """POST /api/devices/{ip}/stop — stop the current effect."""
        try:
            # Set override if not already set so the scheduler doesn't
            # immediately restart the effect on its next poll cycle.
            if not self.device_manager.is_overridden(ip):
                active_entry: Optional[str] = self._get_active_entry_for_ip(ip)
                self.device_manager.mark_override(ip, active_entry)
            status: dict[str, Any] = self.device_manager.stop(ip)
            logging.info("API: stopped effect on %s", ip)
            self._send_json(200, status)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})

    def _handle_post_resume(self, ip: str) -> None:
        """POST /api/devices/{ip}/resume — clear phone override.

        Clears the manual override for this device so the scheduler
        can resume control on its next poll cycle.
        """
        try:
            if ip not in self.device_manager._devices:
                raise KeyError(ip)
            was_overridden: bool = self.device_manager.is_overridden(ip)
            self.device_manager.clear_override(ip)
            logging.info(
                "API: resume schedule on %s (was overridden: %s)",
                ip, was_overridden,
            )
            status: dict[str, Any] = self.device_manager.get_status(ip)
            self._send_json(200, status)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})

    def _handle_post_reset(self, ip: str) -> None:
        """POST /api/devices/{ip}/reset — deep-reset device hardware.

        Stops all software effects, disables any firmware-level multizone
        effect, blanks all zones with acknowledged writes, and powers off.
        This clears stale zone colors stored in the device's non-volatile
        memory.
        """
        try:
            result: dict[str, Any] = self.device_manager.reset(ip)
            # Clear any phone override since the device is now clean.
            self.device_manager.clear_override(ip)
            self._send_json(200, result)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})

    def _handle_post_power(self, ip: str) -> None:
        """POST /api/devices/{ip}/power — turn device on/off.

        Request body::

            {"on": true}
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        on: Any = body.get("on")
        if not isinstance(on, bool):
            self._send_json(400, {"error": "'on' must be a boolean"})
            return

        try:
            # Powering off from the phone should pause the scheduler on
            # this device, otherwise it will restart the effect immediately.
            if not on and not self.device_manager.is_overridden(ip):
                active_entry: Optional[str] = self._get_active_entry_for_ip(ip)
                self.device_manager.mark_override(ip, active_entry)

            result: dict[str, Any] = self.device_manager.set_power(ip, on)
            logging.info("API: power %s on %s", "on" if on else "off", ip)
            self._send_json(200, result)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})

    def _handle_post_identify(self, ip: str) -> None:
        """POST /api/devices/{ip}/identify — pulse brightness to locate device.

        Starts a background thread that pulses the device's brightness
        in a sine wave for :data:`IDENTIFY_DURATION_SECONDS`.  The HTTP
        response returns immediately.

        Sets a phone override for the duration of the pulse so the
        scheduler doesn't restart an effect while identify is running.
        The override is cleared automatically when the pulse finishes.
        """
        try:
            # Override so the scheduler doesn't fight with the pulse.
            active_entry: Optional[str] = self._get_active_entry_for_ip(ip)
            self.device_manager.mark_override(ip, active_entry)
            self.device_manager.identify(ip, on_complete=lambda: (
                self.device_manager.clear_override(ip)
            ))
            logging.info("API: identifying %s (override set)", ip)
            self._send_json(200, {"ip": ip, "identifying": True})
        except KeyError:
            self._send_json(404, {"error": "Device not found"})

    def _handle_post_nickname(self, ip: str) -> None:
        """POST /api/devices/{ip}/nickname — set a custom display name.

        Request body::

            {"nickname": "Porch Lights"}

        An empty string or ``null`` clears the nickname.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        nickname: Any = body.get("nickname")
        if nickname is None:
            nickname = ""
        if not isinstance(nickname, str):
            self._send_json(400, {"error": "'nickname' must be a string"})
            return

        nickname = nickname.strip()

        self.device_manager.set_nickname(ip, nickname)

        # Persist to config file.
        self._save_nicknames()

        logging.info(
            "API: nickname for %s %s",
            ip, f"set to '{nickname}'" if nickname else "cleared",
        )
        self._send_json(200, {"ip": ip, "nickname": nickname or None})

    def _save_nicknames(self) -> None:
        """Persist current nicknames to the config file."""
        nicknames: dict[str, str] = self.device_manager.get_nicknames()
        self._save_config_field("nicknames", nicknames or {})

    def _handle_post_effect_defaults(self, effect_name: str) -> None:
        """POST /api/effects/{name}/defaults — save tuned params as defaults.

        Request body::

            {"params": {"speed": 8.0, "decay": 2.0, ...}}

        Persists the provided parameter values as the new defaults for
        the named effect.  These defaults are used by the scheduler and
        reported by GET /api/effects.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        params: Any = body.get("params")
        if not isinstance(params, dict):
            self._send_json(400, {"error": "'params' must be an object"})
            return

        try:
            self.device_manager.save_effect_defaults(effect_name, params)
        except ValueError as exc:
            self._send_json(404, {"error": str(exc)})
            return

        self._send_json(200, {"ok": True})

    def _handle_post_schedule_enabled(self, index: int) -> None:
        """POST /api/schedule/{index}/enabled — enable or disable an entry.

        Request body::

            {"enabled": false}

        Persists the change to the config file so it survives restarts.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        enabled: Any = body.get("enabled")
        if not isinstance(enabled, bool):
            self._send_json(400, {"error": "'enabled' must be a boolean"})
            return

        specs: list[dict[str, Any]] = self.config.get("schedule", [])
        if index < 0 or index >= len(specs):
            self._send_json(404, {"error": "Schedule entry not found"})
            return

        specs[index]["enabled"] = enabled
        self._save_config_field("schedule", specs)

        name: str = specs[index].get("name", f"entry_{index}")
        logging.info(
            "API: schedule entry '%s' %s",
            name, "enabled" if enabled else "disabled",
        )
        self._send_json(200, {
            "index": index,
            "name": name,
            "enabled": enabled,
        })

    # -- Media handlers -----------------------------------------------------

    def _handle_get_media_sources(self) -> None:
        """GET /api/media/sources — list media sources with status.

        Returns source names, types, and alive status.  Never exposes
        RTSP URLs or credentials.
        """
        mm: Optional[MediaManager] = self.media_manager
        if mm is None:
            self._send_json(200, {"sources": []})
            return
        self._send_json(200, {"sources": mm.get_status()})

    def _handle_get_media_signals(self) -> None:
        """GET /api/media/signals — list available signal names.

        Returns signal metadata for the iOS signal picker UI.
        """
        mm: Optional[MediaManager] = self.media_manager
        if mm is None:
            self._send_json(200, {"signals": []})
            return
        self._send_json(200, {"signals": mm.bus.list_signals()})

    def _handle_get_fleet(self) -> None:
        """GET /api/fleet — distributed fleet status.

        Returns the orchestrator's fleet inventory: online nodes,
        capabilities, assignments, and allocated UDP ports.
        """
        orch: Optional[Any] = self.orchestrator
        if orch is None:
            self._send_json(200, {
                "enabled": False,
                "nodes": [],
                "node_count": 0,
                "message": "Distributed compute not configured",
            })
            return
        status: dict[str, Any] = orch.get_fleet_status()
        status["enabled"] = True
        self._send_json(200, status)

    def _handle_post_assign(self) -> None:
        """POST /api/assign — issue a work assignment to a compute node.

        Request body::

            {
                "node_id": "judy",
                "operator": "AudioExtractor",
                "config": {"source_name": "conway", "bands": 8},
                "inputs": [
                    {"signal_name": "conway:audio:pcm_raw",
                     "transport": "udp", "udp_port": 9420}
                ],
                "outputs": [
                    {"signal_name": "judy:audio:bands",
                     "transport": "mqtt"}
                ]
            }
        """
        orch: Optional[Any] = self.orchestrator
        if orch is None:
            self._send_json(503, {
                "error": "Distributed compute not configured",
            })
            return

        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        node_id: str = body.get("node_id", "")
        operator_name: str = body.get("operator", "")
        if not node_id or not operator_name:
            self._send_json(400, {
                "error": "Missing required fields: node_id, operator",
            })
            return

        # Import SignalBinding and WorkAssignment from distributed module.
        try:
            from distributed.orchestrator import SignalBinding, WorkAssignment
        except ImportError:
            self._send_json(503, {"error": "Distributed module not available"})
            return

        # Build input/output bindings.
        inputs: list[SignalBinding] = [
            SignalBinding.from_dict(b) for b in body.get("inputs", [])
        ]
        outputs: list[SignalBinding] = [
            SignalBinding.from_dict(b) for b in body.get("outputs", [])
        ]

        # Generate assignment ID.
        import time as _time
        assignment_id: str = (
            f"{node_id}-{operator_name.lower()}-{int(_time.time())}"
        )

        assignment: WorkAssignment = WorkAssignment(
            assignment_id=assignment_id,
            operator_name=operator_name,
            operator_config=body.get("config", {}),
            inputs=inputs,
            outputs=outputs,
            action="start",
        )

        success: bool = orch.assign_work(node_id, assignment)
        if success:
            logging.info(
                "API: assigned '%s' to node '%s' (id: %s)",
                operator_name, node_id, assignment_id,
            )
            self._send_json(200, {
                "assigned": True,
                "assignment_id": assignment_id,
                "node_id": node_id,
                "operator": operator_name,
            })
        else:
            self._send_json(409, {
                "error": f"Cannot assign to node '{node_id}'",
                "assigned": False,
            })

    def _handle_post_cancel_assignment(self, node_id: str,
                                       assignment_id: str) -> None:
        """POST /api/assign/{node_id}/cancel/{assignment_id}."""
        orch: Optional[Any] = self.orchestrator
        if orch is None:
            self._send_json(503, {
                "error": "Distributed compute not configured",
            })
            return
        success: bool = orch.cancel_assignment(node_id, assignment_id)
        if success:
            self._send_json(200, {
                "cancelled": True,
                "assignment_id": assignment_id,
            })
        else:
            self._send_json(404, {
                "error": f"Assignment '{assignment_id}' not found",
            })

    def _handle_post_media_source_start(self, name: str) -> None:
        """POST /api/media/sources/{name}/start — manually start a source."""
        mm: Optional[MediaManager] = self.media_manager
        if mm is None:
            self._send_json(503, {
                "error": "Media pipeline not configured",
            })
            return
        if mm.start_source(name):
            logging.info("API: started media source '%s'", name)
            self._send_json(200, {"source": name, "started": True})
        else:
            self._send_json(404, {
                "error": f"Unknown media source: {name}",
            })

    def _handle_post_media_source_stop(self, name: str) -> None:
        """POST /api/media/sources/{name}/stop — manually stop a source."""
        mm: Optional[MediaManager] = self.media_manager
        if mm is None:
            self._send_json(503, {
                "error": "Media pipeline not configured",
            })
            return
        try:
            mm.stop_source(name)
            logging.info("API: stopped media source '%s'", name)
            self._send_json(200, {"source": name, "stopped": True})
        except KeyError:
            self._send_json(404, {
                "error": f"Unknown media source: {name}",
            })

    def _handle_post_signal_ingest(self) -> None:
        """POST /api/media/signals/ingest — write signals from an external source.

        Accepts a JSON body with a ``source`` name and a ``signals`` dict
        mapping signal suffixes to values (scalar float or float array).
        Each signal is written to the bus as ``{source}:audio:{name}``.

        This endpoint enables any device (iPhone, ESP32, browser) to act
        as a media source by posting computed signal values directly to
        the signal bus, bypassing the ffmpeg/extractor pipeline.

        Request body::

            {
                "source": "iphone",
                "signals": {
                    "bands": [0.1, 0.3, 0.8, 0.2, 0.0, 0.1, 0.5, 0.9],
                    "rms": 0.42,
                    "beat": 1.0,
                    "bass": 0.2,
                    "mid": 0.5,
                    "treble": 0.7,
                    "energy": 0.45,
                    "centroid": 0.6
                }
            }
        """
        mm: Optional[MediaManager] = self.media_manager
        if mm is None:
            self._send_json(503, {
                "error": "Media pipeline not configured",
            })
            return

        body: dict = self._read_json_body()
        if body is None:
            return

        source: str = body.get("source", "")
        if not source:
            self._send_json(400, {"error": "'source' is required"})
            return

        signals: dict = body.get("signals", {})
        if not isinstance(signals, dict):
            self._send_json(400, {"error": "'signals' must be an object"})
            return

        bus = mm.bus
        written: int = 0
        for name, value in signals.items():
            signal_name: str = f"{source}:audio:{name}"
            if isinstance(value, (int, float)):
                bus.write(signal_name, float(value))
                written += 1
            elif isinstance(value, list):
                bus.write(signal_name, [float(v) for v in value])
                written += 1

        self._send_json(200, {"written": written})

    # -- Diagnostics endpoints ----------------------------------------------

    def _handle_get_diag_now_playing(self) -> None:
        """GET /api/diagnostics/now_playing — effects currently playing.

        Returns open effect_history records (no ``stopped_at``).
        Falls back to an empty list if diagnostics is unavailable.
        """
        diag = self.device_manager._diag
        if diag is None or not _HAS_DIAGNOSTICS:
            self._send_json(200, [])
            return
        try:
            rows: list[dict[str, Any]] = diag.query_now_playing()
            self._send_json(200, rows)
        except Exception as exc:
            logging.warning("Diagnostics query failed: %s", exc)
            self._send_json(200, [])

    def _handle_get_diag_history(self) -> None:
        """GET /api/diagnostics/history — recent effect events.

        Returns the most recent 50 effect_history records (both
        open and closed).  Falls back to an empty list if diagnostics
        is unavailable.
        """
        diag = self.device_manager._diag
        if diag is None or not _HAS_DIAGNOSTICS:
            self._send_json(200, [])
            return
        try:
            rows: list[dict[str, Any]] = diag.query_history(limit=50)
            self._send_json(200, rows)
        except Exception as exc:
            logging.warning("Diagnostics query failed: %s", exc)
            self._send_json(200, [])

    def _handle_get_discovered_bulbs(self) -> None:
        """GET /api/discovered_bulbs — bulbs found via ARP keepalive.

        Returns the current in-memory set of discovered LIFX bulbs.
        Each entry includes IP and MAC address.  If the keepalive
        daemon is not running, returns an empty list.
        """
        daemon: Optional[BulbKeepAlive] = self.keepalive
        if daemon is None:
            self._send_json(200, {"discovered_bulbs": []})
            return
        bulbs: list[dict[str, str]] = [
            {"ip": ip, "mac": mac}
            for ip, mac in sorted(daemon.known_bulbs.items())
        ]
        self._send_json(200, {"discovered_bulbs": bulbs})

    def _handle_delete_command_identify(self, ip: str) -> None:
        """DELETE /api/command/identify/{ip} — cancel a running identify pulse.

        Sets the stop event for any pulse currently running on *ip*.
        The pulse thread will power the device off and exit on its next
        loop iteration (within :data:`IDENTIFY_FRAME_INTERVAL` seconds).

        Returns 200 if a pulse was cancelled, 404 if none was running.
        """
        with GlowUpRequestHandler._command_identifies_lock:
            event: Optional[threading.Event] = (
                GlowUpRequestHandler._command_identifies.get(ip)
            )
        if event is None:
            self._send_json(404, {
                "error": f"No active identify pulse for {ip}"
            })
            return
        event.set()
        logging.info("API: command/identify — cancelled pulse on %s", ip)
        self._send_json(200, {"ip": ip, "cancelled": True})

    def _handle_get_command_identify_cancel_all(self) -> None:
        """GET /api/command/identify/cancel-all — cancel all active identify pulses.

        Emergency/cleanup endpoint: sets the stop event for every running
        identify pulse on every IP.  Returns the count of cancelled pulses.
        """
        with GlowUpRequestHandler._command_identifies_lock:
            ips_to_cancel: list[str] = list(
                GlowUpRequestHandler._command_identifies.keys()
            )
            for ip in ips_to_cancel:
                event: Optional[threading.Event] = (
                    GlowUpRequestHandler._command_identifies.get(ip)
                )
                if event is not None:
                    event.set()
                    logging.info(
                        "API: command/identify/cancel-all — cancelled pulse on %s",
                        ip,
                    )
        self._send_json(200, {"cancelled": len(ips_to_cancel)})

    def _handle_post_server_power_off_all(self) -> None:
        """POST /api/server/power-off-all — emergency bulk power-off.

        Powers off every device configured in ``server.json`` immediately
        with a 0ms transition.  Returns the count of devices sent the
        power-off command.

        This is a fire-and-forget emergency endpoint — failures on
        individual devices do not stop the power-off of others.
        """
        configured_ips: list[str] = list(self.device_manager._devices.keys())
        off_count: int = 0
        for ip in configured_ips:
            try:
                dev: LifxDevice = LifxDevice(ip)
                dev.set_power(False, duration_ms=0)
                off_count += 1
                logging.info("API: server/power-off-all — powered off %s", ip)
                dev.close()
            except Exception as exc:
                logging.warning(
                    "API: server/power-off-all — power-off failed for %s: %s",
                    ip, exc,
                )
        self._send_json(200, {"devices_off": off_count})

    # -- Registry handlers ---------------------------------------------------

    def _handle_get_registry(self) -> None:
        """GET /api/registry — list all registered devices with live status.

        Returns the full device registry merged with live ARP data so
        each entry includes the current IP and online/offline status.
        """
        reg: Optional[DeviceRegistry] = self.registry
        if reg is None:
            self._send_json(200, {"devices": {}})
            return

        devices: dict[str, dict] = reg.all_devices()
        daemon: Optional[BulbKeepAlive] = self.keepalive
        mac_to_ip: dict[str, str] = {}
        if daemon is not None:
            mac_to_ip = daemon.known_bulbs_by_mac

        result: list[dict] = []
        for mac, entry in sorted(devices.items()):
            ip: str = mac_to_ip.get(mac, "")
            result.append({
                "mac": mac,
                "label": entry.get("label", ""),
                "notes": entry.get("notes", ""),
                "ip": ip,
                "online": bool(ip),
            })

        self._send_json(200, {"devices": result, "count": len(result)})

    def _handle_post_registry_device(self) -> None:
        """POST /api/registry/device — add or update a device.

        Accepts ``{"mac": "...", "label": "...", "notes": "..."}``
        or ``{"ip": "...", "label": "...", "notes": "..."}`` where
        the IP is resolved to a MAC via the ARP table.

        After registering, optionally writes the label to the bulb
        firmware via SetLabel if the device is online.
        """
        body: Optional[dict] = self._read_json_body()
        if body is None:
            return

        reg: Optional[DeviceRegistry] = self.registry
        if reg is None:
            self._send_json(500, {"error": "Registry not loaded"})
            return

        label: str = body.get("label", "").strip()
        notes: str = body.get("notes", "").strip()
        mac: str = body.get("mac", "").strip().lower()
        ip_arg: str = body.get("ip", "").strip()

        # Resolve IP to MAC if no MAC provided.
        if not mac and ip_arg:
            daemon: Optional[BulbKeepAlive] = self.keepalive
            if daemon is not None:
                bulbs: dict[str, str] = daemon.known_bulbs
                mac = bulbs.get(ip_arg, "")

        if not mac:
            self._send_json(400, {"error": "No MAC address — provide mac or a reachable ip"})
            return

        if not label:
            self._send_json(400, {"error": "Label is required"})
            return

        try:
            reg.add_device(mac, label, notes)
            reg.save()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        # Resolve MAC to IP for firmware label write.
        result_ip: str = ip_arg
        if not result_ip:
            daemon = self.keepalive
            if daemon is not None:
                result_ip = daemon.ip_for_mac(mac) or ""

        # Write label to bulb firmware if online.
        firmware_written: bool = False
        if result_ip:
            try:
                from transport import LifxDevice, SOCKET_TIMEOUT
                tmp_dev: LifxDevice = LifxDevice(result_ip)
                tmp_dev.sock.settimeout(SOCKET_TIMEOUT)
                firmware_written = tmp_dev.set_label(label)
                tmp_dev.close()
                # Update the cached device's label so discover reflects
                # the new name without requiring a server restart.
                if firmware_written:
                    cached_dev: Optional[LifxDevice] = (
                        self.device_manager.get_device(result_ip)
                    )
                    if cached_dev is not None:
                        cached_dev.label = label
            except Exception as exc:
                logging.warning(
                    "Failed to write label to %s (%s): %s",
                    result_ip, mac, exc,
                )

        self._send_json(200, {
            "mac": mac,
            "label": label,
            "ip": result_ip,
            "firmware_written": firmware_written,
        })

    def _handle_delete_registry_device(self, mac: str) -> None:
        """DELETE /api/registry/device/{mac} — remove a device.

        Args:
            mac: URL-decoded MAC address or label.
        """
        reg: Optional[DeviceRegistry] = self.registry
        if reg is None:
            self._send_json(500, {"error": "Registry not loaded"})
            return

        if reg.remove_device(mac):
            reg.save()
            self._send_json(200, {"removed": mac})
        else:
            self._send_json(404, {"error": f"Device not found: {mac}"})

    def _handle_post_registry_push_labels(self) -> None:
        """POST /api/registry/push-labels — write all labels to bulb firmware.

        Iterates the registry, resolves each MAC to IP via ARP, and sends
        SetLabel to each online device.
        """
        reg: Optional[DeviceRegistry] = self.registry
        if reg is None:
            self._send_json(500, {"error": "Registry not loaded"})
            return

        daemon: Optional[BulbKeepAlive] = self.keepalive
        mac_to_ip: dict[str, str] = {}
        if daemon is not None:
            mac_to_ip = daemon.known_bulbs_by_mac

        devices: dict[str, dict] = reg.all_devices()
        results: list[dict] = []

        for mac, entry in sorted(devices.items()):
            label: str = entry.get("label", "")
            if not label:
                continue
            ip: str = mac_to_ip.get(mac, "")
            if not ip:
                results.append({"mac": mac, "label": label, "status": "offline"})
                continue

            try:
                from transport import LifxDevice, SOCKET_TIMEOUT
                dev: LifxDevice = LifxDevice(ip)
                dev.sock.settimeout(SOCKET_TIMEOUT)
                ok: bool = dev.set_label(label)
                dev.close()
                results.append({
                    "mac": mac, "label": label, "ip": ip,
                    "status": "ok" if ok else "timeout",
                })
            except Exception as exc:
                results.append({
                    "mac": mac, "label": label, "ip": ip,
                    "status": f"error: {exc}",
                })

        self._send_json(200, {"results": results})

    def _handle_post_registry_push_label(self) -> None:
        """POST /api/registry/push-label — write one label to one bulb.

        Accepts ``{"mac": "...", "label": "..."}`` or
        ``{"ip": "...", "label": "..."}``.
        """
        body: Optional[dict] = self._read_json_body()
        if body is None:
            return

        label: str = body.get("label", "").strip()
        mac: str = body.get("mac", "").strip().lower()
        ip_arg: str = body.get("ip", "").strip()

        if not label:
            self._send_json(400, {"error": "Label is required"})
            return

        # Resolve to IP.
        target_ip: str = ip_arg
        if not target_ip and mac:
            daemon: Optional[BulbKeepAlive] = self.keepalive
            if daemon is not None:
                target_ip = daemon.ip_for_mac(mac) or ""

        if not target_ip:
            self._send_json(400, {"error": "Device offline or no IP/MAC provided"})
            return

        try:
            from transport import LifxDevice, SOCKET_TIMEOUT
            dev: LifxDevice = LifxDevice(target_ip)
            dev.sock.settimeout(SOCKET_TIMEOUT)
            ok: bool = dev.set_label(label)
            dev.close()
            self._send_json(200, {
                "ip": target_ip, "label": label, "firmware_written": ok,
            })
        except Exception as exc:
            self._send_json(500, {"error": f"SetLabel failed: {exc}"})

    # -- Command handlers --------------------------------------------------

    def _handle_get_command_discover(self) -> None:
        """GET /api/command/discover[?ip=X] — return discovered LIFX devices.

        Returns IPs and MACs of all bulbs currently detected by the keepalive
        daemon via ARP scan. The keepalive daemon confirms liveness by
        unicast ping every 15 seconds, so all returned devices are known to
        be on the network.

        Query parameters:
            ip: Optional specific device IP to filter. If omitted, all
                bulbs currently known to the keepalive daemon are returned.

        Response::

            {
                "devices": [
                    {
                        "ip":  "10.0.0.41",
                        "mac": "d0:73:d5:69:70:db"
                    },
                    ...
                ]
            }

        Results are returned immediately (no UDP query overhead) since the
        keepalive daemon maintains a live ARP-based device list.
        """
        qs: dict = parse_qs(urlparse(self.path).query)
        target_ip: Optional[str] = qs.get("ip", [None])[0]

        if target_ip is not None:
            if not _validate_device_id(target_ip):
                self._send_json(400, {"error": "Cannot resolve device identifier"})
                return
            # For specific IP, always return it (even if not in ARP cache yet).
            devices: list[dict] = [{"ip": target_ip, "mac": ""}]
        else:
            daemon: Optional[BulbKeepAlive] = self.keepalive
            if daemon is None:
                self._send_json(200, {"devices": []})
                return
            # Return all currently-known devices from ARP cache (no fresh queries).
            try:
                bulbs_snapshot: dict[str, str] = daemon.known_bulbs
                devices = [
                    {"ip": ip, "mac": mac}
                    for ip, mac in bulbs_snapshot.items()
                ]
            except Exception as exc:
                logging.warning(
                    "command/discover: failed to access keepalive daemon: %s",
                    exc,
                )
                devices = []

        # Enrich each device with metadata from the device manager and
        # registry label from the device registry.
        dm: DeviceManager = self.device_manager
        reg: Optional[DeviceRegistry] = self.registry
        for entry in devices:
            ip: str = entry.get("ip", "")
            mac: str = entry.get("mac", "")
            # Pull cached device info (label, product, group, zones).
            dev: Optional[LifxDevice] = dm.get_device(ip) if ip else None
            if dev is not None:
                entry["label"] = dev.label or ""
                entry["product"] = dev.product_name or ""
                entry["group"] = dev.group or ""
                entry["zones"] = dev.zone_count or 0
                if not mac:
                    entry["mac"] = dev.mac_str
                    mac = dev.mac_str
            # Pull registry label (user-assigned name) if available.
            if reg is not None and mac:
                entry["registry_label"] = reg.mac_to_label(mac) or ""

        logging.info(
            "API: command/discover — returning %d device(s) from ARP cache",
            len(devices),
        )
        self._send_json(200, {"devices": devices})

    def _handle_post_command_identify(self) -> None:
        """POST /api/command/identify — pulse any device by IP to locate it.

        Unlike ``POST /api/devices/{ip}/identify``, this endpoint works for
        any device reachable from the server — it does not require the device
        to be configured in ``server.json``.  Intended for use from client
        machines that cannot reach bulbs directly due to mesh router filtering.

        Request body::

            {
                "ip":       "10.0.0.41",
                "duration": 10.0        (optional, default IDENTIFY_DURATION_SECONDS)
            }

        Response::

            {
                "ip":          "10.0.0.41",
                "identifying": true,
                "duration":    10.0,
                "device": {
                    "ip":      "10.0.0.41",
                    "mac":     "d0:73:d5:69:70:db",
                    "label":   "Bedroom Neon",
                    "product": "LIFX Neon",
                    "zones":   100,
                    "group":   "bedroom"
                }
            }

        The pulse runs asynchronously — the response returns immediately
        while the bulb flashes.  The device is powered off when the
        duration expires.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        raw_ident: Any = body.get("ip") or body.get("device")
        if not raw_ident or not isinstance(raw_ident, str):
            self._send_json(400, {
                "error": "Missing or invalid 'ip' or 'device' field"
            })
            return
        ip: Optional[str] = self._resolve_device_id(raw_ident)
        if ip is None or not _validate_device_id(ip):
            self._send_json(400, {
                "error": f"Cannot resolve device '{raw_ident}'"
            })
            return

        duration: float = float(body.get("duration", IDENTIFY_DURATION_SECONDS))
        if not (0 < duration <= COMMAND_IDENTIFY_MAX_DURATION):
            self._send_json(400, {
                "error": (
                    f"'duration' must be between 0 and "
                    f"{COMMAND_IDENTIFY_MAX_DURATION} seconds"
                )
            })
            return

        # Device is confirmed alive by keepalive daemon ARP ping.
        # Return immediately without blocking on UDP query.
        device_info: dict = {
            "ip":      ip,
            "mac":     "",
            "label":   "",
            "product": "",
            "zones":   0,
            "group":   "",
        }
        dev: LifxDevice = LifxDevice(ip)

        stop_event: threading.Event = threading.Event()

        # Cancel any pulse already running on this IP before starting a new one.
        with GlowUpRequestHandler._command_identifies_lock:
            existing: Optional[threading.Event] = (
                GlowUpRequestHandler._command_identifies.get(ip)
            )
            if existing is not None:
                existing.set()
                logging.info(
                    "API: command/identify — cancelled existing pulse on %s", ip,
                )
            GlowUpRequestHandler._command_identifies[ip] = stop_event

        def _pulse() -> None:
            """Sine-wave brightness pulse loop running in a daemon thread."""
            try:
                dev.set_power(True, duration_ms=0)
                start: float = time_mod.monotonic()
                while (
                    not stop_event.is_set()
                    and time_mod.monotonic() - start < duration
                ):
                    elapsed: float = time_mod.monotonic() - start
                    phase: float = (
                        math.sin(
                            2.0 * math.pi * elapsed / IDENTIFY_CYCLE_SECONDS
                        ) + 1.0
                    ) / 2.0
                    bri_frac: float = (
                        IDENTIFY_MIN_BRI + phase * (1.0 - IDENTIFY_MIN_BRI)
                    )
                    bri: int = int(bri_frac * HSBK_MAX)
                    if dev.is_multizone:
                        dev.set_zones(
                            [(0, 0, bri, KELVIN_DEFAULT)] * (dev.zone_count or 1),
                            duration_ms=0,
                        )
                    else:
                        dev.set_color(0, 0, bri, KELVIN_DEFAULT, duration_ms=0)
                    stop_event.wait(timeout=IDENTIFY_FRAME_INTERVAL)
                dev.set_power(False, duration_ms=DEFAULT_FADE_MS)
            except Exception as exc:
                logging.warning(
                    "command/identify pulse failed for %s: %s", ip, exc,
                )
            finally:
                dev.close()
                with GlowUpRequestHandler._command_identifies_lock:
                    # Only remove our own entry — don't clobber a newer pulse.
                    if GlowUpRequestHandler._command_identifies.get(ip) is stop_event:
                        del GlowUpRequestHandler._command_identifies[ip]

        thread: threading.Thread = threading.Thread(
            target=_pulse, daemon=True, name=f"cmd-identify-{ip}",
        )
        thread.start()
        logging.info(
            "API: command/identify — pulsing %s for %.1fs", ip, duration,
        )
        self._send_json(200, {
            "ip":          ip,
            "identifying": True,
            "duration":    duration,
            "device":      device_info,
        })

    def _handle_get_dashboard(self) -> None:
        """GET /dashboard — serve the static HTML dashboard page."""
        dashboard_path: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "static", "dashboard.html",
        )
        try:
            with open(dashboard_path, "r") as f:
                html: str = f.read()
            body: bytes = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._send_json(404, {"error": "Dashboard page not found"})

    def _save_config_field(self, key: str, value: Any) -> None:
        """Persist a single config field to the config file.

        Reads the config JSON, updates the given key, and writes back.

        Args:
            key:   Top-level config key to update.
            value: The new value.
        """
        config_path: Optional[str] = self.config_path
        if config_path is None:
            return
        try:
            with open(config_path, "r") as f:
                config: dict[str, Any] = json.load(f)
            config[key] = value
            with open(config_path, "w") as f:
                json.dump(config, f, indent=4)
                f.write("\n")
        except Exception as exc:
            logging.warning("Failed to save config field '%s': %s", key, exc)

    # -- Helpers ------------------------------------------------------------

    def _get_active_entry_for_ip(self, ip: str) -> Optional[str]:
        """Find the active schedule entry name for a device or group.

        For group IDs (``group:name``), extracts the group name and
        looks up directly.  For individual IPs, searches all groups
        for one containing this IP.

        Args:
            ip: Device IP address or group identifier.

        Returns:
            The active entry name, or ``None``.
        """
        config: dict[str, Any] = self.config
        if not config:
            return None

        groups: dict[str, list[str]] = _get_groups(config)
        specs: list[dict[str, Any]] = config.get("schedule", [])
        if not specs:
            return None

        now: datetime = datetime.now(timezone.utc).astimezone()

        # For group identifiers, look up by group name directly.
        if _is_group_id(ip):
            group_name: str = _group_name_from_id(ip)
            if group_name in groups:
                active: Optional[dict[str, Any]] = _find_active_entry(
                    specs,
                    config["location"]["latitude"],
                    config["location"]["longitude"],
                    now,
                    group_name,
                )
                if active is not None:
                    return active.get("name")
            return None

        # For individual IPs, search groups.
        for group_name, ips in groups.items():
            if ip in ips:
                active = _find_active_entry(
                    specs,
                    config["location"]["latitude"],
                    config["location"]["longitude"],
                    now,
                    group_name,
                )
                if active is not None:
                    return active.get("name")
                return None

        return None


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
    import argparse

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


def main() -> None:
    """Entry point for the GlowUp REST API server."""
    import argparse

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
    # MediaManager reference — set by _background_startup if configured.
    media_mgr: Optional[MediaManager] = None
    # Orchestrator reference — set by _background_startup if configured.
    orch: Optional[Any] = None
    keepalive: Optional[BulbKeepAlive] = None

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
        nonlocal mqtt_bridge, media_mgr, orch, keepalive

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

            # -- Step 5: Populate DeviceManager and load ------------------
            dm._device_ips = device_ips
            dm._group_config = resolved_groups
            devices: list[dict[str, Any]] = dm.load_devices()
            logging.info("Loaded %d device(s)", len(devices))

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
        if media_mgr is not None:
            media_mgr.shutdown()
        if orch is not None:
            orch.stop()
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
