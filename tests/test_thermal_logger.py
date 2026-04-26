#!/usr/bin/env python3
"""Unit tests for infrastructure.thermal_logger.ThermalLogger.

Covers the persistence surface without touching MQTT:

- record() happy path, missing node_id, throttle protection
- latest() returns one row per node (most recent)
- query() with time window and resolution bucketing
- hosts() distinct node list
- Retention pruning removes old rows
- fan_declared_present bool round-trip
- Extras (throttled_flags, model) extracted from the payload

Tests require a live PostgreSQL connection.  The DSN comes from
glowup_site (``postgres_dsn`` key in /etc/glowup/secrets.json) or the
``GLOWUP_DIAG_DSN`` env var.  Skipped automatically when neither is
set or the cluster is unreachable.

Run::

    python3 -m unittest tests.test_thermal_logger -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "2.0"

import os
import time
import unittest
from typing import Any

from infrastructure.thermal_logger import (
    MIN_WRITE_INTERVAL_S,
    RETENTION_SECONDS,
    DEFAULT_DSN,
    ThermalLogger,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _payload(
    node_id: str,
    cpu_temp_c: float = 55.0,
    fan_rpm: int = 2500,
    fan_pwm_step: int = 1,
    fan_declared_present: bool = False,
    load_1m: float = 0.1,
    load_5m: float = 0.2,
    load_15m: float = 0.3,
    uptime_s: float = 1000.0,
    platform: str = "pi5",
    throttled_flags: str = "0x0",
    model: str = "Raspberry Pi 5 Model B",
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "platform": platform,
        "cpu_temp_c": cpu_temp_c,
        "fan_rpm": fan_rpm,
        "fan_pwm_step": fan_pwm_step,
        "fan_declared_present": fan_declared_present,
        "load_1m": load_1m,
        "load_5m": load_5m,
        "load_15m": load_15m,
        "uptime_s": uptime_s,
        "extra": {
            "throttled_flags": throttled_flags,
            "model": model,
        },
    }


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class ThermalLoggerTestCase(unittest.TestCase):
    """Base: fresh ThermalLogger per test, table cleaned in setUp."""

    def setUp(self) -> None:
        self._tl: ThermalLogger = ThermalLogger(dsn=_TEST_DSN)
        self.assertIsNotNone(self._tl._conn, "PG connection must be open")
        self._exec("DELETE FROM thermal_readings WHERE node_id LIKE %s", ("test-%",))

    def tearDown(self) -> None:
        self._exec("DELETE FROM thermal_readings WHERE node_id LIKE %s", ("test-%",))
        self._tl.close()

    def _exec(self, sql: str, params: tuple = ()) -> list:
        """Run SQL directly against the logger's connection."""
        with self._tl._conn.cursor() as cur:
            cur.execute(sql, params)
            try:
                return cur.fetchall()
            except Exception:
                return []

    def _count(self, node_id: str) -> int:
        rows = self._exec(
            "SELECT COUNT(*) FROM thermal_readings WHERE node_id = %s",
            (node_id,),
        )
        return rows[0][0] if rows else 0

    def _direct_insert(self, node_id: str, ts: float, cpu_temp_c: float) -> None:
        """Bypass record() to set exact timestamps for multi-row tests."""
        self._exec(
            """INSERT INTO thermal_readings
               (node_id, timestamp, cpu_temp_c, fan_rpm, fan_pwm_step,
                fan_declared_present, load_1m, load_5m, load_15m,
                uptime_s, throttled_flags, platform, model)
               VALUES (%s, %s, %s, NULL, NULL, NULL,
                       0.5, 0.4, 0.3, 100.0, '0x0', 'pi5', NULL)""",
            (node_id, ts, cpu_temp_c),
        )


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestRecord(ThermalLoggerTestCase):
    """Write path — happy, rejection, throttling, bool handling."""

    def test_record_happy_path_roundtrip(self) -> None:
        p = _payload("test-hub", cpu_temp_c=56.7, fan_rpm=2739)
        self._tl.record(p)
        latest = self._tl.latest()
        self.assertIn("test-hub", latest)
        row = latest["test-hub"]
        self.assertAlmostEqual(row["cpu_temp_c"], 56.7, places=3)
        self.assertEqual(row["fan_rpm"], 2739)
        self.assertEqual(row["platform"], "pi5")
        self.assertEqual(row["throttled_flags"], "0x0")
        self.assertEqual(row["model"], "Raspberry Pi 5 Model B")

    def test_record_rejects_missing_node_id(self) -> None:
        bad = _payload("ignored")
        del bad["node_id"]
        self._tl.record(bad)
        self.assertNotIn("test-ignored", self._tl.hosts())

    def test_record_rejects_non_string_node_id(self) -> None:
        bad = _payload("ignored")
        bad["node_id"] = 42
        self._tl.record(bad)

    def test_throttle_coalesces_rapid_writes(self) -> None:
        self._tl.record(_payload("test-hub", cpu_temp_c=50.0))
        self._tl.record(_payload("test-hub", cpu_temp_c=60.0))
        self.assertEqual(self._count("test-hub"), 1)

    def test_throttle_is_per_node(self) -> None:
        self._tl.record(_payload("test-hub"))
        self._tl.record(_payload("test-broker"))
        hosts = self._tl.hosts()
        self.assertIn("test-hub", hosts)
        self.assertIn("test-broker", hosts)

    def test_fan_declared_present_bool_roundtrip(self) -> None:
        self._tl.record(_payload("test-a", fan_declared_present=True))
        latest_a = self._tl.latest()
        self.assertTrue(latest_a["test-a"]["fan_declared_present"])

        self._tl.record(_payload("test-b", fan_declared_present=False))
        latest_b = self._tl.latest()
        self.assertFalse(latest_b["test-b"]["fan_declared_present"])

    def test_null_fan_fields_stored_as_none(self) -> None:
        p = _payload("test-mbclock", cpu_temp_c=52.6, platform="pi4",
                     fan_declared_present=True)
        p["fan_rpm"] = None
        p["fan_pwm_step"] = None
        self._tl.record(p)
        row = self._tl.latest()["test-mbclock"]
        self.assertIsNone(row["fan_rpm"])
        self.assertIsNone(row["fan_pwm_step"])
        self.assertTrue(row["fan_declared_present"])


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestLatest(ThermalLoggerTestCase):
    """latest() must return the most-recent row per node."""

    def test_latest_returns_most_recent_per_node(self) -> None:
        old_time = time.time() - 3600
        new_time = time.time()
        self._direct_insert("test-hub", old_time, 40.0)
        self._direct_insert("test-hub", new_time, 70.0)
        self._direct_insert("test-broker", new_time, 55.0)
        latest = self._tl.latest()
        self.assertAlmostEqual(latest["test-hub"]["cpu_temp_c"], 70.0, places=3)
        self.assertAlmostEqual(latest["test-broker"]["cpu_temp_c"], 55.0, places=3)

    def test_latest_empty_when_no_test_nodes(self) -> None:
        # All test-* rows are cleaned in setUp; latest should have no test nodes.
        latest = self._tl.latest()
        for key in latest:
            self.assertFalse(key.startswith("test-"),
                             f"unexpected test node {key} in latest()")


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestQuery(ThermalLoggerTestCase):
    """query() — time windowing + bucket resolution."""

    def test_query_returns_empty_for_unknown_node(self) -> None:
        self.assertEqual(self._tl.query("test-nobody"), [])

    def test_query_filters_by_time_window(self) -> None:
        very_old = time.time() - (48 * 3600)
        recent = time.time() - 600
        self._direct_insert("test-hub", very_old, 40.0)
        self._direct_insert("test-hub", recent, 55.0)
        result = self._tl.query("test-hub", hours=1, resolution=60)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["cpu_temp_c"], 55.0, places=3)

    def test_query_buckets_average_values(self) -> None:
        now = time.time()
        base = int(now / 60) * 60 + 5
        self._direct_insert("test-hub", base,      40.0)
        self._direct_insert("test-hub", base + 10, 60.0)
        self._direct_insert("test-hub", base + 20, 50.0)
        result = self._tl.query("test-hub", hours=1, resolution=60)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["cpu_temp_c"], 50.0, places=3)


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestHosts(ThermalLoggerTestCase):
    """hosts() returns sorted distinct node ids."""

    def test_hosts_returns_distinct_sorted(self) -> None:
        self._tl.record(_payload("test-hub"))
        self._tl.record(_payload("test-broker"))
        self._tl.record(_payload("test-mbclock", platform="pi4"))
        hosts = self._tl.hosts()
        test_hosts = [h for h in hosts if h.startswith("test-")]
        self.assertEqual(test_hosts, sorted(test_hosts))
        self.assertEqual(len(test_hosts), 3)


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestRetention(ThermalLoggerTestCase):
    """_prune() removes rows older than RETENTION_SECONDS."""

    def test_prune_removes_old_rows(self) -> None:
        past = time.time() - (RETENTION_SECONDS + 3600)
        now = time.time()
        for ts in (past, now):
            self._exec(
                """INSERT INTO thermal_readings
                   (node_id, timestamp, cpu_temp_c, fan_rpm, fan_pwm_step,
                    fan_declared_present, load_1m, load_5m, load_15m,
                    uptime_s, throttled_flags, platform, model)
                   VALUES (%s, %s, 50.0, NULL, NULL, NULL,
                           NULL, NULL, NULL, NULL, '0x0', 'pi5', NULL)""",
                ("test-hub", ts),
            )
        self._tl._prune()
        self.assertEqual(self._count("test-hub"), 1)


if __name__ == "__main__":
    unittest.main()
