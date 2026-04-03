"""Pure schedule evaluator — no side effects, no locks, no hardware.

Given the current time, group configuration, schedule entries,
previous state, and active overrides, determines what actions
the scheduler should take.  Returns a list of actions and the
new state.

This module has ZERO dependencies on DeviceManager, Engine,
Controller, or any other stateful object.  It can be tested
with plain data — no mocks, no threads, no network.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Optional

from schedule_utils import find_active_entry as _find_active_entry
from server_utils import group_id_from_name as _group_id_from_name
from solar import SunTimes, sun_times as _sun_times

logger: logging.Logger = logging.getLogger("glowup.scheduling.evaluator")

# ---------------------------------------------------------------------------
# Action dataclass — what the scheduler thread should do
# ---------------------------------------------------------------------------


@dataclass
class ScheduleAction:
    """A single action the scheduler should execute.

    Attributes:
        group:      Group name this action applies to.
        device_id:  Device IP or ``group:Name`` identifier.
        action:     What to do: start, stop, clear_override, or noop.
        effect:     Effect name (for start actions).
        params:     Effect parameters (for start actions).
        entry_name: Schedule entry name (for logging and tracking).
    """

    group: str
    device_id: str
    action: Literal["start", "stop", "clear_override", "noop"]
    effect: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)
    entry_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Pure evaluator
# ---------------------------------------------------------------------------


def evaluate(
    groups: dict[str, list[str]],
    schedule: list[dict[str, Any]],
    prev_state: dict[str, Optional[str]],
    overrides: dict[str, Optional[str]],
    lat: float,
    lon: float,
    now: datetime,
) -> tuple[list[ScheduleAction], dict[str, Optional[str]]]:
    """Evaluate the schedule and determine what actions to take.

    Pure function — no side effects, no locks, no I/O.  Takes
    data in, returns actions out.

    Args:
        groups:     Group name → list of resolved IP addresses.
        schedule:   Schedule entry dicts from config.
        prev_state: Previous tick's state: group name → active entry name
                    (or None if idle).
        overrides:  Currently active phone overrides: device_id → entry name
                    that was active when the override was set.
        lat:        Latitude for solar event calculation.
        lon:        Longitude for solar event calculation.
        now:        Current timezone-aware datetime.

    Returns:
        Tuple of (actions, new_state) where:
        - actions: List of ScheduleAction to execute.
        - new_state: Updated state dict for the next tick.
    """
    actions: list[ScheduleAction] = []
    new_state: dict[str, Optional[str]] = dict(prev_state)

    for group_name, ips in groups.items():
        # Skip empty groups — no devices to control.
        if not ips:
            continue

        # Determine device_id: single IP or virtual group.
        if len(ips) >= 2:
            device_id: str = _group_id_from_name(group_name)
        else:
            device_id = ips[0]

        # Find the currently active schedule entry for this group.
        active: Optional[dict[str, Any]] = _find_active_entry(
            schedule, lat, lon, now, group_name,
        )
        active_name: Optional[str] = (
            active.get("name") if active else None
        )
        prev_name: Optional[str] = prev_state.get(group_name)

        # Ensure new groups get tracked.
        if group_name not in new_state:
            new_state[group_name] = None

        # Check if this device (or any group member) is overridden.
        device_overridden: bool = _is_overridden(
            device_id, overrides, groups,
        )

        if active_name != prev_name:
            # --- TRANSITION detected ---

            # Clear override if it was set against the outgoing entry.
            if device_id in overrides:
                override_entry: Optional[str] = overrides.get(device_id)
                if override_entry == prev_name:
                    actions.append(ScheduleAction(
                        group=group_name,
                        device_id=device_id,
                        action="clear_override",
                        entry_name=prev_name,
                    ))
                    # After clearing, device is no longer overridden.
                    device_overridden = False

            # Stop previous effect (if not overridden).
            if prev_name is not None and not device_overridden:
                actions.append(ScheduleAction(
                    group=group_name,
                    device_id=device_id,
                    action="stop",
                    entry_name=prev_name,
                ))

            # Start new effect (if not overridden).
            if active is not None and not device_overridden:
                actions.append(ScheduleAction(
                    group=group_name,
                    device_id=device_id,
                    action="start",
                    effect=active["effect"],
                    params=dict(active.get("params", {})),
                    entry_name=active_name,
                ))

            new_state[group_name] = active_name

        # No transition — same entry still active (or still idle).
        # No action needed.  The scheduler thread handles restart-
        # if-crashed separately (it needs Controller access).

    # Clean up stale groups from state.
    stale: list[str] = [g for g in new_state if g not in groups]
    for g in stale:
        del new_state[g]

    return actions, new_state


# ---------------------------------------------------------------------------
# Override check — mirrors DeviceManager.is_overridden_or_member
# ---------------------------------------------------------------------------


def _is_overridden(
    device_id: str,
    overrides: dict[str, Optional[str]],
    groups: dict[str, list[str]],
) -> bool:
    """Check if a device or any of its group members is overridden.

    Args:
        device_id: Device IP or ``group:Name`` identifier.
        overrides: Active override map.
        groups:    Group configuration (for member lookup).

    Returns:
        True if the device or any member is overridden.
    """
    if device_id in overrides:
        return True

    # Check group members.
    if device_id.startswith("group:"):
        group_name: str = device_id[6:]
        member_ips: list[str] = groups.get(group_name, [])
        for ip in member_ips:
            if ip in overrides:
                return True

    return False
