"""Reolink NVR snapshot + doorbell-boost adapter.

Connects to a Reolink NVR via ``reolink_aio`` and provides two
capabilities:

- **Snapshots:** caches JPEGs for each configured channel so the
  server can proxy them at ``/api/home/camera/<channel>`` without
  exposing NVR credentials to the browser.
- **Doorbell boost:** polls AI person-detection state on configured
  doorbell channels and, on a rising edge, boosts a set of LIFX
  devices (typically porch whites) to a configured effect by calling
  the local server's ``/api/devices/{ip}/play`` endpoint — which
  automatically marks a phone-style override.  On a held falling
  edge, stops the effect and calls ``/resume`` so the scheduler takes
  over on its next tick.

The adapter runs a background thread with its own asyncio event
loop.  Two concurrent coroutines share the same ``reolink_aio.Host``
connection: one drains snapshots periodically, the other watches AI
events for doorbell channels.

Configuration (in server.json)::

    "nvr": {
        "host": "10.0.0.51",
        "port": 80,
        "username": "admin",
        "password": "secret",
        "channels": [
            {"id": 0, "name": "Shed"},
            {"id": 1, "name": "Backyard"}
        ],
        "snapshot_interval_seconds": 10,
        "doorbell_boost": {
            "enabled": true,
            "channels": [
                {"id": 13, "name": "Front"}
            ],
            "devices": ["10.0.0.124", "10.0.0.147"],
            "effect": "on",
            "params": {"brightness": 100},
            "hold_seconds": 15,
            "poll_interval_seconds": 0.5,
            "server_url": "http://localhost:8420",
            "auth_token": "..."
        }
    }

The ``server_url`` and ``auth_token`` fields are injected by
``run_adapter._create_nvr`` from the top-level server config so they
do not need to be duplicated in the ``nvr`` section by hand.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.2"

import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from adapters.adapter_base import AsyncPollingAdapterBase

logger: logging.Logger = logging.getLogger("glowup.nvr")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default snapshot refresh interval (seconds).
DEFAULT_SNAPSHOT_INTERVAL: float = 10.0

# Minimum interval to avoid hammering the NVR.
MIN_SNAPSHOT_INTERVAL: float = 3.0

# Connection timeout for NVR (seconds).
NVR_CONNECT_TIMEOUT: float = 30.0

# Reconnect delay after NVR failure (seconds).
RECONNECT_DELAY: float = 30.0

# Maximum reconnect delay (seconds).
MAX_RECONNECT_DELAY: float = 300.0

# --- Doorbell boost defaults -----------------------------------------------

# Poll interval for doorbell AI state (seconds).  Fast enough to catch
# a brief approach, slow enough to avoid hammering the NVR.
DEFAULT_DOORBELL_POLL_INTERVAL: float = 0.5

# How long to hold the boost after the person clears before reverting
# to scheduler control.  Gives visitors time to step back without the
# lights flickering off and on.
DEFAULT_DOORBELL_HOLD_SECONDS: float = 15.0

# The AI detection type we key on.  Reolink reports this as "people"
# (plural) for doorbell-family cameras — verified empirically on the
# Reolink Video Doorbell WiFi-W.
DOORBELL_AI_TYPE: str = "people"

# HTTP timeout for server API calls from the boost loop (seconds).
BOOST_HTTP_TIMEOUT: float = 5.0

# Default local server URL if not injected from top-level config.
DEFAULT_SERVER_URL: str = "http://localhost:8420"

# ---------------------------------------------------------------------------
# Optional dependency check
# ---------------------------------------------------------------------------

try:
    from reolink_aio.api import Host
    _HAS_REOLINK: bool = True
except ImportError:
    _HAS_REOLINK = False
    Host = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# NvrAdapter
# ---------------------------------------------------------------------------

class NvrAdapter(AsyncPollingAdapterBase):
    """Pulls JPEG snapshots from a Reolink NVR and caches them.

    Args:
        config: The ``"nvr"`` section of server.json.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize the NVR adapter.

        Args:
            config: NVR config section from server.json, with two
                fields (``server_url`` and ``auth_token``) injected
                by ``run_adapter._create_nvr`` for the doorbell boost
                path.
        """
        super().__init__(
            thread_name="nvr-adapter",
            reconnect_delay=RECONNECT_DELAY,
            max_reconnect_delay=MAX_RECONNECT_DELAY,
        )
        self._host_addr: str = config.get("host", "")
        self._port: int = int(config.get("port", 80))
        self._username: str = config.get("username", "")
        self._password: str = config.get("password", "")
        self._channels: list[dict[str, Any]] = config.get("channels", [])
        self._interval: float = max(
            float(config.get("snapshot_interval_seconds", DEFAULT_SNAPSHOT_INTERVAL)),
            MIN_SNAPSHOT_INTERVAL,
        )

        self._host: Any = None

        # Snapshot cache: channel_id → (jpeg_bytes, timestamp).
        self._lock: threading.Lock = threading.Lock()
        self._snapshots: dict[int, tuple[bytes, float]] = {}

        # -- Doorbell boost config --------------------------------------
        # Pulled out of a nested "doorbell_boost" block so the feature
        # can ship disabled by default and cleanly evolve.
        boost_cfg: dict[str, Any] = config.get("doorbell_boost", {}) or {}
        self._db_enabled: bool = bool(boost_cfg.get("enabled", False))
        self._db_channels: list[dict[str, Any]] = list(
            boost_cfg.get("channels", []) or [],
        )
        self._db_devices: list[str] = list(boost_cfg.get("devices", []) or [])
        self._db_effect: str = str(boost_cfg.get("effect", "on"))
        self._db_params: dict[str, Any] = dict(
            boost_cfg.get("params", {"brightness": 100}) or {},
        )
        self._db_hold: float = float(
            boost_cfg.get("hold_seconds", DEFAULT_DOORBELL_HOLD_SECONDS),
        )
        self._db_poll_interval: float = float(
            boost_cfg.get(
                "poll_interval_seconds", DEFAULT_DOORBELL_POLL_INTERVAL,
            ),
        )
        # server_url / auth_token injected by run_adapter, but fall back
        # to explicit config if the caller set them by hand.
        self._db_server_url: str = str(
            boost_cfg.get("server_url")
            or config.get("server_url")
            or DEFAULT_SERVER_URL,
        )
        self._db_auth_token: str = str(
            boost_cfg.get("auth_token")
            or config.get("auth_token")
            or "",
        )

        # Per-channel edge-detection state for the boost loop.
        # ch_id → {"present": bool, "cleared_at": float}
        #   - present: True while ai_detected(ch, "people") is True
        #   - cleared_at: wall time when the falling edge happened,
        #                 0.0 means "not in hold window"
        self._db_state: dict[int, dict[str, float]] = {}

        # Handle to the async task that runs the doorbell loop, so
        # _disconnect can cancel it cleanly.
        self._db_task: Optional[asyncio.Task] = None

    def _check_prerequisites(self) -> bool:
        """Check reolink_aio, host, and channels."""
        if not _HAS_REOLINK:
            logger.warning(
                "reolink_aio not installed — NVR adapter disabled. "
                "Install with: pip install reolink_aio"
            )
            return False

        if not self._host_addr:
            logger.error("NVR adapter requires host in config")
            return False

        if not self._channels:
            logger.warning("No NVR channels configured")
            return False

        return True

    # --- Public API --------------------------------------------------------

    def get_snapshot(self, channel_id: int) -> Optional[bytes]:
        """Return the most recent cached JPEG snapshot for a channel.

        Args:
            channel_id: The NVR channel number.

        Returns:
            JPEG bytes, or ``None`` if no snapshot is available.
        """
        with self._lock:
            entry: Optional[tuple[bytes, float]] = self._snapshots.get(channel_id)
            if entry:
                return entry[0]
        return None

    def get_channels(self) -> list[dict[str, Any]]:
        """Return the configured channel list.

        Returns:
            List of channel dicts with ``id`` and ``name``.
        """
        return list(self._channels)

    def get_status(self) -> dict[str, Any]:
        """Return adapter status for API responses."""
        with self._lock:
            cached: dict[int, float] = {
                ch_id: ts for ch_id, (_, ts) in self._snapshots.items()
            }
        return {
            "connected": self._host is not None,
            "channels": self._channels,
            "cached_snapshots": {
                ch_id: time.time() - ts
                for ch_id, ts in cached.items()
            },
        }

    # --- AsyncPollingAdapterBase interface ----------------------------------

    async def _connect(self) -> None:
        """Connect to the Reolink NVR and start the doorbell loop."""
        logger.info("Connecting to NVR at %s:%d", self._host_addr, self._port)
        self._host = Host(
            self._host_addr,
            self._username,
            self._password,
            port=self._port,
        )
        await self._host.get_host_data()
        nvr_name: str = getattr(self._host, "nvr_name", "unknown")
        logger.info("Connected to NVR: %s", nvr_name)

        # Kick off the doorbell polling loop as a sibling coroutine
        # sharing the Host connection.  It runs until _disconnect
        # cancels it.  Skipped entirely if the feature is disabled or
        # misconfigured, so non-doorbell deployments pay zero cost.
        if self._should_run_doorbell_loop():
            self._db_task = asyncio.create_task(self._run_doorbell_loop())

    async def _disconnect(self) -> None:
        """Cancel the doorbell loop and disconnect from the NVR."""
        # Cancel the doorbell task first so it doesn't race with the
        # Host teardown when the loop calls get_ai_state_all_ch.
        if self._db_task is not None and not self._db_task.done():
            self._db_task.cancel()
            try:
                await self._db_task
            except (asyncio.CancelledError, Exception):
                pass
        self._db_task = None

        if self._host:
            try:
                await self._host.logout()
            except Exception:
                pass
            self._host = None

    # --- Doorbell boost -----------------------------------------------------

    def _should_run_doorbell_loop(self) -> bool:
        """Return True if the doorbell loop has a valid config."""
        if not self._db_enabled:
            return False
        if not self._db_channels:
            logger.info(
                "Doorbell boost enabled but no channels configured — skipping",
            )
            return False
        if not self._db_devices:
            logger.info(
                "Doorbell boost enabled but no devices configured — skipping",
            )
            return False
        if not self._db_auth_token:
            logger.warning(
                "Doorbell boost enabled but auth_token missing — skipping. "
                "run_adapter._create_nvr should inject it from server.json.",
            )
            return False
        return True

    async def _run_doorbell_loop(self) -> None:
        """Poll AI people-detection on doorbell channels and drive boost.

        On a rising edge (``False → True``) for ``ai[people]`` on any
        configured doorbell channel, calls the boost-on path once.
        On a falling edge, starts a ``hold_seconds`` timer; if people
        reappears during the hold, the timer is cancelled.  When the
        hold expires with people still absent, the boost is released.
        """
        # Seed per-channel edge state.
        channel_ids: list[int] = [
            int(c["id"]) for c in self._db_channels if "id" in c
        ]
        for cid in channel_ids:
            self._db_state[cid] = {"present": 0.0, "cleared_at": 0.0}

        logger.info(
            "Doorbell boost active: channels=%s devices=%s hold=%.0fs "
            "effect=%s params=%s",
            channel_ids, self._db_devices, self._db_hold,
            self._db_effect, self._db_params,
        )

        # Any-channel "present" aggregation — the boost should be
        # on if *any* configured doorbell channel currently sees a
        # person, off only when *all* are clear and holds expired.
        any_boost_on: bool = False

        try:
            while self._running:
                try:
                    await self._host.get_ai_state_all_ch()
                except Exception as exc:
                    logger.warning("Doorbell AI poll failed: %s", exc)
                    await asyncio.sleep(1.0)
                    continue

                now: float = time.time()
                any_present_now: bool = False
                any_in_hold: bool = False

                for cid in channel_ids:
                    state = self._db_state[cid]
                    try:
                        present_now: bool = bool(
                            self._host.ai_detected(cid, DOORBELL_AI_TYPE),
                        )
                    except Exception as exc:
                        logger.debug(
                            "ai_detected(%d, %s) failed: %s",
                            cid, DOORBELL_AI_TYPE, exc,
                        )
                        continue

                    present_prev: bool = bool(state["present"])

                    if present_now and not present_prev:
                        logger.info("Doorbell ch%d people -> detected", cid)
                        state["present"] = 1.0
                        state["cleared_at"] = 0.0
                    elif (
                        present_now and present_prev
                        and state["cleared_at"] > 0.0
                    ):
                        # Re-entered during a hold window — cancel the
                        # pending release so the lights stay on.
                        logger.info(
                            "Doorbell ch%d re-detected during hold, "
                            "cancelling revert", cid,
                        )
                        state["cleared_at"] = 0.0
                    elif not present_now and present_prev:
                        if state["cleared_at"] == 0.0:
                            state["cleared_at"] = now
                            logger.info(
                                "Doorbell ch%d cleared, holding %.0fs",
                                cid, self._db_hold,
                            )

                    # Count toward the boost aggregate.
                    if state["present"] > 0.0:
                        if state["cleared_at"] == 0.0:
                            any_present_now = True
                        elif now - state["cleared_at"] < self._db_hold:
                            any_in_hold = True
                        else:
                            # Hold expired — release this channel.
                            logger.info(
                                "Doorbell ch%d hold expired",
                                cid,
                            )
                            state["present"] = 0.0
                            state["cleared_at"] = 0.0

                boost_should_be_on: bool = any_present_now or any_in_hold

                if boost_should_be_on and not any_boost_on:
                    await asyncio.to_thread(self._http_boost_on)
                    any_boost_on = True
                elif not boost_should_be_on and any_boost_on:
                    await asyncio.to_thread(self._http_boost_off)
                    any_boost_on = False

                await asyncio.sleep(self._db_poll_interval)
        except asyncio.CancelledError:
            # Normal shutdown path — release the boost if we're holding
            # it so we don't leave the porch bulbs overridden after a
            # reconnect cycle.
            if any_boost_on:
                try:
                    await asyncio.to_thread(self._http_boost_off)
                except Exception as exc:
                    logger.warning(
                        "Doorbell boost-off on shutdown failed: %s", exc,
                    )
            raise

    # --- Boost HTTP calls (sync, called via to_thread) ---------------------

    def _http_boost_on(self) -> None:
        """Start the boost effect on every configured device.

        Uses ``/api/devices/{ip}/play`` which already marks each device
        as phone-overridden so the scheduler will not clobber the
        boost while it's active.
        """
        body: dict[str, Any] = {
            "effect": self._db_effect,
            "params": self._db_params,
        }
        logger.info("Doorbell boost ON -> %s", self._db_devices)
        for ip in self._db_devices:
            try:
                self._http_post(f"/api/devices/{ip}/play", body)
            except Exception as exc:
                logger.warning("boost play %s failed: %s", ip, exc)

    def _http_boost_off(self) -> None:
        """Stop the boost effect and release override to the scheduler.

        Calls ``/stop`` (effect off, override still marked) immediately
        followed by ``/resume`` (override cleared).  The explicit stop
        matters when the scheduled window is not currently active —
        without it, the bulbs would stay at boost brightness until the
        next scheduled entry triggered.
        """
        logger.info("Doorbell boost OFF -> %s", self._db_devices)
        for ip in self._db_devices:
            try:
                self._http_post(f"/api/devices/{ip}/stop", None)
            except Exception as exc:
                logger.warning("boost stop %s failed: %s", ip, exc)
            try:
                self._http_post(f"/api/devices/{ip}/resume", None)
            except Exception as exc:
                logger.warning("boost resume %s failed: %s", ip, exc)

    def _http_post(self, path: str, body: Optional[dict[str, Any]]) -> int:
        """POST to the local server and return the HTTP status.

        Args:
            path: URL path starting with ``/``.
            body: JSON body, or ``None`` for an empty POST.

        Returns:
            HTTP status code.

        Raises:
            urllib.error.URLError: network or server failure.
        """
        url: str = f"{self._db_server_url}{path}"
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._db_auth_token}",
        }
        data: Optional[bytes] = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=BOOST_HTTP_TIMEOUT) as resp:
            return int(resp.status)

    async def _run_cycle(self) -> None:
        """Periodically pull snapshots from all channels."""
        while self._running:
            for ch_cfg in self._channels:
                if not self._running:
                    break
                ch_id: int = ch_cfg["id"]
                ch_name: str = ch_cfg.get("name", str(ch_id))
                self._hb(f"snapshot:{ch_name}")
                try:
                    jpeg: Optional[bytes] = await self._host.get_snapshot(ch_id)
                    if jpeg:
                        with self._lock:
                            self._snapshots[ch_id] = (jpeg, time.time())
                except Exception as exc:
                    logger.warning(
                        "Snapshot error for channel %d (%s): %s",
                        ch_id, type(exc).__name__, exc,
                    )

            await asyncio.sleep(self._interval)

    # --- Hooks -------------------------------------------------------------

    def _on_started(self) -> None:
        """Log NVR-specific start message."""
        channel_names: str = ", ".join(
            ch.get("name", str(ch["id"])) for ch in self._channels
        )
        logger.info(
            "NVR adapter started — %d channel(s): %s",
            len(self._channels), channel_names,
        )

    def _on_stopped(self) -> None:
        """Log NVR-specific stop message."""
        logger.info("NVR adapter stopped")
