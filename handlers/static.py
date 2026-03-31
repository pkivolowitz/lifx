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

from server_constants import *  # All constants available


class StaticHandlerMixin:
    """Static read-only endpoint handlers (status, devices, effects, groups)."""

    def _handle_get_status(self) -> None:
        """GET /api/status — server readiness and version.

        Returns a status object indicating whether initial device
        loading has completed.  Clients can poll this endpoint on
        connect and show a "loading devices" message until
        ``ready`` becomes ``true``.
        """
        ready: bool = self.device_manager.ready
        status: str = "ready" if ready else "loading"
        self._send_json(200, {
            "status": status,
            "ready": ready,
            "version": __version__,
        })


    def _handle_get_devices(self) -> None:
        """GET /api/devices — list all configured devices.

        Returns each device's IP, label, product name, zone count,
        group membership, power state, and current effect status.
        """
        devices: list[dict[str, Any]] = self.device_manager.devices_as_list()
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


