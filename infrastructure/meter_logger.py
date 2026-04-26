"""Meter logger — records utility-meter telemetry to PostgreSQL.

Subscribes to ``glowup/meters/+`` on the canonical GlowUp MQTT broker
(hub, localhost in production).  Each parsed payload published by
:mod:`meters.publisher` becomes one row in ``meter_readings``.

Mirrors the :class:`infrastructure.thermal_logger.ThermalLogger`
pattern, with the lessons from the 2026-04-25 silent-death audit
incorporated from day one:

- The ``timestamp`` column stores the **sensor sample time**
  (``payload["ts"]``) — never receipt time.  Receipt time drives the
  rate-limiter only, and is held out as such.  ISO 8601 strings are
  parsed to epoch seconds via :func:`_parse_sample_ts`.
- Schema is validated at the MQTT boundary in
  :meth:`MeterLogger.record`.  A payload missing ``meter_id`` /
  ``meter_type`` / ``consumption`` is dropped with a logged warning,
  not silently zero-filled.
- Every ``except`` block logs the cause.  No bare excepts; no
  catch-and-pass.
- Ownership flagging — the ``ours BOOLEAN`` column — is driven by an
  installer-managed config file
  (``/etc/glowup/meters_owned.json``), never hardcoded in this
  module and never carried in the repo.

Civic motivation: the operator suspects the local water utility is
over-billing irrigation usage by an order of magnitude.  This logger
captures the actual radio transmissions the meter sends, independent
of the utility's reading, so a per-billing-cycle comparison
(:mod:`tools.meter_billing_compare`, Layer 3) can produce
third-party-independent evidence.

Schema::

    CREATE TABLE IF NOT EXISTS meter_readings (
        id BIGSERIAL PRIMARY KEY,
        timestamp DOUBLE PRECISION NOT NULL,
        meter_id TEXT NOT NULL,
        meter_type TEXT NOT NULL,
        consumption DOUBLE PRECISION,
        unit TEXT,
        tamper_phy SMALLINT,
        tamper_enc SMALLINT,
        physical_tamper SMALLINT,
        leak SMALLINT,
        no_use SMALLINT,
        source_node TEXT,
        ours BOOLEAN NOT NULL DEFAULT FALSE,
        raw JSONB
    );
    CREATE INDEX IF NOT EXISTS idx_meter_id_ts
        ON meter_readings(meter_id, timestamp);
    CREATE INDEX IF NOT EXISTS idx_meter_ts
        ON meter_readings(timestamp);
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import datetime
import json
import logging
import os
import threading
import time
from typing import Any, Optional

try:
    import psycopg2
    import psycopg2.extras
    _HAS_PSYCOPG2: bool = True
except ImportError:
    _HAS_PSYCOPG2 = False

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False

logger: logging.Logger = logging.getLogger("glowup.meter_logger")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default PostgreSQL DSN.  Overridden by GLOWUP_DIAG_DSN in server.py
# wiring (same env var the thermal/power loggers honor).
DEFAULT_DSN: str = "postgresql://glowup:changeme@10.0.0.111:5432/glowup"

# 90-day retention.  Operator-blessed 2026-04-25 across both ours and
# neighbor rows; civic-aggregate stats (if ever computed) get stored
# elsewhere on a longer horizon.
RETENTION_SECONDS: float = 90 * 24 * 3600

# Minimum seconds between writes for the same meter.  ERT broadcasts
# every 30-60s; R900 water meters sometimes faster when polled by a
# drive-by reader.  5 min keeps signal density useful (12 samples /
# meter / hour) and bounds storage at a few MB / meter / year.
MIN_WRITE_INTERVAL_S: float = 300.0

# Prune old rows every N writes.
PRUNE_EVERY: int = 200

# MQTT topic wildcard.
METERS_TOPIC_PATTERN: str = "glowup/meters/+"

# paho keepalive (seconds).
_MQTT_KEEPALIVE_S: int = 60

# Default location for the owned-meters config.  Lives outside the
# repo per project convention (installer-managed).  Format::
#
#     {
#         "owned": [
#             {"meter_id": "4599052",
#              "type": "electric", "class": "main",
#              "utility": "Alabama Power", "account": "13294-75051"},
#             ...
#         ]
#     }
#
# Only the ``meter_id`` field is required by this logger; the rest is
# carried for the billing-comparison tool downstream.
DEFAULT_OWNED_PATH: str = "/etc/glowup/meters_owned.json"


_PG_DDL: str = """
CREATE TABLE IF NOT EXISTS meter_readings (
    id BIGSERIAL PRIMARY KEY,
    timestamp DOUBLE PRECISION NOT NULL,
    meter_id TEXT NOT NULL,
    meter_type TEXT NOT NULL,
    consumption DOUBLE PRECISION,
    unit TEXT,
    tamper_phy SMALLINT,
    tamper_enc SMALLINT,
    physical_tamper SMALLINT,
    leak SMALLINT,
    no_use SMALLINT,
    source_node TEXT,
    ours BOOLEAN NOT NULL DEFAULT FALSE,
    raw JSONB
);
CREATE INDEX IF NOT EXISTS idx_meter_id_ts
    ON meter_readings(meter_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_meter_ts
    ON meter_readings(timestamp);
"""


# ---------------------------------------------------------------------------
# Sample-time parser (sample-vs-receipt distinction; see audit 2026-04-25)
# ---------------------------------------------------------------------------


def _parse_sample_ts(raw: Any, fallback: float) -> float:
    """Parse the publisher's ``ts`` field into epoch seconds.

    Accepts ISO 8601 strings (the format
    :mod:`meters.publisher` emits, e.g.
    ``"2026-04-25T20:13:37Z"``) or numeric epoch values.  Unparseable
    or missing input falls back to ``fallback`` (typically receipt
    time) **and logs the coercion failure** — the silent fallback
    pattern is exactly the audit's flagged anti-pattern.

    Mirrors the same helper in
    :mod:`infrastructure.thermal_logger`.  Lifted into a shared
    ``infrastructure._ts`` module post-freeze.

    Args:
        raw:      The ``ts`` field value from the publisher payload.
        fallback: Epoch seconds to return when ``raw`` is missing
                  or unparseable.

    Returns:
        Epoch seconds (float).
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
            logger.warning(
                "meter ts %r is not parseable ISO-8601 — falling back "
                "to receipt time (this row will mis-stamp; investigate "
                "the publisher)",
                raw,
            )
            return fallback
    if raw is not None:
        logger.warning(
            "meter ts has unexpected type %s — falling back to "
            "receipt time",
            type(raw).__name__,
        )
    return fallback


# ---------------------------------------------------------------------------
# Owned-meters config
# ---------------------------------------------------------------------------


def load_owned_meter_ids(path: str = DEFAULT_OWNED_PATH) -> set[str]:
    """Read the owned-meters config and return the set of owned IDs.

    Returns an empty set on any failure with a logged warning — that
    means everything will be flagged ``ours=false`` (neighbor) until
    the config is fixed.  Never raises: a missing config must not
    crash the logger.

    Args:
        path: Filesystem path to the owned-meters JSON.

    Returns:
        Set of meter_id strings, or empty set on failure.
    """
    try:
        with open(path, "r") as f:
            doc: dict[str, Any] = json.load(f)
    except FileNotFoundError:
        logger.info(
            "owned-meters config not found at %s — all meter rows "
            "will be flagged ours=false",
            path,
        )
        return set()
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "owned-meters config %s unreadable: %s — all meter rows "
            "will be flagged ours=false until fixed",
            path, exc,
        )
        return set()

    owned_raw: Any = doc.get("owned")
    if not isinstance(owned_raw, list):
        logger.warning(
            "owned-meters config at %s missing 'owned' list — "
            "all meter rows will be flagged ours=false",
            path,
        )
        return set()

    ids: set[str] = set()
    for entry in owned_raw:
        if not isinstance(entry, dict):
            logger.warning("owned-meters entry not an object: %r", entry)
            continue
        mid: Any = entry.get("meter_id")
        if not isinstance(mid, str) or not mid:
            logger.warning(
                "owned-meters entry missing meter_id: %r", entry,
            )
            continue
        ids.add(mid)
    logger.info(
        "owned-meters loaded from %s: %d meter id(s)", path, len(ids),
    )
    return ids


# ---------------------------------------------------------------------------
# MeterLogger
# ---------------------------------------------------------------------------


class MeterLogger:
    """Records utility-meter telemetry to PostgreSQL via MQTT subscribe.

    Instantiate once per process with a DSN.  Call
    :meth:`start_subscriber` to begin ingesting telemetry; call
    :meth:`close` on shutdown.

    Args:
        dsn:        PostgreSQL connection string.
        owned_path: Filesystem path to the owned-meters config.
                    Read once at construction; reload via
                    :meth:`reload_owned`.
    """

    def __init__(
        self,
        dsn: str = DEFAULT_DSN,
        owned_path: str = DEFAULT_OWNED_PATH,
    ) -> None:
        """See class docstring."""
        self._dsn: str = dsn
        self._owned_path: str = owned_path
        self._lock: threading.Lock = threading.Lock()
        self._conn: Any = None
        self._write_count: int = 0
        self._last_write: dict[str, float] = {}
        self._owned: set[str] = load_owned_meter_ids(owned_path)
        self._client: Optional["mqtt.Client"] = None
        self._subscriber_started: bool = False
        self._open()

    # ---- Owned-list management ---------------------------------------------

    def reload_owned(self) -> None:
        """Re-read the owned-meters config (e.g. on SIGHUP)."""
        new_set: set[str] = load_owned_meter_ids(self._owned_path)
        with self._lock:
            self._owned = new_set

    def is_owned(self, meter_id: str) -> bool:
        """Return ``True`` if ``meter_id`` is in the owned-meters list."""
        with self._lock:
            return meter_id in self._owned

    # ---- DB lifecycle -------------------------------------------------------

    def _open(self) -> None:
        """Open the PG connection and create schema if needed."""
        if not _HAS_PSYCOPG2:
            logger.error("psycopg2 not installed — meter logger disabled")
            return
        try:
            self._conn = psycopg2.connect(self._dsn, connect_timeout=10)
            self._conn.autocommit = True
            with self._conn.cursor() as cur:
                cur.execute(_PG_DDL)
            logger.info(
                "Meter logger connected: %s",
                self._dsn.split("@")[-1],
            )
        except Exception as exc:
            logger.error("Meter logger DB open failed: %s", exc)
            self._conn = None

    # ---- Write path ---------------------------------------------------------

    def record(self, payload: dict[str, Any], source_node: str = "") -> None:
        """Insert a meter reading.

        Validates the payload at the boundary.  Drops with a logged
        warning if any required field is missing or the wrong type;
        does **not** zero-fill or otherwise fabricate plausible
        defaults.

        Throttled to :data:`MIN_WRITE_INTERVAL_S` per meter (last-
        write-wins inside the window).  The throttle uses receipt
        time, not sample time, since its purpose is to bound DB
        growth rather than represent measurement physics.

        Args:
            payload:     Parsed JSON dict from an MQTT message.
            source_node: Hostname of the publisher (extracted upstream
                         from MQTT topic or set explicitly).  Stored
                         as-is; missing → empty string.
        """
        if self._conn is None:
            return

        # ---- Schema validation at the MQTT boundary ----
        if not isinstance(payload, dict):
            logger.warning(
                "meter payload is not a dict: %r",
                type(payload).__name__,
            )
            return

        meter_id: Any = payload.get("meter_id")
        if not isinstance(meter_id, str) or not meter_id:
            logger.warning(
                "meter payload missing meter_id (source_node=%s) — drop",
                source_node,
            )
            return

        meter_type: Any = payload.get("meter_type")
        if not isinstance(meter_type, str) or not meter_type:
            logger.warning(
                "meter payload (id=%s) missing meter_type — drop",
                meter_id,
            )
            return

        consumption: Any = payload.get("consumption")
        if consumption is not None and not isinstance(consumption, (int, float)):
            logger.warning(
                "meter payload (id=%s type=%s) consumption is not "
                "numeric: %r — drop",
                meter_id, meter_type, type(consumption).__name__,
            )
            return

        # ---- Sample vs receipt time ----
        now: float = time.time()
        sample_ts: float = _parse_sample_ts(payload.get("ts"), now)

        # ---- Per-meter rate limit ----
        with self._lock:
            last: float = self._last_write.get(meter_id, 0.0)
            if now - last < MIN_WRITE_INTERVAL_S:
                return
            self._last_write[meter_id] = now
            ours: bool = meter_id in self._owned

        unit: Any = payload.get("unit")
        if unit is not None and not isinstance(unit, str):
            unit = None
        tamper_phy: Optional[int] = _coerce_int(payload.get("tamper_phy"))
        tamper_enc: Optional[int] = _coerce_int(payload.get("tamper_enc"))
        physical_tamper: Optional[int] = _coerce_int(
            payload.get("physical_tamper"),
        )
        leak: Optional[int] = _coerce_int(payload.get("leak"))
        no_use: Optional[int] = _coerce_int(payload.get("no_use"))
        raw: Any = payload.get("raw")
        raw_json: Optional[str] = None
        if isinstance(raw, dict):
            try:
                raw_json = json.dumps(raw)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "meter raw payload not JSON-serializable for "
                    "meter_id=%s: %s",
                    meter_id, exc,
                )
                raw_json = None

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO meter_readings
                       (timestamp, meter_id, meter_type,
                        consumption, unit,
                        tamper_phy, tamper_enc,
                        physical_tamper, leak, no_use,
                        source_node, ours, raw)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        sample_ts, meter_id, meter_type,
                        float(consumption) if consumption is not None else None,
                        unit,
                        tamper_phy, tamper_enc,
                        physical_tamper, leak, no_use,
                        source_node, ours, raw_json,
                    ),
                )
            self._write_count += 1
            if self._write_count % PRUNE_EVERY == 0:
                self._prune()
        except Exception as exc:
            logger.warning(
                "Meter logger write failed for meter_id=%s: %s",
                meter_id, exc,
            )

    def _prune(self) -> None:
        """Delete rows older than :data:`RETENTION_SECONDS`."""
        if self._conn is None:
            return
        cutoff: float = time.time() - RETENTION_SECONDS
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM meter_readings WHERE timestamp < %s",
                    (cutoff,),
                )
                deleted: int = cur.rowcount
            if deleted > 0:
                logger.info("meter prune: deleted %d rows older than 90d",
                            deleted)
        except Exception as exc:
            logger.warning("meter prune failed: %s", exc)

    # ---- Read path ----------------------------------------------------------

    def latest(self) -> dict[str, dict[str, Any]]:
        """Return the most recent row per meter_id.

        Returns:
            Dict mapping ``meter_id`` to a reading dict.  Empty on
            any failure (logged).
        """
        if self._conn is None:
            return {}
        sql: str = """
            SELECT m.meter_id, m.timestamp, m.meter_type,
                   m.consumption, m.unit,
                   m.tamper_phy, m.tamper_enc,
                   m.physical_tamper, m.leak, m.no_use,
                   m.source_node, m.ours
            FROM meter_readings m
            INNER JOIN (
                SELECT meter_id, MAX(timestamp) AS max_ts
                FROM meter_readings
                GROUP BY meter_id
            ) x ON m.meter_id = x.meter_id AND m.timestamp = x.max_ts
        """
        result: dict[str, dict[str, Any]] = {}
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(sql)
                    for row in cur.fetchall():
                        (mid, ts, mtype, cons, unit,
                         tphy, tenc, ptamper, leak, no_use,
                         src, ours) = row
                        result[mid] = {
                            "meter_id": mid,
                            "timestamp": float(ts),
                            "meter_type": mtype,
                            "consumption": cons,
                            "unit": unit,
                            "tamper_phy": tphy,
                            "tamper_enc": tenc,
                            "physical_tamper": ptamper,
                            "leak": leak,
                            "no_use": no_use,
                            "source_node": src,
                            "ours": bool(ours),
                        }
            except Exception as exc:
                logger.warning("meter latest query failed: %s", exc)
        return result

    def meter_ids(self, ours_only: bool = False) -> list[str]:
        """Return distinct meter_ids ever seen.

        Args:
            ours_only: If True, restrict to owned meters only.
        """
        if self._conn is None:
            return []
        sql: str = "SELECT DISTINCT meter_id FROM meter_readings"
        params: tuple[Any, ...] = ()
        if ours_only:
            sql += " WHERE ours = TRUE"
        sql += " ORDER BY meter_id"
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(sql, params)
                    return [r[0] for r in cur.fetchall()]
            except Exception as exc:
                logger.warning("meter ids query failed: %s", exc)
                return []

    # ---- MQTT subscriber ---------------------------------------------------

    def start_subscriber(
        self,
        broker_host: str = "127.0.0.1",
        broker_port: int = 1883,
    ) -> None:
        """Connect a paho client and subscribe to meter telemetry."""
        if not _HAS_PAHO:
            logger.warning(
                "paho-mqtt not installed — meter subscriber disabled. "
                "Install with: sudo apt install -y python3-paho-mqtt",
            )
            return
        if self._subscriber_started:
            logger.debug("meter subscriber already running")
            return

        client: "mqtt.Client" = mqtt.Client(client_id="glowup-meter-logger")
        client.on_connect = self._on_mqtt_connect
        client.on_message = self._on_mqtt_message
        client.on_disconnect = self._on_mqtt_disconnect

        try:
            client.connect(broker_host, broker_port, _MQTT_KEEPALIVE_S)
        except Exception as exc:
            logger.error(
                "Meter subscriber connect to %s:%d failed: %s",
                broker_host, broker_port, exc,
            )
            return
        client.loop_start()
        self._client = client
        self._subscriber_started = True
        logger.info(
            "Meter subscriber started — %s:%d pattern=%s",
            broker_host, broker_port, METERS_TOPIC_PATTERN,
        )

    def _on_mqtt_connect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        flags: dict[str, Any],
        rc: int,
    ) -> None:
        """paho callback — subscribe to the meters topic on connect."""
        if rc == 0:
            client.subscribe(METERS_TOPIC_PATTERN, qos=1)
            logger.info("meter subscriber subscribed to %s",
                        METERS_TOPIC_PATTERN)
        else:
            logger.error("meter subscriber connect rc=%d", rc)

    def _on_mqtt_message(
        self,
        client: "mqtt.Client",
        userdata: Any,
        msg: "mqtt.MQTTMessage",
    ) -> None:
        """paho callback — parse JSON and record."""
        try:
            payload: Any = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning(
                "meter message on %s is not JSON: %s", msg.topic, exc,
            )
            return
        # The publisher's source-node tag isn't carried in the payload
        # today.  If we want it, the publisher will need to add a
        # ``source_node`` field; for now we leave the column empty and
        # revisit when the publisher is deployed.
        self.record(payload, source_node="")

    def _on_mqtt_disconnect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        rc: int,
    ) -> None:
        """paho callback — log unexpected disconnects."""
        if rc != 0:
            logger.warning(
                "meter subscriber unexpected disconnect rc=%d", rc,
            )

    # ---- Shutdown -----------------------------------------------------------

    def close(self) -> None:
        """Stop the subscriber and close the database."""
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as exc:
                logger.warning("meter subscriber shutdown error: %s", exc)
            self._client = None
            self._subscriber_started = False
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as exc:
                logger.warning("meter DB close error: %s", exc)
            self._conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_int(v: Any) -> Optional[int]:
    """Coerce ``v`` to ``int`` if cleanly possible; else ``None``.

    Used for the small-integer flag columns (tamper, leak, no_use).
    Logs nothing on miss because some packets simply lack the field —
    that is normal, not an error.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v != v:  # NaN
            return None
        return int(v)
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            try:
                return int(float(v))
            except ValueError:
                return None
    return None
