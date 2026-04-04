"""Scheduler thread — thin dispatch loop around the pure evaluator.

Polls every SCHEDULER_POLL_SECONDS, snapshots config and device
state under lock, calls the pure evaluator, then dispatches the
resulting actions to the DeviceManager.  No scheduling logic lives
here — it's all in ``evaluator.py``.

The thread survives individual tick failures (logs and continues)
and exits cleanly on stop_event.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.0"

import logging
import threading
from datetime import date, datetime, timezone
from typing import Any, Optional

from scheduling.evaluator import ScheduleAction, evaluate
from server_constants import SCHEDULER_POLL_SECONDS
from solar import SunTimes, sun_times

logger: logging.Logger = logging.getLogger("glowup.scheduling")


class SchedulerThread(threading.Thread):
    """Background thread that manages scheduled effects.

    Calls the pure ``evaluate()`` function each tick to determine
    what actions to take, then dispatches them to the DeviceManager.
    No lock is held during dispatch — only during the brief config
    snapshot.

    Args:
        config:         Parsed server configuration dict.
        device_manager: Shared DeviceManager instance.
    """

    def __init__(
        self,
        config: dict[str, Any],
        device_manager: Any,
    ) -> None:
        """Initialize the scheduler thread.

        Args:
            config:         Full server config with ``location``, ``schedule``.
            device_manager: DeviceManager for play/stop/override calls.
        """
        super().__init__(daemon=True, name="scheduler")
        self._config: dict[str, Any] = config
        self._dm: Any = device_manager
        self._matter: Any = None  # Set via set_matter_adapter().
        self._stop_event: threading.Event = threading.Event()

        # Per-group state: group name → active entry name (or None).
        # Passed to evaluate() each tick and updated with the result.
        self._state: dict[str, Optional[str]] = {}

        # Location — extracted once at startup.
        self._lat: float = config["location"]["latitude"]
        self._lon: float = config["location"]["longitude"]

        # Track date for once-per-day sun time logging.
        self._last_logged_date: Optional[date] = None

    def run(self) -> None:
        """Scheduler main loop — poll, evaluate, dispatch."""
        specs: list[dict[str, Any]] = self._config.get("schedule", [])

        if not specs:
            logging.info("No schedule entries — scheduler idle")
            return

        # Initialize state for all groups.
        with self._dm._lock:
            groups: dict[str, list[str]] = dict(self._dm._group_config)
        for group_name in groups:
            self._state[group_name] = None

        total_devices: int = sum(len(ips) for ips in groups.values())
        logging.info(
            "Scheduler started — %d groups, %d devices, %d entries",
            len(groups), total_devices, len(specs),
        )

        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                logging.error(
                    "Scheduler tick failed: %s", exc, exc_info=True,
                )
            self._stop_event.wait(SCHEDULER_POLL_SECONDS)

        logging.info("Scheduler stopped")

    def _tick(self) -> None:
        """Execute one scheduler cycle: snapshot → evaluate → dispatch."""
        now: datetime = datetime.now(timezone.utc).astimezone()
        today: date = now.date()

        # --- Snapshot under lock (brief) ---
        with self._dm._lock:
            groups: dict[str, list[str]] = dict(self._dm._group_config)
            overrides: dict[str, Optional[str]] = dict(self._dm._overrides)
        specs: list[dict[str, Any]] = self._config.get("schedule", [])

        # Inject Matter devices as virtual groups so the evaluator
        # can schedule them.  Each Matter device appears as a group
        # named "matter:<DeviceName>" with a placeholder IP.
        if self._matter is not None:
            for name in self._matter.get_device_names():
                groups[f"matter:{name}"] = [f"matter:{name}"]

        # --- Log sun times once per day ---
        if today != self._last_logged_date:
            self._log_sun_times(now, today)
            self._last_logged_date = today

        # --- Evaluate (pure, no side effects) ---
        actions, new_state = evaluate(
            groups=groups,
            schedule=specs,
            prev_state=self._state,
            overrides=overrides,
            lat=self._lat,
            lon=self._lon,
            now=now,
        )
        self._state = new_state

        # --- Dispatch actions (no lock held) ---
        for action in actions:
            try:
                self._dispatch(action)
            except Exception as exc:
                logging.warning(
                    "[%s] %s failed on %s: %s",
                    action.group, action.action,
                    action.device_id, exc,
                )

    def set_matter_adapter(self, adapter: Any) -> None:
        """Set the Matter adapter for scheduling Matter devices.

        Args:
            adapter: MatterAdapter instance, or None.
        """
        self._matter = adapter

    def _is_matter_group(self, group_name: str) -> bool:
        """Check if a group name refers to a Matter device.

        Matter groups are prefixed with ``matter:`` in the schedule
        config.

        Args:
            group_name: Group name from the schedule entry.

        Returns:
            True if this is a Matter device group.
        """
        return group_name.startswith("matter:")

    def _dispatch_matter(self, action: ScheduleAction) -> None:
        """Dispatch an action to the Matter adapter.

        Args:
            action: The schedule action to execute.
        """
        if self._matter is None:
            logging.warning(
                "[%s] Matter adapter not available — skipping",
                action.group,
            )
            return

        # Strip the "matter:" prefix to get the device name.
        device_name: str = action.group[7:]

        if action.action == "start":
            effect: str = action.effect or "on"
            if effect == "on":
                self._matter.power_on(device_name)
                logging.info(
                    "[matter:%s] Power on (schedule '%s')",
                    device_name, action.entry_name,
                )
            elif effect == "off":
                self._matter.power_off(device_name)
                logging.info(
                    "[matter:%s] Power off (schedule '%s')",
                    device_name, action.entry_name,
                )
            else:
                logging.warning(
                    "[matter:%s] Unsupported effect '%s' — "
                    "Matter devices only support on/off",
                    device_name, effect,
                )

        elif action.action == "stop":
            self._matter.power_off(device_name)
            logging.info(
                "[matter:%s] Power off (schedule stop '%s')",
                device_name, action.entry_name,
            )

    def _dispatch(self, action: ScheduleAction) -> None:
        """Execute a single schedule action.

        Routes to the Matter adapter for ``matter:`` groups,
        otherwise dispatches to the DeviceManager (LIFX).

        Args:
            action: The action to execute.
        """
        # Route Matter devices to the Matter adapter.
        if self._is_matter_group(action.group):
            self._dispatch_matter(action)
            return

        if action.action == "start":
            logging.info(
                "[%s] Starting '%s' (%s)",
                action.group, action.entry_name, action.effect,
            )
            self._dm.play(
                action.device_id,
                action.effect,
                action.params,
                source="scheduler",
                entry=action.entry_name,
            )

        elif action.action == "stop":
            logging.info(
                "[%s] Stopping '%s'",
                action.group, action.entry_name,
            )
            self._dm.stop(action.device_id)

        elif action.action == "clear_override":
            logging.info(
                "[%s] Clearing phone override on %s "
                "(schedule transition from '%s')",
                action.group, action.device_id, action.entry_name,
            )
            self._dm.clear_override(action.device_id)

    def _log_sun_times(self, now: datetime, today: date) -> None:
        """Log solar event times for the current date.

        Args:
            now:   Current timezone-aware datetime.
            today: Current date.
        """
        utc_offset = now.utcoffset()
        sun: SunTimes = sun_times(
            self._lat, self._lon, today, utc_offset,
        )
        fmt: str = "%H:%M"
        logging.info("Sun times for %s:", today)
        logging.info(
            "  Dawn:    %s",
            sun.dawn.strftime(fmt) if sun.dawn else "N/A",
        )
        logging.info(
            "  Sunrise: %s",
            sun.sunrise.strftime(fmt) if sun.sunrise else "N/A",
        )
        logging.info("  Noon:    %s", sun.noon.strftime(fmt))
        logging.info(
            "  Sunset:  %s",
            sun.sunset.strftime(fmt) if sun.sunset else "N/A",
        )
        logging.info(
            "  Dusk:    %s",
            sun.dusk.strftime(fmt) if sun.dusk else "N/A",
        )

    def stop(self) -> None:
        """Signal the scheduler to stop."""
        self._stop_event.set()
