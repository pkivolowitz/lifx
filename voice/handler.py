"""VoiceHandlerMixin — voice command endpoint for the GlowUp server.

Adds two routes:
- ``POST /api/voice/command``   — receive structured intent, dispatch, return result
- ``GET  /api/voice/capabilities`` — return available effects, groups, devices

The coordinator calls these endpoints to execute voice commands and
to build its dynamic LLM system prompt.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.voice.handler")

# ---------------------------------------------------------------------------
# Color name → HSBK resolution (basic palette)
# ---------------------------------------------------------------------------

# Maps common color names to LIFX HSBK tuples:
# (hue_degrees, saturation_0_1, brightness_0_1, kelvin).
_COLOR_MAP: dict[str, tuple[int, float, float, int]] = {
    "red":        (0,   1.0, 1.0, 3500),
    "orange":     (36,  1.0, 1.0, 3500),
    "yellow":     (60,  1.0, 1.0, 3500),
    "green":      (120, 1.0, 1.0, 3500),
    "cyan":       (180, 1.0, 1.0, 3500),
    "blue":       (240, 1.0, 1.0, 3500),
    "purple":     (280, 1.0, 1.0, 3500),
    "pink":       (320, 1.0, 1.0, 3500),
    "white":      (0,   0.0, 1.0, 4000),
    "warm white": (0,   0.0, 1.0, 2700),
    "cool white": (0,   0.0, 1.0, 6500),
    "daylight":   (0,   0.0, 1.0, 5500),
}


class VoiceHandlerMixin:
    """Handler mixin for voice command endpoints.

    Mixed into ``GlowUpRequestHandler`` alongside other handler
    mixins.  Accesses shared state via class-level attributes:
    ``device_manager``, ``signal_bus``, ``power_logger``, ``config``.
    """

    def _handle_post_voice_command(self) -> None:
        """``POST /api/voice/command`` — execute a voice intent.

        Request body::

            {
                "action": "power",
                "target": "bedroom",
                "params": {"on": false},
                "room": "bedroom"
            }

        Response::

            {
                "status": "ok",
                "confirmation": "Bedroom lights are off.",
                "speak": false
            }
        """
        body: Optional[dict] = self._read_json_body()  # type: ignore[attr-defined]
        if body is None:
            return

        action: str = body.get("action", "unknown")
        target: str = body.get("target", "all")
        params: dict[str, Any] = body.get("params", {})
        room: str = body.get("room", "unknown")

        logger.info(
            "Voice command from %s: action=%s target=%s params=%s",
            room, action, target, params,
        )

        try:
            result = self._dispatch_voice_action(action, target, params)
        except Exception as exc:
            logger.error("Voice command failed: %s", exc)
            result = {
                "status": "error",
                "confirmation": "Something went wrong.",
                "speak": True,
            }

        self._send_json(200, result)  # type: ignore[attr-defined]

    def _handle_get_voice_capabilities(self) -> None:
        """``GET /api/voice/capabilities`` — list available actions.

        Returns effects, groups, devices, sensors, and power devices
        for the coordinator to build its LLM system prompt.
        """
        result: dict[str, Any] = {
            "effects": [],
            "groups": [],
            "devices": [],
            "sensors": [],
            "power_devices": [],
        }

        # Effects from device_manager.
        dm = self.device_manager  # type: ignore[attr-defined]
        if dm is not None:
            try:
                from effects import EFFECT_REGISTRY
                result["effects"] = sorted(EFFECT_REGISTRY.keys())
            except Exception as exc:
                logger.debug("Failed to load effect registry: %s", exc)

        # Groups from config.
        cfg = self.config  # type: ignore[attr-defined]
        if cfg:
            groups = cfg.get("groups", {})
            result["groups"] = sorted(groups.keys())

        # Devices from device_manager.
        if dm is not None:
            try:
                devs = dm.get_device_list()
                result["devices"] = [
                    d.get("label", d.get("ip", ""))
                    for d in devs
                ]
            except Exception as exc:
                logger.debug("Failed to get device list: %s", exc)

        # Sensors from signal_bus.
        bus = self.signal_bus  # type: ignore[attr-defined]
        if bus is not None:
            try:
                all_signals = bus.signal_names()
                # Extract unique sensor labels from signal names.
                sensors: set[str] = set()
                for name in all_signals:
                    parts = name.split(":")
                    if len(parts) >= 2:
                        sensors.add(parts[0])
                result["sensors"] = sorted(sensors)
            except Exception as exc:
                logger.debug("Failed to enumerate sensors: %s", exc)

        # Power devices.
        pl = self.power_logger  # type: ignore[attr-defined]
        if pl is not None:
            try:
                result["power_devices"] = pl.devices()
            except Exception as exc:
                logger.debug("Failed to get power devices: %s", exc)

        self._send_json(200, result)  # type: ignore[attr-defined]

    # --- Internal dispatch -------------------------------------------------

    def _dispatch_voice_action(
        self,
        action: str,
        target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Route a voice action to the appropriate internal handler.

        Args:
            action: Intent action string.
            target: Device label or group name.
            params: Action-specific parameters.

        Returns:
            Result dict with status, confirmation, speak.
        """
        dm = self.device_manager  # type: ignore[attr-defined]

        if action == "power":
            on: bool = params.get("on", True)
            if dm:
                dm.set_power(target, on)
            state: str = "on" if on else "off"
            return {
                "status": "ok",
                "confirmation": f"{target} is {state}.",
                "speak": False,
            }

        elif action == "brightness":
            brightness: int = params.get("brightness", 100)
            if dm:
                dm.set_brightness(target, brightness / 100.0)
            return {
                "status": "ok",
                "confirmation": f"{target} brightness set to {brightness}%.",
                "speak": False,
            }

        elif action == "color":
            color_name: str = params.get("color", "white").lower()
            hsbk = _COLOR_MAP.get(color_name)
            if hsbk and dm:
                # Apply color via solid effect or direct set.
                dm.set_color(target, *hsbk)
            return {
                "status": "ok",
                "confirmation": f"{target} set to {color_name}.",
                "speak": False,
            }

        elif action == "temperature":
            kelvin: int = params.get("temperature", 4000)
            if dm:
                dm.set_color(target, 0, 0.0, 1.0, kelvin)
            return {
                "status": "ok",
                "confirmation": f"{target} set to {kelvin}K.",
                "speak": False,
            }

        elif action == "play_effect":
            effect: str = params.get("effect", "breathe")
            if dm:
                dm.play(target, effect, params)
            return {
                "status": "ok",
                "confirmation": f"Playing {effect} on {target}.",
                "speak": False,
            }

        elif action == "stop":
            if dm:
                dm.stop(target)
            return {
                "status": "ok",
                "confirmation": f"{target} stopped.",
                "speak": False,
            }

        elif action == "query_sensor":
            return self._voice_query_sensor(target, params)

        elif action == "query_power":
            return self._voice_query_power(target)

        elif action == "query_status":
            return self._voice_query_status(target)

        elif action == "unknown":
            return {
                "status": "error",
                "confirmation": "I didn't understand that command.",
                "speak": True,
            }

        else:
            return {
                "status": "error",
                "confirmation": f"I don't know how to {action}.",
                "speak": True,
            }

    def _voice_query_sensor(
        self, target: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle sensor queries from voice commands."""
        bus = self.signal_bus  # type: ignore[attr-defined]
        if bus is None:
            return {
                "status": "error",
                "confirmation": "Sensor data is not available.",
                "speak": True,
            }

        sensor_type: str = params.get("sensor_type", "temperature")
        snapshot = bus.snapshot()

        # Find matching signal.
        for name, value in snapshot.items():
            if target.lower() in name.lower() and sensor_type in name.lower():
                if sensor_type == "temperature" and isinstance(value, (int, float)):
                    f_val: float = value * 9 / 5 + 32
                    return {
                        "status": "ok",
                        "confirmation": f"The temperature is {f_val:.0f} degrees.",
                        "speak": True,
                    }
                elif sensor_type == "humidity" and isinstance(value, (int, float)):
                    return {
                        "status": "ok",
                        "confirmation": f"Humidity is {value:.0f} percent.",
                        "speak": True,
                    }
                elif sensor_type == "motion":
                    state = "detected" if value else "clear"
                    return {
                        "status": "ok",
                        "confirmation": f"Motion is {state}.",
                        "speak": True,
                    }

        return {
            "status": "ok",
            "confirmation": f"I don't have {sensor_type} data for {target}.",
            "speak": True,
        }

    def _voice_query_power(self, target: str) -> dict[str, Any]:
        """Handle power consumption queries."""
        pl = self.power_logger  # type: ignore[attr-defined]
        if pl is None:
            return {
                "status": "error",
                "confirmation": "Power monitoring is not available.",
                "speak": True,
            }

        s = pl.summary(device=target if target != "all" else None, days=7)
        if not s:
            return {
                "status": "ok",
                "confirmation": f"No power data for {target}.",
                "speak": True,
            }

        avg: float = s.get("avg_watts", 0)
        peak: float = s.get("peak_watts", 0)
        kwh: float = s.get("total_kwh", 0)
        cost: float = kwh * 0.171

        return {
            "status": "ok",
            "confirmation": (
                f"{target} averages {avg:.0f} watts, "
                f"peaked at {peak:.0f}. "
                f"Total {kwh:.1f} kilowatt hours, "
                f"about ${cost:.2f}."
            ),
            "speak": True,
        }

    def _voice_query_status(self, target: str) -> dict[str, Any]:
        """Handle 'what's playing' queries."""
        dm = self.device_manager  # type: ignore[attr-defined]
        if dm is None:
            return {
                "status": "error",
                "confirmation": "Device manager is not available.",
                "speak": True,
            }

        try:
            status = dm.get_status(target)
            effect = status.get("effect")
            if effect:
                elapsed = status.get("elapsed", 0)
                return {
                    "status": "ok",
                    "confirmation": (
                        f"{target} is playing {effect}, "
                        f"running for {int(elapsed)} seconds."
                    ),
                    "speak": True,
                }
            else:
                return {
                    "status": "ok",
                    "confirmation": f"Nothing is playing on {target}.",
                    "speak": True,
                }
        except Exception:
            return {
                "status": "ok",
                "confirmation": f"I can't check {target}.",
                "speak": True,
            }
