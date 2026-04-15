"""BLE sensor and automation handlers.

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
from datetime import datetime, time, timedelta
from typing import Any, Optional
from urllib.parse import unquote

# server_constants not used in this module.
from operators import OperatorManager
from automation import validate_automation
from effects import get_registry
from media import SignalBus


class SensorHandlerMixin:
    """BLE sensor and automation handlers."""

    def _handle_get_ble_sensors(self) -> None:
        """GET /api/ble/sensors — all BLE sensor readings.

        Source of truth: ``infrastructure.ble_trigger.sensor_data``,
        the in-process store hydrated by ``BleTriggerManager``'s
        local MQTT subscriber.  After the 2026-04-15 service-pattern
        pivot, the BLE sensor daemon (``glowup-ble-sensor`` on
        broker-2) publishes signals cross-host directly to the hub
        mosquitto on ``glowup/signals/{label}:{prop}`` and JSON
        status blobs on ``glowup/ble/status/{label}``.
        ``BleTriggerManager`` subscribes locally on the hub broker
        and writes both into ``BleSensorData`` — so this endpoint
        reads the same store the watchdog logic does.

        See:
          - docs/35-service-vs-adapter.md
          - docs/28-ble-sensors.md
          - feedback_service_vs_adapter_rule.md

        Per-label payload includes the latest motion (int), float
        temperature, float humidity, optional location string,
        last_update epoch, optional status JSON blob, and watchdog
        countdown from BleTriggerManager (when configured).
        """
        # Local import — avoids a top-level circular import via
        # the server bootstrap order, and keeps the handler module
        # importable in tests that don't pull in BleTriggerManager.
        from infrastructure.ble_trigger import sensor_data as ble_sensor_data

        all_data: dict[str, dict[str, Any]] = ble_sensor_data.get_all()
        locations: dict[str, str] = self.config.get(
            "sensor_locations", {},
        )

        # Build the response from the canonical store, enriching
        # with location strings from server.json.  Watchdog state
        # is appended via the trigger manager's public accessor.
        grouped: dict[str, dict[str, Any]] = {}
        for label, readings in all_data.items():
            entry: dict[str, Any] = dict(readings)
            # motion is stored as int by BleSensorData.update —
            # preserve that contract for the legacy frontend.
            if "motion" in entry:
                try:
                    entry["motion"] = int(entry["motion"])
                except (TypeError, ValueError):
                    pass
            loc: str = locations.get(label, "")
            if loc:
                entry["location"] = loc
            grouped[label] = entry

        # NOTE: the previous BleAdapter-era code attempted to
        # enrich each entry with a "watchdog" countdown sourced
        # from a now-defunct AutomationManager.  That enrichment
        # was already dead (the manager was never instantiated)
        # and is not restored here.  If you ever need motion
        # watchdog state in this payload, add a get_watchdog_states()
        # method to BleTriggerManager that returns
        # {label: {seconds_until_off: float, configured_timeout: float}}
        # and re-add the enrichment loop.  Stash the manager on
        # GlowUpRequestHandler in server.py's _background_startup
        # so the handler can reach it.

        self._send_json(200, grouped)


    def _handle_get_ble_sensor_detail(self, label: str) -> None:
        """GET /api/ble/sensors/{label} — single sensor readings.

        Same source as ``_handle_get_ble_sensors`` but scoped to
        one label.  Returns 404 if no data has been received for
        the label since hub start (a satellite that has paired but
        hasn't reported any value yet shows up as 404 — fall back
        to the all-sensors endpoint to see registered-but-silent
        labels).
        """
        from infrastructure.ble_trigger import sensor_data as ble_sensor_data

        readings: dict[str, Any] = ble_sensor_data.get(label)
        if not readings:
            self._send_json(404, {"error": f"No data for '{label}'"})
            return

        data: dict[str, Any] = dict(readings)
        if "motion" in data:
            try:
                data["motion"] = int(data["motion"])
            except (TypeError, ValueError):
                pass

        locations: dict[str, str] = self.config.get(
            "sensor_locations", {},
        )
        loc: str = locations.get(label, "")
        if loc:
            data["location"] = loc

        self._send_json(200, data)


    def _handle_put_sensor_location(self, label: str) -> None:
        """PUT /api/ble/sensors/{label}/location — set display location.

        Request body::

            {"location": "Living Room"}

        Persists to ``sensor_locations`` in server.json.  Pass an
        empty string or ``null`` to clear.
        """
        body: Optional[dict] = self._read_json_body()
        if body is None:
            return
        location: str = (body.get("location") or "").strip()

        locations: dict[str, str] = dict(
            self.config.get("sensor_locations", {}),
        )
        if location:
            locations[label] = location
        else:
            locations.pop(label, None)

        self.config["sensor_locations"] = locations
        self._save_config_field("sensor_locations", locations)
        self._send_json(200, {"label": label, "location": location or None})

    # ------------------------------------------------------------------
    # Automation endpoints
    # ------------------------------------------------------------------


    def _handle_get_automations(self) -> None:
        """GET /api/automations — list all trigger operators with status.

        Returns trigger operators from the operators config, enriched
        with runtime status from the OperatorManager.
        """
        triggers: list[dict[str, Any]] = self._get_trigger_operators()
        # Get runtime status from OperatorManager.
        om: Optional[OperatorManager] = self.operator_manager
        status_list: list[dict] = om.get_status() if om is not None else []
        status_map: dict[str, dict] = {
            s["name"]: s for s in status_list if s.get("type") == "trigger"
        }
        result: list[dict[str, Any]] = []
        for i, trig in enumerate(triggers):
            entry: dict[str, Any] = dict(trig)
            entry["index"] = i
            st: dict = status_map.get(trig.get("name", ""), {})
            entry["active"] = st.get("active", False)
            entry["last_triggered"] = st.get("last_triggered", 0)
            result.append(entry)
        self._send_json(200, {"automations": result})


    def _handle_post_automation_create(self) -> None:
        """POST /api/automations — create a new trigger operator.

        Request body matches the automation data model.  The entry is
        stored as a trigger operator in the ``operators`` config list.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        # Build validation context.
        config_groups: dict = self.config.get("groups", {})
        known_groups: set[str] = set(config_groups.keys())
        registry: dict = get_registry()
        known_effects: set[str] = set(registry.keys())
        media_effects: set[str] = {
            name for name, cls in registry.items()
            if issubclass(cls, MediaEffect)
        }

        errors: list[str] = validate_automation(
            body, known_groups, known_effects, media_effects,
        )
        if errors:
            self._send_json(400, {"error": "; ".join(errors)})
            return

        # Default fields.
        body.setdefault("enabled", True)
        body.setdefault("schedule_conflict", "defer")
        body.setdefault("off_action", {"effect": "off", "params": {}})

        # Wrap as trigger operator entry.
        body["type"] = "trigger"
        if "name" not in body:
            body["name"] = f"trigger_{int(time_mod.time())}"

        operators_list: list = list(self.config.get("operators", []))
        operators_list.append(body)
        self.config["operators"] = operators_list
        self._save_config_field("operators", operators_list)

        # Hot-reload requires restart for now — OperatorManager doesn't
        # support adding instances at runtime yet.  Log it.
        logging.info(
            "Trigger operator '%s' created — restart to activate",
            body.get("name", "?"),
        )

        triggers: list = self._get_trigger_operators()
        self._send_json(201, {"index": len(triggers) - 1, **body})


    def _handle_put_automation(self, index: int) -> None:
        """PUT /api/automations/{index} — update a trigger operator."""
        triggers: list = self._get_trigger_operators()
        if index < 0 or index >= len(triggers):
            self._send_json(404, {"error": f"No automation at index {index}"})
            return

        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        config_groups: dict = self.config.get("groups", {})
        known_groups: set[str] = set(config_groups.keys())
        registry: dict = get_registry()
        known_effects: set[str] = set(registry.keys())
        media_effects: set[str] = {
            name for name, cls in registry.items()
            if issubclass(cls, MediaEffect)
        }

        errors: list[str] = validate_automation(
            body, known_groups, known_effects, media_effects,
        )
        if errors:
            self._send_json(400, {"error": "; ".join(errors)})
            return

        body.setdefault("enabled", True)
        body.setdefault("schedule_conflict", "defer")
        body.setdefault("off_action", {"effect": "off", "params": {}})
        body["type"] = "trigger"
        body.setdefault("name", triggers[index].get("name", ""))

        # Replace in the full operators list.
        target_name: str = triggers[index].get("name", "")
        operators_list: list = list(self.config.get("operators", []))
        for i, op in enumerate(operators_list):
            if op.get("type") == "trigger" and op.get("name") == target_name:
                operators_list[i] = body
                break
        self.config["operators"] = operators_list
        self._save_config_field("operators", operators_list)

        logging.info(
            "Trigger operator '%s' updated — restart to activate changes",
            body.get("name", "?"),
        )

        self._send_json(200, {"index": index, **body})


    def _handle_post_automation_enabled(self, index: int) -> None:
        """POST /api/automations/{index}/enabled — toggle trigger."""
        triggers: list = self._get_trigger_operators()
        if index < 0 or index >= len(triggers):
            self._send_json(404, {"error": f"No automation at index {index}"})
            return

        auto_name: str = triggers[index].get("name", "")

        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        enabled: bool = bool(body.get("enabled", True))

        # Update in the full operators list.
        operators_list: list = list(self.config.get("operators", []))
        for op in operators_list:
            if op.get("type") == "trigger" and op.get("name") == auto_name:
                op["enabled"] = enabled
                break
        self.config["operators"] = operators_list
        self._save_config_field("operators", operators_list)

        # Also toggle at runtime if operator is running.
        om: Optional[OperatorManager] = self.operator_manager
        if om is not None:
            for slot in om._slots:
                if slot.operator.name == auto_name:
                    from operators.trigger import TriggerOperator
                    if isinstance(slot.operator, TriggerOperator):
                        slot.operator.set_enabled(enabled)
                    break

        self._send_json(200, {
            "index": index,
            "enabled": enabled,
            "name": auto_name,
        })


    def _handle_delete_automation(self, index: int) -> None:
        """DELETE /api/automations/{index} — remove a trigger operator."""
        triggers: list = self._get_trigger_operators()
        if index < 0 or index >= len(triggers):
            self._send_json(404, {"error": f"No automation at index {index}"})
            return

        target_name: str = triggers[index].get("name", "")

        # Remove from the full operators list.
        operators_list: list = list(self.config.get("operators", []))
        operators_list = [
            op for op in operators_list
            if not (op.get("type") == "trigger" and op.get("name") == target_name)
        ]
        self.config["operators"] = operators_list
        self._save_config_field("operators", operators_list)

        logging.info(
            "Trigger operator '%s' deleted — restart to fully remove",
            target_name,
        )

        self._send_json(200, {"deleted": target_name})

    # ------------------------------------------------------------------


    def _get_trigger_operators(self) -> list[dict[str, Any]]:
        """Return trigger-type entries from the operators config list.

        Returns:
            List of trigger operator config dicts.
        """
        return [
            op for op in self.config.get("operators", [])
            if op.get("type") == "trigger"
        ]


