"""Resilient MQTT subscriber + publisher client.

Encapsulates the MQTT lifecycle that was originally implemented inside
``adapters.adapter_base.MqttAdapterBase``: client creation, on-connect
subscription restoration, on-disconnect logging, a silence watchdog
that rebuilds the client on half-open TCP, and unique ``client_id``
per rebuild to avoid the broker-2 mosquitto zombie-session race.

Extracted to break the implicit tie between "is an Adapter" and
"wants a resilient MQTT subscription."  The voice coordinator and
voice satellite are not adapters, but they have the same subscriber
lifecycle needs — silent TCP half-open killed the coordinator
2026-04-18 because it rolled its own connect/loop_start pattern with
no on_disconnect and no watchdog.  See the inline comments on
``_recover_from_silence`` for the full broker-2 incident (commit
7679713, 2026-04-06) that shaped the rebuild-with-fresh-client_id
strategy.

Designed for composition, not inheritance.  Hold one instance per
MQTT connection, pass it the fixed list of topics to subscribe
on every (re)connect, and an ``on_message`` callback.  Call
``publish`` for outbound traffic — the helper reads the current
client atomically so a concurrent watchdog rebuild cannot produce
a use-after-free or a publish-on-dead-socket.

Background on the silent-death bug this prevents:
  Paho's internal ``loop_start`` thread reads/writes the TCP socket
  and drives keepalive PINGREQs.  When the peer's TCP stack half-
  closes without sending FIN (NAT middlebox timeout, broker host
  reboot with lingering socket state, Wi-Fi AP drop without proper
  teardown), writes appear to succeed at the kernel buffer level
  but no responses arrive.  Paho's keepalive SHOULD detect this
  via missing PINGRESP within 1.5 × keepalive, but under specific
  conditions observed on macOS + mosquitto, paho enters a state
  where it neither fires ``on_disconnect`` nor attempts reconnect.
  The watchdog here observes *application-level silence* instead
  of relying on paho's keepalive — if no message is received for
  ``silence_threshold`` seconds while we believe we are connected,
  we tear down and rebuild the client entirely.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "1.0"

import logging
import threading
import time
from typing import Any, Callable, Optional, Sequence

logger: logging.Logger = logging.getLogger("glowup.mqtt_resilient")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT keepalive interval (seconds).  Paho default is 60s; the tighter
# 30s interval detects dead TCP connections sooner via missing PINGRESPs.
# This is the paho-level keepalive — the silence watchdog below is a
# separate, application-level belt that catches the paho-can-not-see-it
# half-open case.
MQTT_KEEPALIVE: int = 30

# Watchdog silence threshold (seconds).  If no message arrives within
# this window after the first message has been observed, force a
# reconnect.  120s allows for protocols that publish every 60s or so
# (heartbeats, Z2M) without false alarms.
WATCHDOG_SILENCE_THRESHOLD: float = 120.0

# Watchdog poll interval (seconds).  How often the watchdog thread
# wakes up to check for silence.  Short enough to detect promptly,
# long enough that the thread is not a CPU concern.
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
# MqttResilientClient
# ---------------------------------------------------------------------------

class MqttResilientClient:
    """Resilient paho-mqtt wrapper with auto-resubscribe + silence watchdog.

    Lifecycle:
        - ``start()`` creates the paho client, wires callbacks, begins
          the paho network loop, and launches the silence watchdog
          thread.  Non-blocking: initial connect is asynchronous, so
          a temporarily-unreachable broker does not prevent the
          owning daemon from finishing its own startup.
        - On every (re)connect the configured ``subscriptions`` are
          re-applied.  Subscribing only once at startup causes silent
          deafness after any broker blip (mbclock, 2026-04-16).
        - The watchdog monitors the last-message-received timestamp.
          After ``silence_threshold`` seconds of silence while
          ``is_connected`` is ``True``, the watchdog tears down the
          client and creates a new one with a fresh ``client_id``.
          See ``_recover_from_silence`` for why rebuild (not
          ``disconnect()+reconnect()``) is essential.
        - ``stop()`` shuts down the paho loop and disconnects cleanly.
          The watchdog thread exits on its next poll cycle.

    Thread safety:
        - ``publish`` is safe to call from any thread.  It reads the
          current client reference atomically; if a watchdog rebuild
          is in flight, publishes during the brief window return
          ``None`` and are logged at ``debug``.
        - Callbacks (``on_message``, ``on_connected``) are invoked
          from paho's internal network thread.  Implementations must
          not block and must be thread-safe with respect to the rest
          of the owning daemon.
        - ``start`` / ``stop`` are expected to be called from the
          main thread of the owning daemon, each exactly once.

    Args:
        broker:           MQTT broker hostname or IP.
        port:             MQTT broker port.
        client_id_prefix: Prefix for the MQTT client ID — a timestamp
                          and a per-instance reconnect counter are
                          appended so every rebuild gets a unique ID.
        subscriptions:    ``(topic, qos)`` tuples.  Subscribed on every
                          (re)connect.  Empty list is legal for
                          publish-only clients.
        on_message:       Callback invoked for every incoming message
                          matching one of the subscriptions.  Signature:
                          ``(topic: str, payload: bytes) -> None``.
        on_connected:     Optional callback invoked after subscriptions
                          are (re)applied on every successful connect.
                          Use for "receive path live" log lines.
        keepalive:        Paho keepalive in seconds.  Defaults to
                          ``MQTT_KEEPALIVE``.
        silence_threshold: Application-level silence threshold in
                          seconds before the watchdog forces a rebuild.
                          Defaults to ``WATCHDOG_SILENCE_THRESHOLD``.
                          Set to a small value (e.g. 4.0) in tests.
        watchdog_poll_interval: How often the watchdog checks for
                          silence, in seconds.  Defaults to
                          ``WATCHDOG_POLL_INTERVAL``.
    """

    def __init__(
        self,
        broker: str,
        port: int,
        client_id_prefix: str,
        subscriptions: Sequence[tuple[str, int]],
        on_message: Callable[[str, bytes], None],
        on_connected: Optional[Callable[[], None]] = None,
        keepalive: int = MQTT_KEEPALIVE,
        silence_threshold: float = WATCHDOG_SILENCE_THRESHOLD,
        watchdog_poll_interval: float = WATCHDOG_POLL_INTERVAL,
    ) -> None:
        """Store configuration; no network activity until ``start``."""
        self._broker: str = broker
        self._port: int = port
        self._client_id_prefix: str = client_id_prefix
        self._subscriptions: list[tuple[str, int]] = list(subscriptions)
        self._on_message_cb: Callable[[str, bytes], None] = on_message
        self._on_connected_cb: Optional[Callable[[], None]] = on_connected
        self._keepalive: int = keepalive
        self._silence_threshold: float = silence_threshold
        self._watchdog_poll_interval: float = watchdog_poll_interval

        self._client: Any = None
        self._running: bool = False
        self._connected: bool = False
        # Monotonic clock; None until the first message is observed.
        # Required because wall-clock time jumps (NTP slews, DST) must
        # not spuriously trip the watchdog.
        self._last_message_time: Optional[float] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        # Per-instance counter so each rebuilt client gets a fresh
        # client_id even if two rebuilds happen within the same
        # epoch second.  broker-2's mosquitto rejected reused
        # client_ids on rapid reconnect (session-takeover race).
        self._reconnect_count: int = 0

    # --- lifecycle ---------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """Whether ``paho-mqtt`` is installed.

        ``start`` is a no-op when this is ``False``.  Callers that
        require MQTT should check this up front and abort with a
        clear error instead of silently running without the helper.
        """
        return _HAS_PAHO

    @property
    def is_connected(self) -> bool:
        """Whether the client is currently connected to the broker."""
        return self._connected

    @property
    def client_id_prefix(self) -> str:
        """The prefix used in generated client IDs (diagnostic only)."""
        return self._client_id_prefix

    def start(self) -> None:
        """Start the client and the silence watchdog.

        Non-blocking.  Initial connect is asynchronous via paho's
        ``connect_async``, so an unreachable broker at startup does
        not prevent the owning daemon from completing its own
        initialization — the watchdog and paho's internal reconnect
        logic will establish the session when the broker becomes
        reachable.

        No-op if ``paho-mqtt`` is not installed; logs a warning.
        Idempotent: a second ``start`` call while already running is
        a no-op (logged at debug).
        """
        if not _HAS_PAHO:
            logger.warning(
                "%s: paho-mqtt not installed — MQTT disabled",
                self._client_id_prefix,
            )
            return

        if self._running:
            logger.debug(
                "%s: start() called while already running — ignoring",
                self._client_id_prefix,
            )
            return

        self._running = True
        self._connected = False
        self._last_message_time = None
        self._reconnect_count = 0

        # Same construction path the watchdog uses for recovery, so
        # any future change to client configuration affects both
        # startup and recovery identically — no two divergent paths.
        self._create_and_start_client()

        # Silence watchdog — detects half-open sockets that paho
        # cannot see.  Daemon thread so it dies with the process if
        # stop() is never called (e.g. the owning daemon crashed).
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name=f"{self._client_id_prefix}-mqtt-watchdog",
        )
        self._watchdog_thread.start()

    def stop(self) -> None:
        """Stop the paho loop and disconnect cleanly.

        The watchdog thread exits on its next poll cycle (it is a
        daemon thread and checks ``self._running`` each iteration).
        Idempotent: repeated calls are safe.
        """
        self._running = False
        client = self._client
        if client is not None:
            try:
                client.loop_stop()
            except Exception as exc:
                logger.debug(
                    "%s: loop_stop on stop() raised: %s",
                    self._client_id_prefix, exc,
                )
            try:
                client.disconnect()
            except Exception as exc:
                logger.debug(
                    "%s: disconnect on stop() raised: %s",
                    self._client_id_prefix, exc,
                )
        self._connected = False

    # --- publish -----------------------------------------------------------

    def publish(
        self,
        topic: str,
        payload: Any,
        qos: int = 0,
        retain: bool = False,
    ) -> Any:
        """Publish a message.

        Thread-safe.  Reads ``self._client`` once atomically so a
        concurrent watchdog rebuild cannot produce a use-after-free.
        During the brief window between old-client teardown and new-
        client creation the reference may be ``None``; in that case
        the publish is dropped and logged at debug.

        Args:
            topic:   MQTT topic to publish to.
            payload: Payload (bytes, str, or any object paho accepts).
            qos:     QoS level (0, 1, or 2).
            retain:  Whether the broker should retain this message.

        Returns:
            The paho ``MQTTMessageInfo`` object, or ``None`` if the
            client is not currently available.
        """
        client = self._client
        if client is None:
            logger.debug(
                "%s: publish while disconnected — dropped (topic=%s)",
                self._client_id_prefix, topic,
            )
            return None
        return client.publish(topic, payload, qos=qos, retain=retain)

    # --- client construction ----------------------------------------------

    def _create_and_start_client(self) -> None:
        """Build a fresh paho client and start its network loop.

        Generates a new ``client_id`` on every call by combining the
        prefix, the current epoch second, and the per-instance
        ``_reconnect_count``.  The counter component is essential:
        the broker-2 mosquitto rejected reused client_ids on rapid
        reconnect (the half-open zombie case), so every rebuild MUST
        receive a brand-new id even if two rebuilds happen within
        the same wall-clock second.

        Uses synchronous ``connect`` for the initial attempt, then
        ``loop_start`` for the background network thread.  This is
        the pattern that worked reliably in the pre-refactor
        coordinator / satellite; ``connect_async`` was tried but on
        Daedalus under launchd the loop thread never drove the
        initial connection attempt (paho 2.x + Python 3.14 + macOS
        LaunchDaemon context — the loop thread started, then sat in
        reconnect backoff without ever issuing the first connect).
        Synchronous ``connect`` avoids that class of bug entirely
        at the cost of blocking the caller briefly on broker round
        trip — acceptable because this is a one-time init cost and
        the watchdog owns all subsequent recovery.

        If the initial synchronous connect raises (broker
        unreachable at startup), the exception is logged but NOT
        propagated: the loop is still started so paho's internal
        exponential backoff can eventually establish the session
        when the broker becomes reachable, and ``_connected``
        stays ``False`` until ``on_connect`` fires with rc=0.
        """
        self._reconnect_count += 1
        client_id: str = (
            f"{self._client_id_prefix}-"
            f"{int(time.time())}-{self._reconnect_count}"
        )

        if _PAHO_V2:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
            )
        else:
            client = mqtt.Client(client_id=client_id)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        try:
            client.connect(
                self._broker, self._port, keepalive=self._keepalive,
            )
        except (OSError, ConnectionError) as exc:
            logger.warning(
                "%s: initial MQTT connect failed: %s "
                "— paho's internal reconnect will retry",
                self._client_id_prefix, exc,
            )
        client.loop_start()
        self._client = client
        logger.info(
            "%s: paho client started (client_id=%s, broker=%s:%d)",
            self._client_id_prefix, client_id,
            self._broker, self._port,
        )

    # --- MQTT callbacks ---------------------------------------------------

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        rc: int,
        properties: Any = None,
    ) -> None:
        """Subscribe to all configured topics on (re)connect.

        Compatible with both paho v1 (4 args) and v2 (5 args)
        callback signatures via the ``properties`` default.

        Subscribing here — rather than once at construction — is
        what prevents silent deafness after any broker blip.  With
        ``clean_session=True`` (paho MQTT 3.1.1 default) the broker
        drops subscriptions on disconnect, so they must be restored
        on every new session.  See mbclock 2026-04-16 for the
        consequence of getting this wrong.
        """
        if rc != 0:
            logger.warning(
                "%s: MQTT connect failed: rc=%d",
                self._client_id_prefix, rc,
            )
            self._connected = False
            return
        self._connected = True
        for topic, qos in self._subscriptions:
            client.subscribe(topic, qos=qos)
        logger.info(
            "%s: MQTT connected; subscribed to %d topic(s)",
            self._client_id_prefix, len(self._subscriptions),
        )
        if self._on_connected_cb is not None:
            try:
                self._on_connected_cb()
            except Exception as exc:
                logger.warning(
                    "%s: on_connected hook raised: %s",
                    self._client_id_prefix, exc,
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

        Compatible with paho v1 and v2 callback signatures:
            v1: ``on_disconnect(client, userdata, rc)``
            v2: ``on_disconnect(client, userdata, flags, rc, properties)``

        Paho calls this when the TCP connection drops.  If ``rc != 0``
        the disconnect was unexpected and paho will attempt its own
        reconnect (the loop is still running).  We log at WARNING so
        the event is visible in journalctl; previously the coordinator
        had no on_disconnect and TCP drops were completely invisible.
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

    def _on_message(
        self, client: Any, userdata: Any, msg: Any,
    ) -> None:
        """Dispatch to the user-provided ``on_message`` callback.

        Updates ``_last_message_time`` on every message — this is
        the heartbeat the silence watchdog uses to detect a half-
        open socket.  Any exception from the user callback is
        caught and logged at WARNING so a buggy handler cannot
        crash paho's internal network thread.
        """
        self._last_message_time = time.monotonic()
        try:
            self._on_message_cb(msg.topic, msg.payload)
        except Exception as exc:
            logger.warning(
                "%s: on_message callback raised on %s: %s",
                self._client_id_prefix, msg.topic, exc,
            )

    # --- silence watchdog -------------------------------------------------

    def _watchdog_loop(self) -> None:
        """Run the watchdog until ``stop`` is called.

        Sleeps in small chunks so ``stop`` is responsive (worst-case
        exit latency is one second, not ``watchdog_poll_interval``).
        Each wake calls ``_watchdog_check`` for a single iteration;
        the check is extracted so unit tests can drive it directly
        without mocking the sleep.
        """
        while self._running:
            remaining: float = self._watchdog_poll_interval
            while remaining > 0 and self._running:
                chunk: float = min(remaining, 1.0)
                time.sleep(chunk)
                remaining -= chunk
            if not self._running:
                break
            self._watchdog_check()

    def _watchdog_check(self) -> bool:
        """One iteration of the silence watchdog.

        Returns ``True`` if a recovery was triggered this iteration,
        ``False`` otherwise.

        Skip conditions (no action, returns ``False``):
            - No message has ever been observed
              (``_last_message_time is None``).  We do not start the
              watchdog clock until real traffic arrives, so a broker
              that is legitimately quiet at startup does not trip us.
            - Silence has not yet reached ``silence_threshold``.
            - We believe we are disconnected (``not _connected``) —
              a recovery is already in flight, or the initial connect
              has not completed.

        On trigger: log a warning and call ``_recover_from_silence``.
        """
        last: Optional[float] = self._last_message_time
        if last is None:
            return False
        silence: float = time.monotonic() - last
        if silence < self._silence_threshold:
            return False
        if not self._connected:
            return False

        logger.warning(
            "%s: no messages for %.0fs — forcing MQTT reconnect "
            "(probable half-open TCP socket)",
            self._client_id_prefix, silence,
        )
        self._recover_from_silence()
        return True

    def _recover_from_silence(self) -> None:
        """Tear down the current client and start a fresh one.

        Full rebuild — NOT ``disconnect()`` followed by ``reconnect()``
        on the existing client.

        History: an earlier fix (commit 7679713, 2026-04-06) used
        the same-client disconnect+reconnect approach.  In production
        against mosquitto + Z2M on broker-2 it produced a zombie
        session: SUBSCRIBE was ACKed, retained messages were
        delivered, but no live publishes ever came through.  The bug
        recurred 14 hours after that fix (2026-04-07 09:14:50) and
        burned 26 hours of Zigbee data.  Generating a brand-new
        ``client_id`` on every recovery avoids the broker-side
        session-takeover race that caused the zombie.

        Order of operations matters:
          1. Reset watchdog state FIRST so that if the watchdog
             fires again before the new client connects, the
             ``last is None`` and ``not _connected`` guards skip.
          2. Best-effort teardown of the old client.  Both calls are
             allowed to fail silently because the old socket may
             already be dead — which is exactly the condition that
             triggered us.  Teardown failures are logged at debug.
          3. Build a fresh client with a new ``client_id``.  This is
             the same code path ``start`` uses, so configuration
             cannot drift between startup and recovery.
        """
        self._last_message_time = None
        self._connected = False

        old_client = self._client
        self._client = None
        if old_client is not None:
            try:
                old_client.loop_stop()
            except Exception as exc:
                logger.debug(
                    "%s: loop_stop on old client raised: %s",
                    self._client_id_prefix, exc,
                )
            try:
                old_client.disconnect()
            except Exception as exc:
                logger.debug(
                    "%s: disconnect on old client raised: %s",
                    self._client_id_prefix, exc,
                )

        self._create_and_start_client()
