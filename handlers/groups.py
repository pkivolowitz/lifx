"""Group CRUD handlers (create, update, delete).

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


class GroupHandlerMixin:
    """Group CRUD handlers (create, update, delete)."""

    def _handle_post_group_create(self) -> None:
        """POST /api/groups — create a new device group.

        Request body::

            {
                "name": "porch",
                "members": ["192.0.2.25", "192.0.2.26"]
            }

        Saves to the ``groups`` config section (the server's device
        groups, not ``schedule_groups`` which is for standalone
        scheduler use).  Updates the runtime group config so the next
        discovery cycle picks up the new group.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        errors: list[str] = []

        name: str = body.get("name", "").strip()
        if not name:
            errors.append("Group name is required")
        elif name.startswith("_"):
            errors.append("Group name must not start with '_'")

        members: Any = body.get("members", [])
        if not isinstance(members, list) or len(members) == 0:
            errors.append("At least one member device is required")
        elif not all(isinstance(m, str) and m.strip() for m in members):
            errors.append("Each member must be a non-empty string")

        # Check for duplicate group name.
        existing_groups: dict[str, Any] = self.config.get("groups", {})
        if name and name in existing_groups:
            errors.append(f"Group '{name}' already exists")

        if errors:
            self._send_json(400, {"error": "; ".join(errors)})
            return

        # Sanitize member list — strip whitespace.
        clean_members: list[str] = [m.strip() for m in members]

        # Persist to config file and update in-memory config.
        all_groups: dict[str, Any] = dict(existing_groups)
        all_groups[name] = clean_members
        self._save_config_field("groups", all_groups)
        self.config["groups"] = all_groups

        # Update runtime group config AND rebuild the VirtualMultizone-
        # Emitter in a single critical section so no other thread can
        # observe the new _group_config entry without the matching
        # emitter in _emitters.  Previously this handler only updated
        # _group_config and waited for load_devices / Rediscover to
        # build the emitter — which meant newly-created groups were
        # silently dead until the user happened to run Rediscover.
        with self.device_manager._lock:
            self.device_manager._group_config[name] = clean_members
            self.device_manager._rebuild_group_emitter_locked(name)

        logging.info(
            "API: group '%s' created with %d member(s): %s",
            name, len(clean_members), ", ".join(clean_members),
        )
        self._send_json(201, {"name": name, "members": clean_members})


    def _handle_put_group_update(self, name: str) -> None:
        """PUT /api/groups/{name} — update an existing group.

        Request body::

            {
                "name": "new_name",
                "members": ["192.0.2.25", "192.0.2.26"]
            }

        The ``name`` field in the body is the new name (may be the
        same as the URL name for member-only changes).  Validates
        that the group exists, that the new name is valid, and that
        at least one member is provided.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        existing_groups: dict[str, Any] = self.config.get("groups", {})
        if name not in existing_groups:
            self._send_json(404, {"error": f"Group '{name}' not found"})
            return

        errors: list[str] = []

        new_name: str = body.get("name", "").strip()
        if not new_name:
            errors.append("Group name is required")
        elif new_name.startswith("_"):
            errors.append("Group name must not start with '_'")
        elif new_name != name and new_name in existing_groups:
            errors.append(f"Group '{new_name}' already exists")

        members: Any = body.get("members", [])
        if not isinstance(members, list) or len(members) == 0:
            errors.append("At least one member device is required")
        elif not all(isinstance(m, str) and m.strip() for m in members):
            errors.append("Each member must be a non-empty string")

        if errors:
            self._send_json(400, {"error": "; ".join(errors)})
            return

        clean_members: list[str] = [m.strip() for m in members]

        # Build updated groups dict — remove old name, add new.
        updated: dict[str, Any] = {
            k: v for k, v in existing_groups.items() if k != name
        }
        updated[new_name] = clean_members
        self._save_config_field("groups", updated)
        self.config["groups"] = updated

        # Update runtime group config AND rebuild the emitter under a
        # single lock.  On rename, the old group's emitter must be
        # torn down before the new one is built so stale group:<old>
        # entries do not linger in _emitters.  The rebuild helper
        # handles the teardown automatically when it sees the name
        # is no longer in _group_config.
        with self.device_manager._lock:
            self.device_manager._group_config.pop(name, None)
            self.device_manager._group_config[new_name] = clean_members
            if new_name != name:
                # Old name is gone from _group_config — helper will
                # drop the old group_id emitter cleanly.
                self.device_manager._rebuild_group_emitter_locked(name)
            self.device_manager._rebuild_group_emitter_locked(new_name)

        # Cascade rename into schedule entries that reference this group.
        renamed_count: int = 0
        if new_name != name:
            specs: list[dict[str, Any]] = self.config.get("schedule", [])
            for spec in specs:
                if spec.get("group") == name:
                    spec["group"] = new_name
                    renamed_count += 1
            if renamed_count > 0:
                self._save_config_field("schedule", specs)

        logging.info(
            "API: group '%s' updated%s — %d member(s): %s%s",
            name,
            f" (renamed to '{new_name}')" if new_name != name else "",
            len(clean_members),
            ", ".join(clean_members),
            f" — {renamed_count} schedule entries updated"
            if renamed_count > 0 else "",
        )
        self._send_json(200, {
            "name": new_name,
            "members": clean_members,
            "schedule_entries_updated": renamed_count,
        })


    def _handle_delete_group(self, name: str) -> None:
        """DELETE /api/groups/{name} — remove a device group.

        Validates the group exists, removes it from config and
        runtime state, and persists the change.
        """
        existing_groups: dict[str, Any] = self.config.get("groups", {})
        if name not in existing_groups:
            self._send_json(404, {"error": f"Group '{name}' not found"})
            return

        updated: dict[str, Any] = {
            k: v for k, v in existing_groups.items() if k != name
        }
        self._save_config_field("groups", updated)
        self.config["groups"] = updated

        # Remove from runtime group config AND tear down the emitter
        # in the same critical section.  The rebuild helper, seeing
        # the name is no longer in _group_config, drops the emitter,
        # stops any active controller, and clears override tracking.
        with self.device_manager._lock:
            self.device_manager._group_config.pop(name, None)
            self.device_manager._rebuild_group_emitter_locked(name)

        # Report any schedule entries that reference the deleted group.
        specs: list[dict[str, Any]] = self.config.get("schedule", [])
        orphaned: list[str] = [
            s.get("name", f"entry_{i}")
            for i, s in enumerate(specs) if s.get("group") == name
        ]

        logging.info(
            "API: group '%s' deleted%s", name,
            f" — {len(orphaned)} schedule entries now reference "
            f"a missing group: {', '.join(orphaned)}"
            if orphaned else "",
        )
        response: dict[str, Any] = {"deleted": name}
        if orphaned:
            response["warning"] = (
                f"{len(orphaned)} schedule entries still reference "
                f"this group: {', '.join(orphaned)}"
            )
        self._send_json(200, response)

    # ------------------------------------------------------------------
    # BLE sensor endpoints
    # ------------------------------------------------------------------


