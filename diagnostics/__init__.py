"""Diagnostics subsystem — optional PostgreSQL-backed event logging.

Records effect play/stop events, device state changes, crash reports,
and signal snapshots to a PostgreSQL database.  All functionality
degrades gracefully when ``psycopg2`` is not installed or the database
is unreachable.

Typical usage::

    from diagnostics import DiagnosticsLogger

    diag = DiagnosticsLogger.from_env()   # reads GLOWUP_DIAG_DSN
    if diag is not None:
        diag.log_play(ip, label, effect_name, params, started_by="api")

If ``psycopg2`` is missing, :meth:`from_env` returns ``None`` and
callers skip logging with a simple ``if diag:`` guard.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import os
import threading
from typing import Any, Optional

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency — psycopg2
# ---------------------------------------------------------------------------

try:
    import psycopg2
    import psycopg2.extras
    _HAS_PSYCOPG2: bool = True
except ImportError:
    psycopg2 = None  # type: ignore[assignment]
    _HAS_PSYCOPG2 = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default connection string (override with GLOWUP_DIAG_DSN env var).
DEFAULT_DSN: str = "postgresql://glowup:glowup@10.0.0.42:5432/glowup"

#: Environment variable for the connection string.
DSN_ENV_VAR: str = "GLOWUP_DIAG_DSN"


# ---------------------------------------------------------------------------
# DiagnosticsLogger
# ---------------------------------------------------------------------------

class DiagnosticsLogger:
    """Thread-safe logger that writes diagnostic events to PostgreSQL.

    All write methods silently swallow database errors so that a
    diagnostics failure never impacts the main application.

    Attributes:
        dsn: The PostgreSQL connection string.
    """

    def __init__(self, dsn: str) -> None:
        """Initialize with a PostgreSQL connection string.

        Args:
            dsn: A ``postgresql://`` connection string.
        """
        self.dsn: str = dsn
        self._conn: Any = None
        self._lock: threading.Lock = threading.Lock()

    @classmethod
    def from_env(cls) -> Optional["DiagnosticsLogger"]:
        """Create a logger from environment or defaults.

        Returns ``None`` if ``psycopg2`` is not installed or the
        database is unreachable.  This is the intended entry point —
        callers should treat a ``None`` return as "diagnostics
        unavailable" and skip all logging.

        Returns:
            A connected :class:`DiagnosticsLogger`, or ``None``.
        """
        if not _HAS_PSYCOPG2:
            logger.debug("Diagnostics unavailable: psycopg2 not installed")
            return None

        dsn: str = os.environ.get(DSN_ENV_VAR, DEFAULT_DSN)
        instance: DiagnosticsLogger = cls(dsn)
        if not instance._connect():
            return None
        logger.info("Diagnostics logger connected to %s", dsn)
        return instance

    # -- Connection management -----------------------------------------------

    def _connect(self) -> bool:
        """Establish or re-establish the database connection.

        Returns:
            ``True`` if connected, ``False`` on failure.
        """
        try:
            self._conn = psycopg2.connect(self.dsn)
            self._conn.autocommit = True
            return True
        except Exception as exc:
            logger.warning("Diagnostics DB connection failed: %s", exc)
            self._conn = None
            return False

    def _execute(self, sql: str, params: tuple) -> bool:
        """Execute a SQL statement with automatic reconnection.

        Args:
            sql:    Parameterized SQL string.
            params: Parameter tuple for the query.

        Returns:
            ``True`` on success, ``False`` on failure.
        """
        with self._lock:
            for attempt in range(2):
                try:
                    if self._conn is None or self._conn.closed:
                        if not self._connect():
                            return False
                    with self._conn.cursor() as cur:
                        cur.execute(sql, params)
                    return True
                except Exception as exc:
                    logger.debug("Diagnostics write failed (attempt %d): %s",
                                 attempt + 1, exc)
                    self._conn = None
            return False

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
                self._conn = None

    # -- Event logging -------------------------------------------------------

    def log_play(
        self,
        device_ip: str,
        device_label: Optional[str],
        effect_name: str,
        params: Optional[dict[str, Any]] = None,
        started_by: str = "api",
    ) -> Optional[int]:
        """Record an effect play event.

        Args:
            device_ip:    Target device IP address.
            device_label: Human-readable device label.
            effect_name:  Name of the effect being started.
            params:       Effect parameters (stored as JSONB).
            started_by:   Origin of the request (``"api"``, ``"cli"``,
                          ``"schedule"``).

        Returns:
            The row ID of the inserted record, or ``None`` on failure.
        """
        sql: str = """
            INSERT INTO effect_history
                (device_ip, device_label, effect_name, params, started_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """
        params_json: Optional[str] = (
            json.dumps(params) if params else None
        )
        with self._lock:
            for attempt in range(2):
                try:
                    if self._conn is None or self._conn.closed:
                        if not self._connect():
                            return None
                    with self._conn.cursor() as cur:
                        cur.execute(sql, (
                            device_ip, device_label, effect_name,
                            params_json, started_by,
                        ))
                        row = cur.fetchone()
                        return row[0] if row else None
                except Exception as exc:
                    logger.debug("Diagnostics log_play failed (attempt %d): %s",
                                 attempt + 1, exc)
                    self._conn = None
        return None

    def log_stop(
        self,
        device_ip: str,
        stop_reason: str = "user",
    ) -> bool:
        """Record when the most recent effect on a device was stopped.

        Updates the ``stopped_at`` and ``stop_reason`` fields of the
        most recent open (un-stopped) record for the given device.

        Args:
            device_ip:   Device IP address.
            stop_reason: Why the effect stopped (``"user"``,
                         ``"replaced"``, ``"crash"``, ``"schedule"``).

        Returns:
            ``True`` if a record was updated, ``False`` otherwise.
        """
        sql: str = """
            UPDATE effect_history
            SET stopped_at = now(), stop_reason = %s
            WHERE id = (
                SELECT id FROM effect_history
                WHERE device_ip = %s AND stopped_at IS NULL
                ORDER BY started_at DESC
                LIMIT 1
            )
        """
        return self._execute(sql, (stop_reason, device_ip))

    def close_stale_records(self) -> int:
        """Close all open effect records on startup.

        Called when the server starts to clean up records left open
        by a previous crash or restart.  Sets ``stop_reason`` to
        ``'server_restart'``.

        Returns:
            Number of records closed.
        """
        sql: str = """
            UPDATE effect_history
            SET stopped_at = now(), stop_reason = 'server_restart'
            WHERE stopped_at IS NULL
        """
        with self._lock:
            for attempt in range(2):
                try:
                    if self._conn is None or self._conn.closed:
                        if not self._connect():
                            return 0
                    with self._conn.cursor() as cur:
                        cur.execute(sql)
                        count: int = cur.rowcount
                        if count > 0:
                            logger.info(
                                "Closed %d stale diagnostics records", count,
                            )
                        return count
                except Exception as exc:
                    logger.debug(
                        "close_stale_records failed (attempt %d): %s",
                        attempt + 1, exc,
                    )
                    self._conn = None
        return 0

    # -- Query methods -------------------------------------------------------

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Execute a SELECT and return rows as dicts.

        Args:
            sql:    Parameterized SQL string.
            params: Parameter tuple for the query.

        Returns:
            List of dicts (one per row), or empty list on failure.
        """
        with self._lock:
            for attempt in range(2):
                try:
                    if self._conn is None or self._conn.closed:
                        if not self._connect():
                            return []
                    with self._conn.cursor() as cur:
                        cur.execute(sql, params)
                        cols: list[str] = [d[0] for d in cur.description]
                        rows: list[dict[str, Any]] = []
                        for row in cur.fetchall():
                            d: dict[str, Any] = {}
                            for i, col in enumerate(cols):
                                val = row[i]
                                # Convert datetimes to ISO strings for JSON.
                                if hasattr(val, 'isoformat'):
                                    val = val.isoformat()
                                d[col] = val
                            rows.append(d)
                        return rows
                except Exception as exc:
                    logger.debug("Diagnostics query failed (attempt %d): %s",
                                 attempt + 1, exc)
                    self._conn = None
        return []

    def query_now_playing(self) -> list[dict[str, Any]]:
        """Return all currently playing effects (open records).

        Returns:
            List of dicts with ``device_ip``, ``device_label``,
            ``effect_name``, ``params``, ``started_by``, ``started_at``.
        """
        sql: str = """
            SELECT device_ip, device_label, effect_name, params,
                   started_by, started_at
            FROM effect_history
            WHERE stopped_at IS NULL
            ORDER BY started_at DESC
        """
        return self._query(sql)

    def query_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent effect history records.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of dicts with all effect_history columns.
        """
        sql: str = """
            SELECT device_ip, device_label, effect_name, params,
                   started_by, started_at, stopped_at, stop_reason
            FROM effect_history
            ORDER BY started_at DESC
            LIMIT %s
        """
        return self._query(sql, (limit,))
