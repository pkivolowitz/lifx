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

from server_constants import *  # All constants available
from operators import OperatorManager
from automation import validate_automation
from effects import get_registry
from media import SignalBus


class SensorHandlerMixin:
    """BLE sensor and automation handlers."""

    def _handle_get_ble_sensors(self) -> None:
        """GET /api/ble/sensors — all BLE sensor readings.

        Reads from the SignalBus (populated by BleAdapter).  Signals
        are named ``ble:{label}:{characteristic}`` — this endpoint
        reconstructs the per-label grouped view and adds location and
        status metadata.
        """
        bus: Optional[SignalBus] = self.signal_bus
        if bus is None:
            self._send_json(200, {})
            return

        locations: dict[str, str] = self.config.get(
            "sensor_locations", {},
        )
        ble_signals: dict = bus.signals_by_transport("ble")

        # Group by label: {label}:{char} → {label: {char: val, ...}}
        grouped: dict[str, dict[str, Any]] = {}
        boot: float = time_mod.monotonic()
        now_wall: float = time_mod.time()
        for name, (value, ts) in ble_signals.items():
            parts: list[str] = name.split(":")
            if len(parts) != 2:
                continue
            label: str = parts[0]
            char: str = parts[1]
            if label not in grouped:
                grouped[label] = {}
            # Convert value to int for motion (legacy compat).
            if char == "motion":
                grouped[label][char] = int(value)
            else:
                grouped[label][char] = value
            # Convert monotonic timestamp to wall-clock epoch.
            if ts is not None:
                wall_ts: float = now_wall - (boot - ts)
                existing: float = grouped[label].get("last_update", 0.0)
                if wall_ts > existing:
                    grouped[label]["last_update"] = wall_ts

        # Enrich with location and status blobs.
        ble_proxy: Optional[Any] = self.ble_adapter
        for lbl, readings in grouped.items():
            loc: str = locations.get(lbl, "")
            if loc:
                readings["location"] = loc
            if ble_proxy is not None and hasattr(ble_proxy, "send_command"):
                try:
                    result: dict = ble_proxy.send_command(
                        "get_status_blob", {"label": lbl},
                    )
                    blob: Optional[dict] = result.get("blob")
                    if blob is not None:
                        readings["status"] = blob
                except (TimeoutError, Exception):
                    pass

        # Enrich with watchdog countdown data from automations.
        auto_mgr: Optional[Any] = self.automation_manager
        if auto_mgr is not None:
            watchdog_states: dict[str, dict] = auto_mgr.get_watchdog_states()
            for lbl, wd in watchdog_states.items():
                if lbl in grouped:
                    grouped[lbl]["watchdog"] = wd

        self._send_json(200, grouped)


    def _handle_get_ble_sensor_detail(self, label: str) -> None:
        """GET /api/ble/sensors/{label} — single sensor readings."""
        bus: Optional[SignalBus] = self.signal_bus
        if bus is None:
            self._send_json(404, {"error": f"No data for '{label}'"})
            return

        prefix: str = f"{label}:"
        signals: dict = bus.signals_by_prefix(prefix)
        if not signals:
            self._send_json(404, {"error": f"No data for '{label}'"})
            return

        data: dict[str, Any] = {}
        boot: float = time_mod.monotonic()
        now_wall: float = time_mod.time()
        for name, (value, ts) in signals.items():
            char: str = name.split(":")[-1]
            if char == "motion":
                data[char] = int(value)
            else:
                data[char] = value
            if ts is not None:
                wall_ts: float = now_wall - (boot - ts)
                existing: float = data.get("last_update", 0.0)
                if wall_ts > existing:
                    data["last_update"] = wall_ts

        locations: dict[str, str] = self.config.get(
            "sensor_locations", {},
        )
        loc: str = locations.get(label, "")
        if loc:
            data["location"] = loc
        ble_proxy2: Optional[Any] = self.ble_adapter
        if ble_proxy2 is not None and hasattr(ble_proxy2, "send_command"):
            try:
                res: dict = ble_proxy2.send_command(
                    "get_status_blob", {"label": label},
                )
                blob2: Optional[dict] = res.get("blob")
                if blob2 is not None:
                    data["status"] = blob2
            except (TimeoutError, Exception):
                pass
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


