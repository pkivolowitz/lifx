"""UDP sender and receiver for high-rate signal data.

Provides socket-level wrappers for the data plane.  The sender fires
frames to one or more targets; the receiver binds a port, runs a daemon
thread, and dispatches decoded frames to callbacks.

Both classes are thread-safe and designed for fire-and-forget delivery.
Sequence numbers (from :mod:`distributed.protocol`) let receivers detect
drops and discard out-of-order packets.

Multicast is attempted first (useful on flat networks); if the OS or
router rejects the join, the receiver falls back to unicast on the same
port.  The sender always works — it just sends to whatever address it's
given, multicast or unicast.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import socket
import struct
import threading
from typing import Callable, Optional

from .protocol import (
    SignalFrame, pack_signal_frame, unpack_signal_frame,
    MSG_SIGNAL_DATA, DTYPE_FLOAT32,
)

logger: logging.Logger = logging.getLogger("glowup.distributed.udp")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default base port for UDP data channels.
UDP_DEFAULT_PORT: int = 9420

# Port range for dynamic allocation by the orchestrator.
UDP_PORT_RANGE: int = 100

# Socket receive buffer size (bytes).  Large enough for a full UDP
# datagram (65535) with some headroom.
UDP_RECV_BUFFER: int = 65536

# Socket send/receive buffer size hint for the OS (256 KB).
# Prevents drops on burst writes.
SOCK_BUFFER_HINT: int = 262144

# Heartbeat interval in seconds (sender publishes, receiver monitors).
HEARTBEAT_INTERVAL: float = 5.0

# Heartbeat timeout — if no data or heartbeat arrives for this long,
# the receiver considers the sender offline.
HEARTBEAT_TIMEOUT: float = 15.0

# Multicast TTL (1 = local subnet only).
MULTICAST_TTL: int = 1

# Range check for multicast addresses (224.0.0.0 – 239.255.255.255).
MULTICAST_MIN: int = 0xE0000000   # 224.0.0.0
MULTICAST_MAX: int = 0xEFFFFFFF   # 239.255.255.255

# Frame callback type: receives the decoded SignalFrame and sender address.
FrameCallback = Callable[[SignalFrame, tuple[str, int]], None]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_multicast(ip: str) -> bool:
    """Check if an IP address is in the multicast range.

    Args:
        ip: IPv4 address string.

    Returns:
        ``True`` if the address is multicast (224.0.0.0/4).
    """
    try:
        packed: bytes = socket.inet_aton(ip)
        addr_int: int = struct.unpack("!I", packed)[0]
        return MULTICAST_MIN <= addr_int <= MULTICAST_MAX
    except (socket.error, struct.error):
        return False


# ---------------------------------------------------------------------------
# UdpSender
# ---------------------------------------------------------------------------

class UdpSender:
    """Send signal frames via UDP to one or more targets.

    Thread-safe.  Maintains a single UDP socket and a list of target
    ``(ip, port)`` tuples.  Frames are serialized via the wire protocol
    and sent without waiting for acknowledgement.

    Args:
        targets: Initial list of ``(ip, port)`` destinations.
    """

    def __init__(self, targets: Optional[list[tuple[str, int]]] = None) -> None:
        """Initialize the sender with optional target list.

        Args:
            targets: List of (ip, port) tuples to send to.
        """
        self._targets: list[tuple[str, int]] = list(targets) if targets else []
        self._socket: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM,
        )
        # Set multicast TTL for subnet-local delivery.
        self._socket.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MULTICAST_TTL,
        )
        # Increase send buffer.
        try:
            self._socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_BUFFER_HINT,
            )
        except OSError:
            pass  # Best-effort; some kernels restrict buffer sizes.
        self._sequence: int = 0
        self._lock: threading.Lock = threading.Lock()

    def add_target(self, ip: str, port: int) -> None:
        """Add a destination address.

        Args:
            ip:   Target IPv4 address (unicast or multicast).
            port: Target UDP port.
        """
        with self._lock:
            target: tuple[str, int] = (ip, port)
            if target not in self._targets:
                self._targets.append(target)

    def remove_target(self, ip: str, port: int) -> None:
        """Remove a destination address.

        Args:
            ip:   Target IPv4 address.
            port: Target UDP port.
        """
        with self._lock:
            try:
                self._targets.remove((ip, port))
            except ValueError:
                pass

    def send(self, name: str, payload: bytes,
             dtype: int = DTYPE_FLOAT32,
             msg_type: int = MSG_SIGNAL_DATA) -> int:
        """Serialize and send a signal frame to all targets.

        Args:
            name:     Signal name.
            payload:  Raw payload bytes.
            dtype:    Data type indicator.
            msg_type: Message type.

        Returns:
            Number of targets the frame was sent to.
        """
        with self._lock:
            seq: int = self._sequence
            self._sequence = (self._sequence + 1) & 0xFFFFFFFF
            targets: list[tuple[str, int]] = list(self._targets)

        try:
            frame: bytes = pack_signal_frame(name, payload, dtype, seq, msg_type)
        except ValueError as exc:
            logger.error("Failed to pack signal frame '%s': %s", name, exc)
            return 0

        sent: int = 0
        for target in targets:
            try:
                self._socket.sendto(frame, target)
                sent += 1
            except OSError as exc:
                logger.warning(
                    "UDP send to %s:%d failed: %s", target[0], target[1], exc,
                )

        return sent

    def close(self) -> None:
        """Close the underlying socket."""
        try:
            self._socket.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# UdpReceiver
# ---------------------------------------------------------------------------

class UdpReceiver:
    """Receive signal frames via UDP on a bound port.

    Runs a daemon thread that calls ``recvfrom()`` in a tight loop,
    unpacks frames via :func:`unpack_signal_frame`, and dispatches to
    registered callbacks.  Out-of-order packets (sequence number older
    than last seen for a given signal name) are dropped.

    Args:
        port:     UDP port to bind on.
        bind_ip:  IP to bind to (default ``"0.0.0.0"`` = all interfaces).
    """

    def __init__(self, port: int = UDP_DEFAULT_PORT,
                 bind_ip: str = "0.0.0.0") -> None:
        """Initialize the receiver.

        Args:
            port:    Port to listen on.
            bind_ip: Interface address to bind.
        """
        self._port: int = port
        self._bind_ip: str = bind_ip
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._callbacks: list[FrameCallback] = []
        self._lock: threading.Lock = threading.Lock()

        # Per-signal sequence tracking for out-of-order detection.
        self._last_seq: dict[str, int] = {}

    @property
    def port(self) -> int:
        """The UDP port this receiver is bound to."""
        return self._port

    def add_callback(self, callback: FrameCallback) -> None:
        """Register a callback for incoming frames.

        Args:
            callback: Function receiving ``(SignalFrame, (ip, port))``.
        """
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def remove_callback(self, callback: FrameCallback) -> None:
        """Unregister a frame callback.

        Args:
            callback: Previously registered callback.
        """
        with self._lock:
            try:
                self._callbacks.remove(callback)
            except ValueError:
                pass

    def start(self, multicast_group: Optional[str] = None) -> None:
        """Bind the socket and start the receiver thread.

        If *multicast_group* is provided and valid, the receiver
        attempts to join that multicast group.  On failure (e.g.,
        mesh router blocks IGMP), it falls back to unicast — the
        socket still works for direct sends.

        Args:
            multicast_group: Optional multicast IP to join.
        """
        if self._running:
            return

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(
            socket.SOL_SOCKET, socket.SO_REUSEADDR, 1,
        )
        # Increase receive buffer.
        try:
            self._socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_BUFFER_HINT,
            )
        except OSError:
            pass

        self._socket.bind((self._bind_ip, self._port))

        # Attempt multicast join if requested.
        if multicast_group and _is_multicast(multicast_group):
            try:
                mreq: bytes = (
                    socket.inet_aton(multicast_group)
                    + socket.inet_aton("0.0.0.0")
                )
                self._socket.setsockopt(
                    socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq,
                )
                logger.info(
                    "Joined multicast group %s on port %d",
                    multicast_group, self._port,
                )
            except OSError as exc:
                logger.warning(
                    "Multicast join failed for %s (falling back to unicast): %s",
                    multicast_group, exc,
                )

        # Set a receive timeout so the thread can check _running.
        self._socket.settimeout(1.0)

        self._running = True
        self._thread = threading.Thread(
            target=self._recv_loop,
            name=f"glowup-udp-recv-{self._port}",
            daemon=True,
        )
        self._thread.start()
        logger.info("UDP receiver started on port %d", self._port)

    def stop(self) -> None:
        """Stop the receiver thread and close the socket."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        self._last_seq.clear()

    def _recv_loop(self) -> None:
        """Main receive loop — runs in daemon thread."""
        while self._running and self._socket:
            try:
                data: bytes
                addr: tuple[str, int]
                data, addr = self._socket.recvfrom(UDP_RECV_BUFFER)
            except socket.timeout:
                continue  # Check _running flag.
            except OSError:
                if self._running:
                    logger.warning("UDP recv error on port %d", self._port)
                break

            # Decode the frame.
            frame: Optional[SignalFrame] = unpack_signal_frame(data)
            if frame is None:
                continue  # Silently drop malformed frames.

            # Out-of-order detection (per signal name).
            last: Optional[int] = self._last_seq.get(frame.name)
            if last is not None:
                # Allow wrap-around: if new seq is much smaller, it wrapped.
                diff: int = (frame.sequence - last) & 0xFFFFFFFF
                if diff > 0x7FFFFFFF:
                    # Sequence went backward — drop stale packet.
                    continue
            self._last_seq[frame.name] = frame.sequence

            # Dispatch to callbacks.
            with self._lock:
                callbacks: list[FrameCallback] = list(self._callbacks)
            for cb in callbacks:
                try:
                    cb(frame, addr)
                except Exception as exc:
                    logger.error(
                        "UDP callback error on signal '%s': %s",
                        frame.name, exc,
                    )
