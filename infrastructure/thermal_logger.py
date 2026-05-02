"""Thermal logger — records Pi hardware thermal telemetry to PostgreSQL.

Subscribes to ``glowup/hardware/thermal/+`` on the GlowUp canonical
MQTT broker (hub, localhost in production).  Each message published by
a ``contrib/sensors/pi_thermal_sensor.py`` instance on a fleet Pi
becomes one row in ``thermal_readings``.

Mirrors the :class:`infrastructure.power_logger.PowerLogger` pattern:
psycopg2, thread-safe writes, throttled inserts, background flush
timer, periodic prune with 7-day retention.

Unlike PowerLogger, the thermal sensor publishes a full snapshot with
every message (not change-based like Zigbee), so there is no
carry-forward state — each message is a complete reading.  The
throttle exists to protect the database from a misconfigured agent
publishing at 1s intervals.

Schema::

    CREATE TABLE IF NOT EXISTS thermal_readings (
        id BIGSERIAL PRIMARY KEY,
        node_id TEXT NOT NULL,
        timestamp DOUBLE PRECISION NOT NULL,
        cpu_temp_c REAL,
        fan_rpm INTEGER,
        fan_pwm_step INTEGER,
        fan_declared_present SMALLINT,
        load_1m REAL,
        load_5m REAL,
        load_15m REAL,
        uptime_s REAL,
        throttled_flags TEXT,
        platform TEXT,
        model TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_thermal_node_ts
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

import datetime
import json
import logging
import threading
import time
from typing import Any, Optional


def _parse_sample_ts(raw: Any, fallback: float) -> float:
    """Parse the sensor's ``ts`` field into epoch seconds.

    Accepts ISO 8601 strings (the format pi_thermal_sensor.py
    publishes — e.g. ``"2026-04-23T15:26:19Z"``) or numeric
    epoch values.  Unparseable or missing input falls back to
    ``fallback`` (typically the receive moment).

    Why this exists: the ``timestamp`` column drives the dashboard's
    "last sample" age calculation.  Storing receipt time instead of
    sample time made dead hosts appear fresh every time the broker
    replayed their retained payloads to a re-subscribing logger.
    Args:
        raw:      The ``ts`` field value from the MQTT payload.
        fallback: Epoch seconds to return when ``raw`` is missing
                  or unparseable.

    Returns:
        Epoch seconds (float).
    """
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        # ``fromisoformat`` accepts "2026-04-23T15:26:19+00:00" but
        # not the bare-Z form pi_thermal_sensor.py emits.  Swap "Z"
        # for "+00:00" before parsing.
        try:
            dt: datetime.datetime = datetime.datetime.fromisoformat(
                raw.replace("Z", "+00:00")
            )
            return dt.timestamp()
        except ValueError:
            pass
    return fallback

try:
    import psycopg2
    _HAS_PSYCOPG2: bool = True
except ImportError:
    _HAS_PSYCOPG2 = False

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False

logger: logging.Logger = logging.getLogger("glowup.thermal_logger")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default PostgreSQL DSN — same resolution order as power_logger:
# /etc/glowup/secrets.json postgres_dsn, then GLOWUP_DIAG_DSN env,
# else empty.  No hardcoded credentials in source.
import os as _os
from glowup_site import site as _site
DEFAULT_DSN: str = (
    _site.get("postgres_dsn")
    or _os.environ.get("GLOWUP_DIAG_DSN")
    or ""
)

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

# paho keepalive (seconds).
_MQTT_KEEPALIVE_S: int = 60

# Seconds to wait for the subscriber thread to join on shutdown.
_SUBSCRIBER_JOIN_TIMEOUT_S: float = 5.0


# ---------------------------------------------------------------------------
# ThermalLogger
# ---------------------------------------------------------------------------

_PG_DDL: str = """
CREATE TABLE IF NOT EXISTS thermal_readings (
    id BIGSERIAL PRIMARY KEY,
    node_id TEXT NOT NULL,
    timestamp DOUBLE PRECISION NOT NULL,
    cpu_temp_c REAL,
    fan_rpm INTEGER,
    fan_pwm_step INTEGER,
    fan_declared_present SMALLINT,
    load_1m REAL,
    load_5m REAL,
    load_15m REAL,
    uptime_s REAL,
    throttled_flags TEXT,
    platform TEXT,
    model TEXT
);
CREATE INDEX IF NOT EXISTS idx_thermal_node_ts
    ON thermal_readings(node_id, timestamp);
"""


class ThermalLogger:
    """Records Pi hardware thermal telemetry to PostgreSQL via MQTT subscribe.

    Instantiate once per process with a DSN.  Call
    ``start_subscriber(host, port)`` to begin ingesting live telemetry
    from the MQTT broker; call ``close()`` on shutdown.

    Query surface for the dashboard:

    - :meth:`latest`    — one most-recent row per node (fleet snapshot)
    - :meth:`query`     — time-bucketed history for a single node
    - :meth:`hosts`     — list of known node ids with any data

    Args:
        dsn: PostgreSQL connection string.
    """

    def __init__(self, dsn: str = DEFAULT_DSN) -> None:
        """See class docstring."""
        self._dsn: str = dsn
        self._lock: threading.Lock = threading.Lock()
        self._conn: Any = None
        self._write_count: int = 0
        self._last_write: dict[str, float] = {}
        self._client: Optional["mqtt.Client"] = None
        self._subscriber_started: bool = False
        self._open()

    # ---- DB lifecycle -------------------------------------------------------

    def _open(self) -> None:
        """Open the PG connection and create schema if needed."""
        if not _HAS_PSYCOPG2:
            logger.info(
                "Thermal logger disabled — psycopg2 not installed "
                "(BASIC scope: no Postgres consumer)"
            )
            return
        if not self._dsn:
            # See power_logger._open for the full rationale: empty
            # DSN is the default BASIC-install state, not an error.
            logger.info(
                "Thermal logger disabled — no DSN configured "
                "(set 'postgres_dsn' in site.json or "
                "GLOWUP_DIAG_DSN env var to enable)"
            )
            return
        try:
            self._conn = psycopg2.connect(self._dsn, connect_timeout=10)
            self._conn.autocommit = True
            with self._conn.cursor() as cur:
                cur.execute(_PG_DDL)
            logger.info("Thermal logger connected: %s", self._dsn.split("@")[-1])
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
        # Sample time as reported by the sensor itself.  Distinct from
        # ``now`` (receipt time) because retained-payload replay during
        # subscriber reconnect would otherwise stamp dead hosts as
        # currently-fresh.  See ``_parse_sample_ts`` docstring.
        sample_ts: float = _parse_sample_ts(payload.get("ts"), now)

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
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO thermal_readings
                           (node_id, timestamp,
                            cpu_temp_c, fan_rpm, fan_pwm_step,
                            fan_declared_present,
                            load_1m, load_5m, load_15m,
                            uptime_s, throttled_flags, platform, model)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            node_id,
                            sample_ts,
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
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM thermal_readings WHERE timestamp < %s",
                    (cutoff,),
                )
                if cur.rowcount > 0:
                    logger.info(
                        "Thermal logger pruned %d old record(s)",
                        cur.rowcount,
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
                with self._conn.cursor() as cur:
                    cur.execute(sql)
                    for row in cur.fetchall():
                        (node_id, ts, temp, rpm, step, declared,
                         l1, l5, l15, uptime, throttle,
                         platform, model) = row
                        result[node_id] = {
                            "node_id": node_id,
                            "timestamp": float(ts),
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
        # AVG() over an integer column returns Postgres `numeric`,
        # which psycopg maps to Python `Decimal` and json.dumps refuses
        # to serialize.  Cast integer aggregates to double precision so
        # the API surface stays uniformly float.
        sql: str = """
            SELECT floor(timestamp / %s)::bigint * %s AS bucket,
                   AVG(cpu_temp_c)::double precision   AS cpu_temp_c,
                   AVG(fan_rpm)::double precision      AS fan_rpm,
                   AVG(fan_pwm_step)::double precision AS fan_pwm_step,
                   MAX(fan_declared_present)           AS fan_declared_present,
                   AVG(load_1m)::double precision      AS load_1m,
                   AVG(load_5m)::double precision      AS load_5m,
                   AVG(load_15m)::double precision     AS load_15m,
                   MAX(uptime_s)                       AS uptime_s,
                   MAX(throttled_flags)                AS throttled_flags
            FROM thermal_readings
            WHERE node_id = %s AND timestamp >= %s
            GROUP BY bucket
            ORDER BY bucket
        """
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(sql, (resolution, resolution, node_id, since))
                    cols: list[str] = [d[0] for d in cur.description]
                    return [dict(zip(cols, row)) for row in cur.fetchall()]
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
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT node_id FROM thermal_readings "
                        "ORDER BY node_id"
                    )
                    return [r[0] for r in cur.fetchall()]
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
                logger.warning("thermal PG close error: %s", exc)
            self._conn = None
