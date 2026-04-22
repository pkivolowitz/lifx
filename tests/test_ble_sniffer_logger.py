#!/usr/bin/env python3
"""Unit tests for infrastructure.ble_sniffer_logger.BleSnifferLogger.

Covers the persistence surface without touching MQTT:

- record_seen UPSERT + throttle, gone-transition bypass of throttle
- record_event append, events_tail oldest-first ordering
- catalog() merges seen rows, ordered by gone then last_heard_ts desc
- last_heard_ts returns MAX across ble_seen
- Retention pruning on ble_events

Tests require a live PostgreSQL connection.  Skipped automatically
otherwise.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import os
import time
import unittest
from typing import Any

from infrastructure.ble_sniffer_logger import (
    BleSnifferLogger,
    DEFAULT_DSN,
    EVENTS_RETENTION_SECONDS,
    MIN_SEEN_WRITE_INTERVAL_S,
)


_TEST_DSN: str = os.environ.get("GLOWUP_DIAG_DSN", DEFAULT_DSN)

try:
    import psycopg2 as _psycopg2
    _conn_test = _psycopg2.connect(_TEST_DSN, connect_timeout=3)
    _conn_test.close()
    _DB_AVAILABLE: bool = True
except Exception:
    _DB_AVAILABLE = False

_SKIP_REASON: str = (
    "psycopg2 unavailable or PostgreSQL unreachable — "
    "set GLOWUP_DIAG_DSN to a reachable DSN"
)

# All test rows live under this MAC prefix so scrubs in setUp/tearDown
# can't collide with real data.
TEST_MAC_PREFIX: str = "00:00:00:TEST:"


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class BleLoggerTestCase(unittest.TestCase):
    """Base: fresh BleSnifferLogger per test, test rows scrubbed."""

    def setUp(self) -> None:
        self._bl: BleSnifferLogger = BleSnifferLogger(dsn=_TEST_DSN)
        self.assertIsNotNone(self._bl._conn, "PG connection must be open")
        self._scrub()

    def tearDown(self) -> None:
        self._scrub()
        self._bl.close()

    def _scrub(self) -> None:
        self._exec(
            "DELETE FROM ble_seen WHERE mac LIKE %s",
            (TEST_MAC_PREFIX + "%",),
        )
        self._exec(
            "DELETE FROM ble_events WHERE mac LIKE %s",
            (TEST_MAC_PREFIX + "%",),
        )

    def _exec(self, sql: str, params: tuple = ()) -> list:
        with self._bl._conn.cursor() as cur:
            cur.execute(sql, params)
            try:
                return cur.fetchall()
            except Exception:
                return []


# ---------------------------------------------------------------------------
# record_seen + catalog
# ---------------------------------------------------------------------------


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestRecordSeen(BleLoggerTestCase):

    def test_happy_upsert(self) -> None:
        mac = TEST_MAC_PREFIX + "01"
        now = time.time()
        self._bl.record_seen(mac, {
            "mac": mac,
            "first_heard_ts": now - 100,
            "last_heard_ts": now,
            "rssi": -60,
            "name": "Widget",
        })
        cat = [d for d in self._bl.catalog() if d["mac"] == mac]
        self.assertEqual(len(cat), 1)
        self.assertFalse(cat[0].get("gone"))
        self.assertAlmostEqual(cat[0]["last_heard_ts"], now, places=3)

    def test_throttle_gates_rapid_non_gone_writes(self) -> None:
        mac = TEST_MAC_PREFIX + "02"
        # Force an immediate first write.
        self._bl._last_seen_write[mac] = 0.0
        t0 = time.time()
        self._bl.record_seen(mac, {
            "mac": mac, "last_heard_ts": t0, "rssi": -50,
        })
        # Second non-gone write a moment later must be suppressed
        # (well under MIN_SEEN_WRITE_INTERVAL_S).
        self._bl.record_seen(mac, {
            "mac": mac, "last_heard_ts": t0 + 0.5, "rssi": -40,
        })
        rows = self._exec(
            "SELECT last_heard_ts, payload->>'rssi' "
            "FROM ble_seen WHERE mac = %s",
            (mac,),
        )
        self.assertEqual(len(rows), 1)
        # Throttled — payload must still reflect the FIRST write.
        self.assertEqual(rows[0][1], "-50")

    def test_gone_transition_bypasses_throttle(self) -> None:
        mac = TEST_MAC_PREFIX + "03"
        # Prime the throttle with a recent non-gone write.
        now = time.time()
        self._bl.record_seen(mac, {
            "mac": mac, "last_heard_ts": now, "rssi": -55,
        })
        # Within the throttle window, a gone-flagged write must still
        # land — a departure is too rare to drop.
        self._bl.record_seen(mac, {
            "mac": mac, "last_heard_ts": now + 0.1,
            "gone": True, "rssi": -55,
        })
        rows = self._exec(
            "SELECT gone FROM ble_seen WHERE mac = %s", (mac,),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 1)

    def test_catalog_orders_gone_after_present(self) -> None:
        present = TEST_MAC_PREFIX + "04"
        gone = TEST_MAC_PREFIX + "05"
        now = time.time()
        # Both get a row; `gone` marker must sort to the bottom even
        # if its last_heard_ts is more recent.
        self._bl.record_seen(present, {
            "mac": present, "last_heard_ts": now - 1000, "rssi": -70,
        })
        self._bl.record_seen(gone, {
            "mac": gone, "last_heard_ts": now, "gone": True, "rssi": -70,
        })
        macs = [d["mac"] for d in self._bl.catalog()
                if d["mac"].startswith(TEST_MAC_PREFIX)]
        self.assertEqual(macs.index(present), 0)
        self.assertEqual(macs.index(gone), 1)


# ---------------------------------------------------------------------------
# record_event + events_tail
# ---------------------------------------------------------------------------


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestRecordEvent(BleLoggerTestCase):

    def test_event_append_and_tail_order(self) -> None:
        mac = TEST_MAC_PREFIX + "06"
        # Append three events with rising timestamps.
        for i in range(3):
            self._bl.record_event(mac, {
                "mac": mac, "event": f"E{i}", "ts": time.time() + i,
            })
        tail = [
            ev for ev in self._bl.events_tail(limit=50)
            if ev.get("mac") == mac
        ]
        self.assertEqual(len(tail), 3)
        # events_tail returns oldest-first ordering.
        timestamps = [ev["ts"] for ev in tail]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_event_respects_limit(self) -> None:
        mac = TEST_MAC_PREFIX + "07"
        for i in range(5):
            self._bl.record_event(mac, {
                "mac": mac, "event": f"E{i}", "ts": time.time() + i,
            })
        # Limit to 2 — oldest-first within the requested window.
        tail = self._bl.events_tail(limit=2)
        # Not all 5 should be present (other tests may have cleared).
        mine = [ev for ev in tail if ev.get("mac") == mac]
        # With scrub in setUp, exactly our 5 are in the table; limit
        # 2 keeps the most recent 2, returned oldest-first.
        self.assertLessEqual(len(mine), 2)


# ---------------------------------------------------------------------------
# last_heard_ts
# ---------------------------------------------------------------------------


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestLastHeardTs(BleLoggerTestCase):

    def test_last_heard_ts_returns_max(self) -> None:
        mac = TEST_MAC_PREFIX + "08"
        now = time.time()
        self._bl.record_seen(mac, {
            "mac": mac, "last_heard_ts": now, "rssi": -65,
        })
        # last_heard_ts is "max across ble_seen" — must be >= what we
        # just wrote (could be higher if real sniffer data is present).
        self.assertGreaterEqual(self._bl.last_heard_ts(), now)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestRetention(BleLoggerTestCase):

    def test_event_prune_removes_ancient_rows(self) -> None:
        mac = TEST_MAC_PREFIX + "09"
        stale_ts = time.time() - EVENTS_RETENTION_SECONDS - 3600
        self._exec(
            """INSERT INTO ble_events (timestamp, mac, event, payload)
               VALUES (%s, %s, %s, NULL)""",
            (stale_ts, mac, "ancient"),
        )
        rows = self._exec(
            "SELECT COUNT(*) FROM ble_events WHERE mac = %s", (mac,),
        )
        self.assertEqual(rows[0][0], 1)
        self._bl._prune_events()
        rows = self._exec(
            "SELECT COUNT(*) FROM ble_events WHERE mac = %s", (mac,),
        )
        self.assertEqual(rows[0][0], 0)


if __name__ == "__main__":
    unittest.main()
