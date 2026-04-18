"""Worker agent — lightweight daemon for compute and emitter nodes.

Runs on any machine in the fleet: Jetsons (compute), Macs (emitters),
the ML box (both), or any node that offers resources.  The agent:

1. Connects to the MQTT broker (on the Pi).
2. Publishes its capabilities as a retained message.
3. Sets an LWT so the orchestrator detects crashes.
4. Subscribes to its assignment topic and executes work.
5. Publishes periodic health metrics.

Two assignment types are supported:

**Compute assignment** — the agent runs an operator (e.g.,
``AudioExtractor``) that transforms signals.  Input signals arrive
via MQTT or UDP; output signals are published the same way.

**Emitter assignment** — the agent instantiates a remote emitter
from the emitter registry (e.g., ``audio_out``, ``screen``),
manages its lifecycle, and feeds it frames received via MQTT or UDP.
This is how a Mac becomes a speaker for the theremin pipeline, or
a Jetson drives a local LED matrix.

Usage::

    python3 -m distributed.worker_agent /etc/glowup/agent.json
    python3 -m distributed.worker_agent --node-id judy --broker 192.0.2.48

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import argparse
import json
import logging
import os
import platform
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Optional

from .capability import (
    NodeCapability, NodeHealth,
    capability_topic, status_topic, assignment_topic, health_topic,
    STATUS_ONLINE, STATUS_OFFLINE,
    CAPABILITY_QOS,
)
from .orchestrator import WorkAssignment, SignalBinding, TRANSPORT_UDP, TRANSPORT_MQTT
from .transport_adapter import UdpTransport, MqttTransport
from network_config import net

logger: logging.Logger = logging.getLogger("glowup.agent")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default MQTT broker (from centralized network config).
DEFAULT_BROKER: str = net.broker

# Default MQTT port.
DEFAULT_PORT: int = 1883

# Health publish interval (seconds).
HEALTH_INTERVAL: float = 5.0

# Client ID prefix for MQTT connections.
CLIENT_ID_PREFIX: str = "glowup-agent"

# MQTT QoS for status and health.
STATUS_QOS: int = 1
HEALTH_QOS: int = 0


# ---------------------------------------------------------------------------
# Running operator state
# ---------------------------------------------------------------------------

class _RunningOperator:
    """Tracks state of an active operator on this agent.

    Attributes:
        assignment_id:  Work assignment identifier.
        operator_name:  Operator class name.
        operator:       The instantiated operator (extractor) object.
        transports:     Active transport adapters to clean up on stop.
    """

    def __init__(self, assignment_id: str, operator_name: str) -> None:
        """Initialize running operator tracking.

        Args:
            assignment_id: Unique assignment identifier.
            operator_name: Operator class name.
        """
        self.assignment_id: str = assignment_id
        self.operator_name: str = operator_name
        self.operator: Optional[Any] = None
        self.transports: list[Any] = []


# ---------------------------------------------------------------------------
# Running emitter state
# ---------------------------------------------------------------------------

class _RunningEmitter:
    """Tracks state of an active remote emitter on this agent.

    Parallels :class:`_RunningOperator` for the emitter lifecycle.
    The emitter instance is created from the emitter registry, opened,
    and fed frames from an MQTT or UDP subscription.

    Attributes:
        assignment_id:  Work assignment identifier.
        emitter_type:   Emitter registry type string.
        emitter:        The instantiated :class:`Emitter` object.
        transports:     Active transport/receiver objects to clean up on stop.
        frame_count:    Total frames dispatched to on_emit().
        failure_count:  Total on_emit() failures.
    """

    def __init__(self, assignment_id: str, emitter_type: str) -> None:
        """Initialize running emitter tracking.

        Args:
            assignment_id: Unique assignment identifier.
            emitter_type:  Emitter registry type string.
        """
        self.assignment_id: str = assignment_id
        self.emitter_type: str = emitter_type
        self.emitter: Optional[Any] = None
        self.transports: list[Any] = []
        self.frame_count: int = 0
        self.failure_count: int = 0


# ---------------------------------------------------------------------------
# WorkerAgent
# ---------------------------------------------------------------------------

class WorkerAgent:
    """Lightweight daemon running on any fleet node.

    Connects to the MQTT broker, publishes capabilities, receives
    work assignments, and executes operators or emitters with the
    appropriate transport adapters.

    Supports two roles:

    * **compute** — runs operators that transform signals.
    * **emitter** — runs remote emitters that express frames on
      local hardware (speakers, displays, GPIO, etc.).

    A single agent can serve both roles simultaneously.

    Args:
        config: Agent configuration dict (from agent.json or CLI args).
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize the worker agent.

        Args:
            config: Configuration dict with at least ``node_id`` and ``broker``.
        """
        self._config: dict[str, Any] = config
        self._node_id: str = config.get("node_id", socket.gethostname())
        self._broker: str = config.get("mqtt_broker", DEFAULT_BROKER)
        self._port: int = config.get("mqtt_port", DEFAULT_PORT)
        self._client: Optional[Any] = None
        self._stop_event: threading.Event = threading.Event()
        self._health_thread: Optional[threading.Thread] = None
        self._operators: dict[str, _RunningOperator] = {}
        self._emitters: dict[str, _RunningEmitter] = {}
        self._lock: threading.Lock = threading.Lock()
        self._start_time: float = 0.0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the agent and block until Ctrl+C or stop_event.

        Connects to MQTT, publishes capability, starts health thread,
        and waits for assignments.
        """
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            print(
                "paho-mqtt is required for the worker agent. "
                "Install with: pip install paho-mqtt",
                file=sys.stderr,
            )
            return

        self._start_time = time.monotonic()

        # Create MQTT client.
        client_id: str = f"{CLIENT_ID_PREFIX}-{self._node_id}-{os.getpid()}"
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )

        username: Optional[str] = self._config.get("mqtt_username")
        password: Optional[str] = self._config.get("mqtt_password")
        if username:
            self._client.username_pw_set(username, password)

        # LWT: broker publishes "offline" if we disconnect unexpectedly.
        self._client.will_set(
            status_topic(self._node_id),
            payload=STATUS_OFFLINE,
            qos=STATUS_QOS,
            retain=True,
        )

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.message_callback_add(
            assignment_topic(self._node_id),
            self._on_assignment,
        )

        # Slow down auto-reconnect to prevent reconnect storms.
        self._client.reconnect_delay_set(
            min_delay=5, max_delay=30,
        )

        # Connect.
        try:
            self._client.connect(self._broker, self._port)
        except Exception as exc:
            logger.error(
                "Failed to connect to MQTT broker %s:%d: %s",
                self._broker, self._port, exc,
            )
            print(
                f"Cannot connect to MQTT broker at {self._broker}:{self._port}: {exc}",
                file=sys.stderr,
            )
            return

        self._client.loop_start()

        # Start health publisher.
        self._health_thread = threading.Thread(
            target=self._health_loop,
            name="agent-health",
            daemon=True,
        )
        self._health_thread.start()

        logger.info(
            "Worker agent '%s' started — broker %s:%d",
            self._node_id, self._broker, self._port,
        )
        print(
            f"Agent '{self._node_id}' running — broker {self._broker}:{self._port}. "
            f"Press Ctrl+C to stop.",
        )

        # Block until stopped.
        self._stop_event.wait()

        # Clean shutdown.
        self._shutdown()

    def stop(self) -> None:
        """Signal the agent to stop."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # MQTT callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client: Any, userdata: Any, flags: Any,
                    reason_code: Any, properties: Any = None) -> None:
        """Handle MQTT connection — publish capability and subscribe."""
        if reason_code != 0:
            logger.error("MQTT connect refused: %s", reason_code)
            return

        logger.info("Connected to MQTT broker")

        # Publish online status.
        client.publish(
            status_topic(self._node_id),
            STATUS_ONLINE,
            qos=STATUS_QOS,
            retain=True,
        )

        # Publish capability.
        self._publish_capability()

        # Subscribe to our assignment topic.
        client.subscribe(
            assignment_topic(self._node_id), qos=1,
        )

    def _on_disconnect(self, client: Any, userdata: Any, flags: Any,
                       reason_code: Any, properties: Any = None) -> None:
        """Handle MQTT disconnection."""
        if reason_code != 0:
            logger.warning(
                "Unexpected MQTT disconnect (rc=%s), reconnecting...",
                reason_code,
            )

    def _on_assignment(self, client: Any, userdata: Any,
                       msg: Any) -> None:
        """Handle an incoming work assignment.

        Routes to the operator or emitter lifecycle based on whether
        the assignment has ``emitter_type`` set.
        """
        try:
            payload: str = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            return

        assignment: Optional[WorkAssignment] = WorkAssignment.from_json(payload)
        if assignment is None:
            logger.warning("Received malformed assignment")
            return

        if assignment.action == "stop":
            # Stop checks both operator and emitter dicts.
            self._stop_assignment(assignment.assignment_id)
        elif assignment.action == "start":
            if assignment.is_emitter_assignment:
                self._start_emitter(assignment)
            else:
                self._start_operator(assignment)
        else:
            logger.warning(
                "Unknown assignment action: '%s'", assignment.action,
            )

    # ------------------------------------------------------------------
    # Capability
    # ------------------------------------------------------------------

    def _publish_capability(self) -> None:
        """Build and publish this node's capability message."""
        cap: NodeCapability = NodeCapability(
            node_id=self._node_id,
            hostname=socket.gethostname(),
            ip=self._get_local_ip(),
            roles=self._config.get("roles", ["compute"]),
            resources=self._config.get("resources", {}),
            operators=self._config.get("operators", []),
            emitters=self._config.get("emitters", []),
            version=__version__,
        )

        if self._client:
            self._client.publish(
                capability_topic(self._node_id),
                cap.to_json(),
                qos=CAPABILITY_QOS,
                retain=True,
            )
            logger.info(
                "Published capability: node=%s, roles=%s",
                self._node_id, cap.roles,
            )

    def _get_local_ip(self) -> str:
        """Detect this machine's LAN IP address.

        Returns:
            IPv4 address string, or ``"127.0.0.1"`` on failure.
        """
        # Configured IP takes precedence.
        configured: str = self._config.get("ip", "")
        if configured:
            return configured

        # Auto-detect by connecting to an external address.
        try:
            s: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.0.0.1", 80))
            ip: str = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    # ------------------------------------------------------------------
    # Operator lifecycle
    # ------------------------------------------------------------------

    def _start_operator(self, assignment: WorkAssignment) -> None:
        """Instantiate and start an operator from a work assignment.

        Args:
            assignment: The work assignment describing what to run.
        """
        with self._lock:
            if assignment.assignment_id in self._operators:
                logger.warning(
                    "Assignment '%s' already running",
                    assignment.assignment_id,
                )
                return

        logger.info(
            "Starting operator '%s' (assignment %s)",
            assignment.operator_name, assignment.assignment_id,
        )

        running: _RunningOperator = _RunningOperator(
            assignment.assignment_id, assignment.operator_name,
        )

        try:
            # Import the operator.
            operator: Any = self._create_operator(assignment)
            if operator is None:
                return
            running.operator = operator

            # Set up input transports.
            for inp in assignment.inputs:
                self._setup_input(inp, operator, running)

            # Set up output transports.
            for out in assignment.outputs:
                self._setup_output(out, operator, running)

        except Exception as exc:
            logger.error(
                "Failed to start operator '%s': %s",
                assignment.operator_name, exc,
            )
            # Clean up partial setup.
            for t in running.transports:
                try:
                    t.stop()
                except Exception as exc:
                    logger.debug("Error stopping operator transport during cleanup: %s", exc)
            return

        with self._lock:
            self._operators[assignment.assignment_id] = running

        logger.info(
            "Operator '%s' running (assignment %s)",
            assignment.operator_name, assignment.assignment_id,
        )

    def _stop_assignment(self, assignment_id: str) -> None:
        """Stop a running operator or emitter by assignment ID.

        Checks both the operator and emitter dicts.

        Args:
            assignment_id: The assignment to stop.
        """
        # Try operator first.
        with self._lock:
            running_op: Optional[_RunningOperator] = self._operators.pop(
                assignment_id, None,
            )
        if running_op is not None:
            self._teardown_operator(running_op)
            return

        # Try emitter.
        with self._lock:
            running_em: Optional[_RunningEmitter] = self._emitters.pop(
                assignment_id, None,
            )
        if running_em is not None:
            self._teardown_emitter(running_em)
            return

        logger.warning(
            "No running operator or emitter for assignment '%s'",
            assignment_id,
        )

    def _stop_operator(self, assignment_id: str) -> None:
        """Stop a running operator and clean up its transports.

        Args:
            assignment_id: The assignment to stop.
        """
        with self._lock:
            running: Optional[_RunningOperator] = self._operators.pop(
                assignment_id, None,
            )

        if running is None:
            logger.warning("No running operator for assignment '%s'", assignment_id)
            return

        self._teardown_operator(running)

    def _teardown_operator(self, running: _RunningOperator) -> None:
        """Clean up a running operator's transports.

        Args:
            running: The running operator state to tear down.
        """
        for t in running.transports:
            try:
                t.stop()
            except Exception as exc:
                logger.error("Error stopping transport: %s", exc)

        logger.info(
            "Stopped operator '%s' (assignment %s)",
            running.operator_name, running.assignment_id,
        )

    def _create_operator(self, assignment: WorkAssignment) -> Optional[Any]:
        """Import and instantiate an operator.

        Currently supports ``AudioExtractor`` from the media pipeline.
        Future operators will be looked up via a registry.

        Args:
            assignment: Work assignment with operator name and config.

        Returns:
            Instantiated operator, or ``None`` on error.
        """
        name: str = assignment.operator_name
        config: dict[str, Any] = assignment.operator_config

        if name == "AudioExtractor":
            try:
                from media import SignalBus
                from media.extractors import AudioExtractor

                bus: SignalBus = SignalBus()
                # Enable MQTT bridge on the operator's bus so ALL
                # output signals (bands, bass, mid, treble, rms,
                # energy, beat, centroid) auto-publish to the broker.
                bus.enable_mqtt(broker=self._broker, port=self._port)
                operator: AudioExtractor = AudioExtractor(
                    source_name=config.get("source_name", "remote"),
                    sample_rate=config.get("sample_rate", 44100),
                    bus=bus,
                    band_count=config.get("bands", 8),
                )
                # Store the bus on the operator for output wiring.
                operator._agent_bus = bus  # type: ignore[attr-defined]
                return operator
            except ImportError as exc:
                logger.error(
                    "Cannot import AudioExtractor: %s", exc,
                )
                return None
        else:
            logger.error("Unknown operator: '%s'", name)
            return None

    def _setup_input(self, binding: SignalBinding, operator: Any,
                     running: _RunningOperator) -> None:
        """Wire an input signal binding to an operator.

        Args:
            binding:  Input signal binding with transport details.
            operator: The operator instance (must have a ``process`` method).
            running:  Running operator state for cleanup tracking.
        """
        if binding.transport == TRANSPORT_UDP:
            # Create a UDP receiver that feeds raw chunks to the operator.
            transport: UdpTransport = UdpTransport(
                listen_port=binding.udp_port,
            )

            def on_raw_frame(frame: Any, addr: tuple[str, int]) -> None:
                """Forward raw UDP payload to the operator."""
                if hasattr(operator, "process"):
                    operator.process(frame.payload)

            from .udp_channel import UdpReceiver
            # We need raw frame access, not SignalValue.
            # Use the receiver directly.
            receiver: UdpReceiver = UdpReceiver(
                port=binding.udp_port,
            )
            receiver.add_callback(on_raw_frame)
            receiver.start()
            running.transports.append(receiver)

        elif binding.transport == TRANSPORT_MQTT:
            # MQTT input — subscribe to signal topic.
            mqtt_transport: MqttTransport = MqttTransport(
                broker=self._broker, port=self._port,
            )

            def on_signal(name: str, value: Any) -> None:
                """Forward MQTT signal to operator."""
                # Convert back to bytes if the operator expects raw data.
                if hasattr(operator, "process") and isinstance(value, list):
                    import struct
                    # Assume float32 array.
                    payload: bytes = struct.pack(
                        f"<{len(value)}f", *value,
                    )
                    operator.process(payload)

            mqtt_transport.subscribe(binding.signal_name, on_signal)
            mqtt_transport.start()
            running.transports.append(mqtt_transport)

    def _setup_output(self, binding: SignalBinding, operator: Any,
                      running: _RunningOperator) -> None:
        """Wire an operator's output signals to a transport.

        The operator writes to its internal SignalBus.  We intercept
        those writes and forward them to the configured transport.

        Args:
            binding:  Output signal binding with transport details.
            operator: The operator instance.
            running:  Running operator state for cleanup tracking.
        """
        if not hasattr(operator, "_agent_bus"):
            return

        bus: Any = operator._agent_bus

        if binding.transport == TRANSPORT_MQTT:
            # Route output signals through MQTT.
            mqtt_transport: MqttTransport = MqttTransport(
                broker=self._broker, port=self._port,
            )
            mqtt_transport.start()
            bus.add_transport("mqtt_out", mqtt_transport)
            bus.set_route(binding.signal_name, "mqtt_out")
            running.transports.append(mqtt_transport)

        elif binding.transport == TRANSPORT_UDP:
            # Route output signals through UDP.
            udp_transport: UdpTransport = UdpTransport(
                targets=[(binding.udp_ip, binding.udp_port)],
            )
            udp_transport.start()
            bus.add_transport("udp_out", udp_transport)
            bus.set_route(binding.signal_name, "udp_out")
            running.transports.append(udp_transport)

    # ------------------------------------------------------------------
    # Emitter lifecycle
    # ------------------------------------------------------------------

    def _start_emitter(self, assignment: WorkAssignment) -> None:
        """Instantiate and start a remote emitter from a work assignment.

        Creates the emitter from the registry, runs its full lifecycle
        (on_configure → on_open), and subscribes to the frame source
        (MQTT or UDP) to feed frames into on_emit().

        Args:
            assignment: The work assignment with ``emitter_type`` set.
        """
        with self._lock:
            if assignment.assignment_id in self._emitters:
                logger.warning(
                    "Emitter assignment '%s' already running",
                    assignment.assignment_id,
                )
                return

        emitter_type: str = assignment.emitter_type or ""
        logger.info(
            "Starting emitter '%s' (assignment %s)",
            emitter_type, assignment.assignment_id,
        )

        running: _RunningEmitter = _RunningEmitter(
            assignment.assignment_id, emitter_type,
        )

        try:
            # Import emitter framework.
            from emitters import create_emitter

            # Create the emitter instance from the registry.
            emitter_name: str = assignment.emitter_config.get(
                "name", f"{self._node_id}:{emitter_type}",
            )
            emitter: Any = create_emitter(
                emitter_type, emitter_name, assignment.emitter_config,
            )
            running.emitter = emitter

            # Deferred configuration with full assignment config.
            emitter.on_configure(assignment.emitter_config)

            # Open: acquire local resources (sockets, audio devices, etc.).
            emitter.on_open()
            emitter._is_open = True

            # Wire frame delivery from input bindings.
            for inp in assignment.inputs:
                self._setup_emitter_input(inp, running)

        except Exception as exc:
            logger.error(
                "Failed to start emitter '%s': %s", emitter_type, exc,
            )
            # Clean up partial setup.
            for t in running.transports:
                try:
                    t.stop()
                except Exception as exc:
                    logger.debug("Error stopping emitter transport during cleanup: %s", exc)
            if running.emitter is not None and running.emitter._is_open:
                try:
                    running.emitter.on_close()
                except Exception as exc:
                    logger.debug("Error closing emitter during cleanup: %s", exc)
            return

        with self._lock:
            self._emitters[assignment.assignment_id] = running

        logger.info(
            "Emitter '%s' running (assignment %s, name '%s')",
            emitter_type, assignment.assignment_id,
            emitter_name,
        )

    def _setup_emitter_input(self, binding: SignalBinding,
                             running: _RunningEmitter) -> None:
        """Wire a frame source to a running emitter.

        Subscribes to the frame topic (MQTT) or port (UDP) and
        dispatches received frames to the emitter's ``on_emit()``.

        Args:
            binding: Input binding describing where frames arrive from.
            running: Running emitter state for cleanup tracking.
        """
        emitter: Any = running.emitter

        if binding.transport == TRANSPORT_MQTT:
            # MQTT frame delivery — JSON payload with frame + metadata.
            mqtt_transport: MqttTransport = MqttTransport(
                broker=self._broker, port=self._port,
            )

            def on_frame_mqtt(name: str, value: Any) -> None:
                """Dispatch an MQTT-delivered frame to the emitter."""
                if emitter is None:
                    return
                # Value is the deserialized JSON.  It may be a dict
                # with "frame" and "metadata" keys, or a raw value.
                if isinstance(value, dict) and "frame" in value:
                    frame: Any = value["frame"]
                    metadata: dict[str, Any] = value.get("metadata", {})
                else:
                    frame = value
                    metadata = {}
                self._dispatch_to_emitter(running, frame, metadata)

            mqtt_transport.subscribe(binding.signal_name, on_frame_mqtt)
            mqtt_transport.start()
            running.transports.append(mqtt_transport)

        elif binding.transport == TRANSPORT_UDP:
            # UDP frame delivery — binary SignalFrame.
            from .udp_channel import UdpReceiver

            receiver: UdpReceiver = UdpReceiver(
                port=binding.udp_port,
            )

            def on_frame_udp(frame: Any, addr: tuple[str, int]) -> None:
                """Dispatch a UDP-delivered frame to the emitter."""
                if emitter is None:
                    return
                # Unpack the SignalFrame payload based on dtype.
                from .protocol import DTYPE_FLOAT32, DTYPE_JSON
                payload: bytes = frame.payload
                if frame.dtype == DTYPE_FLOAT32:
                    # Unpack as float32 array.
                    count: int = len(payload) // 4
                    values: list[float] = list(
                        struct.unpack(f"<{count}f", payload[:count * 4])
                    )
                    decoded_frame: Any = values
                elif frame.dtype == DTYPE_JSON:
                    try:
                        decoded_frame = json.loads(payload.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        return
                else:
                    # Raw bytes — pass through.
                    decoded_frame = payload
                self._dispatch_to_emitter(running, decoded_frame, {})

            receiver.add_callback(on_frame_udp)
            receiver.start()
            running.transports.append(receiver)

    def _dispatch_to_emitter(self, running: _RunningEmitter,
                             frame: Any,
                             metadata: dict[str, Any]) -> None:
        """Call on_emit() on the emitter and track the result.

        Args:
            running:  Running emitter state.
            frame:    Frame data to emit.
            metadata: Per-frame context dict.
        """
        emitter: Any = running.emitter
        if emitter is None:
            return

        running.frame_count += 1
        try:
            success: bool = emitter.on_emit(frame, metadata)
        except Exception:
            success = False
            logger.warning(
                "Emitter '%s' raised exception in on_emit",
                running.emitter_type, exc_info=True,
            )
        if not success:
            running.failure_count += 1

    def _teardown_emitter(self, running: _RunningEmitter) -> None:
        """Flush, close, and clean up a running emitter.

        Args:
            running: The running emitter state to tear down.
        """
        # Stop frame delivery transports first.
        for t in running.transports:
            try:
                t.stop()
            except Exception as exc:
                logger.error("Error stopping emitter transport: %s", exc)

        # Emitter lifecycle: flush then close.
        emitter: Any = running.emitter
        if emitter is not None and emitter._is_open:
            try:
                emitter.on_flush()
            except Exception as exc:
                logger.debug("Error flushing emitter: %s", exc)
            try:
                emitter.on_close()
                emitter._is_open = False
            except Exception as exc:
                logger.error(
                    "Error closing emitter '%s': %s",
                    running.emitter_type, exc,
                )

        logger.info(
            "Stopped emitter '%s' (assignment %s, frames=%d, failures=%d)",
            running.emitter_type, running.assignment_id,
            running.frame_count, running.failure_count,
        )

    # ------------------------------------------------------------------
    # Health monitoring
    # ------------------------------------------------------------------

    def _health_loop(self) -> None:
        """Publish periodic health metrics."""
        while not self._stop_event.is_set():
            self._publish_health()
            self._stop_event.wait(HEALTH_INTERVAL)

    def _publish_health(self) -> None:
        """Gather and publish health metrics."""
        if not self._client:
            return

        with self._lock:
            active_ops: int = len(self._operators)
            active_emitters: int = len(self._emitters)

        uptime: float = time.monotonic() - self._start_time

        health: NodeHealth = NodeHealth(
            node_id=self._node_id,
            cpu_percent=self._get_cpu_percent(),
            memory_percent=self._get_memory_percent(),
            gpu_percent=self._get_gpu_percent(),
            active_ops=active_ops + active_emitters,
            uptime_s=uptime,
        )

        try:
            self._client.publish(
                health_topic(self._node_id),
                health.to_json(),
                qos=HEALTH_QOS,
            )
        except Exception as exc:
            logger.debug("Failed to publish health: %s", exc)

    def _get_cpu_percent(self) -> float:
        """Read CPU usage percentage.

        Returns:
            CPU usage (0-100), or -1 on failure.
        """
        try:
            load: tuple[float, ...] = os.getloadavg()
            # Normalize 1-minute load average by CPU count.
            cpu_count: int = os.cpu_count() or 1
            return min(100.0, (load[0] / cpu_count) * 100.0)
        except (OSError, AttributeError):
            return -1.0

    def _get_memory_percent(self) -> float:
        """Read memory usage percentage.

        Supports Linux (``/proc/meminfo``) and macOS (``vm_stat``).

        Returns:
            Memory usage (0-100), or -1 on failure.
        """
        if platform.system() == "Darwin":
            return self._get_memory_percent_darwin()

        # Linux: /proc/meminfo.
        try:
            with open("/proc/meminfo", "r") as f:
                lines: dict[str, int] = {}
                for line in f:
                    parts: list[str] = line.split()
                    if len(parts) >= 2:
                        key: str = parts[0].rstrip(":")
                        lines[key] = int(parts[1])
                total: int = lines.get("MemTotal", 1)
                available: int = lines.get("MemAvailable", total)
                return ((total - available) / total) * 100.0
        except (OSError, KeyError, ValueError, ZeroDivisionError):
            return -1.0

    def _get_memory_percent_darwin(self) -> float:
        """Read memory usage on macOS via ``vm_stat`` and ``sysctl``.

        Returns:
            Memory usage (0-100), or -1 on failure.
        """
        try:
            # Total physical memory via sysctl.
            result: subprocess.CompletedProcess[str] = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode != 0:
                return -1.0
            total_bytes: int = int(result.stdout.strip())

            # Page statistics via vm_stat.
            result = subprocess.run(
                ["vm_stat"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode != 0:
                return -1.0

            # Parse page size and page counts from vm_stat output.
            stats: dict[str, int] = {}
            page_size: int = 16384  # Default for Apple Silicon.
            for line in result.stdout.splitlines():
                if "page size of" in line:
                    # "Mach Virtual Memory Statistics: (page size of 16384 bytes)"
                    for word in line.split():
                        if word.isdigit():
                            page_size = int(word)
                            break
                elif ":" in line:
                    key, _, val = line.partition(":")
                    val = val.strip().rstrip(".")
                    if val.isdigit():
                        stats[key.strip()] = int(val)

            # Free + inactive + speculative ≈ available.
            free_pages: int = stats.get("Pages free", 0)
            inactive_pages: int = stats.get("Pages inactive", 0)
            speculative_pages: int = stats.get("Pages speculative", 0)
            available_bytes: int = (
                (free_pages + inactive_pages + speculative_pages) * page_size
            )

            if total_bytes <= 0:
                return -1.0
            return ((total_bytes - available_bytes) / total_bytes) * 100.0

        except (OSError, ValueError, subprocess.TimeoutExpired):
            return -1.0

    def _get_gpu_percent(self) -> float:
        """Read GPU utilization percentage (Jetson tegrastats or nvidia-smi).

        Returns:
            GPU usage (0-100), or -1 if no GPU or unreadable.
        """
        # Try nvidia-smi first (desktop GPUs).
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                return float(result.stdout.strip().split("\n")[0])
        except (FileNotFoundError, subprocess.TimeoutExpired,
                ValueError, IndexError):
            pass

        # Try Jetson tegrastats sysfs (multiple known paths).
        for gpu_path in (
            "/sys/devices/platform/gpu.0/load",
            "/sys/devices/gpu.0/load",
            "/sys/devices/platform/bus@0/17000000.gpu/load",
        ):
            try:
                with open(gpu_path, "r") as f:
                    return float(f.read().strip()) / 10.0  # Tegra reports 0-1000.
            except (OSError, ValueError):
                continue

        return -1.0

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        """Clean shutdown: stop operators and emitters, disconnect MQTT."""
        # Stop all operators.
        with self._lock:
            op_ids: list[str] = list(self._operators.keys())
        for aid in op_ids:
            self._stop_operator(aid)

        # Stop all emitters (flush → close lifecycle).
        with self._lock:
            em_ids: list[str] = list(self._emitters.keys())
        for aid in em_ids:
            with self._lock:
                running: Optional[_RunningEmitter] = self._emitters.pop(
                    aid, None,
                )
            if running is not None:
                self._teardown_emitter(running)

        # Publish offline status.
        if self._client:
            try:
                self._client.publish(
                    status_topic(self._node_id),
                    STATUS_OFFLINE,
                    qos=STATUS_QOS,
                    retain=True,
                )
            except Exception as exc:
                logger.debug("Failed to publish offline status: %s", exc)
            self._client.loop_stop()
            self._client.disconnect()

        if self._health_thread and self._health_thread.is_alive():
            self._health_thread.join(timeout=3.0)

        logger.info("Worker agent '%s' shut down", self._node_id)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="GlowUp worker agent — compute and emitter node daemon.",
        epilog="Press Ctrl+C to stop.",
    )
    parser.add_argument(
        "config_file", nargs="?", default=None,
        help="Path to agent.json config file",
    )
    parser.add_argument(
        "--node-id", "-n", default=None,
        help="Node identifier (default: hostname)",
    )
    parser.add_argument(
        "--broker", "-b", default=DEFAULT_BROKER,
        help=f"MQTT broker address (default: {DEFAULT_BROKER})",
    )
    parser.add_argument(
        "--port", "-p", type=int, default=DEFAULT_PORT,
        help=f"MQTT broker port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--roles", "-r", nargs="+", default=["compute"],
        help="Node roles (default: compute)",
    )
    return parser.parse_args()


def main() -> int:
    """Entry point for the worker agent."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    args: argparse.Namespace = _parse_args()

    # Load config from file or CLI args.
    config: dict[str, Any] = {}
    if args.config_file:
        try:
            with open(args.config_file, "r") as f:
                config = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error reading config file: {exc}", file=sys.stderr)
            return 1

    # CLI args override config file.
    if args.node_id:
        config["node_id"] = args.node_id
    if "node_id" not in config:
        config["node_id"] = socket.gethostname()
    config.setdefault("mqtt_broker", args.broker)
    config.setdefault("mqtt_port", args.port)
    config.setdefault("roles", args.roles)

    agent: WorkerAgent = WorkerAgent(config)

    # Handle Ctrl+C.
    signal.signal(signal.SIGINT, lambda *_: agent.stop())
    signal.signal(signal.SIGTERM, lambda *_: agent.stop())

    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
