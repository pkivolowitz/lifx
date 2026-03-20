"""Tests for the diagnostics subsystem.

Requires a reachable PostgreSQL instance with the glowup database.
Tests are skipped automatically when psycopg2 is not installed or
the database is unreachable.

Run::

    python3 -m pytest tests/test_diagnostics.py -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import os
import sys
import unittest

# ---------------------------------------------------------------------------
# Skip the entire module if psycopg2 is unavailable.
# ---------------------------------------------------------------------------

try:
    import psycopg2
    _HAS_PSYCOPG2: bool = True
except ImportError:
    _HAS_PSYCOPG2 = False

# Ensure the project root is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diagnostics import DiagnosticsLogger, DEFAULT_DSN, _HAS_PSYCOPG2 as _MOD_HAS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_DEVICE_IP: str = "10.99.99.99"
TEST_DEVICE_LABEL: str = "TestDevice"
TEST_EFFECT: str = "test_effect"
TEST_PARAMS: dict = {"speed": 2.5, "brightness": 80}


def _db_reachable() -> bool:
    """Check whether the diagnostics database is reachable."""
    if not _HAS_PSYCOPG2:
        return False
    try:
        dsn: str = os.environ.get("GLOWUP_DIAG_DSN", DEFAULT_DSN)
        conn = psycopg2.connect(dsn, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


_SKIP_REASON: str = "psycopg2 not installed or database unreachable"
_CAN_TEST: bool = _db_reachable()


def _cleanup(diag: DiagnosticsLogger) -> None:
    """Remove all test records from the database."""
    diag._execute(
        "DELETE FROM effect_history WHERE device_ip = %s",
        (TEST_DEVICE_IP,),
    )


@unittest.skipUnless(_CAN_TEST, _SKIP_REASON)
class TestDiagnosticsLogger(unittest.TestCase):
    """Integration tests for DiagnosticsLogger against a real database."""

    @classmethod
    def setUpClass(cls) -> None:
        """Create a shared logger instance and clean up test data."""
        cls.diag: DiagnosticsLogger = DiagnosticsLogger.from_env()
        assert cls.diag is not None, "Failed to connect to diagnostics DB"
        _cleanup(cls.diag)

    @classmethod
    def tearDownClass(cls) -> None:
        """Clean up test data and close the connection."""
        _cleanup(cls.diag)
        cls.diag.close()

    def setUp(self) -> None:
        """Ensure a clean state before each test."""
        _cleanup(self.diag)

    # -- log_play -----------------------------------------------------------

    def test_log_play_returns_row_id(self) -> None:
        """log_play should return an integer row ID."""
        row_id = self.diag.log_play(
            TEST_DEVICE_IP, TEST_DEVICE_LABEL, TEST_EFFECT,
            params=TEST_PARAMS, started_by="test",
        )
        self.assertIsInstance(row_id, int)
        self.assertGreater(row_id, 0)

    def test_log_play_stores_params_as_json(self) -> None:
        """Params should be stored and retrievable as a dict."""
        self.diag.log_play(
            TEST_DEVICE_IP, TEST_DEVICE_LABEL, TEST_EFFECT,
            params=TEST_PARAMS, started_by="test",
        )
        rows = self.diag.query_now_playing()
        test_rows = [r for r in rows if r["device_ip"] == TEST_DEVICE_IP]
        self.assertEqual(len(test_rows), 1)
        self.assertEqual(test_rows[0]["params"], TEST_PARAMS)

    def test_log_play_null_params(self) -> None:
        """log_play with no params should store NULL."""
        self.diag.log_play(
            TEST_DEVICE_IP, TEST_DEVICE_LABEL, TEST_EFFECT,
            started_by="test",
        )
        rows = self.diag.query_now_playing()
        test_rows = [r for r in rows if r["device_ip"] == TEST_DEVICE_IP]
        self.assertEqual(len(test_rows), 1)
        self.assertIsNone(test_rows[0]["params"])

    # -- log_stop -----------------------------------------------------------

    def test_log_stop_closes_record(self) -> None:
        """log_stop should set stopped_at and stop_reason."""
        self.diag.log_play(
            TEST_DEVICE_IP, TEST_DEVICE_LABEL, TEST_EFFECT,
            started_by="test",
        )
        result = self.diag.log_stop(TEST_DEVICE_IP, stop_reason="user")
        self.assertTrue(result)

        # Should no longer appear in now_playing.
        rows = self.diag.query_now_playing()
        test_rows = [r for r in rows if r["device_ip"] == TEST_DEVICE_IP]
        self.assertEqual(len(test_rows), 0)

    def test_log_stop_sets_reason(self) -> None:
        """log_stop should record the stop reason in history."""
        self.diag.log_play(
            TEST_DEVICE_IP, TEST_DEVICE_LABEL, TEST_EFFECT,
            started_by="test",
        )
        self.diag.log_stop(TEST_DEVICE_IP, stop_reason="replaced")

        rows = self.diag.query_history(limit=10)
        test_rows = [r for r in rows if r["device_ip"] == TEST_DEVICE_IP]
        self.assertEqual(len(test_rows), 1)
        self.assertEqual(test_rows[0]["stop_reason"], "replaced")
        self.assertIsNotNone(test_rows[0]["stopped_at"])

    def test_log_stop_no_open_record(self) -> None:
        """log_stop on a device with no open record should not crash."""
        result = self.diag.log_stop(TEST_DEVICE_IP, stop_reason="user")
        # Should succeed (execute returns True) even if no rows updated.
        self.assertTrue(result)

    # -- close_stale_records ------------------------------------------------

    def test_close_stale_records(self) -> None:
        """close_stale_records should close all open records."""
        # Create two open records on different "devices".
        self.diag.log_play(
            TEST_DEVICE_IP, TEST_DEVICE_LABEL, "effect_a",
            started_by="test",
        )
        self.diag.log_play(
            TEST_DEVICE_IP, TEST_DEVICE_LABEL, "effect_b",
            started_by="test",
        )
        count = self.diag.close_stale_records()
        # At least our 2 test records should be closed.
        self.assertGreaterEqual(count, 2)

        # No open records for our test device.
        rows = self.diag.query_now_playing()
        test_rows = [r for r in rows if r["device_ip"] == TEST_DEVICE_IP]
        self.assertEqual(len(test_rows), 0)

    # -- query_now_playing --------------------------------------------------

    def test_query_now_playing_returns_open_only(self) -> None:
        """query_now_playing should return only records with no stopped_at."""
        self.diag.log_play(
            TEST_DEVICE_IP, TEST_DEVICE_LABEL, "open_effect",
            started_by="test",
        )
        self.diag.log_play(
            TEST_DEVICE_IP, TEST_DEVICE_LABEL, "closed_effect",
            started_by="test",
        )
        self.diag.log_stop(TEST_DEVICE_IP, stop_reason="test")

        rows = self.diag.query_now_playing()
        test_rows = [r for r in rows if r["device_ip"] == TEST_DEVICE_IP]
        self.assertEqual(len(test_rows), 1)
        self.assertEqual(test_rows[0]["effect_name"], "open_effect")

    # -- query_history ------------------------------------------------------

    def test_query_history_respects_limit(self) -> None:
        """query_history should return at most N records."""
        for i in range(5):
            self.diag.log_play(
                TEST_DEVICE_IP, TEST_DEVICE_LABEL, f"effect_{i}",
                started_by="test",
            )
            self.diag.log_stop(TEST_DEVICE_IP, stop_reason="test")

        rows = self.diag.query_history(limit=3)
        # Could have records from other tests, so just check limit works.
        self.assertLessEqual(len(rows), 3)

    def test_query_history_ordered_desc(self) -> None:
        """query_history should return newest first."""
        for i in range(3):
            self.diag.log_play(
                TEST_DEVICE_IP, TEST_DEVICE_LABEL, f"effect_{i}",
                started_by="test",
            )
            self.diag.log_stop(TEST_DEVICE_IP, stop_reason="test")

        rows = self.diag.query_history(limit=50)
        test_rows = [r for r in rows if r["device_ip"] == TEST_DEVICE_IP]
        # Verify descending order by started_at.
        for i in range(len(test_rows) - 1):
            self.assertGreaterEqual(
                test_rows[i]["started_at"],
                test_rows[i + 1]["started_at"],
            )

    def test_query_history_includes_timestamps(self) -> None:
        """History records should have ISO-formatted timestamps."""
        self.diag.log_play(
            TEST_DEVICE_IP, TEST_DEVICE_LABEL, TEST_EFFECT,
            started_by="test",
        )
        self.diag.log_stop(TEST_DEVICE_IP, stop_reason="test")

        rows = self.diag.query_history(limit=10)
        test_rows = [r for r in rows if r["device_ip"] == TEST_DEVICE_IP]
        self.assertEqual(len(test_rows), 1)
        self.assertIn("T", test_rows[0]["started_at"])  # ISO format
        self.assertIn("T", test_rows[0]["stopped_at"])

    # -- Graceful degradation -----------------------------------------------

    def test_bad_dsn_returns_none(self) -> None:
        """from_env with a bad DSN should return None."""
        original = os.environ.get("GLOWUP_DIAG_DSN")
        try:
            os.environ["GLOWUP_DIAG_DSN"] = "postgresql://bad:bad@127.0.0.1:1/bad"
            result = DiagnosticsLogger.from_env()
            self.assertIsNone(result)
        finally:
            if original is not None:
                os.environ["GLOWUP_DIAG_DSN"] = original
            else:
                os.environ.pop("GLOWUP_DIAG_DSN", None)


class TestDiagnosticsUnavailable(unittest.TestCase):
    """Tests for graceful degradation without psycopg2."""

    def test_from_env_without_psycopg2(self) -> None:
        """from_env should return None when psycopg2 is missing."""
        import diagnostics
        original = diagnostics._HAS_PSYCOPG2
        try:
            diagnostics._HAS_PSYCOPG2 = False
            result = DiagnosticsLogger.from_env()
            self.assertIsNone(result)
        finally:
            diagnostics._HAS_PSYCOPG2 = original


if __name__ == "__main__":
    unittest.main()
