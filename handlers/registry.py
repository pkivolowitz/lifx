"""Device registry handlers (MAC/label management).

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
from device_registry import DeviceRegistry
from infrastructure.bulb_keepalive import BulbKeepAlive


class RegistryHandlerMixin:
    """Device registry handlers (MAC/label management)."""

    def _handle_get_registry(self) -> None:
        """GET /api/registry — list all registered devices with live status.

        Returns the full device registry merged with live ARP data so
        each entry includes the current IP and online/offline status.
        """
        reg: Optional[DeviceRegistry] = self.registry
        if reg is None:
            self._send_json(200, {"devices": {}})
            return

        devices: dict[str, dict] = reg.all_devices()
        daemon: Optional[BulbKeepAlive] = self.keepalive
        mac_to_ip: dict[str, str] = {}
        if daemon is not None:
            mac_to_ip = daemon.known_bulbs_by_mac

        result: list[dict] = []
        for mac, entry in sorted(devices.items()):
            ip: str = mac_to_ip.get(mac, "")
            result.append({
                "mac": mac,
                "label": entry.get("label", ""),
                "notes": entry.get("notes", ""),
                "ip": ip,
                "online": bool(ip),
            })

        self._send_json(200, {"devices": result, "count": len(result)})


    def _handle_post_registry_device(self) -> None:
        """POST /api/registry/device — add or update a device.

        Accepts ``{"mac": "...", "label": "...", "notes": "..."}``
        or ``{"ip": "...", "label": "...", "notes": "..."}`` where
        the IP is resolved to a MAC via the ARP table.

        After registering, optionally writes the label to the bulb
        firmware via SetLabel if the device is online.
        """
        body: Optional[dict] = self._read_json_body()
        if body is None:
            return

        reg: Optional[DeviceRegistry] = self.registry
        if reg is None:
            self._send_json(500, {"error": "Registry not loaded"})
            return

        label: str = body.get("label", "").strip()
        notes: str = body.get("notes", "").strip()
        mac: str = body.get("mac", "").strip().lower()
        ip_arg: str = body.get("ip", "").strip()

        # Resolve IP to MAC if no MAC provided.  When both mac and ip
        # are given, the caller knows the static IP of an offline device
        # — skip the ARP lookup entirely.
        if not mac and ip_arg:
            daemon: Optional[BulbKeepAlive] = self.keepalive
            if daemon is not None:
                bulbs: dict[str, str] = daemon.known_bulbs
                mac = bulbs.get(ip_arg, "")

        if not mac:
            self._send_json(400, {"error": "No MAC address — provide mac or a reachable ip"})
            return

        if not label:
            self._send_json(400, {"error": "Label is required"})
            return

        force: bool = bool(body.get("force", False))

        try:
            reg.add_device(mac, label, notes, force=force, ip=ip_arg)
            reg.save()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        # Resolve MAC to IP for firmware label write.
        result_ip: str = ip_arg
        if not result_ip:
            daemon = self.keepalive
            if daemon is not None:
                result_ip = daemon.ip_for_mac(mac) or ""

        # Write label to bulb firmware if online.
        firmware_written: bool = False
        if result_ip:
            try:

                tmp_dev: LifxDevice = LifxDevice(result_ip)
                tmp_dev.sock.settimeout(SOCKET_TIMEOUT)
                firmware_written = tmp_dev.set_label(label)
                tmp_dev.close()
                # Update the cached device's label so discover reflects
                # the new name without requiring a server restart.
                if firmware_written:
                    cached_dev: Optional[LifxDevice] = (
                        self.device_manager.get_device(result_ip)
                    )
                    if cached_dev is not None:
                        cached_dev.label = label
            except Exception as exc:
                logging.warning(
                    "Failed to write label to %s (%s): %s",
                    result_ip, mac, exc,
                )

        self._send_json(200, {
            "mac": mac,
            "label": label,
            "ip": result_ip,
            "firmware_written": firmware_written,
        })


    def _handle_delete_registry_device(self, mac: str) -> None:
        """DELETE /api/registry/device/{mac} — remove a device.

        Args:
            mac: URL-decoded MAC address or label.
        """
        reg: Optional[DeviceRegistry] = self.registry
        if reg is None:
            self._send_json(500, {"error": "Registry not loaded"})
            return

        if reg.remove_device(mac):
            reg.save()
            self._send_json(200, {"removed": mac})
        else:
            self._send_json(404, {"error": f"Device not found: {mac}"})


    def _handle_post_registry_push_labels(self) -> None:
        """POST /api/registry/push-labels — write all labels to bulb firmware.

        Iterates the registry, resolves each MAC to IP via ARP, and sends
        SetLabel to each online device.
        """
        reg: Optional[DeviceRegistry] = self.registry
        if reg is None:
            self._send_json(500, {"error": "Registry not loaded"})
            return

        daemon: Optional[BulbKeepAlive] = self.keepalive
        mac_to_ip: dict[str, str] = {}
        if daemon is not None:
            mac_to_ip = daemon.known_bulbs_by_mac

        devices: dict[str, dict] = reg.all_devices()
        results: list[dict] = []

        for mac, entry in sorted(devices.items()):
            label: str = entry.get("label", "")
            if not label:
                continue
            ip: str = mac_to_ip.get(mac, "")
            if not ip:
                results.append({"mac": mac, "label": label, "status": "offline"})
                continue

            try:

                dev: LifxDevice = LifxDevice(ip)
                dev.sock.settimeout(SOCKET_TIMEOUT)
                ok: bool = dev.set_label(label)
                dev.close()
                results.append({
                    "mac": mac, "label": label, "ip": ip,
                    "status": "ok" if ok else "timeout",
                })
            except Exception as exc:
                results.append({
                    "mac": mac, "label": label, "ip": ip,
                    "status": f"error: {exc}",
                })

        self._send_json(200, {"results": results})


    def _handle_post_registry_push_label(self) -> None:
        """POST /api/registry/push-label — write one label to one bulb.

        Accepts ``{"mac": "...", "label": "..."}`` or
        ``{"ip": "...", "label": "..."}``.

        An empty label clears the firmware label on the device.
        """
        body: Optional[dict] = self._read_json_body()
        if body is None:
            return

        label: str = body.get("label", "").strip()
        mac: str = body.get("mac", "").strip().lower()
        ip_arg: str = body.get("ip", "").strip()

        if not label and not ip_arg and not mac:
            self._send_json(400, {"error": "IP or MAC is required"})
            return

        # Resolve to IP.
        target_ip: str = ip_arg
        if not target_ip and mac:
            daemon: Optional[BulbKeepAlive] = self.keepalive
            if daemon is not None:
                target_ip = daemon.ip_for_mac(mac) or ""

        if not target_ip:
            self._send_json(400, {"error": "Device offline or no IP/MAC provided"})
            return

        try:
            from transport import LifxDevice, SOCKET_TIMEOUT
            dev: LifxDevice = LifxDevice(target_ip)
            dev.sock.settimeout(SOCKET_TIMEOUT)
            ok: bool = dev.set_label(label)
            dev.close()
            self._send_json(200, {
                "ip": target_ip, "label": label, "firmware_written": ok,
            })
        except Exception as exc:
            self._send_json(500, {"error": f"SetLabel failed: {exc}"})

    # -- Command handlers --------------------------------------------------


