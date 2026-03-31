"""Diagnostics query handlers (now-playing, history, state).

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


class DiagnosticsHandlerMixin:
    """Diagnostics query handlers (now-playing, history, state)."""

    def _handle_get_diag_now_playing(self) -> None:
        """GET /api/diagnostics/now_playing — effects currently playing.

        Returns open effect_history records (no ``stopped_at``).
        Falls back to an empty list if diagnostics is unavailable.
        """
        diag = self.device_manager._diag
        if diag is None or not _HAS_DIAGNOSTICS:
            self._send_json(200, [])
            return
        try:
            rows: list[dict[str, Any]] = diag.query_now_playing()
            self._send_json(200, rows)
        except Exception as exc:
            logging.warning("Diagnostics query failed: %s", exc)
            self._send_json(200, [])


    def _handle_get_diag_history(self) -> None:
        """GET /api/diagnostics/history — recent effect events.

        Returns the most recent 50 effect_history records (both
        open and closed).  Falls back to an empty list if diagnostics
        is unavailable.
        """
        diag = self.device_manager._diag
        if diag is None or not _HAS_DIAGNOSTICS:
            self._send_json(200, [])
            return
        try:
            rows: list[dict[str, Any]] = diag.query_history(limit=50)
            self._send_json(200, rows)
        except Exception as exc:
            logging.warning("Diagnostics query failed: %s", exc)
            self._send_json(200, [])


    def _handle_get_state(self) -> None:
        """GET /api/state — current ownership state of all known devices.

        Returns records written by both server.py and scheduler.py, showing
        which brain owns each device, what effect is running, and why.
        Falls back to an empty list if the state store is unavailable.
        """
        store = self.device_manager._state
        if store is None:
            self._send_json(200, [])
            return
        self._send_json(200, store.get_all())


