"""Dashboard and /home UI endpoint handlers.

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

from server_constants import *  # All constants available
from operators import OperatorManager
from media import SignalBus
from schedule_utils import parse_time_spec as _parse_time_spec
from solar import sun_times


class DashboardHandlerMixin:
    """Dashboard and /home UI endpoint handlers."""

    def _handle_get_dashboard(self) -> None:
        """GET /dashboard — serve the static HTML dashboard page.

        Reads ``static/dashboard.html`` from the server's directory
        and returns it as ``text/html``.  Returns 404 if the file
        is missing.
        """
        dashboard_path: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "dashboard.html",
        )
        try:
            with open(dashboard_path, "r") as f:
                html: str = f.read()
            body: bytes = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # Prevent browser caching so dashboard updates deploy instantly.
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._send_json(404, {"error": "Dashboard page not found"})


    def _handle_get_home(self) -> None:
        """GET /home — serve the sensor display dashboard.

        Reads ``static/home.html`` from the server's directory
        and returns it as ``text/html``.  Display-only page showing
        time, sensor readings, and photos.
        """
        # static/ is at the project root, one level up from handlers/.
        home_path: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "home.html",
        )
        try:
            with open(home_path, "r") as f:
                html: str = f.read()
            body: bytes = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._send_json(404, {"error": "Home display page not found"})


    def _handle_get_home_photos(self) -> None:
        """GET /api/home/photos — list photos available for the home display.

        Scans ``static/photos/`` for image files and returns their
        filenames as a JSON array.  Returns an empty list if the
        directory does not exist.
        """
        # Allowed image extensions.
        IMAGE_EXTS: set[str] = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        photos_dir: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "photos",
        )
        photos: list[str] = []
        if os.path.isdir(photos_dir):
            for entry in sorted(os.listdir(photos_dir)):
                _, ext = os.path.splitext(entry)
                if ext.lower() in IMAGE_EXTS:
                    photos.append(entry)
        self._send_json(200, {"photos": photos})


    def _handle_get_home_lights(self) -> None:
        """GET /api/home/lights — auth-free light status for dashboard.

        Returns power state and current effect for every configured
        group, plus active schedule entries.  Designed for the
        display-only ``/home`` dashboard so it can show bedroom
        light state without requiring an auth token.

        Response::

            {
              "groups": {
                "bedroom": {
                  "power": true,
                  "effect": "aurora",
                  "members": 2
                }
              },
              "active_schedules": [
                {
                  "name": "bedroom night aurora",
                  "group": "bedroom",
                  "effect": "aurora"
                }
              ]
            }
        """
        devices: list[dict[str, Any]] = (
            self.device_manager.devices_as_list()
        )

        # Extract group summaries.
        groups: dict[str, dict[str, Any]] = {}
        for d in devices:
            if d.get("is_group"):
                label: str = d.get("label", "")
                groups[label] = {
                    "power": d.get("power", False),
                    "effect": d.get("current_effect"),
                    "members": len(d.get("member_ips", [])),
                }

        # Active schedule entries.
        config: dict[str, Any] = self.config
        specs: list[dict[str, Any]] = config.get("schedule", [])
        active_schedules: list[dict[str, Any]] = []

        if specs:
            lat: float = config.get("location", {}).get("latitude", 0.0)
            lon: float = config.get("location", {}).get("longitude", 0.0)
            now: datetime = datetime.now(timezone.utc).astimezone()
            utc_offset: timedelta = now.utcoffset()
            today: date = now.date()
            sun: SunTimes = sun_times(lat, lon, today, utc_offset)

            for spec in specs:
                if not spec.get("enabled", True):
                    continue
                if not _entry_runs_on_day(spec, today):
                    continue
                start_dt: Optional[datetime] = _parse_time_spec(
                    spec["start"], sun, today, utc_offset,
                )
                stop_dt: Optional[datetime] = _parse_time_spec(
                    spec["stop"], sun, today, utc_offset,
                )
                if start_dt is None or stop_dt is None:
                    continue
                if stop_dt <= start_dt:
                    stop_dt += timedelta(days=1)
                if start_dt <= now < stop_dt:
                    active_schedules.append({
                        "name": spec.get("name", ""),
                        "group": spec.get("group", ""),
                        "effect": spec.get("effect", ""),
                    })

        self._send_json(200, {
            "groups": groups,
            "active_schedules": active_schedules,
        })


    def _handle_get_home_locks(self) -> None:
        """GET /api/home/locks — lock state for the home display.

        Returns an array of lock objects for the /home dashboard.
        Each lock has an abbreviation (2-letter code for the circle),
        a display name, and a boolean locked state.

        The data comes from the server config's ``locks`` section.
        When a Vivint (or other) integration is active, it updates
        lock state in real time.  Without an integration, the config
        provides static initial state.

        Response::

            {
              "locks": [
                {"abbr": "FD", "name": "Front Door", "locked": true},
                {"abbr": "BD", "name": "Back Door", "locked": true},
                {"abbr": "SD", "name": "Side Door", "locked": false}
              ]
            }
        """
        # Read lock config.  The server config can define locks as:
        #   "locks": [
        #     {"abbr": "FD", "name": "Front Door"},
        #     {"abbr": "BD", "name": "Back Door"},
        #     {"abbr": "SD", "name": "Side Door"}
        #   ]
        # Live state is merged from the lock_state registry (populated
        # by Vivint poller, MQTT, or any other integration).
        lock_defs: list[dict[str, Any]] = self.config.get("locks", [])
        lock_state: dict[str, bool] = getattr(
            self.server, "_lock_state", {},
        )
        lm: Optional[Any] = self.lock_manager
        locks: list[dict[str, Any]] = []
        for lock in lock_defs:
            abbr: str = lock.get("abbr", "?")
            entry: dict[str, Any] = {
                "abbr": abbr,
                "name": lock.get("name", abbr),
                "locked": lock_state.get(abbr),
            }
            # Add battery and last-update timestamp from LockManager.
            if lm is not None:
                battery: Optional[int] = lm.get_battery(abbr)
                if battery is not None:
                    entry["battery"] = battery
                updated_at: Optional[float] = lm.get_updated_at(abbr)
                if updated_at is not None:
                    entry["updated_at"] = updated_at
            locks.append(entry)
        # Add occupancy state from LockManager/SignalBus.
        occupancy: str = "UNKNOWN"
        if lm is not None:
            occupancy = lm.get_occupancy_state()
        self._send_json(200, {"locks": locks, "occupancy": occupancy})


    def _handle_get_home_security(self) -> None:
        """GET /api/home/security — alarm + door sensor state for /home.

        Returns alarm panel state and door contact sensors.

        Response::

            {
              "alarm": "armed_stay",
              "doors": [
                {"name": "Front Door", "open": false, "battery": 68},
                {"name": "Back Door", "open": false, "battery": 36},
                {"name": "Side Door", "open": false, "battery": 86}
              ],
              "sensors": { ... all sensor states ... }
            }
        """
        va: Any = getattr(self.server, "_vivint_adapter", None)
        if va is None:
            self._send_json(200, {
                "alarm": "unknown",
                "doors": [],
                "sensors": {},
            })
            return

        status: dict[str, Any] = va.get_status()
        alarm_state: str = status.get("alarm_state") or "unknown"
        all_sensors: dict[str, Any] = status.get("sensors", {})

        # Extract door contact sensors (exit_entry type = 1).
        # sensor_type may be "exit_entry_1" (enum str) or "1" (int str).
        DOOR_SENSOR_TYPES: set[str] = {"exit_entry_1", "1"}
        doors: list[dict[str, Any]] = []
        for _key, sdata in sorted(all_sensors.items()):
            stype: str = str(sdata.get("sensor_type", ""))
            if stype in DOOR_SENSOR_TYPES:
                doors.append({
                    "name": sdata.get("name", _key),
                    "open": sdata.get("is_on", False),
                    "battery": sdata.get("battery"),
                })

        self._send_json(200, {
            "alarm": alarm_state,
            "doors": doors,
            "sensors": all_sensors,
        })


    def _handle_get_home_cameras(self) -> None:
        """GET /api/home/cameras — list configured camera channels.

        Response::

            {
              "cameras": [
                {"id": 0, "name": "Shed"},
                {"id": 1, "name": "Backyard"}
              ]
            }
        """
        nvr: Any = getattr(self.server, "_nvr_adapter", None)
        if nvr is None:
            self._send_json(200, {"cameras": []})
            return
        self._send_json(200, {"cameras": nvr.get_channels()})


    def _handle_get_home_camera_snapshot(self, channel_str: str) -> None:
        """GET /api/home/camera/{channel} — proxy a JPEG snapshot.

        Returns the cached JPEG snapshot for the given NVR channel.
        Content-Type is image/jpeg.  Returns 404 if no snapshot or
        503 if the NVR adapter is not running.

        Args:
            channel_str: The channel number as a string from the URL.
        """
        try:
            channel: int = int(channel_str.split("?")[0])
        except ValueError:
            self._send_json(400, {"error": "invalid channel"})
            return

        nvr: Any = getattr(self.server, "_nvr_adapter", None)
        if nvr is None:
            self._send_json(503, {"error": "NVR adapter not running"})
            return

        jpeg: Optional[bytes] = nvr.get_snapshot(channel)
        if jpeg is None:
            self._send_json(404, {"error": "no snapshot available"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(jpeg)
        except BrokenPipeError:
            # Client disconnected before receiving the snapshot —
            # common with kiosk polling.  Not an error.
            pass


    def _handle_get_home_occupancy(self) -> None:
        """GET /api/home/occupancy — current occupancy state.

        Response::

            {"state": "HOME"}   or   {"state": "AWAY"}
        """
        lm: Optional[Any] = self.lock_manager
        state: str = "UNKNOWN"
        if lm is not None:
            state = lm.get_occupancy_state()
        self._send_json(200, {"state": state})


    def _handle_get_operators(self) -> None:
        """GET /api/operators — list running operators with status.

        Response::

            {"operators": [{name, type, started, tick_mode, ...}, ...]}
        """
        om: Optional[OperatorManager] = self.operator_manager
        if om is not None:
            self._send_json(200, {"operators": om.get_status()})
        else:
            self._send_json(200, {"operators": []})


    def _handle_get_home_mode(self) -> None:
        """GET /api/home/mode — display mode for the /home dashboard.

        Returns ``{"dark": true}`` when the room lights are off and
        it is nighttime (between sunset and sunrise), indicating
        the dashboard should switch to a dark, low-brightness theme.

        Configuration in server.json::

            "home_display": {
                "location": "Living Room",
                "room_lights": ["Living Room"]
            }

        ``room_lights`` lists group names.  If ALL are powered off
        and the current time is between sunset and sunrise, dark
        mode is activated.
        """
        display_cfg: dict[str, Any] = self.config.get("home_display", {})
        room_groups: list[str] = display_cfg.get("room_lights", [])

        # --- Check if it's night (between sunset and sunrise). ---
        is_night: bool = False
        loc: dict[str, Any] = self.config.get("location", {})
        lat: float = loc.get("latitude", 0.0)
        lon: float = loc.get("longitude", 0.0)
        if lat or lon:
            now: datetime = datetime.now(timezone.utc).astimezone()
            utc_offset: timedelta = now.utcoffset()
            today: date = now.date()
            sun: SunTimes = sun_times(lat, lon, today, utc_offset)
            # Night = before sunrise or after sunset.
            if sun.sunrise and sun.sunset:
                is_night = now < sun.sunrise or now >= sun.sunset

        # --- Check if room lights are off. ---
        lights_off: bool = True
        if room_groups:
            devices: list[dict[str, Any]] = (
                self.device_manager.devices_as_list()
            )
            for d in devices:
                if not d.get("is_group"):
                    continue
                label: str = d.get("label", "")
                if label in room_groups and d.get("power"):
                    lights_off = False
                    break

        dark: bool = is_night and lights_off
        self._send_json(200, {
            "dark": dark,
            "is_night": is_night,
            "lights_off": lights_off,
            "location": display_cfg.get("location", ""),
        })


    def _handle_get_home_printer(self) -> None:
        """GET /api/home/printer — printer consumable and error state.

        Returns the last known printer status from the printer adapter.
        The adapter polls the Brother CSV endpoint periodically (default
        once per day).  Only returns data when a printer alert is active
        (toner_low, no_paper, jam, cover_open, drum_low) so the frontend
        can show/hide a popup card accordingly.

        Response::

            {
              "status": "ok",
              "name": "Brother HL-5470DW",
              "alerts": [],
              "details": { ... },
              "last_poll": 1711720000.0
            }
        """
        pa: Any = getattr(self.server, "_printer_adapter", None)
        if pa is None:
            self._send_json(200, {"status": "unconfigured", "alerts": []})
            return
        state: dict[str, Any] = pa.get_status()
        self._send_json(200, {
            "status": state.get("status", "unknown"),
            "name": state.get("name", ""),
            "alerts": state.get("details", {}).get("alerts", []),
            "details": state.get("details", {}),
            "last_poll": state.get("last_poll", 0),
        })


    def _handle_get_home_soil(self) -> None:
        """GET /api/home/soil — soil moisture sensor data.

        Returns all Zigbee soil moisture sensors with their latest
        readings.  The zigbee adapter publishes to the signal bus;
        this endpoint reads the bus for any signal with
        ``soil_moisture`` in its name.

        Response::

            {
              "sensors": [
                {
                  "name": "soil_sensor_1",
                  "soil_moisture": 42.0,
                  "temperature": 25.1,
                  "battery": 100.0,
                  "humidity": 55.0
                }
              ]
            }
        """
        bus: Any = self.signal_bus
        if bus is None:
            self._send_json(200, {"sensors": []})
            return

        # Scan the bus for signals matching soil sensor patterns.
        # Zigbee adapter writes: {device_name}:{property}
        sensors: dict[str, dict[str, Any]] = {}
        try:
            all_signals: dict[str, Any] = bus.snapshot()
        except Exception:
            all_signals = {}

        # Pass 1: identify soil sensors by presence of soil_moisture.
        for signal_name in all_signals:
            parts: list[str] = signal_name.split(":")
            if len(parts) == 2 and parts[1] == "soil_moisture":
                sensors[parts[0]] = {"name": parts[0]}

        # Pass 2: collect all properties for identified soil sensors.
        for signal_name, value in all_signals.items():
            parts = signal_name.split(":")
            if len(parts) != 2:
                continue
            device: str = parts[0]
            prop: str = parts[1]
            if device not in sensors:
                continue
            if prop == "battery":
                # Zigbee adapter normalizes battery to 0.0-1.0;
                # convert back to percentage for display.
                sensors[device]["battery"] = round(value * 100, 0)
            else:
                sensors[device][prop] = value

        result: list[dict[str, Any]] = sorted(
            sensors.values(), key=lambda s: s.get("name", ""),
        )
        self._send_json(200, {"sensors": result})

    def _handle_get_photo(self, filename: str) -> None:
        """GET /photos/{filename} — serve a photo from static/photos/.

        Validates the filename to prevent directory traversal,
        then serves the image with appropriate content type.
        """
        # Content types by extension.
        CONTENT_TYPES: dict[str, str] = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp",
        }
        # Reject any path traversal attempts.
        if "/" in filename or "\\" in filename or ".." in filename:
            self._send_json(400, {"error": "Invalid filename"})
            return
        _, ext = os.path.splitext(filename)
        ctype: str = CONTENT_TYPES.get(ext.lower(), "")
        if not ctype:
            self._send_json(400, {"error": "Unsupported image type"})
            return
        photo_path: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "photos", filename,
        )
        try:
            with open(photo_path, "rb") as f:
                data: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            # Cache photos for 5 minutes — they change rarely.
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self._send_json(404, {"error": f"Photo not found: {filename}"})


    def _save_config_field(self, key: str, value: Any) -> None:
        """Persist a single config field to the config file.

        Reads the config JSON, updates the given key, and writes back.
        Schedule entries are saved to the external schedule file if
        one is configured (``_schedule_path``).

        Serialized by ``_config_save_lock`` so concurrent saves on
        different keys do not clobber each other.

        Args:
            key:   Top-level config key to update.
            value: The new value.
        """
        with self._config_save_lock:
            # Route schedule writes to the schedule file if it exists.
            sched_path: Optional[str] = self.config.get("_schedule_path")
            if key == "schedule" and sched_path:
                try:
                    with open(sched_path, "r") as f:
                        sched_config: dict[str, Any] = json.load(f)
                    sched_config["schedule"] = value
                    with open(sched_path, "w") as f:
                        json.dump(sched_config, f, indent=4)
                        f.write("\n")
                except Exception as exc:
                    logging.exception(
                        "Failed to save schedule to '%s'",
                        sched_path,
                    )
                return

            config_path: Optional[str] = self.config_path
            if config_path is None:
                return
            try:
                with open(config_path, "r") as f:
                    config: dict[str, Any] = json.load(f)
                config[key] = value
                with open(config_path, "w") as f:
                    json.dump(config, f, indent=4)
                    f.write("\n")
            except Exception as exc:
                logging.warning(
                    "Failed to save config field '%s': %s",
                    key, exc, exc_info=True,
                )

    # -- Power monitoring ---------------------------------------------------

    def _handle_get_power_page(self) -> None:
        """GET /power — serve the power monitoring dashboard."""
        static_dir: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static",
        )
        path: str = os.path.join(static_dir, "power.html")
        try:
            with open(path, "rb") as f:
                content: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json(404, {"error": "power.html not found"})

    def _handle_get_power_readings(self) -> None:
        """GET /api/power/readings?device=X&hours=N&resolution=N"""
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        device: str = params.get("device", [None])[0]
        hours: float = float(params.get("hours", ["1"])[0])
        resolution: int = int(params.get("resolution", ["60"])[0])

        pl = self.power_logger
        if pl is None:
            self._send_json(200, {"readings": []})
            return
        readings = pl.query(device=device, hours=hours, resolution=resolution)
        self._send_json(200, {"readings": readings})

    def _handle_get_power_summary(self) -> None:
        """GET /api/power/summary?device=X&days=N"""
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        device: str = params.get("device", [None])[0]
        days: int = int(params.get("days", ["7"])[0])

        pl = self.power_logger
        if pl is None:
            self._send_json(200, {})
            return
        summary = pl.summary(device=device, days=days)
        self._send_json(200, summary)

    def _handle_get_power_devices(self) -> None:
        """GET /api/power/devices"""
        pl = self.power_logger
        if pl is None:
            self._send_json(200, {"devices": []})
            return
        self._send_json(200, {"devices": pl.devices()})

    # -- Helpers ------------------------------------------------------------


