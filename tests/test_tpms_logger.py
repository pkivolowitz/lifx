#!/usr/bin/env python3
"""Unit tests for infrastructure.tpms_logger.TpmsLogger.

Covers the persistence surface without touching MQTT:

- record() happy path, missing fields, JSONB preservation
- unique_sensors() aggregation (first/last/count) and ordering
- last_seen_ts() across the full observation set
- Retention pruning removes old rows
- _coerce_float resilience against mixed rtl_433 types

Tests require a live PostgreSQL connection.  Set GLOWUP_DIAG_DSN or
the DEFAULT_DSN default must be reachable.  Skipped automatically
otherwise.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import os
import time
import unittest
from typing import Any

from infrastructure.tpms_logger import (
    DEFAULT_DSN,
    RETENTION_SECONDS,
    TpmsLogger,
    _coerce_float,
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

# All test rows use this model prefix so setUp/tearDown can scrub
# them without touching production data.
TEST_MODEL_PREFIX: str = "TestModel-"


def _frame(
    model_suffix: str,
    sensor_id: str,
    pressure_kPa: float = 230.0,
    temperature_C: float = 22.5,
    battery_ok: bool = True,
) -> dict[str, Any]:
    return {
        "time": "2026-04-22 00:00:00",
        "model": TEST_MODEL_PREFIX + model_suffix,
        "type": "TPMS",
        "id": sensor_id,
        "status": 24,
        "battery_ok": 1 if battery_ok else 0,
        "counter": 3,
        "failed": "OK",
        "pressure_kPa": pressure_kPa,
        "temperature_C": temperature_C,
        "mic": "CRC",
    }


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TpmsLoggerTestCase(unittest.TestCase):
    """Base: fresh TpmsLogger per test, test rows scrubbed."""

    def setUp(self) -> None:
        self._tl: TpmsLogger = TpmsLogger(dsn=_TEST_DSN)
        self.assertIsNotNone(self._tl._conn, "PG connection must be open")
        self._exec(
            "DELETE FROM tpms_observations WHERE model LIKE %s",
            (TEST_MODEL_PREFIX + "%",),
        )

    def tearDown(self) -> None:
        self._exec(
            "DELETE FROM tpms_observations WHERE model LIKE %s",
            (TEST_MODEL_PREFIX + "%",),
        )
        self._tl.close()

    def _exec(self, sql: str, params: tuple = ()) -> list:
        """Run SQL directly against the logger's connection."""
        with self._tl._conn.cursor() as cur:
            cur.execute(sql, params)
            try:
                return cur.fetchall()
            except Exception:
                return []

    def _count(self, model: str) -> int:
        rows = self._exec(
            "SELECT COUNT(*) FROM tpms_observations WHERE model = %s",
            (model,),
        )
        return rows[0][0] if rows else 0


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestRecord(TpmsLoggerTestCase):
    """Write path — happy, rejection, JSONB round-trip."""

    def test_record_happy_path(self) -> None:
        model = TEST_MODEL_PREFIX + "A"
        self._tl.record(_frame("A", "00000001", pressure_kPa=247.5))
        sensors = self._tl.unique_sensors()
        matching = [s for s in sensors if s["model"] == model]
        self.assertEqual(len(matching), 1)
        entry = matching[0]
        self.assertEqual(entry["id"], "00000001")
        self.assertEqual(entry["count"], 1)
        # last_payload must round-trip through JSONB.
        self.assertAlmostEqual(
            entry["last_payload"].get("pressure_kPa"), 247.5, places=3,
        )

    def test_record_missing_model_or_id_dropped(self) -> None:
        bad_no_model = _frame("X", "00000002")
        del bad_no_model["model"]
        self._tl.record(bad_no_model)
        bad_no_id = _frame("X", "00000002")
        del bad_no_id["id"]
        self._tl.record(bad_no_id)
        # No rows under TEST_MODEL_PREFIX-X should exist.
        self.assertEqual(self._count(TEST_MODEL_PREFIX + "X"), 0)

    def test_record_coerces_non_numeric_fields(self) -> None:
        # rtl_433 occasionally emits integers or strings where the
        # logger expects floats.  Coercion must not crash the write.
        frame = _frame("B", "00000003")
        frame["pressure_kPa"] = "230"
        frame["temperature_C"] = 22
        self._tl.record(frame)
        sensors = self._tl.unique_sensors()
        matching = [
            s for s in sensors if s["model"] == TEST_MODEL_PREFIX + "B"
        ]
        self.assertEqual(len(matching), 1)

    def test_record_treats_garbage_numbers_as_null(self) -> None:
        frame = _frame("C", "00000004")
        frame["pressure_kPa"] = "not-a-number"
        self._tl.record(frame)
        rows = self._exec(
            "SELECT pressure_kpa FROM tpms_observations WHERE model = %s",
            (TEST_MODEL_PREFIX + "C",),
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0][0])


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestUniqueSensors(TpmsLoggerTestCase):
    """Aggregation and ordering."""

    def test_unique_sensors_counts_bursts(self) -> None:
        model = TEST_MODEL_PREFIX + "D"
        for i in range(8):
            self._tl.record(_frame("D", "00000005"))
        matching = [
            s for s in self._tl.unique_sensors() if s["model"] == model
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["count"], 8)
        # first_seen <= last_seen (same second is fine)
        self.assertLessEqual(
            matching[0]["first_seen"], matching[0]["last_seen"],
        )

    def test_unique_sensors_sorted_by_last_seen_desc(self) -> None:
        # Two distinct sensors, second logged after first — second
        # must sort ahead.
        self._tl.record(_frame("E", "00000006"))
        time.sleep(0.01)
        self._tl.record(_frame("E", "00000007"))
        test_rows = [
            s for s in self._tl.unique_sensors()
            if s["model"] == TEST_MODEL_PREFIX + "E"
        ]
        self.assertEqual(len(test_rows), 2)
        self.assertGreaterEqual(
            test_rows[0]["last_seen"], test_rows[1]["last_seen"],
        )


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestLastSeenTs(TpmsLoggerTestCase):
    """last_seen_ts returns the MAX across all rows, 0 if empty."""

    def test_last_seen_ts_reflects_latest_row(self) -> None:
        before = self._tl.last_seen_ts()
        self._tl.record(_frame("F", "00000008"))
        after = self._tl.last_seen_ts()
        self.assertGreater(after, before)


@unittest.skipUnless(_DB_AVAILABLE, _SKIP_REASON)
class TestRetention(TpmsLoggerTestCase):
    """_prune deletes rows older than RETENTION_SECONDS."""

    def test_prune_removes_ancient_rows(self) -> None:
        model = TEST_MODEL_PREFIX + "G"
        stale_ts = time.time() - RETENTION_SECONDS - 3600
        # Direct insert to place a row with an explicit stale ts.
        self._exec(
            """INSERT INTO tpms_observations
               (timestamp, model, sensor_id, pressure_kpa,
                temperature_c, battery_ok, payload)
               VALUES (%s, %s, %s, NULL, NULL, NULL, NULL)""",
            (stale_ts, model, "00000009"),
        )
        self.assertEqual(self._count(model), 1)
        self._tl._prune()
        self.assertEqual(self._count(model), 0)


class TestCoerceFloat(unittest.TestCase):
    """_coerce_float — module-level helper, no DB needed."""

    def test_none_becomes_none(self) -> None:
        self.assertIsNone(_coerce_float(None))

    def test_int_becomes_float(self) -> None:
        self.assertAlmostEqual(_coerce_float(7), 7.0)

    def test_numeric_string_becomes_float(self) -> None:
        self.assertAlmostEqual(_coerce_float("3.14"), 3.14)

    def test_garbage_string_becomes_none(self) -> None:
        self.assertIsNone(_coerce_float("nope"))


if __name__ == "__main__":
    unittest.main()
