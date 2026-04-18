"""Orchestrator — fleet management and work assignment.

Runs on the Pi alongside the GlowUp server.  Watches MQTT for
node capability and health messages, maintains a fleet inventory,
and assigns work to compute and emitter nodes when pipelines are
configured.

The orchestrator's job is deciding **who runs what** and **which
transport carries each signal**.  It does not touch the data plane
— signals and frames flow directly between nodes via UDP or MQTT.

Work assignments are published as MQTT messages to individual nodes.
Each assignment describes either:

**Compute assignment** (operator):
- What operator to run (e.g., ``AudioExtractor``)
- Input signals and their transport (MQTT or UDP endpoint)
- Output signals and their transport
- Operator configuration (bands, window size, etc.)

**Emitter assignment** (remote emitter):
- What emitter type to instantiate (e.g., ``audio_out``)
- Input frame source (MQTT topic or UDP endpoint)
- Emitter configuration (device-specific params)

The orchestrator allocates UDP ports from a configurable range to
prevent conflicts when multiple workers share a machine.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .capability import (
    NodeCapability, NodeHealth,
    NODE_TOPIC_PREFIX, CAPABILITY_SUFFIX, STATUS_SUFFIX,
    HEALTH_SUFFIX, ASSIGNMENT_SUFFIX,
    STATUS_ONLINE, STATUS_OFFLINE,
    ROLE_COMPUTE, ROLE_EMITTER,
    capability_topic, status_topic, assignment_topic,
)
from .udp_channel import UDP_DEFAULT_PORT, UDP_PORT_RANGE

logger: logging.Logger = logging.getLogger("glowup.distributed.orchestrator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT QoS for assignments (at-least-once — we need delivery).
ASSIGNMENT_QOS: int = 1

# Default transport for small derived signals.
TRANSPORT_MQTT: str = "mqtt"

# Default transport for high-rate raw data.
TRANSPORT_UDP: str = "udp"

# Health check interval — nodes that haven't reported health in
# this many seconds are considered stale.
HEALTH_STALE_THRESHOLD: float = 30.0

# Maximum number of concurrent assignments per node (prevents overload).
MAX_ASSIGNMENTS_PER_NODE: int = 4


# ---------------------------------------------------------------------------
# SignalBinding
# ---------------------------------------------------------------------------

@dataclass
class SignalBinding:
    """Describes one input or output signal with transport details.

    Attributes:
        signal_name:     Signal name (e.g. ``"mic:audio:pcm_raw"``).
        transport:       ``"mqtt"`` or ``"udp"``.
        udp_ip:          UDP target/listen IP (for UDP transport).
        udp_port:        UDP target/listen port (for UDP transport).
        dtype:           Wire data type (from protocol.py constants).
    """
    signal_name: str
    transport: str = TRANSPORT_MQTT
    udp_ip: str = ""
    udp_port: int = 0
    dtype: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict.

        Returns:
            Dictionary representation.
        """
        d: dict[str, Any] = {
            "signal_name": self.signal_name,
            "transport": self.transport,
        }
        if self.transport == TRANSPORT_UDP:
            d["udp_ip"] = self.udp_ip
            d["udp_port"] = self.udp_port
        if self.dtype:
            d["dtype"] = self.dtype
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SignalBinding":
        """Create from a dictionary.

        Args:
            d: Dictionary with binding fields.

        Returns:
            A :class:`SignalBinding` instance.
        """
        return cls(
            signal_name=d.get("signal_name", ""),
            transport=d.get("transport", TRANSPORT_MQTT),
            udp_ip=d.get("udp_ip", ""),
            udp_port=d.get("udp_port", 0),
            dtype=d.get("dtype", 0),
        )


# ---------------------------------------------------------------------------
# WorkAssignment
# ---------------------------------------------------------------------------

@dataclass
class WorkAssignment:
    """Instructions sent from orchestrator to a worker node.

    Published as an MQTT message to ``glowup/node/{node_id}/assignment``.

    Supports two modes:

    **Compute assignment** (``emitter_type`` is ``None``):
        The agent runs an operator that processes signals.
        ``operator_name`` and ``operator_config`` describe the operator.
        ``inputs``/``outputs`` describe signal routing.

    **Emitter assignment** (``emitter_type`` is set):
        The agent instantiates a remote emitter and feeds it frames.
        ``emitter_type`` selects the emitter from the registry.
        ``emitter_config`` provides emitter-specific parameters.
        ``inputs`` describes where frame data arrives from (MQTT topic
        or UDP port).  ``operator_name`` and ``outputs`` are ignored.

    Attributes:
        assignment_id:   Unique identifier for this assignment.
        operator_name:   Operator/extractor class name (compute mode).
        operator_config: Configuration dict for the operator (compute mode).
        inputs:          Input signal/frame bindings.
        outputs:         Output signal bindings (compute mode only).
        action:          ``"start"`` or ``"stop"``.
        emitter_type:    Emitter registry type (emitter mode).  When set,
                         the assignment is treated as an emitter assignment.
        emitter_config:  Emitter-specific configuration dict (emitter mode).
    """
    assignment_id: str
    operator_name: str = ""
    operator_config: dict[str, Any] = field(default_factory=dict)
    inputs: list[SignalBinding] = field(default_factory=list)
    outputs: list[SignalBinding] = field(default_factory=list)
    action: str = "start"
    emitter_type: Optional[str] = None
    emitter_config: dict[str, Any] = field(default_factory=dict)
    # Optional UDP port for low-latency frame delivery to emitter nodes.
    udp_port: Optional[int] = None

    @property
    def is_emitter_assignment(self) -> bool:
        """Whether this assignment targets a remote emitter.

        Returns:
            ``True`` if ``emitter_type`` is set.
        """
        return self.emitter_type is not None

    def to_json(self) -> str:
        """Serialize to JSON for MQTT publishing.

        Returns:
            Compact JSON string.
        """
        d: dict[str, Any] = {
            "assignment_id": self.assignment_id,
            "action": self.action,
        }
        if self.emitter_type is not None:
            # Emitter assignment.
            d["emitter_type"] = self.emitter_type
            d["emitter_config"] = self.emitter_config
            d["inputs"] = [b.to_dict() for b in self.inputs]
        else:
            # Compute assignment.
            d["operator_name"] = self.operator_name
            d["operator_config"] = self.operator_config
            d["inputs"] = [b.to_dict() for b in self.inputs]
            d["outputs"] = [b.to_dict() for b in self.outputs]
        return json.dumps(d, separators=(",", ":"))

    @classmethod
    def from_json(cls, data: str) -> Optional["WorkAssignment"]:
        """Deserialize from JSON.

        Args:
            data: JSON string from MQTT payload.

        Returns:
            A :class:`WorkAssignment` instance or ``None``.
        """
        try:
            d: dict[str, Any] = json.loads(data)
            return cls(
                assignment_id=d.get("assignment_id", ""),
                operator_name=d.get("operator_name", ""),
                operator_config=d.get("operator_config", {}),
                inputs=[
                    SignalBinding.from_dict(b)
                    for b in d.get("inputs", [])
                ],
                outputs=[
                    SignalBinding.from_dict(b)
                    for b in d.get("outputs", [])
                ],
                action=d.get("action", "start"),
                emitter_type=d.get("emitter_type"),
                emitter_config=d.get("emitter_config", {}),
            )
        except (json.JSONDecodeError, TypeError):
            return None


# ---------------------------------------------------------------------------
# FleetNode — internal tracking
# ---------------------------------------------------------------------------

@dataclass
class _FleetNode:
    """Internal state for a tracked compute node.

    Attributes:
        capability:     Last-received capability message.
        online:         Whether the node is currently online.
        last_health:    Last-received health metrics (or None).
        last_seen:      Monotonic timestamp of last message.
        assignments:    Active assignment IDs on this node.
    """
    capability: NodeCapability
    online: bool = False
    last_health: Optional[NodeHealth] = None
    last_seen: float = 0.0
    assignments: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """Manages the distributed compute fleet.

    Runs on the Pi alongside the GlowUp server.  Watches MQTT for
    node capability and health messages.  Assigns work to compute
    nodes when pipelines are configured via the REST API.

    Args:
        mqtt_client: A connected paho-mqtt client instance.
        config:      ``"distributed"`` section from server.json.
    """

    def __init__(self, mqtt_client: Any,
                 config: Optional[dict[str, Any]] = None) -> None:
        """Initialize the orchestrator.

        Args:
            mqtt_client: Connected paho-mqtt client.
            config:      Optional distributed config section.
        """
        self._client: Any = mqtt_client
        self._config: dict[str, Any] = config or {}
        self._fleet: dict[str, _FleetNode] = {}
        self._lock: threading.Lock = threading.Lock()
        self._running: bool = False

        # UDP port allocator.
        self._port_base: int = self._config.get(
            "udp_port_base", UDP_DEFAULT_PORT,
        )
        self._port_range: int = self._config.get(
            "udp_port_range", UDP_PORT_RANGE,
        )
        self._allocated_ports: set[int] = set()
        # Tracks which port was allocated for each assignment so we can
        # release ports when nodes go offline or assignments are cancelled.
        self._assignment_ports: dict[str, int] = {}

        # Assignment counter for unique IDs.
        self._assignment_counter: int = 0

    def start(self) -> None:
        """Subscribe to fleet management topics and start monitoring.

        Must be called after the MQTT client is connected.
        """
        if self._running:
            return
        self._running = True

        # Subscribe to all node capability, status, and health topics.
        self._client.subscribe(
            f"{NODE_TOPIC_PREFIX}+/{CAPABILITY_SUFFIX}", qos=1,
        )
        self._client.subscribe(
            f"{NODE_TOPIC_PREFIX}+/{STATUS_SUFFIX}", qos=1,
        )
        self._client.subscribe(
            f"{NODE_TOPIC_PREFIX}+/{HEALTH_SUFFIX}", qos=0,
        )

        # Register our message handler.
        self._client.message_callback_add(
            f"{NODE_TOPIC_PREFIX}+/{CAPABILITY_SUFFIX}",
            self._on_capability_msg,
        )
        self._client.message_callback_add(
            f"{NODE_TOPIC_PREFIX}+/{STATUS_SUFFIX}",
            self._on_status_msg,
        )
        self._client.message_callback_add(
            f"{NODE_TOPIC_PREFIX}+/{HEALTH_SUFFIX}",
            self._on_health_msg,
        )

        logger.info("Orchestrator started — listening for node capabilities")

    def stop(self) -> None:
        """Stop monitoring and clean up."""
        self._running = False
        logger.info("Orchestrator stopped")

    # ------------------------------------------------------------------
    # Fleet status
    # ------------------------------------------------------------------

    def get_fleet_status(self) -> dict[str, Any]:
        """Return the current fleet inventory.

        Returns:
            Dict with node statuses, suitable for API responses.
        """
        with self._lock:
            nodes: list[dict[str, Any]] = []
            for node_id, node in sorted(self._fleet.items()):
                entry: dict[str, Any] = {
                    "node_id": node_id,
                    "online": node.online,
                    "capability": node.capability.to_dict(),
                    "assignments": list(node.assignments),
                    "assignment_count": len(node.assignments),
                }
                if node.last_health:
                    entry["health"] = {
                        "cpu_percent": node.last_health.cpu_percent,
                        "memory_percent": node.last_health.memory_percent,
                        "gpu_percent": node.last_health.gpu_percent,
                        "active_ops": node.last_health.active_ops,
                        "uptime_s": node.last_health.uptime_s,
                    }
                nodes.append(entry)

            return {
                "nodes": nodes,
                "node_count": len(nodes),
                "online_count": sum(1 for n in self._fleet.values() if n.online),
                "total_assignments": sum(
                    len(n.assignments) for n in self._fleet.values()
                ),
                "allocated_ports": sorted(self._allocated_ports),
            }

    def get_online_nodes(self) -> list[str]:
        """Return IDs of all online nodes.

        Returns:
            Sorted list of online node ID strings.
        """
        with self._lock:
            return sorted(
                nid for nid, n in self._fleet.items() if n.online
            )

    def get_compute_nodes(self) -> list[str]:
        """Return IDs of online nodes with the ``compute`` role.

        Returns:
            Sorted list of compute-capable node IDs.
        """
        with self._lock:
            return sorted(
                nid for nid, n in self._fleet.items()
                if n.online and ROLE_COMPUTE in n.capability.roles
            )

    def get_emitter_nodes(self) -> list[str]:
        """Return IDs of online nodes with the ``emitter`` role.

        Returns:
            Sorted list of emitter-capable node IDs.
        """
        with self._lock:
            return sorted(
                nid for nid, n in self._fleet.items()
                if n.online and ROLE_EMITTER in n.capability.roles
            )

    def select_emitter_node(self,
                            emitter_type: str) -> Optional[str]:
        """Select the best available node that can run a given emitter type.

        Selection criteria:
        1. Node must be online with the ``emitter`` role.
        2. Node must advertise the requested emitter type in its
           capability ``emitters`` list.
        3. Node must have assignment capacity.
        4. Prefer lowest current assignment count.

        Args:
            emitter_type: Emitter registry type to match (e.g., ``"audio_out"``).

        Returns:
            Node ID of the selected node, or ``None`` if no suitable
            node is available.
        """
        with self._lock:
            candidates: list[tuple[str, _FleetNode]] = []
            for nid, n in self._fleet.items():
                if not n.online:
                    continue
                if ROLE_EMITTER not in n.capability.roles:
                    continue
                if len(n.assignments) >= MAX_ASSIGNMENTS_PER_NODE:
                    continue
                # Check if the node advertises this emitter type.
                node_emitter_types: list[str] = [
                    e.get("type", "") for e in n.capability.emitters
                ]
                if emitter_type not in node_emitter_types:
                    continue
                candidates.append((nid, n))

        if not candidates:
            return None

        # Prefer lowest assignment count.
        candidates.sort(key=lambda item: len(item[1].assignments))
        return candidates[0][0]

    # ------------------------------------------------------------------
    # Work assignment
    # ------------------------------------------------------------------

    def assign_work(self, node_id: str,
                    assignment: WorkAssignment) -> bool:
        """Send a work assignment to a specific node.

        Args:
            node_id:    Target node identifier.
            assignment: The work assignment to send.

        Returns:
            ``True`` if the assignment was published successfully.
        """
        with self._lock:
            node: Optional[_FleetNode] = self._fleet.get(node_id)
            if node is None:
                logger.error("Cannot assign work: node '%s' not found", node_id)
                return False
            if not node.online:
                logger.error("Cannot assign work: node '%s' is offline", node_id)
                return False
            if len(node.assignments) >= MAX_ASSIGNMENTS_PER_NODE:
                logger.error(
                    "Cannot assign work: node '%s' has %d assignments (max %d)",
                    node_id, len(node.assignments), MAX_ASSIGNMENTS_PER_NODE,
                )
                return False

        # Publish the assignment.
        topic: str = assignment_topic(node_id)
        payload: str = assignment.to_json()
        try:
            self._client.publish(topic, payload, qos=ASSIGNMENT_QOS)
        except Exception as exc:
            logger.error(
                "Failed to publish assignment to '%s': %s", node_id, exc,
            )
            return False

        # Track the assignment and its port allocation.
        with self._lock:
            if node_id in self._fleet:
                self._fleet[node_id].assignments.append(
                    assignment.assignment_id,
                )
                if assignment.udp_port:
                    self._assignment_ports[assignment.assignment_id] = (
                        assignment.udp_port
                    )

        what: str = (
            f"emitter:{assignment.emitter_type}"
            if assignment.is_emitter_assignment
            else f"operator:{assignment.operator_name}"
        )
        logger.info(
            "Assigned %s to node '%s' (id: %s)",
            what, node_id, assignment.assignment_id,
        )
        return True

    def cancel_assignment(self, node_id: str,
                          assignment_id: str) -> bool:
        """Cancel a work assignment on a node.

        Sends a stop message to the node and deallocates any UDP ports.

        Args:
            node_id:       Target node identifier.
            assignment_id: The assignment to cancel.

        Returns:
            ``True`` if the cancellation was published.
        """
        stop_msg: WorkAssignment = WorkAssignment(
            assignment_id=assignment_id,
            operator_name="",
            action="stop",
        )
        topic: str = assignment_topic(node_id)
        try:
            self._client.publish(topic, stop_msg.to_json(), qos=ASSIGNMENT_QOS)
        except Exception as exc:
            logger.error(
                "Failed to cancel assignment '%s' on '%s': %s",
                assignment_id, node_id, exc,
            )
            return False

        # Remove from tracking and release any allocated port.
        with self._lock:
            node: Optional[_FleetNode] = self._fleet.get(node_id)
            if node and assignment_id in node.assignments:
                node.assignments.remove(assignment_id)
            port: int = self._assignment_ports.pop(assignment_id, 0)
            if port:
                self._allocated_ports.discard(port)

        logger.info(
            "Cancelled assignment '%s' on node '%s'",
            assignment_id, node_id,
        )
        return True

    def select_compute_node(self,
                            operator_name: str = "") -> Optional[str]:
        """Select the best available compute node for work.

        Selection criteria (in order):
        1. Node must be online with the ``compute`` role.
        2. Node must have capacity (fewer than MAX_ASSIGNMENTS_PER_NODE).
        3. Prefer nodes with GPU resources.
        4. Prefer nodes with lowest current assignment count.

        Args:
            operator_name: Optional operator name for matching
                           node capabilities (future use).

        Returns:
            Node ID of the selected node, or ``None`` if no suitable
            node is available.
        """
        with self._lock:
            candidates: list[tuple[str, _FleetNode]] = [
                (nid, n) for nid, n in self._fleet.items()
                if n.online
                and ROLE_COMPUTE in n.capability.roles
                and len(n.assignments) < MAX_ASSIGNMENTS_PER_NODE
            ]

        if not candidates:
            return None

        # Sort by: has GPU (desc), assignment count (asc).
        def sort_key(item: tuple[str, _FleetNode]) -> tuple[int, int]:
            """Rank candidates: GPU nodes first, then fewest assignments."""
            nid, node = item
            has_gpu: int = 1 if node.capability.resources.get("gpus") else 0
            return (-has_gpu, len(node.assignments))

        candidates.sort(key=sort_key)
        return candidates[0][0]

    # ------------------------------------------------------------------
    # Port allocation
    # ------------------------------------------------------------------

    def allocate_port(self) -> Optional[int]:
        """Allocate a UDP port from the pool.

        Returns:
            An available port number, or ``None`` if the pool is exhausted.
        """
        with self._lock:
            for offset in range(self._port_range):
                port: int = self._port_base + offset
                if port not in self._allocated_ports:
                    self._allocated_ports.add(port)
                    return port
        logger.error("UDP port pool exhausted (base=%d, range=%d)",
                     self._port_base, self._port_range)
        return None

    def release_port(self, port: int) -> None:
        """Return a UDP port to the pool.

        Args:
            port: The port number to release.
        """
        with self._lock:
            self._allocated_ports.discard(port)

    # ------------------------------------------------------------------
    # Convenience: build an assignment
    # ------------------------------------------------------------------

    def next_assignment_id(self) -> str:
        """Generate a unique assignment ID.

        Returns:
            String like ``"assign-001"``.
        """
        with self._lock:
            self._assignment_counter += 1
            return f"assign-{self._assignment_counter:03d}"

    # ------------------------------------------------------------------
    # MQTT message handlers
    # ------------------------------------------------------------------

    def _on_capability_msg(self, client: Any, userdata: Any,
                           msg: Any) -> None:
        """Handle a node capability message."""
        try:
            payload: str = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            return

        cap: Optional[NodeCapability] = NodeCapability.from_json(payload)
        if cap is None:
            return

        with self._lock:
            if cap.node_id in self._fleet:
                self._fleet[cap.node_id].capability = cap
                self._fleet[cap.node_id].last_seen = time.monotonic()
            else:
                self._fleet[cap.node_id] = _FleetNode(
                    capability=cap,
                    online=True,
                    last_seen=time.monotonic(),
                )
        logger.info(
            "Node '%s' registered: roles=%s, ip=%s",
            cap.node_id, cap.roles, cap.ip,
        )

    def _on_status_msg(self, client: Any, userdata: Any,
                       msg: Any) -> None:
        """Handle a node status (online/offline) message."""
        try:
            payload: str = msg.payload.decode("utf-8").strip()
        except UnicodeDecodeError:
            return

        # Extract node_id from topic: glowup/node/{node_id}/status
        parts: list[str] = msg.topic.split("/")
        if len(parts) < 3:
            return
        node_id: str = parts[-2]

        with self._lock:
            if node_id in self._fleet:
                self._fleet[node_id].online = (payload == STATUS_ONLINE)
                self._fleet[node_id].last_seen = time.monotonic()

                if payload == STATUS_OFFLINE:
                    # Release ports allocated to the dead node's
                    # assignments, then clear the assignment list.
                    for aid in self._fleet[node_id].assignments:
                        port = self._assignment_ports.pop(aid, 0)
                        if port:
                            self._allocated_ports.discard(port)
                    self._fleet[node_id].assignments.clear()
                    logger.warning("Node '%s' went offline", node_id)
                else:
                    logger.info("Node '%s' came online", node_id)

    def _on_health_msg(self, client: Any, userdata: Any,
                       msg: Any) -> None:
        """Handle a node health metrics message."""
        try:
            payload: str = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            return

        health: Optional[NodeHealth] = NodeHealth.from_json(payload)
        if health is None:
            return

        with self._lock:
            if health.node_id in self._fleet:
                self._fleet[health.node_id].last_health = health
                self._fleet[health.node_id].last_seen = time.monotonic()
