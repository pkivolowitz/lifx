"""Matter adapter — controls Matter-over-WiFi devices via python-matter-server.

Connects to a running python-matter-server instance (WebSocket API),
discovers commissioned nodes, and exposes them as on/off devices in
the GlowUp signal bus.  Supports power on/off commands via the
standard GlowUp REST API.

Configuration in server.json::

    "matter": {
        "enabled": true,
        "server_url": "ws://localhost:5580/ws",
        "devices": {
            "Backyard Lights 1": {"node_id": 10},
            "Backyard Lights 2": {"node_id": 11},
            "Test Plug":         {"node_id": 9}
        }
    }

Requires: python-matter-server, home-assistant-chip-clusters.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import asyncio
import logging
import threading
import time
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.matter")

# ---------------------------------------------------------------------------
# Optional dependency check
# ---------------------------------------------------------------------------

try:
    import aiohttp
    from matter_server.client import MatterClient
    from chip.clusters import Objects as clusters
    _HAS_MATTER: bool = True
except ImportError:
    _HAS_MATTER = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default python-matter-server WebSocket URL.
DEFAULT_SERVER_URL: str = "ws://localhost:5580/ws"

# Reconnect delay on connection failure (seconds).
RECONNECT_DELAY: float = 10.0

# Maximum reconnect delay (seconds).
MAX_RECONNECT_DELAY: float = 120.0

# On/Off cluster endpoint (standard Matter on/off plug).
ONOFF_ENDPOINT: int = 1

# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class MatterAdapter:
    """Matter device adapter for GlowUp.

    Connects to python-matter-server, maps node IDs to friendly names,
    and provides on/off control.  State changes are published to the
    signal bus.

    Args:
        config:     Matter config dict from server.json.
        bus:        GlowUp SignalBus instance for state publishing.
        server_url: python-matter-server WebSocket URL.
    """

    def __init__(
        self,
        config: dict[str, Any],
        bus: Any,
        server_url: str = DEFAULT_SERVER_URL,
    ) -> None:
        """Initialize the Matter adapter.

        Args:
            config:     Matter section of server config.
            bus:        SignalBus for publishing device state.
            server_url: WebSocket URL for python-matter-server.
        """
        self._config: dict[str, Any] = config
        self._bus: Any = bus
        self._server_url: str = server_url
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._client: Optional[Any] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Command queue: (node_id, command, result_future).
        # HTTP/scheduler threads put commands here; the background
        # loop picks them up and executes in its own async context.
        import queue
        self._cmd_queue: "queue.Queue[tuple]" = queue.Queue()

        # Node ID → friendly name mapping from config.
        self._devices: dict[int, str] = {}
        for name, dev_cfg in config.get("devices", {}).items():
            node_id: int = dev_cfg.get("node_id", 0)
            if node_id > 0:
                self._devices[node_id] = name

        # Reverse: friendly name → node ID.
        self._name_to_node: dict[str, int] = {
            v: k for k, v in self._devices.items()
        }

        # Cached power state: device name → bool (True = on).
        self._power_states: dict[str, bool] = {}

    @property
    def running(self) -> bool:
        """Whether the adapter is running."""
        return self._running

    def start(self) -> None:
        """Start the Matter adapter background thread."""
        if not _HAS_MATTER:
            logger.warning(
                "Matter adapter not started — "
                "python-matter-server not installed"
            )
            return

        if not self._devices:
            logger.warning(
                "Matter adapter not started — no devices configured"
            )
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="matter-adapter",
        )
        self._thread.start()
        logger.info(
            "Matter adapter started — %d device(s), server %s",
            len(self._devices), self._server_url,
        )

    def stop(self) -> None:
        """Stop the Matter adapter."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10.0)
        logger.info("Matter adapter stopped")

    # -- Background thread --------------------------------------------------

    def _run_loop(self) -> None:
        """Background thread: connect, subscribe, reconnect on failure."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        delay: float = RECONNECT_DELAY
        while self._running:
            try:
                self._loop.run_until_complete(self._run_async())
                delay = RECONNECT_DELAY  # Reset on clean exit.
            except (Exception, asyncio.CancelledError) as exc:
                # CancelledError is BaseException in Python 3.8+; catching
                # it explicitly prevents it from silently killing this thread
                # when the WebSocket closes mid-session.
                logger.warning(
                    "Matter connection failed: %s — retrying in %.0fs",
                    exc, delay,
                )
                # Interruptible sleep.
                end: float = time.monotonic() + delay
                while time.monotonic() < end and self._running:
                    time.sleep(1.0)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def _run_async(self) -> None:
        """Async main: connect to Matter server, poll state."""
        async with aiohttp.ClientSession() as session:
            self._client = MatterClient(self._server_url, session)
            await self._client.connect()

            # start_listening() is REQUIRED — it runs the WebSocket
            # receive loop that delivers node updates and command
            # responses.  Without it, send_device_command hangs
            # forever waiting for a response no one reads.
            listen_task = asyncio.create_task(
                self._client.start_listening(),
            )

            # Wait for node data to arrive from the server.
            await asyncio.sleep(2.0)
            logger.info("Connected to Matter server")

            # Initial state sync.
            await self._sync_state()

            # Poll state and drain command queue while running.
            try:
                while self._running:
                    # Exit immediately if the WebSocket receive loop died
                    # so _run_loop can reconnect rather than spinning
                    # against a dead connection.
                    if listen_task.done():
                        exc = (listen_task.exception()
                               if not listen_task.cancelled() else None)
                        logger.warning(
                            "Matter WebSocket closed: %s — reconnecting",
                            exc,
                        )
                        break

                    # Drain pending commands from HTTP/scheduler threads.
                    while not self._cmd_queue.empty():
                        try:
                            node_id, command, future = (
                                self._cmd_queue.get_nowait()
                            )
                            try:
                                await self._send_command(node_id, command)
                                future.set_result(True)
                            except Exception as exc:
                                future.set_exception(exc)
                        except Exception:
                            break

                    await asyncio.sleep(1.0)
                    await self._sync_state()
            finally:
                if not listen_task.done():
                    listen_task.cancel()
                try:
                    await listen_task
                except (asyncio.CancelledError, Exception):
                    pass
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                self._client = None

    async def _sync_state(self) -> None:
        """Read on/off state of all configured devices and publish to bus."""
        if self._client is None:
            return

        for node_id, name in self._devices.items():
            try:
                node = self._client.get_node(node_id)
                if node is None or not node.available:
                    continue
                # Read OnOff attribute via the proper API.
                on_off = node.get_attribute_value(
                    ONOFF_ENDPOINT, None,
                    clusters.OnOff.Attributes.OnOff,
                )
                if on_off is not None:
                    self._power_states[name] = bool(on_off)
                    if self._bus is not None:
                        self._bus.publish(
                            f"matter:{name}:state",
                            "on" if on_off else "off",
                        )
            except Exception as exc:
                logger.debug(
                    "State read failed for %s (node %d): %s",
                    name, node_id, exc,
                )

    # -- Command interface --------------------------------------------------

    async def _send_command(
        self,
        node_id: int,
        command: Any,
    ) -> None:
        """Send a Matter command to a device.

        Args:
            node_id: Matter node ID.
            command: CHIP cluster command object.
        """
        if self._client is None:
            raise RuntimeError("Matter client not connected")
        await self._client.send_device_command(
            node_id=node_id,
            endpoint_id=ONOFF_ENDPOINT,
            command=command,
        )

    def power_on(self, device_name: str) -> bool:
        """Turn a Matter device on.

        Args:
            device_name: Friendly name from config.

        Returns:
            True if command sent successfully.
        """
        return self._power_command(device_name, clusters.OnOff.Commands.On())

    def power_off(self, device_name: str) -> bool:
        """Turn a Matter device off.

        Args:
            device_name: Friendly name from config.

        Returns:
            True if command sent successfully.
        """
        return self._power_command(device_name, clusters.OnOff.Commands.Off())

    def toggle(self, device_name: str) -> bool:
        """Toggle a Matter device.

        Args:
            device_name: Friendly name from config.

        Returns:
            True if command sent successfully.
        """
        return self._power_command(
            device_name, clusters.OnOff.Commands.Toggle(),
        )

    def _power_command(self, device_name: str, command: Any) -> bool:
        """Execute a power command by device name.

        Puts the command on the queue for the background loop to
        execute in its own async context (where the MatterClient
        and aiohttp session live).

        Args:
            device_name: Friendly name from config.
            command:     CHIP cluster command object.

        Returns:
            True if command sent successfully.
        """
        node_id: Optional[int] = self._name_to_node.get(device_name)
        if node_id is None:
            logger.warning("Unknown Matter device: %s", device_name)
            return False

        if self._client is None:
            logger.error(
                "Matter command failed for %s: Not connected",
                device_name,
            )
            return False

        import concurrent.futures
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._cmd_queue.put((node_id, command, future))

        try:
            future.result(timeout=10.0)
            logger.info(
                "Matter command sent: %s → %s",
                device_name, type(command).__name__,
            )
            return True
        except concurrent.futures.TimeoutError:
            logger.error(
                "Matter command timed out for %s", device_name,
            )
            return False
        except Exception as exc:
            logger.error(
                "Matter command failed for %s: %s",
                device_name, exc,
            )
            return False

    def get_device_names(self) -> list[str]:
        """Return list of configured Matter device names."""
        return list(self._name_to_node.keys())

    def get_power_state(self, device_name: str) -> Optional[bool]:
        """Return cached power state for a device.

        Args:
            device_name: Friendly name from config.

        Returns:
            True if on, False if off, None if unknown.
        """
        return self._power_states.get(device_name)

    def get_status(self) -> dict[str, Any]:
        """Return adapter status for API responses."""
        return {
            "running": self._running,
            "connected": self._client is not None,
            "devices": {
                name: {"node_id": nid}
                for name, nid in self._name_to_node.items()
            },
        }
