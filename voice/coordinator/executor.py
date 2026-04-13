"""Config-driven voice intent executor.

Dispatches voice intents to GlowUp API calls using action
definitions from ``actions.yml``.  Simple commands and queries
are handled generically by the config.  Complex queries use
named handler functions — the config equivalent of function
pointers.  Adding a new voice skill is a YAML entry, not code.

Chat (freeform Ollama conversation with history) is the one
special case that lives in code rather than config.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.1"

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from voice import constants as C

logger: logging.Logger = logging.getLogger("glowup.voice.executor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Mobile, AL residential electricity rate ($/kWh).
_ELECTRICITY_RATE_PER_KWH: float = 0.171

# Repair hints for adapters that need special onboarding beyond a restart.
_REPAIR_HINTS: dict[str, str] = {
    "vivint": "Run vivint setup in the adapters directory to re-authenticate.",
    "nvr": "Try saying repair NVR, or reboot the NVR hardware.",
}

# ---------------------------------------------------------------------------
# Chat constants
# ---------------------------------------------------------------------------

# Maximum exchanges (user + assistant pairs) kept per room.
_CHAT_HISTORY_MAX: int = 10

# History expires after 30 minutes of inactivity.
_CHAT_HISTORY_TTL_S: float = 1800.0

# System prompt for freeform chat — concise spoken responses.
_CHAT_SYSTEM_PROMPT: str = (
    "You are GlowUp, a home assistant built on the Gemma 3 language "
    "model running locally via Ollama. Your responses "
    "are spoken aloud via text-to-speech. Be straightforward, polite, "
    "and factual. Every response MUST be 1-2 sentences maximum. "
    "You have a warm personality."
)

# ---------------------------------------------------------------------------
# Actions config path — adjacent to this module.
# ---------------------------------------------------------------------------

_ACTIONS_FILE: Path = Path(__file__).parent / "actions.yml"


class GlowUpExecutor:
    """Config-driven voice intent executor.

    Loads action definitions from ``actions.yml`` and dispatches
    intents through a generic command/query pipeline.  Complex
    queries delegate to named handler functions registered at init.

    Args:
        api_base:     GlowUp server URL.
        auth_token:   Bearer token for API auth.
        chat_model:   Ollama model for freeform chat.
        ollama_host:  Ollama API base URL.
    """

    def __init__(
        self,
        api_base: str = "http://localhost:8420",
        auth_token: str = "",
        chat_model: str = "gemma3:27b",
        ollama_host: str = "http://localhost:11434",
    ) -> None:
        """Initialize the executor."""
        self._api_base: str = api_base.rstrip("/")
        self._auth_token: str = auth_token
        self._chat_model: str = chat_model
        self._ollama_host: str = ollama_host

        # Per-room conversation history.
        self._chat_history: dict[str, list[dict[str, str]]] = {}
        self._chat_timestamps: dict[str, float] = {}

        # Load action definitions.
        self._actions: dict[str, dict[str, Any]] = self._load_actions()

        # Named query handlers — the config's "function pointers."
        # Each takes (api_data, action_config, target_raw, params)
        # and returns a result dict.
        self._handlers: dict[str, Any] = {
            "power_state": self._handle_power_state,
            "sensor_reading": self._handle_sensor_reading,
            "power_summary": self._handle_power_summary,
            "soil_moisture": self._handle_soil_moisture,
            "weather": self._handle_weather,
            "system_status": self._handle_system_status,
            "set_voice": self._handle_set_voice,
            "tell_time": self._handle_tell_time,
            "tell_date": self._handle_tell_date,
            "lock_status": self._handle_lock_status,
            "door_status": self._handle_door_status,
            "alarm_status": self._handle_alarm_status,
            "battery_status": self._handle_battery_status,
            "printer_status": self._handle_printer_status,
            "schedule_status": self._handle_schedule_status,
            "uptime_status": self._handle_uptime_status,
            "shopping_add": self._handle_shopping_add,
            "shopping_remove": self._handle_shopping_remove,
            "shopping_query": self._handle_shopping_query,
            "shopping_clear": self._handle_shopping_clear,
            "identify_room": self._handle_identify_room,
            "commands": self._handle_commands,
            "help_lights": self._handle_help_lights,
            "help_shopping": self._handle_help_shopping,
            "help_security": self._handle_help_security,
            "help_system": self._handle_help_system,
            "help_sensors": self._handle_help_sensors,
            "list_sensors": self._handle_list_sensors,
            "list_groups": self._handle_list_groups,
            "list_doors": self._handle_list_doors,
            "list_locks": self._handle_list_locks,
            "enable_voice_gate": self._handle_enable_voice_gate,
            "disable_voice_gate": self._handle_disable_voice_gate,
        }

        # TTS reference — set after init by the coordinator daemon.
        self._tts: Any = None

        # MQTT client reference — set after init by the coordinator
        # daemon via :meth:`set_mqtt_client`.  Used by gate handlers
        # to publish retained gate state.  None when running under
        # unit tests that don't wire MQTT.
        self._mqtt_client: Any = None

        logger.info(
            "Loaded %d action definitions from %s",
            len(self._actions), _ACTIONS_FILE.name,
        )

    def get_action_label(self, action: str) -> str:
        """Get the human-readable label for an action.

        Used by the pipeline to speak "Waiting on the {label}"
        before execution begins.

        Args:
            action: Action name from intent.

        Returns:
            Label string, or empty string if not found.
        """
        cfg: dict[str, Any] | None = self._actions.get(action)
        if cfg is None:
            return ""
        return cfg.get("label", "")

    def get_action_type(self, action: str) -> str:
        """Get the action type (command, query, chat).

        Args:
            action: Action name from intent.

        Returns:
            Type string, or empty string if not found.
        """
        cfg: dict[str, Any] | None = self._actions.get(action)
        if cfg is None:
            return ""
        return cfg.get("type", "")

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_actions(self) -> dict[str, dict[str, Any]]:
        """Load action definitions from YAML.

        Returns:
            Dict mapping action name → config dict.
        """
        if not _ACTIONS_FILE.exists():
            logger.error("Actions file not found: %s", _ACTIONS_FILE)
            return {}

        with open(_ACTIONS_FILE, "r") as f:
            return yaml.safe_load(f) or {}

    # ------------------------------------------------------------------
    # HTTP client
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to the GlowUp API.

        Args:
            method: HTTP method.
            path:   URL path.
            body:   Request body (JSON-encoded).

        Returns:
            Parsed JSON response dict.
        """
        url: str = f"{self._api_base}{path}"
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._auth_token}",
            "Content-Type": "application/json",
        }

        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(
            url, data=data, headers=headers, method=method,
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # Log the response body for debugging.
            error_body: str = ""
            try:
                error_body = exc.read().decode()[:200]
            except Exception:
                pass
            logger.error(
                "API %s %s → %d: %s (body: %s)",
                method, path, exc.code, exc.reason, error_body,
            )
            raise

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

    def _resolve_target(self, target: str) -> str:
        """Resolve a fuzzy target name to an actual group or device.

        Args:
            target: Raw target string from the LLM.

        Returns:
            Resolved name with ``group:`` prefix for groups.
        """
        if target.lower() == "all":
            return "all"

        try:
            data = self._request("GET", "/api/groups")
            groups: dict = data.get("groups", {})
            target_lower: str = target.lower().strip()

            # Exact match.
            for name in groups:
                if name.lower() == target_lower:
                    logger.info(
                        "Target '%s' resolved to group 'group:%s'",
                        target, name,
                    )
                    return f"group:{name}"

            # Substring match.
            for name in groups:
                if target_lower in name.lower():
                    logger.info(
                        "Fuzzy target '%s' resolved to group 'group:%s'",
                        target, name,
                    )
                    return f"group:{name}"

            # Device list.
            dev_data = self._request("GET", "/api/devices")
            devices: list = dev_data.get("devices", [])
            for dev in devices:
                label: str = dev.get("label", "")
                if target_lower in label.lower():
                    logger.info(
                        "Fuzzy target '%s' resolved to device '%s'",
                        target, label,
                    )
                    return label

        except Exception as exc:
            logger.debug("Target resolution failed: %s", exc)

        return target

    # ------------------------------------------------------------------
    # Generic dispatch
    # ------------------------------------------------------------------

    def execute(
        self, intent: dict[str, Any], room: str,
    ) -> dict[str, Any]:
        """Execute a voice intent via config-driven dispatch.

        Args:
            intent: Parsed intent with ``action``, ``target``, ``params``.
            room:   Originating room name.

        Returns:
            Dict with ``status``, ``confirmation``, ``speak``.
        """
        # Stash room for handlers that need it (e.g., identify_room).
        self._current_room: str = room

        action: str = intent.get("action", "unknown")
        target_raw: str = intent.get("target", "all")
        target_raw = self._resolve_target(target_raw)
        target_url: str = urllib.parse.quote(target_raw, safe="")
        params: dict[str, Any] = intent.get("params", {})

        # Human-readable target (strip group: prefix).
        display_target: str = (
            target_raw[6:] if target_raw.startswith("group:") else target_raw
        )

        # Look up action config.
        action_cfg: dict[str, Any] | None = self._actions.get(action)

        if action_cfg is None:
            # Route unknown actions to chat as fallback.
            action_cfg = self._actions.get("chat")
            if action_cfg and action_cfg.get("type") == "chat":
                params["message"] = params.get("message", intent.get("target", ""))

        if action_cfg is None:
            return {
                "status": "error",
                "confirmation": f"I don't know how to {action}.",
                "speak": True,
            }

        try:
            action_type: str = action_cfg.get("type", "command")

            if action_type == "command":
                return self._dispatch_command(
                    action_cfg, target_url, display_target, params,
                )
            elif action_type == "query":
                return self._dispatch_query(
                    action_cfg, target_url, target_raw,
                    display_target, params,
                )
            elif action_type == "chat":
                message: str = params.get("message", "")
                return self._exec_chat(message, room)
            else:
                return {
                    "status": "error",
                    "confirmation": f"Unknown action type: {action_type}.",
                    "speak": True,
                }
        except Exception as exc:
            logger.error("Execution failed: %s", exc)
            return {
                "status": "error",
                "confirmation": "Something went wrong. Please try again.",
                "speak": True,
            }

    # ------------------------------------------------------------------
    # Command dispatch — POST to API, format confirmation
    # ------------------------------------------------------------------

    def _dispatch_command(
        self,
        cfg: dict[str, Any],
        target_url: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a command action from config.

        Args:
            cfg:            Action config from YAML.
            target_url:     URL-encoded target for API path.
            display_target: Human-readable target for confirmation.
            params:         Intent parameters.

        Returns:
            Result dict.
        """
        # Build API path.
        path: str = cfg["path"].format(target=target_url)

        # Build request body — resolve "param:key" references.
        body: Any = self._resolve_body(cfg.get("body", {}), params)

        logger.debug("Command: %s %s body=%s", cfg.get("method", "POST"), path, body)

        # Make the API call.
        self._request(cfg.get("method", "POST"), path, body)

        # Build confirmation text.
        confirmation: str = self._format_confirm(
            cfg.get("confirm", "Done."),
            display_target,
            params,
            cfg.get("param_map", {}),
        )

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": cfg.get("speak", False),
        }

    def _resolve_body(
        self, body_template: Any, params: dict[str, Any],
    ) -> Any:
        """Resolve ``param:key`` references in a body template.

        Recursively walks the body dict/list and replaces string
        values starting with ``param:`` with the corresponding
        intent parameter value (preserving type).

        Args:
            body_template: Body structure from YAML.
            params:        Intent parameters.

        Returns:
            Resolved body with actual parameter values.
        """
        if isinstance(body_template, dict):
            return {
                k: self._resolve_body(v, params)
                for k, v in body_template.items()
            }
        elif isinstance(body_template, list):
            return [self._resolve_body(v, params) for v in body_template]
        elif isinstance(body_template, str) and body_template.startswith("param:"):
            key: str = body_template[6:]
            return params.get(key, body_template)
        return body_template

    def _format_confirm(
        self,
        template: str,
        display_target: str,
        params: dict[str, Any],
        param_map: dict[str, Any] | None = None,
    ) -> str:
        """Format a confirmation template with target and params.

        Args:
            template:       Format string from YAML.
            display_target: Human-readable target name.
            params:         Intent parameters.
            param_map:      Optional value mappings (e.g., bool → text).

        Returns:
            Formatted confirmation string.
        """
        # Build substitution dict.
        subs: dict[str, Any] = {"target": display_target}
        subs.update(params)

        # Apply param_map — e.g., on=true → on_off="on".
        if param_map:
            for param_name, mapping in param_map.items():
                value = params.get(param_name)
                output_key: str = mapping.get("key", param_name)
                # Convert to string for lookup in the map.
                str_value: str = str(value).lower()
                subs[output_key] = mapping.get(str_value, str(value))

        try:
            return template.format_map(subs)
        except KeyError as exc:
            logger.warning("Confirm template key missing: %s", exc)
            return template

    # ------------------------------------------------------------------
    # Query dispatch — delegates to named handler functions
    # ------------------------------------------------------------------

    def _dispatch_query(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a query action via a named handler.

        Args:
            cfg:            Action config from YAML.
            target_url:     URL-encoded target.
            target_raw:     Raw resolved target (with group: prefix).
            display_target: Human-readable target.
            params:         Intent parameters.

        Returns:
            Result dict from the handler.
        """
        handler_name: str = cfg.get("handler", "")
        handler = self._handlers.get(handler_name)

        if handler is None:
            logger.error("No handler registered for '%s'", handler_name)
            return {
                "status": "error",
                "confirmation": f"Query handler '{handler_name}' not found.",
                "speak": True,
            }

        return handler(cfg, target_url, target_raw, display_target, params)

    # ------------------------------------------------------------------
    # Query handlers — the "function pointers" referenced by config
    # ------------------------------------------------------------------

    def _handle_power_state(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Check if devices/group are on or off."""
        try:
            all_devices: dict[str, Any] = self._request("GET", "/api/devices")
            devices: list[dict[str, Any]] = all_devices.get("devices", [])

            is_group: bool = target_raw.startswith("group:")
            powered: list[bool] = []

            for dev in devices:
                label: str = dev.get("label", "")
                if is_group:
                    if (dev.get("group", "") == display_target
                            or label == display_target):
                        powered.append(dev.get("power", False))
                else:
                    if label == display_target:
                        powered.append(dev.get("power", False))

            if not powered:
                # Fall back to effect status.
                return self._handle_effect_status(
                    cfg, target_url, target_raw, display_target, params,
                )

            if all(powered):
                state = "on"
            elif not any(powered):
                state = "off"
            else:
                on_count: int = sum(powered)
                state = f"partially on, {on_count} of {len(powered)}"

            return {
                "status": "ok",
                "confirmation": f"{display_target} is {state}.",
                "speak": True,
            }
        except Exception:
            return {
                "status": "ok",
                "confirmation": f"I can't check the status of {display_target}.",
                "speak": True,
            }

    def _handle_effect_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Check what effect is playing on a device."""
        try:
            data = self._request(
                "GET", f"/api/devices/{target_url}/status",
            )
            effect: str = data.get("effect", "nothing")
            if effect and effect != "none":
                elapsed: float = data.get("elapsed", 0)
                return {
                    "status": "ok",
                    "confirmation": (
                        f"{display_target} is playing {effect}, "
                        f"running for {elapsed:.0f} seconds."
                    ),
                    "speak": True,
                }
            return {
                "status": "ok",
                "confirmation": f"Nothing is playing on {display_target}.",
                "speak": True,
            }
        except Exception:
            return {
                "status": "ok",
                "confirmation": f"I can't check the status of {display_target}.",
                "speak": True,
            }

    def _handle_sensor_reading(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query BLE sensor readings (temperature, humidity, motion)."""
        try:
            data = self._request("GET", "/api/ble/sensors")
        except Exception as exc:
            logger.error("Sensor API request failed: %s", exc)
            return {
                "status": "ok",
                "confirmation": (
                    f"I can't reach the sensors for {display_target}."
                ),
                "speak": True,
            }
        sensor_type: str = params.get("sensor_type", "temperature")

        for label, readings in data.items():
            if display_target.lower() in label.lower():
                value = readings.get(sensor_type)
                if value is not None:
                    if sensor_type == "temperature":
                        f_val: float = value * 9 / 5 + 32
                        return {
                            "status": "ok",
                            "confirmation": (
                                f"The {label} temperature is "
                                f"{f_val:.0f} degrees."
                            ),
                            "speak": True,
                        }
                    elif sensor_type == "humidity":
                        return {
                            "status": "ok",
                            "confirmation": (
                                f"The {label} humidity is "
                                f"{value:.0f} percent."
                            ),
                            "speak": True,
                        }
                    elif sensor_type == "motion":
                        state: str = "detected" if value else "clear"
                        return {
                            "status": "ok",
                            "confirmation": f"Motion in {label} is {state}.",
                            "speak": True,
                        }

        return {
            "status": "ok",
            "confirmation": (
                f"I don't have {sensor_type} data for {display_target}."
            ),
            "speak": True,
        }

    def _handle_power_summary(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query power consumption from Zigbee plugs."""
        try:
            data = self._request(
                "GET", f"/api/power/summary?device={target_url}",
            )
        except Exception:
            data = self._request("GET", "/api/power/summary")

        avg: float = data.get("avg_watts", 0)
        peak: float = data.get("peak_watts", 0)
        kwh: float = data.get("total_kwh", 0)
        cost: float = kwh * _ELECTRICITY_RATE_PER_KWH

        return {
            "status": "ok",
            "confirmation": (
                f"{display_target} averages {avg:.0f} watts, "
                f"peaked at {peak:.0f} watts. "
                f"Total energy {kwh:.1f} kilowatt hours, "
                f"costing about ${cost:.2f}."
            ),
            "speak": True,
        }

    def _handle_soil_moisture(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query soil moisture for a yard zone.

        Zone mapping comes from the action config (actions.yml),
        not hardcoded — add new sensors by editing the YAML.
        """
        zones: dict[str, Any] = cfg.get("zones", {})
        threshold: float = cfg.get("threshold", 40.0)

        # Find the zone — fuzzy match against config keys.
        zone_lower: str = display_target.lower().strip()
        zone_cfg: dict[str, Any] | None = zones.get(zone_lower)

        if zone_cfg is None:
            for key in zones:
                if zone_lower in key or key in zone_lower:
                    zone_cfg = zones[key]
                    zone_lower = key
                    break

        if zone_cfg is None:
            return {
                "status": "error",
                "confirmation": (
                    f"I don't have soil sensors for {display_target}."
                ),
                "speak": True,
            }

        try:
            data: dict[str, Any] = self._request("GET", "/api/home/soil")
            sensors: list[dict[str, Any]] = data.get("sensors", [])
            readings: dict[str, dict[str, Any]] = {
                s.get("name", ""): s for s in sensors if s.get("name")
            }

            parts: list[str] = []
            moisture_values: list[float] = []

            for sensor_def in zone_cfg.get("sensors", []):
                sensor_name: str = sensor_def["name"]
                direction: str = sensor_def["direction"]
                reading = readings.get(sensor_name)
                if reading and "soil_moisture" in reading:
                    pct: float = reading["soil_moisture"]
                    moisture_values.append(pct)
                    parts.append(f"{pct:.0f}% to the {direction}")
                else:
                    parts.append(f"no reading to the {direction}")

            if not moisture_values:
                return {
                    "status": "ok",
                    "confirmation": (
                        f"No soil readings available for {zone_lower}."
                    ),
                    "speak": True,
                }

            moisture_text: str = " and ".join(parts)
            avg_moisture: float = (
                sum(moisture_values) / len(moisture_values)
            )
            suggestion: str = (
                "Watering is suggested."
                if avg_moisture < threshold
                else "Watering is not suggested."
            )

            return {
                "status": "ok",
                "confirmation": (
                    f"The present soil moisture in the {zone_lower} is "
                    f"{moisture_text}. {suggestion}"
                ),
                "speak": True,
            }
        except Exception as exc:
            logger.error("Soil query failed: %s", exc)
            return {
                "status": "error",
                "confirmation": (
                    f"I couldn't check the soil in {display_target}."
                ),
                "speak": True,
            }

    # ------------------------------------------------------------------
    # Weather — Open-Meteo API (free, no key required)
    # ------------------------------------------------------------------

    # Mobile, AL coordinates — same as the /home dashboard uses.
    _WEATHER_LAT: float = 30.69
    _WEATHER_LON: float = -88.04
    _WEATHER_URL: str = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,weather_code,"
        "wind_speed_10m"
        "&temperature_unit=fahrenheit&wind_speed_unit=mph"
    )

    # WMO weather interpretation codes → plain English.
    _WMO_CODES: dict[int, str] = {
        0: "clear sky", 1: "mainly clear", 2: "partly cloudy",
        3: "overcast", 45: "foggy", 48: "depositing rime fog",
        51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
        61: "slight rain", 63: "moderate rain", 65: "heavy rain",
        66: "light freezing rain", 67: "heavy freezing rain",
        71: "slight snow", 73: "moderate snow", 75: "heavy snow",
        77: "snow grains", 80: "slight rain showers",
        81: "moderate rain showers", 82: "violent rain showers",
        85: "slight snow showers", 86: "heavy snow showers",
        95: "thunderstorm", 96: "thunderstorm with slight hail",
        99: "thunderstorm with heavy hail",
    }

    def _handle_weather(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query current weather from Open-Meteo (free, no API key)."""
        url: str = self._WEATHER_URL.format(
            lat=self._WEATHER_LAT, lon=self._WEATHER_LON,
        )
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data: dict[str, Any] = json.loads(resp.read())

            current: dict[str, Any] = data.get("current", {})
            temp: float = current.get("temperature_2m", 0)
            humidity: float = current.get("relative_humidity_2m", 0)
            wind: float = current.get("wind_speed_10m", 0)
            code: int = current.get("weather_code", 0)
            condition: str = self._WMO_CODES.get(code, "unknown conditions")

            return {
                "status": "ok",
                "confirmation": (
                    f"It is currently {temp:.0f} degrees with {condition}. "
                    f"Humidity is {humidity:.0f}% and "
                    f"wind is {wind:.0f} miles per hour."
                ),
                "speak": True,
            }
        except Exception as exc:
            logger.error("Weather query failed: %s", exc)
            return {
                "status": "error",
                "confirmation": "I couldn't get the weather right now.",
                "speak": True,
            }

    # ------------------------------------------------------------------
    # System status — comprehensive health check
    # ------------------------------------------------------------------

    def _handle_system_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Check overall system health and verbalize the result.

        Queries server status, device count, adapter states, and
        schedule.  If everything is healthy, responds with
        "All is well."  Otherwise enumerates problems.
        """
        problems: list[str] = []

        # 1. Server status + adapter health (single API call).
        adapters: dict[str, dict[str, Any]] = {}
        try:
            status_data: dict[str, Any] = self._request(
                "GET", "/api/status",
            )
            if not status_data.get("ready", False):
                problems.append("server is still loading")
            adapters = status_data.get("adapters", {})
        except Exception:
            return {
                "status": "error",
                "confirmation": "The server is not responding.",
                "speak": True,
            }

        # 2. Check each adapter/daemon.
        adapter_ok: list[str] = []
        adapter_bad: list[str] = []
        for name, info in adapters.items():
            # Adapters report health differently:
            #   - threads: {"running": bool}
            #   - network adapters: {"connected": bool}
            #   - printer: {"status": "ok"/"error"}
            # An adapter is healthy if ANY positive indicator is present.
            healthy: bool = (
                info.get("running", False)
                or info.get("connected", False)
                or info.get("status") == "ok"
            )
            if healthy:
                adapter_ok.append(name)
            else:
                adapter_bad.append(name)
                hint: str = _REPAIR_HINTS.get(name, "")
                if hint:
                    problems.append(f"{name} is down. {hint}")
                else:
                    problems.append(f"{name} is down")

        # 3. Device count.
        total: int = 0
        powered_on: int = 0
        try:
            dev_data: dict[str, Any] = self._request(
                "GET", "/api/devices",
            )
            devices: list[dict[str, Any]] = dev_data.get("devices", [])
            total = len(devices)
            powered_on = sum(
                1 for d in devices if d.get("power", False)
            )
            if total == 0:
                problems.append("no devices discovered")
        except Exception:
            problems.append("cannot read device list")

        # 4. Schedule.
        schedule_count: int = 0
        try:
            sched_data: dict[str, Any] = self._request(
                "GET", "/api/schedule",
            )
            entries: list = sched_data.get("entries", [])
            schedule_count = sum(
                1 for e in entries if e.get("enabled", True)
            )
        except Exception:
            pass  # Non-critical.

        # 5. Build spoken response — keep it short to avoid
        # audio breakup on long TTS playback.
        if not problems:
            confirmation: str = (
                f"All is well. {total} devices, {powered_on} on, "
                f"{len(adapter_ok)} adapters healthy."
            )
        else:
            confirmation = ". ".join(
                p.capitalize() for p in problems
            ) + "."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    # ------------------------------------------------------------------
    # TTS voice management
    # ------------------------------------------------------------------

    def set_tts(self, tts: Any) -> None:
        """Set the TTS engine reference for voice-change commands.

        Args:
            tts: TextToSpeech instance from the coordinator.
        """
        self._tts = tts

    def set_mqtt_client(self, client: Any) -> None:
        """Set the MQTT client reference for gate publish handlers.

        Called by the coordinator daemon after MQTT is connected.
        Gate handlers use this client to publish retained state on
        ``glowup/voice/gate/<room_slug>``.  Left as None in unit
        tests — handlers detect the missing client and return an
        error result instead of crashing.

        Args:
            client: paho-mqtt Client instance.
        """
        self._mqtt_client = client

    def _handle_set_voice(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Change the macOS say voice at runtime."""
        voice: str = params.get("voice_name", display_target)
        if self._tts is None:
            return {
                "status": "error",
                "confirmation": "Voice engine is not available.",
                "speak": True,
            }
        self._tts.voice_name = voice
        return {
            "status": "ok",
            "confirmation": f"Voice changed to {voice}.",
            "speak": True,
        }

    # ------------------------------------------------------------------
    # Tell time — local clock, no API call
    # ------------------------------------------------------------------

    def _handle_tell_time(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Speak the current local time.

        Formats the time in 12-hour spoken form (e.g., "It is 3:45 PM").
        No API call required — reads the system clock directly.
        """
        now: datetime = datetime.now()
        hour: int = now.hour
        minute: int = now.minute
        period: str = "AM" if hour < 12 else "PM"

        # Convert to 12-hour format.
        display_hour: int = hour % 12
        if display_hour == 0:
            display_hour = 12

        if minute == 0:
            time_str: str = f"{display_hour} {period}"
        else:
            time_str = f"{display_hour}:{minute:02d} {period}"

        return {
            "status": "ok",
            "confirmation": f"It is {time_str}.",
            "speak": True,
        }

    def _handle_tell_date(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Speak today's date and day of the week.

        No API call required — reads the system clock directly.
        """
        now: datetime = datetime.now()
        # "Saturday, April 5th, 2026"
        day_name: str = now.strftime("%A")
        month_name: str = now.strftime("%B")
        day_num: int = now.day
        year: int = now.year

        # Ordinal suffix.
        if 11 <= day_num <= 13:
            suffix: str = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(day_num % 10, "th")

        return {
            "status": "ok",
            "confirmation": f"Today is {day_name}, {month_name} {day_num}{suffix}, {year}.",
            "speak": True,
        }

    def _handle_lock_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report lock status from Vivint adapter.

        One API call to /api/home/locks.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/home/locks")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the lock system.",
                "speak": True,
            }

        locks: list[dict[str, Any]] = data.get("locks", [])
        if not locks:
            return {
                "status": "ok",
                "confirmation": "No locks are configured.",
                "speak": True,
            }

        # Filter to specific lock if target is not "all".
        if target_raw and target_raw != "all":
            target_lower: str = target_raw.lower()
            locks = [
                lk for lk in locks
                if target_lower in lk.get("name", "").lower()
            ]
            if not locks:
                return {
                    "status": "ok",
                    "confirmation": f"I don't see a lock called {display_target}.",
                    "speak": True,
                }

        locked: list[str] = []
        unlocked: list[str] = []
        for lk in locks:
            name: str = lk.get("name", "unknown")
            # API returns "locked": true/false.
            if lk.get("locked", False):
                locked.append(name)
            else:
                unlocked.append(name)

        if not unlocked:
            confirmation: str = "All locks are locked."
        elif not locked:
            confirmation = "All locks are unlocked."
        else:
            unlocked_names: str = ", ".join(unlocked)
            confirmation = f"{unlocked_names} {'is' if len(unlocked) == 1 else 'are'} unlocked."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_door_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report door contact sensor status from Vivint adapter.

        One API call to /api/home/security.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/home/security")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the security system.",
                "speak": True,
            }

        doors: list[dict[str, Any]] = data.get("doors", [])
        if not doors:
            return {
                "status": "ok",
                "confirmation": "No door sensors are configured.",
                "speak": True,
            }

        open_doors: list[str] = [
            d.get("name", "unknown") for d in doors if d.get("open", False)
        ]

        if not open_doors:
            confirmation: str = "All doors are closed."
        else:
            names: str = ", ".join(open_doors)
            confirmation = f"{names} {'is' if len(open_doors) == 1 else 'are'} open."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_alarm_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report alarm/security panel state from Vivint adapter.

        One API call to /api/home/security.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/home/security")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the security system.",
                "speak": True,
            }

        state: str = data.get("alarm_state", "unknown")
        # Map internal state names to spoken forms.
        spoken_map: dict[str, str] = {
            "disarmed": "disarmed",
            "armed_stay": "armed in stay mode",
            "armed_away": "armed in away mode",
            "unknown": "in an unknown state",
        }
        spoken: str = spoken_map.get(state, state)

        return {
            "status": "ok",
            "confirmation": f"The alarm is {spoken}.",
            "speak": True,
        }

    def _handle_battery_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report Vivint devices that are audibly complaining.

        Answers "any low batteries?" and "are any sensors chirping?".
        A Vivint sensor chirps when its low_battery flag is set, when
        it is reporting tampered, or (on smoke/CO units) when it is
        bypassed.  This handler walks the Vivint adapter's sensor and
        lock state and reports every "attention needed" reason with
        the human-readable sensor name so Perry can locate the one
        that is actually beeping without hunting by ear.

        Checked signals (per sensor, via /api/status):
          - ``low_battery`` boolean (vivintpy authoritative chirp reason)
          - ``is_tampered`` boolean (audible on most Vivint gear)
          - ``is_bypassed`` boolean (panel announces bypassed zones)
          - ``battery`` percent <30%   (legacy fallback for sensors that
                                         report a numeric level)

        First Alert detectors are NOT on the network and cannot be
        queried — they are excluded from this response.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/status")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the server.",
                "speak": True,
            }

        # Legacy percent threshold for sensors/locks that report a
        # numeric battery level.  Smoke detectors report boolean low
        # only, so the bool path below is the authoritative source.
        LOW_THRESHOLD: int = 30

        attention: list[str] = []  # Human-readable reasons, one per issue.
        vivint: dict[str, Any] = data.get("adapters", {}).get("vivint", {})

        # Check locks — percent battery only, no tamper/bypass fields.
        for name, info in vivint.get("locks", {}).items():
            batt: float = info.get("battery", 1.0)
            pct: int = int(batt * 100) if batt <= 1.0 else int(batt)
            if pct < LOW_THRESHOLD:
                label: str = name.replace("_", " ").title()
                attention.append(f"{label} at {pct} percent")

        # Check sensors — percent AND the new boolean fault fields
        # (low_battery / is_tampered / is_bypassed) published by the
        # vivint adapter as of 2026-04-12.  The booleans catch smoke
        # detectors which only report "low yes/no," not a percentage.
        for name, info in vivint.get("sensors", {}).items():
            label = info.get("name", name)

            # Boolean low-battery → authoritative chirp reason.
            if info.get("low_battery"):
                attention.append(f"{label} low battery")

            # Tamper → audible beep until cleared.
            if info.get("is_tampered"):
                attention.append(f"{label} tampered")

            # Bypassed → panel chirps to remind you a zone is off.
            if info.get("is_bypassed"):
                attention.append(f"{label} bypassed")

            # Numeric battery percent, for sensors that report one.
            batt_val: Any = info.get("battery")
            if batt_val is not None:
                pct = int(batt_val)
                # Only flag on percent if we did NOT already flag on
                # the boolean — avoid saying the same thing twice.
                if pct < LOW_THRESHOLD and not info.get("low_battery"):
                    attention.append(f"{label} at {pct} percent")

        if not attention:
            confirmation: str = (
                "All Vivint sensors are reporting clean. "
                "If you still hear chirping, check a First Alert unit."
            )
        else:
            # Speak the first few by name so Perry can walk straight
            # to the offender; summarize the tail.
            head: list[str] = attention[:5]
            confirmation = (
                f"{len(attention)} need attention: " + ", ".join(head)
            )
            if len(attention) > 5:
                confirmation += f", and {len(attention) - 5} more"
            confirmation += "."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_printer_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report printer health from the printer adapter.

        One API call to /api/status.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/status")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the server.",
                "speak": True,
            }

        printer: dict[str, Any] = data.get("adapters", {}).get("printer", {})
        if not printer:
            return {
                "status": "ok",
                "confirmation": "No printer is configured.",
                "speak": True,
            }

        status: str = printer.get("status", "unknown")
        details: dict[str, Any] = printer.get("details", {})
        alerts: list[str] = details.get("alerts", [])
        drum_pct: float = details.get("drum_life_pct", 0)
        page_count: int = details.get("page_count", 0)

        if status == "ok" and not alerts:
            confirmation: str = (
                f"Printer is fine. Drum at {drum_pct:.0f}%, "
                f"{page_count} pages printed."
            )
        else:
            alert_str: str = ", ".join(alerts) if alerts else "unknown issue"
            confirmation = f"Printer needs attention: {alert_str}."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_schedule_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report active schedule entries.

        One API call to /api/schedule.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/schedule")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the server.",
                "speak": True,
            }

        entries: list[dict[str, Any]] = data.get("entries", [])
        enabled: list[dict[str, Any]] = [
            e for e in entries if e.get("enabled", True)
        ]

        if not enabled:
            return {
                "status": "ok",
                "confirmation": "No active schedules.",
                "speak": True,
            }

        # Summarize: count + first few targets.
        targets: list[str] = []
        for e in enabled[:5]:
            t: str = e.get("target", e.get("label", "unknown"))
            if t not in targets:
                targets.append(t)

        target_str: str = ", ".join(targets)
        confirmation: str = (
            f"{len(enabled)} active schedules covering {target_str}."
        )

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_uptime_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report server uptime.

        One API call to /api/status (checks response time as proxy).
        Uses the process start time from /api/status if available,
        otherwise just confirms the server is reachable.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/status")
        except Exception:
            return {
                "status": "error",
                "confirmation": "The server is not responding.",
                "speak": True,
            }

        # Server doesn't expose uptime directly. Report adapter count
        # and ready state as a proxy.
        adapters: dict[str, Any] = data.get("adapters", {})
        healthy: int = sum(
            1 for info in adapters.values()
            if info.get("running") or info.get("connected") or info.get("status") == "ok"
        )

        return {
            "status": "ok",
            "confirmation": (
                f"Server is up and ready with {healthy} adapters online."
            ),
            "speak": True,
        }

    def _handle_shopping_add(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Add an item to the shopping list.

        One API call to POST /api/shopping.
        """
        item_text: str = params.get("item", "").strip()
        if not item_text:
            return {
                "status": "error",
                "confirmation": "I didn't catch what to add.",
                "speak": True,
            }
        try:
            self._request("POST", "/api/shopping", {"text": item_text})
        except Exception:
            return {
                "status": "error",
                "confirmation": "Failed to add to the shopping list.",
                "speak": True,
            }
        return {
            "status": "ok",
            "confirmation": f"Added {item_text} to the shopping list.",
            "speak": True,
        }

    def _handle_shopping_remove(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Remove an item from the shopping list.

        Fetches the list, finds the item, deletes by ID.
        """
        item_text: str = params.get("item", "").strip()
        if not item_text:
            return {
                "status": "error",
                "confirmation": "I didn't catch what to remove.",
                "speak": True,
            }
        try:
            data: dict[str, Any] = self._request("GET", "/api/shopping")
            items: list[dict[str, Any]] = data.get("items", [])
            target_lower: str = item_text.lower()
            for item in items:
                if (item["text"].lower() == target_lower
                        and not item.get("checked")):
                    self._request("DELETE", f"/api/shopping/{item['id']}")
                    return {
                        "status": "ok",
                        "confirmation": f"Removed {item_text} from the list.",
                        "speak": True,
                    }
            return {
                "status": "ok",
                "confirmation": f"{item_text} is not on the list.",
                "speak": True,
            }
        except Exception:
            return {
                "status": "error",
                "confirmation": "Failed to check the shopping list.",
                "speak": True,
            }

    def _handle_shopping_query(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query the shopping list — specific item or full list.

        One API call to GET /api/shopping.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/shopping")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Failed to check the shopping list.",
                "speak": True,
            }

        items: list[dict[str, Any]] = [
            i for i in data.get("items", []) if not i.get("checked")
        ]

        # Specific item query.
        item_text: str = params.get("item", "").strip()
        if item_text:
            found: bool = any(
                i["text"].lower() == item_text.lower() for i in items
            )
            if found:
                return {
                    "status": "ok",
                    "confirmation": f"Yes, {item_text} is on the list.",
                    "speak": True,
                }
            return {
                "status": "ok",
                "confirmation": f"No, {item_text} is not on the list.",
                "speak": True,
            }

        # Full list.
        if not items:
            return {
                "status": "ok",
                "confirmation": "The shopping list is empty.",
                "speak": True,
            }

        # Read up to 8 items to keep TTS reasonable.
        names: list[str] = [i["text"] for i in items[:8]]
        count: int = len(items)
        listing: str = ", ".join(names)
        if count > 8:
            listing += f", and {count - 8} more"
        return {
            "status": "ok",
            "confirmation": f"{count} items: {listing}.",
            "speak": True,
        }

    def _handle_shopping_clear(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Clear the entire shopping list.

        Fetches and deletes all items.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/shopping")
            items: list[dict[str, Any]] = data.get("items", [])
            for item in items:
                self._request("DELETE", f"/api/shopping/{item['id']}")
            return {
                "status": "ok",
                "confirmation": f"Shopping list cleared. {len(items)} items removed.",
                "speak": True,
            }
        except Exception:
            return {
                "status": "error",
                "confirmation": "Failed to clear the shopping list.",
                "speak": True,
            }

    def _handle_identify_room(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Identify which satellite/room is processing this request.

        No API call — the room name comes from the satellite's MQTT
        header, passed through the pipeline as display_target.
        """
        # display_target is "all" for untargeted queries, but the
        # room is passed through the pipeline's room parameter.
        # The executor receives room via the intent's target, but
        # the actual room comes from the pipeline caller.
        # We store it on self during execute() dispatch.
        room: str = getattr(self, "_current_room", "unknown")
        return {
            "status": "ok",
            "confirmation": f"You are in the {room}.",
            "speak": True,
        }

    # ------------------------------------------------------------------
    # Voice gate handlers — enable/disable the doorbell (and any other
    # gated satellite) from an interior trusted room, time-bounded.
    # ------------------------------------------------------------------

    # Words that spell durations — allow "two hours", "2 hours", etc.
    # Kept as a class constant so the handler is pure and testable.
    _NUMBER_WORDS: dict[str, int] = {
        "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
        "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
        "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
        "twenty": 20, "thirty": 30, "forty": 40, "fortyfive": 45,
        "forty-five": 45, "sixty": 60, "ninety": 90,
    }

    @classmethod
    def _parse_duration_seconds(
        cls, params: dict[str, Any],
    ) -> int:
        """Extract a duration in seconds from intent params.

        Accepts any of the following shapes (first match wins):
        - ``duration_seconds`` (int or numeric string)
        - ``duration_minutes``
        - ``duration_hours``
        - ``duration`` as a free-form string like ``"two hours"``,
          ``"30 minutes"``, ``"10 min"``, ``"1h"``, ``"90s"``

        Returns 0 when no recognizable duration is present so the
        caller can reject the request.  Zero is deliberately returned
        for missing OR malformed input — the handler must not invent
        a default, per the approved design.
        """
        # Primary path: explicit seconds.
        if "duration_seconds" in params:
            try:
                return max(0, int(params["duration_seconds"]))
            except (TypeError, ValueError):
                return 0

        if "duration_minutes" in params:
            try:
                return max(0, int(params["duration_minutes"]) * 60)
            except (TypeError, ValueError):
                return 0

        if "duration_hours" in params:
            try:
                return max(0, int(params["duration_hours"]) * 3600)
            except (TypeError, ValueError):
                return 0

        raw: Any = params.get("duration")
        if not isinstance(raw, str) or not raw.strip():
            return 0

        text: str = raw.strip().lower()
        # Tokenize on whitespace; handle "1h", "30m", "90s" as units.
        tokens: list[str] = text.replace(",", " ").split()
        value: int = 0
        unit_multiplier: int = 0

        for tok in tokens:
            # Compact forms: "2h", "30m", "90s".
            if tok.endswith("h") and tok[:-1].isdigit():
                return int(tok[:-1]) * 3600
            if tok.endswith("m") and tok[:-1].isdigit():
                return int(tok[:-1]) * 60
            if tok.endswith("s") and tok[:-1].isdigit():
                return int(tok[:-1])

            if tok.isdigit():
                value = int(tok)
                continue
            if tok in cls._NUMBER_WORDS:
                value = cls._NUMBER_WORDS[tok]
                continue
            if tok.startswith("hour"):
                unit_multiplier = 3600
            elif tok.startswith("min"):
                unit_multiplier = 60
            elif tok.startswith("sec"):
                unit_multiplier = 1

        if value > 0 and unit_multiplier > 0:
            return value * unit_multiplier
        return 0

    def _gate_slug_for_target(self, target_raw: str) -> str:
        """Map an intent target onto a gate room slug.

        The intent parser returns targets like ``"doorbell"``,
        ``"porch"``, ``"front porch"``, etc.  The satellite's gate
        topic uses the slug of its room name — Doorbell → ``doorbell``.
        We treat "porch" and "doorbell" as aliases for the Front
        Doorbell gate.  Any other target becomes its own slug so
        future gated rooms work without code changes.
        """
        t: str = (target_raw or "").strip().lower()
        if t in ("doorbell", "porch", "front porch", "front door",
                 "front doorbell"):
            return "doorbell"
        return t.replace(" ", "_")

    def _handle_enable_voice_gate(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Enable a gated satellite's mic for a bounded duration.

        Enforces three rules from the approved design:

        - The originating room must be in
          :data:`voice.constants.VOICE_GATE_ALLOWED_ROOMS`.  This
          stops a gated exterior satellite from enabling itself.
        - A duration is REQUIRED.  Missing/zero duration is a parse
          error, not a silent default — the satellite speaks back
          "how long?" and the caller re-issues with a duration.
        - The duration is clamped to
          :data:`voice.constants.VOICE_GATE_MAX_SECONDS`.  Any
          longer request is trimmed and the spoken reply says so.
        """
        room: str = getattr(self, "_current_room", "unknown")
        if room not in C.VOICE_GATE_ALLOWED_ROOMS:
            logger.warning(
                "Voice gate enable rejected: room %r not in allowlist",
                room,
            )
            return {
                "status": "error",
                "confirmation": (
                    "I can't enable the porch from here."
                ),
                "speak": True,
            }

        requested: int = self._parse_duration_seconds(params)
        if requested <= 0:
            return {
                "status": "error",
                "confirmation": (
                    "How long should I open the porch for?"
                ),
                "speak": True,
            }

        clamped: int = min(requested, C.VOICE_GATE_MAX_SECONDS)
        was_clamped: bool = clamped < requested

        slug: str = self._gate_slug_for_target(target_raw)
        topic: str = f"{C.TOPIC_VOICE_GATE_PREFIX}/{slug}"
        expires_at: float = time.time() + float(clamped)

        if self._mqtt_client is None:
            logger.error(
                "Gate enable requested but no MQTT client wired",
            )
            return {
                "status": "error",
                "confirmation": (
                    "I can't reach the satellites right now."
                ),
                "speak": True,
            }

        payload: bytes = json.dumps({
            "enabled": True,
            "expires_at": expires_at,
        }).encode("utf-8")

        try:
            self._mqtt_client.publish(topic, payload, qos=1, retain=True)
        except Exception as exc:
            logger.error("Gate enable publish failed: %s", exc)
            return {
                "status": "error",
                "confirmation": "Publishing to the gate failed.",
                "speak": True,
            }

        logger.info(
            "Gate enabled on %s for %ds from room %r (requested=%ds, clamped=%s)",
            topic, clamped, room, requested, was_clamped,
        )

        if was_clamped:
            confirmation: str = (
                "OK, opening the porch for two hours — "
                "I can't do longer."
            )
        else:
            confirmation = self._format_gate_duration(clamped)

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_disable_voice_gate(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Close a gated satellite's gate immediately.

        No allowlist check — closing is always safe.  Publishes a
        retained ``{"enabled": false, "expires_at": 0}`` message so
        a reconnecting satellite picks up the closed state.
        """
        slug: str = self._gate_slug_for_target(target_raw)
        topic: str = f"{C.TOPIC_VOICE_GATE_PREFIX}/{slug}"

        if self._mqtt_client is None:
            logger.error(
                "Gate disable requested but no MQTT client wired",
            )
            return {
                "status": "error",
                "confirmation": (
                    "I can't reach the satellites right now."
                ),
                "speak": True,
            }

        payload: bytes = json.dumps({
            "enabled": False,
            "expires_at": 0,
        }).encode("utf-8")

        try:
            self._mqtt_client.publish(topic, payload, qos=1, retain=True)
        except Exception as exc:
            logger.error("Gate disable publish failed: %s", exc)
            return {
                "status": "error",
                "confirmation": "Publishing to the gate failed.",
                "speak": True,
            }

        logger.info("Gate closed on %s", topic)
        return {
            "status": "ok",
            "confirmation": "Porch closed.",
            "speak": True,
        }

    @staticmethod
    def _format_gate_duration(seconds: int) -> str:
        """Render a gate duration as a spoken confirmation."""
        if seconds % 3600 == 0 and seconds >= 3600:
            h: int = seconds // 3600
            noun: str = "hour" if h == 1 else "hours"
            return f"Opening the porch for {h} {noun}."
        if seconds % 60 == 0 and seconds >= 60:
            m: int = seconds // 60
            noun = "minute" if m == 1 else "minutes"
            return f"Opening the porch for {m} {noun}."
        return f"Opening the porch for {seconds} seconds."

    # ------------------------------------------------------------------
    # Commands / help / list handlers
    # ------------------------------------------------------------------

    def _handle_commands(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Top-level command summary — speaks categories."""
        return {
            "status": "ok",
            "confirmation": (
                "I can control lights, answer questions about sensors "
                "and weather, manage your shopping list, check home "
                "security, and have a conversation. "
                "Say 'help lights', 'help sensors', 'help shopping', "
                "'help security', or 'help system' for details."
            ),
            "speak": True,
        }

    def _handle_help_lights(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Help for lighting commands."""
        return {
            "status": "ok",
            "confirmation": (
                "I can turn lights on or off, set brightness, "
                "change color or color temperature, play effects, "
                "and stop them. You can target a single light, "
                "a group, or say 'all'. "
                "Say 'list groups' to hear your groups."
            ),
            "speak": True,
        }

    def _handle_help_shopping(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Help for shopping list commands."""
        return {
            "status": "ok",
            "confirmation": (
                "I can add items to the shopping list, remove items, "
                "read the list, or clear it. "
                "For example, say 'add milk to the shopping list' "
                "or 'what's on the shopping list'."
            ),
            "speak": True,
        }

    def _handle_help_security(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Help for home security commands."""
        return {
            "status": "ok",
            "confirmation": (
                "I can check if doors are locked, if any doors are open, "
                "the alarm status, and battery levels. "
                "Say 'list locks' or 'list doors' to hear the names."
            ),
            "speak": True,
        }

    def _handle_help_system(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Help for system commands."""
        return {
            "status": "ok",
            "confirmation": (
                "I can check system status, report uptime, "
                "restart an adapter, change my voice, "
                "tell time and date, check the schedule, "
                "check the printer, and flush pending requests."
            ),
            "speak": True,
        }

    def _handle_help_sensors(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Help for sensor commands."""
        return {
            "status": "ok",
            "confirmation": (
                "I can read temperature and humidity from indoor sensors, "
                "check soil moisture, report power usage from smart plugs, "
                "and get the weather. "
                "Say 'list sensors' to hear what sensors are available."
            ),
            "speak": True,
        }

    def _handle_list_sensors(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Enumerate available sensors from live API data."""
        parts: list[str] = []

        # BLE sensors (temperature, humidity).
        try:
            ble_data: dict[str, Any] = self._request("GET", "/api/ble/sensors")
            if ble_data:
                names: list[str] = list(ble_data.keys())
                parts.append(
                    f"Indoor sensors: {', '.join(names)}"
                )
        except Exception:
            parts.append("Indoor sensors are not responding")

        # Soil moisture sensors (from actions.yml zones).
        soil_cfg: dict[str, Any] | None = self._actions.get("query_soil")
        if soil_cfg:
            zones: dict[str, Any] = soil_cfg.get("zones", {})
            if zones:
                zone_names: list[str] = list(zones.keys())
                parts.append(
                    f"Soil sensors: {', '.join(zone_names)}"
                )

        # Power monitors (Zigbee smart plugs).
        try:
            power_data: dict[str, Any] = self._request(
                "GET", "/api/power/devices",
            )
            raw_devices: Any = power_data.get("devices", [])
            # API returns a list of device name strings.
            if isinstance(raw_devices, list) and raw_devices:
                parts.append(
                    f"Power monitors: {', '.join(str(d) for d in raw_devices)}"
                )
        except Exception:
            pass

        if not parts:
            confirmation: str = "No sensors are available right now."
        else:
            confirmation = ". ".join(parts) + "."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_list_groups(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Enumerate light groups from live API data."""
        try:
            data: dict[str, Any] = self._request("GET", "/api/groups")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the lighting system.",
                "speak": True,
            }

        groups_dict: dict[str, Any] = data.get("groups", {})
        group_names: list[str] = [
            name for name in groups_dict.keys() if name != "all"
        ]
        if not group_names:
            confirmation: str = "No groups are configured."
        else:
            confirmation = f"Your groups are: {', '.join(group_names)}."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_list_doors(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Enumerate door sensors from live API data."""
        try:
            data: dict[str, Any] = self._request("GET", "/api/home/security")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the security system.",
                "speak": True,
            }

        doors: list[dict[str, Any]] = data.get("doors", [])
        if not doors:
            confirmation: str = "No door sensors are configured."
        else:
            names: list[str] = [d.get("name", "unknown") for d in doors]
            confirmation = f"Your door sensors are: {', '.join(names)}."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_list_locks(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Enumerate locks from live API data."""
        try:
            data: dict[str, Any] = self._request("GET", "/api/home/locks")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the lock system.",
                "speak": True,
            }

        locks: list[dict[str, Any]] = data.get("locks", [])
        if not locks:
            confirmation: str = "No locks are configured."
        else:
            names: list[str] = [lk.get("name", "unknown") for lk in locks]
            confirmation = f"Your locks are: {', '.join(names)}."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    # ------------------------------------------------------------------
    # Chat — freeform Ollama conversation (the one special case)
    # ------------------------------------------------------------------

    def _get_chat_history(self, room: str) -> list[dict[str, str]]:
        """Get conversation history for a room, expiring stale sessions.

        Args:
            room: Room name for history isolation.

        Returns:
            List of message dicts.
        """
        now: float = time.time()
        last_active: float = self._chat_timestamps.get(room, 0.0)

        if now - last_active > _CHAT_HISTORY_TTL_S:
            self._chat_history.pop(room, None)

        self._chat_timestamps[room] = now

        if room not in self._chat_history:
            self._chat_history[room] = []

        return self._chat_history[room]

    def _exec_chat(
        self, message: str, room: str,
    ) -> dict[str, Any]:
        """Handle freeform chat via Ollama with per-room history.

        Args:
            message: The user's spoken message.
            room:    Room name for history isolation.

        Returns:
            Dict with spoken confirmation.
        """
        if not message.strip():
            return {
                "status": "error",
                "confirmation": "I didn't catch that.",
                "speak": True,
            }

        history: list[dict[str, str]] = self._get_chat_history(room)

        messages: list[dict[str, str]] = [
            {"role": "system", "content": _CHAT_SYSTEM_PROMPT},
        ]
        messages.extend(history)
        messages.append({"role": "user", "content": message})

        payload: dict[str, Any] = {
            "model": self._chat_model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.7,
                "num_predict": 120,
            },
        }

        body: bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self._ollama_host}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            t0: float = time.time()
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                reply: str = (
                    result.get("message", {}).get("content", "").strip()
                )
            elapsed: float = time.time() - t0
            logger.info(
                "Chat response in %.2fs (%d chars): '%s'",
                elapsed, len(reply), reply[:80],
            )

            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": reply})

            while len(history) > _CHAT_HISTORY_MAX * 2:
                history.pop(0)
                history.pop(0)

            return {
                "status": "ok",
                "confirmation": reply,
                "speak": True,
            }
        except Exception as exc:
            logger.error("Chat failed: %s", exc)
            return {
                "status": "error",
                "confirmation": "Sorry, I couldn't think of a response.",
                "speak": True,
            }
