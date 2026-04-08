"""Power logger — records Zigbee smart plug readings to SQLite.

Subscribes to the SignalBus for power-related signals from Zigbee
smart plugs and stores readings in a SQLite database.  Provides
query methods for the /power dashboard.

Data retention: 7 days.  Older records are pruned automatically.

Schema::

    CREATE TABLE power_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device TEXT NOT NULL,
        timestamp REAL NOT NULL,
        power REAL,
        voltage REAL,
        current_a REAL,
        energy REAL,
        power_factor REAL
    );
    CREATE INDEX idx_power_device_ts ON power_readings(device, timestamp);
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import os
import sqlite3
import threading
import time
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.power_logger")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default database path (alongside server.json).
DEFAULT_DB_PATH: str = "/etc/glowup/power.db"

# Data retention in seconds (7 days).
RETENTION_SECONDS: float = 7 * 24 * 3600

# Minimum interval between writes for the same device (seconds).
# Prevents flooding the DB when Z2M reports rapidly.
MIN_WRITE_INTERVAL: float = 5.0

# Prune old records every N writes.
PRUNE_EVERY: int = 100

# Power-related signal property names from the Zigbee adapter.
POWER_PROPERTIES: set[str] = {
    "power", "voltage", "current", "energy", "power_factor",
    "ac_frequency",
}

# Schema version — stored in SQLite user_version pragma.
SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# PowerLogger
# ---------------------------------------------------------------------------

class PowerLogger:
    """Records smart plug power readings to SQLite.

    Thread-safe.  Designed to be called from the Zigbee adapter's
    signal bus write path.

    Args:
        db_path: Path to the SQLite database file.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        """Initialize the power logger.

        Args:
            db_path: Path to the SQLite database file.
        """
        self._db_path: str = db_path
        self._lock: threading.Lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._write_count: int = 0
        # Per-device last-write timestamp to throttle writes.
        self._last_write: dict[str, float] = {}
        # Per-device CARRY-FORWARD state.  Holds the most recent value
        # of every property ever seen for the device — NEVER popped.
        # Each new record() merges the incoming property into this
        # dict; each write snapshots the dict into a row.  This is
        # the fix for the production NULL-column bug observed
        # 2026-04-08: ThirdReality plugs do change-based reporting,
        # so a single Z2M message can contain only `current` (no
        # power, no voltage).  The previous design popped _pending
        # on every write, so any subsequent partial message produced
        # rows with NULL columns for properties that did not happen
        # to arrive in that exact 5-second window.
        self._pending: dict[str, dict[str, float]] = {}
        # Per-device dirty flag — True if _pending has been modified
        # since the last write for this device.  flush_pending()
        # only writes a row when dirty, preventing the carry-forward
        # state from generating redundant snapshots when the device
        # has gone silent.
        self._dirty: dict[str, bool] = {}
        self._running: bool = True
        self._flush_thread: Optional[threading.Thread] = None
        self._open()
        self._start_flush_timer()

    def _start_flush_timer(self) -> None:
        """Start the background flush timer.

        Runs as a daemon thread, calling flush_pending() every
        MIN_WRITE_INTERVAL seconds to drain orphaned _pending data.
        """
        def _flush_loop() -> None:
            while self._running:
                # Interruptible sleep — check _running every second.
                remaining: float = MIN_WRITE_INTERVAL
                while remaining > 0 and self._running:
                    chunk: float = min(remaining, 1.0)
                    time.sleep(chunk)
                    remaining -= chunk
                if self._running:
                    self.flush_pending()

        self._flush_thread = threading.Thread(
            target=_flush_loop,
            daemon=True,
            name="power-logger-flush",
        )
        self._flush_thread.start()

    def _open(self) -> None:
        """Open the database and create tables if needed."""
        try:
            from infrastructure.timed_io import timed_io, IOClass
            with timed_io("power_logger.connect", IOClass.INSTANT):
                self._conn = sqlite3.connect(
                    self._db_path,
                    check_same_thread=False,
                    timeout=5,
                )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS power_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    power REAL,
                    voltage REAL,
                    current_a REAL,
                    energy REAL,
                    power_factor REAL
                )
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_power_device_ts
                ON power_readings(device, timestamp)
            """)
            self._conn.commit()
            logger.info("Power logger opened: %s", self._db_path)
        except Exception as exc:
            logger.error("Power logger DB open failed: %s", exc)
            self._conn = None

    def record(self, device: str, prop: str, value: float) -> None:
        """Record a power-related signal value.

        Merges the incoming property into the device's carry-forward
        state and writes a row when the throttle interval has passed.

        Carry-forward semantics:  ThirdReality smart plugs (and most
        Zigbee devices) do change-based reporting — a Z2M message can
        contain any subset of the device's properties, only those
        that have changed beyond the report-on-change threshold since
        the last sample.  The PowerLogger therefore cannot treat each
        record() call as a complete snapshot.  Instead, ``_pending``
        holds the most recently observed value of every property
        ever seen for the device, and each row written is a snapshot
        of that dict at write time.  Properties not present in the
        current message inherit their previous value.  This is the
        fix for the 2026-04-08 NULL-column bug, when the dashboard
        appeared to show LRTV "drops to 0 W" while the TV was on —
        those rows were not 0 W readings, they were sparse messages
        producing NULL columns that the chart's ``|| 0`` JS coercion
        rendered as zero.

        A device whose plug stops reporting will eventually carry
        the same row contents forward indefinitely; ``flush_pending``
        consults the per-device ``_dirty`` flag to avoid writing
        redundant snapshots when nothing has actually changed.

        Args:
            device: Device friendly name (e.g., ``ML_Power``).
            prop:   Property name (``power``, ``voltage``, etc.).
            value:  The numeric value.
        """
        if self._conn is None:
            return
        if prop not in POWER_PROPERTIES:
            return

        with self._lock:
            if device not in self._pending:
                self._pending[device] = {}
            self._pending[device][prop] = value
            self._dirty[device] = True

            # Throttle: only write if enough time has passed.
            now: float = time.time()
            last: float = self._last_write.get(device, 0.0)
            if now - last < MIN_WRITE_INTERVAL:
                return

            # Snapshot the carry-forward state.  Do NOT pop —
            # ``_pending`` must persist across writes so that
            # properties seen earlier remain available when later
            # messages contain only a subset of fields.
            readings: dict[str, float] = dict(self._pending[device])
            if not readings:
                return

            self._last_write[device] = now
            self._dirty[device] = False
            try:
                self._conn.execute(
                    """INSERT INTO power_readings
                       (device, timestamp, power, voltage, current_a,
                        energy, power_factor)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        device,
                        now,
                        readings.get("power"),
                        readings.get("voltage"),
                        readings.get("current"),
                        readings.get("energy"),
                        readings.get("power_factor"),
                    ),
                )
                self._conn.commit()
                self._write_count += 1

                # Periodic prune.
                if self._write_count % PRUNE_EVERY == 0:
                    self._prune()
            except Exception as exc:
                logger.warning("Power logger write failed: %s", exc)

    def flush_pending(self) -> None:
        """Flush any pending dirty carry-forward state on a timer.

        Called by a background timer to ensure that if a device is
        updated once and then goes quiet (e.g., MqttSignalBus dedup
        suppresses subsequent identical values, or the device
        actually stops reporting), the most recent state still lands
        in the database within ``MIN_WRITE_INTERVAL``.

        Carry-forward interaction:  Because ``record()`` no longer
        pops ``_pending``, this method must NOT write on every poll
        cycle — that would generate a steady stream of redundant
        snapshots when the device is silent and pollute the database.
        The per-device ``_dirty`` flag prevents this: it is set
        ``True`` on every ``record()`` and cleared when a row is
        written (either here or in ``record()``).  flush_pending
        only emits a row when ``_dirty`` is ``True``, so each
        burst of activity produces at most one extra row beyond what
        the inline throttle in ``record()`` would have written.

        Thread-safe.  Respects per-device throttle.
        """
        if self._conn is None:
            return

        now: float = time.time()
        with self._lock:
            # Snapshot device list to avoid mutating dict during iteration.
            devices: list[str] = list(self._pending.keys())
            for device in devices:
                # Skip silent devices — nothing new since last write.
                if not self._dirty.get(device, False):
                    continue

                last: float = self._last_write.get(device, 0.0)
                if now - last < MIN_WRITE_INTERVAL:
                    continue

                # Snapshot, do not pop.  Carry-forward state must
                # persist for the next message that may be sparse.
                readings: dict[str, float] = dict(self._pending[device])
                if not readings:
                    continue

                self._last_write[device] = now
                self._dirty[device] = False
                try:
                    self._conn.execute(
                        """INSERT INTO power_readings
                           (device, timestamp, power, voltage, current_a,
                            energy, power_factor)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            device,
                            now,
                            readings.get("power"),
                            readings.get("voltage"),
                            readings.get("current"),
                            readings.get("energy"),
                            readings.get("power_factor"),
                        ),
                    )
                    self._conn.commit()
                    self._write_count += 1

                    if self._write_count % PRUNE_EVERY == 0:
                        self._prune()
                except Exception as exc:
                    logger.warning("Power logger flush failed: %s", exc)

    def _prune(self) -> None:
        """Remove records older than RETENTION_SECONDS."""
        if self._conn is None:
            return
        cutoff: float = time.time() - RETENTION_SECONDS
        try:
            cursor = self._conn.execute(
                "DELETE FROM power_readings WHERE timestamp < ?",
                (cutoff,),
            )
            self._conn.commit()
            if cursor.rowcount > 0:
                logger.info(
                    "Power logger pruned %d old record(s)", cursor.rowcount,
                )
        except Exception as exc:
            logger.warning("Power logger prune failed: %s", exc)

    def query(
        self,
        device: Optional[str] = None,
        hours: float = 1.0,
        resolution: int = 300,
    ) -> list[dict[str, Any]]:
        """Query power readings for charting.

        Returns averaged readings at the given resolution (seconds per
        data point).  Default: 5-minute buckets over the last hour.

        Args:
            device:     Device name filter (None = all devices).
            hours:      How many hours of history to return.
            resolution: Bucket size in seconds for averaging.

        Returns:
            List of dicts with ``timestamp``, ``device``, ``power``,
            ``voltage``, ``current_a``, ``energy``, ``power_factor``.
        """
        if self._conn is None:
            return []

        since: float = time.time() - (hours * 3600)
        bucket_expr: str = f"CAST(timestamp / {resolution} AS INTEGER) * {resolution}"

        if device:
            sql = f"""
                SELECT {bucket_expr} AS bucket, device,
                       AVG(power) AS power,
                       AVG(voltage) AS voltage,
                       AVG(current_a) AS current_a,
                       MAX(energy) AS energy,
                       AVG(power_factor) AS power_factor
                FROM power_readings
                WHERE device = ? AND timestamp >= ?
                GROUP BY bucket, device
                ORDER BY bucket
            """
            params = (device, since)
        else:
            sql = f"""
                SELECT {bucket_expr} AS bucket, device,
                       AVG(power) AS power,
                       AVG(voltage) AS voltage,
                       AVG(current_a) AS current_a,
                       MAX(energy) AS energy,
                       AVG(power_factor) AS power_factor
                FROM power_readings
                WHERE timestamp >= ?
                GROUP BY bucket, device
                ORDER BY bucket
            """
            params = (since,)

        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)
                cols = [d[0] for d in cursor.description]
                return [dict(zip(cols, row)) for row in cursor.fetchall()]
            except Exception as exc:
                logger.warning("Power logger query failed: %s", exc)
                return []

    def summary(
        self, device: Optional[str] = None, days: int = 7,
    ) -> dict[str, Any]:
        """Return summary statistics for the dashboard.

        Args:
            device: Device name filter (None = all).
            days:   Number of days to summarize.

        Returns:
            Dict with ``total_kwh``, ``avg_watts``, ``peak_watts``,
            ``days_covered``, ``devices``.
        """
        if self._conn is None:
            return {}

        since: float = time.time() - (days * 86400)

        with self._lock:
            try:
                if device:
                    where = "WHERE device = ? AND timestamp >= ?"
                    params = (device, since)
                else:
                    where = "WHERE timestamp >= ?"
                    params = (since,)

                row = self._conn.execute(f"""
                    SELECT AVG(power) AS avg_watts,
                           MAX(power) AS peak_watts,
                           MAX(energy) - MIN(energy) AS total_kwh,
                           (MAX(timestamp) - MIN(timestamp)) / 86400.0 AS days_covered,
                           COUNT(DISTINCT device) AS device_count
                    FROM power_readings
                    {where}
                """, params).fetchone()

                if row is None:
                    return {}

                return {
                    "avg_watts": round(row[0] or 0, 1),
                    "peak_watts": round(row[1] or 0, 1),
                    "total_kwh": round(row[2] or 0, 2),
                    "days_covered": round(row[3] or 0, 1),
                    "device_count": row[4] or 0,
                }
            except Exception as exc:
                logger.warning("Power logger summary failed: %s", exc)
                return {}

    def devices(self) -> list[str]:
        """Return list of known device names."""
        if self._conn is None:
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT DISTINCT device FROM power_readings ORDER BY device"
                ).fetchall()
                return [r[0] for r in rows]
            except Exception as exc:
                logger.warning("Power logger devices query failed: %s", exc)
                return []

    def close(self) -> None:
        """Stop the flush timer and close the database connection."""
        self._running = False
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=MIN_WRITE_INTERVAL + 2)
            self._flush_thread = None
        # Final flush — write any remaining pending data.
        self.flush_pending()
        if self._conn:
            self._conn.close()
            self._conn = None
