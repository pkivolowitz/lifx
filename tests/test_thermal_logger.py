#!/usr/bin/env python3
"""Unit tests for infrastructure.thermal_logger.ThermalLogger.

Covers the persistence surface without touching MQTT:

- Schema creation and column set
- ``record()`` happy path, missing node_id, throttle protection
- ``latest()`` returns one row per node (most recent)
- ``query()`` with time window and resolution bucketing
- ``hosts()`` distinct node list
- Retention pruning removes old rows
- ``fan_declared_present`` bool round-trip (0/1 INTEGER in SQLite)
- Extras (``throttled_flags``, ``model``) extracted from the payload

No MQTT broker, no paho import required — tests call ``record()``
directly with dict payloads.  The subscriber path is covered by the
live integration flow (pi_thermal_sensor deploy → hub broker →
ThermalLogger).

Run::

    python3 -m unittest tests.test_thermal_logger -v
    python3 -m pytest tests/test_thermal_logger.py -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import os
import sqlite3
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

from infrastructure.thermal_logger import (
    MIN_WRITE_INTERVAL_S,
    RETENTION_SECONDS,
    ThermalLogger,
)


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
    """Build a thermal-sensor-shaped payload dict."""
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


class ThermalLoggerTestCase(unittest.TestCase):
    """Base: temp-file DB, fresh ThermalLogger per test."""

    def setUp(self) -> None:
        """Create a temp SQLite file and instantiate a logger."""
        self._tmp: tempfile.TemporaryDirectory = tempfile.TemporaryDirectory()
        self._db_path: str = os.path.join(self._tmp.name, "thermal.db")
        self._tl: ThermalLogger = ThermalLogger(db_path=self._db_path)

    def tearDown(self) -> None:
        """Close logger + remove temp dir."""
        self._tl.close()
        self._tmp.cleanup()


class TestSchema(ThermalLoggerTestCase):
    """Schema shape, index, and columns."""

    def test_table_and_index_exist(self) -> None:
        """The expected table and index are created on open."""
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            tables: set[str] = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("thermal_readings", tables)
            indexes: set[str] = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            self.assertIn("idx_thermal_node_ts", indexes)
        finally:
            conn.close()

    def test_columns_match_payload_schema(self) -> None:
        """Every field in the sensor payload has a column."""
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            cols: set[str] = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(thermal_readings)"
                ).fetchall()
            }
            expected: set[str] = {
                "id", "node_id", "timestamp",
                "cpu_temp_c", "fan_rpm", "fan_pwm_step",
                "fan_declared_present",
                "load_1m", "load_5m", "load_15m",
                "uptime_s", "throttled_flags", "platform", "model",
            }
            missing: set[str] = expected - cols
            self.assertFalse(missing, f"missing columns: {missing}")
        finally:
            conn.close()


class TestRecord(ThermalLoggerTestCase):
    """Write path — happy, rejection, throttling, bool handling."""

    def test_record_happy_path_roundtrip(self) -> None:
        """A complete payload inserts and round-trips through latest()."""
        p: dict[str, Any] = _payload("hub", cpu_temp_c=56.7, fan_rpm=2739)
        self._tl.record(p)
        latest: dict[str, dict[str, Any]] = self._tl.latest()
        self.assertIn("hub", latest)
        row: dict[str, Any] = latest["hub"]
        self.assertEqual(row["cpu_temp_c"], 56.7)
        self.assertEqual(row["fan_rpm"], 2739)
        self.assertEqual(row["platform"], "pi5")
        self.assertEqual(row["throttled_flags"], "0x0")
        self.assertEqual(row["model"], "Raspberry Pi 5 Model B")

    def test_record_rejects_missing_node_id(self) -> None:
        """Payload without node_id is dropped silently (logged)."""
        bad: dict[str, Any] = _payload("ignored")
        del bad["node_id"]
        self._tl.record(bad)
        self.assertEqual(self._tl.hosts(), [])

    def test_record_rejects_non_string_node_id(self) -> None:
        """node_id must be a string."""
        bad: dict[str, Any] = _payload("ignored")
        bad["node_id"] = 42
        self._tl.record(bad)
        self.assertEqual(self._tl.hosts(), [])

    def test_throttle_coalesces_rapid_writes(self) -> None:
        """Two record() calls within MIN_WRITE_INTERVAL_S → one row."""
        self._tl.record(_payload("hub", cpu_temp_c=50.0))
        self._tl.record(_payload("hub", cpu_temp_c=60.0))
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM thermal_readings WHERE node_id = ?",
                ("hub",),
            ).fetchone()
            self.assertEqual(count, 1)
        finally:
            conn.close()

    def test_throttle_is_per_node(self) -> None:
        """Throttle is scoped per-node — different nodes are independent."""
        self._tl.record(_payload("hub"))
        self._tl.record(_payload("broker-2"))
        self.assertEqual(sorted(self._tl.hosts()), ["broker-2", "hub"])

    def test_fan_declared_present_bool_roundtrip(self) -> None:
        """fan_declared_present True/False round-trips through SQLite."""
        self._tl.record(_payload("a", fan_declared_present=True))
        latest_a: dict[str, dict[str, Any]] = self._tl.latest()
        self.assertTrue(latest_a["a"]["fan_declared_present"])

        # Advance time to escape throttle before writing a second node.
        time.sleep(0)  # no sleep needed — different node_id
        self._tl.record(_payload("b", fan_declared_present=False))
        latest_b: dict[str, dict[str, Any]] = self._tl.latest()
        self.assertFalse(latest_b["b"]["fan_declared_present"])

    def test_null_fields_are_stored_as_none(self) -> None:
        """Pi 4 style payload (null fan fields) round-trips as None."""
        p: dict[str, Any] = _payload(
            "mbclock",
            cpu_temp_c=52.6,
            platform="pi4",
            fan_declared_present=True,
        )
        p["fan_rpm"] = None
        p["fan_pwm_step"] = None
        self._tl.record(p)
        row: dict[str, Any] = self._tl.latest()["mbclock"]
        self.assertIsNone(row["fan_rpm"])
        self.assertIsNone(row["fan_pwm_step"])
        self.assertTrue(row["fan_declared_present"])


class TestLatest(ThermalLoggerTestCase):
    """latest() must return the most-recent row per node."""

    def test_latest_returns_most_recent_per_node(self) -> None:
        """When a node has multiple rows, latest() takes the newest."""
        # First insert at simulated old time.
        old_time: float = time.time() - 3600
        new_time: float = time.time()
        self._direct_insert("hub", old_time, cpu_temp_c=40.0)
        self._direct_insert("hub", new_time, cpu_temp_c=70.0)
        self._direct_insert("broker-2", new_time, cpu_temp_c=55.0)
        latest: dict[str, dict[str, Any]] = self._tl.latest()
        self.assertEqual(latest["hub"]["cpu_temp_c"], 70.0)
        self.assertEqual(latest["broker-2"]["cpu_temp_c"], 55.0)

    def test_latest_empty_on_empty_db(self) -> None:
        """Fresh logger has no data → latest() returns empty dict."""
        self.assertEqual(self._tl.latest(), {})

    def _direct_insert(
        self,
        node_id: str,
        ts: float,
        cpu_temp_c: float,
    ) -> None:
        """Bypass record() to set exact timestamps for multi-row tests."""
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """INSERT INTO thermal_readings
                   (node_id, timestamp, cpu_temp_c, fan_rpm, fan_pwm_step,
                    fan_declared_present,
                    load_1m, load_5m, load_15m,
                    uptime_s, throttled_flags, platform, model)
                   VALUES (?, ?, ?, NULL, NULL, NULL,
                           NULL, NULL, NULL, NULL, ?, ?, NULL)""",
                (node_id, ts, cpu_temp_c, "0x0", "pi5"),
            )
            conn.commit()
        finally:
            conn.close()


class TestQuery(ThermalLoggerTestCase):
    """query() — time windowing + bucket resolution."""

    def test_query_returns_empty_for_unknown_node(self) -> None:
        """Query for a node with no data → empty list."""
        self.assertEqual(self._tl.query("nobody"), [])

    def test_query_filters_by_time_window(self) -> None:
        """Rows older than `hours` are excluded."""
        very_old: float = time.time() - (48 * 3600)
        recent: float = time.time() - 600
        self._direct_insert("hub", very_old, 40.0)
        self._direct_insert("hub", recent, 55.0)
        result: list[dict[str, Any]] = self._tl.query(
            "hub", hours=1, resolution=60,
        )
        # Only the recent row should survive the 1-hour window.
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["cpu_temp_c"], 55.0, places=3)

    def test_query_buckets_average_values(self) -> None:
        """Multiple rows in a single bucket average correctly."""
        now: float = time.time()
        # Three rows within the same 60s bucket.
        base: float = int(now / 60) * 60 + 5
        self._direct_insert("hub", base,     40.0)
        self._direct_insert("hub", base + 10, 60.0)
        self._direct_insert("hub", base + 20, 50.0)
        result: list[dict[str, Any]] = self._tl.query(
            "hub", hours=1, resolution=60,
        )
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["cpu_temp_c"], 50.0, places=3)

    def _direct_insert(
        self,
        node_id: str,
        ts: float,
        cpu_temp_c: float,
    ) -> None:
        """Bypass record() — see TestLatest for rationale."""
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """INSERT INTO thermal_readings
                   (node_id, timestamp, cpu_temp_c, fan_rpm, fan_pwm_step,
                    fan_declared_present,
                    load_1m, load_5m, load_15m,
                    uptime_s, throttled_flags, platform, model)
                   VALUES (?, ?, ?, NULL, NULL, NULL,
                           0.5, 0.4, 0.3, 100.0, "0x0", "pi5", NULL)""",
                (node_id, ts, cpu_temp_c),
            )
            conn.commit()
        finally:
            conn.close()


class TestHosts(ThermalLoggerTestCase):
    """hosts() returns sorted distinct node ids."""

    def test_hosts_empty_on_empty_db(self) -> None:
        """No data → empty list."""
        self.assertEqual(self._tl.hosts(), [])

    def test_hosts_returns_distinct_sorted(self) -> None:
        """Three nodes each with one row → three distinct hosts."""
        self._tl.record(_payload("hub"))
        self._tl.record(_payload("broker-2"))
        self._tl.record(_payload("mbclock", platform="pi4"))
        self.assertEqual(
            self._tl.hosts(), ["broker-2", "hub", "mbclock"],
        )


class TestRetention(ThermalLoggerTestCase):
    """_prune() removes rows older than RETENTION_SECONDS."""

    def test_prune_removes_old_rows(self) -> None:
        """A row past the retention window is gone after _prune()."""
        past: float = time.time() - (RETENTION_SECONDS + 3600)
        now: float = time.time()
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            for ts in (past, now):
                conn.execute(
                    """INSERT INTO thermal_readings
                       (node_id, timestamp, cpu_temp_c, fan_rpm, fan_pwm_step,
                        fan_declared_present, load_1m, load_5m, load_15m,
                        uptime_s, throttled_flags, platform, model)
                       VALUES (?, ?, 50.0, NULL, NULL, NULL,
                               NULL, NULL, NULL, NULL, '0x0', 'pi5', NULL)""",
                    ("hub", ts),
                )
            conn.commit()
        finally:
            conn.close()

        self._tl._prune()

        conn = sqlite3.connect(self._db_path)
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM thermal_readings WHERE node_id='hub'",
            ).fetchone()
            self.assertEqual(count, 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
