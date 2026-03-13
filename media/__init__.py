"""GlowUp media stream subsystem.

Provides a source-agnostic media pipeline for extracting real-time signals
from audio and video streams (cameras, files, microphones) and feeding them
into the effect engine via the SignalBus.

Architecture::

    MediaSource  → raw PCM / video frames (ffmpeg pipe)
    SignalExtractor → named float/array signals (FFT, beat, motion)
    SignalBus    → thread-safe registry read by engine binding resolver
    MediaManager → lifecycle orchestration (refcount, start/stop)

Optional dependencies (graceful degradation):

    numpy       — 30x FFT speedup (Tier 1)
    scipy       — advanced spectral analysis (Tier 2)
    opencv      — video extraction + GPU accel (Tier 3)
    paho-mqtt   — distributed signal bus (Tier 4)

Public classes:
    SignalBus    — thread-safe named signal registry
    SignalMeta   — metadata for a registered signal
    MediaManager — lifecycle manager for sources and extractors
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

# ---------------------------------------------------------------------------
# Optional dependency: paho-mqtt for distributed signal bus
# ---------------------------------------------------------------------------

try:
    import paho.mqtt.client as mqtt
    _HAS_MQTT: bool = True
except ImportError:
    mqtt = None  # type: ignore[assignment]
    _HAS_MQTT = False

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# A signal value is either a scalar float or an array of floats.
SignalValue = Union[float, list[float]]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic prefix for signal bus network bridge.
SIGNAL_TOPIC_PREFIX: str = "glowup/signals/"

# Default idle timeout before stopping an unused media source (seconds).
DEFAULT_IDLE_TIMEOUT: float = 30.0

# MQTT QoS level for signal publishing (0 = fire-and-forget, lowest latency).
MQTT_QOS: int = 0

logger: logging.Logger = logging.getLogger("glowup.media")


# ---------------------------------------------------------------------------
# SignalMeta
# ---------------------------------------------------------------------------

@dataclass
class SignalMeta:
    """Metadata describing a registered signal.

    Used by the REST API and iOS app to present available signals
    for binding to effect parameters.

    Attributes:
        signal_type: ``"scalar"`` or ``"array"``.
        description: Human-readable description of the signal.
        source_name: Name of the media source that produces this signal.
        min_val:     Expected minimum value (always 0.0 for normalized).
        max_val:     Expected maximum value (always 1.0 for normalized).
    """
    signal_type: str = "scalar"
    description: str = ""
    source_name: str = ""
    min_val: float = 0.0
    max_val: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary.

        Returns:
            Dict suitable for API responses.
        """
        return {
            "type": self.signal_type,
            "description": self.description,
            "source": self.source_name,
            "min": self.min_val,
            "max": self.max_val,
        }


# ---------------------------------------------------------------------------
# SignalBus
# ---------------------------------------------------------------------------

class SignalBus:
    """Thread-safe registry of named signal values.

    The bus is the universal data exchange layer.  SignalExtractors write
    normalized values; the engine binding resolver reads them.  All
    operations are atomic under a single lock — contention is negligible
    at 20 Hz read rate and ~15 Hz write rate.

    Optional MQTT bridge mode (requires ``paho-mqtt``): when enabled,
    every local write is also published to the MQTT broker, and remote
    signals published by other nodes are received and merged into the
    local dict.  This enables distributed compute without changing any
    effect or engine code.

    Transport routing (v1.1): named :class:`TransportAdapter` instances
    can be registered via :meth:`add_transport`.  Per-signal routes
    configured via :meth:`set_route` direct ``write()`` calls to the
    appropriate transport.  If no route is configured, the legacy MQTT
    bridge path is used as a fallback.
    """

    def __init__(self) -> None:
        """Initialize an empty signal bus."""
        self._signals: dict[str, SignalValue] = {}
        self._metadata: dict[str, SignalMeta] = {}
        self._lock: threading.Lock = threading.Lock()
        self._mqtt_client: Optional[Any] = None
        self._mqtt_connected: bool = False

        # Transport routing (v1.1).
        self._transports: dict[str, Any] = {}         # name → TransportAdapter
        self._signal_routes: dict[str, str] = {}       # signal_name → transport_name

    # ------------------------------------------------------------------
    # Core read/write
    # ------------------------------------------------------------------

    def write(self, name: str, value: SignalValue) -> None:
        """Atomically update a signal value.

        If a transport route is configured for this signal, the value is
        published via that transport adapter.  Otherwise, falls back to
        the legacy MQTT bridge if active.

        Args:
            name:  Hierarchical signal name (e.g. ``"backyard:audio:bass"``).
            value: Normalized float in [0.0, 1.0] or list of such floats.
        """
        with self._lock:
            self._signals[name] = value
        # Check transport routing (v1.1) first.
        transport_name: Optional[str] = self._signal_routes.get(name)
        if transport_name and transport_name in self._transports:
            try:
                self._transports[transport_name].publish(name, value)
            except Exception:
                pass  # Best-effort.
            return
        # Fallback: legacy MQTT bridge (outside lock to avoid blocking).
        if self._mqtt_client and self._mqtt_connected:
            try:
                payload: str = json.dumps(value)
                self._mqtt_client.publish(
                    SIGNAL_TOPIC_PREFIX + name, payload, qos=MQTT_QOS
                )
            except Exception:
                pass  # Best-effort; don't crash on MQTT failure.

    def read(self, name: str, default: SignalValue = 0.0) -> SignalValue:
        """Atomically read a signal value.

        Args:
            name:    Signal name to look up.
            default: Value returned if the signal does not exist.

        Returns:
            The current signal value, or *default* if unregistered.
        """
        with self._lock:
            return self._signals.get(name, default)

    def read_many(self, names: list[str],
                  default: SignalValue = 0.0) -> dict[str, SignalValue]:
        """Atomically read multiple signals in one lock acquisition.

        More efficient than calling :meth:`read` in a loop when the
        binding resolver needs several signals per frame.

        Args:
            names:   List of signal names.
            default: Default for missing signals.

        Returns:
            Dict mapping each name to its current value.
        """
        with self._lock:
            return {n: self._signals.get(n, default) for n in names}

    # ------------------------------------------------------------------
    # Registration and discovery
    # ------------------------------------------------------------------

    def register(self, name: str, meta: SignalMeta) -> None:
        """Declare a signal with metadata before first write.

        This is called by extractors during initialization so that the
        API can advertise available signals to the iOS app's picker.

        Args:
            name: Signal name.
            meta: Metadata describing the signal.
        """
        with self._lock:
            self._metadata[name] = meta
            # Initialize with a zero value if not already present.
            if name not in self._signals:
                if meta.signal_type == "array":
                    self._signals[name] = []
                else:
                    self._signals[name] = 0.0

    def unregister(self, name: str) -> None:
        """Remove a signal from the bus.

        Called when a source or extractor is stopped.

        Args:
            name: Signal name to remove.
        """
        with self._lock:
            self._signals.pop(name, None)
            self._metadata.pop(name, None)

    def list_signals(self) -> list[dict[str, Any]]:
        """Return metadata for all registered signals.

        Used by ``GET /api/media/signals`` to populate the iOS picker.

        Returns:
            List of dicts with ``"name"`` and metadata fields.
        """
        with self._lock:
            result: list[dict[str, Any]] = []
            for name, meta in sorted(self._metadata.items()):
                entry: dict[str, Any] = {"name": name}
                entry.update(meta.to_dict())
                result.append(entry)
            return result

    def signal_names(self) -> list[str]:
        """Return sorted list of all registered signal names.

        Returns:
            List of signal name strings.
        """
        with self._lock:
            return sorted(self._metadata.keys())

    # ------------------------------------------------------------------
    # Transport routing (v1.1)
    # ------------------------------------------------------------------

    def add_transport(self, name: str, adapter: Any) -> None:
        """Register a named transport adapter.

        The adapter must implement the :class:`TransportAdapter` interface
        (``publish``, ``subscribe``, ``start``, ``stop``).  Once registered,
        signals can be routed to this transport via :meth:`set_route`.

        Args:
            name:    Transport name (e.g. ``"udp"``, ``"mqtt_v2"``).
            adapter: A :class:`TransportAdapter` instance.
        """
        self._transports[name] = adapter

    def remove_transport(self, name: str) -> None:
        """Unregister a transport adapter.

        Also removes all signal routes pointing to this transport.

        Args:
            name: Transport name to remove.
        """
        self._transports.pop(name, None)
        # Clear routes that referenced this transport.
        stale: list[str] = [
            sig for sig, tn in self._signal_routes.items() if tn == name
        ]
        for sig in stale:
            del self._signal_routes[sig]

    def set_route(self, signal_name: str, transport_name: str) -> None:
        """Route a signal to a specific transport for publishing.

        When ``write()`` is called for this signal, the value is
        published via the named transport instead of the legacy
        MQTT bridge.

        Args:
            signal_name:    Signal name to route.
            transport_name: Name of a registered transport adapter.
        """
        self._signal_routes[signal_name] = transport_name

    def clear_route(self, signal_name: str) -> None:
        """Remove the transport route for a signal.

        The signal reverts to the legacy MQTT bridge (if active)
        or local-only operation.

        Args:
            signal_name: Signal name to un-route.
        """
        self._signal_routes.pop(signal_name, None)

    def subscribe_remote(self, signal_name: str,
                         transport_name: str) -> None:
        """Subscribe to a remote signal via a transport adapter.

        Incoming values are merged into the local signal dict,
        making them available via :meth:`read` as if they were
        written locally.

        Args:
            signal_name:    Signal name to subscribe to.
            transport_name: Name of the transport adapter to use.
        """
        adapter = self._transports.get(transport_name)
        if adapter is None:
            logger.warning(
                "Cannot subscribe to '%s': transport '%s' not found",
                signal_name, transport_name,
            )
            return

        def _ingest(name: str, value: SignalValue) -> None:
            """Merge a remote signal into the local bus."""
            with self._lock:
                self._signals[name] = value

        adapter.subscribe(signal_name, _ingest)

    # ------------------------------------------------------------------
    # MQTT bridge
    # ------------------------------------------------------------------

    def enable_mqtt(self, broker: str, port: int = 1883,
                    username: Optional[str] = None,
                    password: Optional[str] = None,
                    tls: bool = False) -> bool:
        """Enable MQTT bridge mode for distributed signal sharing.

        Requires ``paho-mqtt`` (Tier 4).  If the package is not installed,
        this method logs a warning and returns ``False``.

        Args:
            broker:   MQTT broker hostname or IP.
            port:     Broker port (default 1883).
            username: Optional MQTT username.
            password: Optional MQTT password.
            tls:      Enable TLS if ``True``.

        Returns:
            ``True`` if MQTT bridge was started successfully.
        """
        if not _HAS_MQTT:
            logger.warning(
                "paho-mqtt not installed — MQTT signal bridge unavailable. "
                "Install with: pip install paho-mqtt"
            )
            return False

        client = mqtt.Client(
            client_id=f"glowup-signals-{int(time.time())}",
            protocol=mqtt.MQTTv311,
        )
        if username:
            client.username_pw_set(username, password)
        if tls:
            client.tls_set()

        def on_connect(client: Any, userdata: Any, flags: Any,
                       rc: int) -> None:
            """Subscribe to remote signals on successful connect."""
            if rc == 0:
                self._mqtt_connected = True
                client.subscribe(SIGNAL_TOPIC_PREFIX + "#", qos=MQTT_QOS)
                logger.info("SignalBus MQTT bridge connected to %s:%d",
                            broker, port)
            else:
                logger.error("SignalBus MQTT connect failed: rc=%d", rc)

        def on_disconnect(client: Any, userdata: Any, rc: int) -> None:
            """Mark bridge as disconnected."""
            self._mqtt_connected = False
            if rc != 0:
                logger.warning("SignalBus MQTT disconnected unexpectedly")

        def on_message(client: Any, userdata: Any, msg: Any) -> None:
            """Receive a remote signal and merge into local bus."""
            if not msg.topic.startswith(SIGNAL_TOPIC_PREFIX):
                return
            signal_name: str = msg.topic[len(SIGNAL_TOPIC_PREFIX):]
            try:
                value = json.loads(msg.payload.decode("utf-8"))
                # Only accept scalars and float lists.
                if isinstance(value, (int, float)):
                    with self._lock:
                        self._signals[signal_name] = float(value)
                elif isinstance(value, list):
                    with self._lock:
                        self._signals[signal_name] = [
                            float(v) for v in value
                        ]
            except (json.JSONDecodeError, ValueError, TypeError):
                pass  # Ignore malformed payloads.

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message

        try:
            client.connect_async(broker, port)
            client.loop_start()
            self._mqtt_client = client
            return True
        except Exception as exc:
            logger.error("Failed to start MQTT bridge: %s", exc)
            return False

    def disable_mqtt(self) -> None:
        """Disconnect the MQTT bridge if active."""
        if self._mqtt_client:
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
            except Exception:
                pass
            self._mqtt_client = None
            self._mqtt_connected = False


# ---------------------------------------------------------------------------
# MediaManager
# ---------------------------------------------------------------------------

class MediaManager:
    """Lifecycle manager for media sources and signal extractors.

    Owns the :class:`SignalBus` and all :class:`MediaSource` instances.
    Implements reference counting so that RTSP streams are only active
    when at least one effect binding references them, and are stopped
    after an idle timeout when unused.

    The manager is constructed by the server during startup if
    ``media_sources`` is present in the config file.
    """

    def __init__(self) -> None:
        """Initialize an empty media manager."""
        self._bus: SignalBus = SignalBus()
        self._sources: dict[str, Any] = {}       # name → MediaSource
        self._extractors: dict[str, list] = {}    # name → [SignalExtractor]
        self._ref_counts: dict[str, int] = {}     # name → active binding count
        self._idle_timers: dict[str, float] = {}  # name → last release time
        self._lock: threading.Lock = threading.Lock()
        self._idle_timeout: float = DEFAULT_IDLE_TIMEOUT
        self._configured: bool = False

    @property
    def bus(self) -> SignalBus:
        """The signal bus shared by all sources and effects.

        Returns:
            The :class:`SignalBus` instance.
        """
        return self._bus

    def configure(self, config: dict[str, Any]) -> None:
        """Parse media configuration and create sources.

        Sources are created but not started — they activate on first
        :meth:`acquire` call.  MQTT bridge is enabled if ``signal_bus.mqtt``
        is ``true`` in the config.

        Args:
            config: Full server config dict (parsed from server.json).
        """
        from .source import create_source  # Deferred to avoid circular import.

        media_cfg: dict[str, Any] = config.get("media_sources", {})
        bus_cfg: dict[str, Any] = config.get("signal_bus", {})

        # Create sources from config.
        for name, src_cfg in media_cfg.items():
            try:
                source = create_source(name, src_cfg, self._bus)
                with self._lock:
                    self._sources[name] = source
                    self._ref_counts[name] = 0
                logger.info("Configured media source: %s (%s)",
                            name, src_cfg.get("type", "unknown"))
            except Exception as exc:
                logger.error("Failed to configure source '%s': %s",
                             name, exc)

        # Enable MQTT bridge if configured.
        if bus_cfg.get("mqtt"):
            mqtt_cfg: dict[str, Any] = config.get("mqtt", {})
            self._bus.enable_mqtt(
                broker=mqtt_cfg.get("broker", "localhost"),
                port=mqtt_cfg.get("port", 1883),
                username=mqtt_cfg.get("username"),
                password=mqtt_cfg.get("password"),
                tls=mqtt_cfg.get("tls", False),
            )

        self._configured = True

    def acquire(self, source_name: str) -> bool:
        """Increment reference count for a source; start if first reference.

        Called when an effect binding references a signal from this source.

        Args:
            source_name: Name of the media source (from config).

        Returns:
            ``True`` if the source exists and is (or was) started.
        """
        with self._lock:
            if source_name not in self._sources:
                logger.warning("Unknown media source: %s", source_name)
                return False
            self._ref_counts[source_name] = (
                self._ref_counts.get(source_name, 0) + 1
            )
            # Clear idle timer.
            self._idle_timers.pop(source_name, None)
            source = self._sources[source_name]

        # Start outside lock to avoid holding it during ffmpeg spawn.
        if not source.is_alive():
            try:
                source.start()
                logger.info("Started media source: %s", source_name)
            except Exception as exc:
                logger.error("Failed to start source '%s': %s",
                             source_name, exc)
                return False
        return True

    def release(self, source_name: str) -> None:
        """Decrement reference count; schedule stop if zero.

        Called when an effect with bindings is stopped.

        Args:
            source_name: Name of the media source.
        """
        with self._lock:
            count: int = self._ref_counts.get(source_name, 0)
            if count > 0:
                count -= 1
                self._ref_counts[source_name] = count
            if count == 0:
                self._idle_timers[source_name] = time.time()

    def check_idle(self) -> None:
        """Stop sources that have been idle past the timeout.

        Should be called periodically (e.g., from the scheduler loop).
        """
        now: float = time.time()
        to_stop: list[str] = []
        with self._lock:
            for name, idle_since in list(self._idle_timers.items()):
                if now - idle_since >= self._idle_timeout:
                    to_stop.append(name)
                    del self._idle_timers[name]

        for name in to_stop:
            source = self._sources.get(name)
            if source and source.is_alive():
                try:
                    source.stop()
                    logger.info("Stopped idle media source: %s", name)
                except Exception as exc:
                    logger.error("Error stopping source '%s': %s", name, exc)

    def get_source_names(self) -> list[str]:
        """Return sorted list of configured source names.

        Returns:
            List of source name strings.
        """
        with self._lock:
            return sorted(self._sources.keys())

    def get_status(self) -> dict[str, Any]:
        """Return status of all configured sources.

        Credentials are **never** exposed in this response.

        Returns:
            Dict suitable for ``GET /api/media/sources`` response.
        """
        with self._lock:
            sources: list[dict[str, Any]] = []
            for name in sorted(self._sources.keys()):
                source = self._sources[name]
                sources.append({
                    "name": name,
                    "type": getattr(source, "source_type", "unknown"),
                    "media_type": getattr(source, "media_type", "unknown"),
                    "alive": source.is_alive(),
                    "ref_count": self._ref_counts.get(name, 0),
                })
            return {
                "sources": sources,
                "signal_count": len(self._bus._metadata),
                "mqtt_connected": self._bus._mqtt_connected,
            }

    def start_source(self, source_name: str) -> bool:
        """Manually start a media source.

        Args:
            source_name: Name of the source to start.

        Returns:
            ``True`` if started successfully.
        """
        return self.acquire(source_name)

    def stop_source(self, source_name: str) -> bool:
        """Manually stop a media source.

        Args:
            source_name: Name of the source to stop.

        Returns:
            ``True`` if the source existed.
        """
        with self._lock:
            source = self._sources.get(source_name)
            if not source:
                return False
            self._ref_counts[source_name] = 0
            self._idle_timers.pop(source_name, None)
        if source.is_alive():
            source.stop()
        return True

    def shutdown(self) -> None:
        """Stop all sources and disable MQTT bridge.

        Called during server shutdown.
        """
        with self._lock:
            names: list[str] = list(self._sources.keys())
        for name in names:
            source = self._sources.get(name)
            if source and source.is_alive():
                try:
                    source.stop()
                except Exception:
                    pass
        self._bus.disable_mqtt()
        logger.info("MediaManager shut down")

    def extract_source_name(self, signal_name: str) -> Optional[str]:
        """Parse the source name from a hierarchical signal name.

        Signal names follow the convention ``{source}:{extractor}:{signal}``.
        This method returns the first component.

        Args:
            signal_name: Full signal name (e.g. ``"backyard:audio:bass"``).

        Returns:
            Source name (e.g. ``"backyard"``), or ``None`` if malformed.
        """
        parts: list[str] = signal_name.split(":")
        if len(parts) >= 2:
            return parts[0]
        return None
