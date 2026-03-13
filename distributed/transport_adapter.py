"""Transport adapter abstraction — MQTT and UDP signal transports.

Provides a unified interface for publishing and subscribing to signals
regardless of the underlying transport mechanism.  The SignalBus uses
these adapters to route signals through the appropriate channel:

* **MqttTransport** — JSON-serialized signals over MQTT topics.
  Best for low-rate derived signals (beat, BPM, frequency bands).
  Provides pub/sub fanout and reliable delivery via the broker.

* **UdpTransport** — Binary-framed signals over UDP sockets.
  Best for high-rate raw data (PCM audio, video frames, sensor arrays).
  Direct point-to-point or multicast, no broker overhead.

The transport adapter layer keeps transport concerns out of the SignalBus
API.  Effects call ``bus.read()`` / ``bus.write()`` without knowing
whether the signal traveled over MQTT or UDP.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional, Union

from .protocol import (
    DTYPE_FLOAT32, DTYPE_INT16_PCM, DTYPE_JSON,
    MSG_SIGNAL_DATA,
    pack_float32_array, unpack_float32_array,
    pack_int16_array, unpack_int16_array,
    SignalFrame,
)
from .udp_channel import UdpSender, UdpReceiver

logger: logging.Logger = logging.getLogger("glowup.distributed.transport")

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# Signal values match the SignalBus convention: scalar or float list.
SignalValue = Union[float, list[float]]

# Callback for received signals: (signal_name, value) → None.
SignalCallback = Callable[[str, SignalValue], None]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic prefix (matches media/__init__.py convention).
MQTT_SIGNAL_PREFIX: str = "glowup/signals/"

# MQTT QoS for signal publishing.
MQTT_QOS: int = 0


# ---------------------------------------------------------------------------
# TransportAdapter ABC
# ---------------------------------------------------------------------------

class TransportAdapter(ABC):
    """Abstract transport for sending and receiving signal data.

    Each adapter implementation handles one transport mechanism.
    The SignalBus delegates publish/subscribe to the appropriate
    adapter based on signal routing configuration.
    """

    @abstractmethod
    def publish(self, name: str, value: SignalValue,
                dtype: int = DTYPE_FLOAT32) -> None:
        """Publish a signal value to remote consumers.

        Args:
            name:  Signal name.
            value: Signal value (float or list[float]).
            dtype: Wire data type hint.
        """

    @abstractmethod
    def subscribe(self, name: str, callback: SignalCallback) -> None:
        """Register a callback for a remote signal.

        Args:
            name:     Signal name (or pattern for MQTT).
            callback: Called with ``(signal_name, value)`` on arrival.
        """

    @abstractmethod
    def unsubscribe(self, name: str) -> None:
        """Remove subscription for a signal.

        Args:
            name: Signal name (or pattern).
        """

    @abstractmethod
    def start(self) -> None:
        """Start the transport (connect, bind, etc.)."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the transport and release resources."""


# ---------------------------------------------------------------------------
# MqttTransport
# ---------------------------------------------------------------------------

class MqttTransport(TransportAdapter):
    """MQTT transport for low-rate derived signals.

    Wraps a paho-mqtt client to publish and subscribe to signals
    on ``glowup/signals/{name}`` topics.  Values are JSON-serialized.

    This adapter is intended for small, infrequent signals like
    frequency bands (8 floats at 15 Hz), beat pulses, BPM, etc.
    For raw audio/video streams, use :class:`UdpTransport`.

    Args:
        broker:   MQTT broker hostname or IP.
        port:     Broker port.
        username: Optional MQTT username.
        password: Optional MQTT password.
    """

    def __init__(self, broker: str = "localhost", port: int = 1883,
                 username: Optional[str] = None,
                 password: Optional[str] = None) -> None:
        """Initialize the MQTT transport.

        Args:
            broker:   Broker hostname or IP.
            port:     Broker port (default 1883).
            username: Optional authentication username.
            password: Optional authentication password.
        """
        self._broker: str = broker
        self._port: int = port
        self._username: Optional[str] = username
        self._password: Optional[str] = password
        self._client: Optional[Any] = None
        self._connected: bool = False
        self._callbacks: dict[str, list[SignalCallback]] = {}

    def publish(self, name: str, value: SignalValue,
                dtype: int = DTYPE_FLOAT32) -> None:
        """Publish a signal as JSON to the MQTT broker.

        Args:
            name:  Signal name.
            value: Float or list[float].
            dtype: Ignored for MQTT (always JSON).
        """
        if not self._client or not self._connected:
            return
        try:
            payload: str = json.dumps(value)
            self._client.publish(
                MQTT_SIGNAL_PREFIX + name, payload, qos=MQTT_QOS,
            )
        except Exception:
            pass  # Best-effort.

    def subscribe(self, name: str, callback: SignalCallback) -> None:
        """Subscribe to a signal topic on the broker.

        Args:
            name:     Signal name.
            callback: Called with ``(name, value)`` on message arrival.
        """
        if name not in self._callbacks:
            self._callbacks[name] = []
            # Subscribe to the specific topic.
            if self._client and self._connected:
                self._client.subscribe(
                    MQTT_SIGNAL_PREFIX + name, qos=MQTT_QOS,
                )
        self._callbacks[name].append(callback)

    def unsubscribe(self, name: str) -> None:
        """Unsubscribe from a signal topic.

        Args:
            name: Signal name.
        """
        self._callbacks.pop(name, None)
        if self._client and self._connected:
            try:
                self._client.unsubscribe(MQTT_SIGNAL_PREFIX + name)
            except Exception:
                pass

    def start(self) -> None:
        """Connect to the MQTT broker and start the network loop."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.error(
                "paho-mqtt not installed — MqttTransport unavailable. "
                "Install with: pip install paho-mqtt"
            )
            return

        client_id: str = f"glowup-transport-{int(time.time())}"
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )

        if self._username:
            self._client.username_pw_set(self._username, self._password)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        try:
            self._client.connect(self._broker, self._port)
            self._client.loop_start()
        except Exception as exc:
            logger.error(
                "MqttTransport connect to %s:%d failed: %s",
                self._broker, self._port, exc,
            )

    def stop(self) -> None:
        """Disconnect from the broker."""
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
            self._connected = False

    def _on_connect(self, client: Any, userdata: Any, flags: Any,
                    reason_code: Any, properties: Any = None) -> None:
        """Handle broker connection — resubscribe to all signals."""
        if reason_code == 0:
            self._connected = True
            # Resubscribe to all registered signal names.
            for name in self._callbacks:
                client.subscribe(
                    MQTT_SIGNAL_PREFIX + name, qos=MQTT_QOS,
                )
            logger.info(
                "MqttTransport connected to %s:%d",
                self._broker, self._port,
            )
        else:
            logger.error(
                "MqttTransport connect refused: %s", reason_code,
            )

    def _on_disconnect(self, client: Any, userdata: Any, flags: Any,
                       reason_code: Any, properties: Any = None) -> None:
        """Handle disconnect — mark as not connected."""
        self._connected = False
        if reason_code != 0:
            logger.warning(
                "MqttTransport disconnected unexpectedly (rc=%s)",
                reason_code,
            )

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Dispatch incoming MQTT messages to signal callbacks."""
        if not msg.topic.startswith(MQTT_SIGNAL_PREFIX):
            return
        signal_name: str = msg.topic[len(MQTT_SIGNAL_PREFIX):]
        try:
            value = json.loads(msg.payload.decode("utf-8"))
            # Normalize to SignalValue (float or list[float]).
            if isinstance(value, (int, float)):
                value = float(value)
            elif isinstance(value, list):
                value = [float(v) for v in value]
            else:
                return  # Unsupported type.
        except (json.JSONDecodeError, ValueError, TypeError):
            return

        # Dispatch to signal-specific callbacks.
        callbacks: list[SignalCallback] = self._callbacks.get(
            signal_name, [],
        )
        for cb in callbacks:
            try:
                cb(signal_name, value)
            except Exception as exc:
                logger.error(
                    "MqttTransport callback error for '%s': %s",
                    signal_name, exc,
                )


# ---------------------------------------------------------------------------
# UdpTransport
# ---------------------------------------------------------------------------

class UdpTransport(TransportAdapter):
    """UDP transport for high-rate raw signal data.

    Wraps :class:`UdpSender` and :class:`UdpReceiver` to provide the
    same publish/subscribe interface as :class:`MqttTransport`, but
    using binary-framed UDP for minimal overhead.

    The orchestrator configures target addresses and listen ports
    via the work assignment.  The transport sends to configured
    targets and receives on the configured port.

    Args:
        listen_port:     UDP port to receive on.
        targets:         List of ``(ip, port)`` destinations for sends.
        multicast_group: Optional multicast group to join for receives.
    """

    def __init__(self, listen_port: int = 0,
                 targets: Optional[list[tuple[str, int]]] = None,
                 multicast_group: Optional[str] = None) -> None:
        """Initialize the UDP transport.

        Args:
            listen_port:     Port to bind for receiving (0 = send-only).
            targets:         Destination addresses for publishing.
            multicast_group: Optional multicast group to join.
        """
        self._listen_port: int = listen_port
        self._targets: list[tuple[str, int]] = list(targets) if targets else []
        self._multicast_group: Optional[str] = multicast_group
        self._sender: Optional[UdpSender] = None
        self._receiver: Optional[UdpReceiver] = None
        self._callbacks: dict[str, list[SignalCallback]] = {}

    def publish(self, name: str, value: SignalValue,
                dtype: int = DTYPE_FLOAT32) -> None:
        """Serialize and send a signal value via UDP.

        Args:
            name:  Signal name.
            value: Float or list[float].
            dtype: Wire data type (determines serialization).
        """
        if not self._sender:
            return

        # Serialize based on dtype.
        if dtype == DTYPE_JSON:
            payload: bytes = json.dumps(value).encode("utf-8")
        elif isinstance(value, list):
            payload = pack_float32_array(value)
            dtype = DTYPE_FLOAT32
        elif isinstance(value, (int, float)):
            payload = pack_float32_array([float(value)])
            dtype = DTYPE_FLOAT32
        else:
            return

        self._sender.send(name, payload, dtype)

    def publish_raw(self, name: str, payload: bytes,
                    dtype: int = DTYPE_INT16_PCM) -> None:
        """Send pre-serialized raw bytes via UDP.

        Use this for raw PCM audio or video frames that are already
        in the correct wire format — avoids unnecessary re-encoding.

        Args:
            name:    Signal name.
            payload: Pre-serialized payload bytes.
            dtype:   Data type indicator.
        """
        if self._sender:
            self._sender.send(name, payload, dtype)

    def subscribe(self, name: str, callback: SignalCallback) -> None:
        """Register a callback for a signal arriving via UDP.

        Args:
            name:     Signal name to filter for.
            callback: Called with ``(name, value)`` on frame arrival.
        """
        if name not in self._callbacks:
            self._callbacks[name] = []
        self._callbacks[name].append(callback)

    def unsubscribe(self, name: str) -> None:
        """Remove subscription for a signal.

        Args:
            name: Signal name.
        """
        self._callbacks.pop(name, None)

    def add_target(self, ip: str, port: int) -> None:
        """Add a destination address for publishing.

        Args:
            ip:   Target IPv4 address.
            port: Target UDP port.
        """
        if self._sender:
            self._sender.add_target(ip, port)

    def remove_target(self, ip: str, port: int) -> None:
        """Remove a destination address.

        Args:
            ip:   Target IPv4 address.
            port: Target UDP port.
        """
        if self._sender:
            self._sender.remove_target(ip, port)

    def start(self) -> None:
        """Create sender/receiver and start listening."""
        # Always create a sender.
        self._sender = UdpSender(targets=self._targets)

        # Only create a receiver if a listen port is configured.
        if self._listen_port > 0:
            self._receiver = UdpReceiver(port=self._listen_port)
            self._receiver.add_callback(self._on_frame)
            self._receiver.start(multicast_group=self._multicast_group)

    def stop(self) -> None:
        """Stop receiver and close sender."""
        if self._receiver:
            self._receiver.stop()
            self._receiver = None
        if self._sender:
            self._sender.close()
            self._sender = None

    def _on_frame(self, frame: SignalFrame,
                  addr: tuple[str, int]) -> None:
        """Decode incoming UDP frame and dispatch to callbacks.

        Args:
            frame: Decoded signal frame.
            addr:  Sender ``(ip, port)`` tuple.
        """
        # Decode payload to SignalValue.
        value: SignalValue
        if frame.dtype == DTYPE_FLOAT32:
            floats: list[float] = unpack_float32_array(frame.payload)
            if len(floats) == 1:
                value = floats[0]
            else:
                value = floats
        elif frame.dtype == DTYPE_JSON:
            try:
                raw = json.loads(frame.payload.decode("utf-8"))
                if isinstance(raw, (int, float)):
                    value = float(raw)
                elif isinstance(raw, list):
                    value = [float(v) for v in raw]
                else:
                    return
            except (json.JSONDecodeError, ValueError):
                return
        else:
            # For raw dtypes (INT16_PCM, RGB24), pass as-is won't fit
            # SignalValue.  Dispatch the frame directly to callbacks
            # that accept raw data — they can check frame.dtype.
            # For the standard SignalValue path, skip.
            return

        # Dispatch to signal-specific callbacks.
        callbacks: list[SignalCallback] = self._callbacks.get(
            frame.name, [],
        )
        # Also dispatch to wildcard subscribers (subscribed to "").
        callbacks = callbacks + self._callbacks.get("", [])

        for cb in callbacks:
            try:
                cb(frame.name, value)
            except Exception as exc:
                logger.error(
                    "UdpTransport callback error for '%s': %s",
                    frame.name, exc,
                )
