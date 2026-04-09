"""Device manager — centralized management of LIFX devices, groups, and grids.

Handles device discovery (via ARP), emitter construction, controller
lifecycle, effect playback, power state tracking, and override management.

Extracted from server.py for modularity.  The server imports and
instantiates this class; all state mutation is thread-safe.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import logging
import math
import os
import threading
import time as time_mod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

from effects import (
    get_registry, create_effect, get_effect_names, MediaEffect,
    HSBK, HSBK_MAX, KELVIN_DEFAULT,
)
from emitters import Emitter
from emitters.lifx import LifxEmitter
from emitters.virtual import VirtualMultizoneEmitter
from emitters.virtual_grid import VirtualGridEmitter
from engine import Controller
from media import SignalBus
from media.source import AudioStreamServer
from transport import LifxDevice, SendMode, SINGLE_ZONE_COUNT
from infrastructure.adapter_proxy import KeepaliveProxy
from device_registry import DeviceRegistry

from server_constants import (
    DEFAULT_FADE_MS, BRIGHTNESS_PERCENTAGE_SCALE,
    DEVICE_WAKEUP_DELAY_SECONDS, EFFECT_DEFAULTS_FILENAME,
    GROUP_PREFIX, GRID_PREFIX,
    IDENTIFY_DURATION_SECONDS, IDENTIFY_CYCLE_SECONDS,
    IDENTIFY_FRAME_INTERVAL, IDENTIFY_MIN_BRI,
)
from server_utils import (
    validate_ip as _validate_ip,
    is_group_id as _is_group_id,
    group_name_from_id as _group_name_from_id,
    group_id_from_name as _group_id_from_name,
    is_grid_id as _is_grid_id,
    grid_name_from_id as _grid_name_from_id,
    grid_id_from_name as _grid_id_from_name,
)

# Optional diagnostics subsystem (requires psycopg2 + PostgreSQL).
try:
    from diagnostics import DiagnosticsLogger
    _HAS_DIAGNOSTICS: bool = True
except ImportError:
    DiagnosticsLogger = None  # type: ignore[assignment,misc]
    _HAS_DIAGNOSTICS = False

# SQLite state store.
try:
    from state_store import StateStore
    _HAS_STATE_STORE: bool = True
except ImportError:
    StateStore = None  # type: ignore[assignment,misc]
    _HAS_STATE_STORE = False

logger: logging.Logger = logging.getLogger("glowup.device_manager")


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
                 groups: Optional[dict[str, list[str]]] = None,
                 grids: Optional[dict[str, Any]] = None) -> None:
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
            grids:      Grid name → grid definition dict from config.
                        2D spatial arrangements of matrix/strip devices.
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
        # Grid config: grid name → grid definition dict.
        self._grid_config: dict[str, Any] = grids or {}
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
        # Play source tracking: device ID → client name that started
        # the current effect (e.g. "Conway", "Perry's iPhone", "scheduler").
        self._play_sources: dict[str, str] = {}
        # Power state tracking: device ID → True/False.
        # Updated by power queries and the power endpoint.
        # Devices not in this dict have unknown power — default False
        # (safer than assuming on for unreachable devices).
        self._power_states: dict[str, bool] = {}
        # Dynamic music source tracking: device ID → source name.
        # Used to clean up on-demand DirectorySource instances on stop.
        self._music_sources: dict[str, str] = {}
        # TCP audio stream servers: device ID → AudioStreamServer.
        self._audio_streams: dict[str, AudioStreamServer] = {}
        # Optional device registry for label resolution on unreachable
        # devices.  Set by server.py after construction.
        self._registry: Optional[DeviceRegistry] = None
        # Optional keepalive daemon for MAC→IP lookups.
        self._keepalive: Optional[KeepaliveProxy] = None
        # Readiness flag: False until initial load completes.
        self._ready: bool = False
        # Optional diagnostics logger (None if psycopg2 or DB unavailable).
        self._diag: Optional[Any] = None
        if _HAS_DIAGNOSTICS:
            self._diag = DiagnosticsLogger.from_env()
            if self._diag is not None:
                self._diag.close_stale_records()
        # Optional state store — shared with scheduler.py via SQLite WAL.
        # Records which brain (server vs scheduler) owns each bulb and why,
        # so the dashboard is accurate even when the scheduler is running.
        self._state: Optional[Any] = None
        if _HAS_STATE_STORE:
            db_path: str = os.path.join(config_dir, "state.db") \
                if config_dir else "state.db"
            self._state = StateStore.open(db_path)

    def query_power_state(self, ip: str) -> None:
        """Query a single device's power state and cache the result.

        Sends a UDP light-state query to the device.  On success,
        updates ``_power_states``.  On failure (timeout, unreachable),
        the existing cached state is left unchanged.

        Args:
            ip: Device IP address.
        """
        with self._lock:
            dev: Optional[LifxDevice] = self._devices.get(ip)
        if dev is None:
            return
        try:
            state = dev.query_light_state()
            if state is not None:
                self._power_states[ip] = state[4] > 0
        except Exception:
            pass  # Leave cached state unchanged.

    def query_all_power_states(self) -> None:
        """Query power state for every loaded device.

        Called at startup and periodically by the keepalive daemon.
        """
        with self._lock:
            ips: list[str] = list(self._devices.keys())
        for ip in ips:
            try:
                from server import TRACING_ENABLED, _thread_heartbeats
                if TRACING_ENABLED:
                    _thread_heartbeats[threading.current_thread().name] = (
                        f"power_query:{ip}", time.monotonic(),
                    )
            except ImportError:
                pass
            self.query_power_state(ip)
        logging.info(
            "Power state queried: %d on, %d off",
            sum(1 for v in self._power_states.values() if v),
            sum(1 for v in self._power_states.values() if not v),
        )

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

        def _probe_device(ip: str) -> tuple[str, Optional[LifxDevice]]:
            """Query a single device.  Returns (ip, device) or (ip, None).

            Some bulbs accept fire-and-forget commands but never respond
            to query packets.  This appears to be Orbi mesh router
            filtering — bulbs on certain satellites don't forward unicast
            UDP replies back to the Pi.  The bulbs work fine for effects
            and power commands (fire-and-forget), they just can't answer
            queries.

            When query_all fails, we keep the device with safe defaults
            (single zone, unknown product) so it participates in groups
            and accepts commands.  The config says the device exists;
            we trust the config.
            """
            try:
                dev: LifxDevice = LifxDevice(ip)
                dev.query_all()
                if dev.product is not None:
                    return (ip, dev)
                # Query failed but the device is in the config.
                # Keep it with defaults — it can still accept commands.
                dev.product_name = "LIFX (query-silent)"
                dev.zone_count = SINGLE_ZONE_COUNT
                logging.warning(
                    "Device %s did not answer queries — loaded with "
                    "defaults (1 zone, commands only)",
                    ip,
                )
                return (ip, dev)
            except Exception as exc:
                logging.warning("Device %s unreachable: %s", ip, exc)
            return (ip, None)

        # Probe all devices in parallel — unreachable devices time out
        # concurrently instead of serializing ~18s each.
        with ThreadPoolExecutor(
            max_workers=len(self._device_ips) or 1,
        ) as pool:
            futures = {
                pool.submit(_probe_device, ip): ip
                for ip in self._device_ips
            }
            for future in as_completed(futures):
                ip, dev = future.result()
                if dev is not None:
                    new_map[ip] = dev
                    logging.info(
                        "  loaded %s — %s (%s) [%s zones]",
                        dev.label or "?", dev.product_name or "?",
                        dev.ip, dev.zone_count or "?",
                    )

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
            # Single-member groups still get a virtual emitter so they
            # can be referenced by group:Name in operators and the API.
            for group_name, ips in self._group_config.items():
                if not ips:
                    continue
                member_emitters: list[Emitter] = [
                    new_emitters[ip] for ip in ips
                    if ip in new_emitters
                ]
                if not member_emitters:
                    logging.warning(
                        "Group '%s' has no reachable devices "
                        "(%d configured) — skipping virtual emitter",
                        group_name, len(ips),
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

            # Build VirtualGridEmitters for 2D device grids.
            grid_config: dict[str, Any] = self._grid_config
            for grid_name, gdef in grid_config.items():
                if grid_name.startswith("_"):
                    continue
                try:
                    grid_em: Optional[VirtualGridEmitter] = (
                        self._build_grid_emitter(
                            grid_name, gdef, new_emitters,
                        )
                    )
                except Exception as exc:
                    logging.warning(
                        "Grid '%s' construction failed: %s",
                        grid_name, exc,
                    )
                    continue
                if grid_em is not None:
                    grid_id: str = _grid_id_from_name(grid_name)
                    old_gctrl: Optional[Controller] = (
                        self._controllers.get(grid_id)
                    )
                    if old_gctrl is not None:
                        old_gctrl.stop(fade_ms=0)
                        del self._controllers[grid_id]
                    new_emitters[grid_id] = grid_em

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

    def _rebuild_group_emitter_locked(self, group_name: str) -> None:
        """Rebuild (or remove) the virtual emitter for a single group.

        This is the narrow hot-path for dashboard-driven group CRUD —
        it avoids re-probing every device the way :meth:`load_devices`
        does.  Call it after mutating ``_group_config`` to bring
        ``_emitters`` and ``_controllers`` back into sync without a
        full rediscover.

        Semantics:

        - If *group_name* is no longer present in ``_group_config``
          (delete case), any existing emitter, controller, and
          override tracking for the corresponding ``group:<name>``
          identifier are dropped.
        - If *group_name* is present but its membership list is empty
          or none of its members are currently cached in
          ``_emitters``, the same drop happens and a warning is
          logged — the group is effectively dead until the user runs
          Rediscover to introspect the new members.
        - Otherwise, any lingering controller for the old group
          definition is stopped (so in-flight effects on the previous
          membership do not keep running), a fresh
          :class:`VirtualMultizoneEmitter` is constructed from the
          currently-cached member emitters, and the result is stored
          in ``_emitters`` under the ``group:<name>`` key.

        Caller must hold ``self._lock``.  Matches the locking
        discipline used by :meth:`load_devices`.

        Args:
            group_name: The human-readable group name — the same key
                used in ``_group_config``.  Not the ``group:<name>``
                emitter identifier.
        """
        group_id: str = _group_id_from_name(group_name)

        # Stop any lingering controller for this group before
        # touching its emitter — a running effect on the old
        # definition would otherwise keep firing at stale member
        # emitters.  Happens on both rebuild and delete paths.
        old_ctrl: Optional[Controller] = self._controllers.pop(
            group_id, None,
        )
        if old_ctrl is not None:
            old_ctrl.stop(fade_ms=0)

        ips: list[str] = self._group_config.get(group_name, [])
        if not ips:
            # Delete case, or empty-membership update — drop the
            # emitter and override tracking entirely.
            self._emitters.pop(group_id, None)
            self._overrides.pop(group_id, None)
            logger.info(
                "Group '%s' — emitter removed (no members)", group_name,
            )
            return

        # Assemble member emitters from the currently-cached entries.
        # IPs not in ``_emitters`` are skipped — for freshly-discovered
        # devices that have not yet been introspected via Rediscover,
        # the user must Rediscover first for them to participate.
        member_emitters: list[Emitter] = [
            self._emitters[ip] for ip in ips if ip in self._emitters
        ]
        if not member_emitters:
            logger.warning(
                "Group '%s' has no reachable devices (%d configured) "
                "— emitter not rebuilt, run Rediscover to introspect "
                "new members", group_name, len(ips),
            )
            self._emitters.pop(group_id, None)
            self._overrides.pop(group_id, None)
            return

        vem: VirtualMultizoneEmitter = VirtualMultizoneEmitter(
            member_emitters, name=group_name, owns_emitters=False,
        )
        self._emitters[group_id] = vem
        logger.info(
            "Group '%s' rebuilt — %d emitter(s), %d zone(s)",
            group_name, len(member_emitters), vem.zone_count,
        )

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

    def _build_grid_emitter(
        self,
        grid_name: str,
        gdef: dict[str, Any],
        emitters: dict[str, Emitter],
    ) -> Optional[VirtualGridEmitter]:
        """Build a VirtualGridEmitter from a grid definition.

        Resolves cell labels to emitters, discovers member geometry,
        enforces homogeneity, and constructs the grid emitter.

        Args:
            grid_name: Display name for the grid.
            gdef:      Grid definition dict (dimensions, member, cells).
            emitters:  Current emitter map (IP → Emitter).

        Returns:
            A :class:`VirtualGridEmitter`, or ``None`` if construction
            fails (insufficient members, geometry mismatch, etc.).
        """
        dims: Any = gdef.get("dimensions")
        if not isinstance(dims, list) or len(dims) != 2:
            logging.warning(
                "Grid '%s': 'dimensions' must be [cols, rows]", grid_name,
            )
            return None

        cell_cols: int = int(dims[0])
        cell_rows: int = int(dims[1])

        # Parse cell assignments: "col,row" → label/IP.
        raw_cells: dict[str, str] = gdef.get("cells", {})
        if not raw_cells:
            logging.warning("Grid '%s': no cells defined", grid_name)
            return None

        # Resolve cell labels to emitters.
        cell_emitters: dict[tuple[int, int], Emitter] = {}
        for key, ident in raw_cells.items():
            parts: list[str] = key.split(",")
            if len(parts) != 2:
                logging.warning(
                    "Grid '%s': bad cell key '%s'", grid_name, key,
                )
                continue
            col: int = int(parts[0].strip())
            row: int = int(parts[1].strip())

            # Resolve identifier (label, MAC, or IP) to an emitter.
            resolved_ip: Optional[str] = None
            if _validate_ip(ident):
                resolved_ip = ident
            elif self._registry and self._keepalive:
                resolved_ip = self._registry.resolve_to_ip(
                    ident, self._keepalive,
                )
            if resolved_ip is None or resolved_ip not in emitters:
                logging.warning(
                    "Grid '%s': cannot resolve cell %d,%d ('%s')",
                    grid_name, col, row, ident,
                )
                continue
            cell_emitters[(col, row)] = emitters[resolved_ip]

        if not cell_emitters:
            logging.warning(
                "Grid '%s': no cells could be resolved", grid_name,
            )
            return None

        # Discover member geometry from the first resolved emitter
        # and enforce homogeneity.
        member_w: int = 1
        member_h: int = 1
        first_em: Emitter = next(iter(cell_emitters.values()))

        if hasattr(first_em, 'is_matrix') and first_em.is_matrix:
            member_w = getattr(first_em, 'matrix_width', 1) or 1
            member_h = getattr(first_em, 'matrix_height', 1) or 1
        elif first_em.is_multizone:
            # Strip-as-scanline: width = zone count, height = 1.
            member_w = first_em.zone_count or 1
            member_h = 1
        # Else single-zone: 1×1.

        # If member spec is in the config, prefer it (allows simulator
        # compatibility and pre-configuration before hardware arrives).
        member_cfg: dict[str, Any] = gdef.get("member", {})
        mat: Any = member_cfg.get("matrix")
        if mat and isinstance(mat, list) and len(mat) == 2:
            member_w = int(mat[0])
            member_h = int(mat[1])

        # Verify all members have matching geometry.
        for (c, r), em in cell_emitters.items():
            em_w: int = 1
            em_h: int = 1
            if hasattr(em, 'is_matrix') and em.is_matrix:
                em_w = getattr(em, 'matrix_width', 1) or 1
                em_h = getattr(em, 'matrix_height', 1) or 1
            elif em.is_multizone:
                em_w = em.zone_count or 1
                em_h = 1
            if em_w != member_w or em_h != member_h:
                logging.warning(
                    "Grid '%s': cell %d,%d geometry %d×%d "
                    "mismatches expected %d×%d",
                    grid_name, c, r, em_w, em_h, member_w, member_h,
                )
                return None

        grid_em: VirtualGridEmitter = VirtualGridEmitter(
            cell_emitters=cell_emitters,
            cell_cols=cell_cols,
            cell_rows=cell_rows,
            member_w=member_w,
            member_h=member_h,
            name=grid_name,
            owns_emitters=False,
        )
        return grid_em

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

    def reintrospect(self, ip: str) -> dict[str, Any]:
        """Re-query a device's zone count and rebuild affected groups.

        Sends a fresh zone-count query to the physical device, then
        rebuilds the zone map of every VirtualMultizoneEmitter that
        contains it.  Does not interrupt running effects — the next
        render frame will use the new geometry.

        Args:
            ip: Device IP address.

        Returns:
            A summary dict with old/new zone counts and affected groups.

        Raises:
            KeyError: If the IP is not a loaded device.
        """
        with self._lock:
            dev: Optional[LifxDevice] = self._devices.get(ip)
        if dev is None:
            raise KeyError(f"Unknown device: {ip}")

        old_zones: Optional[int] = dev.zone_count

        # Re-query the device outside the lock (UDP I/O).
        if dev.is_matrix:
            dev.query_device_chain()
        elif dev.is_multizone:
            dev.query_zone_count()
        # Non-multizone devices are always 1 zone — nothing to re-query.

        new_zones: Optional[int] = dev.zone_count
        logging.info(
            "Reintrospect %s (%s): %s → %s zones",
            dev.label or "?", ip, old_zones, new_zones,
        )

        # Rebuild zone maps for any group containing this device.
        rebuilt_groups: list[str] = []
        with self._lock:
            for eid, em in self._emitters.items():
                if not isinstance(em, VirtualMultizoneEmitter):
                    continue
                # Check if this device's emitter is a member.
                member_ips: list[str] = [
                    m.emitter_id for m in em.get_emitter_list()
                ]
                if ip in member_ips:
                    old_total: int = em.zone_count or 0
                    new_total: int = em.rebuild_zone_map()
                    rebuilt_groups.append(eid)
                    logging.info(
                        "  group '%s' zone map rebuilt: %d → %d zones",
                        em.label, old_total, new_total,
                    )

        return {
            "ip": ip,
            "label": dev.label,
            "old_zones": old_zones,
            "new_zones": new_zones,
            "rebuilt_groups": rebuilt_groups,
        }

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
        source: Optional[str] = None,
        entry: Optional[str] = None,
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
            source:      Optional client name that started the effect
                         (e.g. "Conway", "Perry's iPhone").
            entry:       Optional schedule entry name (set by the
                         internal scheduler so the state store records
                         which entry is driving this device).

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
                self._power_states[ip] = True
                if isinstance(em, (VirtualMultizoneEmitter, VirtualGridEmitter)):
                    for member in em.get_emitter_list():
                        self._power_states[member.emitter_id] = True
            except Exception as exc:
                logging.warning("power_on failed for %s before play: %s", ip, exc)
        # Auto-inject matrix width/height for 2D effects on matrix
        # or grid emitters.  Without this the effect defaults (7×5)
        # would produce the wrong pixel count for larger grids.
        if em is not None:
            mw: Optional[int] = getattr(em, 'matrix_width', None)
            mh: Optional[int] = getattr(em, 'matrix_height', None)
            if mw and mh:
                if "width" not in params:
                    params["width"] = mw
                if "height" not in params:
                    params["height"] = mh

        # Close the previous effect's diagnostics record before starting
        # a new one, so replaced effects get a proper stop_reason.
        if self._diag is not None:
            self._diag.log_stop(ip, stop_reason="replaced")

        # Transient effects (on, off) use execute() for a one-shot
        # committed state write, NOT the render loop.  The render loop
        # writes transient HSBK frames that don't update the bulb's
        # persistent state — so the app reads stale brightness and the
        # schedule's brightness=30 shows as 100 in the UI.
        from effects import create_effect, Effect
        effect_instance: Effect = create_effect(effect_name, **params)
        if getattr(effect_instance, "is_transient", False) and em is not None:
            effect_instance.execute(em)
            # Mark as "running" so the scheduler doesn't restart it.
            # Transient effects don't use the Engine render loop, so
            # we set engine.running directly and store the effect ref
            # so get_status() reports correctly.
            ctrl._current_effect_name = effect_name
            ctrl._last_effect_name = effect_name
            ctrl._last_params = dict(params)
            ctrl.engine.effect = effect_instance
            ctrl.engine.running = True
        else:
            ctrl.play(effect_name, bindings=bindings,
                      signal_bus=signal_bus, **params)
        # Track which client started this effect.
        if source:
            self._play_sources[ip] = source
        else:
            self._play_sources.pop(ip, None)
        if self._diag is not None:
            em_info: dict[str, Any] = em.get_info() if em else {}
            self._diag.log_play(
                device_ip=ip,
                device_label=em_info.get("label"),
                effect_name=effect_name,
                params=params,
                started_by=source or "api",
            )
        result: dict[str, Any] = ctrl.get_status()
        result["overridden"] = self.is_overridden(ip)
        result["source"] = source
        if self._state is not None:
            src: str = source or "server"
            # Groups: write a row per member IP so the dashboard shows
            # individual bulbs, not a virtual "group:Name" pseudo-IP.
            if em is not None and isinstance(em, VirtualMultizoneEmitter):
                for member in em.get_emitter_list():
                    m_label: Optional[str] = (
                        member.get_info().get("label")
                    )
                    self._state.upsert(
                        ip=member.emitter_id, label=m_label,
                        power=True, effect=effect_name,
                        source=src, entry=entry,
                    )
            else:
                label: Optional[str] = (
                    em.get_info().get("label") if em else None
                )
                self._state.upsert(
                    ip=ip, label=label,
                    power=True, effect=effect_name,
                    source=src, entry=entry,
                )
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
        self._power_states[ip] = False
        em_stop: Optional[Emitter] = self.get_emitter(ip)
        if em_stop is not None and isinstance(em_stop, VirtualMultizoneEmitter):
            for member in em_stop.get_emitter_list():
                self._power_states[member.emitter_id] = False
        self._play_sources.pop(ip, None)
        if self._diag is not None:
            self._diag.log_stop(ip, stop_reason="user")
        if self._state is not None:
            # Groups: clear each member IP individually (mirrors play).
            if em_stop is not None and isinstance(
                em_stop, VirtualMultizoneEmitter,
            ):
                for member in em_stop.get_emitter_list():
                    m_label: Optional[str] = (
                        member.get_info().get("label")
                    )
                    self._state.upsert(
                        ip=member.emitter_id, label=m_label,
                        power=False, effect=None, source="server",
                    )
            else:
                stop_label: Optional[str] = (
                    em_stop.get_info().get("label")
                    if em_stop else None
                )
                self._state.upsert(
                    ip=ip, label=stop_label,
                    power=False, effect=None, source="server",
                )
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
            # Snapshot emitter under lock so the fallback path
            # doesn't race with load_devices() replacing _emitters.
            em_snapshot: Emitter = self._emitters[ip]

        overridden: bool = self.is_overridden(ip)

        if ctrl is not None:
            result: dict[str, Any] = ctrl.get_status()
            result["overridden"] = overridden
            return result

        # No controller yet — return idle status from the emitter.
        return {
            "running": False,
            "effect": None,
            "params": {},
            "fps": 0,
            "overridden": overridden,
            "devices": [em_snapshot.get_info()],
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
        if not on and em.zone_count:
            if hasattr(em, 'is_matrix') and em.is_matrix:
                blank: list[HSBK] = [
                    (0, 0, 0, KELVIN_DEFAULT)
                ] * em.zone_count
                em.send_tile_zones(blank, duration_ms=0)
            elif em.is_multizone:
                blank = [
                    (0, 0, 0, KELVIN_DEFAULT)
                ] * em.zone_count
                em.send_zones(blank, duration_ms=0,
                             mode=SendMode.GUARANTEED)

        if on:
            em.power_on(duration_ms=DEFAULT_FADE_MS)
            # Restore full-brightness warm white after power-on.
            # The blank-before-off logic writes black to all zones so
            # the firmware doesn't flash stale effect colors on next
            # power-on.  But that means powering on restores zero
            # brightness — the bulb is electrically on but dark.
            # Write a clean neutral state so the lights are visible.
            if em.zone_count:
                warm_white: list[HSBK] = [
                    (0, 0, HSBK_MAX, KELVIN_DEFAULT)
                ] * em.zone_count
                if hasattr(em, 'is_matrix') and em.is_matrix:
                    em.send_tile_zones(warm_white, duration_ms=DEFAULT_FADE_MS)
                elif em.is_multizone:
                    em.send_zones(warm_white, duration_ms=DEFAULT_FADE_MS,
                                 mode=SendMode.GUARANTEED)
                elif hasattr(em, '_device') and em._device is not None:
                    em._device.set_color(
                        0, 0, HSBK_MAX, KELVIN_DEFAULT,
                        duration_ms=DEFAULT_FADE_MS,
                    )
        else:
            em.power_off(duration_ms=DEFAULT_FADE_MS)

        # Update the power state cache so the app and dashboard reflect
        # the new state immediately — without waiting for the next
        # keepalive UDP query cycle.
        self._power_states[ip] = on
        if isinstance(em, VirtualMultizoneEmitter):
            for member in em.get_emitter_list():
                self._power_states[member.emitter_id] = on

        return {"ip": ip, "power": "on" if on else "off"}

    def set_brightness(self, ip: str, brightness: int) -> dict[str, Any]:
        """Set brightness on a device or group without changing colour.

        Brightness is specified as a percentage (0–100) and mapped to
        the LIFX HSBK brightness range (0–65535).  For groups the
        command fans out to every member.

        Args:
            ip: Device IP address or group identifier.
            brightness: Brightness percentage (0–100).

        Returns:
            A dict confirming the action.

        Raises:
            KeyError: If the device IP is not configured.
        """
        em: Optional[Emitter] = self.get_emitter(ip)
        if em is None:
            raise KeyError(f"Unknown device: {ip}")

        # Map 0–100 → 0–65535.
        bri: int = int(HSBK_MAX * max(0, min(BRIGHTNESS_PERCENTAGE_SCALE, brightness)) / BRIGHTNESS_PERCENTAGE_SCALE)

        if isinstance(em, VirtualMultizoneEmitter):
            for member in em.get_emitter_list():
                if hasattr(member, '_device') and member._device is not None:
                    member._device.set_color(
                        0, 0, bri, KELVIN_DEFAULT,
                        duration_ms=DEFAULT_FADE_MS,
                    )
        elif hasattr(em, '_device') and em._device is not None:
            em._device.set_color(
                0, 0, bri, KELVIN_DEFAULT,
                duration_ms=DEFAULT_FADE_MS,
            )

        return {"ip": ip, "brightness": brightness}

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

            # 2. Clear any firmware-level effect (multizone or tile).
            if dev.is_matrix:
                try:
                    dev.clear_tile_effect()
                    logging.info("Reset %s: tile effect cleared", dev.ip)
                except Exception as exc:
                    logging.warning(
                        "Reset %s: clear_tile_effect failed: %s",
                        dev.ip, exc,
                    )
            elif dev.is_multizone:
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
            time_mod.sleep(DEVICE_WAKEUP_DELAY_SECONDS)

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

                    if hasattr(em, 'is_matrix') and em.is_matrix:
                        color: HSBK = (0, 0, bri, KELVIN_DEFAULT)
                        colors: list[HSBK] = [color] * (em.zone_count or 1)
                        em.send_tile_zones(colors, duration_ms=0)
                    elif em.is_multizone:
                        color = (0, 0, bri, KELVIN_DEFAULT)
                        colors = [color] * (em.zone_count or 1)
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
                "affinity": sorted(cls.affinity),
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

        Group status is derived from member state:

        - *power*: ``True`` if any member is powered on.
        - *current_effect*: from the group's own controller.

        Individual devices participating in a group effect are
        annotated with ``group_effect`` and ``group_name`` so the
        dashboard can show an IN GROUP badge.

        Returns:
            A sorted list of emitter metadata dicts.
        """
        # First pass: build group membership and active-effect maps.
        # group_id → (effect_name, group_label, [member_ips])
        # Snapshot emitters under lock to avoid RuntimeError from
        # concurrent dict modification during iteration.
        with self._lock:
            emitter_snapshot: list[tuple[str, Any]] = list(
                self._emitters.items()
            )
        active_groups: dict[str, tuple[str, str, list[str]]] = {}
        for dev_id, em in emitter_snapshot:
            if not isinstance(em, VirtualMultizoneEmitter):
                continue
            with self._lock:
                ctrl: Optional[Controller] = self._controllers.get(dev_id)
            effect_name: Optional[str] = None
            if ctrl is not None:
                status: dict[str, Any] = ctrl.get_status()
                if status.get("running"):
                    effect_name = status.get("effect")
            if effect_name:
                member_ips: list[str] = [
                    m.emitter_id for m in em.get_emitter_list()
                ]
                active_groups[dev_id] = (effect_name, em.label, member_ips)

        # Reverse map: member IP → (effect_name, group_label) for
        # annotating individual devices that are in an active group.
        ip_to_group_effect: dict[str, tuple[str, str]] = {}
        for _gid, (eff, glabel, mips) in active_groups.items():
            for mip in mips:
                ip_to_group_effect[mip] = (eff, glabel)

        result: list[dict[str, Any]] = []
        for dev_id, em in sorted(emitter_snapshot):
            with self._lock:
                ctrl = self._controllers.get(dev_id)
            current_effect: Optional[str] = None
            if ctrl is not None:
                status = ctrl.get_status()
                if status.get("running"):
                    current_effect = status.get("effect")
            is_group: bool = isinstance(em, VirtualMultizoneEmitter)
            is_grid: bool = isinstance(em, VirtualGridEmitter)
            nickname: Optional[str] = self._nicknames.get(dev_id)
            source: Optional[str] = (
                self._play_sources.get(dev_id) if current_effect else None
            )
            is_matrix: bool = (
                hasattr(em, 'is_matrix') and em.is_matrix
            )
            # Resolve label: if the emitter label is just the raw IP
            # (device never responded to queries), try the registry.
            label: str = em.label
            if label == dev_id and self._registry is not None:
                reg_label: Optional[str] = (
                    self._registry.ip_to_label(dev_id, self._keepalive)
                )
                if reg_label:
                    label = reg_label

            entry: dict[str, Any] = {
                "ip": dev_id,
                "label": label,
                "nickname": nickname,
                "product": em.product_name,
                "zones": em.zone_count,
                "is_multizone": em.is_multizone,
                "is_matrix": is_matrix,
                "current_effect": current_effect,
                "source": source,
                "overridden": self.is_overridden(dev_id),
                "is_group": is_group,
                "is_grid": is_grid,
            }
            if is_matrix:
                entry["width"] = getattr(em, 'matrix_width', None)
                entry["height"] = getattr(em, 'matrix_height', None)

            if is_grid:
                entry["mac"] = ""
                entry["group"] = ""
                member_ip_list = [
                    m.emitter_id for m in em.get_emitter_list()
                ]
                entry["member_ips"] = member_ip_list
                entry["power"] = any(
                    self._power_states.get(mip, False)
                    for mip in member_ip_list
                )
            elif is_group:
                entry["mac"] = ""
                entry["group"] = em.label
                member_ip_list: list[str] = [
                    m.emitter_id for m in em.get_emitter_list()
                ]
                entry["member_ips"] = member_ip_list
                # Derive group power from member power states:
                # on if ANY member is powered on.
                entry["power"] = any(
                    self._power_states.get(mip, False)
                    for mip in member_ip_list
                )
            else:
                # Individual device power state.  Default False for
                # unreachable devices — don't claim on if never queried.
                entry["power"] = self._power_states.get(dev_id, False)
                if isinstance(em, LifxEmitter):
                    entry["mac"] = em.transport.mac_str
                    entry["group"] = em.transport.group
                # Annotate with group effect if this device is a
                # member of an active group.
                ge: Optional[tuple[str, str]] = ip_to_group_effect.get(dev_id)
                if ge is not None:
                    entry["group_effect"] = ge[0]
                    entry["group_name"] = ge[1]

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
            except Exception as exc:
                logging.warning("ctrl.stop failed during removal of %s: %s", ip, exc)
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
# Schedule time parsing — shared implementation in schedule_utils.py
# ---------------------------------------------------------------------------
from schedule_utils import (
    parse_time_spec as _parse_time_spec,
    entry_runs_on_day as _entry_runs_on_day,
    resolve_entries as _resolve_entries,
    find_active_entry as _find_active_entry,
    validate_days as _validate_days,
    days_display as _days_display,
    VALID_DAY_LETTERS as _VALID_DAY_LETTERS,
)


# ---------------------------------------------------------------------------
# Scheduler thread
# ---------------------------------------------------------------------------

