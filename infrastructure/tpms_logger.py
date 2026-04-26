"""TPMS logger — records RF tire-pressure-sensor decodes to PostgreSQL.

Subscribes to ``glowup/tpms/events`` on the GlowUp canonical MQTT
broker (hub, localhost in production).  Every decoded frame emitted
by ernie's ``rtl_433`` pipeline becomes one row in
``tpms_observations``.

Mirrors the :class:`infrastructure.thermal_logger.ThermalLogger`
pattern (psycopg2, threading.Lock, guarded paho import, periodic
prune) so the three loggers under ``infrastructure/`` stay visually
symmetric and future maintenance can rely on a single mental model.

Unlike thermal/power, TPMS frames are **not** throttled.  A sensor
only transmits while the wheel is rotating above ~20 km/h, so a
"burst" of 8-12 frames at 1 Hz is exactly the signal — dropping any
of it loses a sighting.  At expected neighborhood traffic volumes
(20 vehicles/day * 4 sensors * 10 frames = 800 rows/day) the storage
cost is negligible.

Schema::

    CREATE TABLE IF NOT EXISTS tpms_observations (
        id BIGSERIAL PRIMARY KEY,
        timestamp DOUBLE PRECISION NOT NULL,
        model TEXT NOT NULL,
        sensor_id TEXT NOT NULL,
        pressure_kpa REAL,
        temperature_c REAL,
        battery_ok SMALLINT,
        payload JSONB
    );
    CREATE INDEX IF NOT EXISTS idx_tpms_sensor_ts
        ON tpms_observations(model, sensor_id, timestamp);
    CREATE INDEX IF NOT EXISTS idx_tpms_ts
        ON tpms_observations(timestamp);

The ``JSONB payload`` column preserves the full rtl_433 frame so
future work (vehicle clustering, protocol fingerprinting) can mine
fields this logger doesn't yet know about without a schema change.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import logging
import threading
import time
from typing import Any, Optional

try:
    import psycopg2
    from psycopg2.extras import Json as _PgJson
    _HAS_PSYCOPG2: bool = True
except ImportError:
    _HAS_PSYCOPG2 = False
    _PgJson = None  # type: ignore[assignment]

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False

logger: logging.Logger = logging.getLogger("glowup.tpms_logger")

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

# 30-day retention.  TPMS fingerprints stabilize within a week per the
# design memo (project_tpms_vehicle_detection.md); 30 d gives a full
# month of rollover for neighbor/visitor pattern analysis without
# unbounded growth.
RETENTION_SECONDS: float = 30 * 24 * 3600

# Prune old rows every N writes.  TPMS volume is low, so pruning
# weekly is fine even with a generous PRUNE_EVERY.
PRUNE_EVERY: int = 500

# MQTT topic the logger subscribes to.  Ernie's rtl_433 adapter
# publishes one message per decoded frame.
TPMS_TOPIC: str = "glowup/tpms/events"

# paho keepalive (seconds).
_MQTT_KEEPALIVE_S: int = 60


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_PG_DDL: str = """
CREATE TABLE IF NOT EXISTS tpms_observations (
    id BIGSERIAL PRIMARY KEY,
    timestamp DOUBLE PRECISION NOT NULL,
    model TEXT NOT NULL,
    sensor_id TEXT NOT NULL,
    pressure_kpa REAL,
    temperature_c REAL,
    battery_ok SMALLINT,
    payload JSONB
);
CREATE INDEX IF NOT EXISTS idx_tpms_sensor_ts
    ON tpms_observations(model, sensor_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_tpms_ts
    ON tpms_observations(timestamp);
"""


# ---------------------------------------------------------------------------
# TpmsLogger
# ---------------------------------------------------------------------------


class TpmsLogger:
    """Records TPMS decodes to PostgreSQL via MQTT subscribe.

    Instantiate once per process with a DSN.  Call
    ``start_subscriber(host, port)`` to begin ingesting live decodes
    from the MQTT broker; call ``close()`` on shutdown.

    Query surface for the ``/ernie`` dashboard:

    - :meth:`unique_sensors` — one entry per (model, id), first/last
      seen, count.  Equivalent to the old in-memory
      ``_ernie_tpms`` dict but backed by persistent storage.
    - :meth:`recent`        — the most recent N raw observations for
      a time-sorted event feed.

    Args:
        dsn: PostgreSQL connection string.
    """

    def __init__(self, dsn: str = DEFAULT_DSN) -> None:
        """See class docstring."""
        self._dsn: str = dsn
        self._lock: threading.Lock = threading.Lock()
        self._conn: Any = None
        self._write_count: int = 0
        self._client: Optional["mqtt.Client"] = None
        self._subscriber_started: bool = False
        self._open()

    # ---- DB lifecycle -------------------------------------------------------

    def _open(self) -> None:
        """Open the PG connection and create schema if needed."""
        if not _HAS_PSYCOPG2:
            logger.error("psycopg2 not installed — tpms logger disabled")
            return
        try:
            self._conn = psycopg2.connect(self._dsn, connect_timeout=10)
            self._conn.autocommit = True
            with self._conn.cursor() as cur:
                cur.execute(_PG_DDL)
            logger.info("TPMS logger connected: %s", self._dsn.split("@")[-1])
        except Exception as exc:
            logger.error("TPMS logger DB open failed: %s", exc)
            self._conn = None

    # ---- Write path ---------------------------------------------------------

    def record(self, payload: dict[str, Any]) -> None:
        """Insert one TPMS observation.

        Accepts a parsed ``rtl_433`` JSON frame.  ``model`` and
        ``id`` are required (they form the sensor fingerprint); any
        other field may be absent and is stored as NULL.  The full
        frame is preserved in the ``payload`` JSONB column so later
        work can mine fields we don't currently extract.

        Args:
            payload: Parsed JSON dict from an MQTT message.
        """
        if self._conn is None:
            return

        model: Any = payload.get("model")
        sensor_id: Any = payload.get("id")
        if model is None or sensor_id is None:
            logger.debug("tpms frame missing model/id, dropping")
            return

        model_str: str = str(model)
        id_str: str = str(sensor_id)
        now: float = time.time()

        pressure: Optional[float] = _coerce_float(payload.get("pressure_kPa"))
        temperature: Optional[float] = _coerce_float(
            payload.get("temperature_C"),
        )
        battery_raw: Any = payload.get("battery_ok")
        battery: Optional[int] = None
        if battery_raw is not None:
            battery = 1 if bool(battery_raw) else 0

        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO tpms_observations
                           (timestamp, model, sensor_id,
                            pressure_kpa, temperature_c,
                            battery_ok, payload)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (
                            now, model_str, id_str,
                            pressure, temperature, battery,
                            _PgJson(payload) if _PgJson else None,
                        ),
                    )
                self._write_count += 1
                if self._write_count % PRUNE_EVERY == 0:
                    self._prune()
            except Exception as exc:
                logger.warning(
                    "TPMS logger write failed for %s:%s: %s",
                    model_str, id_str, exc,
                )

    def _prune(self) -> None:
        """Delete rows older than :data:`RETENTION_SECONDS`."""
        if self._conn is None:
            return
        cutoff: float = time.time() - RETENTION_SECONDS
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM tpms_observations WHERE timestamp < %s",
                    (cutoff,),
                )
                if cur.rowcount > 0:
                    logger.info(
                        "TPMS logger pruned %d old observation(s)",
                        cur.rowcount,
                    )
        except Exception as exc:
            logger.warning("TPMS logger prune failed: %s", exc)

    # ---- Query surface ------------------------------------------------------

    def unique_sensors(
        self,
        window_s: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """Return one entry per distinct (model, sensor_id) recently seen.

        Each entry carries first/last timestamps, total observation
        count, and the most recent raw payload — matching the shape
        of the old in-memory ``_ernie_tpms`` dict so the dashboard
        API contract doesn't change.

        Args:
            window_s: If set, only include sensors whose most recent
                observation is within ``now - window_s``.  ``first_seen``
                and ``count`` are computed over the SAME window, so a
                sensor that has been transmitting for weeks appears
                freshly in the window with a count that reflects only
                the in-window traffic.  ``None`` returns everything in
                the retention window (expensive on long-running
                databases — use a window for polled dashboards).

        Returns:
            List sorted by ``last_seen`` descending.
        """
        if self._conn is None:
            return []
        clause: str = ""
        params: tuple = ()
        if window_s is not None and window_s > 0:
            clause = "WHERE timestamp >= %s"
            params = (time.time() - float(window_s),)
        sql: str = f"""
            SELECT model, sensor_id,
                   MIN(timestamp) AS first_seen,
                   MAX(timestamp) AS last_seen,
                   COUNT(*)       AS count,
                   (SELECT payload FROM tpms_observations o2
                     WHERE o2.model = o1.model
                       AND o2.sensor_id = o1.sensor_id
                       {"AND o2.timestamp >= %s" if clause else ""}
                     ORDER BY timestamp DESC LIMIT 1) AS last_payload
            FROM tpms_observations o1
            {clause}
            GROUP BY model, sensor_id
            ORDER BY MAX(timestamp) DESC
        """
        # Two %s substitutions when windowed: one for the subquery
        # filter, one for the outer WHERE.
        exec_params: tuple = (params + params) if clause else ()
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(sql, exec_params)
                    out: list[dict[str, Any]] = []
                    for row in cur.fetchall():
                        model, sid, first_s, last_s, count, last_p = row
                        out.append({
                            "model": model,
                            "id": sid,
                            "first_seen": float(first_s),
                            "last_seen": float(last_s),
                            "count": int(count),
                            "last_payload": last_p or {},
                        })
                    return out
            except Exception as exc:
                logger.warning(
                    "TPMS logger unique_sensors query failed: %s", exc,
                )
                return []

    def last_seen_ts(self) -> float:
        """Return the timestamp of the most recent observation, or 0."""
        if self._conn is None:
            return 0.0
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT MAX(timestamp) FROM tpms_observations",
                    )
                    row: Any = cur.fetchone()
                    if row and row[0] is not None:
                        return float(row[0])
            except Exception as exc:
                logger.warning(
                    "TPMS logger last_seen_ts query failed: %s", exc,
                )
        return 0.0

    # ---- MQTT subscriber ----------------------------------------------------

    def start_subscriber(
        self,
        broker_host: str = "127.0.0.1",
        broker_port: int = 1883,
    ) -> None:
        """Connect a paho client and subscribe to TPMS events.

        Guarded — if paho-mqtt is not importable the subscriber is a
        no-op and a warning is logged.  The query surface is still
        usable for data written by a previous run.

        Args:
            broker_host: MQTT broker hostname or IP.
            broker_port: MQTT broker TCP port.
        """
        if not _HAS_PAHO:
            logger.warning(
                "paho-mqtt not installed — tpms subscriber disabled",
            )
            return
        if self._subscriber_started:
            logger.debug("tpms subscriber already running")
            return

        client: mqtt.Client = mqtt.Client(client_id="glowup-tpms-logger")
        client.on_connect = self._on_mqtt_connect
        client.on_message = self._on_mqtt_message
        client.on_disconnect = self._on_mqtt_disconnect

        try:
            client.connect(broker_host, broker_port, _MQTT_KEEPALIVE_S)
        except Exception as exc:
            logger.error(
                "TPMS subscriber connect to %s:%d failed: %s",
                broker_host, broker_port, exc,
            )
            return
        client.loop_start()
        self._client = client
        self._subscriber_started = True
        logger.info(
            "TPMS subscriber started — %s:%d topic=%s",
            broker_host, broker_port, TPMS_TOPIC,
        )

    def _on_mqtt_connect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        flags: dict[str, Any],
        rc: int,
    ) -> None:
        """paho callback — (re-)subscribe on every connect.

        Re-subscribing on every connect (not just init) is the
        defense against the well-known paho pattern where a silent
        reconnect leaves the client deaf.  See
        feedback_paho_resubscribe_on_connect.md.
        """
        if rc == 0:
            client.subscribe(TPMS_TOPIC, qos=1)
            logger.info("tpms subscriber subscribed to %s", TPMS_TOPIC)
        else:
            logger.error("tpms subscriber connect rc=%d", rc)

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
                "tpms message on %s is not JSON: %s", msg.topic, exc,
            )
            return
        if not isinstance(payload, dict):
            logger.warning(
                "tpms message on %s is not a JSON object: %r",
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
            logger.warning("tpms subscriber unexpected disconnect rc=%d", rc)

    # ---- Shutdown -----------------------------------------------------------

    def close(self) -> None:
        """Stop the subscriber and close the database."""
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as exc:
                logger.warning("tpms subscriber shutdown error: %s", exc)
            self._client = None
            self._subscriber_started = False
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as exc:
                logger.warning("tpms PG close error: %s", exc)
            self._conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_float(val: Any) -> Optional[float]:
    """Best-effort numeric coercion from rtl_433's mixed-type fields.

    rtl_433 sometimes emits an int where we expect a float; sometimes
    a string; sometimes absent.  Returns None when conversion is not
    possible so the column can be stored as NULL.
    """
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
