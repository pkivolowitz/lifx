"""Device discovery, identification, and system control handlers.

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

from server_constants import (
    COMMAND_IDENTIFY_MAX_DURATION,
    DEFAULT_FADE_MS,
    IDENTIFY_CYCLE_SECONDS,
    IDENTIFY_DURATION_SECONDS,
    IDENTIFY_FRAME_INTERVAL,
    IDENTIFY_MIN_BRI,
)
from device_registry import DeviceRegistry
from infrastructure.adapter_proxy import KeepaliveProxy


class DiscoveryHandlerMixin:
    """Device discovery, identification, and system control handlers."""

    def _handle_get_discovered_bulbs(self) -> None:
        """GET /api/discovered_bulbs — bulbs found via ARP keepalive.

        Returns the current in-memory set of discovered LIFX bulbs.
        Each entry includes IP and MAC address.  If the keepalive
        daemon is not running, returns an empty list.
        """
        daemon: Optional[KeepaliveProxy] = self.keepalive
        if daemon is None:
            self._send_json(200, {"discovered_bulbs": []})
            return
        reg: Optional[DeviceRegistry] = self.registry
        bulbs: list[dict[str, str]] = []
        for ip, mac in sorted(daemon.known_bulbs.items()):
            entry: dict[str, str] = {"ip": ip, "mac": mac}
            if reg is not None:
                label: Optional[str] = reg.mac_to_label(mac)
                if label is not None:
                    entry["label"] = label
            bulbs.append(entry)
        self._send_json(200, {"discovered_bulbs": bulbs})


    def _handle_delete_command_identify(self, ip: str) -> None:
        """DELETE /api/command/identify/{ip} — cancel a running identify pulse.

        Sets the stop event for any pulse currently running on *ip*.
        The pulse thread will power the device off and exit on its next
        loop iteration (within :data:`IDENTIFY_FRAME_INTERVAL` seconds).

        Returns 200 if a pulse was cancelled, 404 if none was running.
        """
        with GlowUpRequestHandler._command_identifies_lock:
            event: Optional[threading.Event] = (
                GlowUpRequestHandler._command_identifies.get(ip)
            )
        if event is None:
            self._send_json(404, {
                "error": f"No active identify pulse for {ip}"
            })
            return
        event.set()
        logging.info("API: command/identify — cancelled pulse on %s", ip)
        self._send_json(200, {"ip": ip, "cancelled": True})


    def _handle_get_command_identify_cancel_all(self) -> None:
        """GET /api/command/identify/cancel-all — cancel all active identify pulses.

        Emergency/cleanup endpoint: sets the stop event for every running
        identify pulse on every IP.  Returns the count of cancelled pulses.
        """
        with GlowUpRequestHandler._command_identifies_lock:
            ips_to_cancel: list[str] = list(
                GlowUpRequestHandler._command_identifies.keys()
            )
            for ip in ips_to_cancel:
                event: Optional[threading.Event] = (
                    GlowUpRequestHandler._command_identifies.get(ip)
                )
                if event is not None:
                    event.set()
                    logging.info(
                        "API: command/identify/cancel-all — cancelled pulse on %s",
                        ip,
                    )
        self._send_json(200, {"cancelled": len(ips_to_cancel)})


    def _handle_post_server_power_off_all(self) -> None:
        """POST /api/server/power-off-all — emergency bulk power-off.

        Powers off every device configured in ``server.json`` immediately
        with a 0ms transition.  Returns the count of devices sent the
        power-off command.

        This is a fire-and-forget emergency endpoint — failures on
        individual devices do not stop the power-off of others.
        """
        configured_ips: list[str] = list(self.device_manager._devices.keys())
        off_count: int = 0
        for ip in configured_ips:
            try:
                dev: LifxDevice = LifxDevice(ip)
                dev.set_power(False, duration_ms=0)
                off_count += 1
                logging.info("API: server/power-off-all — powered off %s", ip)
                dev.close()
            except Exception as exc:
                logging.warning(
                    "API: server/power-off-all — power-off failed for %s: %s",
                    ip, exc,
                )
        self._send_json(200, {"devices_off": off_count})


    def _handle_post_server_rediscover(self) -> None:
        """POST /api/server/rediscover — re-resolve groups and reload devices.

        Re-runs the label/MAC → IP resolution, rebuilds virtual group
        emitters, and probes all devices.  Equivalent to the startup
        sequence (steps 4–5) but without restarting the server.

        Use this after powering on new bulbs, adding devices to
        groups, or changing the device registry.
        """
        config: dict[str, Any] = self.config
        keepalive = self.keepalive
        device_reg: DeviceRegistry = self.registry
        dm: DeviceManager = self.device_manager

        # Lazy import — server.py imports this mixin, so top-level
        # import would be circular.
        from server import _get_groups, _resolve_config_groups

        # Re-resolve groups from config (picks up any API changes).
        raw_groups: dict[str, list[str]] = _get_groups(config)
        resolved_groups: dict[str, list[str]]
        device_ips: list[str]
        unresolved: list[tuple[str, str]]
        resolved_groups, device_ips, unresolved = (
            _resolve_config_groups(raw_groups, device_reg, keepalive)
        )

        for group_name, ident in unresolved:
            logging.warning(
                "Rediscover: group '%s' cannot resolve '%s'",
                group_name, ident,
            )

        # Auto-load registered devices not in any group.
        group_ip_set: set[str] = set(device_ips)
        mac_to_ip: dict[str, str] = keepalive.known_bulbs_by_mac
        for mac in device_reg.all_devices():
            ip: Optional[str] = mac_to_ip.get(mac)
            if ip is not None and ip not in group_ip_set:
                device_ips.append(ip)
                group_ip_set.add(ip)

        device_ips.sort()

        # Reload devices with the fresh resolution.
        dm._device_ips = device_ips
        dm._group_config = resolved_groups
        devices: list[dict[str, Any]] = dm.load_devices()
        dm.query_all_power_states()

        logging.info(
            "API: rediscover — %d devices, %d groups, %d unresolved",
            len(devices), len(resolved_groups), len(unresolved),
        )
        self._send_json(200, {
            "devices": len(devices),
            "groups": len(resolved_groups),
            "unresolved": len(unresolved),
        })

    # -- Adapter restart -----------------------------------------------------

    # Adapter attribute names on the server object, keyed by the
    # user-facing name used in the REST API and voice commands.
    _ADAPTER_ATTRS: dict[str, str] = {
        "zigbee": "_zigbee_adapter",
        "vivint": "_vivint_adapter",
        "nvr": "_nvr_adapter",
        "printer": "_printer_adapter",
        "mqtt": "_mqtt_bridge",
        "matter": "_matter_adapter",
    }

    def _handle_post_adapter_restart(self, name: str) -> None:
        """POST /api/adapters/{name}/restart — stop and start an adapter.

        Restarts a named adapter daemon thread.  Used by the voice
        "repair" command and the dashboard.  Adapters that are not
        present (not configured or import failed) return 404.

        Args:
            name: Adapter name (zigbee, vivint, nvr, printer, mqtt).
        """
        attr: Optional[str] = self._ADAPTER_ATTRS.get(name)

        if attr is None:
            self._send_json(404, {
                "error": f"Unknown adapter: {name}",
                "available": list(self._ADAPTER_ATTRS.keys()),
            })
            return

        adapter: Any = getattr(self.server, attr, None)
        if adapter is None:
            self._send_json(404, {
                "error": f"Adapter '{name}' is not configured or not loaded",
            })
            return

        try:
            # Stop the adapter (graceful shutdown).
            if hasattr(adapter, "stop"):
                adapter.stop()
            # Start it again.
            if hasattr(adapter, "start"):
                adapter.start()
            logging.info("API: restarted adapter '%s'", name)
            self._send_json(200, {
                "adapter": name,
                "restarted": True,
            })
        except Exception as exc:
            logging.error(
                "API: failed to restart adapter '%s': %s", name, exc,
            )
            self._send_json(500, {
                "adapter": name,
                "restarted": False,
                "error": str(exc),
            })

    # -- Matter device endpoints ---------------------------------------------

    def _handle_get_matter_devices(self) -> None:
        """GET /api/matter/devices — list Matter devices with state."""
        adapter: Any = getattr(self.server, "_matter_adapter", None)
        if adapter is None:
            self._send_json(200, {"devices": []})
            return

        status: dict[str, Any] = adapter.get_status()
        devices: list[dict[str, Any]] = []
        for name, info in status.get("devices", {}).items():
            devices.append({
                "name": name,
                "node_id": info.get("node_id"),
                "type": "switch",
                "power": adapter.get_power_state(name),
            })
        self._send_json(200, {"devices": devices})

    def _handle_post_matter_power(self, name: str) -> None:
        """POST /api/matter/{name}/power — on, off, or toggle a Matter device.

        Body: {"on": true/false} or {"action": "toggle"}

        Args:
            name: Matter device friendly name.
        """
        adapter: Any = getattr(self.server, "_matter_adapter", None)
        if adapter is None:
            self._send_json(503, {"error": "Matter adapter not running"})
            return

        body: dict[str, Any] = self._read_json_body() or {}
        action: str = body.get("action", "")

        if action == "toggle":
            ok: bool = adapter.toggle(name)
        elif body.get("on", True):
            ok = adapter.power_on(name)
        else:
            ok = adapter.power_off(name)

        if ok:
            self._send_json(200, {"device": name, "ok": True})
        else:
            self._send_json(400, {
                "error": f"Failed to control '{name}'",
            })

    # -- Registry handlers ---------------------------------------------------


    def _handle_get_command_discover(self) -> None:
        """GET /api/command/discover[?ip=X] — return discovered LIFX devices.

        Returns IPs and MACs of all bulbs currently detected by the keepalive
        daemon via ARP scan. The keepalive daemon confirms liveness by
        unicast ping every 15 seconds, so all returned devices are known to
        be on the network.

        Query parameters:
            ip: Optional specific device IP to filter. If omitted, all
                bulbs currently known to the keepalive daemon are returned.

        Response::

            {
                "devices": [
                    {
                        "ip":  "10.0.0.41",
                        "mac": "d0:73:d5:69:70:db"
                    },
                    ...
                ]
            }

        Results are returned immediately (no UDP query overhead) since the
        keepalive daemon maintains a live ARP-based device list.
        """
        qs: dict = parse_qs(urlparse(self.path).query)
        target_ip: Optional[str] = qs.get("ip", [None])[0]

        if target_ip is not None:
            if not _validate_device_id(target_ip):
                self._send_json(400, {"error": "Cannot resolve device identifier"})
                return
            # For specific IP, always return it (even if not in ARP cache yet).
            devices: list[dict] = [{"ip": target_ip, "mac": ""}]
        else:
            daemon: Optional[KeepaliveProxy] = self.keepalive
            if daemon is None:
                self._send_json(200, {"devices": []})
                return
            # Return all currently-known devices from ARP cache (no fresh queries).
            try:
                bulbs_snapshot: dict[str, str] = daemon.known_bulbs
                devices = [
                    {"ip": ip, "mac": mac}
                    for ip, mac in bulbs_snapshot.items()
                ]
            except Exception as exc:
                logging.warning(
                    "command/discover: failed to access keepalive daemon: %s",
                    exc,
                )
                devices = []

        # Enrich each device with metadata from the device manager and
        # registry label from the device registry.
        dm: DeviceManager = self.device_manager
        reg: Optional[DeviceRegistry] = self.registry
        for entry in devices:
            ip: str = entry.get("ip", "")
            mac: str = entry.get("mac", "")
            # Pull cached device info (label, product, group, zones).
            dev: Optional[LifxDevice] = dm.get_device(ip) if ip else None
            if dev is not None:
                entry["label"] = dev.label or ""
                entry["product"] = dev.product_name or ""
                entry["group"] = dev.group or ""
                entry["zones"] = dev.zone_count or 0
                if not mac:
                    entry["mac"] = dev.mac_str
                    mac = dev.mac_str
            # Pull registry label (user-assigned name) if available.
            if reg is not None and mac:
                entry["registry_label"] = reg.mac_to_label(mac) or ""

        # Append offline registry devices not found in the ARP cache.
        # These are registered but not currently on the network (e.g.
        # photo-eye-controlled bulbs powered off during the day).
        live_macs: set[str] = {e.get("mac", "") for e in devices}
        if reg is not None and target_ip is None:
            for r_mac, r_entry in reg.all_devices().items():
                if r_mac not in live_macs:
                    devices.append({
                        "ip": "",
                        "mac": r_mac,
                        "label": "",
                        "product": "",
                        "group": "",
                        "zones": 0,
                        "registry_label": r_entry.get("label", ""),
                        "offline": True,
                    })

        logging.info(
            "API: command/discover — returning %d device(s) (%d from ARP cache)",
            len(devices), len(live_macs),
        )
        self._send_json(200, {"devices": devices})


    def _handle_post_command_identify(self) -> None:
        """POST /api/command/identify — pulse any device by IP to locate it.

        Unlike ``POST /api/devices/{ip}/identify``, this endpoint works for
        any device reachable from the server — it does not require the device
        to be configured in ``server.json``.  Intended for use from client
        machines that cannot reach bulbs directly due to mesh router filtering.

        Request body::

            {
                "ip":       "10.0.0.41",
                "duration": 10.0        (optional, default IDENTIFY_DURATION_SECONDS)
            }

        Response::

            {
                "ip":          "10.0.0.41",
                "identifying": true,
                "duration":    10.0,
                "device": {
                    "ip":      "10.0.0.41",
                    "mac":     "d0:73:d5:69:70:db",
                    "label":   "Bedroom Neon",
                    "product": "LIFX Neon",
                    "zones":   100,
                    "group":   "bedroom"
                }
            }

        The pulse runs asynchronously — the response returns immediately
        while the bulb flashes.  The device is powered off when the
        duration expires.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        raw_ident: Any = body.get("ip") or body.get("device")
        if not raw_ident or not isinstance(raw_ident, str):
            self._send_json(400, {
                "error": "Missing or invalid 'ip' or 'device' field"
            })
            return
        ip: Optional[str] = self._resolve_device_id(raw_ident)
        if ip is None or not _validate_device_id(ip):
            self._send_json(400, {
                "error": f"Cannot resolve device '{raw_ident}'"
            })
            return

        duration: float = float(body.get("duration", IDENTIFY_DURATION_SECONDS))
        if not (0 < duration <= COMMAND_IDENTIFY_MAX_DURATION):
            self._send_json(400, {
                "error": (
                    f"'duration' must be between 0 and "
                    f"{COMMAND_IDENTIFY_MAX_DURATION} seconds"
                )
            })
            return

        # Device is confirmed alive by keepalive daemon ARP ping.
        # Return immediately without blocking on UDP query.
        device_info: dict = {
            "ip":      ip,
            "mac":     "",
            "label":   "",
            "product": "",
            "zones":   0,
            "group":   "",
        }
        dev: LifxDevice = LifxDevice(ip)

        stop_event: threading.Event = threading.Event()

        # Cancel any pulse already running on this IP before starting a new one.
        with GlowUpRequestHandler._command_identifies_lock:
            existing: Optional[threading.Event] = (
                GlowUpRequestHandler._command_identifies.get(ip)
            )
            if existing is not None:
                existing.set()
                logging.info(
                    "API: command/identify — cancelled existing pulse on %s", ip,
                )
            GlowUpRequestHandler._command_identifies[ip] = stop_event

        def _pulse() -> None:
            """Sine-wave brightness pulse loop running in a daemon thread."""
            try:
                dev.set_power(True, duration_ms=0)
                start: float = time_mod.monotonic()
                while (
                    not stop_event.is_set()
                    and time_mod.monotonic() - start < duration
                ):
                    elapsed: float = time_mod.monotonic() - start
                    phase: float = (
                        math.sin(
                            2.0 * math.pi * elapsed / IDENTIFY_CYCLE_SECONDS
                        ) + 1.0
                    ) / 2.0
                    bri_frac: float = (
                        IDENTIFY_MIN_BRI + phase * (1.0 - IDENTIFY_MIN_BRI)
                    )
                    bri: int = int(bri_frac * HSBK_MAX)
                    if dev.is_multizone:
                        dev.set_zones(
                            [(0, 0, bri, KELVIN_DEFAULT)] * (dev.zone_count or 1),
                            duration_ms=0,
                        )
                    else:
                        dev.set_color(0, 0, bri, KELVIN_DEFAULT, duration_ms=0)
                    stop_event.wait(timeout=IDENTIFY_FRAME_INTERVAL)
                dev.set_power(False, duration_ms=DEFAULT_FADE_MS)
            except Exception as exc:
                logging.warning(
                    "command/identify pulse failed for %s: %s", ip, exc,
                )
            finally:
                dev.close()
                with GlowUpRequestHandler._command_identifies_lock:
                    # Only remove our own entry — don't clobber a newer pulse.
                    if GlowUpRequestHandler._command_identifies.get(ip) is stop_event:
                        del GlowUpRequestHandler._command_identifies[ip]

        thread: threading.Thread = threading.Thread(
            target=_pulse, daemon=True, name=f"cmd-identify-{ip}",
        )
        thread.start()
        logging.info(
            "API: command/identify — pulsing %s for %.1fs", ip, duration,
        )
        self._send_json(200, {
            "ip":          ip,
            "identifying": True,
            "duration":    duration,
            "device":      device_info,
        })


