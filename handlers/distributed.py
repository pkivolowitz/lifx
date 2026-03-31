"""Distributed compute fleet and assignment handlers.

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


class DistributedHandlerMixin:
    """Distributed compute fleet and assignment handlers."""

    def _handle_get_fleet(self) -> None:
        """GET /api/fleet — distributed fleet status.

        Returns the orchestrator's fleet inventory: online nodes,
        capabilities, assignments, and allocated UDP ports.
        """
        orch: Optional[Any] = self.orchestrator
        if orch is None:
            self._send_json(200, {
                "enabled": False,
                "nodes": [],
                "node_count": 0,
                "message": "Distributed compute not configured",
            })
            return
        status: dict[str, Any] = orch.get_fleet_status()
        status["enabled"] = True
        self._send_json(200, status)


    def _handle_post_assign(self) -> None:
        """POST /api/assign — issue a work assignment to a compute node.

        Request body::

            {
                "node_id": "judy",
                "operator": "AudioExtractor",
                "config": {"source_name": "conway", "bands": 8},
                "inputs": [
                    {"signal_name": "conway:audio:pcm_raw",
                     "transport": "udp", "udp_port": 9420}
                ],
                "outputs": [
                    {"signal_name": "judy:audio:bands",
                     "transport": "mqtt"}
                ]
            }
        """
        orch: Optional[Any] = self.orchestrator
        if orch is None:
            self._send_json(503, {
                "error": "Distributed compute not configured",
            })
            return

        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        node_id: str = body.get("node_id", "")
        operator_name: str = body.get("operator", "")
        if not node_id or not operator_name:
            self._send_json(400, {
                "error": "Missing required fields: node_id, operator",
            })
            return

        # Import SignalBinding and WorkAssignment from distributed module.
        try:
            from distributed.orchestrator import SignalBinding, WorkAssignment
        except ImportError:
            self._send_json(503, {"error": "Distributed module not available"})
            return

        # Build input/output bindings.
        inputs: list[SignalBinding] = [
            SignalBinding.from_dict(b) for b in body.get("inputs", [])
        ]
        outputs: list[SignalBinding] = [
            SignalBinding.from_dict(b) for b in body.get("outputs", [])
        ]

        # Generate assignment ID.
        assignment_id: str = (
            f"{node_id}-{operator_name.lower()}-{int(time_mod.time())}"
        )

        assignment: WorkAssignment = WorkAssignment(
            assignment_id=assignment_id,
            operator_name=operator_name,
            operator_config=body.get("config", {}),
            inputs=inputs,
            outputs=outputs,
            action="start",
        )

        success: bool = orch.assign_work(node_id, assignment)
        if success:
            logging.info(
                "API: assigned '%s' to node '%s' (id: %s)",
                operator_name, node_id, assignment_id,
            )
            self._send_json(200, {
                "assigned": True,
                "assignment_id": assignment_id,
                "node_id": node_id,
                "operator": operator_name,
            })
        else:
            self._send_json(409, {
                "error": f"Cannot assign to node '{node_id}'",
                "assigned": False,
            })


    def _handle_post_cancel_assignment(self, node_id: str,
                                       assignment_id: str) -> None:
        """POST /api/assign/{node_id}/cancel/{assignment_id}."""
        orch: Optional[Any] = self.orchestrator
        if orch is None:
            self._send_json(503, {
                "error": "Distributed compute not configured",
            })
            return
        success: bool = orch.cancel_assignment(node_id, assignment_id)
        if success:
            self._send_json(200, {
                "cancelled": True,
                "assignment_id": assignment_id,
            })
        else:
            self._send_json(404, {
                "error": f"Assignment '{assignment_id}' not found",
            })


