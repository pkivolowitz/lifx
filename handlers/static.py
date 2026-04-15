"""Static read-only endpoint handlers (status, devices, effects, groups).

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


class StaticHandlerMixin:
    """Static read-only endpoint handlers (status, devices, effects, groups)."""

    def _handle_get_status(self) -> None:
        """GET /api/status — server readiness, version, and adapter health.

        Returns a status object indicating whether initial device
        loading has completed, plus the running/connected state of
        every adapter and daemon thread.  Clients can poll this
        endpoint on connect and show a "loading devices" message
        until ``ready`` becomes ``true``.
        """
        ready: bool = self.device_manager.ready
        status: str = "ready" if ready else "loading"

        # Adapter/daemon health — each reports running/connected.
        # Zigbee is absent: it runs on broker-2 as glowup-zigbee-service
        # and is reported via the broker-2 signals liveness probe in
        # _handle_get_home_health, not as a local adapter proxy.
        adapters: dict[str, dict[str, Any]] = {}
        for attr, label in [
            ("_vivint_adapter", "vivint"),
            ("_nvr_adapter", "nvr"),
            ("_printer_adapter", "printer"),
            ("_mqtt_bridge", "mqtt"),
            ("_matter_adapter", "matter"),
        ]:
            obj: Any = getattr(self.server, attr, None)
            if obj is not None:
                try:
                    adapters[label] = obj.get_status()
                except Exception:
                    adapters[label] = {"running": True}

        # Keepalive thread (class attribute on the handler).
        ka: Any = getattr(self.__class__, "keepalive", None)
        if ka is not None:
            adapters["keepalive"] = {"running": ka.is_alive()}

        # Scheduler thread (class attribute on the handler).
        sched: Any = getattr(self.__class__, "scheduler", None)
        if sched is not None:
            adapters["scheduler"] = {"running": sched.is_alive()}

        self._send_json(200, {
            "status": status,
            "ready": ready,
            "version": __version__,
            "adapters": adapters,
        })


    def _handle_get_devices(self) -> None:
        """GET /api/devices — list all configured devices.

        Returns each device's IP, label, product name, zone count,
        group membership, power state, and current effect status.
        Includes Matter-only groups as virtual group entries.
        """
        devices: list[dict[str, Any]] = self.device_manager.devices_as_list()

        # Inject Matter-only groups that the DeviceManager doesn't
        # know about (no LIFX emitters, only matter: members).
        dm_groups: set[str] = {
            d.get("label", "") for d in devices if d.get("is_group")
        }
        config_groups: dict[str, list[str]] = (
            self.device_manager._group_config
        )
        for gname, members in config_groups.items():
            if gname in dm_groups:
                continue  # Already has an emitter.
            # Check if this group has any matter: members.
            matter_members: list[str] = [
                m for m in members if m.startswith("matter:")
            ]
            if not matter_members:
                continue
            # Derive group power from member states.
            matter_a: Any = getattr(self.server, "_matter_adapter", None)
            group_power: Optional[bool] = None
            if matter_a is not None:
                states: list[bool] = []
                for m in matter_members:
                    ps: Optional[bool] = matter_a.get_power_state(m[7:])
                    if ps is not None:
                        states.append(ps)
                if states:
                    group_power = any(states)
            devices.append({
                "ip": f"group:{gname}",
                "label": gname,
                "nickname": None,
                "product": "Matter Group",
                "zones": len(matter_members),
                "is_multizone": True,
                "is_matrix": False,
                "current_effect": None,
                "source": None,
                "overridden": False,
                "is_group": True,
                "is_grid": False,
                "mac": "",
                "group": gname,
                "member_ips": members,
                "power": group_power,
            })

        self._send_json(200, {"devices": devices})


    def _handle_get_effects(self) -> None:
        """GET /api/effects — list effects with param metadata.

        Returns the effect registry: each effect's name, description,
        tunable parameters with min/max/default, and any saved
        user defaults from ``effect_defaults`` in the config.
        """
        effects: dict[str, Any] = self.device_manager.list_effects()
        self._send_json(200, {"effects": effects})


    def _handle_get_groups(self) -> None:
        """GET /api/groups — resolved device groups (IPs).

        Returns all device groups with members resolved to live IP
        addresses.  Config groups are defined with labels/MACs, but
        this endpoint returns IPs so CLI and API clients can connect
        directly.

        Response::

            {
                "groups": {
                    "porch": ["192.0.2.25", "192.0.2.26"],
                    "office": ["192.0.2.30"]
                }
            }
        """
        # Use the DeviceManager's resolved groups (labels → IPs).
        groups: dict[str, list[str]] = dict(self.device_manager._group_config)
        # Include schedule-specific groups so schedule dropdowns are complete.
        sched_groups: dict[str, Any] = self.config.get("schedule_groups", {})
        for name, entries in sched_groups.items():
            if not name.startswith("_") and name not in groups:
                groups[name] = entries
        self._send_json(200, {"groups": groups})


