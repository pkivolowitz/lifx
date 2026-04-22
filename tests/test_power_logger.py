"""Tests for infrastructure.power_logger.PowerLogger.

Covers construction, recording, throttling, carry-forward, pruning,
queries, summaries, device listing, thread safety, and the mark_offline
retained-MQTT-replay defense.

Tests require a live PostgreSQL connection.  Set GLOWUP_DIAG_DSN or the
DEFAULT_DSN default must be reachable.  Skipped automatically otherwise.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.0"

import os
import threading
import time
import unittest

from infrastructure.power_logger import (
    MIN_WRITE_INTERVAL,
    POWER_PROPERTIES,
    PRUNE_EVERY,
    RETENTION_SECONDS,
    DEFAULT_DSN,
    PowerLogger,
)

# ---------------------------------------------------------------------------
# PG availability gate
# ---------------------------------------------------------------------------

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

# Use a device-name prefix that tests clean up on setUp/tearDown.
_PREFIX = "test_pl_"


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class PowerLoggerTestCase(unittest.TestCase):
    """Base class — fresh logger, test rows cleaned per test."""

    def setUp(self) -> None:
        self.pl = PowerLogger(dsn=_TEST_DSN)
        self.assertIsNotNone(self.pl._conn)
        self.pl._last_write.clear()
        self._clean()

    def tearDown(self) -> None:
        self._clean()
        self.pl.close()

    def _clean(self) -> None:
        self._exec(
            "DELETE FROM power_readings WHERE device LIKE %s",
            (f"{_PREFIX}%",),
        )

    def _exec(self, sql: str, params: tuple = ()) -> list:
        if self.pl._conn is None:
            return []
        with self.pl._conn.cursor() as cur:
            cur.execute(sql, params)
            try:
                return cur.fetchall()
            except Exception:
                return []

    def _count(self, device: str) -> int:
        rows = self._exec(
            "SELECT COUNT(*) FROM power_readings WHERE device = %s",
            (device,),
        )
        return rows[0][0] if rows else 0

    def _latest_row(self, device: str) -> tuple | None:
        rows = self._exec(
            "SELECT power, voltage, current_a, energy, power_factor "
            "FROM power_readings WHERE device = %s "
            "ORDER BY timestamp DESC LIMIT 1",
            (device,),
        )
        return rows[0] if rows else None

    def _all_rows(self, device: str) -> list[tuple]:
        return self._exec(
            "SELECT power, voltage, current_a, energy, power_factor "
            "FROM power_readings WHERE device = %s ORDER BY id",
            (device,),
        )

    def dev(self, name: str = "plug") -> str:
        return _PREFIX + name


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestConstruction(PowerLoggerTestCase):

    def test_connection_open(self) -> None:
        self.assertIsNotNone(self.pl._conn)

    def test_bad_dsn_does_not_crash(self) -> None:
        pl = PowerLogger(dsn="postgresql://bad:bad@127.0.0.1:1/bad")
        self.assertIsNone(pl._conn)
        pl.record("dev", "power", 100.0)
        self.assertEqual(pl.query(), [])
        self.assertEqual(pl.summary(), {})
        self.assertEqual(pl.devices(), [])
        pl.close()


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestRecord(PowerLoggerTestCase):

    def test_record_power(self) -> None:
        self.pl.record(self.dev(), "power", 167.3)
        rows = self.pl.query(device=self.dev(), hours=1, resolution=1)
        self.assertGreater(len(rows), 0)
        self.assertAlmostEqual(rows[0]["power"], 167.3, places=1)

    def test_ignores_non_power_properties(self) -> None:
        for prop in ("occupancy", "temperature", "contact"):
            self.pl.record(self.dev(), prop, 1.0)
        self.assertNotIn(self.dev(), self.pl.devices())

    def test_throttle_prevents_rapid_writes(self) -> None:
        self.pl.record(self.dev(), "power", 100.0)
        count_before = self.pl._write_count
        self.pl.record(self.dev(), "power", 110.0)
        self.assertEqual(self.pl._write_count, count_before)

    def test_throttle_allows_after_interval(self) -> None:
        self.pl.record(self.dev(), "power", 100.0)
        count_after_first = self.pl._write_count
        self.pl._last_write[self.dev()] = time.time() - MIN_WRITE_INTERVAL - 1
        self.pl.record(self.dev(), "power", 110.0)
        self.assertEqual(self.pl._write_count, count_after_first + 1)

    def test_multiple_devices_independent_throttle(self) -> None:
        self.pl.record(self.dev("a"), "power", 100.0)
        self.pl.record(self.dev("b"), "power", 200.0)
        self.assertIn(self.dev("a"), self.pl.devices())
        self.assertIn(self.dev("b"), self.pl.devices())

    def test_record_with_none_conn_is_noop(self) -> None:
        self.pl._conn = None
        self.pl.record(self.dev(), "power", 100.0)

    def test_all_power_properties_accepted(self) -> None:
        for prop in POWER_PROPERTIES:
            self.pl._last_write.clear()
            self.pl.record(self.dev(), prop, 42.0)
        self.assertGreater(self.pl._write_count, 0)


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestMarkOffline(PowerLoggerTestCase):

    def test_mark_offline_writes_null_sentinel_row(self) -> None:
        self.pl.record(self.dev(), "power", 168.5)
        self.pl.record(self.dev(), "voltage", 121.6)
        self.pl._last_write[self.dev()] = 0.0
        self.pl.record(self.dev(), "power", 168.5)
        self.pl.mark_offline(self.dev())
        row = self._latest_row(self.dev())
        self.assertIsNotNone(row)
        self.assertTrue(all(v is None for v in row),
                        f"expected all-NULL row, got {row}")

    def test_mark_offline_clears_pending(self) -> None:
        self.pl.record(self.dev(), "power", 168.5)
        self.assertIn(self.dev(), self.pl._pending)
        self.pl.mark_offline(self.dev())
        self.assertNotIn(self.dev(), self.pl._pending)
        self.assertNotIn(self.dev(), self.pl._dirty)

    def test_mark_offline_does_not_affect_other_devices(self) -> None:
        self.pl.record(self.dev("a"), "power", 168.5)
        self.pl.record(self.dev("b"), "power", 100.0)
        self.pl.mark_offline(self.dev("a"))
        self.assertIn(self.dev("b"), self.pl._pending)

    def test_mark_offline_on_unknown_device_writes_sentinel(self) -> None:
        self.pl.mark_offline(self.dev("never-seen"))
        rows = self.pl.query(device=self.dev("never-seen"), hours=1, resolution=1)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["power"])

    def test_mark_offline_with_none_conn_is_noop(self) -> None:
        self.pl._conn = None
        self.pl.mark_offline(self.dev())

    def test_mark_offline_is_idempotent(self) -> None:
        self.pl.record(self.dev(), "power", 168.5)
        self.pl._last_write[self.dev()] = 0.0
        self.pl.record(self.dev(), "power", 168.5)
        self.pl.mark_offline(self.dev())
        first_count = self._count(self.dev())
        for _ in range(5):
            self.pl.mark_offline(self.dev())
        self.assertEqual(self._count(self.dev()), first_count)

    def test_mark_offline_writes_new_sentinel_after_real_reading(self) -> None:
        self.pl.record(self.dev(), "power", 168.5)
        self.pl._last_write[self.dev()] = 0.0
        self.pl.record(self.dev(), "power", 168.5)
        self.pl.mark_offline(self.dev())
        before_count = self._count(self.dev())
        self.pl._last_write[self.dev()] = 0.0
        self.pl.record(self.dev(), "power", 75.0)
        self.pl.mark_offline(self.dev())
        self.assertEqual(self._count(self.dev()), before_count + 2)


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestPrune(PowerLoggerTestCase):

    def test_prune_removes_old_records(self) -> None:
        old_ts = time.time() - RETENTION_SECONDS - 3600
        self._exec(
            "INSERT INTO power_readings (device, timestamp, power) "
            "VALUES (%s, %s, %s)",
            (self.dev("old"), old_ts, 50.0),
        )
        self.assertIn(self.dev("old"), self.pl.devices())
        self.pl._prune()
        self.assertNotIn(self.dev("old"), self.pl.devices())

    def test_prune_keeps_recent_records(self) -> None:
        self.pl.record(self.dev("recent"), "power", 100.0)
        self.pl._prune()
        self.assertIn(self.dev("recent"), self.pl.devices())

    def test_auto_prune_triggers_at_interval(self) -> None:
        from unittest.mock import patch
        self.pl._write_count = PRUNE_EVERY - 1
        with patch.object(self.pl, "_prune") as mock_prune:
            self.pl.record(self.dev(), "power", 100.0)
            mock_prune.assert_called_once()


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestQuery(PowerLoggerTestCase):

    def setUp(self) -> None:
        super().setUp()
        now = time.time()
        for i in range(120):
            ts = now - (120 - i) * 60
            self._exec(
                "INSERT INTO power_readings "
                "(device, timestamp, power, voltage, current_a) "
                "VALUES (%s, %s, %s, %s, %s)",
                (self.dev(), ts, 100.0 + i, 122.0, 0.82 + i * 0.01),
            )

    def test_query_returns_data(self) -> None:
        rows = self.pl.query(device=self.dev(), hours=3, resolution=300)
        self.assertGreater(len(rows), 0)

    def test_query_respects_time_window(self) -> None:
        short = self.pl.query(device=self.dev(), hours=0.5, resolution=60)
        long = self.pl.query(device=self.dev(), hours=3, resolution=60)
        self.assertLessEqual(len(short), len(long))

    def test_query_respects_resolution(self) -> None:
        fine = self.pl.query(device=self.dev(), hours=2, resolution=60)
        coarse = self.pl.query(device=self.dev(), hours=2, resolution=600)
        self.assertGreater(len(fine), len(coarse))

    def test_query_all_devices(self) -> None:
        self._exec(
            "INSERT INTO power_readings (device, timestamp, power) "
            "VALUES (%s, %s, %s)",
            (self.dev("other"), time.time(), 50.0),
        )
        rows = self.pl.query(hours=3, resolution=300)
        devices = {r["device"] for r in rows}
        self.assertIn(self.dev(), devices)
        self.assertIn(self.dev("other"), devices)

    def test_query_nonexistent_device(self) -> None:
        rows = self.pl.query(device=self.dev("nonexistent"), hours=3, resolution=300)
        self.assertEqual(rows, [])

    def test_query_with_none_conn(self) -> None:
        self.pl._conn = None
        self.assertEqual(self.pl.query(), [])

    def test_query_result_has_expected_keys(self) -> None:
        rows = self.pl.query(device=self.dev(), hours=3, resolution=300)
        expected = {"bucket", "device", "power", "voltage",
                    "current_a", "energy", "power_factor"}
        self.assertTrue(expected.issubset(set(rows[0].keys())))

    def test_query_averages_within_bucket(self) -> None:
        coarse = self.pl.query(device=self.dev(), hours=2, resolution=3600)
        self.assertGreater(len(coarse), 0)
        self.assertIsInstance(coarse[0]["power"], float)


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestSummary(PowerLoggerTestCase):

    def setUp(self) -> None:
        super().setUp()
        now = time.time()
        for i in range(144):
            ts = now - (144 - i) * 600
            self._exec(
                "INSERT INTO power_readings (device, timestamp, power, energy) "
                "VALUES (%s, %s, %s, %s)",
                (self.dev("a"), ts, 150.0 + (i % 50), float(i) * 0.01),
            )

    def test_summary_returns_expected_keys(self) -> None:
        s = self.pl.summary(device=self.dev("a"), days=7)
        for key in ("avg_watts", "peak_watts", "total_kwh",
                    "days_covered", "device_count"):
            self.assertIn(key, s)

    def test_summary_avg_watts_reasonable(self) -> None:
        s = self.pl.summary(device=self.dev("a"), days=7)
        self.assertGreater(s["avg_watts"], 100)
        self.assertLess(s["avg_watts"], 250)

    def test_summary_peak_watts(self) -> None:
        s = self.pl.summary(device=self.dev("a"), days=7)
        self.assertAlmostEqual(s["peak_watts"], 199.0, places=0)

    def test_summary_with_none_conn(self) -> None:
        self.pl._conn = None
        self.assertEqual(self.pl.summary(), {})


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestDevices(PowerLoggerTestCase):

    def test_empty_returns_empty_list(self) -> None:
        devs = [d for d in self.pl.devices() if d.startswith(_PREFIX)]
        self.assertEqual(devs, [])

    def test_returns_distinct_sorted(self) -> None:
        for dev in [self.dev("z"), self.dev("a"), self.dev("m")]:
            self._exec(
                "INSERT INTO power_readings (device, timestamp, power) "
                "VALUES (%s, %s, %s)",
                (dev, time.time(), 100.0),
            )
        devs = [d for d in self.pl.devices() if d.startswith(_PREFIX)]
        self.assertEqual(devs, sorted(devs))
        self.assertEqual(len(devs), 3)

    def test_devices_with_none_conn(self) -> None:
        self.pl._conn = None
        self.assertEqual(self.pl.devices(), [])


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestClose(PowerLoggerTestCase):

    def test_close_sets_conn_none(self) -> None:
        self.assertIsNotNone(self.pl._conn)
        self.pl.close()
        self.assertIsNone(self.pl._conn)

    def test_double_close_safe(self) -> None:
        self.pl.close()
        self.pl.close()


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestFlushPending(PowerLoggerTestCase):

    def test_pending_data_flushed_without_record_call(self) -> None:
        self.pl.record(self.dev(), "power", 0.0)
        count_after_first = self.pl._write_count
        self.pl.record(self.dev(), "power", 13.1)
        self.assertEqual(self.pl._write_count, count_after_first)
        self.assertIn(self.dev(), self.pl._pending)
        self.pl._last_write[self.dev()] = time.time() - MIN_WRITE_INTERVAL - 1
        self.pl.flush_pending()
        self.assertGreater(self.pl._write_count, count_after_first)

    def test_flush_respects_throttle(self) -> None:
        self.pl.record(self.dev(), "power", 100.0)
        self.pl.record(self.dev(), "power", 200.0)
        self.pl.flush_pending()
        self.assertIn(self.dev(), self.pl._pending)

    def test_flush_clears_dirty_but_keeps_pending(self) -> None:
        self.pl.record(self.dev(), "power", 50.0)
        self.pl.record(self.dev(), "power", 75.0)
        self.pl._last_write[self.dev()] = time.time() - MIN_WRITE_INTERVAL - 1
        self.pl.flush_pending()
        self.assertIn(self.dev(), self.pl._pending)
        self.assertEqual(self.pl._pending[self.dev()].get("power"), 75.0)
        self.assertFalse(self.pl._dirty.get(self.dev(), False))

    def test_flush_noop_when_no_pending(self) -> None:
        self.pl.flush_pending()


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestCarryForward(PowerLoggerTestCase):
    """Regression: sparse Z2M messages must not produce NULL-column rows."""

    def test_pending_not_popped_after_write(self) -> None:
        self.pl.record(self.dev(), "power", 100.0)
        self.assertIn(self.dev(), self.pl._pending)
        self.assertEqual(self.pl._pending[self.dev()].get("power"), 100.0)
        self.assertFalse(self.pl._dirty.get(self.dev(), False))

    def test_lrtv_sparse_messages_produce_complete_rows(self) -> None:
        """The production LRTV reproduction — sparse current-only message must
        carry forward all other properties."""
        self.pl.record(self.dev("lrtv"), "power", 245.0)
        self.pl.record(self.dev("lrtv"), "voltage", 124.8)
        self.pl.record(self.dev("lrtv"), "current", 2.0)
        self.pl.record(self.dev("lrtv"), "energy", 5.11)
        self.pl.record(self.dev("lrtv"), "power_factor", 1.0)
        self.pl._last_write[self.dev("lrtv")] = time.time() - MIN_WRITE_INTERVAL - 1
        self.pl.record(self.dev("lrtv"), "current", 2.13)
        rows = self._all_rows(self.dev("lrtv"))
        self.assertGreaterEqual(len(rows), 2)
        last = rows[-1]
        col_names = ("power", "voltage", "current_a", "energy", "power_factor")
        for col, val in zip(col_names, last):
            self.assertIsNotNone(val, f"carry-forward failed: {col} is NULL")
        self.assertAlmostEqual(last[0], 245.0, places=1)
        self.assertAlmostEqual(last[1], 124.8, places=1)
        self.assertAlmostEqual(last[2], 2.13,  places=2)
        self.assertAlmostEqual(last[3], 5.11,  places=2)
        self.assertAlmostEqual(last[4], 1.0,   places=2)

    def test_flush_pending_skips_when_not_dirty(self) -> None:
        self.pl.record(self.dev(), "power", 100.0)
        count_before = self.pl._write_count
        self.pl._last_write[self.dev()] = time.time() - MIN_WRITE_INTERVAL - 1
        self.pl.flush_pending()
        self.assertEqual(self.pl._write_count, count_before)


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestThreadSafety(PowerLoggerTestCase):

    def test_concurrent_records(self) -> None:
        errors: list = []

        def worker(name: str) -> None:
            try:
                for i in range(20):
                    self.pl._last_write[name] = 0.0
                    self.pl.record(name, "power", 100.0 + i)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(self.dev(f"t{i}"),))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"errors: {errors}")
        test_devs = [d for d in self.pl.devices() if d.startswith(_PREFIX)]
        self.assertEqual(len(test_devs), 5)


if __name__ == "__main__":
    unittest.main()
