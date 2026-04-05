"""Server-side proxy for out-of-process adapters.

Each ``AdapterProxy`` instance represents one adapter process.
It subscribes to the adapter's MQTT heartbeat and status topics,
caches the latest health data, and provides a ``send_command()``
method for request/reply over MQTT.

The server creates one proxy per adapter and uses them wherever
it previously called adapter methods directly.

Usage::

    proxy = AdapterProxy("zigbee", mqtt_client)
    status = proxy.get_status()         # cached heartbeat data
    alive = proxy.is_alive()            # LWT + heartbeat staleness
    result = proxy.send_command("send", {"device": "LRTV", "payload": {"state": "ON"}})
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.1"

import concurrent.futures
import json
import logging
import threading
import time
import uuid
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.adapter_proxy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How stale a heartbeat can be before the adapter is considered dead
# (3 missed heartbeats at 15-second intervals).
HEARTBEAT_STALE_S: float = 50.0

# Default timeout for command responses.
COMMAND_TIMEOUT_S: float = 10.0

# MQTT topic templates (must match process_base.py).
TOPIC_STATUS: str = "glowup/adapter/{id}/status"
TOPIC_HEARTBEAT: str = "glowup/adapter/{id}/heartbeat"
TOPIC_COMMAND: str = "glowup/adapter/{id}/command/{action}"
TOPIC_RESPONSE_PREFIX: str = "glowup/adapter/{id}/response/"


class AdapterProxy:
    """Server-side proxy for an out-of-process adapter.

    Subscribes to the adapter's MQTT topics and provides
    synchronous access to adapter state and commands.

    Args:
        adapter_id:  Adapter identifier (e.g., "zigbee").
        mqtt_client: Connected paho MQTT client (the server's client).
    """

    def __init__(
        self,
        adapter_id: str,
        mqtt_client: Any,
    ) -> None:
        """Initialize the proxy and subscribe to adapter topics."""
        self._adapter_id: str = adapter_id
        self._client: Any = mqtt_client

        # Cached state from MQTT messages.
        self._online: bool = False
        self._last_heartbeat: Optional[dict[str, Any]] = None
        self._last_heartbeat_ts: float = 0.0
        self._lock: threading.Lock = threading.Lock()

        # Pending command responses: correlation_id → Future.
        self._pending: dict[str, concurrent.futures.Future] = {}
        self._pending_lock: threading.Lock = threading.Lock()

        # Subscribe to this adapter's topics.
        status_topic: str = TOPIC_STATUS.format(id=adapter_id)
        heartbeat_topic: str = TOPIC_HEARTBEAT.format(id=adapter_id)
        response_topic: str = f"glowup/adapter/{adapter_id}/response/+"

        mqtt_client.subscribe(status_topic, qos=1)
        mqtt_client.subscribe(heartbeat_topic, qos=0)
        mqtt_client.subscribe(response_topic, qos=1)

        # Register the message callback.  The server's MQTT client
        # dispatches messages by topic — we filter in our callback.
        mqtt_client.message_callback_add(status_topic, self._on_status)
        mqtt_client.message_callback_add(heartbeat_topic, self._on_heartbeat)
        mqtt_client.message_callback_add(
            f"glowup/adapter/{adapter_id}/response/+",
            self._on_response,
        )

        logger.info("[proxy:%s] Subscribed to adapter topics", adapter_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return the adapter's last known status.

        Returns the ``detail`` field from the most recent heartbeat,
        plus ``running`` and ``connected`` flags for compatibility
        with the existing handler code that checks these keys.

        Returns:
            Status dict, or ``{"running": False}`` if no data.
        """
        with self._lock:
            if self._last_heartbeat is None:
                return {"running": self._online}
            detail: dict[str, Any] = dict(
                self._last_heartbeat.get("detail", {}),
            )
            detail["running"] = self._online and not self._is_stale()
            return detail

    def is_alive(self) -> bool:
        """Check if the adapter is alive (LWT online + fresh heartbeat).

        Returns:
            True if the adapter is online and heartbeat is fresh.
        """
        with self._lock:
            return self._online and not self._is_stale()

    def send_command(
        self,
        action: str,
        params: Optional[dict[str, Any]] = None,
        timeout: float = COMMAND_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Send a command to the adapter and wait for a response.

        Args:
            action:  Command action name (e.g., "send", "restart").
            params:  Command parameters.
            timeout: Max seconds to wait for response.

        Returns:
            Response dict from the adapter.

        Raises:
            TimeoutError: If no response within timeout.
        """
        correlation_id: str = uuid.uuid4().hex[:8]
        future: concurrent.futures.Future = concurrent.futures.Future()

        with self._pending_lock:
            self._pending[correlation_id] = future

        # Publish command.
        topic: str = TOPIC_COMMAND.format(
            id=self._adapter_id, action=action,
        )
        # The + wildcard in TOPIC_COMMAND is for subscribe only.
        # For publish, we use the concrete action.
        topic = f"glowup/adapter/{self._adapter_id}/command/{action}"
        payload: dict[str, Any] = {
            "correlation_id": correlation_id,
            "params": params or {},
        }
        self._client.publish(topic, json.dumps(payload), qos=1)

        logger.debug(
            "[proxy:%s] Sent command %s (corr=%s)",
            self._adapter_id, action, correlation_id,
        )

        # Wait for response.
        try:
            result: dict[str, Any] = future.result(timeout=timeout)
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(
                "[proxy:%s] Command %s timed out after %.1fs (corr=%s)",
                self._adapter_id, action, timeout, correlation_id,
            )
            raise TimeoutError(
                f"Adapter {self._adapter_id} did not respond to "
                f"{action} within {timeout}s"
            )
        finally:
            with self._pending_lock:
                self._pending.pop(correlation_id, None)

    # ------------------------------------------------------------------
    # MQTT callbacks
    # ------------------------------------------------------------------

    def _on_status(
        self, client: Any, userdata: Any, message: Any,
    ) -> None:
        """Handle adapter online/offline status (LWT)."""
        payload: str = message.payload.decode("utf-8", errors="replace")
        online: bool = payload.strip().lower() == "online"
        with self._lock:
            self._online = online
        logger.info(
            "[proxy:%s] Status: %s",
            self._adapter_id, "ONLINE" if online else "OFFLINE",
        )

    def _on_heartbeat(
        self, client: Any, userdata: Any, message: Any,
    ) -> None:
        """Handle adapter heartbeat — cache the latest data."""
        try:
            data: dict[str, Any] = json.loads(message.payload)
        except (json.JSONDecodeError, ValueError):
            return

        with self._lock:
            self._last_heartbeat = data
            self._last_heartbeat_ts = time.monotonic()
            # Heartbeat implies online even if we missed the LWT.
            self._online = True

    def _on_response(
        self, client: Any, userdata: Any, message: Any,
    ) -> None:
        """Handle command response — resolve the waiting Future."""
        # Extract correlation_id from topic.
        # Topic: glowup/adapter/{id}/response/{corr}
        parts: list[str] = message.topic.split("/")
        if len(parts) < 5:
            return
        correlation_id: str = parts[4]

        try:
            data: dict[str, Any] = json.loads(message.payload)
        except (json.JSONDecodeError, ValueError):
            data = {"status": "error", "error": "invalid response payload"}

        with self._pending_lock:
            future: Optional[concurrent.futures.Future] = (
                self._pending.get(correlation_id)
            )

        if future is not None and not future.done():
            future.set_result(data)
            logger.debug(
                "[proxy:%s] Response received (corr=%s): %s",
                self._adapter_id, correlation_id, data.get("status"),
            )
        else:
            logger.debug(
                "[proxy:%s] Orphan response (corr=%s) — no waiter",
                self._adapter_id, correlation_id,
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _is_stale(self) -> bool:
        """Check if the last heartbeat is too old.

        Must be called under self._lock.
        """
        if self._last_heartbeat_ts == 0.0:
            return True
        return (
            time.monotonic() - self._last_heartbeat_ts > HEARTBEAT_STALE_S
        )


# ---------------------------------------------------------------------------
# MatterProxyWrapper — adapter-compatible interface for scheduler/handlers
# ---------------------------------------------------------------------------

class MatterProxyWrapper:
    """Adapter-compatible wrapper translating method calls to proxy commands.

    The scheduler and handlers call ``power_on(name)``,
    ``get_device_names()``, etc.  This wrapper translates each call
    into a :meth:`AdapterProxy.send_command` invocation so existing
    code does not need to know about the proxy protocol.

    Args:
        proxy: The underlying :class:`AdapterProxy` for the Matter adapter.
    """

    def __init__(self, proxy: AdapterProxy) -> None:
        """Initialize with the Matter proxy."""
        self._proxy: AdapterProxy = proxy

    def get_device_names(self) -> list[str]:
        """Return list of configured Matter device names."""
        try:
            result: dict[str, Any] = self._proxy.send_command(
                "get_devices", {},
            )
            return result.get("devices", [])
        except TimeoutError:
            return []

    def power_on(self, device_name: str) -> bool:
        """Turn a Matter device on."""
        try:
            result: dict[str, Any] = self._proxy.send_command(
                "power_on", {"device_name": device_name},
            )
            return result.get("status") == "ok"
        except TimeoutError:
            return False

    def power_off(self, device_name: str) -> bool:
        """Turn a Matter device off."""
        try:
            result: dict[str, Any] = self._proxy.send_command(
                "power_off", {"device_name": device_name},
            )
            return result.get("status") == "ok"
        except TimeoutError:
            return False

    def toggle(self, device_name: str) -> bool:
        """Toggle a Matter device."""
        try:
            result: dict[str, Any] = self._proxy.send_command(
                "toggle", {"device_name": device_name},
            )
            return result.get("status") == "ok"
        except TimeoutError:
            return False

    def get_power_state(self, device_name: str) -> Optional[bool]:
        """Return cached power state for a device.

        Returns:
            ``True`` (on), ``False`` (off), or ``None`` if unknown.
        """
        try:
            result: dict[str, Any] = self._proxy.send_command(
                "get_power_state", {"device_name": device_name},
            )
            return result.get("power")
        except TimeoutError:
            return None

    def get_status(self) -> dict[str, Any]:
        """Return adapter status from cached heartbeat."""
        return self._proxy.get_status()

    def is_alive(self) -> bool:
        """Check if the Matter adapter process is alive."""
        return self._proxy.is_alive()

    @property
    def running(self) -> bool:
        """Whether the adapter process is alive."""
        return self._proxy.is_alive()
