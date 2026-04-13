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


class TestPowerLoggerMarkOffline(unittest.TestCase):
    """Tests for mark_offline() — the retained-MQTT-replay defense."""

    def setUp(self) -> None:
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmpfile.name
        self._tmpfile.close()
        self.pl = PowerLogger(db_path=self._path)
        self.pl._last_write.clear()

    def tearDown(self) -> None:
        self.pl.close()
        os.unlink(self._path)

    def test_mark_offline_writes_null_sentinel_row(self) -> None:
        """mark_offline writes a row with NULL values at the current time."""
        # Prime with a real reading.
        self.pl.record("ML_Power", "power", 168.5)
        self.pl.record("ML_Power", "voltage", 121.6)
        self.pl._last_write["ML_Power"] = 0.0
        self.pl.record("ML_Power", "power", 168.5)

        # Now mark offline.
        self.pl.mark_offline("ML_Power")

        # Query the raw row table directly — `query()` averages within
        # time buckets, and two rows recorded in the same second would
        # collapse to a non-null average.  What matters for the
        # dashboard is that the *absolute most recent* raw row has
        # NULL power, because the frontend reads the last bucket of
        # the time-series and we want that bucket's average to be
        # NULL-only (no live values mixed in) on the next real query.
        cursor = self.pl._conn.execute(
            "SELECT power, voltage, current_a, energy, power_factor "
            "FROM power_readings WHERE device = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            ("ML_Power",),
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row, "expected at least one row")
        self.assertTrue(
            all(v is None for v in row),
            f"expected newest raw row to be all-NULL, got {row}",
        )

    def test_mark_offline_clears_pending_carry_forward(self) -> None:
        """After mark_offline, _pending for the device is empty so a
        later flush_pending cannot resurrect pre-offline values."""
        self.pl.record("ML_Power", "power", 168.5)
        self.pl.record("ML_Power", "voltage", 121.6)
        # _pending should now contain ML_Power.
        self.assertIn("ML_Power", self.pl._pending)
        self.assertEqual(self.pl._pending["ML_Power"]["power"], 168.5)

        self.pl.mark_offline("ML_Power")

        # _pending no longer holds the stale values.
        self.assertNotIn("ML_Power", self.pl._pending)
        self.assertNotIn("ML_Power", self.pl._dirty)

    def test_mark_offline_does_not_affect_other_devices(self) -> None:
        """mark_offline on one device leaves other devices' pending state alone."""
        self.pl.record("ML_Power", "power", 168.5)
        self.pl.record("LRTV", "power", 100.0)
        self.assertIn("LRTV", self.pl._pending)

        self.pl.mark_offline("ML_Power")

        self.assertIn("LRTV", self.pl._pending)
        self.assertEqual(self.pl._pending["LRTV"]["power"], 100.0)

    def test_mark_offline_on_unknown_device_writes_sentinel(self) -> None:
        """mark_offline on a device never seen before still records
        the transition — valid for the adapter-side case where a
        device comes up already offline and we have no prior row."""
        self.pl.mark_offline("NeverSeen")
        rows = self.pl.query(device="NeverSeen", hours=1, resolution=1)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["power"])

    def test_mark_offline_with_none_conn_is_noop(self) -> None:
        """Closed/failed connection path must not crash."""
        self.pl._conn = None
        # Must not raise.
        self.pl.mark_offline("ML_Power")


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


class TestPowerLoggerPeriodicFlush(unittest.TestCase):
    """Tests for the periodic flush timer.

    When MqttSignalBus dedup suppresses unchanged signals, PowerLogger
    stops receiving record() calls.  Accumulated data in _pending must
    still be flushed to the database by a background timer.
    """

    def setUp(self) -> None:
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmpfile.name
        self._tmpfile.close()
        self.pl = PowerLogger(db_path=self._path)
        self.pl._last_write.clear()

    def tearDown(self) -> None:
        self.pl.close()
        os.unlink(self._path)

    def test_pending_data_flushed_without_record_call(self) -> None:
        """Pending data must be flushed even if no further record() arrives.

        This is the exact scenario that broke the power dashboard:
        a value gets accumulated and throttled, then MqttSignalBus
        dedup suppresses all subsequent signals.  The flush timer
        must write the orphaned pending data.
        """
        # First write goes through — establishes baseline.
        self.pl.record("BYIR", "power", 0.0)
        count_after_first = self.pl._write_count

        # Second record within throttle window — accumulated but not written.
        self.pl.record("BYIR", "power", 13.1)
        self.assertEqual(self.pl._write_count, count_after_first,
                         "Throttled record should not have written yet")
        self.assertIn("BYIR", self.pl._pending,
                       "Throttled value must be in _pending buffer")

        # Backdate last write so flush timer considers it eligible.
        self.pl._last_write["BYIR"] = time.time() - MIN_WRITE_INTERVAL - 1

        # Trigger the flush (simulates what the background timer does).
        self.pl.flush_pending()

        # The 13.1W value must now be in the database.
        # Use write_count to confirm a second write occurred.
        self.assertGreater(self.pl._write_count, count_after_first,
                           "Pending 13.1W was not flushed to database")

    def test_flush_respects_throttle(self) -> None:
        """Flush must not write data whose device is still within throttle."""
        self.pl.record("dev", "power", 100.0)
        # Accumulate without writing.
        self.pl.record("dev", "power", 200.0)
        # Do NOT backdate — throttle is still active.
        self.pl.flush_pending()
        # Should still be pending, not written.
        self.assertIn("dev", self.pl._pending)

    def test_flush_clears_dirty_but_keeps_pending(self) -> None:
        """After flush writes, _dirty is cleared but _pending state is kept.

        Updated 2026-04-08 from the original test_flush_clears_pending,
        which asserted ``_pending`` was destructively popped after each
        flush.  That destructive pop was the root cause of the LRTV
        NULL-column bug — see TestPowerLoggerCarryForward and the
        project_zigbee_adapter_zombie / power_logger memory entries.
        New semantics: _pending holds the device's full carry-forward
        state and is never popped; _dirty tracks whether anything new
        has arrived since the last write.
        """
        self.pl.record("dev", "power", 50.0)
        self.pl.record("dev", "power", 75.0)
        self.pl._last_write["dev"] = time.time() - MIN_WRITE_INTERVAL - 1
        self.pl.flush_pending()
        # Carry-forward state is preserved.
        self.assertIn("dev", self.pl._pending)
        self.assertEqual(self.pl._pending["dev"].get("power"), 75.0)
        # And the dirty flag was cleared by the write.
        self.assertFalse(self.pl._dirty.get("dev", False))

    def test_flush_noop_when_no_pending(self) -> None:
        """Flush with empty _pending does nothing and does not crash."""
        self.pl.flush_pending()  # Must not raise.

    def test_flush_multiple_devices(self) -> None:
        """Flush writes pending data for all eligible devices.

        With carry-forward semantics, _pending is never popped — both
        devices retain their state across the flush.  The observable
        difference is the per-device _dirty flag: A's was cleared by
        the flush write; B's stays True because B was still inside
        its throttle window so no row was written for it.
        """
        # Device A — eligible for flush.
        self.pl.record("A", "power", 10.0)
        self.pl.record("A", "power", 20.0)
        self.pl._last_write["A"] = time.time() - MIN_WRITE_INTERVAL - 1

        # Device B — still within throttle.
        self.pl.record("B", "power", 30.0)
        self.pl.record("B", "power", 40.0)
        # B's last write is recent — don't backdate.

        self.pl.flush_pending()

        # Both devices still have carry-forward state.
        self.assertIn("A", self.pl._pending)
        self.assertIn("B", self.pl._pending)
        # A was flushed (dirty cleared), B was throttle-skipped (still dirty).
        self.assertFalse(self.pl._dirty.get("A", False))
        self.assertTrue(self.pl._dirty.get("B", False))


class TestPowerLoggerCarryForward(unittest.TestCase):
    """Regression tests for the carry-forward fix (2026-04-08).

    Background:  ThirdReality smart plugs (and most Zigbee devices) do
    change-based reporting — a single Z2M message contains only the
    properties that have crossed the report-on-change threshold since
    the last sample.  In production on 2026-04-08, the LRTV plug
    appeared on the dashboard to "drop to 0 W" every few seconds
    while the TV was running, but the plug never actually reported
    0 W.  Investigation showed:

    - The drops were rendered from rows where ``power`` was NULL.
    - The chart's ``readings[i].power || 0`` JS coercion was silently
      turning NULL into 0 for display.
    - The NULL rows were a write artifact: the original ``record()``
      destructively popped ``_pending`` on every write, so when a
      sparse Z2M message arrived containing only ``current``, the
      row written for it had NULL for the four other columns.

    The fix changes ``_pending`` to a carry-forward state that is
    never popped, plus a per-device ``_dirty`` flag so flush_pending
    does not write redundant snapshots.  These tests pin both halves
    of the new behavior so the bug cannot silently come back.

    The test ``test_lrtv_sparse_messages_produce_complete_rows`` is
    the literal production reproduction — it simulates the exact
    sequence that produced the bad rows in the live database and
    asserts every row has all five columns populated.  It would
    fail against the pre-fix code.
    """

    def setUp(self) -> None:
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmpfile.name
        self._tmpfile.close()
        self.pl = PowerLogger(db_path=self._path)
        self.pl._last_write.clear()

    def tearDown(self) -> None:
        self.pl.close()
        os.unlink(self._path)

    def _all_rows(self, device: str) -> list[tuple]:
        """Return every row for a device with all five reading columns.

        Bypasses ``query()`` because that method buckets and averages —
        we need to inspect the raw rows to see NULLs.
        """
        conn = sqlite3.connect(self._path)
        try:
            return conn.execute(
                "SELECT power, voltage, current_a, energy, power_factor "
                "FROM power_readings WHERE device=? ORDER BY id",
                (device,),
            ).fetchall()
        finally:
            conn.close()

    def test_pending_is_not_popped_after_write(self) -> None:
        """After a write, _pending[device] still contains the snapshot."""
        self.pl.record("dev", "power", 100.0)
        # Write happened — _pending must STILL hold the value.
        self.assertIn("dev", self.pl._pending)
        self.assertEqual(self.pl._pending["dev"].get("power"), 100.0)
        # And _dirty was cleared.
        self.assertFalse(self.pl._dirty.get("dev", False))

    def test_subsequent_record_merges_into_pending(self) -> None:
        """A second property record() merges into the carry-forward dict."""
        self.pl.record("dev", "power", 100.0)
        # Throttle is now active.  Add voltage — must merge, not replace.
        self.pl.record("dev", "voltage", 124.8)
        self.assertEqual(self.pl._pending["dev"].get("power"), 100.0)
        self.assertEqual(self.pl._pending["dev"].get("voltage"), 124.8)
        # And the merge marked the device dirty (a write is owed).
        self.assertTrue(self.pl._dirty.get("dev", False))

    def test_lrtv_sparse_messages_produce_complete_rows(self) -> None:
        """The production LRTV reproduction.

        Simulates: a complete first Z2M message with all five
        properties, then a sparse second Z2M message containing
        ONLY ``current`` (the most-changing property on a TV plug).
        After the fix, the row written for the sparse message must
        contain the carried-forward values of the four other
        properties, not NULL.

        Pre-fix behavior: the second row had power=NULL, voltage=NULL,
        energy=NULL, power_factor=NULL — the chart's ``|| 0`` coercion
        rendered the NULL power as a drop to 0 W.

        Post-fix behavior: every row carries the most recently seen
        value of every property.
        """
        # First Z2M message — all five properties.  Throttle is empty,
        # so the first record() call writes a row with whichever
        # property arrives first.  The remaining properties accumulate
        # in _pending under throttle suppression.
        self.pl.record("LRTV", "power", 245.0)
        self.pl.record("LRTV", "voltage", 124.8)
        self.pl.record("LRTV", "current", 2.0)
        self.pl.record("LRTV", "energy", 5.11)
        self.pl.record("LRTV", "power_factor", 1.0)

        # Backdate so the next call is past the throttle window —
        # the next record() will write a fresh row.
        self.pl._last_write["LRTV"] = time.time() - MIN_WRITE_INTERVAL - 1

        # Sparse second Z2M message — ONLY ``current`` changed.
        # This is the EXACT shape that produced
        #     (None, None, 2.13, None, None)
        # rows in the production database before the fix.
        self.pl.record("LRTV", "current", 2.13)

        rows: list[tuple] = self._all_rows("LRTV")
        self.assertGreaterEqual(
            len(rows), 2,
            "Expected at least 2 rows (one per write window)",
        )

        # The most recent row — the one written from the sparse
        # current-only message — must carry forward all the other
        # properties.  Pre-fix, this row was
        # (None, None, 2.13, None, None); post-fix, all five must
        # be populated.
        last_row: tuple = rows[-1]
        col_names: tuple = (
            "power", "voltage", "current_a", "energy", "power_factor",
        )
        for col, val in zip(col_names, last_row):
            self.assertIsNotNone(
                val,
                f"Carry-forward failed: column {col} is NULL in row "
                f"written from sparse message.  Full row: "
                f"{dict(zip(col_names, last_row))}",
            )
        # And the values must be the carry-forward, not stale garbage.
        self.assertAlmostEqual(last_row[0], 245.0, places=1)   # power
        self.assertAlmostEqual(last_row[1], 124.8, places=1)   # voltage
        self.assertAlmostEqual(last_row[2], 2.13, places=2)    # current (fresh)
        self.assertAlmostEqual(last_row[3], 5.11, places=2)    # energy
        self.assertAlmostEqual(last_row[4], 1.0, places=2)     # power_factor

    def test_flush_pending_skips_when_not_dirty(self) -> None:
        """flush_pending must NOT write a row when _dirty is False.

        Without the dirty flag, carry-forward state would generate a
        steady stream of redundant identical snapshots whenever a
        device went silent.  The dirty flag is the throttle that
        keeps the database honest about what has actually changed.
        """
        self.pl.record("dev", "power", 100.0)
        # Now _dirty["dev"] is False (write just happened).
        write_count_before: int = self.pl._write_count
        # Backdate the throttle so the only thing standing between
        # us and a redundant write is the dirty flag.
        self.pl._last_write["dev"] = time.time() - MIN_WRITE_INTERVAL - 1
        self.pl.flush_pending()
        # No new row was written — silence stays silent.
        self.assertEqual(
            self.pl._write_count, write_count_before,
            "flush_pending wrote a redundant row when nothing was dirty",
        )

    def test_flush_pending_writes_dirty_after_dedup_silence(self) -> None:
        """flush_pending must write the latest snapshot for the dedup case.

        Scenario the original flush_pending was added to handle:
        a property arrives, gets accumulated and throttled, then
        MqttSignalBus dedup suppresses every subsequent value (or
        the device just goes quiet).  Without flush_pending, the
        accumulated value sits in _pending forever.
        """
        # First write — establishes baseline, _dirty=False.
        self.pl.record("dev", "power", 100.0)
        write_count_after_first: int = self.pl._write_count
        # Sparse follow-up update arrives within the throttle
        # window — accumulated, marked dirty, not written yet.
        self.pl.record("dev", "voltage", 124.8)
        self.assertTrue(self.pl._dirty.get("dev"))
        # No further record() calls (dedup, silence, whatever).
        # Backdate so the throttle is no longer the gate.
        self.pl._last_write["dev"] = time.time() - MIN_WRITE_INTERVAL - 1
        self.pl.flush_pending()
        # A row WAS written — flush honored the dirty flag.
        self.assertGreater(
            self.pl._write_count, write_count_after_first,
            "Dirty pending data was not flushed",
        )
        # And dirty is now cleared.
        self.assertFalse(self.pl._dirty.get("dev", False))
        # Next flush is a no-op (silence stays silent again).
        write_count_after_flush: int = self.pl._write_count
        self.pl._last_write["dev"] = time.time() - MIN_WRITE_INTERVAL - 1
        self.pl.flush_pending()
        self.assertEqual(self.pl._write_count, write_count_after_flush)


if __name__ == "__main__":
    unittest.main()
