"""Thermal logger — records Pi hardware thermal telemetry to SQLite.

Subscribes to ``glowup/hardware/thermal/+`` on the GlowUp canonical
MQTT broker (hub, localhost in production).  Each message published by
a ``contrib/sensors/pi_thermal_sensor.py`` instance on a fleet Pi
becomes one row in ``thermal_readings``.

Mirrors the :class:`infrastructure.power_logger.PowerLogger` pattern:
WAL SQLite, thread-safe writes, throttled inserts, background flush
timer, periodic prune with 7-day retention.

Unlike PowerLogger, the thermal sensor publishes a full snapshot with
every message (not change-based like Zigbee), so there is no
carry-forward state — each message is a complete reading.  The
throttle exists to protect the database from a misconfigured agent
publishing at 1s intervals.

Schema::

    CREATE TABLE thermal_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id TEXT NOT NULL,
        timestamp REAL NOT NULL,
        cpu_temp_c REAL,
        fan_rpm INTEGER,
        fan_pwm_step INTEGER,
        fan_declared_present INTEGER,
        load_1m REAL,
        load_5m REAL,
        load_15m REAL,
        uptime_s REAL,
        throttled_flags TEXT,
        platform TEXT,
        model TEXT
    );
    CREATE INDEX idx_thermal_node_ts
        ON thermal_readings(node_id, timestamp);

Subscriber lifecycle:

- ``start_subscriber(host, port)`` spins up a paho client in a loop
  thread.  Guarded — if paho is not importable the subscriber silently
  no-ops and the logger is still usable as a query surface for
  historical data written by a previous run.
- ``close()`` stops the subscriber and closes the database.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import logging
import sqlite3
import threading
import time
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False

logger: logging.Logger = logging.getLogger("glowup.thermal_logger")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default database path (alongside server.json).
DEFAULT_DB_PATH: str = "/etc/glowup/thermal.db"

# 7-day retention to match PowerLogger.
RETENTION_SECONDS: float = 7 * 24 * 3600

# Minimum seconds between writes for the same node.  Sensor defaults
# to 30s publish interval so 10s is a generous guardrail — a
# misconfigured sensor running at 1s cannot flood the database.
MIN_WRITE_INTERVAL_S: float = 10.0

# Prune old rows every N writes.
PRUNE_EVERY: int = 100

# MQTT topic wildcard the logger subscribes to.
THERMAL_TOPIC_PATTERN: str = "glowup/hardware/thermal/+"

# Schema version (stored in user_version PRAGMA).
SCHEMA_VERSION: int = 1

# paho keepalive (seconds).
_MQTT_KEEPALIVE_S: int = 60

# Seconds to wait for the subscriber thread to join on shutdown.
_SUBSCRIBER_JOIN_TIMEOUT_S: float = 5.0


# ---------------------------------------------------------------------------
# ThermalLogger
# ---------------------------------------------------------------------------

class ThermalLogger:
    """Records Pi hardware thermal telemetry to SQLite via MQTT subscribe.

    Instantiate once per process with a database path.  Call
    ``start_subscriber(host, port)`` to begin ingesting live telemetry
    from the MQTT broker; call ``close()`` on shutdown.

    Query surface for the dashboard:

    - :meth:`latest`    — one most-recent row per node (fleet snapshot)
    - :meth:`query`     — time-bucketed history for a single node
    - :meth:`hosts`     — list of known node ids with any data

    Args:
        db_path: Filesystem path to the SQLite database file.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        """See class docstring."""
        self._db_path: str = db_path
        self._lock: threading.Lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._write_count: int = 0
        self._last_write: dict[str, float] = {}
        self._client: Optional["mqtt.Client"] = None
        self._subscriber_started: bool = False
        self._open()

    # ---- DB lifecycle -------------------------------------------------------

    def _open(self) -> None:
        """Open the database and create schema if needed."""
        try:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                timeout=5,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS thermal_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    cpu_temp_c REAL,
                    fan_rpm INTEGER,
                    fan_pwm_step INTEGER,
                    fan_declared_present INTEGER,
                    load_1m REAL,
                    load_5m REAL,
                    load_15m REAL,
                    uptime_s REAL,
                    throttled_flags TEXT,
                    platform TEXT,
                    model TEXT
                )
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_thermal_node_ts
                ON thermal_readings(node_id, timestamp)
            """)
            self._conn.commit()
            logger.info("Thermal logger opened: %s", self._db_path)
        except Exception as exc:
            logger.error("Thermal logger DB open failed: %s", exc)
            self._conn = None

    # ---- Write path ---------------------------------------------------------

    def record(self, payload: dict[str, Any]) -> None:
        """Insert a thermal reading.

        Accepts the full JSON payload published by
        ``pi_thermal_sensor.py`` as a ``dict``.  Missing fields are
        stored as NULL.  Throttled to :data:`MIN_WRITE_INTERVAL_S`
        seconds per node to bound database growth.

        Args:
            payload: Parsed JSON dict from an MQTT message.
        """
        if self._conn is None:
            return

        node_id: Optional[str] = payload.get("node_id")
        if not node_id or not isinstance(node_id, str):
            logger.debug("thermal reading missing node_id, dropping")
            return
        if node_id.startswith("itest-"):
            # Integration tests synthesize node_ids of the form
            # itest-<pid>-<ms>.  Tests publish on glowup/test/thermal/
            # which this logger does not subscribe to, but a stale
            # retained message on glowup/hardware/thermal/itest-* would
            # replay on every reconnect.  Drop at the ingest boundary
            # so the production dashboard can never show a test host.
            logger.debug("dropping itest payload node_id=%s", node_id)
            return

        now: float = time.time()

        with self._lock:
            last: float = self._last_write.get(node_id, 0.0)
            if now - last < MIN_WRITE_INTERVAL_S:
                return

            fan_declared: Optional[int] = None
            raw_fan_declared: Any = payload.get("fan_declared_present")
            if raw_fan_declared is not None:
                # SQLite has no native bool — store as 0/1 INTEGER so
                # downstream queries can filter with the same idiom.
                fan_declared = 1 if bool(raw_fan_declared) else 0

            extra: dict[str, Any] = payload.get("extra") or {}
            throttled_flags: Optional[str] = extra.get("throttled_flags")
            model: Optional[str] = extra.get("model")

            self._last_write[node_id] = now
            try:
                self._conn.execute(
                    """INSERT INTO thermal_readings
                       (node_id, timestamp,
                        cpu_temp_c, fan_rpm, fan_pwm_step,
                        fan_declared_present,
                        load_1m, load_5m, load_15m,
                        uptime_s, throttled_flags, platform, model)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        node_id,
                        now,
                        payload.get("cpu_temp_c"),
                        payload.get("fan_rpm"),
                        payload.get("fan_pwm_step"),
                        fan_declared,
                        payload.get("load_1m"),
                        payload.get("load_5m"),
                        payload.get("load_15m"),
                        payload.get("uptime_s"),
                        throttled_flags,
                        payload.get("platform"),
                        model,
                    ),
                )
                self._conn.commit()
                self._write_count += 1
                if self._write_count % PRUNE_EVERY == 0:
                    self._prune()
            except Exception as exc:
                logger.warning(
                    "Thermal logger write failed for %s: %s", node_id, exc,
                )

    def _prune(self) -> None:
        """Delete rows older than :data:`RETENTION_SECONDS`."""
        if self._conn is None:
            return
        cutoff: float = time.time() - RETENTION_SECONDS
        try:
            cursor = self._conn.execute(
                "DELETE FROM thermal_readings WHERE timestamp < ?",
                (cutoff,),
            )
            self._conn.commit()
            if cursor.rowcount > 0:
                logger.info(
                    "Thermal logger pruned %d old record(s)",
                    cursor.rowcount,
                )
        except Exception as exc:
            logger.warning("Thermal logger prune failed: %s", exc)

    # ---- Query surface ------------------------------------------------------

    def latest(self) -> dict[str, dict[str, Any]]:
        """Return the most recent row per known node.

        Used by the fleet dashboard to populate the rigid columnar
        grid — one row per host, always the most recent sample.

        Returns:
            Dict mapping ``node_id`` to a reading dict.  Empty on any
            failure.
        """
        if self._conn is None:
            return {}
        sql: str = """
            SELECT t.node_id, t.timestamp,
                   t.cpu_temp_c, t.fan_rpm, t.fan_pwm_step,
                   t.fan_declared_present,
                   t.load_1m, t.load_5m, t.load_15m,
                   t.uptime_s, t.throttled_flags,
                   t.platform, t.model
            FROM thermal_readings t
            INNER JOIN (
                SELECT node_id, MAX(timestamp) AS max_ts
                FROM thermal_readings
                GROUP BY node_id
            ) m ON t.node_id = m.node_id AND t.timestamp = m.max_ts
        """
        result: dict[str, dict[str, Any]] = {}
        with self._lock:
            try:
                cursor = self._conn.execute(sql)
                for row in cursor.fetchall():
                    (node_id, ts, temp, rpm, step, declared,
                     l1, l5, l15, uptime, throttle,
                     platform, model) = row
                    result[node_id] = {
                        "node_id": node_id,
                        "timestamp": ts,
                        "cpu_temp_c": temp,
                        "fan_rpm": rpm,
                        "fan_pwm_step": step,
                        "fan_declared_present": (
                            None if declared is None else bool(declared)
                        ),
                        "load_1m": l1,
                        "load_5m": l5,
                        "load_15m": l15,
                        "uptime_s": uptime,
                        "throttled_flags": throttle,
                        "platform": platform,
                        "model": model,
                    }
            except Exception as exc:
                logger.warning("Thermal logger latest query failed: %s", exc)
        return result

    def query(
        self,
        node_id: str,
        hours: float = 1.0,
        resolution: int = 60,
    ) -> list[dict[str, Any]]:
        """Return time-bucketed history for a single node.

        Args:
            node_id:    Node identifier (e.g. ``"hub"``, ``"broker-2"``).
            hours:      Window size in hours.
            resolution: Bucket size in seconds for averaging.

        Returns:
            List of dicts sorted by ``bucket`` ascending, each with
            averaged numeric fields.  ``throttled_flags`` is the MAX
            (string-compared) over the bucket so any event that fired
            anywhere in the window remains visible.
        """
        if self._conn is None:
            return []
        since: float = time.time() - (hours * 3600)
        bucket_expr: str = (
            f"CAST(timestamp / {resolution} AS INTEGER) * {resolution}"
        )
        sql: str = f"""
            SELECT {bucket_expr} AS bucket,
                   AVG(cpu_temp_c)     AS cpu_temp_c,
                   AVG(fan_rpm)        AS fan_rpm,
                   AVG(fan_pwm_step)   AS fan_pwm_step,
                   MAX(fan_declared_present) AS fan_declared_present,
                   AVG(load_1m)        AS load_1m,
                   AVG(load_5m)        AS load_5m,
                   AVG(load_15m)       AS load_15m,
                   MAX(uptime_s)       AS uptime_s,
                   MAX(throttled_flags) AS throttled_flags
            FROM thermal_readings
            WHERE node_id = ? AND timestamp >= ?
            GROUP BY bucket
            ORDER BY bucket
        """
        with self._lock:
            try:
                cursor = self._conn.execute(sql, (node_id, since))
                cols: list[str] = [d[0] for d in cursor.description]
                return [dict(zip(cols, row)) for row in cursor.fetchall()]
            except Exception as exc:
                logger.warning(
                    "Thermal logger query failed for %s: %s", node_id, exc,
                )
                return []

    def hosts(self) -> list[str]:
        """Return the list of distinct node ids that have any data."""
        if self._conn is None:
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT DISTINCT node_id FROM thermal_readings "
                    "ORDER BY node_id"
                ).fetchall()
                return [r[0] for r in rows]
            except Exception as exc:
                logger.warning(
                    "Thermal logger hosts query failed: %s", exc,
                )
                return []

    # ---- MQTT subscriber ---------------------------------------------------

    def start_subscriber(
        self,
        broker_host: str = "127.0.0.1",
        broker_port: int = 1883,
    ) -> None:
        """Connect a paho client and subscribe to thermal telemetry.

        Guarded — if paho-mqtt is not importable the subscriber is a
        no-op and a warning is logged.  The query surface is still
        usable for data written by a previous run.

        Args:
            broker_host: MQTT broker hostname or IP.
            broker_port: MQTT broker TCP port.
        """
        if not _HAS_PAHO:
            logger.warning(
                "paho-mqtt not installed — thermal subscriber disabled. "
                "Install with: sudo apt install -y python3-paho-mqtt",
            )
            return
        if self._subscriber_started:
            logger.debug("thermal subscriber already running")
            return

        client: mqtt.Client = mqtt.Client(
            client_id="glowup-thermal-logger",
        )
        client.on_connect = self._on_mqtt_connect
        client.on_message = self._on_mqtt_message
        client.on_disconnect = self._on_mqtt_disconnect

        try:
            client.connect(broker_host, broker_port, _MQTT_KEEPALIVE_S)
        except Exception as exc:
            logger.error(
                "Thermal subscriber connect to %s:%d failed: %s",
                broker_host, broker_port, exc,
            )
            return
        client.loop_start()
        self._client = client
        self._subscriber_started = True
        logger.info(
            "Thermal subscriber started — %s:%d pattern=%s",
            broker_host, broker_port, THERMAL_TOPIC_PATTERN,
        )

    def _on_mqtt_connect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        flags: dict[str, Any],
        rc: int,
    ) -> None:
        """paho callback — subscribe to the thermal topic on connect."""
        if rc == 0:
            client.subscribe(THERMAL_TOPIC_PATTERN, qos=1)
            logger.info("thermal subscriber subscribed to %s",
                        THERMAL_TOPIC_PATTERN)
        else:
            logger.error("thermal subscriber connect rc=%d", rc)

    def _on_mqtt_message(
        self,
        client: "mqtt.Client",
        userdata: Any,
        msg: "mqtt.MQTTMessage",
    ) -> None:
        """paho callback — parse JSON and record."""
        try:
            payload: dict[str, Any] = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning(
                "thermal message on %s is not JSON: %s", msg.topic, exc,
            )
            return
        if not isinstance(payload, dict):
            logger.warning(
                "thermal message on %s is not a JSON object: %r",
                msg.topic, type(payload).__name__,
            )
            return
        self.record(payload)

    def _on_mqtt_disconnect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        rc: int,
    ) -> None:
        """paho callback — log unexpected disconnects."""
        if rc != 0:
            logger.warning(
                "thermal subscriber unexpected disconnect rc=%d", rc,
            )

    # ---- Shutdown -----------------------------------------------------------

    def close(self) -> None:
        """Stop the subscriber and close the database."""
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as exc:
                logger.warning("thermal subscriber shutdown error: %s", exc)
            self._client = None
            self._subscriber_started = False
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as exc:
                logger.warning("thermal DB close error: %s", exc)
            self._conn = None
