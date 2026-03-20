"""ARP-based LIFX bulb discovery and UDP keepalive daemon.

Passively monitors the system ARP table for MAC addresses matching the
LIFX OUI (``d0:73:d5``).  When a new bulb IP appears, the daemon begins
sending periodic unicast ``GetService`` (type 2) packets to keep the
bulb's WiFi radio from entering deep power-save sleep.

**Why this works:** LIFX bulbs use ESP32 WiFi with aggressive power-save
(Max Modem Sleep).  Without periodic traffic, bulbs sleep for 10+ seconds
and miss UDP commands.  Apple's HomeKit sidesteps this with persistent
TCP connections; we achieve the same effect with lightweight UDP pings.

**Platform support:**

- **Linux** — reads ``/proc/net/arp`` directly (zero-cost, no subprocess).
- **macOS** — parses ``arp -a`` output (subprocess, used for dev machines).

**Database persistence:** When PostgreSQL is available (via the diagnostics
DSN), each newly discovered bulb is recorded in ``discovered_bulbs`` with
its IP, MAC, and first/last-seen timestamps.  This lets you track the
fleet and verify the keepalive is working.

Usage::

    from bulb_keepalive import BulbKeepAlive

    daemon = BulbKeepAlive(on_new_bulb=my_callback)
    daemon.start()
    # ... later ...
    daemon.stop()
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import asyncio
import logging
import os
import platform
import random
import re
import socket
import struct
import subprocess
import threading
import time
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: LIFX Organizationally Unique Identifier (first 3 octets of MAC).
LIFX_OUI: str = "d0:73:d5"

#: LIFX LAN protocol port.
LIFX_PORT: int = 56700

#: How often to scan the ARP table for new bulbs (seconds).
ARP_SCAN_INTERVAL: float = 5.0

#: How often to ping each known bulb with GetService (seconds).
#: Must be frequent enough to prevent deep sleep but not so aggressive
#: that it constitutes a flood.  Apple's HAP keepalive is ~60 s; we go
#: shorter because UDP has no delivery guarantee.
KEEPALIVE_INTERVAL: float = 15.0

#: Number of GetService packets per keepalive ping.
#: Multiple packets improve odds of hitting the DTIM window.
KEEPALIVE_BURST: int = 2

#: Delay between burst packets (seconds).
KEEPALIVE_BURST_DELAY: float = 0.05

#: Number of consecutive ARP misses before a bulb is considered expired.
#: At the default 5-second scan interval, 24 misses ≈ 2 minutes.
EXPIRY_MISS_COUNT: int = 24

#: How often to ping-sweep the entire subnet to populate the kernel ARP cache
#: (seconds).  The keepalive reads /proc/net/arp passively — it only sees
#: bulbs that have already sent traffic to this host.  On a Deco mesh network
#: bulbs associated with a distant node never appear until we ping them first.
SUBNET_SWEEP_INTERVAL: float = 60.0

#: LIFX protocol constants (duplicated from transport to keep this module
#: importable standalone — e.g. on machines without the full codebase).
_PROTOCOL: int = 1024
_ADDRESSABLE: int = 1 << 12
_TAGGED: int = 1 << 13
_HEADER_SIZE: int = 36
_MSG_GET_SERVICE: int = 2
_SOURCE_ID_MIN: int = 2
_SOURCE_ID_MAX: int = (1 << 32) - 1

#: SQL to create the discovered_bulbs table if it doesn't exist.
_CREATE_TABLE_SQL: str = """
    CREATE TABLE IF NOT EXISTS discovered_bulbs (
        ip          TEXT    NOT NULL,
        mac         TEXT    NOT NULL,
        first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (ip)
    )
"""

#: SQL to upsert a discovered bulb (insert or update last_seen).
_UPSERT_SQL: str = """
    INSERT INTO discovered_bulbs (ip, mac)
    VALUES (%s, %s)
    ON CONFLICT (ip) DO UPDATE
        SET mac = EXCLUDED.mac,
            last_seen = now()
"""

# Module logger.
logger: logging.Logger = logging.getLogger("glowup.bulb_keepalive")

# ---------------------------------------------------------------------------
# Optional dependency — lanscan (subnet ping sweep)
# ---------------------------------------------------------------------------

try:
    from lanscan import get_local_network, ping_sweep
    _HAS_LANSCAN: bool = True
except ImportError:
    _HAS_LANSCAN = False

# ---------------------------------------------------------------------------
# Optional dependency — psycopg2
# ---------------------------------------------------------------------------

try:
    import psycopg2
    _HAS_PSYCOPG2: bool = True
except ImportError:
    psycopg2 = None  # type: ignore[assignment]
    _HAS_PSYCOPG2 = False


# ---------------------------------------------------------------------------
# ARP table readers (platform-specific)
# ---------------------------------------------------------------------------

def _read_arp_linux() -> dict[str, str]:
    """Read ``/proc/net/arp`` and return {IP: MAC} for LIFX devices.

    ``/proc/net/arp`` columns: IP, HW type, Flags, HW address, Mask, Device.
    We filter on Flags != 0x0 (incomplete entries) and the LIFX OUI prefix.

    Returns:
        Mapping of IP address to lowercase MAC string for LIFX devices.
    """
    result: dict[str, str] = {}
    try:
        with open("/proc/net/arp", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                ip: str = parts[0]
                mac: str = parts[3].lower()
                # Skip incomplete entries (MAC is 00:00:00:00:00:00)
                if mac.startswith(LIFX_OUI):
                    result[ip] = mac
    except OSError as exc:
        logger.debug("Failed to read /proc/net/arp: %s", exc)
    return result


#: Regex matching a single arp -a output line on macOS.
#: Example: ? (10.0.0.2) at d0:73:d5:78:e5:c6 on en0 ifscope [ethernet]
_ARP_MAC_RE: re.Pattern[str] = re.compile(
    r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([\da-fA-F:]+)"
)


def _read_arp_macos() -> dict[str, str]:
    """Parse ``arp -a`` output and return {IP: MAC} for LIFX devices.

    Returns:
        Mapping of IP address to lowercase MAC string for LIFX devices.
    """
    result: dict[str, str] = {}
    try:
        output: str = subprocess.check_output(
            ["arp", "-an"], text=True, timeout=10.0,
        )
        for match in _ARP_MAC_RE.finditer(output):
            ip: str = match.group(1)
            mac: str = match.group(2).lower()
            if mac.startswith(LIFX_OUI):
                result[ip] = mac
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("Failed to run arp -a: %s", exc)
    return result


# Select the platform-appropriate reader at import time.
_IS_LINUX: bool = platform.system() == "Linux"
_read_arp: Callable[[], dict[str, str]] = (
    _read_arp_linux if _IS_LINUX else _read_arp_macos
)


# ---------------------------------------------------------------------------
# Database persistence
# ---------------------------------------------------------------------------

class _BulbDB:
    """Optional PostgreSQL persistence for discovered bulbs.

    Follows the same graceful-degradation pattern as
    :class:`diagnostics.DiagnosticsLogger`: if ``psycopg2`` is missing
    or the database is unreachable, all methods silently no-op.
    """

    def __init__(self) -> None:
        self._conn: Any = None
        self._lock: threading.Lock = threading.Lock()
        self._available: bool = False

    def connect(self) -> bool:
        """Attempt to connect using the diagnostics DSN.

        Returns:
            ``True`` if connected and the table exists, ``False`` otherwise.
        """
        if not _HAS_PSYCOPG2:
            logger.debug("BulbDB unavailable: psycopg2 not installed")
            return False

        # Reuse the same DSN resolution as the diagnostics subsystem.
        try:
            from network_config import net
            default_dsn: str = (
                f"postgresql://glowup:changeme@{net.db_host}:5432/glowup"
            )
        except ImportError:
            default_dsn = "postgresql://glowup:changeme@localhost:5432/glowup"

        dsn: str = os.environ.get("GLOWUP_DIAG_DSN", default_dsn)

        try:
            self._conn = psycopg2.connect(dsn)
            self._conn.autocommit = True
            # Ensure the table exists.
            with self._conn.cursor() as cur:
                cur.execute(_CREATE_TABLE_SQL)
            self._available = True
            logger.info("BulbDB connected — recording discoveries to PostgreSQL")
            return True
        except Exception as exc:
            logger.info("BulbDB unavailable (DB not required): %s", exc)
            self._conn = None
            self._available = False
            return False

    def record(self, ip: str, mac: str) -> None:
        """Upsert a discovered bulb (insert or bump last_seen).

        Args:
            ip:  Bulb IPv4 address.
            mac: Bulb MAC address (lowercase, colon-separated).
        """
        if not self._available:
            return
        with self._lock:
            for attempt in range(2):
                try:
                    if self._conn is None or self._conn.closed:
                        if not self._reconnect():
                            return
                    with self._conn.cursor() as cur:
                        cur.execute(_UPSERT_SQL, (ip, mac))
                    return
                except Exception as exc:
                    logger.debug(
                        "BulbDB record failed (attempt %d): %s",
                        attempt + 1, exc,
                    )
                    self._conn = None

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
                self._available = False

    def _reconnect(self) -> bool:
        """Re-establish the database connection after a failure."""
        try:
            from network_config import net
            default_dsn: str = (
                f"postgresql://glowup:changeme@{net.db_host}:5432/glowup"
            )
        except ImportError:
            default_dsn = "postgresql://glowup:changeme@localhost:5432/glowup"

        dsn: str = os.environ.get("GLOWUP_DIAG_DSN", default_dsn)
        try:
            self._conn = psycopg2.connect(dsn)
            self._conn.autocommit = True
            return True
        except Exception:
            self._conn = None
            return False


# ---------------------------------------------------------------------------
# Keepalive packet builder
# ---------------------------------------------------------------------------

def _build_getservice(source_id: int) -> bytes:
    """Build a 36-byte GetService (type 2) packet.

    This is a tagged, response-requested broadcast-style header that
    works equally well as a unicast probe.  The target is all-zeros
    (the device responds regardless when addressed by IP).

    Args:
        source_id: Session-unique source identifier (2 .. 2^32-1).

    Returns:
        A 36-byte LIFX header with no payload.
    """
    size: int = _HEADER_SIZE
    flags: int = _PROTOCOL | _ADDRESSABLE | _TAGGED
    frame: bytes = struct.pack("<HHI", size, flags, source_id)
    target: bytes = b'\x00' * 8
    reserved: bytes = b'\x00' * 6
    ack_res: int = 0x01  # res_required=1, ack_required=0
    seq: int = 0
    frame_addr: bytes = target + reserved + struct.pack("<BB", ack_res, seq)
    proto_header: bytes = struct.pack("<QHH", 0, _MSG_GET_SERVICE, 0)
    return frame + frame_addr + proto_header


# ---------------------------------------------------------------------------
# BulbKeepAlive daemon
# ---------------------------------------------------------------------------

class BulbKeepAlive(threading.Thread):
    """Background daemon that discovers LIFX bulbs via ARP and keeps them awake.

    The daemon runs two interleaved loops on a single thread:

    1. **ARP scan** — every :data:`ARP_SCAN_INTERVAL` seconds, reads the
       system ARP table and filters for the LIFX OUI.  New IPs are added
       to the known set and announced via the *on_new_bulb* callback.

    2. **Keepalive ping** — every :data:`KEEPALIVE_INTERVAL` seconds,
       sends a short burst of unicast ``GetService`` packets to each
       known bulb.  This keeps the bulb's WiFi radio active so that
       subsequent effect commands are received immediately.

    When PostgreSQL is available, each discovery event is recorded in
    the ``discovered_bulbs`` table with first/last-seen timestamps.

    Args:
        on_new_bulb: Optional callback invoked as ``on_new_bulb(ip, mac)``
                     when a previously-unknown LIFX bulb is found.  Called
                     from the daemon thread — keep it fast or dispatch.
        arp_interval:       Override for :data:`ARP_SCAN_INTERVAL`.
        keepalive_interval: Override for :data:`KEEPALIVE_INTERVAL`.
    """

    def __init__(
        self,
        on_new_bulb: Optional[Callable[[str, str], None]] = None,
        *,
        arp_interval: float = ARP_SCAN_INTERVAL,
        keepalive_interval: float = KEEPALIVE_INTERVAL,
        sweep_interval: float = SUBNET_SWEEP_INTERVAL,
    ) -> None:
        super().__init__(daemon=True, name="bulb-keepalive")
        self._on_new_bulb: Optional[Callable[[str, str], None]] = on_new_bulb
        self._arp_interval: float = arp_interval
        self._keepalive_interval: float = keepalive_interval
        self._sweep_interval: float = sweep_interval
        self._stop_event: threading.Event = threading.Event()
        # {IP: MAC} — currently-known LIFX bulbs.
        self._known: dict[str, str] = {}
        # {IP: int} — consecutive ARP misses per bulb.  Reset to 0
        # each time the bulb appears in the ARP table; incremented
        # each scan where it is absent.  Bulb is expired when the
        # count reaches EXPIRY_MISS_COUNT.
        self._misses: dict[str, int] = {}
        self._lock: threading.Lock = threading.Lock()
        self._source_id: int = random.randint(_SOURCE_ID_MIN, _SOURCE_ID_MAX)
        self._packet: bytes = _build_getservice(self._source_id)
        self._db: _BulbDB = _BulbDB()
        # Set after the first ARP scan completes, so callers can wait
        # for the daemon to have a populated device table.
        self._initial_scan_done: threading.Event = threading.Event()

    # -- Public API --------------------------------------------------------

    def wait_initial_scan(self, timeout: float = 30.0) -> bool:
        """Block until the first ARP scan has completed.

        The keepalive performs a subnet sweep followed by an ARP read
        on its first loop iteration.  This method lets startup code
        wait for that data before resolving config identifiers.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            ``True`` if the scan completed, ``False`` on timeout.
        """
        return self._initial_scan_done.wait(timeout=timeout)

    @property
    def known_bulbs(self) -> dict[str, str]:
        """Return a snapshot of {IP: MAC} for all discovered bulbs."""
        with self._lock:
            return dict(self._known)

    @property
    def known_bulbs_by_mac(self) -> dict[str, str]:
        """Return a snapshot of {MAC: IP} — reverse of :attr:`known_bulbs`.

        Used by :class:`DeviceRegistry` to resolve MAC addresses to
        current IP addresses at runtime.
        """
        with self._lock:
            return {mac: ip for ip, mac in self._known.items()}

    def ip_for_mac(self, mac: str) -> Optional[str]:
        """Return the current IP for a MAC address, or ``None`` if offline.

        Thread-safe single-device lookup.

        Args:
            mac: Lowercase colon-separated MAC (e.g. ``d0:73:d5:69:70:db``).
        """
        mac_lower: str = mac.lower()
        with self._lock:
            for ip, known_mac in self._known.items():
                if known_mac == mac_lower:
                    return ip
        return None

    def stop(self) -> None:
        """Signal the daemon to stop and wait for it to exit."""
        self._stop_event.set()

    # -- Thread body -------------------------------------------------------

    def run(self) -> None:
        """Main loop: interleave ARP scans and keepalive pings."""
        # Try to connect to DB — if it fails, we just skip persistence.
        self._db.connect()

        logger.info(
            "Keepalive daemon started — ARP every %.1fs, ping every %.1fs, "
            "subnet sweep every %.1fs",
            self._arp_interval, self._keepalive_interval, self._sweep_interval,
        )

        sock: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        next_arp: float = 0.0        # scan immediately on first tick
        next_ping: float = 0.0       # ping immediately on first tick
        next_sweep: float = 0.0      # sweep immediately on first tick

        try:
            while not self._stop_event.is_set():
                now: float = time.monotonic()

                # --- Subnet sweep (populate ARP cache before reading it) ---
                if now >= next_sweep:
                    self._sweep_subnet()
                    next_sweep = now + self._sweep_interval

                # --- ARP scan ---
                if now >= next_arp:
                    self._scan_arp()
                    next_arp = now + self._arp_interval
                    # Signal that the first scan is done so startup
                    # code waiting on wait_initial_scan() can proceed.
                    if not self._initial_scan_done.is_set():
                        self._initial_scan_done.set()
                        logger.info(
                            "Initial ARP scan complete — %d bulb(s) found",
                            len(self._known),
                        )

                # --- Keepalive ping ---
                if now >= next_ping:
                    self._ping_all(sock)
                    next_ping = now + self._keepalive_interval

                # Sleep until the next event, but wake on stop.
                sleep_for: float = (
                    min(next_arp, next_ping, next_sweep) - time.monotonic()
                )
                if sleep_for > 0:
                    self._stop_event.wait(timeout=sleep_for)
        finally:
            sock.close()
            self._db.close()
            logger.info("Keepalive daemon stopped (%d bulbs tracked)",
                        len(self._known))

    # -- Internals ---------------------------------------------------------

    def _sweep_subnet(self) -> None:
        """Ping every host on the local subnet to populate the kernel ARP cache.

        The keepalive reads ``/proc/net/arp`` passively — it only discovers
        bulbs that have already sent traffic to this host.  On a Deco mesh
        network, bulbs associated with a distant node may never appear in the
        ARP cache unless we reach out to them first.  This sweep sends one
        ICMP echo to every host on the subnet; the kernel records their MAC
        addresses in ``/proc/net/arp`` as replies arrive, making them visible
        on the next :meth:`_scan_arp` call.

        The sweep is a no-op if ``lanscan`` is not importable.
        """
        if not _HAS_LANSCAN:
            logger.debug("Subnet sweep skipped — lanscan not available")
            return
        try:
            network = get_local_network()
            if network is None:
                logger.debug("Subnet sweep: could not determine local network")
                return
            num_hosts: int = network.num_addresses - 2
            logger.debug(
                "Subnet sweep: pinging %d hosts on %s", num_hosts, network,
            )
            asyncio.run(ping_sweep(network))
            logger.debug("Subnet sweep complete")
        except Exception as exc:
            logger.debug("Subnet sweep failed: %s", exc)

    def _scan_arp(self) -> None:
        """Read the ARP table, register new devices, and expire stale ones."""
        arp_entries: dict[str, str] = _read_arp()
        with self._lock:
            # --- Process bulbs present in ARP ---
            for ip, mac in arp_entries.items():
                if ip not in self._known:
                    self._known[ip] = mac
                    self._misses[ip] = 0
                    logger.info(
                        "Discovered LIFX bulb %s (%s) via ARP", ip, mac,
                    )
                    self._db.record(ip, mac)
                    if self._on_new_bulb is not None:
                        try:
                            self._on_new_bulb(ip, mac)
                        except Exception:
                            logger.exception(
                                "on_new_bulb callback failed for %s", ip,
                            )
                else:
                    # Bulb still present — reset miss counter, bump DB.
                    self._misses[ip] = 0
                    self._db.record(ip, mac)

            # --- Increment miss counters for absent bulbs ---
            expired: list[str] = []
            for ip in list(self._known):
                if ip not in arp_entries:
                    self._misses[ip] = self._misses.get(ip, 0) + 1
                    if self._misses[ip] >= EXPIRY_MISS_COUNT:
                        expired.append(ip)

            # --- Remove expired bulbs ---
            for ip in expired:
                mac: str = self._known.pop(ip, "?")
                self._misses.pop(ip, None)
                logger.info(
                    "Expired LIFX bulb %s (%s) — absent from ARP for %d scans",
                    ip, mac, EXPIRY_MISS_COUNT,
                )

    def _ping_all(self, sock: socket.socket) -> None:
        """Send a keepalive burst to every known bulb."""
        with self._lock:
            targets: list[str] = list(self._known.keys())

        if not targets:
            return

        for ip in targets:
            for _ in range(KEEPALIVE_BURST):
                try:
                    sock.sendto(self._packet, (ip, LIFX_PORT))
                except OSError as exc:
                    logger.debug("Keepalive send to %s failed: %s", ip, exc)
                    break
                if KEEPALIVE_BURST > 1:
                    time.sleep(KEEPALIVE_BURST_DELAY)

        logger.debug("Pinged %d bulb(s)", len(targets))


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    def _report(ip: str, mac: str) -> None:
        print(f"NEW BULB: {ip}  ({mac})")

    daemon = BulbKeepAlive(on_new_bulb=_report)
    daemon.start()
    print("Keepalive daemon running — Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        daemon.stop()
        daemon.join(timeout=3.0)
        print(f"\nFinal bulb list: {daemon.known_bulbs}")
