"""Tests for SchedulerThread — the bugs that burned us 2026-04-02.

Every test here corresponds to a real production failure:
- Missing imports (timezone, date, _group_id_from_name, _log_sun_times)
- Deadlock (holding DeviceManager lock during play/stop calls)
- Empty group crash (IndexError on ips[0])
- First-tick transition (prev=None → active=entry must start the effect)
- Schedule entry matching (correct entry for the current time window)
- Override preservation (phone override survives schedule transitions)
- Crashed effect restart (effect stopped running, scheduler restarts it)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import threading
import time
import unittest
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Optional
from unittest.mock import MagicMock, patch, PropertyMock

from scheduling.scheduler_thread import SchedulerThread, _log_sun_times


# ---------------------------------------------------------------------------
# Fake DeviceManager that records calls instead of touching hardware
# ---------------------------------------------------------------------------

class FakeDeviceManager:
    """Mock DeviceManager that tracks play/stop calls.

    Has a real threading.Lock so deadlock tests are meaningful.
    """

    def __init__(
        self, groups: dict[str, list[str]] | None = None,
    ) -> None:
        self._lock: threading.RLock = threading.RLock()
        self._group_config: dict[str, list[str]] = groups or {}
        self.play_calls: list[tuple[str, str, dict]] = []
        self.stop_calls: list[str] = []
        self._overrides: dict[str, str] = {}
        self._running: dict[str, bool] = {}

    def play(
        self, device_id: str, effect: str, params: dict,
        bindings: Any = None, signal_bus: Any = None,
        source: str = "", entry: str = "",
    ) -> None:
        """Record a play call. Acquires lock like the real one."""
        with self._lock:
            self.play_calls.append((device_id, effect, params))
            self._running[device_id] = True

    def stop(self, device_id: str) -> None:
        """Record a stop call. Acquires lock like the real one."""
        with self._lock:
            self.stop_calls.append(device_id)
            self._running[device_id] = False

    def is_overridden(self, device_id: str) -> bool:
        return device_id in self._overrides

    def get_override_entry(self, device_id: str) -> Optional[str]:
        return self._overrides.get(device_id)

    def clear_override(self, device_id: str) -> None:
        self._overrides.pop(device_id, None)

    def is_overridden_or_member(self, device_id: str) -> bool:
        return device_id in self._overrides

    def get_or_create_controller(self, device_id: str) -> Any:
        return FakeController(self._running.get(device_id, False))

    def get_controller(self, device_id: str) -> Any:
        return self.get_or_create_controller(device_id)


class FakeController:
    """Fake Controller that reports running status."""

    def __init__(self, running: bool = False) -> None:
        self._running: bool = running

    def get_status(self) -> dict[str, Any]:
        return {"running": self._running}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    schedule: list[dict[str, Any]] | None = None,
    lat: float = 30.6954,
    lon: float = -88.0399,
) -> dict[str, Any]:
    """Build a minimal config dict for SchedulerThread."""
    return {
        "location": {"latitude": lat, "longitude": lon},
        "schedule": schedule or [],
    }


def _make_entry(
    name: str,
    group: str,
    start: str = "00:00",
    stop: str = "23:59",
    effect: str = "aurora",
    params: dict | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Build a schedule entry dict."""
    return {
        "name": name,
        "group": group,
        "start": start,
        "stop": stop,
        "effect": effect,
        "params": params or {},
        "enabled": enabled,
    }


# ---------------------------------------------------------------------------
# Import sanity — the bugs that crashed us
# ---------------------------------------------------------------------------

class TestImportSanity(unittest.TestCase):
    """Verify all symbols used by SchedulerThread are importable.

    These tests exist because the module crashed on startup with
    NameError for timezone, date, _group_id_from_name, and
    _log_sun_times. If any import breaks again, these fail immediately.
    """

    def test_datetime_timezone_exists(self) -> None:
        """timezone.utc is used on line 144 — was missing."""
        from scheduling.scheduler_thread import timezone
        self.assertIsNotNone(timezone.utc)

    def test_datetime_date_exists(self) -> None:
        """date is used on line 145 — was missing."""
        from scheduling.scheduler_thread import date
        self.assertIsNotNone(date.today())

    def test_group_id_from_name_callable(self) -> None:
        """_group_id_from_name is used on line 189 — was missing."""
        from scheduling.scheduler_thread import _group_id_from_name
        result = _group_id_from_name("Test Group")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("group:"))

    def test_log_sun_times_callable(self) -> None:
        """_log_sun_times is used on line 169 — was missing."""
        from solar import SunTimes
        sun = SunTimes(
            dawn=datetime(2026, 4, 2, 6, 16),
            sunrise=datetime(2026, 4, 2, 6, 40),
            noon=datetime(2026, 4, 2, 12, 55),
            sunset=datetime(2026, 4, 2, 19, 11),
            dusk=datetime(2026, 4, 2, 19, 35),
        )
        # Should not raise.
        _log_sun_times(sun, date(2026, 4, 2))

    def test_controller_importable(self) -> None:
        """Controller is used on line 288 — was missing."""
        from engine import Controller
        self.assertIsNotNone(Controller)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction(unittest.TestCase):
    """SchedulerThread must construct without touching hardware."""

    def test_basic_construction(self) -> None:
        """Construct with minimal config and fake DM."""
        config = _make_config(schedule=[_make_entry("test", "group1")])
        dm = FakeDeviceManager(groups={"group1": ["10.0.0.1"]})
        sched = SchedulerThread(config, dm)
        self.assertIsNotNone(sched)
        self.assertTrue(sched.daemon)

    def test_no_schedule_entries(self) -> None:
        """Empty schedule should not crash."""
        config = _make_config(schedule=[])
        dm = FakeDeviceManager()
        sched = SchedulerThread(config, dm)
        self.assertIsNotNone(sched)


# ---------------------------------------------------------------------------
# _tick behavior — the core scheduling logic
# ---------------------------------------------------------------------------

class TestTick(unittest.TestCase):
    """Tests for _tick() — the per-cycle evaluation."""

    def _make_scheduler(
        self,
        groups: dict[str, list[str]],
        schedule: list[dict[str, Any]],
    ) -> tuple[SchedulerThread, FakeDeviceManager]:
        """Build a scheduler ready for _tick() calls."""
        config = _make_config(schedule=schedule)
        dm = FakeDeviceManager(groups=groups)
        sched = SchedulerThread(config, dm)
        # Initialize state that run() would set.
        sched._last_logged_date = date.today()
        for g in groups:
            sched._group_entries[g] = None
        return sched, dm

    def test_first_tick_starts_active_effect(self) -> None:
        """First tick transitions from None → active entry.

        This was the core failure: the scheduler initialized
        prev_name=None, found an active entry, but never called play().
        """
        entry = _make_entry("Night", "porch", start="00:00", stop="23:59")
        sched, dm = self._make_scheduler(
            groups={"porch": ["10.0.0.42"]},
            schedule=[entry],
        )
        sched._tick(30.69, -88.04, [entry])
        self.assertEqual(len(dm.play_calls), 1)
        self.assertEqual(dm.play_calls[0][0], "10.0.0.42")
        self.assertEqual(dm.play_calls[0][1], "aurora")

    def test_empty_group_skipped(self) -> None:
        """Groups with zero resolved IPs must not crash.

        This was IndexError: list index out of range on ips[0].
        """
        entry = _make_entry("Night", "bedroom", start="00:00", stop="23:59")
        sched, dm = self._make_scheduler(
            groups={"bedroom": []},
            schedule=[entry],
        )
        # Must not raise.
        sched._tick(30.69, -88.04, [entry])
        self.assertEqual(len(dm.play_calls), 0)

    def test_no_deadlock_on_play(self) -> None:
        """_tick must not hold DM lock when calling play().

        This was the deadlock: _tick held self._dm._lock for the
        entire cycle, then called self._dm.play() which acquires
        the same lock. With a non-reentrant Lock, this deadlocks.
        With RLock, it works but is still wrong design.

        We verify by using a non-reentrant Lock. If _tick holds it
        during play(), the play() call will deadlock and the test
        times out.
        """
        entry = _make_entry("Night", "porch", start="00:00", stop="23:59")
        sched, dm = self._make_scheduler(
            groups={"porch": ["10.0.0.42"]},
            schedule=[entry],
        )
        # Replace RLock with a non-reentrant Lock.
        dm._lock = threading.Lock()

        # Run _tick in a thread with a timeout.
        result: list[bool] = []

        def run_tick() -> None:
            try:
                sched._tick(30.69, -88.04, [entry])
                result.append(True)
            except Exception:
                result.append(False)

        t = threading.Thread(target=run_tick)
        t.start()
        t.join(timeout=5)

        if t.is_alive():
            self.fail(
                "DEADLOCK: _tick held DM lock during play() call. "
                "Thread did not complete within 5 seconds."
            )
        self.assertTrue(result[0])

    def test_multi_device_group_uses_group_id(self) -> None:
        """Groups with 2+ IPs use group:Name as device_id."""
        entry = _make_entry(
            "Whites", "porch", start="00:00", stop="23:59", effect="on",
        )
        sched, dm = self._make_scheduler(
            groups={"porch": ["10.0.0.42", "10.0.0.43"]},
            schedule=[entry],
        )
        sched._tick(30.69, -88.04, [entry])
        self.assertEqual(len(dm.play_calls), 1)
        self.assertTrue(dm.play_calls[0][0].startswith("group:"))

    def test_single_device_group_uses_ip(self) -> None:
        """Groups with 1 IP use that IP as device_id."""
        entry = _make_entry(
            "String", "porch", start="00:00", stop="23:59",
        )
        sched, dm = self._make_scheduler(
            groups={"porch": ["10.0.0.213"]},
            schedule=[entry],
        )
        sched._tick(30.69, -88.04, [entry])
        self.assertEqual(dm.play_calls[0][0], "10.0.0.213")

    def test_no_active_entry_logs_idle(self) -> None:
        """Groups with no matching schedule entry get 'idle' transition."""
        # Entry is for group "kitchen" but the only group is "bedroom".
        entry = _make_entry("Night", "kitchen", start="00:00", stop="23:59")
        sched, dm = self._make_scheduler(
            groups={"bedroom": ["10.0.0.1"]},
            schedule=[entry],
        )
        sched._tick(30.69, -88.04, [entry])
        # No play calls — "bedroom" has no matching entry.
        self.assertEqual(len(dm.play_calls), 0)

    def test_same_entry_no_retrigger(self) -> None:
        """Second tick with same active entry does not re-play."""
        entry = _make_entry("Night", "porch", start="00:00", stop="23:59")
        sched, dm = self._make_scheduler(
            groups={"porch": ["10.0.0.42"]},
            schedule=[entry],
        )
        sched._tick(30.69, -88.04, [entry])
        self.assertEqual(len(dm.play_calls), 1)
        # Second tick — same entry still active, no transition.
        sched._tick(30.69, -88.04, [entry])
        self.assertEqual(len(dm.play_calls), 1)  # Still 1, not 2.

    def test_transition_stops_previous_starts_new(self) -> None:
        """Schedule transition stops old effect, starts new one.

        _tick re-reads specs from self._config, so we mutate the
        config's schedule list to simulate a real transition.
        """
        entry_a = _make_entry(
            "Evening", "porch", start="00:00", stop="23:59", effect="fireworks",
        )
        entry_b = _make_entry(
            "Overnight", "porch", start="00:00", stop="23:59", effect="aurora",
        )
        sched, dm = self._make_scheduler(
            groups={"porch": ["10.0.0.42"]},
            schedule=[entry_a],
        )
        # First tick — starts Evening.
        sched._tick(30.69, -88.04, [entry_a])
        self.assertEqual(len(dm.play_calls), 1)
        self.assertEqual(dm.play_calls[0][1], "fireworks")

        # Mutate the config's schedule to entry_b so _tick reads it.
        sched._config["schedule"] = [entry_b]
        sched._tick(30.69, -88.04, [entry_b])

        # Should have stopped Evening and started Overnight.
        self.assertIn("10.0.0.42", dm.stop_calls)
        self.assertEqual(dm.play_calls[-1][1], "aurora")

    def test_effect_params_passed_correctly(self) -> None:
        """Effect params from schedule entry reach play() exactly.

        Generator Lighting at brightness=30 must NOT arrive as 100.
        This test exists because scheduled effects were ignoring params.
        """
        entry = _make_entry(
            "Generator Lighting", "gen",
            start="00:00", stop="23:59",
            effect="on",
            params={"brightness": 30},
        )
        sched, dm = self._make_scheduler(
            groups={"gen": ["10.0.0.164", "10.0.0.180"]},
            schedule=[entry],
        )
        sched._tick(30.69, -88.04, [entry])
        self.assertEqual(len(dm.play_calls), 1)
        played_params = dm.play_calls[0][2]
        self.assertEqual(played_params["brightness"], 30)

    def test_multiple_groups_all_start(self) -> None:
        """Multiple groups with active entries all get started."""
        entries = [
            _make_entry("A", "g1", effect="aurora", params={"brightness": 50}),
            _make_entry("B", "g2", effect="on", params={"brightness": 30}),
            _make_entry("C", "g3", effect="fireworks", params={"speed": 10}),
        ]
        sched, dm = self._make_scheduler(
            groups={
                "g1": ["10.0.0.1"],
                "g2": ["10.0.0.2", "10.0.0.3"],
                "g3": ["10.0.0.4"],
            },
            schedule=entries,
        )
        sched._tick(30.69, -88.04, entries)
        self.assertEqual(len(dm.play_calls), 3)
        effects = {c[1] for c in dm.play_calls}
        self.assertEqual(effects, {"aurora", "on", "fireworks"})
        # Verify each group got its own params.
        for dev_id, effect, params in dm.play_calls:
            if effect == "on":
                self.assertEqual(params["brightness"], 30)
            elif effect == "aurora":
                self.assertEqual(params["brightness"], 50)
            elif effect == "fireworks":
                self.assertEqual(params["speed"], 10)


class TestOverrides(unittest.TestCase):
    """Override (phone control) interaction with scheduler."""

    def _make_scheduler(
        self,
        groups: dict[str, list[str]],
        schedule: list[dict[str, Any]],
    ) -> tuple[SchedulerThread, FakeDeviceManager]:
        config = _make_config(schedule=schedule)
        dm = FakeDeviceManager(groups=groups)
        sched = SchedulerThread(config, dm)
        sched._last_logged_date = date.today()
        for g in groups:
            sched._group_entries[g] = None
        return sched, dm

    def test_overridden_device_not_started(self) -> None:
        """Scheduler skips devices with active phone override."""
        entry = _make_entry("Night", "porch", start="00:00", stop="23:59")
        sched, dm = self._make_scheduler(
            groups={"porch": ["10.0.0.42"]},
            schedule=[entry],
        )
        dm._overrides["10.0.0.42"] = "some_entry"
        sched._tick(30.69, -88.04, [entry])
        # Override active — scheduler must not call play.
        self.assertEqual(len(dm.play_calls), 0)


class TestTickResilience(unittest.TestCase):
    """_tick must survive individual errors without dying."""

    def test_play_exception_caught_per_group(self) -> None:
        """play() exception for one group doesn't prevent other groups.

        The try/except around play() in _tick catches per-group errors
        and logs a warning. Other groups still get processed.
        """
        entry_a = _make_entry("A", "groupA", start="00:00", stop="23:59")
        entry_b = _make_entry("B", "groupB", start="00:00", stop="23:59")
        config = _make_config(schedule=[entry_a, entry_b])
        dm = FakeDeviceManager(groups={
            "groupA": ["10.0.0.1"],
            "groupB": ["10.0.0.2"],
        })
        sched = SchedulerThread(config, dm)
        sched._last_logged_date = date.today()
        for g in dm._group_config:
            sched._group_entries[g] = None

        # Make play raise on the first call only.
        call_count = [0]
        orig_play = dm.play

        def failing_play(*args: Any, **kwargs: Any) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("simulated play failure")
            orig_play(*args, **kwargs)

        dm.play = failing_play

        # Should not raise — the per-group try/except catches it.
        sched._tick(30.69, -88.04, [entry_a, entry_b])

        # Second group should still have been started despite first failing.
        self.assertEqual(len(dm.play_calls), 1)  # Only groupB succeeded.


class TestRunLoop(unittest.TestCase):
    """Tests for the run() loop control flow."""

    def test_empty_schedule_returns_immediately(self) -> None:
        """Scheduler with no entries exits run() without looping."""
        config = _make_config(schedule=[])
        dm = FakeDeviceManager(groups={"test": ["10.0.0.1"]})
        sched = SchedulerThread(config, dm)

        # run() should return immediately for empty schedule.
        t = threading.Thread(target=sched.run)
        t.start()
        t.join(timeout=3)
        self.assertFalse(t.is_alive(), "run() should exit for empty schedule")

    def test_stop_event_exits_loop(self) -> None:
        """Setting stop event causes run() to exit."""
        entry = _make_entry("Night", "porch", start="00:00", stop="23:59")
        config = _make_config(schedule=[entry])
        dm = FakeDeviceManager(groups={"porch": ["10.0.0.42"]})
        sched = SchedulerThread(config, dm)

        # Pre-set stop before starting.
        sched._stop_event.set()
        t = threading.Thread(target=sched.run)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "run() should exit when stop is set")

    @patch("scheduling.scheduler_thread.SCHEDULER_POLL_SECONDS", 0.1)
    def test_tick_exception_does_not_kill_loop(self) -> None:
        """Exception in _tick is caught — loop continues."""
        entry = _make_entry("Night", "porch", start="00:00", stop="23:59")
        config = _make_config(schedule=[entry])
        dm = FakeDeviceManager(groups={"porch": ["10.0.0.42"]})
        sched = SchedulerThread(config, dm)

        tick_count = [0]

        def counting_tick(*args: Any, **kw: Any) -> None:
            tick_count[0] += 1
            if tick_count[0] == 1:
                raise RuntimeError("first tick fails")
            # Stop after second tick.
            sched._stop_event.set()

        sched._tick = counting_tick

        t = threading.Thread(target=sched.run)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "run() should exit after stop_event")
        # Must have ticked at least twice — first failed, second stopped.
        self.assertGreaterEqual(tick_count[0], 2)


if __name__ == "__main__":
    unittest.main()
