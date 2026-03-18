"""Capability registration — nodes describe what they can do.

Each compute node publishes a :class:`NodeCapability` message to the
MQTT broker on startup.  The orchestrator reads these messages to
build a fleet inventory and make intelligent work assignment decisions.

Capabilities are published as retained MQTT messages so they survive
broker restarts.  Each node also sets an LWT (Last Will and Testament)
that the broker publishes automatically when the node disconnects
unexpectedly, allowing the orchestrator to detect offline nodes.

MQTT topics::

    glowup/node/{node_id}/capability   — retained JSON capability message
    glowup/node/{node_id}/status       — "online" or "offline" (LWT)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic prefix for node management.
NODE_TOPIC_PREFIX: str = "glowup/node/"

# Topic suffixes.
CAPABILITY_SUFFIX: str = "capability"
STATUS_SUFFIX: str = "status"
ASSIGNMENT_SUFFIX: str = "assignment"
HEALTH_SUFFIX: str = "health"

# Status payloads.
STATUS_ONLINE: str = "online"
STATUS_OFFLINE: str = "offline"

# MQTT QoS for capability messages.
CAPABILITY_QOS: int = 1

# Node roles.
ROLE_SENSOR: str = "sensor"
ROLE_COMPUTE: str = "compute"
ROLE_EMITTER: str = "emitter"
ROLE_ORCHESTRATOR: str = "orchestrator"

# Valid roles for validation.
VALID_ROLES: set[str] = {ROLE_SENSOR, ROLE_COMPUTE, ROLE_EMITTER, ROLE_ORCHESTRATOR}


# ---------------------------------------------------------------------------
# NodeCapability
# ---------------------------------------------------------------------------

@dataclass
class NodeCapability:
    """Describes what a node can do in the distributed fleet.

    Published to MQTT as a retained message so the orchestrator always
    has the latest state, even after reconnection.

    Attributes:
        node_id:    Unique identifier (e.g. ``"judy"``, ``"pi"``).
        hostname:   Network hostname (e.g. ``"judy.local"``).
        ip:         IPv4 address (e.g. ``"192.0.2.63"``).
        roles:      List of roles (``"sensor"``, ``"compute"``, ``"emitter"``).
        resources:  Hardware resources (GPUs, CPU cores, RAM).
        operators:  List of operator descriptors this node can run.
        emitters:   List of emitter descriptors this node exposes.
        version:    Agent software version.
        timestamp:  ISO 8601 time when capability was generated.
    """
    node_id: str
    hostname: str = ""
    ip: str = ""
    roles: list[str] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    operators: list[dict[str, Any]] = field(default_factory=list)
    emitters: list[dict[str, Any]] = field(default_factory=list)
    version: str = "1.0"
    timestamp: str = ""

    def __post_init__(self) -> None:
        """Set timestamp if not provided."""
        if not self.timestamp:
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def to_json(self) -> str:
        """Serialize to a JSON string for MQTT publishing.

        Returns:
            Compact JSON representation.
        """
        return json.dumps(self.to_dict(), separators=(",", ":"))

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-safe dictionary.

        Returns:
            Dict with all capability fields.
        """
        return {
            "node_id": self.node_id,
            "hostname": self.hostname,
            "ip": self.ip,
            "roles": self.roles,
            "resources": self.resources,
            "operators": self.operators,
            "emitters": self.emitters,
            "version": self.version,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_json(cls, data: str) -> Optional["NodeCapability"]:
        """Deserialize from a JSON string.

        Returns ``None`` for malformed data (rather than raising).

        Args:
            data: JSON string from MQTT payload.

        Returns:
            A :class:`NodeCapability` instance or ``None``.
        """
        try:
            d: dict[str, Any] = json.loads(data)
            return cls.from_dict(d)
        except (json.JSONDecodeError, TypeError, KeyError):
            return None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Optional["NodeCapability"]:
        """Create from a dictionary.

        Args:
            d: Dictionary with capability fields.

        Returns:
            A :class:`NodeCapability` instance or ``None`` if ``node_id``
            is missing.
        """
        node_id: str = d.get("node_id", "")
        if not node_id:
            return None
        return cls(
            node_id=node_id,
            hostname=d.get("hostname", ""),
            ip=d.get("ip", ""),
            roles=d.get("roles", []),
            resources=d.get("resources", {}),
            operators=d.get("operators", []),
            emitters=d.get("emitters", []),
            version=d.get("version", "1.0"),
            timestamp=d.get("timestamp", ""),
        )


# ---------------------------------------------------------------------------
# Topic helpers
# ---------------------------------------------------------------------------

def capability_topic(node_id: str) -> str:
    """Build the MQTT topic for a node's capability message.

    Args:
        node_id: Node identifier.

    Returns:
        Full MQTT topic string.
    """
    return f"{NODE_TOPIC_PREFIX}{node_id}/{CAPABILITY_SUFFIX}"


def status_topic(node_id: str) -> str:
    """Build the MQTT topic for a node's online/offline status.

    Args:
        node_id: Node identifier.

    Returns:
        Full MQTT topic string.
    """
    return f"{NODE_TOPIC_PREFIX}{node_id}/{STATUS_SUFFIX}"


def assignment_topic(node_id: str) -> str:
    """Build the MQTT topic for sending work assignments to a node.

    Args:
        node_id: Node identifier.

    Returns:
        Full MQTT topic string.
    """
    return f"{NODE_TOPIC_PREFIX}{node_id}/{ASSIGNMENT_SUFFIX}"


def health_topic(node_id: str) -> str:
    """Build the MQTT topic for a node's health metrics.

    Args:
        node_id: Node identifier.

    Returns:
        Full MQTT topic string.
    """
    return f"{NODE_TOPIC_PREFIX}{node_id}/{HEALTH_SUFFIX}"


# ---------------------------------------------------------------------------
# NodeHealth
# ---------------------------------------------------------------------------

@dataclass
class NodeHealth:
    """Periodic health metrics published by a worker agent.

    Published at regular intervals (e.g., every 5 seconds) so the
    orchestrator can detect stale nodes and monitor load.

    Attributes:
        node_id:       Node identifier.
        cpu_percent:   CPU usage percentage (0-100).
        memory_percent: RAM usage percentage (0-100).
        gpu_percent:   GPU utilization percentage (0-100, -1 if no GPU).
        active_ops:    Number of operators currently running.
        uptime_s:      Seconds since agent started.
        timestamp:     ISO 8601 time.
    """
    node_id: str
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    gpu_percent: float = -1.0
    active_ops: int = 0
    uptime_s: float = 0.0
    timestamp: str = ""

    def __post_init__(self) -> None:
        """Set timestamp if not provided."""
        if not self.timestamp:
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def to_json(self) -> str:
        """Serialize to JSON.

        Returns:
            Compact JSON string.
        """
        return json.dumps({
            "node_id": self.node_id,
            "cpu_percent": self.cpu_percent,
            "memory_percent": self.memory_percent,
            "gpu_percent": self.gpu_percent,
            "active_ops": self.active_ops,
            "uptime_s": self.uptime_s,
            "timestamp": self.timestamp,
        }, separators=(",", ":"))

    @classmethod
    def from_json(cls, data: str) -> Optional["NodeHealth"]:
        """Deserialize from JSON.

        Args:
            data: JSON string.

        Returns:
            A :class:`NodeHealth` instance or ``None``.
        """
        try:
            d: dict[str, Any] = json.loads(data)
            node_id: str = d.get("node_id", "")
            if not node_id:
                return None
            return cls(
                node_id=node_id,
                cpu_percent=d.get("cpu_percent", 0.0),
                memory_percent=d.get("memory_percent", 0.0),
                gpu_percent=d.get("gpu_percent", -1.0),
                active_ops=d.get("active_ops", 0),
                uptime_s=d.get("uptime_s", 0.0),
                timestamp=d.get("timestamp", ""),
            )
        except (json.JSONDecodeError, TypeError):
            return None
