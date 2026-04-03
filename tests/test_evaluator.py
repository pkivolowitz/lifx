"""Tests for the pure schedule evaluator — every production failure as a test.

Zero mocks.  Zero threads.  Zero hardware.  Pass data in, check actions out.
Each test corresponds to a real bug or a real scenario that burned us.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import unittest
from datetime import datetime, timezone
from typing import Any, Optional

from scheduling.evaluator import ScheduleAction, evaluate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_at(hour: int, minute: int = 0) -> datetime:
    """Create a timezone-aware datetime at the given hour today."""
    return datetime(2026, 4, 2, hour, minute, tzinfo=timezone.utc)


def _entry(
    name: str,
    group: str,
    start: str = "00:00",
    stop: str = "23:59",
    effect: str = "aurora",
    params: dict | None = None,
    enabled: bool = True,
    days: str = "",
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
        "days": days,
    }


# Mobile, AL coordinates — used for all tests.
_LAT: float = 30.6954
_LON: float = -88.0399


def _actions_by_type(
    actions: list[ScheduleAction], action_type: str,
) -> list[ScheduleAction]:
    """Filter actions by type."""
    return [a for a in actions if a.action == action_type]


# ---------------------------------------------------------------------------
# First tick — the bug that killed scheduling on Daedalus
# ---------------------------------------------------------------------------

class TestFirstTick(unittest.TestCase):
    """First tick transitions from None → active entry."""

    def test_single_group_starts(self) -> None:
        """One group with one active entry → one start action."""
        actions, state = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[_entry("Night", "porch")],
            prev_state={"porch": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(22),
        )
        starts = _actions_by_type(actions, "start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].device_id, "10.0.0.42")
        self.assertEqual(starts[0].effect, "aurora")
        self.assertEqual(starts[0].entry_name, "Night")

    def test_multiple_groups_all_start(self) -> None:
        """Multiple groups with active entries → all start."""
        actions, state = evaluate(
            groups={
                "porch": ["10.0.0.42"],
                "gen": ["10.0.0.164", "10.0.0.180"],
                "landing": ["10.0.0.120"],
            },
            schedule=[
                _entry("Porch Night", "porch", effect="aurora"),
                _entry("Gen Light", "gen", effect="on", params={"brightness": 30}),
                _entry("Landing", "landing", effect="aurora"),
            ],
            prev_state={"porch": None, "gen": None, "landing": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(22),
        )
        starts = _actions_by_type(actions, "start")
        self.assertEqual(len(starts), 3)

    def test_new_group_auto_tracked(self) -> None:
        """Group not in prev_state gets added to new_state."""
        actions, state = evaluate(
            groups={"new_group": ["10.0.0.99"]},
            schedule=[_entry("Test", "new_group")],
            prev_state={},  # Empty — no prior state.
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(22),
        )
        self.assertIn("new_group", state)
        starts = _actions_by_type(actions, "start")
        self.assertEqual(len(starts), 1)


# ---------------------------------------------------------------------------
# Params passthrough — the Generator brightness=30 → 100 bug
# ---------------------------------------------------------------------------

class TestParamsPassthrough(unittest.TestCase):
    """Effect params from schedule entries must reach actions exactly."""

    def test_brightness_30(self) -> None:
        """brightness=30 in schedule → brightness=30 in action."""
        actions, _ = evaluate(
            groups={"gen": ["10.0.0.164", "10.0.0.180"]},
            schedule=[_entry("Gen", "gen", effect="on", params={"brightness": 30})],
            prev_state={"gen": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(22),
        )
        starts = _actions_by_type(actions, "start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].params["brightness"], 30)

    def test_complex_params(self) -> None:
        """Multiple params preserved."""
        actions, _ = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[_entry("Evening", "porch", effect="fireworks",
                            params={"speed": 10, "brightness": 100})],
            prev_state={"porch": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(22),
        )
        starts = _actions_by_type(actions, "start")
        self.assertEqual(starts[0].params["speed"], 10)
        self.assertEqual(starts[0].params["brightness"], 100)

    def test_empty_params(self) -> None:
        """No params → empty dict, not None."""
        actions, _ = evaluate(
            groups={"x": ["10.0.0.1"]},
            schedule=[_entry("X", "x", params=None)],
            prev_state={"x": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(12),
        )
        starts = _actions_by_type(actions, "start")
        self.assertIsInstance(starts[0].params, dict)


# ---------------------------------------------------------------------------
# Empty groups — the IndexError crash
# ---------------------------------------------------------------------------

class TestEmptyGroups(unittest.TestCase):
    """Groups with zero IPs must be skipped, not crash."""

    def test_empty_group_no_crash(self) -> None:
        """Empty group produces no actions and no exception."""
        actions, state = evaluate(
            groups={"bedroom": []},
            schedule=[_entry("Night", "bedroom")],
            prev_state={"bedroom": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(22),
        )
        self.assertEqual(len(actions), 0)

    def test_mixed_empty_and_populated(self) -> None:
        """Empty group skipped, populated group starts."""
        actions, _ = evaluate(
            groups={"empty": [], "ok": ["10.0.0.1"]},
            schedule=[
                _entry("A", "empty"),
                _entry("B", "ok"),
            ],
            prev_state={"empty": None, "ok": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(12),
        )
        starts = _actions_by_type(actions, "start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].group, "ok")


# ---------------------------------------------------------------------------
# Device ID resolution — single IP vs group:Name
# ---------------------------------------------------------------------------

class TestDeviceId(unittest.TestCase):
    """Single-IP groups use IP, multi-IP groups use group:Name."""

    def test_single_ip_uses_ip(self) -> None:
        """1-device group → device_id is the IP."""
        actions, _ = evaluate(
            groups={"porch": ["10.0.0.213"]},
            schedule=[_entry("Night", "porch")],
            prev_state={"porch": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(22),
        )
        self.assertEqual(actions[0].device_id, "10.0.0.213")

    def test_multi_ip_uses_group(self) -> None:
        """2+ device group → device_id is group:Name."""
        actions, _ = evaluate(
            groups={"whites": ["10.0.0.124", "10.0.0.147"]},
            schedule=[_entry("On", "whites")],
            prev_state={"whites": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(22),
        )
        self.assertTrue(actions[0].device_id.startswith("group:"))


# ---------------------------------------------------------------------------
# Steady state — no retrigger
# ---------------------------------------------------------------------------

class TestSteadyState(unittest.TestCase):
    """Same entry active → no actions."""

    def test_no_retrigger(self) -> None:
        """Second tick with same entry → zero actions."""
        actions, _ = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[_entry("Night", "porch")],
            prev_state={"porch": "Night"},  # Already running.
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(22),
        )
        self.assertEqual(len(actions), 0)


# ---------------------------------------------------------------------------
# Transitions — stop old, start new
# ---------------------------------------------------------------------------

class TestTransitions(unittest.TestCase):
    """Schedule transitions stop old effects and start new ones."""

    def test_entry_to_entry(self) -> None:
        """Entry A → Entry B: stop A, start B."""
        actions, state = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[_entry("Overnight", "porch", effect="aurora")],
            prev_state={"porch": "Evening"},  # Different entry was active.
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(23, 30),
        )
        stops = _actions_by_type(actions, "stop")
        starts = _actions_by_type(actions, "start")
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].entry_name, "Evening")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].effect, "aurora")
        self.assertEqual(state["porch"], "Overnight")

    def test_entry_to_idle(self) -> None:
        """Entry A → no active entry: stop A."""
        actions, state = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[_entry("Night", "porch", start="20:00", stop="22:00")],
            prev_state={"porch": "Night"},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(23),  # Outside 20:00-22:00 window.
        )
        stops = _actions_by_type(actions, "stop")
        self.assertEqual(len(stops), 1)
        starts = _actions_by_type(actions, "start")
        self.assertEqual(len(starts), 0)
        self.assertIsNone(state["porch"])

    def test_idle_stays_idle(self) -> None:
        """No entry active, was idle → zero actions."""
        actions, _ = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[_entry("Day", "porch", start="08:00", stop="12:00")],
            prev_state={"porch": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(23),  # Outside window, prev was None.
        )
        self.assertEqual(len(actions), 0)


# ---------------------------------------------------------------------------
# Overrides — phone control interaction
# ---------------------------------------------------------------------------

class TestOverrides(unittest.TestCase):
    """Phone overrides prevent scheduler from starting effects."""

    def test_overridden_device_not_started(self) -> None:
        """Overridden device → no start action on transition."""
        actions, _ = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[_entry("Night", "porch")],
            prev_state={"porch": None},
            overrides={"10.0.0.42": "some_entry"},
            lat=_LAT, lon=_LON,
            now=_now_at(22),
        )
        starts = _actions_by_type(actions, "start")
        self.assertEqual(len(starts), 0)

    def test_overridden_group_member_blocks_group(self) -> None:
        """Override on one group member → blocks entire group."""
        actions, _ = evaluate(
            groups={"whites": ["10.0.0.124", "10.0.0.147"]},
            schedule=[_entry("On", "whites")],
            prev_state={"whites": None},
            overrides={"10.0.0.124": "something"},  # One member overridden.
            lat=_LAT, lon=_LON,
            now=_now_at(22),
        )
        starts = _actions_by_type(actions, "start")
        self.assertEqual(len(starts), 0)

    def test_override_cleared_on_matching_transition(self) -> None:
        """Override matching outgoing entry → clear_override action."""
        actions, _ = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[],  # No active entry (transition to idle).
            prev_state={"porch": "Evening"},
            overrides={"10.0.0.42": "Evening"},  # Matches outgoing.
            lat=_LAT, lon=_LON,
            now=_now_at(23),
        )
        clears = _actions_by_type(actions, "clear_override")
        self.assertEqual(len(clears), 1)
        self.assertEqual(clears[0].device_id, "10.0.0.42")

    def test_override_preserved_on_mismatched_transition(self) -> None:
        """Override for different entry → NOT cleared."""
        actions, _ = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[],
            prev_state={"porch": "Evening"},
            overrides={"10.0.0.42": "OTHER_ENTRY"},  # Doesn't match.
            lat=_LAT, lon=_LON,
            now=_now_at(23),
        )
        clears = _actions_by_type(actions, "clear_override")
        self.assertEqual(len(clears), 0)

    def test_after_clear_override_effect_starts(self) -> None:
        """Clearing override on transition allows new entry to start."""
        actions, _ = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[_entry("Overnight", "porch", effect="aurora")],
            prev_state={"porch": "Evening"},
            overrides={"10.0.0.42": "Evening"},  # Will be cleared.
            lat=_LAT, lon=_LON,
            now=_now_at(23, 30),
        )
        clears = _actions_by_type(actions, "clear_override")
        starts = _actions_by_type(actions, "start")
        self.assertEqual(len(clears), 1)
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].effect, "aurora")


# ---------------------------------------------------------------------------
# Disabled entries
# ---------------------------------------------------------------------------

class TestDisabledEntries(unittest.TestCase):
    """Disabled entries are ignored."""

    def test_disabled_not_started(self) -> None:
        """enabled=false → no start action."""
        actions, _ = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[_entry("Night", "porch", enabled=False)],
            prev_state={"porch": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(22),
        )
        starts = _actions_by_type(actions, "start")
        self.assertEqual(len(starts), 0)


# ---------------------------------------------------------------------------
# Day filtering
# ---------------------------------------------------------------------------

class TestDayFiltering(unittest.TestCase):
    """Day-of-week filtering via MTWRFSU."""

    def test_wrong_day_skipped(self) -> None:
        """Entry for Monday only, tested on Wednesday → skip."""
        # 2026-04-02 is a Thursday (weekday=3 → R).
        actions, _ = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[_entry("Weekday", "porch", days="M")],
            prev_state={"porch": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(12),
        )
        starts = _actions_by_type(actions, "start")
        self.assertEqual(len(starts), 0)

    def test_correct_day_starts(self) -> None:
        """Entry for Thursday (R), tested on Thursday → start."""
        actions, _ = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[_entry("Thursday", "porch", days="R")],
            prev_state={"porch": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(12),
        )
        starts = _actions_by_type(actions, "start")
        self.assertEqual(len(starts), 1)


# ---------------------------------------------------------------------------
# No matching group — schedule entry for nonexistent group
# ---------------------------------------------------------------------------

class TestUnmatchedGroups(unittest.TestCase):
    """Groups without matching schedule entries stay idle."""

    def test_no_entry_for_group(self) -> None:
        """Group exists but no schedule entry targets it → no action."""
        actions, _ = evaluate(
            groups={"bedroom": ["10.0.0.1"]},
            schedule=[_entry("Kitchen", "kitchen")],  # Wrong group.
            prev_state={"bedroom": None},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(12),
        )
        self.assertEqual(len(actions), 0)


# ---------------------------------------------------------------------------
# State cleanup — stale groups removed
# ---------------------------------------------------------------------------

class TestStateCleanup(unittest.TestCase):
    """Stale groups are removed from state."""

    def test_deleted_group_removed_from_state(self) -> None:
        """Group in prev_state but not in groups → removed."""
        _, state = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[],
            prev_state={"porch": None, "deleted_group": "old_entry"},
            overrides={},
            lat=_LAT, lon=_LON,
            now=_now_at(12),
        )
        self.assertNotIn("deleted_group", state)
        self.assertIn("porch", state)


# ---------------------------------------------------------------------------
# Action ordering
# ---------------------------------------------------------------------------

class TestActionOrdering(unittest.TestCase):
    """Clear overrides come before stops, stops before starts."""

    def test_clear_before_stop_before_start(self) -> None:
        """On transition: clear_override, then stop, then start."""
        actions, _ = evaluate(
            groups={"porch": ["10.0.0.42"]},
            schedule=[_entry("Overnight", "porch", effect="aurora")],
            prev_state={"porch": "Evening"},
            overrides={"10.0.0.42": "Evening"},
            lat=_LAT, lon=_LON,
            now=_now_at(23, 30),
        )
        types = [a.action for a in actions]
        # clear_override must come before stop and start.
        clear_idx = types.index("clear_override")
        stop_idx = types.index("stop")
        start_idx = types.index("start")
        self.assertLess(clear_idx, stop_idx)
        self.assertLess(stop_idx, start_idx)


if __name__ == "__main__":
    unittest.main()
