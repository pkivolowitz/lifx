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

# server_constants not used in this module.
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


    def _handle_get_vivint_page(self) -> None:
        """GET /vivint — serve the full Vivint status dashboard page.

        Reads ``static/vivint.html`` and returns it as text/html.
        The page fetches ``/api/home/vivint`` (unauthenticated) and
        renders the complete adapter state: alarm panel, locks, and
        every sensor grouped by parent detector with full metadata.
        """
        vivint_path: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "vivint.html",
        )
        try:
            with open(vivint_path, "r") as f:
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
            self._send_json(404, {"error": "Vivint dashboard page not found"})


    def _handle_get_home_vivint(self) -> None:
        """GET /api/home/vivint — full Vivint adapter state, no auth.

        Returns the complete vivint adapter status dict (alarm panel,
        locks, sensors with all metadata) for the unauthenticated
        /vivint dashboard page.  Safe to expose: contains no secrets,
        no passwords, no remote-control endpoints — purely read-only
        state already visible on the control panel in the hallway.
        """
        va: Any = getattr(self.server, "_vivint_adapter", None)
        if va is None:
            self._send_json(200, {
                "connected": False,
                "alarm_state": "unknown",
                "locks": {},
                "sensors": {},
            })
            return
        self._send_json(200, va.get_status())


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
        # Channel list is in the proxy's cached heartbeat status.
        status: dict[str, Any] = nvr.get_status()
        self._send_json(200, {"cameras": status.get("channels", [])})


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

        # Fetch snapshot from the NVR process's HTTP sidecar.
        # The sidecar port is in the proxy's heartbeat status.
        nvr_status: dict[str, Any] = nvr.get_status()
        sidecar_port: int = nvr_status.get("sidecar_port", 8421)
        sidecar_url: str = (
            f"http://localhost:{sidecar_port}/snapshot/{channel}"
        )
        try:
            import urllib.request
            req: urllib.request.Request = urllib.request.Request(sidecar_url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                jpeg: bytes = resp.read()
        except Exception:
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


    # --- Binding CRUD endpoints -------------------------------------------

    def _handle_get_bindings(self) -> None:
        """GET /api/signals/bindings — list all active param bindings.

        Response::

            {"bindings": [
                {"operator": "occ", "param": "away_confirm_seconds",
                 "target": "occ:away_confirm_seconds",
                 "source": "house:occupancy:state",
                 "scale": [5.0, 1.0]},
                ...
            ]}
        """
        om: Optional[OperatorManager] = self.operator_manager
        if om is not None:
            self._send_json(200, {"bindings": om.get_all_bindings()})
        else:
            self._send_json(200, {"bindings": []})

    def _handle_post_binding(self) -> None:
        """POST /api/signals/bindings — create or replace a binding.

        Request body::

            {"operator": "cylon_runner", "param": "speed",
             "signal": "breathe_runner:speed",
             "scale": [0.1, 30.0], "reduce": "max"}

        Responds 400 if the binding would create a cycle, the operator
        is not found, or the param does not exist.
        """
        om: Optional[OperatorManager] = self.operator_manager
        if om is None:
            self._send_json(503, {"error": "Operator manager not running"})
            return
        body: dict = self._read_json_body()
        if not body:
            self._send_json(400, {"error": "Missing request body"})
            return
        op_name: str = body.get("operator", "")
        param_name: str = body.get("param", "")
        source: str = body.get("signal", "")
        if not op_name or not param_name or not source:
            self._send_json(400, {
                "error": "Required fields: operator, param, signal",
            })
            return
        spec: dict = {"signal": source}
        if "scale" in body:
            spec["scale"] = body["scale"]
        if "reduce" in body:
            spec["reduce"] = body["reduce"]
        try:
            om.create_binding(op_name, param_name, spec)
            self._send_json(200, {"ok": True, "binding": {
                "target": f"{op_name}:{param_name}",
                "source": source,
            }})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})

    def _handle_delete_binding(self, target: str) -> None:
        """DELETE /api/signals/bindings/{target} — remove a binding.

        The *target* path segment is ``operator:param`` (e.g.,
        ``cylon_runner:speed``).  Param keeps its last bound value.
        """
        om: Optional[OperatorManager] = self.operator_manager
        if om is None:
            self._send_json(503, {"error": "Operator manager not running"})
            return
        parts: list[str] = target.split(":", 1)
        if len(parts) != 2:
            self._send_json(400, {
                "error": "Target must be operator:param (e.g., occ:speed)",
            })
            return
        op_name, param_name = parts
        try:
            om.remove_binding(op_name, param_name)
            self._send_json(200, {"ok": True})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})

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


    def _handle_get_nav_config(self) -> None:
        """GET /api/config/nav — navigation links for the site nav bar.

        Returns the list of nav links from server.json ``nav_links``.
        Pages build the nav bar dynamically from this endpoint so
        no internal IPs are hardcoded in HTML.

        Default links (Home, Dashboard, Power, Thermal, I/O,
        Shopping) are always included.  External links (e.g.,
        Zigbee2MQTT) come from config.
        """
        # Built-in pages — always present.
        links: list[dict[str, str]] = [
            {"label": "Home", "href": "/home"},
            {"label": "Dashboard", "href": "/dashboard"},
            {"label": "Power", "href": "/power"},
            {"label": "Thermal", "href": "/thermal"},
            {"label": "I/O", "href": "/io"},
            {"label": "Shopping", "href": "/shopping"},
            {"label": "Vivint", "href": "/vivint"},
        ]
        # External links from config.
        extra: list[dict[str, str]] = self.config.get("nav_links", [])
        links.extend(extra)
        self._send_json(200, {"links": links})


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

    def _handle_get_home_health(self) -> None:
        """GET /api/home/health — system health for the /home dashboard.

        Returns adapter status, device count, and schedule count
        in a compact format for the health scroller tile. Auth-free
        so the kiosk clock can poll it.

        Response::

            {
              "ready": true,
              "adapters": {"zigbee": true, "vivint": true, ...},
              "devices": 17,
              "schedules": 5
            }
        """
        # Adapter health — reuse the same logic as /api/status.
        adapter_health: dict[str, bool] = {}
        for attr, label in [
            ("_zigbee_adapter", "zigbee"),
            ("_vivint_adapter", "vivint"),
            ("_nvr_adapter", "nvr"),
            ("_printer_adapter", "printer"),
            ("_mqtt_bridge", "mqtt"),
            ("_matter_adapter", "matter"),
        ]:
            obj: Any = getattr(self.server, attr, None)
            if obj is not None:
                try:
                    info: dict[str, Any] = obj.get_status()
                    healthy: bool = (
                        info.get("running", False)
                        or info.get("connected", False)
                        or info.get("status") == "ok"
                    )
                    adapter_health[label] = healthy
                except Exception:
                    adapter_health[label] = False

        # Keepalive thread.
        ka: Any = getattr(self.__class__, "keepalive", None)
        if ka is not None:
            adapter_health["keepalive"] = ka.is_alive()

        # Scheduler thread.
        sched: Any = getattr(self.__class__, "scheduler", None)
        if sched is not None:
            adapter_health["scheduler"] = sched.is_alive()

        # Device count.
        device_count: int = 0
        try:
            dm: Any = self.device_manager
            devices: list = dm.devices_as_list()
            # Exclude groups — count physical devices only.
            device_count = sum(
                1 for d in devices if not d.get("is_group", False)
            )
        except Exception:
            pass

        # Schedule count — read from scheduler's config.
        schedule_count: int = 0
        sched_obj: Any = getattr(self.__class__, "scheduler", None)
        if sched_obj is not None:
            try:
                specs: list = sched_obj._config.get("schedule", [])
                schedule_count = sum(
                    1 for s in specs if s.get("enabled", True)
                )
            except Exception:
                pass

        self._send_json(200, {
            "ready": getattr(self.device_manager, "ready", False),
            "adapters": adapter_health,
            "devices": device_count,
            "schedules": schedule_count,
        })

    def _handle_get_io_page(self) -> None:
        """GET /io — serve the I/O timing dashboard."""
        static_dir: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static",
        )
        path: str = os.path.join(static_dir, "io.html")
        try:
            with open(path, "rb") as f:
                content: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json(404, {"error": "io.html not found"})

    def _handle_get_io_stats(self) -> None:
        """GET /api/io/stats — timed I/O histogram data per label.

        Returns per-label statistics: call count, timeout count,
        min/max/avg/p50/p95/p99 in milliseconds, and the assigned
        IO class.  Used by the IO dashboard to visualize blocking
        operation performance.

        Response::

            {
              "labels": {
                "lanscan.arp": {
                  "class": "FAST",
                  "count": 342,
                  "timeouts": 2,
                  "min_ms": 0.1,
                  "max_ms": 1800.0,
                  "avg_ms": 12.3,
                  "p50_ms": 8.1,
                  "p95_ms": 45.2,
                  "p99_ms": 180.0
                }
              }
            }
        """
        from infrastructure.timed_io import get_all_stats, WINDOW_SECONDS
        all_stats = get_all_stats()
        result: dict[str, dict[str, Any]] = {}
        for label, stats in all_stats.items():
            result[label] = {
                "class": stats.io_class.name,
                "window": {
                    "seconds": WINDOW_SECONDS,
                    "count": stats.window_count(),
                    "exceeded": stats.window_exceeded(),
                    "min_ms": round(stats.window_min_ms(), 1),
                    "max_ms": round(stats.window_max_ms(), 1),
                    "avg_ms": round(stats.window_avg_ms(), 1),
                    "stddev_ms": round(stats.window_stddev_ms(), 1),
                    "p50_ms": round(stats.window_percentile(0.50), 1),
                    "p95_ms": round(stats.window_percentile(0.95), 1),
                    "p99_ms": round(stats.window_percentile(0.99), 1),
                },
                "lifetime": {
                    "count": stats.count,
                    "exceeded": stats.timeout_count,
                    "min_ms": round(stats.min_ms, 1)
                        if stats.min_ms != float("inf") else 0.0,
                    "max_ms": round(stats.max_ms, 1),
                    "avg_ms": round(stats.avg_ms(), 1),
                    "stddev_ms": round(stats.stddev_ms(), 1),
                },
            }
        self._send_json(200, {"labels": result})

    def _handle_get_home_all(self) -> None:
        """GET /api/home/all — bundled response for the /home dashboard.

        Returns all tile data in a single JSON response, eliminating
        the need for 6+ separate HTTP requests per poll cycle.
        One connection, one thread, one response.

        Response::

            {
              "locks": { ... },
              "security": { ... },
              "health": { ... },
              "cameras": { ... },
              "printer": { ... },
              "soil": { ... },
              "occupancy": { ... }
            }
        """
        result: dict[str, Any] = {}

        # Locks.
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
            if lm is not None:
                battery: Optional[int] = lm.get_battery(abbr)
                if battery is not None:
                    entry["battery"] = battery
                updated_at: Optional[float] = lm.get_updated_at(abbr)
                if updated_at is not None:
                    entry["updated_at"] = updated_at
            locks.append(entry)
        occupancy: str = "UNKNOWN"
        if lm is not None:
            occupancy = lm.get_occupancy_state()
        result["locks"] = {"locks": locks, "occupancy": occupancy}

        # Security.
        va: Any = getattr(self.server, "_vivint_adapter", None)
        if va is not None:
            status: dict[str, Any] = va.get_status()
            alarm_state: str = status.get("alarm_state") or "unknown"
            all_sensors: dict[str, Any] = status.get("sensors", {})
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
            result["security"] = {
                "alarm": alarm_state, "doors": doors,
                "sensors": all_sensors,
            }
        else:
            result["security"] = {
                "alarm": "unknown", "doors": [], "sensors": {},
            }

        # Health.
        adapter_health: dict[str, bool] = {}
        for attr, label in [
            ("_zigbee_adapter", "zigbee"),
            ("_vivint_adapter", "vivint"),
            ("_nvr_adapter", "nvr"),
            ("_printer_adapter", "printer"),
            ("_mqtt_bridge", "mqtt"),
            ("_matter_adapter", "matter"),
        ]:
            obj: Any = getattr(self.server, attr, None)
            if obj is not None:
                try:
                    info: dict[str, Any] = obj.get_status()
                    healthy: bool = (
                        info.get("running", False)
                        or info.get("connected", False)
                        or info.get("status") == "ok"
                    )
                    adapter_health[label] = healthy
                except Exception:
                    adapter_health[label] = False
        ka: Any = getattr(self.__class__, "keepalive", None)
        if ka is not None:
            adapter_health["keepalive"] = ka.is_alive()
        sched: Any = getattr(self.__class__, "scheduler", None)
        if sched is not None:
            adapter_health["scheduler"] = sched.is_alive()
        device_count: int = 0
        try:
            dm: Any = self.device_manager
            devs: list = dm.devices_as_list()
            device_count = sum(
                1 for d in devs if not d.get("is_group", False)
            )
        except Exception:
            pass
        schedule_count: int = 0
        if sched is not None:
            try:
                specs: list = sched._config.get("schedule", [])
                schedule_count = sum(
                    1 for s in specs if s.get("enabled", True)
                )
            except Exception:
                pass
        result["health"] = {
            "ready": getattr(self.device_manager, "ready", False),
            "adapters": adapter_health,
            "devices": device_count,
            "schedules": schedule_count,
        }

        # Cameras.
        nvr: Any = getattr(self.server, "_nvr_adapter", None)
        if nvr is not None:
            nvr_st: dict[str, Any] = nvr.get_status()
            result["cameras"] = {"cameras": nvr_st.get("channels", [])}
        else:
            result["cameras"] = {"cameras": []}

        # Printer.
        pa: Any = getattr(self.server, "_printer_adapter", None)
        if pa is not None:
            pstate: dict[str, Any] = pa.get_status()
            result["printer"] = {
                "status": pstate.get("status", "unknown"),
                "name": pstate.get("name", ""),
                "alerts": pstate.get("details", {}).get("alerts", []),
                "details": pstate.get("details", {}),
                "last_poll": pstate.get("last_poll", 0),
            }
        else:
            result["printer"] = {"status": "unconfigured", "alerts": []}

        # Soil.
        bus: Any = self.signal_bus
        if bus is not None:
            try:
                all_signals: dict[str, Any] = bus.snapshot()
            except Exception:
                all_signals = {}
            sensors_map: dict[str, dict[str, Any]] = {}
            for signal_name in all_signals:
                parts: list[str] = signal_name.split(":")
                if len(parts) == 2 and parts[1] == "soil_moisture":
                    sensors_map[parts[0]] = {"name": parts[0]}
            for signal_name, meta in all_signals.items():
                parts = signal_name.split(":")
                if len(parts) == 2 and parts[0] in sensors_map:
                    sensors_map[parts[0]][parts[1]] = meta
            soil_list: list[dict[str, Any]] = list(sensors_map.values())
            result["soil"] = {"sensors": soil_list}
        else:
            result["soil"] = {"sensors": []}

        # Hints — derived flags for consumers (e.g. mbclock kiosk).
        # night_mode is produced by a CombineOperator reading
        # time:is_night AND NOT group:main_bedroom:any_on.  Consumers
        # should honor this instead of making their own time-of-day
        # decision so operator config (schedule, per-room gating) is
        # the single source of truth.
        hints: dict[str, Any] = {}
        if bus is not None:
            try:
                night_val = bus.read("kiosk:night_mode", 0.0)
                hints["night_mode"] = bool(float(night_val) >= 0.5)
            except (TypeError, ValueError):
                hints["night_mode"] = False
        result["hints"] = hints

        self._send_json(200, result)

    def _handle_get_static_js(self, filename: str) -> None:
        """GET /js/{filename} — serve a shared JavaScript file from static/js/.

        All dashboards share reusable client-side code (site nav bar,
        future shared widgets).  Mirrors ``_handle_get_photo`` for path
        validation and MIME handling.  Only ``.js`` files are served.
        Directory traversal is rejected.
        """
        # Reject any path traversal attempts.
        if "/" in filename or "\\" in filename or ".." in filename:
            self._send_json(400, {"error": "Invalid filename"})
            return
        # Only serve .js — this handler is not a general static server.
        if not filename.endswith(".js"):
            self._send_json(400, {"error": "Only .js files are served"})
            return
        js_path: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "js", filename,
        )
        try:
            with open(js_path, "rb") as f:
                data: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            # 5-minute cache — short enough for fast iteration,
            # long enough to matter on multi-tab kiosks.
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self._send_json(404, {"error": f"JS file not found: {filename}"})


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

    def _handle_get_power_plug_states(self) -> None:
        """GET /api/power/plug_states — live ON/OFF state for every smart plug.

        Proxies the zigbee_service REST endpoint on broker-2
        (``http://{broker}:8422/devices``) and distills its response
        into a device-keyed map.  The dashboard uses this to render
        the on/off toggle accurately — inferring state from power
        draw misreported any ON plug drawing under 1 W (dark TV, idle
        charger, empty outlet) as OFF on every refresh.

        The state chain (source of truth → UI) is::

            Zigbee plug relay (genOnOff attribute)
              → Z2M publishes on zigbee2mqtt/{device}
              → zigbee_service maintains in-memory DeviceState
              → HTTP /devices returns {state, power_w, online, ...}
              → this proxy strips to {state, power_w, online, age_sec}
              → /power.html renders the toggle

        Returns::

            {
              "plugs": {
                "LRTV":     {"state": "ON",  "power_w": 0.0, "online": true,  "age_sec": 12.3},
                "BYIR":     {"state": "ON",  "power_w": 2.3, "online": true,  "age_sec":  3.1},
                "ML_Power": {"state": null,  "power_w": null, "online": false, "age_sec": 24838.9}
              },
              "source": "http://10.0.0.123:8422/devices"
            }

        On proxy failure the endpoint still returns 200 with
        ``{"plugs": {}, "error": "..."}`` so the dashboard degrades
        gracefully rather than breaking the whole page.
        """
        # Broker-2 owns Zigbee end-to-end (commit 1d3d8df).  Its
        # HTTP host is the same as its MQTT broker host in config.
        zigbee_cfg: dict[str, Any] = self.config.get("zigbee", {}) or {}
        broker_host: str = zigbee_cfg.get("broker", "localhost")
        # Port 8422 is the zigbee_service default (GLZ_HTTP_PORT).
        zigbee_http_port: int = int(zigbee_cfg.get("http_port", 8422))
        url: str = f"http://{broker_host}:{zigbee_http_port}/devices"

        import urllib.request
        import urllib.error
        plugs: dict[str, dict[str, Any]] = {}
        try:
            # Short timeout — the dashboard refreshes this; if broker-2
            # is unreachable we return an empty map rather than stall.
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                raw: bytes = resp.read()
            data: dict[str, Any] = json.loads(raw)
            for dev in data.get("devices", []):
                name: Optional[str] = dev.get("name")
                if not isinstance(name, str) or not name:
                    continue
                # Only expose plugs — devices that carry a ``state``
                # attribute at all (sensors do not).  A null state on
                # an offline device is still reported so the UI can
                # show it as greyed-out rather than dropping it.
                plugs[name] = {
                    "state": dev.get("state"),
                    "power_w": dev.get("power_w"),
                    "online": bool(dev.get("online", False)),
                    "age_sec": dev.get("age_sec"),
                }
            self._send_json(200, {"plugs": plugs, "source": url})
        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError, ValueError) as exc:
            # Fail open — dashboard keeps working, just without
            # authoritative state.
            self._send_json(200, {
                "plugs": {},
                "source": url,
                "error": f"{type(exc).__name__}: {exc}",
            })

    # ---- Thermal dashboard ------------------------------------------------

    def _handle_get_thermal_page(self) -> None:
        """GET /thermal — serve the fleet thermal grid HTML.

        Rigid columnar dashboard showing every node's most recent
        thermal sample, false-colored by CPU temperature.
        """
        static_dir: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "static",
        )
        path: str = os.path.join(static_dir, "thermal.html")
        try:
            with open(path, "rb") as f:
                content: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json(404, {"error": "thermal.html not found"})

    def _handle_get_thermal_detail_page(self, node_id: str) -> None:
        """GET /thermal/host/{node_id} — per-host detail HTML.

        The HTML file is static; ``node_id`` is read client-side from
        ``location.pathname`` so there is no templating here.  We
        ignore the captured ``node_id`` — it is validated at query
        time by the ``/api/thermal/readings`` handler.

        Args:
            node_id: The captured URL segment (unused at this layer).
        """
        del node_id  # Consumed by the client-side script, not here.
        static_dir: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "static",
        )
        path: str = os.path.join(static_dir, "thermal_detail.html")
        try:
            with open(path, "rb") as f:
                content: bytes = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json(404, {"error": "thermal_detail.html not found"})

    def _handle_get_thermal_latest(self) -> None:
        """GET /api/thermal/latest — fleet snapshot.

        Returns a dict keyed by node_id with the most recent row for
        each known host.  The fleet dashboard polls this every 5
        seconds to refresh the grid.
        """
        tl: Any = getattr(self, "thermal_logger", None)
        if tl is None:
            self._send_json(200, {"hosts": {}})
            return
        self._send_json(200, {"hosts": tl.latest()})

    def _handle_get_thermal_hosts(self) -> None:
        """GET /api/thermal/hosts — distinct node_ids with any data."""
        tl: Any = getattr(self, "thermal_logger", None)
        if tl is None:
            self._send_json(200, {"hosts": []})
            return
        self._send_json(200, {"hosts": tl.hosts()})

    def _handle_get_thermal_readings(self) -> None:
        """GET /api/thermal/readings?node=X&hours=N&resolution=N.

        Returns a time-bucketed history for a single node, for the
        per-host detail page charts.  ``hours`` and ``resolution``
        default to 1 hour at 60-second resolution to match the
        default dashboard range.
        """
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        node: Optional[str] = params.get("node", [None])[0]
        if not node:
            self._send_json(400, {"error": "node parameter required"})
            return
        hours: float = float(params.get("hours", ["1"])[0])
        resolution: int = int(params.get("resolution", ["60"])[0])

        tl: Any = getattr(self, "thermal_logger", None)
        if tl is None:
            self._send_json(200, {"readings": []})
            return
        readings = tl.query(node_id=node, hours=hours, resolution=resolution)
        self._send_json(200, {"readings": readings})

    def _handle_post_zigbee_set(self) -> None:
        """POST /api/zigbee/set — send a command to a Zigbee device.

        Publishes a payload to zigbee2mqtt/{device}/set via the
        Zigbee adapter's MQTT connection.

        Request body::

            {"device": "LRTV", "payload": {"state": "ON"}}
        """
        body: dict[str, Any] = self._read_json_body()
        if not body:
            self._send_json(400, {"error": "missing JSON body"})
            return

        device: str = body.get("device", "")
        payload: dict[str, Any] = body.get("payload", {})

        if not device or not payload:
            self._send_json(400, {"error": "device and payload required"})
            return

        proxy: Any = getattr(self.server, "_zigbee_adapter", None)
        if proxy is None or not hasattr(proxy, "send_command"):
            self._send_json(503, {"error": "Zigbee adapter not available"})
            return

        try:
            result: dict[str, Any] = proxy.send_command(
                "send", {"device": device, "payload": payload},
            )
            if result.get("status") == "ok":
                self._send_json(200, {"status": "ok", "device": device})
            else:
                self._send_json(502, {
                    "error": result.get("error", f"failed to send to {device}"),
                })
        except TimeoutError:
            self._send_json(504, {"error": "Zigbee adapter timed out"})

    # -- Helpers ------------------------------------------------------------


