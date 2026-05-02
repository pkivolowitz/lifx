"""Power logger — records Zigbee smart plug readings to PostgreSQL.

Subscribes to the SignalBus for power-related signals from Zigbee
smart plugs and stores readings in a PostgreSQL database.  Provides
query methods for the /power dashboard.

Data retention: 7 days.  Older records are pruned automatically.

Schema::

    CREATE TABLE IF NOT EXISTS power_readings (
        id BIGSERIAL PRIMARY KEY,
        device TEXT NOT NULL,
        timestamp DOUBLE PRECISION NOT NULL,
        power REAL,
        voltage REAL,
        current_a REAL,
        energy REAL,
        power_factor REAL
    );
    CREATE INDEX IF NOT EXISTS idx_power_device_ts
        ON power_readings(device, timestamp);
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import os
import threading
import time
from typing import Any, Optional

try:
    import psycopg2
    _HAS_PSYCOPG2: bool = True
except ImportError:
    _HAS_PSYCOPG2 = False

logger: logging.Logger = logging.getLogger("glowup.power_logger")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default PostgreSQL DSN.  Resolved (in priority order) from:
#   1. /etc/glowup/secrets.json key "postgres_dsn" (preferred)
#   2. GLOWUP_DIAG_DSN environment variable (legacy /etc/glowup/diag.env)
#   3. empty string -> caller passes None / fails fast
# No hardcoded credentials in source — that was a leak in the public
# repo (Perry's jail IP + a placeholder password) and would cause
# late failures on a deploy without proper secrets.
import os as _os  # avoid shadowing if `os` is imported below
from glowup_site import site as _site
DEFAULT_DSN: str = (
    _site.get("postgres_dsn")
    or _os.environ.get("GLOWUP_DIAG_DSN")
    or ""
)

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

_PG_DDL: str = """
CREATE TABLE IF NOT EXISTS power_readings (
    id BIGSERIAL PRIMARY KEY,
    device TEXT NOT NULL,
    timestamp DOUBLE PRECISION NOT NULL,
    power REAL,
    voltage REAL,
    current_a REAL,
    energy REAL,
    power_factor REAL
);
CREATE INDEX IF NOT EXISTS idx_power_device_ts
    ON power_readings(device, timestamp);
"""


# ---------------------------------------------------------------------------
# PowerLogger
# ---------------------------------------------------------------------------

class PowerLogger:
    """Records smart plug power readings to PostgreSQL.

    Thread-safe.  Designed to be called from the Zigbee adapter's
    signal bus write path.

    Args:
        dsn: PostgreSQL connection string.
    """

    def __init__(self, dsn: str = DEFAULT_DSN) -> None:
        """Initialize the power logger.

        Args:
            dsn: PostgreSQL connection string.
        """
        self._dsn: str = dsn
        self._lock: threading.Lock = threading.Lock()
        self._conn: Any = None
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
        """Open the PG connection and create tables if needed."""
        if not _HAS_PSYCOPG2:
            logger.info(
                "Power logger disabled — psycopg2 not installed "
                "(BASIC scope: no Postgres consumer)"
            )
            return
        if not self._dsn:
            # No DSN configured anywhere — site.json has no
            # ``postgres_dsn``, the GLOWUP_DIAG_DSN env var is unset,
            # and the installer didn't seed one.  This is the
            # default BASIC-install state, not an error.  A connect
            # attempt against an empty DSN falls back to libpq
            # defaults (local Unix socket) and emits the noisy
            # "could not connect ... PGSQL.5432 ... No such file"
            # ERROR every BASIC operator would see.  Stay quiet.
            logger.info(
                "Power logger disabled — no DSN configured "
                "(set 'postgres_dsn' in site.json or "
                "GLOWUP_DIAG_DSN env var to enable)"
            )
            return
        try:
            from infrastructure.timed_io import timed_io, IOClass
            with timed_io("power_logger.connect", IOClass.INSTANT):
                self._conn = psycopg2.connect(self._dsn, connect_timeout=10)
            self._conn.autocommit = True
            with self._conn.cursor() as cur:
                cur.execute(_PG_DDL)
            logger.info("Power logger connected: %s", self._dsn.split("@")[-1])
        except Exception as exc:
            # DSN was configured but connect failed — that's a real
            # operator-visible misconfiguration on a fleet host
            # (Postgres down, wrong credentials, network).  Keep
            # ERROR for that case; only the empty-DSN path above
            # downgrades to INFO.
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
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO power_readings
                           (device, timestamp, power, voltage, current_a,
                            energy, power_factor)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
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
                self._write_count += 1

                # Periodic prune.
                if self._write_count % PRUNE_EVERY == 0:
                    self._prune()
            except Exception as exc:
                logger.warning("Power logger write failed: %s", exc)

    def mark_offline(self, device: str) -> None:
        """Mark a device as offline and record the transition.

        Called by the Zigbee adapter when a device's availability
        flips from online to offline.  Two effects:

        1.  The carry-forward state (``_pending``) is cleared for the
            device so any subsequent retained-MQTT replay of a stale
            ``zigbee2mqtt/<device>`` payload cannot revive the old
            values.  If a retained replay does land after an offline
            mark, the adapter's availability gate drops it; this
            method is the second line of defense in case the gate
            fails.
        2.  A sentinel row is written to ``power_readings`` with
            ``timestamp=now`` and every numeric column ``NULL``.  This
            row sits as the most recent entry for the device, so the
            dashboard's last-reading render resolves to ``NULL``
            (rendered as the em-dash placeholder in the frontend)
            rather than the stale pre-offline values.  Existing
            historical rows are untouched — time-series charts keep
            their full history and the transition point is visible
            as a gap where the device went dark.

        The fix is specifically for the retained-MQTT-replay failure
        mode observed 2026-04-12: ML_Power's ``zigbee2mqtt/ML_Power``
        topic retained ``state=ON power=168.5`` from before broker-2's
        04-09 death, and every adapter reconnect replayed the same
        payload into ``power.db``, so the /power dashboard showed
        "ON 168.5 W" hours after the plug had been physically switched
        off.  The combination of the adapter-side availability gate
        plus this sentinel row eliminates both the ingest path
        (payload drop) and the render path (null rendering) of the
        bug.

        Args:
            device: Device friendly name (e.g., ``ML_Power``).
        """
        if self._conn is None:
            return
        now: float = time.time()
        with self._lock:
            # Drop carry-forward state so no subsequent flush_pending
            # resurrects pre-offline values.
            self._pending.pop(device, None)
            self._dirty.pop(device, None)
            self._last_write[device] = now
            try:
                # Idempotency: if the most recent row for this device
                # is already a NULL sentinel, skip writing another
                # one.  Retained ``:_availability`` signals will
                # deliver an offline state to every subscriber on
                # reconnect, and without this check every server
                # restart would append a fresh NULL row and pollute
                # the DB with duplicates.
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT power, voltage, current_a, energy, "
                        "power_factor FROM power_readings "
                        "WHERE device = %s ORDER BY timestamp DESC LIMIT 1",
                        (device,),
                    )
                    row = cur.fetchone()
                    if row is not None and all(v is None for v in row):
                        logger.debug(
                            "Power logger: %s already marked offline "
                            "(most recent row is a NULL sentinel)",
                            device,
                        )
                        return

                    cur.execute(
                        """INSERT INTO power_readings
                           (device, timestamp, power, voltage, current_a,
                            energy, power_factor)
                           VALUES (%s, %s, NULL, NULL, NULL, NULL, NULL)""",
                        (device, now),
                    )
                self._write_count += 1
                logger.info(
                    "Power logger: marked %s offline (sentinel NULL row)",
                    device,
                )
            except Exception as exc:
                logger.warning(
                    "Power logger offline mark failed for %s: %s",
                    device, exc,
                )

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
                    with self._conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO power_readings
                               (device, timestamp, power, voltage, current_a,
                                energy, power_factor)
                               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
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
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM power_readings WHERE timestamp < %s",
                    (cutoff,),
                )
                if cur.rowcount > 0:
                    logger.info(
                        "Power logger pruned %d old record(s)", cur.rowcount,
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

        if device:
            sql = """
                SELECT floor(timestamp / %s)::bigint * %s AS bucket, device,
                       AVG(power) AS power,
                       AVG(voltage) AS voltage,
                       AVG(current_a) AS current_a,
                       MAX(energy) AS energy,
                       AVG(power_factor) AS power_factor
                FROM power_readings
                WHERE device = %s AND timestamp >= %s
                GROUP BY bucket, device
                ORDER BY bucket
            """
            params = (resolution, resolution, device, since)
        else:
            sql = """
                SELECT floor(timestamp / %s)::bigint * %s AS bucket, device,
                       AVG(power) AS power,
                       AVG(voltage) AS voltage,
                       AVG(current_a) AS current_a,
                       MAX(energy) AS energy,
                       AVG(power_factor) AS power_factor
                FROM power_readings
                WHERE timestamp >= %s
                GROUP BY bucket, device
                ORDER BY bucket
            """
            params = (resolution, resolution, since)

        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(sql, params)
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, row)) for row in cur.fetchall()]
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
                    where = "WHERE device = %s AND timestamp >= %s"
                    params = (device, since)
                else:
                    where = "WHERE timestamp >= %s"
                    params = (since,)

                with self._conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT AVG(power) AS avg_watts,
                               MAX(power) AS peak_watts,
                               MAX(energy) - MIN(energy) AS total_kwh,
                               (MAX(timestamp) - MIN(timestamp)) / 86400.0 AS days_covered,
                               COUNT(DISTINCT device) AS device_count
                        FROM power_readings
                        {where}
                    """, params)
                    row = cur.fetchone()

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
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT device FROM power_readings ORDER BY device"
                    )
                    return [r[0] for r in cur.fetchall()]
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
