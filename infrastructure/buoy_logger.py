"""Buoy logger — records NDBC observations to PostgreSQL.

Subscribes to ``glowup/maritime/buoy/+`` (the topic
``maritime/buoy_scraper.py`` publishes onto) and writes one row per
distinct observation to ``buoy_observations``.

Mirrors :class:`infrastructure.thermal_logger.ThermalLogger` and
:class:`infrastructure.meter_logger.MeterLogger`:

- DSN resolution: site.postgres_dsn → $GLOWUP_DIAG_DSN → empty
  (logger no-ops without a DSN, query surface returns []).
- 90-day retention (matches meters; buoy obs are noisier than
  fleet thermals so a longer window is friendlier for trend
  analysis).
- ON CONFLICT (station_id, obs_ts) DO NOTHING so the scraper's
  retry-friendly polling is harmless — re-publishing the same row
  doesn't double-count.

Schema::

    CREATE TABLE IF NOT EXISTS buoy_observations (
        id BIGSERIAL PRIMARY KEY,
        station_id      TEXT NOT NULL,
        obs_ts          DOUBLE PRECISION NOT NULL,
        received_ts     DOUBLE PRECISION NOT NULL,
        wind_dir_deg    REAL,
        wind_speed_kt   REAL,
        wind_gust_kt    REAL,
        wave_height_m   REAL,
        wave_period_s   REAL,
        wave_period_avg_s REAL,
        wave_dir_deg    REAL,
        pressure_mb     REAL,
        pressure_tendency_mb REAL,
        air_temp_c      REAL,
        water_temp_c    REAL,
        dewpoint_c      REAL,
        visibility_nmi  REAL,
        tide_ft         REAL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS buoy_obs_station_ts
        ON buoy_observations (station_id, obs_ts);
    CREATE INDEX IF NOT EXISTS buoy_obs_obs_ts
        ON buoy_observations (obs_ts);

Query surface for the dashboard:

- :meth:`history` — time-bucketed series for one station.  Powers
  the chart cards on /buoys/<station>.

Live current state is served by
:class:`infrastructure.buoy_buffer.BuoyBuffer` on a parallel MQTT
subscription, NOT by query against this table — the buffer is
faster and the table is for retention / charting.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import datetime
import json
import logging
import threading
import time
from typing import Any, Optional

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


logger: logging.Logger = logging.getLogger("glowup.buoy_logger")


# ─── Constants ────────────────────────────────────────────────────────

import os as _os
from glowup_site import site as _site

# Default PostgreSQL DSN — same resolution order as the rest of the
# diagnostic-style loggers.  No hardcoded credentials in source.
DEFAULT_DSN: str = (
    _site.get("postgres_dsn")
    or _os.environ.get("GLOWUP_DIAG_DSN")
    or ""
)

# 90-day retention for buoy data.  Longer than the fleet-thermal
# 7-day window because trend analysis (storm tracking, seasonal
# patterns) wants a larger lookback than fleet-host diagnostics.
RETENTION_SECONDS: float = 90 * 24 * 3600

# Prune cadence — every Nth INSERT.
PRUNE_EVERY: int = 200

# MQTT topic + paho keepalive — match buoy_buffer.py.
BUOY_TOPIC_PATTERN: str = "glowup/maritime/buoy/+"
_MQTT_KEEPALIVE_S: int = 60

_PG_DDL: str = """
CREATE TABLE IF NOT EXISTS buoy_observations (
    id BIGSERIAL PRIMARY KEY,
    station_id TEXT NOT NULL,
    obs_ts DOUBLE PRECISION NOT NULL,
    received_ts DOUBLE PRECISION NOT NULL,
    wind_dir_deg REAL,
    wind_speed_kt REAL,
    wind_gust_kt REAL,
    wave_height_m REAL,
    wave_period_s REAL,
    wave_period_avg_s REAL,
    wave_dir_deg REAL,
    pressure_mb REAL,
    pressure_tendency_mb REAL,
    air_temp_c REAL,
    water_temp_c REAL,
    dewpoint_c REAL,
    visibility_nmi REAL,
    tide_ft REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS buoy_obs_station_ts
    ON buoy_observations (station_id, obs_ts);
CREATE INDEX IF NOT EXISTS buoy_obs_obs_ts
    ON buoy_observations (obs_ts);
"""


# Numeric fields the schema accepts (column order matters for
# INSERT).  Kept as a single source of truth so adding a field is
# one edit here + one DDL change.
_NUMERIC_FIELDS: tuple[str, ...] = (
    "wind_dir_deg",
    "wind_speed_kt",
    "wind_gust_kt",
    "wave_height_m",
    "wave_period_s",
    "wave_period_avg_s",
    "wave_dir_deg",
    "pressure_mb",
    "pressure_tendency_mb",
    "air_temp_c",
    "water_temp_c",
    "dewpoint_c",
    "visibility_nmi",
    "tide_ft",
)


# ─── Helper ───────────────────────────────────────────────────────────

def _parse_obs_ts(raw: Any, fallback: float) -> float:
    """Parse the scraper's ``obs_ts`` ISO 8601 string to epoch seconds.

    Same shape as thermal_logger._parse_sample_ts — we want the
    sensor's ground-truth observation moment, not the receipt
    moment, so retained-payload replay across logger reconnects
    can't counterfeit fresh data for a stale observation.
    """
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            dt: datetime.datetime = datetime.datetime.fromisoformat(
                raw.replace("Z", "+00:00")
            )
            return dt.timestamp()
        except ValueError:
            pass
    return fallback


def _coerce_float(v: Any) -> Optional[float]:
    """Coerce to float or None — drop anything else without raising."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


# ─── Logger ───────────────────────────────────────────────────────────

class BuoyLogger:
    """NDBC observation → PostgreSQL writer."""

    def __init__(self, dsn: str = DEFAULT_DSN) -> None:
        """Open the DB connection and ensure schema."""
        self._dsn: str = dsn
        self._lock: threading.Lock = threading.Lock()
        self._conn: Any = None
        self._write_count: int = 0
        self._client: Optional["mqtt.Client"] = None
        self._subscriber_started: bool = False
        self._open()

    # -- DB lifecycle --------------------------------------------------------

    def _open(self) -> None:
        """Open the PG connection and create schema if needed."""
        if not _HAS_PSYCOPG2:
            logger.error("psycopg2 not installed — buoy logger disabled")
            return
        if not self._dsn:
            logger.warning(
                "buoy logger has no DSN — set postgres_dsn in /etc/glowup/"
                "secrets.json or GLOWUP_DIAG_DSN; logger disabled",
            )
            return
        try:
            self._conn = psycopg2.connect(self._dsn, connect_timeout=10)
            self._conn.autocommit = True
            with self._conn.cursor() as cur:
                cur.execute(_PG_DDL)
            logger.info(
                "buoy logger connected: %s",
                self._dsn.split("@")[-1],
            )
        except Exception as exc:
            logger.error("buoy logger DB open failed: %s", exc)
            self._conn = None

    def close(self) -> None:
        """Stop the subscriber and close the database."""
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as exc:
                logger.debug("buoy logger MQTT shutdown: %s", exc)
            self._client = None
            self._subscriber_started = False
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # -- Write path ----------------------------------------------------------

    def record(self, payload: dict[str, Any]) -> None:
        """Insert one observation row.  Idempotent on (station_id, obs_ts)."""
        if self._conn is None:
            return
        sid: Any = payload.get("station_id")
        if not isinstance(sid, str) or not sid:
            return
        now: float = time.time()
        obs_ts: float = _parse_obs_ts(payload.get("obs_ts"), now)
        values: tuple[Any, ...] = (
            sid, obs_ts, now,
            *(_coerce_float(payload.get(f)) for f in _NUMERIC_FIELDS),
        )
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        f"""INSERT INTO buoy_observations
                            (station_id, obs_ts, received_ts,
                             {", ".join(_NUMERIC_FIELDS)})
                            VALUES (%s, %s, %s, {", ".join(["%s"] * len(_NUMERIC_FIELDS))})
                            ON CONFLICT (station_id, obs_ts) DO NOTHING""",
                        values,
                    )
                # cur.rowcount is 0 on conflict, 1 on real insert.
                # Only count real inserts toward the prune trigger so
                # repeated re-fetches don't accelerate retention churn.
                if cur.rowcount > 0:
                    self._write_count += 1
                    if self._write_count % PRUNE_EVERY == 0:
                        self._prune()
            except Exception as exc:
                logger.warning(
                    "buoy logger write failed for %s: %s", sid, exc,
                )

    def _prune(self) -> None:
        """Delete rows older than :data:`RETENTION_SECONDS`."""
        if self._conn is None:
            return
        cutoff: float = time.time() - RETENTION_SECONDS
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM buoy_observations WHERE obs_ts < %s",
                    (cutoff,),
                )
                if cur.rowcount > 0:
                    logger.info("buoy logger pruned %d old row(s)", cur.rowcount)
        except Exception as exc:
            logger.warning("buoy logger prune failed: %s", exc)

    # -- Query surface -------------------------------------------------------

    def history(
        self,
        station_id: str,
        hours: float = 24.0,
        resolution_s: int = 0,
    ) -> list[dict[str, Any]]:
        """Return time-ordered observations for one station.

        Args:
            station_id:    NDBC station id.
            hours:         Window size in hours.  Beyond
                           RETENTION_SECONDS, results are empty.
            resolution_s:  If > 0, average into buckets of this many
                           seconds — drops chart cardinality for wide
                           windows.  ``0`` returns raw rows.

        Returns:
            List of dicts, one per bucket / row, oldest first.  Each
            dict carries ``obs_ts`` (epoch seconds float) and every
            numeric field (None when unavailable).
        """
        if self._conn is None:
            return []
        since: float = time.time() - (hours * 3600)
        if resolution_s and resolution_s > 0:
            avg_cols: str = ",\n                   ".join(
                f"AVG({f})::double precision AS {f}"
                for f in _NUMERIC_FIELDS
            )
            sql: str = f"""
                SELECT floor(obs_ts / %s)::bigint * %s AS obs_ts,
                       {avg_cols}
                FROM buoy_observations
                WHERE station_id = %s AND obs_ts >= %s
                GROUP BY 1
                ORDER BY 1
            """
            params: tuple[Any, ...] = (resolution_s, resolution_s, station_id, since)
        else:
            cols: str = ",\n                       ".join(_NUMERIC_FIELDS)
            sql = f"""
                SELECT obs_ts,
                       {cols}
                FROM buoy_observations
                WHERE station_id = %s AND obs_ts >= %s
                ORDER BY obs_ts
            """
            params = (station_id, since)
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(sql, params)
                    col_names: list[str] = [d[0] for d in cur.description]
                    return [
                        {
                            k: (float(v) if isinstance(v, (int, float)) else v)
                            for k, v in zip(col_names, row)
                        }
                        for row in cur.fetchall()
                    ]
            except Exception as exc:
                logger.warning(
                    "buoy logger history query failed for %s: %s",
                    station_id, exc,
                )
                return []

    def stations(self) -> list[str]:
        """Return distinct station ids present in the table."""
        if self._conn is None:
            return []
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT station_id FROM buoy_observations "
                        "ORDER BY station_id"
                    )
                    return [r[0] for r in cur.fetchall()]
            except Exception as exc:
                logger.warning("buoy logger stations query failed: %s", exc)
                return []

    # -- MQTT subscriber -----------------------------------------------------

    def start_subscriber(
        self,
        broker_host: str = "127.0.0.1",
        broker_port: int = 1883,
    ) -> None:
        """Connect a paho client and subscribe to the buoy firehose."""
        if not _HAS_PAHO:
            logger.warning("paho-mqtt not installed — buoy subscriber disabled")
            return
        if self._subscriber_started:
            logger.debug("buoy logger subscriber already running")
            return

        client: "mqtt.Client" = mqtt.Client(client_id="glowup-buoy-logger")
        client.on_connect = self._on_mqtt_connect
        client.on_message = self._on_mqtt_message
        client.on_disconnect = self._on_mqtt_disconnect

        try:
            client.connect(broker_host, broker_port, _MQTT_KEEPALIVE_S)
        except Exception as exc:
            logger.error(
                "buoy logger connect to %s:%d failed: %s",
                broker_host, broker_port, exc,
            )
            return
        client.loop_start()
        self._client = client
        self._subscriber_started = True
        logger.info(
            "buoy logger subscriber started — %s:%d topic=%s",
            broker_host, broker_port, BUOY_TOPIC_PATTERN,
        )

    def _on_mqtt_connect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        flags: dict[str, Any],
        rc: int,
    ) -> None:
        """(Re)subscribe on every connect — paho doesn't persist subs."""
        if rc == 0:
            client.subscribe(BUOY_TOPIC_PATTERN, qos=1)
            logger.info("buoy logger subscribed to %s", BUOY_TOPIC_PATTERN)
        else:
            logger.error("buoy logger connect rc=%d", rc)

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
                "buoy message on %s is not JSON: %s", msg.topic, exc,
            )
            return
        if not isinstance(payload, dict):
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
            logger.warning("buoy logger disconnect rc=%d", rc)
