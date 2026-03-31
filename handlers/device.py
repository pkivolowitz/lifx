"""Device control handlers (play, stop, power, identify, etc).

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
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional
from urllib.parse import unquote

from server_constants import *  # All constants available
from effects import MediaEffect
from emitters import Emitter
from engine import Controller
from media import MediaManager, SignalBus
from media.source import AudioStreamServer
from transport import LifxDevice, SendMode
from schedule_utils import find_active_entry as _find_active_entry
from server_utils import (
    is_group_id as _is_group_id,
    group_name_from_id as _group_name_from_id,
    get_groups as _get_groups,
)


class DeviceHandlerMixin:
    """Device control handlers (play, stop, power, identify, etc)."""

    def _handle_get_device_status(self, ip: str) -> None:
        """GET /api/devices/{ip}/status — device effect status.

        Returns the currently playing effect name, parameters, elapsed
        time, and override state for a single device.

        Args:
            ip: Device IP address (URL-decoded by dispatch).
        """
        try:
            status: dict[str, Any] = self.device_manager.get_status(ip)
            self._send_json(200, status)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})


    def _handle_get_device_colors(self, ip: str) -> None:
        """GET /api/devices/{ip}/colors — zone color snapshot.

        Queries the device for its current zone colors and returns them
        as a list of HSBK tuples.  Returns 503 if the device cannot
        be reached for a live query.

        Args:
            ip: Device IP address (URL-decoded by dispatch).
        """
        try:
            colors = self.device_manager.get_colors(ip)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})
            return

        if colors is None:
            self._send_json(503, {"error": "Could not query device colors"})
            return

        self._send_json(200, {"zones": colors})


    def _handle_get_device_colors_stream(self, ip: str) -> None:
        """GET /api/devices/{ip}/colors/stream — SSE color stream at 4 Hz.

        Creates a temporary :class:`LifxDevice` for read-only zone color
        queries to avoid socket contention with the engine.  The stream
        runs until the client disconnects.
        """
        em: Optional[Emitter] = self.device_manager.get_emitter(ip)
        if em is None:
            self._send_json(404, {"error": "Device not found"})
            return

        self._send_sse_headers()

        # Send an initial padding comment to flush Cloudflare's response
        # buffer.  Cloudflare Tunnel buffers small chunks; a ~4KB initial
        # payload forces the proxy to begin streaming immediately.
        padding: str = ": " + " " * 4096 + "\n\n"
        try:
            self.wfile.write(padding.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        stream_start: float = time_mod.time()
        try:
            while True:
                # Enforce maximum stream lifetime to prevent resource
                # exhaustion from abandoned connections.
                if time_mod.time() - stream_start > SSE_TIMEOUT_SECONDS:
                    break

                # Read colors from the engine's in-memory frame buffer.
                # Zero UDP overhead, zero socket contention.  When no
                # effect is running the frame is None and we skip the
                # event — the app shows "Connecting..." which is accurate.
                ctrl: Optional[Controller] = self.device_manager.get_controller(ip)
                if ctrl is not None:
                    colors = ctrl.get_last_frame()
                    if colors is not None:
                        payload: str = json.dumps({
                            "zones": [
                                {"h": h, "s": s, "b": b, "k": k}
                                for h, s, b, k in colors
                            ],
                        })
                        self.wfile.write(
                            f"data: {payload}\n\n".encode("utf-8"),
                        )
                        self.wfile.flush()

                time_mod.sleep(SSE_POLL_INTERVAL)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client disconnected — clean exit.
            pass

    # -- POST handlers ------------------------------------------------------


    def _handle_post_play(self, ip: str) -> None:
        """POST /api/devices/{ip}/play — start an effect.

        Request body::

            {
                "effect": "cylon",
                "params": {"speed": 2.0, "hue": 120},
                "bindings": {
                    "brightness": {
                        "signal": "backyard:audio:bass",
                        "scale": [20, 100]
                    }
                }
            }

        The optional ``bindings`` field maps parameter names to media
        signals.  Each binding specifies a signal name and optional
        ``scale`` (output range) and ``reduce`` (for array signals:
        ``"max"``, ``"mean"``, or ``"sum"``).
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        effect_name: Optional[str] = body.get("effect")
        if not effect_name or not isinstance(effect_name, str):
            self._send_json(400, {"error": "Missing or invalid 'effect'"})
            return

        params: dict[str, Any] = body.get("params", {})
        if not isinstance(params, dict):
            self._send_json(400, {"error": "'params' must be an object"})
            return

        # Extract optional signal bindings for media-reactive effects.
        bindings: Optional[dict[str, Any]] = body.get("bindings")
        if bindings is not None and not isinstance(bindings, dict):
            self._send_json(400, {"error": "'bindings' must be an object"})
            return

        # Optional source identifier — the client name that started
        # the effect (e.g. "Conway", "Perry's iPhone").
        source: Optional[str] = body.get("source")
        if source is not None and not isinstance(source, str):
            source = None

        # On-demand music directory — create a DirectorySource at runtime.
        music_dir: Optional[str] = body.get("music_dir")
        music_source_name: Optional[str] = None
        if music_dir and isinstance(music_dir, str):
            mm: Optional[MediaManager] = self.media_manager
            if mm is None:
                # No MediaManager — create one with a SignalBus.
                mm = MediaManager()
                mm.configure(self.config)
                GlowUpRequestHandler.media_manager = mm
            music_source_name = f"_music_{ip.replace(':', '_')}"
            bands: int = body.get("bands", 32)
            ok: bool = mm.add_source(music_source_name, {
                "type": "directory",
                "path": music_dir,
                "recursive": True,
                "sample_rate": 44100,
                "extractors": {"audio": {"bands": bands}},
            })
            if not ok:
                self._send_json(400, {
                    "error": f"Cannot start music from: {music_dir}"
                })
                return
            # Set the effect's source param to the dynamic source name.
            params["source"] = music_source_name

            # Clean up any stale audio stream server for this device.
            old_stream: Optional[AudioStreamServer] = (
                self.device_manager._audio_streams.pop(ip, None)
            )
            if old_stream is not None:
                old_stream.stop()

            # Start a TCP audio stream server so the CLI can play audio
            # locally via ffplay tcp://host:port.
            stream_srv: AudioStreamServer = AudioStreamServer()
            try:
                stream_srv.start()
            except OSError as exc:
                logging.warning(
                    "Audio stream port %d in use, retrying: %s",
                    stream_srv.port, exc,
                )
                # Port may be in TIME_WAIT — try the next one.
                stream_srv = AudioStreamServer(port=stream_srv.port + 1)
                stream_srv.start()
            # Register the streamer as an extractor on the source.
            with mm._lock:
                src = mm._sources.get(music_source_name)
            if src is not None:
                src.add_extractor(stream_srv.on_chunk)
            self.device_manager._audio_streams[ip] = stream_srv

            logging.info(
                "API: created music source '%s' from %s "
                "(audio stream on tcp port %d)",
                music_source_name, music_dir, stream_srv.port,
            )

        # Resolve signal bus — pass if we have bindings OR a media manager
        # (MediaEffects need the bus even without explicit bindings).
        signal_bus: Optional[SignalBus] = None
        if self.media_manager is not None:
            signal_bus = self.media_manager.bus

        try:
            active_entry: Optional[str] = self._get_active_entry_for_ip(ip)

            status: dict[str, Any] = self.device_manager.play(
                ip, effect_name, params,
                bindings=bindings, signal_bus=signal_bus,
                source=source,
            )
            # Track override AFTER successful play — if play fails,
            # the override must not persist (C15: override deadlock).
            self.device_manager.mark_override(ip, active_entry)
            # Track the dynamic music source so stop can clean it up.
            if music_source_name:
                self.device_manager._music_sources[ip] = music_source_name
                # Include the audio stream port in the response.
                stream: Optional[AudioStreamServer] = (
                    self.device_manager._audio_streams.get(ip)
                )
                if stream is not None:
                    status["audio_stream_port"] = stream.port
            else:
                self.device_manager._music_sources.pop(ip, None)
            logging.info(
                "API: playing '%s' on %s from %s (params: %s, bindings: %s)",
                effect_name, ip, source or "unknown",
                params,
                list(bindings.keys()) if bindings else None,
            )
            self._send_json(200, status)
        except KeyError:
            # Clean up music source if play failed.
            if music_source_name and self.media_manager:
                self.media_manager.remove_source(music_source_name)
            self._send_json(404, {"error": "Device not found"})
        except ValueError:
            if music_source_name and self.media_manager:
                self.media_manager.remove_source(music_source_name)
            self._send_json(400, {"error": "Invalid effect or parameters"})


    def _handle_post_stop(self, ip: str) -> None:
        """POST /api/devices/{ip}/stop — stop the current effect.

        Stops the effect engine, powers off the device, and sets a
        scheduler override so the schedule does not immediately restart
        the effect on its next poll cycle.

        Args:
            ip: Device IP address (URL-decoded by dispatch).
        """
        try:
            # Set override if not already set so the scheduler doesn't
            # immediately restart the effect on its next poll cycle.
            if not self.device_manager.is_overridden(ip):
                active_entry: Optional[str] = self._get_active_entry_for_ip(ip)
                self.device_manager.mark_override(ip, active_entry)
            status: dict[str, Any] = self.device_manager.stop(ip)
            # Clean up TCP audio stream server for this device.
            audio_srv: Optional[AudioStreamServer] = (
                self.device_manager._audio_streams.pop(ip, None)
            )
            if audio_srv is not None:
                audio_srv.stop()
            # Clean up any dynamic music source for this device.
            music_name: Optional[str] = (
                self.device_manager._music_sources.pop(ip, None)
            )
            if music_name and self.media_manager:
                self.media_manager.remove_source(music_name)
                logging.info("API: cleaned up music source '%s'", music_name)
            logging.info("API: stopped effect on %s", ip)
            self._send_json(200, status)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})


    def _handle_post_resume(self, ip: str) -> None:
        """POST /api/devices/{ip}/resume — clear phone override.

        Clears the manual override for this device so the scheduler
        can resume control on its next poll cycle.
        """
        try:
            if ip not in self.device_manager._devices:
                raise KeyError(ip)
            was_overridden: bool = self.device_manager.is_overridden(ip)
            self.device_manager.clear_override(ip)
            logging.info(
                "API: resume schedule on %s (was overridden: %s)",
                ip, was_overridden,
            )
            status: dict[str, Any] = self.device_manager.get_status(ip)
            self._send_json(200, status)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})


    def _handle_post_reset(self, ip: str) -> None:
        """POST /api/devices/{ip}/reset — deep-reset device hardware.

        Stops all software effects, disables any firmware-level multizone
        effect, blanks all zones with acknowledged writes, and powers off.
        This clears stale zone colors stored in the device's non-volatile
        memory.
        """
        try:
            result: dict[str, Any] = self.device_manager.reset(ip)
            # Clear any phone override since the device is now clean.
            self.device_manager.clear_override(ip)
            self._send_json(200, result)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})


    def _handle_post_power(self, ip: str) -> None:
        """POST /api/devices/{ip}/power — turn device on/off.

        Request body::

            {"on": true}
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        on: Any = body.get("on")
        if not isinstance(on, bool):
            self._send_json(400, {"error": "'on' must be a boolean"})
            return

        try:
            # Powering off from the phone should pause the scheduler on
            # this device, otherwise it will restart the effect immediately.
            if not on and not self.device_manager.is_overridden(ip):
                active_entry: Optional[str] = self._get_active_entry_for_ip(ip)
                self.device_manager.mark_override(ip, active_entry)

            # If this is a group without a virtual emitter (members were
            # unreachable at startup), fall back to direct per-member
            # power commands using the group config.
            em: Optional[Emitter] = self.device_manager.get_emitter(ip)
            if em is None and _is_group_id(ip):
                group_name: str = _group_name_from_id(ip)
                member_ips: list[str] = (
                    self.device_manager._group_config.get(group_name, [])
                )
                if not member_ips:
                    self._send_json(404, {"error": "Group not found"})
                    return
                succeeded: int = 0
                for mip in member_ips:
                    try:
                        dev: LifxDevice = LifxDevice(mip)
                        try:
                            if on:
                                dev.set_power(True, duration_ms=DEFAULT_FADE_MS)
                            else:
                                dev.set_power(False, duration_ms=DEFAULT_FADE_MS)
                            with self.device_manager._lock:
                                self.device_manager._power_states[mip] = on
                            succeeded += 1
                        finally:
                            dev.close()
                    except Exception as exc:
                        logging.warning(
                            "API: power %s failed for group member %s: %s",
                            "on" if on else "off", mip, exc,
                        )
                with self.device_manager._lock:
                    self.device_manager._power_states[ip] = on
                logging.info(
                    "API: power %s on group %s (%d/%d members)",
                    "on" if on else "off", group_name,
                    succeeded, len(member_ips),
                )
                self._send_json(200, {
                    "ip": ip, "power": "on" if on else "off",
                    "members_reached": succeeded,
                    "members_total": len(member_ips),
                })
                return

            result: dict[str, Any] = self.device_manager.set_power(ip, on)
            # set_power() already updates _power_states for the device
            # and all group members — no need to duplicate here.
            logging.info("API: power %s on %s", "on" if on else "off", ip)
            self._send_json(200, result)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})


    def _handle_post_reintrospect(self, ip: str) -> None:
        """POST /api/devices/{ip}/reintrospect — re-query device geometry.

        Re-queries the device's zone count (multizone) or tile chain
        (matrix) and rebuilds the zone map of any group that contains it.
        Use this when a string light reports a wrong zone count after
        power cycling or firmware glitch.

        No request body required.
        """
        try:
            result: dict[str, Any] = self.device_manager.reintrospect(ip)
            self._send_json(200, result)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})


    def _handle_post_brightness(self, ip: str) -> None:
        """POST /api/devices/{ip}/brightness — set brightness (dimmer).

        Request body::

            {"brightness": 75}

        Brightness is an integer percentage (0–100).  Sets warm white
        at the given brightness.  For groups, fans out to every member.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        brightness: Any = body.get("brightness")
        if not isinstance(brightness, (int, float)):
            self._send_json(
                400, {"error": "'brightness' must be a number (0-100)"},
            )
            return
        brightness = int(brightness)
        if not (0 <= brightness <= 100):
            self._send_json(
                400, {"error": "'brightness' must be between 0 and 100"},
            )
            return

        try:
            result: dict[str, Any] = (
                self.device_manager.set_brightness(ip, brightness)
            )
            logging.info("API: brightness %d%% on %s", brightness, ip)
            self._send_json(200, result)
        except KeyError:
            self._send_json(404, {"error": "Device not found"})


    def _handle_post_identify(self, ip: str) -> None:
        """POST /api/devices/{ip}/identify — pulse brightness to locate device.

        Starts a background thread that pulses the device's brightness
        in a sine wave for :data:`IDENTIFY_DURATION_SECONDS`.  The HTTP
        response returns immediately.

        Sets a phone override for the duration of the pulse so the
        scheduler doesn't restart an effect while identify is running.
        The override is cleared automatically when the pulse finishes.
        """
        try:
            # Override so the scheduler doesn't fight with the pulse.
            active_entry: Optional[str] = self._get_active_entry_for_ip(ip)
            self.device_manager.mark_override(ip, active_entry)
            self.device_manager.identify(ip, on_complete=lambda: (
                self.device_manager.clear_override(ip)
            ))
            logging.info("API: identifying %s (override set)", ip)
            self._send_json(200, {"ip": ip, "identifying": True})
        except KeyError:
            self._send_json(404, {"error": "Device not found"})


    def _handle_post_nickname(self, ip: str) -> None:
        """POST /api/devices/{ip}/nickname — set a custom display name.

        Request body::

            {"nickname": "Porch Lights"}

        An empty string or ``null`` clears the nickname.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        nickname: Any = body.get("nickname")
        if nickname is None:
            nickname = ""
        if not isinstance(nickname, str):
            self._send_json(400, {"error": "'nickname' must be a string"})
            return

        nickname = nickname.strip()

        self.device_manager.set_nickname(ip, nickname)

        # Persist to config file.
        self._save_nicknames()

        logging.info(
            "API: nickname for %s %s",
            ip, f"set to '{nickname}'" if nickname else "cleared",
        )
        self._send_json(200, {"ip": ip, "nickname": nickname or None})


    def _save_nicknames(self) -> None:
        """Persist current nicknames to the config file."""
        nicknames: dict[str, str] = self.device_manager.get_nicknames()
        self._save_config_field("nicknames", nicknames or {})


    def _handle_post_effect_defaults(self, effect_name: str) -> None:
        """POST /api/effects/{name}/defaults — save tuned params as defaults.

        Request body::

            {"params": {"speed": 8.0, "decay": 2.0, ...}}

        Persists the provided parameter values as the new defaults for
        the named effect.  These defaults are used by the scheduler and
        reported by GET /api/effects.
        """
        body: Optional[dict[str, Any]] = self._read_json_body()
        if body is None:
            return

        params: Any = body.get("params")
        if not isinstance(params, dict):
            self._send_json(400, {"error": "'params' must be an object"})
            return

        try:
            self.device_manager.save_effect_defaults(effect_name, params)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        self._send_json(200, {"ok": True})


    def _get_active_entry_for_ip(self, ip: str) -> Optional[str]:
        """Find the active schedule entry name for a device or group.

        For group IDs (``group:name``), extracts the group name and
        looks up directly.  For individual IPs, searches all groups
        for one containing this IP.

        Args:
            ip: Device IP address or group identifier.

        Returns:
            The active entry name, or ``None``.
        """
        config: dict[str, Any] = self.config
        if not config:
            return None

        groups: dict[str, list[str]] = _get_groups(config)
        specs: list[dict[str, Any]] = config.get("schedule", [])
        if not specs:
            return None

        now: datetime = datetime.now(timezone.utc).astimezone()

        # For group identifiers, look up by group name directly.
        if _is_group_id(ip):
            group_name: str = _group_name_from_id(ip)
            if group_name in groups:
                active: Optional[dict[str, Any]] = _find_active_entry(
                    specs,
                    config["location"]["latitude"],
                    config["location"]["longitude"],
                    now,
                    group_name,
                )
                if active is not None:
                    return active.get("name")
            return None

        # For individual IPs, search groups.
        for group_name, ips in groups.items():
            if ip in ips:
                active = _find_active_entry(
                    specs,
                    config["location"]["latitude"],
                    config["location"]["longitude"],
                    now,
                    group_name,
                )
                if active is not None:
                    return active.get("name")
                return None

        return None

