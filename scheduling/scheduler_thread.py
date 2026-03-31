"""Embedded schedule evaluator thread.

Background thread that checks schedule entries against the current
time and sun position, starting and stopping effects on devices
through the DeviceManager.

Extracted from server.py.  Each class in its own file per project
convention.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import logging
import threading
import time as time_mod
from datetime import datetime, time, timedelta
from typing import Any, Optional

from device_manager import DeviceManager
from schedule_utils import (
    parse_time_spec as _parse_time_spec,
    entry_runs_on_day as _entry_runs_on_day,
    resolve_entries as _resolve_entries,
    find_active_entry as _find_active_entry,
)
from server_constants import SCHEDULER_POLL_SECONDS, DEFAULT_FADE_MS
from solar import SunTimes, sun_times

logger: logging.Logger = logging.getLogger("glowup.scheduling")


class SchedulerThread(threading.Thread):
    """Background thread that manages scheduled effects.

    Replaces scheduler.py's subprocess-based approach with direct
    :class:`Controller` calls through the :class:`DeviceManager`.
    Respects phone overrides: skips devices that have been overridden
    by the REST API, and clears overrides at schedule transitions.
    """

    def __init__(
        self,
        config: dict[str, Any],
        device_manager: DeviceManager,
    ) -> None:
        """Initialize the scheduler thread.

        Args:
            config:         Parsed server configuration dict.
            device_manager: Shared :class:`DeviceManager` instance.
        """
        super().__init__(daemon=True, name="scheduler")
        self._config: dict[str, Any] = config
        self._dm: DeviceManager = device_manager
        self._stop_event: threading.Event = threading.Event()

        # Per-group state: tracks which schedule entry is currently active.
        self._group_entries: dict[str, Optional[str]] = {}

    def run(self) -> None:
        """Scheduler main loop — poll for schedule transitions.

        Groups and schedule entries are re-read each iteration so
        that runtime changes (group rename/create/delete, schedule
        edits) take effect without a server restart.
        """
        lat: float = self._config["location"]["latitude"]
        lon: float = self._config["location"]["longitude"]

        # Initial snapshot for the startup log line.
        with self._dm._lock:
            groups: dict[str, list[str]] = dict(self._dm._group_config)
        specs: list[dict[str, Any]] = self._config.get("schedule", [])

        if not specs:
            logging.info("No schedule entries — scheduler idle")
            return

        # Initialize per-group state.
        for group_name in groups:
            self._group_entries[group_name] = None

        last_logged_date: Optional[date] = None

        total_devices: int = sum(len(ips) for ips in groups.values())
        logging.info(
            "Scheduler started — %d groups, %d devices, %d entries",
            len(groups), total_devices, len(specs),
        )

        while not self._stop_event.is_set():
            now: datetime = datetime.now(timezone.utc).astimezone()
            today: date = now.date()

            # Re-read groups and schedule entries each iteration so
            # runtime API changes (rename, create, delete) are picked
            # up without restarting.  Snapshot under lock.
            with self._dm._lock:
                groups = dict(self._dm._group_config)
            specs = self._config.get("schedule", [])

            # Initialize tracking for newly-appeared groups and
            # clean up entries for groups that were deleted.
            for group_name in groups:
                if group_name not in self._group_entries:
                    self._group_entries[group_name] = None
            stale_groups: list[str] = [
                g for g in self._group_entries if g not in groups
            ]
            for g in stale_groups:
                del self._group_entries[g]

            # Log sun times once per day.
            if today != last_logged_date:
                utc_offset: timedelta = now.utcoffset()
                sun: SunTimes = sun_times(lat, lon, today, utc_offset)
                _log_sun_times(sun, today)
                last_logged_date = today

            # Per-group scheduling.
            for group_name, ips in groups.items():
                active: Optional[dict[str, Any]] = _find_active_entry(
                    specs, lat, lon, now, group_name,
                )
                active_name: Optional[str] = (
                    active.get("name") if active else None
                )
                prev_name: Optional[str] = self._group_entries.get(
                    group_name,
                )

                # Device ID for this group: virtual device for multi-IP
                # groups, individual IP for single-device groups.
                if len(ips) >= 2:
                    device_id: str = _group_id_from_name(group_name)
                else:
                    device_id = ips[0]

                if active_name != prev_name:
                    # Schedule transition — clear overrides only if
                    # the override was set against the outgoing entry.
                    if self._dm.is_overridden(device_id):
                        override_entry: Optional[str] = (
                            self._dm.get_override_entry(device_id)
                        )
                        if override_entry == prev_name:
                            logging.info(
                                "[%s] Clearing phone override on "
                                "%s (schedule transition from "
                                "'%s' to '%s')",
                                group_name, device_id,
                                prev_name, active_name,
                            )
                            self._dm.clear_override(device_id)
                        else:
                            logging.info(
                                "[%s] Preserving phone override "
                                "on %s (override entry '%s' != "
                                "outgoing '%s')",
                                group_name, device_id,
                                override_entry, prev_name,
                            )

                    # Stop previous effect if not overridden.
                    # Use is_overridden_or_member so that an override
                    # on an individual member device (e.g. 192.0.2.62)
                    # prevents the scheduler from clobbering it when
                    # the group (e.g. group:porch) transitions.
                    if prev_name is not None:
                        if not self._dm.is_overridden_or_member(
                            device_id,
                        ):
                            logging.info(
                                "[%s] Stopping '%s'",
                                group_name, prev_name,
                            )
                            try:
                                self._dm.stop(device_id)
                            except (KeyError, Exception) as exc:
                                logging.warning(
                                    "[%s] Error stopping %s: %s",
                                    group_name, device_id, exc,
                                )

                    # Start new effect if not overridden.
                    if active is not None:
                        if not self._dm.is_overridden_or_member(
                            device_id,
                        ):
                            effect: str = active["effect"]
                            params: dict[str, Any] = active.get(
                                "params", {},
                            )
                            # Pass bindings from schedule entry if present.
                            sched_bindings: Optional[dict] = active.get(
                                "bindings",
                            )
                            sched_bus: Optional[SignalBus] = None
                            mm: Optional[MediaManager] = (
                                GlowUpRequestHandler.media_manager
                            )
                            if sched_bindings and mm is not None:
                                sched_bus = mm.bus
                            logging.info(
                                "[%s] Starting '%s' (%s)",
                                group_name, active_name, effect,
                            )
                            try:
                                self._dm.play(
                                    device_id, effect, params,
                                    bindings=sched_bindings,
                                    signal_bus=sched_bus,
                                    source="scheduler",
                                    entry=active_name,
                                )
                            except (KeyError, ValueError, Exception) as exc:
                                logging.warning(
                                    "[%s] Error starting %s on %s: %s",
                                    group_name, effect, device_id, exc,
                                )
                    else:
                        logging.info(
                            "[%s] No active entry — idle", group_name,
                        )

                    self._group_entries[group_name] = active_name

                elif active is not None:
                    # Same entry still active — ensure running
                    # (restart if crashed).  Check members too so
                    # an individual device override isn't clobbered.
                    if self._dm.is_overridden_or_member(device_id):
                        continue
                    ctrl: Optional[Controller] = (
                        self._dm.get_or_create_controller(device_id)
                    )
                    if ctrl is not None:
                        status: dict[str, Any] = ctrl.get_status()
                        if not status.get("running"):
                            effect_name: str = active["effect"]
                            params_restart: dict = active.get(
                                "params", {},
                            )
                            restart_bindings: Optional[dict] = (
                                active.get("bindings")
                            )
                            restart_bus: Optional[SignalBus] = None
                            rmm: Optional[MediaManager] = (
                                GlowUpRequestHandler.media_manager
                            )
                            if restart_bindings and rmm is not None:
                                restart_bus = rmm.bus
                            logging.info(
                                "[%s] Restarting '%s' on %s",
                                group_name, active_name, device_id,
                            )
                            try:
                                self._dm.play(
                                    device_id, effect_name,
                                    params_restart,
                                    bindings=restart_bindings,
                                    signal_bus=restart_bus,
                                    source="scheduler",
                                    entry=active_name,
                                )
                            except Exception as exc:
                                logging.warning(
                                    "[%s] Restart error on %s: %s",
                                    group_name, device_id, exc,
                                )

            # Sleep until next poll, checking for stop every second.
            self._stop_event.wait(SCHEDULER_POLL_SECONDS)

        logging.info("Scheduler stopped")

    def stop(self) -> None:
        """Signal the scheduler to stop."""
        self._stop_event.set()

