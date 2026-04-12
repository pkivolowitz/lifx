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

# The AI detection type we key on.  Reolink reports this as "people"
# (plural) for doorbell-family cameras — verified empirically on the
# Reolink Video Doorbell WiFi-W.
DOORBELL_AI_TYPE: str = "people"


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

    def __init__(
        self,
        config: dict[str, Any],
        mqtt_client: Any = None,
    ) -> None:
        """Initialize the NVR adapter.

        Args:
            config:      NVR config section from server.json.
            mqtt_client: Optional paho MQTT client for publishing
                         doorbell person-detection signals to the bus.
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
        self._mqtt_client: Any = mqtt_client

        # Snapshot cache: channel_id → (jpeg_bytes, timestamp).
        self._lock: threading.Lock = threading.Lock()
        self._snapshots: dict[int, tuple[bytes, float]] = {}

        # -- Doorbell person-detection config ---------------------------
        # Publishes doorbell:person signal (1.0 / 0.0) to the MQTT bus
        # so TriggerOperators can handle porch lights through the
        # standard SOE pipeline.
        boost_cfg: dict[str, Any] = config.get("doorbell_boost", {}) or {}
        self._db_enabled: bool = bool(boost_cfg.get("enabled", False))
        self._db_channels: list[dict[str, Any]] = list(
            boost_cfg.get("channels", []) or [],
        )
        self._db_poll_interval: float = float(
            boost_cfg.get(
                "poll_interval_seconds", DEFAULT_DOORBELL_POLL_INTERVAL,
            ),
        )

        # Per-channel edge-detection state.
        # ch_id → {"present": float (0.0 or 1.0)}
        self._db_state: dict[int, dict[str, float]] = {}

        # Handle to the async task that runs the doorbell loop.
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
                "Doorbell detection enabled but no channels configured — skipping",
            )
            return False
        return True

    async def _run_doorbell_loop(self) -> None:
        """Poll AI people-detection and publish ``doorbell:person`` signal.

        Publishes 1.0 on rising edge (person detected), 0.0 on falling
        edge (person gone).  The TriggerOperator watching this signal
        handles porch lights through the standard SOE pipeline —
        play/stop/resume, debounce, schedule conflict.
        """
        channel_ids: list[int] = [
            int(c["id"]) for c in self._db_channels if "id" in c
        ]
        for cid in channel_ids:
            self._db_state[cid] = {"present": 0.0}

        logger.info(
            "Doorbell person detection active: channels=%s",
            channel_ids,
        )

        # Aggregate across channels — any channel seeing a person = 1.0.
        last_published: float = 0.0

        try:
            while self._running:
                try:
                    await self._host.get_ai_state_all_ch()
                except Exception as exc:
                    logger.warning("Doorbell AI poll failed: %s", exc)
                    await asyncio.sleep(1.0)
                    continue

                any_present: bool = False
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

                    prev: bool = bool(state["present"])
                    if present_now and not prev:
                        logger.info("Doorbell ch%d people -> detected", cid)
                    elif not present_now and prev:
                        logger.info("Doorbell ch%d people -> cleared", cid)
                    state["present"] = 1.0 if present_now else 0.0

                    if present_now:
                        any_present = True

                # Publish signal on edge change only.
                signal_value: float = 1.0 if any_present else 0.0
                if signal_value != last_published:
                    self._publish_signal("doorbell:person", signal_value)
                    last_published = signal_value

                await asyncio.sleep(self._db_poll_interval)
        except asyncio.CancelledError:
            # Publish 0 on shutdown so the trigger clears.
            if last_published > 0.0:
                self._publish_signal("doorbell:person", 0.0)
            raise

    # --- MQTT signal publishing -------------------------------------------

    def _publish_signal(self, signal_name: str, value: float) -> None:
        """Publish a signal value to the MQTT bus.

        Topic: ``glowup/signals/{signal_name}`` so the server's
        remote-signal subscriber writes it to the local SignalBus.
        Matches the convention used by vivint_adapter and other adapters.

        Args:
            signal_name: Signal name (e.g., ``"doorbell:person"``).
            value:       Signal value (0.0 or 1.0).
        """
        if not self._mqtt_client:
            logger.debug(
                "No MQTT client — cannot publish %s=%s",
                signal_name, value,
            )
            return
        topic: str = f"glowup/signals/{signal_name}"
        try:
            self._mqtt_client.publish(topic, str(value), qos=1)
            logger.info("Published %s = %s", topic, value)
        except Exception as exc:
            logger.warning("MQTT publish %s failed: %s", topic, exc)

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
