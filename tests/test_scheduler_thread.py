"""Tests for SchedulerThread — thread lifecycle, deadlock, dispatch.

The pure evaluation logic is tested in test_evaluator.py.
These tests cover the thread infrastructure: construction, locking,
dispatch, error handling, and lifecycle.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.0"

import threading
import unittest
from datetime import date, datetime, timezone
from typing import Any, Optional
from unittest.mock import patch

from scheduling.scheduler_thread import SchedulerThread


# ---------------------------------------------------------------------------
# Fake DeviceManager — records calls, has a real lock
# ---------------------------------------------------------------------------

class FakeDeviceManager:
    """Mock DeviceManager with real lock for deadlock tests."""

    def __init__(
        self, groups: dict[str, list[str]] | None = None,
    ) -> None:
        self._lock: threading.RLock = threading.RLock()
        self._group_config: dict[str, list[str]] = groups or {}
        self._overrides: dict[str, Optional[str]] = {}
        self.play_calls: list[tuple[str, str, dict]] = []
        self.stop_calls: list[str] = []
        self.clear_calls: list[str] = []

    def play(
        self, device_id: str, effect: str, params: dict,
        source: str = "", entry: str = "", **kw: Any,
    ) -> None:
        """Record play call. Acquires lock like the real DM."""
        with self._lock:
            self.play_calls.append((device_id, effect, params))

    def stop(self, device_id: str) -> None:
        """Record stop call. Acquires lock like the real DM."""
        with self._lock:
            self.stop_calls.append(device_id)

    def clear_override(self, device_id: str) -> None:
        """Record clear_override call."""
        self.clear_calls.append(device_id)
        self._overrides.pop(device_id, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(
    schedule: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build minimal config."""
    return {
        "location": {"latitude": 30.6954, "longitude": -88.0399},
        "schedule": schedule or [],
    }


def _entry(
    name: str, group: str,
    effect: str = "aurora", params: dict | None = None,
) -> dict[str, Any]:
    """Build a schedule entry that's always active."""
    return {
        "name": name, "group": group,
        "start": "00:00", "stop": "23:59",
        "effect": effect, "params": params or {},
        "enabled": True, "days": "",
    }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction(unittest.TestCase):
    """SchedulerThread constructs without touching hardware."""

    def test_basic(self) -> None:
        """Construct with minimal config."""
        dm = FakeDeviceManager(groups={"g": ["192.0.2.1"]})
        sched = SchedulerThread(_config(schedule=[_entry("X", "g")]), dm)
        self.assertTrue(sched.daemon)
        self.assertEqual(sched.name, "scheduler")

    def test_empty_schedule(self) -> None:
        """Empty schedule constructs without error."""
        dm = FakeDeviceManager()
        sched = SchedulerThread(_config(), dm)
        self.assertIsNotNone(sched)


# ---------------------------------------------------------------------------
# No deadlock — THE critical test
# ---------------------------------------------------------------------------

class TestNoDeadlock(unittest.TestCase):
    """_tick must NOT hold DM lock during play/stop calls."""

    def test_no_deadlock_with_non_reentrant_lock(self) -> None:
        """Use non-reentrant Lock — deadlocks if held during play().

        This test catches the exact bug that killed scheduling:
        holding _dm._lock during the entire _tick, then calling
        _dm.play() which also acquires the lock.
        """
        dm = FakeDeviceManager(groups={"porch": ["192.0.2.42"]})
        sched = SchedulerThread(
            _config(schedule=[_entry("Night", "porch")]), dm,
        )
        sched._state = {"porch": None}
        sched._last_logged_date = date.today()

        # Replace RLock with non-reentrant Lock.
        dm._lock = threading.Lock()

        result: list[bool] = []

        def run_tick() -> None:
            try:
                sched._tick()
                result.append(True)
            except Exception:
                result.append(False)

        t = threading.Thread(target=run_tick)
        t.start()
        t.join(timeout=5)

        if t.is_alive():
            self.fail(
                "DEADLOCK: _tick held DM lock during play/stop. "
                "Thread did not complete within 5 seconds."
            )
        self.assertTrue(result[0])
        self.assertEqual(len(dm.play_calls), 1)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

class TestDispatch(unittest.TestCase):
    """_tick dispatches evaluator actions to DeviceManager."""

    def test_first_tick_starts_effects(self) -> None:
        """First tick starts active effects via DM.play()."""
        dm = FakeDeviceManager(groups={
            "porch": ["192.0.2.42"],
            "gen": ["192.0.2.164", "192.0.2.180"],
        })
        sched = SchedulerThread(
            _config(schedule=[
                _entry("Porch", "porch"),
                _entry("Gen", "gen", effect="on", params={"brightness": 30}),
            ]),
            dm,
        )
        sched._state = {"porch": None, "gen": None}
        sched._last_logged_date = date.today()

        sched._tick()

        self.assertEqual(len(dm.play_calls), 2)
        effects = {c[1] for c in dm.play_calls}
        self.assertEqual(effects, {"aurora", "on"})

    def test_params_arrive_at_dm(self) -> None:
        """brightness=30 reaches DM.play() exactly."""
        dm = FakeDeviceManager(groups={"gen": ["192.0.2.164"]})
        sched = SchedulerThread(
            _config(schedule=[
                _entry("Gen", "gen", effect="on", params={"brightness": 30}),
            ]),
            dm,
        )
        sched._state = {"gen": None}
        sched._last_logged_date = date.today()

        sched._tick()

        self.assertEqual(dm.play_calls[0][2]["brightness"], 30)

    def test_clear_override_dispatched(self) -> None:
        """clear_override action calls DM.clear_override()."""
        dm = FakeDeviceManager(groups={"porch": ["192.0.2.42"]})
        dm._overrides = {"192.0.2.42": "Evening"}
        sched = SchedulerThread(
            _config(schedule=[_entry("Overnight", "porch")]),
            dm,
        )
        sched._state = {"porch": "Evening"}
        sched._last_logged_date = date.today()

        sched._tick()

        self.assertIn("192.0.2.42", dm.clear_calls)

    def test_dispatch_error_does_not_kill_tick(self) -> None:
        """play() exception is caught — other groups still process."""
        dm = FakeDeviceManager(groups={
            "a": ["192.0.2.1"],
            "b": ["192.0.2.2"],
        })
        sched = SchedulerThread(
            _config(schedule=[_entry("A", "a"), _entry("B", "b")]),
            dm,
        )
        sched._state = {"a": None, "b": None}
        sched._last_logged_date = date.today()

        call_count = [0]
        orig_play = dm.play

        def failing_play(*args: Any, **kwargs: Any) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("boom")
            orig_play(*args, **kwargs)

        dm.play = failing_play

        # Should not raise.
        sched._tick()
        # Second group should have succeeded.
        self.assertEqual(len(dm.play_calls), 1)


# ---------------------------------------------------------------------------
# Run loop lifecycle
# ---------------------------------------------------------------------------

class TestRunLoop(unittest.TestCase):
    """Thread lifecycle: empty schedule exits, stop event exits."""

    def test_empty_schedule_exits(self) -> None:
        """Empty schedule → run() returns immediately."""
        dm = FakeDeviceManager(groups={"g": ["192.0.2.1"]})
        sched = SchedulerThread(_config(), dm)
        t = threading.Thread(target=sched.run)
        t.start()
        t.join(timeout=3)
        self.assertFalse(t.is_alive())

    def test_stop_event_exits(self) -> None:
        """Pre-set stop event → run() exits after one tick."""
        dm = FakeDeviceManager(groups={"g": ["192.0.2.1"]})
        sched = SchedulerThread(
            _config(schedule=[_entry("X", "g")]), dm,
        )
        sched._stop_event.set()
        t = threading.Thread(target=sched.run)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())

    @patch("scheduling.scheduler_thread.SCHEDULER_POLL_SECONDS", 0.1)
    def test_tick_exception_continues_loop(self) -> None:
        """Exception in _tick is caught — loop continues."""
        dm = FakeDeviceManager(groups={"g": ["192.0.2.1"]})
        sched = SchedulerThread(
            _config(schedule=[_entry("X", "g")]), dm,
        )
        tick_count = [0]

        def counting_tick() -> None:
            tick_count[0] += 1
            if tick_count[0] == 1:
                raise RuntimeError("first tick fails")
            sched._stop_event.set()

        sched._tick = counting_tick
        t = threading.Thread(target=sched.run)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertGreaterEqual(tick_count[0], 2)


if __name__ == "__main__":
    unittest.main()
