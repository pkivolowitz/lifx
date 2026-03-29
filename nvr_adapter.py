"""Reolink NVR snapshot adapter — proxies camera snapshots for /home.

Connects to a Reolink NVR via ``reolink_aio`` and provides JPEG
snapshots for each configured channel.  The server proxies these
at ``/api/home/camera/<channel>`` so the browser never touches
NVR credentials.

Snapshots are cached in memory and refreshed on a configurable
interval (default 10s).  The adapter runs a background thread with
its own asyncio event loop.

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
        "snapshot_interval_seconds": 10
    }
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import asyncio
import logging
import threading
import time
from typing import Any, Optional

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

class NvrAdapter:
    """Pulls JPEG snapshots from a Reolink NVR and caches them.

    Args:
        config: The ``"nvr"`` section of server.json.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize the NVR adapter.

        Args:
            config: NVR config section from server.json.
        """
        self._host_addr: str = config.get("host", "")
        self._port: int = int(config.get("port", 80))
        self._username: str = config.get("username", "")
        self._password: str = config.get("password", "")
        self._channels: list[dict[str, Any]] = config.get("channels", [])
        self._interval: float = max(
            float(config.get("snapshot_interval_seconds", DEFAULT_SNAPSHOT_INTERVAL)),
            MIN_SNAPSHOT_INTERVAL,
        )

        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._host: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Snapshot cache: channel_id → (jpeg_bytes, timestamp).
        self._lock: threading.Lock = threading.Lock()
        self._snapshots: dict[int, tuple[bytes, float]] = {}

    def start(self) -> None:
        """Start the NVR adapter in a background thread."""
        if not _HAS_REOLINK:
            logger.warning(
                "reolink_aio not installed — NVR adapter disabled. "
                "Install with: pip install reolink_aio"
            )
            return

        if not self._host_addr:
            logger.error("NVR adapter requires host in config")
            return

        if not self._channels:
            logger.warning("No NVR channels configured")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="nvr-adapter",
        )
        self._thread.start()
        channel_names: str = ", ".join(
            ch.get("name", str(ch["id"])) for ch in self._channels
        )
        logger.info(
            "NVR adapter started — %d channel(s): %s",
            len(self._channels), channel_names,
        )

    def stop(self) -> None:
        """Stop the adapter and disconnect from NVR."""
        self._running = False
        if self._loop and self._host:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._disconnect(), self._loop,
                )
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=10.0)
        logger.info("NVR adapter stopped")

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

    # --- Internal event loop -----------------------------------------------

    def _run_loop(self) -> None:
        """Background thread: runs asyncio event loop for reolink_aio."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main_loop())
        except Exception as exc:
            logger.error("NVR adapter loop crashed: %s", exc)
        finally:
            self._loop.close()

    async def _main_loop(self) -> None:
        """Async main loop: connect and poll snapshots."""
        reconnect_delay: float = RECONNECT_DELAY

        while self._running:
            try:
                await self._connect()
                reconnect_delay = RECONNECT_DELAY
                await self._snapshot_loop()
            except Exception as exc:
                logger.error(
                    "NVR connection error: %s — retrying in %.0fs",
                    exc, reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(
                    reconnect_delay * 2.0, MAX_RECONNECT_DELAY,
                )

    async def _connect(self) -> None:
        """Connect to the Reolink NVR."""
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

    async def _disconnect(self) -> None:
        """Disconnect from NVR."""
        if self._host:
            try:
                await self._host.logout()
            except Exception:
                pass
            self._host = None

    async def _snapshot_loop(self) -> None:
        """Periodically pull snapshots from all channels."""
        while self._running:
            for ch_cfg in self._channels:
                if not self._running:
                    break
                ch_id: int = ch_cfg["id"]
                try:
                    jpeg: Optional[bytes] = await self._host.get_snapshot(ch_id)
                    if jpeg:
                        with self._lock:
                            self._snapshots[ch_id] = (jpeg, time.time())
                except Exception as exc:
                    logger.debug(
                        "Snapshot error for channel %d: %s", ch_id, exc,
                    )

            await asyncio.sleep(self._interval)
