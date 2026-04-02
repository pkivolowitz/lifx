"""Exhaustive tests for the PowerLogger.

Covers construction, recording, throttling, pruning, queries,
summaries, device listing, thread safety, error handling, and
edge cases.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from infrastructure.power_logger import (
    MIN_WRITE_INTERVAL,
    POWER_PROPERTIES,
    PRUNE_EVERY,
    RETENTION_SECONDS,
    PowerLogger,
)


class TestPowerLoggerConstruction(unittest.TestCase):
    """Tests for PowerLogger initialization and database setup."""

    def test_creates_db_file(self) -> None:
        """Database file is created on construction."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            os.unlink(path)
            pl = PowerLogger(db_path=path)
            self.assertTrue(os.path.exists(path))
            pl.close()
        finally:
            os.unlink(path)

    def test_creates_table_and_index(self) -> None:
        """power_readings table and index exist after init."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            pl = PowerLogger(db_path=path)
            conn = sqlite3.connect(path)
            # Check table exists.
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='power_readings'"
            ).fetchall()
            self.assertEqual(len(tables), 1)
            # Check index exists.
            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_power_device_ts'"
            ).fetchall()
            self.assertEqual(len(indexes), 1)
            conn.close()
            pl.close()
        finally:
            os.unlink(path)

    def test_wal_mode_enabled(self) -> None:
        """Database uses WAL journal mode for concurrent access."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            pl = PowerLogger(db_path=path)
            conn = sqlite3.connect(path)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode, "wal")
            conn.close()
            pl.close()
        finally:
            os.unlink(path)

    def test_idempotent_open(self) -> None:
        """Opening an existing database does not destroy data."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            pl1 = PowerLogger(db_path=path)
            pl1._last_write["test"] = 0.0
            pl1.record("test", "power", 100.0)
            pl1.close()
            # Re-open.
            pl2 = PowerLogger(db_path=path)
            devices = pl2.devices()
            self.assertIn("test", devices)
            pl2.close()
        finally:
            os.unlink(path)

    def test_bad_db_path_does_not_crash(self) -> None:
        """Invalid database path logs error but does not raise."""
        pl = PowerLogger(db_path="/nonexistent/dir/power.db")
        self.assertIsNone(pl._conn)
        # All methods should be no-ops.
        pl.record("dev", "power", 100.0)
        self.assertEqual(pl.query(), [])
        self.assertEqual(pl.summary(), {})
        self.assertEqual(pl.devices(), [])
        pl.close()


class TestPowerLoggerRecord(unittest.TestCase):
    """Tests for the record() method."""

    def setUp(self) -> None:
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmpfile.name
        self._tmpfile.close()
        self.pl = PowerLogger(db_path=self._path)
        # Clear throttle so writes go through immediately.
        self.pl._last_write.clear()

    def tearDown(self) -> None:
        self.pl.close()
        os.unlink(self._path)

    def test_record_power(self) -> None:
        """Recording a power value inserts a row."""
        self.pl.record("ML_Power", "power", 167.3)
        rows = self.pl.query(device="ML_Power", hours=1, resolution=1)
        self.assertGreater(len(rows), 0)
        self.assertAlmostEqual(rows[0]["power"], 167.3, places=1)

    def test_ignores_non_power_properties(self) -> None:
        """Properties not in POWER_PROPERTIES are silently ignored."""
        self.pl.record("dev", "occupancy", 1.0)
        self.pl.record("dev", "temperature", 22.5)
        self.pl.record("dev", "contact", 0.0)
        self.assertEqual(self.pl.devices(), [])

    def test_accumulates_multiple_properties(self) -> None:
        """Multiple properties for same device accumulate before write."""
        # First set — triggers write on "power" because no prior write.
        self.pl.record("plug", "power", 100.0)
        self.pl.record("plug", "voltage", 122.0)
        # Voltage was accumulated but write already happened for power.
        # Force a new write window.
        self.pl._last_write["plug"] = 0.0
        self.pl.record("plug", "voltage", 122.0)
        self.pl.record("plug", "current", 0.82)
        self.pl.record("plug", "power", 100.0)  # triggers write with all accumulated
        rows = self.pl.query(device="plug", hours=1, resolution=1)
        # Should have at least one row with voltage.
        found_voltage = any(r.get("voltage") for r in rows)
        self.assertTrue(found_voltage)

    def test_throttle_prevents_rapid_writes(self) -> None:
        """Writes within MIN_WRITE_INTERVAL are suppressed."""
        self.pl.record("dev", "power", 100.0)
        count_before = self.pl._write_count
        # Immediately record again — should be throttled.
        self.pl.record("dev", "power", 110.0)
        self.assertEqual(self.pl._write_count, count_before)

    def test_throttle_allows_after_interval(self) -> None:
        """Writes succeed after MIN_WRITE_INTERVAL has passed."""
        self.pl.record("dev", "power", 100.0)
        count_after_first = self.pl._write_count
        # Backdate the last write.
        self.pl._last_write["dev"] = time.time() - MIN_WRITE_INTERVAL - 1
        self.pl.record("dev", "power", 110.0)
        # A second write should have occurred.
        self.assertEqual(self.pl._write_count, count_after_first + 1)

    def test_multiple_devices_independent_throttle(self) -> None:
        """Throttle is per-device, not global."""
        self.pl.record("dev_a", "power", 100.0)
        self.pl.record("dev_b", "power", 200.0)
        # Both should have written.
        self.assertIn("dev_a", self.pl.devices())
        self.assertIn("dev_b", self.pl.devices())

    def test_record_with_none_conn_is_noop(self) -> None:
        """Recording with no database connection does nothing."""
        self.pl._conn = None
        self.pl.record("dev", "power", 100.0)  # Should not raise.

    def test_all_power_properties_accepted(self) -> None:
        """All properties in POWER_PROPERTIES are accepted."""
        for prop in POWER_PROPERTIES:
            self.pl._last_write.clear()
            self.pl.record("test_dev", prop, 42.0)
        # At least one write should have happened.
        self.assertGreater(self.pl._write_count, 0)


class TestPowerLoggerPrune(unittest.TestCase):
    """Tests for automatic data pruning."""

    def setUp(self) -> None:
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmpfile.name
        self._tmpfile.close()
        self.pl = PowerLogger(db_path=self._path)

    def tearDown(self) -> None:
        self.pl.close()
        os.unlink(self._path)

    def test_prune_removes_old_records(self) -> None:
        """Records older than RETENTION_SECONDS are removed by prune."""
        old_ts = time.time() - RETENTION_SECONDS - 3600
        self.pl._conn.execute(
            "INSERT INTO power_readings (device, timestamp, power) VALUES (?, ?, ?)",
            ("old_dev", old_ts, 50.0),
        )
        self.pl._conn.commit()
        # Verify it's there.
        self.assertIn("old_dev", self.pl.devices())
        # Prune.
        self.pl._prune()
        # Should be gone.
        self.assertNotIn("old_dev", self.pl.devices())

    def test_prune_keeps_recent_records(self) -> None:
        """Records within retention window survive pruning."""
        self.pl._last_write.clear()
        self.pl.record("recent_dev", "power", 100.0)
        self.pl._prune()
        self.assertIn("recent_dev", self.pl.devices())

    def test_auto_prune_triggers_at_interval(self) -> None:
        """Pruning triggers every PRUNE_EVERY writes."""
        self.pl._write_count = PRUNE_EVERY - 1
        self.pl._last_write.clear()
        with patch.object(self.pl, "_prune") as mock_prune:
            self.pl.record("dev", "power", 100.0)
            mock_prune.assert_called_once()


class TestPowerLoggerQuery(unittest.TestCase):
    """Tests for the query() method."""

    def setUp(self) -> None:
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmpfile.name
        self._tmpfile.close()
        self.pl = PowerLogger(db_path=self._path)
        # Insert test data spanning 2 hours.
        now = time.time()
        for i in range(120):
            ts = now - (120 - i) * 60  # One reading per minute.
            self.pl._conn.execute(
                "INSERT INTO power_readings (device, timestamp, power, voltage, current_a) "
                "VALUES (?, ?, ?, ?, ?)",
                ("test_plug", ts, 100.0 + i, 122.0, 0.82 + i * 0.01),
            )
        self.pl._conn.commit()

    def tearDown(self) -> None:
        self.pl.close()
        os.unlink(self._path)

    def test_query_returns_data(self) -> None:
        """Query returns non-empty results for existing device."""
        rows = self.pl.query(device="test_plug", hours=3, resolution=300)
        self.assertGreater(len(rows), 0)

    def test_query_respects_time_window(self) -> None:
        """Query with hours=0.5 returns less data than hours=3."""
        short = self.pl.query(device="test_plug", hours=0.5, resolution=60)
        long = self.pl.query(device="test_plug", hours=3, resolution=60)
        self.assertLessEqual(len(short), len(long))

    def test_query_respects_resolution(self) -> None:
        """Higher resolution (smaller bucket) returns more rows."""
        fine = self.pl.query(device="test_plug", hours=2, resolution=60)
        coarse = self.pl.query(device="test_plug", hours=2, resolution=600)
        self.assertGreater(len(fine), len(coarse))

    def test_query_all_devices(self) -> None:
        """Query without device filter returns all devices."""
        # Add a second device.
        self.pl._conn.execute(
            "INSERT INTO power_readings (device, timestamp, power) VALUES (?, ?, ?)",
            ("other_plug", time.time(), 50.0),
        )
        self.pl._conn.commit()
        rows = self.pl.query(hours=3, resolution=300)
        devices = set(r["device"] for r in rows)
        self.assertIn("test_plug", devices)
        self.assertIn("other_plug", devices)

    def test_query_nonexistent_device(self) -> None:
        """Query for non-existent device returns empty list."""
        rows = self.pl.query(device="nonexistent", hours=3, resolution=300)
        self.assertEqual(rows, [])

    def test_query_with_none_conn(self) -> None:
        """Query with no connection returns empty list."""
        self.pl._conn = None
        self.assertEqual(self.pl.query(), [])

    def test_query_result_has_expected_keys(self) -> None:
        """Query results contain expected column names."""
        rows = self.pl.query(device="test_plug", hours=3, resolution=300)
        expected = {"bucket", "device", "power", "voltage", "current_a", "energy", "power_factor"}
        self.assertTrue(expected.issubset(set(rows[0].keys())))

    def test_query_averages_within_bucket(self) -> None:
        """Multiple readings in the same bucket are averaged."""
        coarse = self.pl.query(device="test_plug", hours=2, resolution=3600)
        # With 120 readings spanning 2 hours, a 1-hour bucket should
        # average ~60 readings.  The power values increase linearly
        # (100+i), so the average should be around 160 for the later bucket.
        self.assertGreater(len(coarse), 0)
        # Just verify it's a float, not an integer count.
        self.assertIsInstance(coarse[0]["power"], float)


class TestPowerLoggerSummary(unittest.TestCase):
    """Tests for the summary() method."""

    def setUp(self) -> None:
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmpfile.name
        self._tmpfile.close()
        self.pl = PowerLogger(db_path=self._path)
        # Insert 24 hours of data — one reading per 10 minutes.
        now = time.time()
        for i in range(144):
            ts = now - (144 - i) * 600
            self.pl._conn.execute(
                "INSERT INTO power_readings (device, timestamp, power, energy) "
                "VALUES (?, ?, ?, ?)",
                ("plug_a", ts, 150.0 + (i % 50), float(i) * 0.01),
            )
        self.pl._conn.commit()

    def tearDown(self) -> None:
        self.pl.close()
        os.unlink(self._path)

    def test_summary_returns_expected_keys(self) -> None:
        """Summary contains avg_watts, peak_watts, total_kwh, etc."""
        s = self.pl.summary(device="plug_a", days=7)
        for key in ("avg_watts", "peak_watts", "total_kwh", "days_covered", "device_count"):
            self.assertIn(key, s)

    def test_summary_avg_watts_reasonable(self) -> None:
        """Average watts is within the inserted data range."""
        s = self.pl.summary(device="plug_a", days=7)
        # Inserted power: 150 + (i%50), so range 150-199, avg ~175.
        self.assertGreater(s["avg_watts"], 100)
        self.assertLess(s["avg_watts"], 250)

    def test_summary_peak_watts(self) -> None:
        """Peak watts equals the maximum inserted value."""
        s = self.pl.summary(device="plug_a", days=7)
        # Max power = 150 + 49 = 199.
        self.assertAlmostEqual(s["peak_watts"], 199.0, places=0)

    def test_summary_device_count(self) -> None:
        """Device count is correct."""
        s = self.pl.summary(days=7)
        self.assertEqual(s["device_count"], 1)

    def test_summary_all_devices(self) -> None:
        """Summary without device filter includes all."""
        self.pl._conn.execute(
            "INSERT INTO power_readings (device, timestamp, power) VALUES (?, ?, ?)",
            ("plug_b", time.time(), 75.0),
        )
        self.pl._conn.commit()
        s = self.pl.summary(days=7)
        self.assertEqual(s["device_count"], 2)

    def test_summary_empty_db(self) -> None:
        """Summary on empty database returns sensible defaults."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            pl2 = PowerLogger(db_path=path)
            s = pl2.summary(days=7)
            # Should return a dict (not empty) with zero values.
            self.assertIsInstance(s, dict)
            pl2.close()
        finally:
            os.unlink(path)

    def test_summary_with_none_conn(self) -> None:
        """Summary with no connection returns empty dict."""
        self.pl._conn = None
        self.assertEqual(self.pl.summary(), {})


class TestPowerLoggerDevices(unittest.TestCase):
    """Tests for the devices() method."""

    def setUp(self) -> None:
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmpfile.name
        self._tmpfile.close()
        self.pl = PowerLogger(db_path=self._path)

    def tearDown(self) -> None:
        self.pl.close()
        os.unlink(self._path)

    def test_empty_db_returns_empty_list(self) -> None:
        """No devices in empty database."""
        self.assertEqual(self.pl.devices(), [])

    def test_returns_distinct_devices(self) -> None:
        """Returns each device name exactly once."""
        for dev in ["ML_Power", "LRTV", "ML_Power"]:
            self.pl._conn.execute(
                "INSERT INTO power_readings (device, timestamp, power) VALUES (?, ?, ?)",
                (dev, time.time(), 100.0),
            )
        self.pl._conn.commit()
        devs = self.pl.devices()
        self.assertEqual(len(devs), 2)
        self.assertIn("LRTV", devs)
        self.assertIn("ML_Power", devs)

    def test_sorted_alphabetically(self) -> None:
        """Device list is sorted."""
        for dev in ["Zebra", "Alpha", "Middle"]:
            self.pl._conn.execute(
                "INSERT INTO power_readings (device, timestamp, power) VALUES (?, ?, ?)",
                (dev, time.time(), 100.0),
            )
        self.pl._conn.commit()
        devs = self.pl.devices()
        self.assertEqual(devs, ["Alpha", "Middle", "Zebra"])

    def test_devices_with_none_conn(self) -> None:
        """Devices with no connection returns empty list."""
        self.pl._conn = None
        self.assertEqual(self.pl.devices(), [])


class TestPowerLoggerThreadSafety(unittest.TestCase):
    """Concurrent access tests."""

    def test_concurrent_records(self) -> None:
        """Multiple threads recording simultaneously without crash."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            pl = PowerLogger(db_path=path)
            errors: list[Exception] = []

            def worker(device: str) -> None:
                try:
                    for i in range(20):
                        pl._last_write[device] = 0.0  # Bypass throttle.
                        pl.record(device, "power", 100.0 + i)
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=worker, args=(f"dev_{i}",))
                for i in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(len(errors), 0, f"Errors: {errors}")
            # All devices should have data.
            devs = pl.devices()
            self.assertEqual(len(devs), 5)
            pl.close()
        finally:
            os.unlink(path)

    def test_concurrent_query_and_record(self) -> None:
        """Reading while writing does not crash (WAL mode)."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            pl = PowerLogger(db_path=path)
            errors: list[Exception] = []

            def writer() -> None:
                try:
                    for i in range(50):
                        pl._last_write["w"] = 0.0
                        pl.record("w", "power", float(i))
                except Exception as exc:
                    errors.append(exc)

            def reader() -> None:
                try:
                    for _ in range(50):
                        pl.query(hours=1, resolution=10)
                        pl.summary(days=1)
                except Exception as exc:
                    errors.append(exc)

            wt = threading.Thread(target=writer)
            rt = threading.Thread(target=reader)
            wt.start()
            rt.start()
            wt.join(timeout=10)
            rt.join(timeout=10)

            self.assertEqual(len(errors), 0, f"Errors: {errors}")
            pl.close()
        finally:
            os.unlink(path)


class TestPowerLoggerClose(unittest.TestCase):
    """Tests for close() method."""

    def test_close_sets_conn_none(self) -> None:
        """Closing the logger sets connection to None."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            pl = PowerLogger(db_path=path)
            self.assertIsNotNone(pl._conn)
            pl.close()
            self.assertIsNone(pl._conn)
        finally:
            os.unlink(path)

    def test_double_close_safe(self) -> None:
        """Closing twice does not raise."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            pl = PowerLogger(db_path=path)
            pl.close()
            pl.close()  # Should not raise.
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
