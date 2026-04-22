"""BLE sniffer logger — persists ernie's BLE v2 state stream to PostgreSQL.

Subscribes to two related topic classes published by the v2 BLE
sniffer on ernie (.153) — see ``contrib/sensors/ble_sniffer.py``:

- ``glowup/ble/seen/<mac>``   retained snapshot of the current state
                              of every MAC the sniffer has recently
                              tracked.  Upserted on receipt.
- ``glowup/ble/events/<mac>`` non-retained per-event log (appearance,
                              loss, RPA rotation).  Appended on
                              receipt.

Mirrors :class:`infrastructure.thermal_logger.ThermalLogger` — same
psycopg2 + paho lifecycle, same prune cadence, same guarded imports.
Splitting the two tables rather than collapsing into one
``ble_observations`` keeps the storage shape aligned with the
upstream topic taxonomy, so the logger never has to decide between
overwrite and append on the same row.

Schemas::

    CREATE TABLE IF NOT EXISTS ble_seen (
        mac TEXT PRIMARY KEY,
        first_heard_ts DOUBLE PRECISION,
        last_heard_ts  DOUBLE PRECISION,
        gone           SMALLINT DEFAULT 0,
        payload        JSONB,
        updated_ts     DOUBLE PRECISION
    );

    CREATE TABLE IF NOT EXISTS ble_events (
        id BIGSERIAL PRIMARY KEY,
        timestamp DOUBLE PRECISION NOT NULL,
        mac       TEXT NOT NULL,
        event     TEXT,
        payload   JSONB
    );
    CREATE INDEX IF NOT EXISTS idx_ble_events_mac_ts
        ON ble_events(mac, timestamp);
    CREATE INDEX IF NOT EXISTS idx_ble_events_ts
        ON ble_events(timestamp);

``ble_seen`` is naturally bounded by distinct MACs ever observed and
is **not** pruned; ``ble_events`` rolls over with a 30-day retention.
The ``gone`` marker is preserved rather than deleting the row so the
dashboard can render "recently departed" devices, matching the v2
sniffer's retained-gone-marker convention.
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

logger: logging.Logger = logging.getLogger("glowup.ble_sniffer_logger")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default PostgreSQL DSN.  Override via GLOWUP_DIAG_DSN env var in server.py.
DEFAULT_DSN: str = "postgresql://glowup:changeme@10.0.0.111:5432/glowup"

# 30-day retention on the append-only event log.  `ble_seen` is
# bounded by distinct MACs — no retention needed.
EVENTS_RETENTION_SECONDS: float = 30 * 24 * 3600

# Throttle `ble_seen` UPSERTs per MAC.  Retained messages on
# glowup/ble/seen/<mac> refresh every few seconds for active
# devices; 10 s per-MAC write gate keeps the primary-key hot path
# off the disk without losing the device-state signal.
MIN_SEEN_WRITE_INTERVAL_S: float = 10.0

# Prune old event rows every N event writes.
PRUNE_EVERY: int = 500

# MQTT topic wildcards.
SEEN_TOPIC_PATTERN: str = "glowup/ble/seen/#"
EVENTS_TOPIC_PATTERN: str = "glowup/ble/events/#"

# paho keepalive (seconds).
_MQTT_KEEPALIVE_S: int = 60

# How many recent events the dashboard's event-feed endpoint returns.
# 500 matches the old in-process ring buffer cap so clients don't
# see a behavior change.
DEFAULT_EVENTS_TAIL: int = 500


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_PG_DDL: str = """
CREATE TABLE IF NOT EXISTS ble_seen (
    mac            TEXT PRIMARY KEY,
    first_heard_ts DOUBLE PRECISION,
    last_heard_ts  DOUBLE PRECISION,
    gone           SMALLINT DEFAULT 0,
    payload        JSONB,
    updated_ts     DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS ble_events (
    id BIGSERIAL PRIMARY KEY,
    timestamp DOUBLE PRECISION NOT NULL,
    mac       TEXT NOT NULL,
    event     TEXT,
    payload   JSONB
);
CREATE INDEX IF NOT EXISTS idx_ble_events_mac_ts
    ON ble_events(mac, timestamp);
CREATE INDEX IF NOT EXISTS idx_ble_events_ts
    ON ble_events(timestamp);
"""


# ---------------------------------------------------------------------------
# BleSnifferLogger
# ---------------------------------------------------------------------------


class BleSnifferLogger:
    """Persists the BLE v2 sniffer state + event streams to PostgreSQL.

    Args:
        dsn: PostgreSQL connection string.
    """

    def __init__(self, dsn: str = DEFAULT_DSN) -> None:
        """See class docstring."""
        self._dsn: str = dsn
        self._lock: threading.Lock = threading.Lock()
        self._conn: Any = None
        self._event_write_count: int = 0
        # Per-MAC throttle for `ble_seen` UPSERTs.
        self._last_seen_write: dict[str, float] = {}
        self._client: Optional["mqtt.Client"] = None
        self._subscriber_started: bool = False
        self._open()

    # ---- DB lifecycle -------------------------------------------------------

    def _open(self) -> None:
        """Open the PG connection and create schema if needed."""
        if not _HAS_PSYCOPG2:
            logger.error(
                "psycopg2 not installed — ble sniffer logger disabled",
            )
            return
        try:
            self._conn = psycopg2.connect(self._dsn, connect_timeout=10)
            self._conn.autocommit = True
            with self._conn.cursor() as cur:
                cur.execute(_PG_DDL)
            logger.info(
                "BLE sniffer logger connected: %s",
                self._dsn.split("@")[-1],
            )
        except Exception as exc:
            logger.error("BLE sniffer logger DB open failed: %s", exc)
            self._conn = None

    # ---- Write paths --------------------------------------------------------

    def record_seen(self, mac: str, payload: dict[str, Any]) -> None:
        """UPSERT one ``glowup/ble/seen/<mac>`` snapshot.

        The v2 sniffer publishes a payload with ``gone=True`` to mark
        a MAC as recently departed.  Rather than deleting the row the
        logger preserves prior state and only flips the ``gone`` flag
        — matches the dashboard contract that "gone" devices still
        render, just de-emphasized.

        Args:
            mac:     MAC address (topic tail).
            payload: Parsed JSON dict from the retained message.
        """
        if self._conn is None:
            return
        if not mac:
            logger.debug("ble_seen write missing mac, dropping")
            return

        now: float = time.time()
        with self._lock:
            last: float = self._last_seen_write.get(mac, 0.0)
            # "gone" transitions are always written even under the
            # throttle — a departure is too rare to drop.
            is_gone: bool = bool(payload.get("gone"))
            if not is_gone and (now - last) < MIN_SEEN_WRITE_INTERVAL_S:
                return
            self._last_seen_write[mac] = now

            last_heard: Optional[float] = _coerce_float(
                payload.get("last_heard_ts"),
            )
            first_heard: Optional[float] = _coerce_float(
                payload.get("first_heard_ts"),
            )
            try:
                with self._conn.cursor() as cur:
                    # UPSERT — first_heard is set on insert and
                    # preserved on conflict (it's the earliest
                    # timestamp we've ever seen for the MAC).
                    # last_heard / payload / gone always advance.
                    cur.execute(
                        """INSERT INTO ble_seen
                           (mac, first_heard_ts, last_heard_ts,
                            gone, payload, updated_ts)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT (mac) DO UPDATE SET
                             last_heard_ts = COALESCE(
                               EXCLUDED.last_heard_ts,
                               ble_seen.last_heard_ts
                             ),
                             gone = EXCLUDED.gone,
                             payload = EXCLUDED.payload,
                             updated_ts = EXCLUDED.updated_ts""",
                        (
                            mac,
                            first_heard or last_heard or now,
                            last_heard or now,
                            1 if is_gone else 0,
                            _PgJson(payload) if _PgJson else None,
                            now,
                        ),
                    )
            except Exception as exc:
                logger.warning(
                    "BLE seen UPSERT failed for %s: %s", mac, exc,
                )

    def record_event(self, mac: str, payload: dict[str, Any]) -> None:
        """Append one ``glowup/ble/events/<mac>`` event row.

        Args:
            mac:     MAC address (topic tail).
            payload: Parsed JSON dict from the event message.
        """
        if self._conn is None:
            return
        if not mac:
            logger.debug("ble_events write missing mac, dropping")
            return

        now: float = time.time()
        event_type: Optional[str] = payload.get("event") or payload.get("type")
        ts: Optional[float] = (
            _coerce_float(payload.get("ts"))
            or _coerce_float(payload.get("timestamp"))
            or now
        )
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO ble_events
                           (timestamp, mac, event, payload)
                           VALUES (%s, %s, %s, %s)""",
                        (
                            ts, mac, event_type,
                            _PgJson(payload) if _PgJson else None,
                        ),
                    )
                self._event_write_count += 1
                if self._event_write_count % PRUNE_EVERY == 0:
                    self._prune_events()
            except Exception as exc:
                logger.warning(
                    "BLE event insert failed for %s: %s", mac, exc,
                )

    def _prune_events(self) -> None:
        """Delete event rows older than :data:`EVENTS_RETENTION_SECONDS`."""
        if self._conn is None:
            return
        cutoff: float = time.time() - EVENTS_RETENTION_SECONDS
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM ble_events WHERE timestamp < %s",
                    (cutoff,),
                )
                if cur.rowcount > 0:
                    logger.info(
                        "BLE sniffer logger pruned %d old event(s)",
                        cur.rowcount,
                    )
        except Exception as exc:
            logger.warning("BLE event prune failed: %s", exc)

    # ---- Query surface ------------------------------------------------------

    def catalog(
        self,
        window_s: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """Return MACs the sniffer has seen, freshest first.

        Drives the ``/api/ernie/ble`` dashboard endpoint.  Keeps
        ``gone`` entries at the bottom to match prior ordering.

        Args:
            window_s: If set, only include MACs whose
                ``last_heard_ts`` is within ``now - window_s``.
                ``gone`` entries are filtered by ``updated_ts``
                instead — a recently-departed MAC stays visible for
                the window after the retired-marker arrives and then
                rotates out.  ``None`` returns the full catalog
                (every MAC ever seen).

        Returns:
            List of dicts — the ``payload`` JSONB is unwrapped and
            enriched with ``gone`` and ``last_heard_ts``.
        """
        if self._conn is None:
            return []
        # The dashboard cares about "what was recently audible", so a
        # present MAC filters on `last_heard_ts` and a gone-marked MAC
        # filters on `updated_ts` (the flip-to-gone moment) — otherwise
        # a MAC that went gone hours ago but has a stale
        # `last_heard_ts` would keep appearing.
        clause: str = ""
        params: tuple = ()
        if window_s is not None and window_s > 0:
            cutoff: float = time.time() - float(window_s)
            clause = (
                "WHERE (gone = 0 AND last_heard_ts >= %s) "
                "   OR (gone = 1 AND COALESCE(updated_ts, 0) >= %s)"
            )
            params = (cutoff, cutoff)
        sql: str = f"""
            SELECT mac, first_heard_ts, last_heard_ts,
                   gone, payload
            FROM ble_seen
            {clause}
            ORDER BY gone ASC,
                     last_heard_ts DESC NULLS LAST
        """
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(sql, params)
                    out: list[dict[str, Any]] = []
                    for mac, first_s, last_s, gone, payload in cur.fetchall():
                        entry: dict[str, Any] = dict(payload or {})
                        entry["mac"] = mac
                        if first_s is not None:
                            entry.setdefault("first_heard_ts", float(first_s))
                        if last_s is not None:
                            entry["last_heard_ts"] = float(last_s)
                        entry["gone"] = bool(gone)
                        out.append(entry)
                    return out
            except Exception as exc:
                logger.warning(
                    "BLE catalog query failed: %s", exc,
                )
                return []

    def events_tail(self, limit: int = DEFAULT_EVENTS_TAIL) -> list[dict[str, Any]]:
        """Return the most recent ``limit`` events, oldest-first.

        Oldest-first matches the "log file tail" mental model of the
        dashboard event panel (new rows append at the bottom).
        """
        if self._conn is None:
            return []
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """SELECT timestamp, mac, event, payload
                           FROM ble_events
                           ORDER BY id DESC
                           LIMIT %s""",
                        (limit,),
                    )
                    rows: list[Any] = list(cur.fetchall())
            except Exception as exc:
                logger.warning("BLE events_tail query failed: %s", exc)
                return []
        rows.reverse()
        out: list[dict[str, Any]] = []
        for ts, mac, event, payload in rows:
            entry: dict[str, Any] = dict(payload or {})
            entry["mac"] = mac
            entry["ts"] = float(ts)
            if event:
                entry.setdefault("event", event)
            out.append(entry)
        return out

    def last_heard_ts(self) -> float:
        """Return the freshest ``last_heard_ts`` across all MACs, or 0.

        Drives the dashboard's "ble-sniffer alive" health heuristic.
        """
        if self._conn is None:
            return 0.0
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT MAX(last_heard_ts) FROM ble_seen",
                    )
                    row: Any = cur.fetchone()
                    if row and row[0] is not None:
                        return float(row[0])
            except Exception as exc:
                logger.warning(
                    "BLE last_heard_ts query failed: %s", exc,
                )
        return 0.0

    # ---- MQTT subscriber ----------------------------------------------------

    def start_subscriber(
        self,
        broker_host: str = "127.0.0.1",
        broker_port: int = 1883,
    ) -> None:
        """Connect a paho client and subscribe to both BLE topic classes.

        Guarded — if paho-mqtt is not importable the subscriber is a
        no-op.

        Args:
            broker_host: MQTT broker hostname or IP.
            broker_port: MQTT broker TCP port.
        """
        if not _HAS_PAHO:
            logger.warning(
                "paho-mqtt not installed — ble subscriber disabled",
            )
            return
        if self._subscriber_started:
            logger.debug("ble subscriber already running")
            return

        client: mqtt.Client = mqtt.Client(
            client_id="glowup-ble-sniffer-logger",
        )
        client.on_connect = self._on_mqtt_connect
        client.on_message = self._on_mqtt_message
        client.on_disconnect = self._on_mqtt_disconnect

        try:
            client.connect(broker_host, broker_port, _MQTT_KEEPALIVE_S)
        except Exception as exc:
            logger.error(
                "BLE subscriber connect to %s:%d failed: %s",
                broker_host, broker_port, exc,
            )
            return
        client.loop_start()
        self._client = client
        self._subscriber_started = True
        logger.info(
            "BLE sniffer subscriber started — %s:%d patterns=%s,%s",
            broker_host, broker_port,
            SEEN_TOPIC_PATTERN, EVENTS_TOPIC_PATTERN,
        )

    def _on_mqtt_connect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        flags: dict[str, Any],
        rc: int,
    ) -> None:
        """paho callback — (re-)subscribe on every connect.

        Re-subscribe on every connect is mandatory for paho to
        survive a silent reconnect; see
        ``feedback_paho_resubscribe_on_connect.md``.
        """
        if rc == 0:
            client.subscribe(SEEN_TOPIC_PATTERN, qos=1)
            client.subscribe(EVENTS_TOPIC_PATTERN, qos=1)
            logger.info(
                "ble subscriber subscribed to %s and %s",
                SEEN_TOPIC_PATTERN, EVENTS_TOPIC_PATTERN,
            )
        else:
            logger.error("ble subscriber connect rc=%d", rc)

    def _on_mqtt_message(
        self,
        client: "mqtt.Client",
        userdata: Any,
        msg: "mqtt.MQTTMessage",
    ) -> None:
        """paho callback — route by topic prefix."""
        topic: str = msg.topic or ""
        try:
            payload: dict[str, Any] = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning(
                "ble message on %s is not JSON: %s", topic, exc,
            )
            return
        if not isinstance(payload, dict):
            logger.warning(
                "ble message on %s is not a JSON object: %r",
                topic, type(payload).__name__,
            )
            return

        mac: str = topic.rsplit("/", 1)[-1] if "/" in topic else ""
        # Payload-level mac always wins over the topic tail — some
        # retained publishers normalise case only in the payload.
        mac = str(payload.get("mac") or mac)
        if not mac:
            return

        if topic.startswith("glowup/ble/seen/"):
            self.record_seen(mac, payload)
        elif topic.startswith("glowup/ble/events/"):
            self.record_event(mac, payload)
        else:
            logger.debug("ble subscriber ignoring topic %s", topic)

    def _on_mqtt_disconnect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        rc: int,
    ) -> None:
        """paho callback — log unexpected disconnects."""
        if rc != 0:
            logger.warning("ble subscriber unexpected disconnect rc=%d", rc)

    # ---- Shutdown -----------------------------------------------------------

    def close(self) -> None:
        """Stop the subscriber and close the database."""
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as exc:
                logger.warning("ble subscriber shutdown error: %s", exc)
            self._client = None
            self._subscriber_started = False
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as exc:
                logger.warning("ble PG close error: %s", exc)
            self._conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_float(val: Any) -> Optional[float]:
    """Best-effort numeric coercion.

    Returns ``None`` on any failure so columns can be stored as NULL.
    """
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
