"""AirPlay audio player — streams TTS audio to HomePods.

Routes synthesized speech to AirPlay speakers by room name.
Uses pyatv for device discovery and audio streaming.

Room-to-device mapping is configured at startup; unknown rooms
fall back to the default device.  A dedicated event loop thread
keeps connections alive between utterances.

Requires HomePod "Allow Speaker & TV Access" set to
"Everyone on the Same Network" in the Home app.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.2"

import asyncio
import concurrent.futures
import logging
import os
import tempfile
import threading
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.voice.airplay")

# ---------------------------------------------------------------------------
# Optional pyatv import
# ---------------------------------------------------------------------------

try:
    import pyatv
    _HAS_PYATV: bool = True
except ImportError:
    pyatv = None  # type: ignore[assignment]
    _HAS_PYATV = False


class AirPlayPlayer:
    """Stream audio to AirPlay devices by room name.

    Runs a dedicated asyncio event loop in a background thread so
    pyatv connections persist between plays (avoids 7-second
    reconnect on every utterance).

    Args:
        room_map: Dict mapping room name → AirPlay device name.
                  Example: ``{"pi-satellite": "Dining Room"}``
        default_device: Fallback AirPlay device name when room
                        has no explicit mapping.
    """

    def __init__(
        self,
        room_map: Optional[dict[str, str]] = None,
        default_device: Optional[str] = None,
    ) -> None:
        """Initialize the AirPlay player."""
        if not _HAS_PYATV:
            raise ImportError("pyatv not installed — pip install pyatv")

        self._room_map: dict[str, str] = room_map or {}
        self._default_device: Optional[str] = default_device

        # Cached connections: device_name → pyatv AppleTV instance.
        self._connections: dict[str, Any] = {}
        # Cached scan results: device_name → pyatv config.
        self._device_configs: dict[str, Any] = {}

        # Dedicated event loop in a background thread so pyatv
        # connections stay alive between play() calls.
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread: threading.Thread = threading.Thread(
            target=self._loop.run_forever,
            name="airplay-loop",
            daemon=True,
        )
        self._thread.start()

        logger.info(
            "AirPlay player: rooms=%s default=%s",
            list(self._room_map.keys()) or "(none)",
            self._default_device,
        )

        # Pre-scan and pre-connect to default device at startup.
        # Avoids the 5-second mDNS scan penalty on first utterance.
        if self._default_device:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._warmup(self._default_device), self._loop,
                )
                future.result(timeout=15)
            except Exception as exc:
                logger.warning("AirPlay warmup failed: %s", exc)

    def play(self, room: str, audio_bytes: bytes) -> bool:
        """Play WAV audio on the AirPlay device for a room.

        Args:
            room:        Room name (from satellite).
            audio_bytes: WAV-encoded audio data.

        Returns:
            True if playback succeeded, False otherwise.
        """
        device_name: str = self._room_map.get(
            room, self._default_device or "",
        )
        if not device_name:
            logger.warning(
                "[%s] No AirPlay device mapped — skipping playback", room,
            )
            return False

        try:
            # Submit coroutine to the persistent event loop and
            # wait for the result from the calling thread.
            future: concurrent.futures.Future = (
                asyncio.run_coroutine_threadsafe(
                    self._play_async(device_name, audio_bytes),
                    self._loop,
                )
            )
            # 30-second timeout for discovery + connect + stream.
            return future.result(timeout=30)
        except Exception as exc:
            logger.error(
                "[%s] AirPlay playback to '%s' failed: %s",
                room, device_name, exc,
            )
            # Drop cached connection on failure so next attempt re-connects.
            self._connections.pop(device_name, None)
            return False

    async def _get_connection(self, device_name: str) -> Any:
        """Get or create a cached connection to an AirPlay device.

        Args:
            device_name: AirPlay device name.

        Returns:
            Connected pyatv AppleTV instance.

        Raises:
            ConnectionError: If device not found or connection fails.
        """
        # Return cached connection if still valid.
        if device_name in self._connections:
            return self._connections[device_name]

        # Scan for the device if not cached.
        if device_name not in self._device_configs:
            devices = await pyatv.scan(self._loop)
            for device in devices:
                # Cache all discovered devices while we're at it.
                self._device_configs[device.name] = device

        config = self._device_configs.get(device_name)
        if config is None:
            raise ConnectionError(
                f"AirPlay device '{device_name}' not found on network",
            )

        # Connect and cache.
        atv = await pyatv.connect(config, self._loop)
        self._connections[device_name] = atv
        logger.info("AirPlay connected to '%s'", device_name)
        return atv

    async def _play_async(
        self, device_name: str, audio_bytes: bytes,
    ) -> bool:
        """Stream audio to a named AirPlay device.

        Args:
            device_name: AirPlay device name to target.
            audio_bytes: WAV audio data.

        Returns:
            True on success.
        """
        atv = await self._get_connection(device_name)

        # Write WAV to a temp file — pyatv stream_file needs a path.
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        try:
            os.write(tmp_fd, audio_bytes)
        except Exception:
            os.close(tmp_fd)
            raise
        finally:
            # Close fd whether write succeeded or not — the path
            # remains for stream_file to read.
            try:
                os.close(tmp_fd)
            except OSError:
                pass  # Already closed in the success path above.

        try:
            await atv.stream.stream_file(tmp_path)
            logger.info(
                "Played %d bytes on '%s'",
                len(audio_bytes), device_name,
            )
            return True
        except Exception:
            # Connection may have gone stale — drop cache and re-raise.
            self._connections.pop(device_name, None)
            raise
        finally:
            os.unlink(tmp_path)

    async def _warmup(self, device_name: str) -> None:
        """Pre-scan and pre-connect to a device at startup.

        Args:
            device_name: AirPlay device name to connect to.
        """
        logger.info("AirPlay warmup: scanning for '%s'...", device_name)
        devices = await pyatv.scan(self._loop)
        for device in devices:
            self._device_configs[device.name] = device

        if device_name not in self._device_configs:
            logger.warning("AirPlay warmup: '%s' not found", device_name)
            return

        atv = await pyatv.connect(
            self._device_configs[device_name], self._loop,
        )
        self._connections[device_name] = atv
        logger.info("AirPlay warmup: connected to '%s'", device_name)

    def close(self) -> None:
        """Close all cached connections and stop the event loop."""
        for name, atv in self._connections.items():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._close_atv(atv), self._loop,
                ).result(timeout=5)
                logger.info("AirPlay disconnected from '%s'", name)
            except Exception as exc:
                logger.debug(
                    "Failed to close AirPlay connection '%s': %s",
                    name, exc,
                )
        self._connections.clear()
        self._loop.call_soon_threadsafe(self._loop.stop)

    @staticmethod
    async def _close_atv(atv: Any) -> None:
        """Close a pyatv connection."""
        await atv.close()
