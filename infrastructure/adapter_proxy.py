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


# ---------------------------------------------------------------------------
# KeepaliveProxy — drop-in replacement for BulbKeepAlive
# ---------------------------------------------------------------------------

class KeepaliveProxy:
    """MQTT-backed proxy that presents the same interface as BulbKeepAlive.

    Subscribes to the keepalive process's MQTT topics and maintains
    local copies of the IP-to-MAC map and power states.  Handlers,
    device_manager, and registry code call ``known_bulbs``,
    ``known_bulbs_by_mac``, ``ip_for_mac()``, and ``is_alive()``
    exactly as they did with the in-process BulbKeepAlive.

    Args:
        mqtt_client: Connected paho MQTT client (the server's proc_mqtt).
    """

    # MQTT topics (must match keepalive_process.py).
    _TOPIC_DISCOVERED: str = "glowup/adapter/keepalive/discovered"
    _TOPIC_NEW_BULB: str = "glowup/adapter/keepalive/event/new_bulb"
    _TOPIC_STATUS: str = "glowup/adapter/keepalive/status"
    _TOPIC_HEARTBEAT: str = "glowup/adapter/keepalive/heartbeat"

    def __init__(self, mqtt_client: Any) -> None:
        """Initialize and subscribe to keepalive topics."""
        self._client: Any = mqtt_client
        self._lock: threading.Lock = threading.Lock()

        # IP → MAC map, populated from retained discovery message.
        self._known: dict[str, str] = {}

        # Adapter health state.
        self._online: bool = False
        self._last_heartbeat_ts: float = 0.0
        self._last_heartbeat: Optional[dict[str, Any]] = None
        self._initial_scan_done: threading.Event = threading.Event()

        # Callback for new bulb events — server wires this up
        # the same way it wired keepalive._on_new_bulb.
        self._on_new_bulb: Optional[Any] = None

        # Callback for whole-map updates from the keepalive process.
        # Fires every time the retained IP→MAC map is received, which
        # happens once at startup (from the retained payload) and then
        # whenever the keepalive republishes after a scan cycle. The
        # server uses this to reconcile the DeviceManager against
        # devices that were already known to keepalive before the
        # server started — the new-bulb hook above does not cover
        # that case because no individual new-bulb events are fired
        # for already-known bulbs at server startup.
        self._on_device_map: Optional[Any] = None

        # Callback for power query results — server wires this
        # the same way it wired keepalive._on_power_query.
        self._on_power_query: Optional[Any] = None

        # Subscribe to keepalive topics.
        mqtt_client.subscribe(self._TOPIC_DISCOVERED, qos=1)
        mqtt_client.subscribe(self._TOPIC_NEW_BULB, qos=1)
        mqtt_client.subscribe(self._TOPIC_STATUS, qos=1)
        mqtt_client.subscribe(self._TOPIC_HEARTBEAT, qos=0)

        mqtt_client.message_callback_add(
            self._TOPIC_DISCOVERED, self._on_discovered,
        )
        mqtt_client.message_callback_add(
            self._TOPIC_NEW_BULB, self._on_new_bulb_msg,
        )
        mqtt_client.message_callback_add(
            self._TOPIC_STATUS, self._on_status,
        )
        mqtt_client.message_callback_add(
            self._TOPIC_HEARTBEAT, self._on_heartbeat,
        )

        logger.info("[keepalive-proxy] Subscribed to keepalive topics")

    # ------------------------------------------------------------------
    # BulbKeepAlive-compatible interface
    # ------------------------------------------------------------------

    @property
    def known_bulbs(self) -> dict[str, str]:
        """Return snapshot of {IP: MAC} for all discovered bulbs."""
        with self._lock:
            return dict(self._known)

    @property
    def known_bulbs_by_mac(self) -> dict[str, str]:
        """Return snapshot of {MAC: IP} — reverse of known_bulbs."""
        with self._lock:
            return {mac: ip for ip, mac in self._known.items()}

    def ip_for_mac(self, mac: str) -> Optional[str]:
        """Return current IP for a MAC address, or None if offline.

        Args:
            mac: Lowercase colon-separated MAC.
        """
        mac_lower: str = mac.lower()
        with self._lock:
            for ip, known_mac in self._known.items():
                if known_mac == mac_lower:
                    return ip
        return None

    def is_alive(self) -> bool:
        """Check if the keepalive process is alive."""
        with self._lock:
            if not self._online:
                return False
            if self._last_heartbeat_ts == 0.0:
                return False
            return (
                time.monotonic() - self._last_heartbeat_ts
                < HEARTBEAT_STALE_S
            )

    def wait_initial_scan(self, timeout: float = 30.0) -> bool:
        """Block until the keepalive process reports initial scan done.

        If the retained heartbeat already has ``initial_scan_done: true``,
        returns immediately.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            True if scan completed, False on timeout.
        """
        return self._initial_scan_done.wait(timeout=timeout)

    # Stubs for compatibility — the proxy doesn't run threads.
    def start(self) -> None:
        """No-op — the keepalive process is managed by systemd."""

    def stop(self) -> None:
        """No-op — the keepalive process is managed by systemd."""

    def join(self, timeout: float = 3.0) -> None:
        """No-op — no thread to join."""

    # ------------------------------------------------------------------
    # MQTT callbacks
    # ------------------------------------------------------------------

    def _on_discovered(
        self, client: Any, userdata: Any, message: Any,
    ) -> None:
        """Handle retained IP→MAC map from the keepalive process.

        Fires ``self._on_device_map`` after the snapshot is stored so
        subscribers can reconcile state that depends on the full map
        (e.g. the DeviceManager's unresolved-entry backlog).
        """
        try:
            data: dict[str, str] = json.loads(message.payload)
        except (json.JSONDecodeError, ValueError):
            return
        with self._lock:
            self._known = data
        logger.info(
            "[keepalive-proxy] Received device map: %d bulb(s)",
            len(data),
        )
        cb = self._on_device_map
        if cb is not None:
            try:
                cb(dict(data))
            except Exception as exc:
                logger.warning(
                    "[keepalive-proxy] on_device_map callback failed: %s",
                    exc,
                )

    def _on_new_bulb_msg(
        self, client: Any, userdata: Any, message: Any,
    ) -> None:
        """Handle new bulb discovery event."""
        try:
            data: dict[str, str] = json.loads(message.payload)
        except (json.JSONDecodeError, ValueError):
            return
        ip: str = data.get("ip", "")
        mac: str = data.get("mac", "")
        if ip and mac:
            with self._lock:
                self._known[ip] = mac
            # Fire the callback — same as BulbKeepAlive._on_new_bulb.
            cb = self._on_new_bulb
            if cb is not None:
                try:
                    cb(ip, mac)
                except Exception as exc:
                    logger.warning(
                        "[keepalive-proxy] on_new_bulb callback failed: %s",
                        exc,
                    )

    def _on_status(
        self, client: Any, userdata: Any, message: Any,
    ) -> None:
        """Handle keepalive process online/offline (LWT)."""
        payload: str = message.payload.decode("utf-8", errors="replace")
        online: bool = payload.strip().lower() == "online"
        with self._lock:
            self._online = online
        logger.info(
            "[keepalive-proxy] Status: %s",
            "ONLINE" if online else "OFFLINE",
        )

    def _on_heartbeat(
        self, client: Any, userdata: Any, message: Any,
    ) -> None:
        """Handle keepalive heartbeat — check for initial_scan_done."""
        try:
            data: dict[str, Any] = json.loads(message.payload)
        except (json.JSONDecodeError, ValueError):
            return
        with self._lock:
            self._last_heartbeat = data
            self._last_heartbeat_ts = time.monotonic()
            self._online = True
        # Check if initial scan is done.
        detail: dict[str, Any] = data.get("detail", {})
        if detail.get("initial_scan_done", False):
            self._initial_scan_done.set()
