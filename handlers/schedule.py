"""Schedule CRUD handlers.

Mixin class for GlowUpRequestHandler.  Extracted from server.py.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import logging
import math
import os
import socket
import struct
import threading
import time as time_mod
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from urllib.parse import unquote

# server_constants not used in this module.
from schedule_utils import (
    parse_time_spec as _parse_time_spec,
    entry_runs_on_day as _entry_runs_on_day,
    resolve_entries as _resolve_entries,
    find_active_entry as _find_active_entry,
    validate_days as _validate_days,
    days_display as _days_display,
    VALID_DAY_LETTERS as _VALID_DAY_LETTERS,
    SYMBOLIC_RE as _SYMBOLIC_RE,
    FIXED_TIME_RE as _FIXED_TIME_RE,
)
from solar import SunTimes, sun_times
from effects import get_registry


class ScheduleHandlerMixin:
    """Schedule CRUD handlers."""

    def _handle_get_schedule(self) -> None:
        """GET /api/schedule — schedule entries with resolved times.

        Returns the schedule entries from the config, each enriched
        with resolved start/stop times for today and an ``active``
        flag indicating whether the entry is running right now.
        """
        config: dict[str, Any] = self.config
        specs: list[dict[str, Any]] = config.get("schedule", [])
        if not specs:
            self._send_json(200, {"entries": []})
            return

        lat: float = config.get("location", {}).get("latitude", 0.0)
        lon: float = config.get("location", {}).get("longitude", 0.0)

        now: datetime = datetime.now(timezone.utc).astimezone()
        utc_offset: timedelta = now.utcoffset()
        today: date = now.date()

        # Resolve times for today to determine active status and
        # display times.  We resolve without group filter to get all.
        sun: SunTimes = sun_times(lat, lon, today, utc_offset)

        entries: list[dict[str, Any]] = []
        for i, spec in enumerate(specs):
            enabled: bool = spec.get("enabled", True)
            days_raw: str = spec.get("days", "")

            # Resolve start/stop for display.
            start_resolved: Optional[datetime] = _parse_time_spec(
                spec["start"], sun, today, utc_offset,
            )
            stop_resolved: Optional[datetime] = _parse_time_spec(
                spec["stop"], sun, today, utc_offset,
            )

            start_str: Optional[str] = None
            stop_str: Optional[str] = None
            active: bool = False

            if start_resolved is not None and stop_resolved is not None:
                # Handle overnight entries.
                if stop_resolved <= start_resolved:
                    stop_resolved += timedelta(days=1)
                start_str = start_resolved.strftime("%H:%M")
                stop_str = stop_resolved.strftime("%H:%M")
                if stop_resolved.date() != start_resolved.date():
                    stop_str = stop_resolved.strftime("%H:%M (+1)")

                # Active if enabled, runs today, and we're in the window.
                if (enabled
                        and _entry_runs_on_day(spec, today)
                        and start_resolved <= now < stop_resolved):
                    active = True

            entry: dict[str, Any] = {
                "index": i,
                "name": spec.get("name", f"entry_{i}"),
                "group": spec.get("group", ""),
                "effect": spec.get("effect", ""),
                "start": spec.get("start", ""),
                "stop": spec.get("stop", ""),
                "start_resolved": start_str,
                "stop_resolved": stop_str,
                "params": spec.get("params", {}),
                "days": days_raw,
                "days_display": _days_display(days_raw),
                "enabled": enabled,
                "active": active,
            }
            entries.append(entry)

        self._send_json(200, {"entries": entries})


    def _handle_post_schedule_create(self) -> None:
        """POST /api/schedule — create a new schedule entry.

        Request body::

            {
                "name": "porch evening aurora",
                "group": "porch",
                "start": "sunset-30m",
                "stop": "23:00",
                "effect": "aurora",
                "params": {"speed": 10.0, "brightness": 100},
                "days": ""
            }

        Validates all fields identically to the PUT handler.
        New entries are created enabled by default.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        # --- Validate required fields (same checks as PUT) ---
        errors: list[str] = []

        name: str = body.get("name", "").strip()
        if not name:
            errors.append("Name is required")

        group: str = body.get("group", "").strip()
        if not group:
            errors.append("Group is required")

        effect: str = body.get("effect", "").strip()
        if not effect:
            errors.append("Effect is required")

        start_spec: str = body.get("start", "").strip()
        stop_spec: str = body.get("stop", "").strip()
        if not start_spec:
            errors.append("Start time is required")
        if not stop_spec:
            errors.append("Stop time is required")

        # Validate time specs parse correctly.
        if start_spec and not (
            _FIXED_TIME_RE.match(start_spec) or _SYMBOLIC_RE.match(start_spec)
        ):
            errors.append(
                f"Invalid start time: {start_spec!r} "
                "(use HH:MM or sunrise/sunset/dawn/dusk/noon/midnight[+-Nh][Mm])"
            )
        if stop_spec and not (
            _FIXED_TIME_RE.match(stop_spec) or _SYMBOLIC_RE.match(stop_spec)
        ):
            errors.append(
                f"Invalid stop time: {stop_spec!r} "
                "(use HH:MM or sunrise/sunset/dawn/dusk/noon/midnight[+-Nh][Mm])"
            )

        # Validate effect exists in registry.
        if effect:
            registry: dict = get_registry()
            if effect not in registry:
                errors.append(f"Unknown effect: {effect!r}")

        # Validate group exists in config.
        if group:
            config_groups: dict[str, Any] = self.config.get("groups", {})
            sched_groups: dict[str, Any] = (
                self.config.get("schedule_groups", {})
                or self.config.get("groups", {})
            )
            if group not in config_groups and group not in sched_groups:
                errors.append(f"Unknown group: {group!r}")

        # Validate days if provided.
        days_raw: str = body.get("days", "").strip()

        # Validate params is a dict if provided.
        params: Any = body.get("params", {})
        if not isinstance(params, dict):
            errors.append("params must be an object")

        if errors:
            self._send_json(400, {"error": "; ".join(errors)})
            return

        # --- Append new entry ---
        entry: dict[str, Any] = {
            "name": name,
            "group": group,
            "start": start_spec,
            "stop": stop_spec,
            "effect": effect,
            "params": params if isinstance(params, dict) else {},
            "enabled": True,
        }
        if days_raw:
            entry["days"] = days_raw

        # Copy the list before mutating — the scheduler thread reads
        # the live config concurrently.  Assign back atomically.
        specs: list[dict[str, Any]] = list(self.config.get("schedule", []))
        specs.append(entry)
        self.config["schedule"] = specs
        self._save_config_field("schedule", specs)

        new_index: int = len(specs) - 1
        logging.info(
            "API: schedule entry %d created: '%s' %s on %s (%s→%s)",
            new_index, name, effect, group, start_spec, stop_spec,
        )
        self._send_json(201, {
            "index": new_index,
            "name": name,
            "created": True,
        })


    def _handle_post_schedule_enabled(self, index: int) -> None:
        """POST /api/schedule/{index}/enabled — enable or disable an entry.

        Request body::

            {"enabled": false}

        Persists the change to the config file so it survives restarts.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        enabled: Any = body.get("enabled")
        if not isinstance(enabled, bool):
            self._send_json(400, {"error": "'enabled' must be a boolean"})
            return

        specs: list[dict[str, Any]] = list(self.config.get("schedule", []))
        if index < 0 or index >= len(specs):
            self._send_json(404, {"error": "Schedule entry not found"})
            return

        specs[index]["enabled"] = enabled
        self.config["schedule"] = specs
        self._save_config_field("schedule", specs)

        name: str = specs[index].get("name", f"entry_{index}")
        logging.info(
            "API: schedule entry '%s' %s",
            name, "enabled" if enabled else "disabled",
        )
        self._send_json(200, {
            "index": index,
            "name": name,
            "enabled": enabled,
        })


    def _handle_delete_schedule_entry(self, index: int) -> None:
        """DELETE /api/schedule/{index} — remove a schedule entry.

        Validates the index is within bounds, removes the entry from
        the schedule list, and persists the change.  Returns the name
        of the deleted entry for confirmation.
        """
        specs: list[dict[str, Any]] = list(self.config.get("schedule", []))
        if index < 0 or index >= len(specs):
            self._send_json(404, {"error": "Schedule entry not found"})
            return

        removed: dict[str, Any] = specs.pop(index)
        self.config["schedule"] = specs
        self._save_config_field("schedule", specs)

        # Clear any overrides that reference the deleted entry so
        # the scheduler can resume managing those IPs rather than
        # leaving stale overrides from a non-existent entry.
        name: str = removed.get("name", f"entry_{index}")
        with self.device_manager._lock:
            stale_ips: list[str] = [
                ip for ip, entry in self.device_manager._overrides.items()
                if entry == name
            ]
            for ip in stale_ips:
                self.device_manager._overrides.pop(ip, None)

        logging.info("API: schedule entry deleted: '%s' (was index %d)", name, index)
        self._send_json(200, {"deleted": name, "former_index": index})


    def _handle_put_schedule_entry(self, index: int) -> None:
        """PUT /api/schedule/{index} — update a schedule entry.

        Request body::

            {
                "name": "porch evening aurora",
                "group": "porch",
                "start": "sunset-30m",
                "stop": "23:00",
                "effect": "aurora",
                "params": {"speed": 10.0, "brightness": 100},
                "enabled": true,
                "days": ""
            }

        Validates time specs and effect name before persisting.
        Sends 400 with details on validation failure.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        specs: list[dict[str, Any]] = list(self.config.get("schedule", []))
        if index < 0 or index >= len(specs):
            self._send_json(404, {"error": "Schedule entry not found"})
            return

        # --- Validate required fields ---
        errors: list[str] = []

        name: str = body.get("name", "").strip()
        if not name:
            errors.append("Name is required")

        group: str = body.get("group", "").strip()
        if not group:
            errors.append("Group is required")

        effect: str = body.get("effect", "").strip()
        if not effect:
            errors.append("Effect is required")

        start_spec: str = body.get("start", "").strip()
        stop_spec: str = body.get("stop", "").strip()
        if not start_spec:
            errors.append("Start time is required")
        if not stop_spec:
            errors.append("Stop time is required")

        # Validate time specs parse correctly.
        if start_spec and not (
            _FIXED_TIME_RE.match(start_spec) or _SYMBOLIC_RE.match(start_spec)
        ):
            errors.append(
                f"Invalid start time: {start_spec!r} "
                "(use HH:MM or sunrise/sunset/dawn/dusk/noon/midnight[+-Nh][Mm])"
            )
        if stop_spec and not (
            _FIXED_TIME_RE.match(stop_spec) or _SYMBOLIC_RE.match(stop_spec)
        ):
            errors.append(
                f"Invalid stop time: {stop_spec!r} "
                "(use HH:MM or sunrise/sunset/dawn/dusk/noon/midnight[+-Nh][Mm])"
            )

        # Validate effect exists.
        if effect:
            registry: dict = get_registry()
            if effect not in registry:
                errors.append(f"Unknown effect: {effect!r}")

        # Validate group exists in config.
        if group:
            config_groups: dict[str, Any] = self.config.get("groups", {})
            sched_groups: dict[str, Any] = (
                self.config.get("schedule_groups", {})
                or self.config.get("groups", {})
            )
            # Check both server groups and schedule-specific groups.
            if group not in config_groups and group not in sched_groups:
                errors.append(f"Unknown group: {group!r}")

        # Validate days if provided.
        days_raw: str = body.get("days", "").strip()

        # Validate params is a dict if provided.
        params: Any = body.get("params", {})
        if not isinstance(params, dict):
            errors.append("params must be an object")

        if errors:
            self._send_json(400, {"error": "; ".join(errors)})
            return

        # --- Apply update ---
        enabled: bool = body.get("enabled", specs[index].get("enabled", True))

        specs[index] = {
            "name": name,
            "group": group,
            "start": start_spec,
            "stop": stop_spec,
            "effect": effect,
            "params": params if isinstance(params, dict) else {},
            "enabled": enabled,
        }
        if days_raw:
            specs[index]["days"] = days_raw

        self.config["schedule"] = specs
        self._save_config_field("schedule", specs)

        logging.info(
            "API: schedule entry %d updated: '%s' %s on %s (%s→%s)",
            index, name, effect, group, start_spec, stop_spec,
        )
        self._send_json(200, {
            "index": index,
            "name": name,
            "updated": True,
        })

    # -- Media handlers -----------------------------------------------------


