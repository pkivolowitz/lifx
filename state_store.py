"""Device state store — SQLite-backed record of which brain owns each bulb.

Shared between server.py and scheduler.py.  Both write on effect start/stop;
neither writes at frame rate.  The dashboard reads via GET /api/state.

SQLite WAL mode allows concurrent readers with serialised writers.  The
threading.Lock serialises writes from multiple server threads; SQLite's own
write lock handles cross-process safety (server vs scheduler).

DB path is supplied by the caller.  Defaults in server.py and scheduler.py
fall back to ``state.db`` alongside the active config file.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "1.0"

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Schema: one row per device keyed by IP.
# label   — human label when known (server writes it; scheduler may not have it)
# power   — 1=on  0=off  NULL=unknown
# effect  — NULL means idle / off
# source  — 'server' | 'scheduler' | '' (unknown)
# entry   — schedule entry name (scheduler writes only; NULL from server)
_DDL: str = """
CREATE TABLE IF NOT EXISTS device_state (
    ip         TEXT NOT NULL PRIMARY KEY,
    label      TEXT,
    power      INTEGER,
    effect     TEXT,
    source     TEXT,
    entry      TEXT,
    updated_at TEXT NOT NULL
)
"""


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------


class StateStore:
    """SQLite-backed device state store.

    One row per device (keyed by IP).  Written on effect start/stop by both
    server.py and scheduler.py.  Read by the dashboard via GET /api/state.

    Use :meth:`open` rather than instantiating directly — it returns None
    on failure so callers degrade gracefully instead of crashing.
    """

    def __init__(self, db_path: str) -> None:
        """Open the database and create the schema if needed.

        Args:
            db_path: Path to the SQLite file.  Created if absent.

        Raises:
            sqlite3.Error: If the file cannot be opened or initialised.
        """
        self._db_path: str = db_path
        # Serialise writes from multiple server threads.
        self._lock: threading.Lock = threading.Lock()
        # check_same_thread=False — server uses a thread pool.
        # _lock above prevents concurrent writes; SQLite WAL handles
        # cross-process write serialisation.
        from infrastructure.timed_io import timed_io, IOClass
        with timed_io("state_store.connect", IOClass.INSTANT):
            self._conn: sqlite3.Connection = sqlite3.connect(
                db_path, check_same_thread=False, timeout=5,
            )
        self._conn.row_factory = sqlite3.Row
        # WAL allows readers to run concurrently with a single writer.
        self._conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL gives crash-safe writes without full-sync overhead.
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_DDL)
        self._conn.commit()
        logger.info("State store opened at %s", db_path)

    @classmethod
    def open(cls, db_path: str) -> Optional["StateStore"]:
        """Open the state store, returning None on any failure.

        Callers treat None as "unavailable" and skip writes/reads.

        Args:
            db_path: Path to the SQLite file.

        Returns:
            A ready :class:`StateStore`, or None if the file cannot be opened.
        """
        try:
            return cls(db_path)
        except Exception as exc:
            logger.warning("State store unavailable at %s: %s", db_path, exc)
            return None

    def upsert(
        self,
        ip: str,
        *,
        label: Optional[str] = None,
        power: Optional[bool] = None,
        effect: Optional[str] = None,
        source: str = "",
        entry: Optional[str] = None,
    ) -> None:
        """Insert or update one device's state record.

        label is sticky — once written it is preserved even if subsequent
        callers omit it (scheduler knows IPs but not labels).  All other
        fields always reflect the latest write.

        Args:
            ip:     Device IP address — the primary key.
            label:  Human-readable label when known.
            power:  True=on  False=off  None=unknown.
            effect: Effect name, or None when idle/off.
            source: 'server' | 'scheduler' | ''.
            entry:  Schedule entry name (scheduler writes only).
        """
        now: str = datetime.now(timezone.utc).isoformat()
        power_int: Optional[int] = (
            None if power is None else (1 if power else 0)
        )
        # label: COALESCE — once written (by server, which knows labels), it
        #   survives writes from callers that don't know labels (scheduler).
        # entry: cleared when effect is NULL (device going idle), otherwise
        #   COALESCE — scheduler's entry name survives server writes that
        #   don't carry an entry (server never sets entry).
        # power, effect, source: last-write-wins — always reflect latest state.
        sql = """
            INSERT INTO device_state
                (ip, label, power, effect, source, entry, updated_at)
            VALUES
                (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                label      = COALESCE(excluded.label, label),
                power      = excluded.power,
                effect     = excluded.effect,
                source     = excluded.source,
                entry      = CASE
                                 WHEN excluded.effect IS NULL THEN NULL
                                 ELSE COALESCE(excluded.entry, entry)
                             END,
                updated_at = excluded.updated_at
        """
        try:
            with self._lock:
                self._conn.execute(
                    sql,
                    (ip, label, power_int, effect, source, entry, now),
                )
                self._conn.commit()
        except Exception as exc:
            # Non-fatal — log and continue.  A broken state store must not
            # interrupt effect playback.
            logger.debug("State store upsert failed for %s: %s", ip, exc)

    def get_all(self) -> list[dict[str, Any]]:
        """Return all device state records ordered by IP.

        Returns:
            List of dicts with keys: ip, label, power, effect, source,
            entry, updated_at.  power is True / False / None.
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM device_state ORDER BY ip"
                ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                d: dict[str, Any] = dict(row)
                if d["power"] is not None:
                    d["power"] = bool(d["power"])
                result.append(d)
            return result
        except Exception as exc:
            logger.debug("State store get_all failed: %s", exc)
            return []
