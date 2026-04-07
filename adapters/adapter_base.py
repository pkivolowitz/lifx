"""Adapter base classes — lifecycle management for all GlowUp adapters.

Three transport-specific base classes handle the duplicated lifecycle
boilerplate across the adapter family:

- ``MqttAdapterBase`` — MQTT subscriber adapters (BLE, Zigbee)
- ``PollingAdapterBase`` — synchronous polling thread (Printer)
- ``AsyncPollingAdapterBase`` — asyncio event loop + reconnect (Vivint, NVR)

All three inherit from ``AdapterBase``, which provides the ``_running``
flag and ``running`` property.

Usage::

    class MyMqttAdapter(MqttAdapterBase):
        def __init__(self, bus, broker="localhost"):
            super().__init__(
                broker=broker, port=1883,
                subscribe_prefix="my/topic",
                client_id_prefix="my-adapter",
            )
            self._bus = bus

        def _handle_message(self, topic, payload):
            # Process incoming MQTT message
            ...

    class MyPoller(PollingAdapterBase):
        def __init__(self, interval=60.0):
            super().__init__(
                poll_interval=interval,
                thread_name="my-poller",
            )

        def _do_poll(self):
            # One poll cycle
            ...

    class MyAsyncAdapter(AsyncPollingAdapterBase):
        def __init__(self):
            super().__init__(thread_name="my-async")

        async def _connect(self):
            ...

        async def _disconnect(self):
            ...

        async def _run_cycle(self):
            while self._running:
                await asyncio.sleep(1)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import asyncio
import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.adapter_base")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Thread join timeout (seconds) — generous to allow async cleanup.
THREAD_JOIN_TIMEOUT: float = 10.0

# Maximum sleep chunk for interruptible polling (seconds).
# Smaller values make stop() more responsive but waste CPU.
SLEEP_CHUNK: float = 5.0

# MQTT keepalive interval (seconds).  Paho default is 60s; tighter
# interval detects dead TCP connections sooner via missing PINGRESPs.
MQTT_KEEPALIVE: int = 30

# Watchdog silence threshold (seconds).  If no MQTT message arrives
# within this window after the first message, the watchdog forces a
# reconnect.  Z2M publishes every ~10s; 120s allows for transient
# gaps without false alarms.
WATCHDOG_SILENCE_THRESHOLD: float = 120.0

# Watchdog poll interval (seconds).  How often the watchdog thread
# checks for silence.  Short enough to detect promptly, long enough
# to avoid wasting CPU.
WATCHDOG_POLL_INTERVAL: float = 15.0

# ---------------------------------------------------------------------------
# Optional paho-mqtt dependency
# ---------------------------------------------------------------------------

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
    # paho 2.x uses CallbackAPIVersion; 1.x does not.
    _PAHO_V2: bool = hasattr(mqtt, "CallbackAPIVersion")
except ImportError:
    _HAS_PAHO = False
    _PAHO_V2 = False
    mqtt = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# AdapterBase
# ---------------------------------------------------------------------------

class AdapterBase(ABC):
    """Abstract base for all adapters — enforces start/stop lifecycle.

    Provides:
        - ``_running`` flag (managed by subclass start/stop)
        - ``running`` read-only property

    Every concrete adapter in GlowUp inherits from one of the three
    transport-specific subclasses below, which in turn inherit from
    this class.
    """

    def __init__(self) -> None:
        """Initialize the running flag."""
        self._running: bool = False

    @property
    def running(self) -> bool:
        """Whether the adapter is currently running."""
        return self._running

    @abstractmethod
    def start(self) -> None:
        """Start the adapter — begin processing."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the adapter and release resources."""


# ---------------------------------------------------------------------------
# MqttAdapterBase
# ---------------------------------------------------------------------------

class MqttAdapterBase(AdapterBase):
    """Base class for adapters that subscribe to MQTT topics.

    Handles paho client creation (v1 and v2), ``connect_async``, network
    loop management, ``on_connect`` subscription, disconnect detection,
    silence watchdog, and ``stop``/``disconnect``.

    Disconnect detection:  Paho's ``on_disconnect`` callback is wired so
    that TCP drops are logged at WARNING.  This surfaces the event that
    was previously invisible.

    Silence watchdog:  A background thread monitors the time since the
    last received message.  If no message arrives within
    ``WATCHDOG_SILENCE_THRESHOLD`` seconds (after at least one message
    has been seen), the watchdog forces a disconnect+reconnect.  This
    catches the half-open TCP socket case where paho believes it is
    connected but no data is flowing.

    Subclasses must implement ``_handle_message(topic, payload)``.

    Args:
        broker:           MQTT broker hostname or IP.
        port:             MQTT broker port.
        subscribe_prefix: Topic prefix to subscribe to (``{prefix}/#``).
        client_id_prefix: Prefix for the MQTT client ID (timestamp appended).
    """

    def __init__(
        self,
        broker: str,
        port: int,
        subscribe_prefix: str,
        client_id_prefix: str,
    ) -> None:
        """Initialize MQTT adapter state.

        Args:
            broker:           MQTT broker hostname or IP.
            port:             MQTT broker port.
            subscribe_prefix: Topic prefix to subscribe to.
            client_id_prefix: Prefix for the MQTT client ID.
        """
        super().__init__()
        self._broker: str = broker
        self._port: int = port
        self._subscribe_prefix: str = subscribe_prefix
        self._client_id_prefix: str = client_id_prefix
        self._client: Any = None
        # Connection state tracking.
        self._connected: bool = False
        # Watchdog timestamp — monotonic clock, None until first message.
        self._last_message_time: Optional[float] = None
        self._watchdog_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the MQTT subscriber.

        Creates a paho MQTT client, wires callbacks (including
        ``on_disconnect``), starts the network loop, and launches
        the silence watchdog thread.  No-op if ``paho-mqtt`` is not
        installed.
        """
        if not _HAS_PAHO:
            logger.warning(
                "%s: paho-mqtt not installed — adapter disabled",
                self._client_id_prefix,
            )
            return

        self._running = True
        self._connected = False
        self._last_message_time = None
        client_id: str = f"{self._client_id_prefix}-{int(time.time())}"

        if _PAHO_V2:
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
            )
        else:
            self._client = mqtt.Client(client_id=client_id)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message_dispatch
        self._client.connect_async(
            self._broker, self._port, keepalive=MQTT_KEEPALIVE,
        )
        self._client.loop_start()

        # Silence watchdog — detects half-open sockets that paho
        # cannot see.  Runs as a daemon thread so it dies with the
        # process if stop() is never called.
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name=f"{self._client_id_prefix}-watchdog",
        )
        self._watchdog_thread.start()

        self._on_started()

    def stop(self) -> None:
        """Stop the MQTT subscriber, watchdog, and disconnect."""
        self._running = False
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
        self._connected = False
        # Watchdog thread is a daemon and checks _running; it will
        # exit on the next poll cycle.
        self._on_stopped()

    # --- MQTT callbacks ----------------------------------------------------

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        rc: int,
        properties: Any = None,
    ) -> None:
        """Subscribe to the configured topic on successful connection.

        Compatible with both paho v1 (4 args) and v2 (5 args) callback
        signatures via the ``properties`` default.

        Args:
            client:     The paho MQTT client.
            userdata:   User data (unused).
            flags:      Connection flags.
            rc:         Return code (0 = success).
            properties: MQTT v5 properties (unused).
        """
        if rc != 0:
            logger.warning(
                "%s: MQTT connect failed: rc=%d",
                self._client_id_prefix, rc,
            )
            self._connected = False
            return
        self._connected = True
        topic: str = f"{self._subscribe_prefix}/#"
        client.subscribe(topic)
        logger.info(
            "%s: subscribed to %s",
            self._client_id_prefix, topic,
        )

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        flags_or_rc: Any,
        rc: Any = None,
        properties: Any = None,
    ) -> None:
        """Log disconnection and update connection state.

        Compatible with both paho v1 and v2 callback signatures:
            - v1: ``on_disconnect(client, userdata, rc)``
            - v2: ``on_disconnect(client, userdata, flags, rc, properties)``

        Paho calls this when the TCP connection drops.  If rc != 0
        the disconnect was unexpected and paho will auto-reconnect
        (if loop_start is running).  Either way, we log at WARNING
        so the event is visible in journalctl.

        Args:
            client:       The paho MQTT client.
            userdata:     User data (unused).
            flags_or_rc:  Disconnect flags (v2) or return code (v1).
            rc:           Return code (v2 only; None for v1).
            properties:   MQTT v5 properties (unused).
        """
        # Normalize rc across paho v1 (3 args) and v2 (5 args).
        actual_rc: Any = flags_or_rc if rc is None else rc

        self._connected = False
        if actual_rc == 0:
            logger.info(
                "%s: MQTT disconnected (clean)",
                self._client_id_prefix,
            )
        else:
            logger.warning(
                "%s: MQTT disconnected unexpectedly (rc=%s) "
                "— paho will attempt reconnect",
                self._client_id_prefix, actual_rc,
            )

    def _on_message_dispatch(
        self, client: Any, userdata: Any, msg: Any,
    ) -> None:
        """Dispatch incoming message to subclass handler.

        Updates the watchdog timestamp on every message.  Catches
        exceptions from the subclass handler to prevent crashing
        paho's internal network thread, but logs at WARNING so
        persistent errors are visible.

        Args:
            client:   The paho MQTT client.
            userdata: User data (unused).
            msg:      The MQTT message (has ``.topic`` and ``.payload``).
        """
        self._last_message_time = time.monotonic()
        try:
            self._handle_message(msg.topic, msg.payload)
        except Exception as exc:
            logger.warning(
                "%s: message handler error on %s: %s",
                self._client_id_prefix, msg.topic, exc,
            )

    # --- Silence watchdog --------------------------------------------------

    def _watchdog_loop(self) -> None:
        """Monitor for message silence and force reconnect if detected.

        Runs on a daemon thread.  After the first message is received,
        checks every ``WATCHDOG_POLL_INTERVAL`` seconds whether
        ``WATCHDOG_SILENCE_THRESHOLD`` has elapsed without a message.

        If silence is detected and we believe we are still connected,
        the connection is stale (half-open socket).  Force a
        disconnect so paho's auto-reconnect kicks in.
        """
        while self._running:
            # Interruptible sleep — exit promptly on stop().
            remaining: float = WATCHDOG_POLL_INTERVAL
            while remaining > 0 and self._running:
                chunk: float = min(remaining, 1.0)
                time.sleep(chunk)
                remaining -= chunk

            if not self._running:
                break

            # Only check after we've received at least one message.
            last: Optional[float] = self._last_message_time
            if last is None:
                continue

            silence: float = time.monotonic() - last
            if silence >= WATCHDOG_SILENCE_THRESHOLD and self._connected:
                logger.warning(
                    "%s: no messages for %.0fs — forcing reconnect "
                    "(probable half-open socket)",
                    self._client_id_prefix, silence,
                )
                # Reset timestamp so we don't spam reconnects every
                # poll cycle.  Next message will set it again.
                self._last_message_time = None
                self._connected = False
                try:
                    self._client.disconnect()
                except Exception:
                    pass
                # Paho's loop_start thread will detect the disconnect
                # and begin reconnecting automatically.
                try:
                    self._client.reconnect()
                except Exception as exc:
                    logger.warning(
                        "%s: reconnect failed: %s — paho will retry",
                        self._client_id_prefix, exc,
                    )

    @abstractmethod
    def _handle_message(self, topic: str, payload: bytes) -> None:
        """Process an incoming MQTT message.

        Called from paho's network thread — implementations must be
        thread-safe.

        Args:
            topic:   The MQTT topic string.
            payload: The raw message payload bytes.
        """

    # --- Hooks -------------------------------------------------------------

    def _on_started(self) -> None:
        """Hook called after the MQTT client is started.

        Override for custom startup logging.  Default logs at INFO.
        """
        logger.info(
            "%s: started — subscribing to %s/#",
            self._client_id_prefix, self._subscribe_prefix,
        )

    def _on_stopped(self) -> None:
        """Hook called after the MQTT client is stopped.

        Override for custom shutdown logging.  Default logs at INFO.
        """
        logger.info("%s: stopped", self._client_id_prefix)


# ---------------------------------------------------------------------------
# PollingAdapterBase
# ---------------------------------------------------------------------------

class PollingAdapterBase(AdapterBase):
    """Base class for adapters that poll on a daemon thread.

    Provides a background thread with interruptible sleep between polls.
    Calls ``_do_poll()`` immediately on start, then every
    ``poll_interval`` seconds.

    Subclasses must implement ``_do_poll()``.  Override
    ``_check_prerequisites()`` to gate startup on config or dependencies.

    Args:
        poll_interval: Seconds between poll cycles.
        thread_name:   Name for the daemon thread.
    """

    def __init__(
        self,
        poll_interval: float,
        thread_name: str,
    ) -> None:
        """Initialize polling adapter state.

        Args:
            poll_interval: Seconds between poll cycles.
            thread_name:   Name for the daemon thread.
        """
        super().__init__()
        self._poll_interval: float = poll_interval
        self._thread_name: str = thread_name
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the polling thread.

        Calls ``_check_prerequisites()`` first — if that returns
        ``False``, the adapter does not start.
        """
        if not self._check_prerequisites():
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name=self._thread_name,
        )
        self._thread.start()
        self._on_started()

    def stop(self) -> None:
        """Stop the polling thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=THREAD_JOIN_TIMEOUT)
        self._on_stopped()

    def _poll_loop(self) -> None:
        """Run polls with interruptible sleep between them.

        First poll is immediate; subsequent polls wait ``_poll_interval``
        seconds.  Sleep is broken into small chunks so ``stop()`` is
        responsive (worst-case latency = ``SLEEP_CHUNK`` seconds).
        """
        self._do_poll()
        while self._running:
            # Interruptible sleep — check _running every chunk.
            remaining: float = self._poll_interval
            while remaining > 0 and self._running:
                chunk: float = min(remaining, SLEEP_CHUNK)
                time.sleep(chunk)
                remaining -= chunk
            if self._running:
                self._do_poll()

    def _check_prerequisites(self) -> bool:
        """Check whether the adapter can start.

        Override to validate configuration or dependencies.  Return
        ``False`` to prevent startup — the adapter logs why and stays
        stopped.

        Returns:
            ``True`` if startup should proceed (default).
        """
        return True

    @abstractmethod
    def _do_poll(self) -> None:
        """Execute a single poll cycle.

        Called on the polling thread — must be thread-safe.  Exceptions
        are NOT caught by the base; subclasses should handle their own
        errors to keep the loop running.
        """

    # --- Hooks -------------------------------------------------------------

    def _on_started(self) -> None:
        """Hook called after the polling thread starts."""
        logger.info(
            "%s: started (poll every %.0fs)",
            self._thread_name, self._poll_interval,
        )

    def _on_stopped(self) -> None:
        """Hook called after the polling thread stops."""
        logger.info("%s: stopped", self._thread_name)


# ---------------------------------------------------------------------------
# AsyncPollingAdapterBase
# ---------------------------------------------------------------------------

class AsyncPollingAdapterBase(AdapterBase):
    """Base class for adapters with an asyncio event loop and reconnect.

    Runs a background daemon thread hosting an asyncio event loop.
    The loop calls ``_connect()``, then ``_run_cycle()`` (the main work
    loop).  On failure, reconnects with exponential backoff capped at
    ``max_reconnect_delay``.

    Subclasses must implement ``_connect()``, ``_disconnect()``, and
    ``_run_cycle()``.  Override ``_check_prerequisites()`` to gate
    startup on config or dependencies.

    Args:
        thread_name:         Name for the daemon thread.
        reconnect_delay:     Initial reconnect delay (seconds).
        max_reconnect_delay: Maximum reconnect delay (seconds).
    """

    def __init__(
        self,
        thread_name: str,
        reconnect_delay: float = 30.0,
        max_reconnect_delay: float = 300.0,
    ) -> None:
        """Initialize async polling adapter state.

        Args:
            thread_name:         Name for the daemon thread.
            reconnect_delay:     Initial reconnect delay (seconds).
            max_reconnect_delay: Maximum reconnect delay (seconds).
        """
        super().__init__()
        self._thread_name: str = thread_name
        self._initial_reconnect_delay: float = reconnect_delay
        self._max_reconnect_delay: float = max_reconnect_delay
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self) -> None:
        """Start the background thread and event loop.

        Calls ``_check_prerequisites()`` first — if that returns
        ``False``, the adapter does not start.
        """
        if not self._check_prerequisites():
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=self._thread_name,
        )
        self._thread.start()
        self._on_started()

    def stop(self) -> None:
        """Stop the event loop and join the background thread."""
        self._running = False
        if self._loop:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._disconnect(), self._loop,
                )
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=THREAD_JOIN_TIMEOUT)
        self._on_stopped()

    def _hb(self, activity: str) -> None:
        """Record a heartbeat and honor single-step gate.

        No-op unless GLOWUP_TRACE=1 is set in the environment.
        When single-step debugging is active, blocks until the
        inspector releases the gate.

        Args:
            activity: Short description of current operation.
        """
        try:
            from server import TRACING_ENABLED, _thread_heartbeats, _gate
            if TRACING_ENABLED:
                _thread_heartbeats[self._thread_name] = (
                    activity, time.monotonic(),
                )
                _gate(self._thread_name)
        except ImportError:
            pass

    def _run_loop(self) -> None:
        """Background thread entry point — create and run the event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main_loop())
        except Exception as exc:
            logger.error(
                "%s: event loop crashed: %s", self._thread_name, exc,
            )
        finally:
            self._loop.close()

    async def _main_loop(self) -> None:
        """Connect, run cycle, reconnect on failure with backoff.

        Backoff doubles on each failure and resets after a successful
        connection.  Capped at ``_max_reconnect_delay``.
        """
        delay: float = self._initial_reconnect_delay
        while self._running:
            try:
                self._hb("connecting")
                await self._connect()
                # Reset backoff after successful connection.
                delay = self._initial_reconnect_delay
                self._hb("running")
                await self._run_cycle()
            except Exception as exc:
                if not self._running:
                    break
                self._hb(f"error: {type(exc).__name__}")
                logger.error(
                    "%s: connection error: %s — retrying in %.0fs",
                    self._thread_name, exc, delay,
                )
            finally:
                # Always release the session so the remote service
                # does not accumulate stale connections (e.g., NVR
                # "max session" error from leaked logins).
                self._hb("disconnecting")
                try:
                    await self._disconnect()
                except Exception:
                    pass
            if self._running:
                self._hb(f"backoff {delay:.0f}s")
                await asyncio.sleep(delay)
                # Exponential backoff, capped.
                delay = min(delay * 2.0, self._max_reconnect_delay)

    def _check_prerequisites(self) -> bool:
        """Check whether the adapter can start.

        Override to validate configuration or dependencies.  Return
        ``False`` to prevent startup.

        Returns:
            ``True`` if startup should proceed (default).
        """
        return True

    @abstractmethod
    async def _connect(self) -> None:
        """Connect to the external service."""

    @abstractmethod
    async def _disconnect(self) -> None:
        """Disconnect from the external service."""

    @abstractmethod
    async def _run_cycle(self) -> None:
        """Main work loop — runs after successful connection.

        Should loop internally (checking ``self._running``) until the
        connection drops or the adapter is stopped.
        """

    # --- Hooks -------------------------------------------------------------

    def _on_started(self) -> None:
        """Hook called after the thread starts."""
        logger.info("%s: started", self._thread_name)

    def _on_stopped(self) -> None:
        """Hook called after the thread stops."""
        logger.info("%s: stopped", self._thread_name)
