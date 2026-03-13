"""GlowUp distributed compute subsystem.

Provides the two-tier transport and orchestration layer for distributing
signal processing across a fleet of compute nodes.  The architecture
separates concerns into two planes:

**Control plane (MQTT):**
    Capability registration, work assignments, health monitoring, and
    low-frequency derived signals (beat, BPM, scene triggers).  Uses
    the existing MQTT broker on the Pi.

**Data plane (UDP):**
    High-rate raw data streams (PCM audio, video frames, sensor arrays).
    Direct unicast between nodes — no broker overhead, binary wire format,
    fire-and-forget delivery.  Falls back from multicast to unicast when
    mesh routers block multicast.

The SignalBus API is unchanged: ``bus.write(name, value)`` and
``bus.read(name, default)`` work identically regardless of which
transport carries the data.  The orchestrator decides transport
per signal in the work assignment; the bus routes accordingly.

Modules:
    protocol           — UDP binary wire format (pack/unpack)
    udp_channel        — UdpSender / UdpReceiver socket wrappers
    transport_adapter  — TransportAdapter ABC, MqttTransport, UdpTransport
    capability         — NodeCapability dataclass and serialization
    orchestrator       — Orchestrator: fleet management + work assignment
    worker_agent       — WorkerAgent daemon for compute nodes
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"
